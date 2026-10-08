"""Parse → synthesize each row → assemble episode/character tracks → ZIP."""

from collections import defaultdict
from pathlib import Path
import tempfile

from .audio_builder import build_track, read_clip
from .excel_parser import parse_excel
from .exporter import export_archive, track_names, write_report
from .tts_client import ElevenLabsClient


def run_pipeline(excel_path, output_path, *, fps=25, sheet_name=None,
                 synthesize=None, progress=print, progress_state=None):
    # All spreadsheet validation happens before constructing the API client.
    rows = parse_excel(excel_path, fps=fps, sheet_name=sheet_name)
    output_path = Path(output_path)
    if output_path.suffix.lower() != ".zip":
        raise ValueError("Output must be a .zip file")
    if output_path.exists():
        raise ValueError(f"Output already exists: {output_path}; choose a new filename")
    synthesize = synthesize if synthesize is not None else ElevenLabsClient()
    output_path.parent.mkdir(parents=True, exist_ok=True)
    groups, episode_ends = defaultdict(list), defaultdict(int)
    records, clip_lengths = [], {}
    names = track_names({row.group for row in rows})
    def update_state(percent, phase):
        if progress_state is not None:
            progress_state({"percent": percent, "phase": phase})

    update_state(0, "Generating audio")
    progress(f"Validated {len(rows)} dialogue rows; {len(names)} tracks; {fps} fps")
    with tempfile.TemporaryDirectory(prefix="ad-v2-", dir=output_path.parent) as workspace:
        workspace = Path(workspace)
        for index, row in enumerate(rows, start=1):
            progress(f"Generating {index}/{len(rows)}: Excel row {row.row_number}, episode {row.episode}, {row.character}")
            try:
                wav = synthesize(row.text, row.voice_id)
                duration = len(read_clip(wav))
            except Exception as exc:
                raise RuntimeError(f"Excel row {row.row_number}: {exc}") from exc
            clip_path = workspace / f"row_{row.row_number}.wav"
            clip_path.write_bytes(wav)
            clip_lengths[row.row_number] = duration
            groups[row.group].append((row, clip_path))
            episode_ends[row.episode] = max(episode_ends[row.episode], row.end_ms,
                                            row.start_ms + duration)
            update_state(round(index / len(rows) * 90), "Generating audio")
        for group, items in groups.items():
            previous_end = 0
            for row, _ in sorted(items, key=lambda item: (item[0].start_ms, item[0].row_number)):
                duration = clip_lengths[row.row_number]
                overrun = max(0, row.start_ms + duration - row.end_ms)
                overlap = max(0, min(previous_end - row.start_ms, duration))
                previous_end = max(previous_end, row.start_ms + duration)
                flags = []
                if overrun:
                    flags.append("TCR_OUT_OVERRUN")
                if overlap:
                    flags.append("SAME_CHARACTER_OVERLAP")
                if flags:
                    progress(f"Review Excel row {row.row_number}: {', '.join(flags)}")
                records.append(dict(excel_row=row.row_number, episode=row.episode,
                                    character=row.character, voice_id=row.voice_id,
                                    start_ms=row.start_ms, end_ms=row.end_ms,
                                    actual_duration_ms=duration, overrun_ms=overrun,
                                    same_character_overlap_ms=overlap,
                                    status=";".join(flags) or "OK", track=names[group]))
        files = []
        update_state(90, "Building character tracks")
        for index, group in enumerate(sorted(groups), start=1):
            progress(f"Building {names[group]}")
            track_path = workspace / names[group]
            build_track(sorted(groups[group], key=lambda item: item[0].start_ms),
                        episode_ends[group[0]], track_path)
            files.append(track_path)
            update_state(90 + round(index / len(groups) * 9), "Building character tracks")
        report_path = workspace / "processing_report.csv"
        write_report(report_path, sorted(records, key=lambda record: record["excel_row"]))
        update_state(99, "Packaging download")
        export_archive(output_path, files + [report_path])
    summary = {"output": str(output_path), "rows": len(rows), "tracks": len(groups),
               "flagged_rows": sum(record["status"] != "OK" for record in records)}
    progress(f"Complete: {summary['tracks']} tracks, {summary['flagged_rows']} rows flagged for review")
    return summary
