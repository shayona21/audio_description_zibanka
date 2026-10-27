"""Parse → synthesize each row → fit each line to its slot → assemble episode/character tracks → ZIP.

ZIP layout (read by the Dubbing Quality Check when it imports a run):
  tracks/Episode_<ep>_<character>.wav   one full-length timeline per <character, episode>
  lines/<ep>_<character>_<row>.wav      one clip per dubbed row (the same audio as in the track)
  Episode_<ep>_timeline.csv             combined timeline per episode
  processing_report.csv                 one record per row, including the fit speed
  script.xlsx                           copy of the input workbook
"""

from collections import defaultdict
from pathlib import Path
import shutil
import tempfile

from .audio_builder import build_track, read_clip
from .audio_speed import check_ffmpeg, fit_to_slot
from .excel_parser import parse_excel
from .exporter import (LINES_DIR, SCRIPT_NAME, TRACKS_DIR, export_archive, format_tcr, line_name, track_names,
                       timeline_names, write_report, write_timeline)
from .timing import place_dialogue
from .tts_client import ElevenLabsClient


def run_pipeline(excel_path, output_path, *, fps=25, sheet_name=None, synthesize=None,
                 progress=print, progress_state=None, fit_lines=True):
    """``fit_lines=False`` skips pass 2 (each clip keeps its ElevenLabs length); tests use it to
    check the placement rules on their own."""
    # All spreadsheet validation happens before constructing the API client.
    rows = parse_excel(excel_path, fps=fps, sheet_name=sheet_name)
    output_path = Path(output_path)
    if output_path.suffix.lower() != ".zip":
        raise ValueError("Output must be a .zip file")
    if output_path.exists():
        raise ValueError(f"Output already exists: {output_path}; choose a new filename")
    if fit_lines:
        check_ffmpeg()  # before any ElevenLabs request is paid for
    synthesize = synthesize if synthesize is not None else ElevenLabsClient()
    output_path.parent.mkdir(parents=True, exist_ok=True)
    groups, episode_ends = defaultdict(list), defaultdict(int)
    episode_timelines = defaultdict(list)
    records, clip_lengths, clip_paths, fit_speeds = [], {}, {}, {}
    names = track_names({row.group for row in rows})
    def update_state(percent, phase):
        if progress_state is not None:
            progress_state({"percent": percent, "phase": phase})

    update_state(0, "Generating audio")
    progress(f"Validated {len(rows)} dialogue rows; {len(names)} tracks; {fps} fps")
    with tempfile.TemporaryDirectory(prefix="ad-v2-", dir=output_path.parent) as workspace:
        workspace = Path(workspace)
        (workspace / TRACKS_DIR).mkdir()
        (workspace / LINES_DIR).mkdir()
        for index, row in enumerate(rows, start=1):
            progress(f"Generating {index}/{len(rows)}: Excel row {row.row_number}, episode {row.episode}, {row.character}")
            try:
                wav = synthesize(row.text, row.voice_id)
                speed = 1.0
                if fit_lines:
                    wav, speed = fit_to_slot(wav, row.end_ms - row.start_ms)
                duration = len(read_clip(wav))
            except Exception as exc:
                raise RuntimeError(f"Excel row {row.row_number}: {exc}") from exc
            if speed != 1.0:
                progress(f"Fitted Excel row {row.row_number} to its slot at {speed:.2f}x")
            clip_path = workspace / LINES_DIR / line_name(row)
            clip_path.write_bytes(wav)
            clip_lengths[row.row_number] = duration
            clip_paths[row.row_number] = clip_path
            fit_speeds[row.row_number] = speed
            update_state(round(index / len(rows) * 90), "Generating audio")
        progress("Resolving audio collisions independently for each episode and character")
        for placed in place_dialogue(rows, clip_lengths):
            row = placed.row
            groups[row.group].append((placed, clip_paths[row.row_number]))
            episode_timelines[row.episode].append(placed)
            episode_ends[row.episode] = max(episode_ends[row.episode], row.end_ms, placed.end_ms)
        for group, items in groups.items():
            for placed, _ in items:
                row = placed.row
                duration = placed.end_ms - placed.start_ms
                scripted_duration = row.end_ms - row.start_ms
                overrun = max(0, duration - scripted_duration)
                status = "TCR_OUT_OVERRUN" if overrun else "OK"
                if overrun:
                    progress(f"Review Excel row {row.row_number}: {status}")
                records.append(dict(excel_row=row.row_number, episode=row.episode,
                                    character=row.character, voice_id=row.voice_id,
                                    start_TCR=format_tcr(row.start_ms, fps),
                                    end_TCR=format_tcr(row.end_ms, fps),
                                    duration=scripted_duration,
                                    actual_duration_ms=duration, overrun_amnt_ms=overrun,
                                    status=status, track=names[group],
                                    adjusted_start_TCR=format_tcr(placed.start_ms, fps),
                                    adjusted_end_TCR=format_tcr(placed.end_ms, fps),
                                    fit_speed=f"{fit_speeds[row.row_number]:.2f}"))
        files = list(clip_paths.values())
        update_state(90, "Building character tracks")
        for index, group in enumerate(sorted(groups), start=1):
            progress(f"Building {names[group]}")
            track_path = workspace / TRACKS_DIR / names[group]
            build_track(groups[group], episode_ends[group[0]], track_path)
            files.append(track_path)
            update_state(90 + round(index / len(groups) * 9), "Building character tracks")
        for episode, name in timeline_names(episode_timelines).items():
            timeline_path = workspace / name
            write_timeline(timeline_path, episode_timelines[episode])
            files.append(timeline_path)
        report_path = workspace / "processing_report.csv"
        write_report(report_path, sorted(records, key=lambda record: record["excel_row"]))
        script_copy = workspace / SCRIPT_NAME
        shutil.copyfile(excel_path, script_copy)
        update_state(99, "Packaging download")
        export_archive(output_path, files + [report_path, script_copy], root=workspace)
    summary = {"output": str(output_path), "rows": len(rows), "tracks": len(groups),
               "flagged_rows": sum(record["status"] != "OK" for record in records),
               "fitted_rows": sum(speed != 1.0 for speed in fit_speeds.values())}
    progress(f"Complete: {summary['tracks']} tracks, {summary['fitted_rows']} rows fitted to their slot, "
             f"{summary['flagged_rows']} rows flagged for review")
    return summary
