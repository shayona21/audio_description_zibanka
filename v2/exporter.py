import csv
import re
import zipfile


def safe_component(value):
    value = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", value).strip(" .")
    return value[:70].rstrip(" .") or "unnamed"


# ZIP layout. Quality Check reads these names when it imports a run, so keep both tools in step.
TRACKS_DIR = "tracks"          # one full-length timeline WAV per <character, episode>
LINES_DIR = "lines"            # one WAV per dubbed row
SCRIPT_NAME = "script.xlsx"    # copy of the input workbook


def line_name(row):
    """``<episode>_<character>_<Excel row>.wav``; the row number makes every name unique."""
    return f"{safe_component(row.episode)}_{safe_component(row.character)}_{row.row_number}.wav"


def track_names(groups):
    names, used = {}, set()
    for episode, character in sorted(groups):
        stem = f"Episode_{safe_component(episode)}_{safe_component(character)}"
        name, suffix = f"{stem}.wav", 2
        while name.casefold() in used:
            name = f"{stem}_{suffix}.wav"
            suffix += 1
        used.add(name.casefold())
        names[(episode, character)] = name
    return names


REPORT_FIELDS = ("excel_row", "episode", "character", "voice_id", "start_TCR", "end_TCR",
                 "duration", "actual_duration_ms", "overrun_amnt_ms", "status", "track",
                 "adjusted_start_TCR", "adjusted_end_TCR", "fit_speed")

TIMELINE_FIELDS = ("Dialogue", "Original time start", "Original time end",
                   "Adjusted time start", "Adjusted time end", "Delay")


def timeline_names(episodes):
    names, used = {}, set()
    for episode in sorted(episodes):
        stem = f"Episode_{safe_component(episode)}_timeline"
        name, suffix = f"{stem}.csv", 2
        while name.casefold() in used:
            name = f"{stem}_{suffix}.csv"
            suffix += 1
        used.add(name.casefold())
        names[episode] = name
    return names


def format_timestamp(milliseconds):
    seconds, milliseconds = divmod(milliseconds, 1000)
    minutes, seconds = divmod(seconds, 60)
    hours, minutes = divmod(minutes, 60)
    return f"{hours:02d}:{minutes:02d}:{seconds:02d}.{milliseconds:03d}"


def format_tcr(milliseconds, fps):
    """Display millisecond timing at the nearest frame, with ties rounded up.

    Round total frames first so rollover never produces an invalid frame field.
    This affects display only, not audio placement or duration calculations.
    """
    total_frames = (milliseconds * fps + 500) // 1000
    seconds, frames = divmod(total_frames, fps)
    minutes, seconds = divmod(seconds, 60)
    hours, minutes = divmod(minutes, 60)
    return f"{hours:02d}:{minutes:02d}:{seconds:02d}:{frames:02d}"


def write_timeline(path, placements):
    records = []
    for placed in sorted(placements, key=lambda p: (p.start_ms, p.row.row_number)):
        row = placed.row
        records.append({
            "Dialogue": f"{row.character}: {row.text}",
            "Original time start": format_timestamp(row.start_ms),
            "Original time end": format_timestamp(row.end_ms),
            "Adjusted time start": format_timestamp(placed.start_ms),
            "Adjusted time end": format_timestamp(placed.end_ms),
            "Delay": f"{placed.delay_ms / 1000:.3f} s",
        })
    write_csv(path, records, TIMELINE_FIELDS)


def write_report(path, records):
    write_csv(path, records, REPORT_FIELDS)


def write_csv(path, records, fields):
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        for record in records:
            # Prevent spreadsheet formulas when opening the report in Excel.
            safe = {key: ("'" + value if isinstance(value, str)
                          and value.lstrip().startswith(("=", "+", "-", "@")) else value)
                    for key, value in record.items()}
            writer.writerow(safe)


def export_archive(output_path, files, root=None):
    """Write ``files`` to a ZIP atomically. With ``root``, folders below it are kept in the ZIP
    (``root/tracks/x.wav`` -> ``tracks/x.wav``); without it every file sits at the top level."""
    temporary = output_path.with_suffix(".zip.partial")
    try:
        with zipfile.ZipFile(temporary, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            for path in files:
                archive.write(path, arcname=path.relative_to(root).as_posix() if root else path.name)
        temporary.replace(output_path)
    finally:
        temporary.unlink(missing_ok=True)
