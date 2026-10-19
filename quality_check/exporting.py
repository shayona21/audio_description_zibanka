"""Block 6: package the reviewed episode in the same layout AD version 2 delivers.

The ZIP holds:
  Episode_<ep>_<character>.wav   one per character, as reviewed (edits and line speeds included)
  Episode_<ep>_timeline.csv      V2's combined timeline, recomputed for the reviewed audio
  processing_report.csv          V2's report, same columns, for the reviewed audio
  <script>_QC.xlsx               the uploaded workbook with edited lines written into TARGET DIALOGUE;
                                 changed cells are highlighted and carry a note with the script wording
  qc_changes.csv                 one row per line changed in review (text, line speed)

File names and CSV formats come from ``v2.exporter`` so downstream steps can treat this ZIP like
an AD version 2 output.
"""

from pathlib import Path
import shutil
import tempfile

from openpyxl import load_workbook
from openpyxl.comments import Comment
from openpyxl.styles import PatternFill

from v2.audio_builder import read_clip
from v2.exporter import (export_archive, format_tcr, safe_component, timeline_names, track_names,
                         write_csv, write_report, write_timeline)
from v2.timing import place_dialogue

from .dubbing import clip_path

CHANGED_FILL = PatternFill("solid", fgColor="FFF2CC")  # Excel's pale yellow "changed" convention
CHANGE_FIELDS = ("excel_row", "character", "start_TCR", "script_dialogue", "reviewed_dialogue", "line_speed")


def edited_workbook(source, target, sheet_name, edits, speeds):
    """Copy the workbook, writing reviewed text into TARGET DIALOGUE. Nothing else is touched."""
    book = load_workbook(source)  # full mode: keeps formatting, other sheets and columns as they are
    try:
        sheet = book[sheet_name] if sheet_name else book.worksheets[0]
        headers = [" ".join(str(c.value or "").upper().split()) for c in sheet[1]]
        column = headers.index("TARGET DIALOGUE") + 1
        for row_key in sorted(set(edits) | set(speeds), key=int):
            cell = sheet.cell(row=int(row_key), column=column)
            notes = []
            if row_key in edits:
                cell.value = edits[row_key]["text"]
                notes.append(f"Script wording: {edits[row_key]['original']}")
            if row_key in speeds:
                notes.append(f"Line sped up to {speeds[row_key]:.2f}x in quality check.")
            cell.fill = CHANGED_FILL
            cell.comment = Comment("\n".join(notes), "Dubbing Quality Check", width=320, height=110)
        book.save(target)
    finally:
        book.close()


def build_export(folder, record, rows, out_path):
    """Write the delivery ZIP for a reviewed session to ``out_path``."""
    folder = Path(folder)
    fps = record["fps"]
    edits = record.get("edits") or {}
    speeds = record.get("line_speeds") or {}
    episode = record["summary"]["episode"]
    index = {c["name"]: i for i, c in enumerate(record["summary"]["characters"])}

    # Placements for the audio as it is now (same rule and clip lengths the tracks were built from).
    lengths = {row.row_number: len(read_clip(clip_path(folder, row.row_number).read_bytes())) for row in rows}
    placed = place_dialogue(rows, lengths)
    names = track_names({row.group for row in rows})

    with tempfile.TemporaryDirectory(dir=folder) as work:
        work = Path(work)
        files = []
        for (ep, character), name in sorted(names.items()):
            target = work / name
            shutil.copyfile(folder / "tracks" / f"{index[character]}.wav", target)
            files.append(target)

        timeline = work / timeline_names({episode})[episode]
        write_timeline(timeline, placed)
        files.append(timeline)

        records = []
        for p in placed:
            row = p.row
            duration = p.end_ms - p.start_ms
            scripted = row.end_ms - row.start_ms
            overrun = max(0, duration - scripted)
            records.append(dict(excel_row=row.row_number, episode=row.episode, character=row.character,
                                voice_id=row.voice_id, start_TCR=format_tcr(row.start_ms, fps),
                                end_TCR=format_tcr(row.end_ms, fps), duration=scripted,
                                actual_duration_ms=duration, overrun_amnt_ms=overrun,
                                status="TCR_OUT_OVERRUN" if overrun else "OK", track=names[row.group],
                                adjusted_start_TCR=format_tcr(p.start_ms, fps),
                                adjusted_end_TCR=format_tcr(p.end_ms, fps)))
        report = work / "processing_report.csv"
        write_report(report, sorted(records, key=lambda r: r["excel_row"]))
        files.append(report)

        stem = safe_component(Path(record["script"]["name"]).stem) or "script"
        workbook = work / f"{stem}_QC.xlsx"
        edited_workbook(folder / "script.xlsx", workbook, record.get("sheet"), edits, speeds)
        files.append(workbook)

        by_row = {row.row_number: row for row in rows}
        changes = []
        for key in sorted(set(edits) | set(speeds), key=int):
            row = by_row[int(key)]
            changes.append({"excel_row": row.row_number, "character": row.character,
                            "start_TCR": format_tcr(row.start_ms, fps),
                            "script_dialogue": edits[key]["original"] if key in edits else row.text,
                            "reviewed_dialogue": row.text,
                            "line_speed": f"{speeds.get(key, 1.0):.2f}x"})
        change_file = work / "qc_changes.csv"
        write_csv(change_file, changes, CHANGE_FIELDS)
        files.append(change_file)

        out_path = Path(out_path)
        export_archive(out_path, files)
    return {"name": f"Episode_{safe_component(episode)}_QC.zip", "changes": len(changes),
            "tracks": len(names), "workbook": workbook.name}
