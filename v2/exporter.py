import csv
import re
import zipfile


def safe_component(value):
    value = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", value).strip(" .")
    return value[:70].rstrip(" .") or "unnamed"


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


REPORT_FIELDS = ("excel_row", "episode", "character", "voice_id", "start_ms", "end_ms",
                 "actual_duration_ms", "overrun_ms", "same_character_overlap_ms", "status", "track")


def write_report(path, records):
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=REPORT_FIELDS)
        writer.writeheader()
        for record in records:
            # Prevent spreadsheet formulas when opening the report in Excel.
            safe = {key: ("'" + value if isinstance(value, str)
                          and value.lstrip().startswith(("=", "+", "-", "@")) else value)
                    for key, value in record.items()}
            writer.writerow(safe)


def export_archive(output_path, files):
    temporary = output_path.with_suffix(".zip.partial")
    try:
        with zipfile.ZipFile(temporary, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            for path in files:
                archive.write(path, arcname=path.name)
        temporary.replace(output_path)
    finally:
        temporary.unlink(missing_ok=True)
