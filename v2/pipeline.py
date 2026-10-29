"""Parse → synthesize each row (pass 1) → fit each line to its slot (pass 2) → place → tracks → ZIP.

ZIP layout (Quality Check reads it when it imports a run). Names are lowercase, spaces as "_":
  pass1/lines/ep<ep>_<character>_row<row>.wav   ElevenLabs as delivered, one per dubbed row
  pass1/tracks/ep<ep>_<character>.wav           one timeline per <character, episode>, from pass 1 lines
  pass2/lines/ep<ep>_<character>_row<row>.wav   the same lines fitted to their slots with FFmpeg
  pass2/tracks/ep<ep>_<character>.wav           one timeline per <character, episode>, from pass 2 lines
  ep<ep>_timeline.csv                           combined timeline per episode (pass 2)
  processing_report.csv                         one record per row, pass 1 and pass 2 side by side
  script.xlsx                                   copy of the input workbook

Both passes are placed with the same rule (``place_dialogue``), so each pass's tracks show exactly
how that pass would play.
"""

from collections import defaultdict
from pathlib import Path
import shutil
import tempfile

from .audio_builder import build_track, read_clip
from .audio_speed import check_ffmpeg, fit_to_slot
from .excel_parser import parse_excel
from .exporter import (LINES_DIR, PASS1_DIR, PASS2_DIR, SCRIPT_NAME, TRACKS_DIR, export_archive, format_tcr,
                       line_name, track_names, timeline_names, write_report, write_timeline)
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
    names = track_names({row.group for row in rows})
    lengths = {PASS1_DIR: {}, PASS2_DIR: {}}   # pass -> {row: clip length in ms}
    clip_paths = {PASS1_DIR: {}, PASS2_DIR: {}}
    fit_speeds = {}

    def update_state(percent, phase):
        if progress_state is not None:
            progress_state({"percent": percent, "phase": phase})

    update_state(0, "Generating audio")
    progress(f"Validated {len(rows)} dialogue rows; {len(names)} tracks; {fps} fps")
    with tempfile.TemporaryDirectory(prefix="ad-v2-", dir=output_path.parent) as workspace:
        workspace = Path(workspace)
        for pass_dir in (PASS1_DIR, PASS2_DIR):
            (workspace / pass_dir / LINES_DIR).mkdir(parents=True)
            (workspace / pass_dir / TRACKS_DIR).mkdir()
        for index, row in enumerate(rows, start=1):
            progress(f"Generating {index}/{len(rows)}: Excel row {row.row_number}, episode {row.episode}, {row.character}")
            try:
                take = synthesize(row.text, row.voice_id)
                take_ms = len(read_clip(take))
                fitted, speed = fit_to_slot(take, row.end_ms - row.start_ms) if fit_lines else (take, 1.0)
                fitted_ms = len(read_clip(fitted))
            except Exception as exc:
                raise RuntimeError(f"Excel row {row.row_number}: {exc}") from exc
            if speed != 1.0:
                progress(f"Fitted Excel row {row.row_number} to its slot at {speed:.2f}x")
            for pass_dir, wav, length in ((PASS1_DIR, take, take_ms), (PASS2_DIR, fitted, fitted_ms)):
                path = workspace / pass_dir / LINES_DIR / line_name(row)
                path.write_bytes(wav)
                clip_paths[pass_dir][row.row_number] = path
                lengths[pass_dir][row.row_number] = length
            fit_speeds[row.row_number] = speed
            update_state(round(index / len(rows) * 90), "Generating audio")

        progress("Resolving audio collisions independently for each episode and character")
        placements = {pass_dir: {p.row.row_number: p for p in place_dialogue(rows, lengths[pass_dir])}
                      for pass_dir in (PASS1_DIR, PASS2_DIR)}
        files = [path for pass_dir in (PASS1_DIR, PASS2_DIR) for path in clip_paths[pass_dir].values()]

        update_state(90, "Building character tracks")
        for step, pass_dir in enumerate((PASS1_DIR, PASS2_DIR)):
            groups, episode_ends = defaultdict(list), defaultdict(int)
            for placed in placements[pass_dir].values():
                row = placed.row
                groups[row.group].append((placed, clip_paths[pass_dir][row.row_number]))
                episode_ends[row.episode] = max(episode_ends[row.episode], row.end_ms, placed.end_ms)
            for index, group in enumerate(sorted(groups), start=1):
                progress(f"Building {pass_dir}/{names[group]}")
                track_path = workspace / pass_dir / TRACKS_DIR / names[group]
                build_track(groups[group], episode_ends[group[0]], track_path)
                files.append(track_path)
                update_state(90 + round((step + index / len(groups)) * 4.5), "Building character tracks")

        records = []
        episode_timelines = defaultdict(list)
        for row in rows:
            placed = placements[PASS2_DIR][row.row_number]
            episode_timelines[row.episode].append(placed)
            slot = row.end_ms - row.start_ms
            pass1_ms = lengths[PASS1_DIR][row.row_number]
            pass2_ms = placed.end_ms - placed.start_ms
            overrun = max(0, pass2_ms - slot)
            status = "TCR_OUT_OVERRUN" if overrun else "OK"
            if overrun:
                progress(f"Review Excel row {row.row_number}: {status}")
            records.append(dict(excel_row=row.row_number, episode=row.episode,
                                character=row.character, voice_id=row.voice_id,
                                start_TCR=format_tcr(row.start_ms, fps), end_TCR=format_tcr(row.end_ms, fps),
                                duration=slot,
                                pass1_duration_ms=pass1_ms, pass1_overrun_ms=max(0, pass1_ms - slot),
                                fit_speed=f"{fit_speeds[row.row_number]:.2f}",
                                pass2_duration_ms=pass2_ms, pass2_overrun_ms=overrun,
                                status=status, track=names[row.group],
                                adjusted_start_TCR=format_tcr(placed.start_ms, fps),
                                adjusted_end_TCR=format_tcr(placed.end_ms, fps)))
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
    summary = {"output": str(output_path), "rows": len(rows), "tracks": len(names),
               "flagged_rows": sum(record["status"] != "OK" for record in records),
               "fitted_rows": sum(speed != 1.0 for speed in fit_speeds.values())}
    progress(f"Complete: {summary['tracks']} tracks, {summary['fitted_rows']} rows fitted to their slot, "
             f"{summary['flagged_rows']} rows flagged for review")
    return summary
