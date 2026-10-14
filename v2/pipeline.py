"""Parse → synthesize each row → assemble episode/character tracks → ZIP."""

from collections import defaultdict
from pathlib import Path
import tempfile

from .audio_builder import build_track, read_clip
from .audio_speed import adjust_audio_speed, check_speed_support
from .excel_parser import parse_excel
from .exporter import export_archive, format_tcr, track_names, timeline_names, write_report, write_timeline
from .timing import place_dialogue
from .tts_client import ElevenLabsClient


def run_pipeline(excel_path, output_path, *, fps=25, sheet_name=None, speed=1.0,
                 synthesize=None, progress=print, progress_state=None):
    # All spreadsheet validation happens before constructing the API client.
    rows = parse_excel(excel_path, fps=fps, sheet_name=sheet_name)
    output_path = Path(output_path)
    if output_path.suffix.lower() != ".zip":
        raise ValueError("Output must be a .zip file")
    if output_path.exists():
        raise ValueError(f"Output already exists: {output_path}; choose a new filename")
    speed = check_speed_support(speed)
    synthesize = synthesize if synthesize is not None else ElevenLabsClient()
    output_path.parent.mkdir(parents=True, exist_ok=True)
    groups, episode_ends = defaultdict(list), defaultdict(int)
    episode_timelines = defaultdict(list)
    records, clip_lengths, clip_paths = [], {}, {}
    names = track_names({row.group for row in rows})
    def update_state(percent, phase):
        if progress_state is not None:
            progress_state({"percent": percent, "phase": phase})

    update_state(0, "Generating audio")
    progress(f"Validated {len(rows)} dialogue rows; {len(names)} tracks; {fps} fps")
    progress(f"Speed dial: {speed:.2f}x (pitch preserved)")
    with tempfile.TemporaryDirectory(prefix="ad-v2-", dir=output_path.parent) as workspace:
        workspace = Path(workspace)
        for index, row in enumerate(rows, start=1):
            progress(f"Generating {index}/{len(rows)}: Excel row {row.row_number}, episode {row.episode}, {row.character}")
            try:
                wav = synthesize(row.text, row.voice_id)
                wav = adjust_audio_speed(wav, speed)
                duration = len(read_clip(wav))
            except Exception as exc:
                raise RuntimeError(f"Excel row {row.row_number}: {exc}") from exc
            clip_path = workspace / f"row_{row.row_number}.wav"
            clip_path.write_bytes(wav)
            clip_lengths[row.row_number] = duration
            clip_paths[row.row_number] = clip_path
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
                                    adjusted_end_TCR=format_tcr(placed.end_ms, fps)))
        files = []
        update_state(90, "Building character tracks")
        for index, group in enumerate(sorted(groups), start=1):
            progress(f"Building {names[group]}")
            track_path = workspace / names[group]
            build_track(groups[group], episode_ends[group[0]], track_path)
            files.append(track_path)
            update_state(90 + round(index / len(groups) * 9), "Building character tracks")
        for episode, name in timeline_names(episode_timelines).items():
            timeline_path = workspace / name
            write_timeline(timeline_path, episode_timelines[episode])
            files.append(timeline_path)
        report_path = workspace / "processing_report.csv"
        write_report(report_path, sorted(records, key=lambda record: record["excel_row"]))
        update_state(99, "Packaging download")
        export_archive(output_path, files + [report_path])
    summary = {"output": str(output_path), "rows": len(rows), "tracks": len(groups),
               "flagged_rows": sum(record["status"] != "OK" for record in records)}
    progress(f"Complete: {summary['tracks']} tracks, {summary['flagged_rows']} rows flagged for review")
    return summary
