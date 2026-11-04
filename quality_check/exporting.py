"""Block 6: package the reviewed episode in the same layout AD version 2 delivers.

The ZIP holds:
  ep<ep>_<character>.wav         one per character, as reviewed (edits and line speeds included)
  ep<ep>_timeline.csv            V2's combined timeline, recomputed for the reviewed audio
  processing_report.csv          V2's report, same columns. pass1_ is V2's first take (imported runs)
                                 or the take generated here; pass1_5_ is the line's take; pass2_ is the
                                 audio as reviewed (pass 2's speed or the reviewer's)
  <script>_qc.xlsx               the workbook with changed lines written into TARGET DIALOGUE; changed
                                 cells are highlighted and carry a note with the script wording
  qc_changes.csv                 one row per changed line: Gemini's rewrites in AD Tool V2 and the
                                 reviewer's text and speed changes

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
from v2.audio_speed import format_sig, measure
from v2.exporter import (episode_tag, export_archive, format_tcr, safe_component, timeline_names, track_names,
                         write_csv, write_report, write_timeline)
from v2.timing import place_dialogue

from .dubbing import clip_path, take_path

CHANGED_FILL = PatternFill("solid", fgColor="FFF2CC")  # Excel's pale yellow "changed" convention
CHANGE_FIELDS = ("excel_row", "character", "start_TCR", "changed_by", "script_dialogue", "reviewed_dialogue",
                 "line_speed")


def headers_of(sheet):
    return [" ".join(str(c.value or "").upper().split()) for c in sheet[1]]


def edited_workbook(source, target, sheet_name, edits, speeds, fits):
    """Copy the workbook, writing changed text into TARGET DIALOGUE. Nothing else is touched. Without
    a worksheet name (an imported run), the first worksheet with a TARGET DIALOGUE header is used."""
    book = load_workbook(source)  # full mode: keeps formatting, other sheets and columns as they are
    try:
        if sheet_name:
            sheet = book[sheet_name]
        else:
            sheet = next((s for s in book.worksheets if "TARGET DIALOGUE" in headers_of(s)), book.worksheets[0])
        column = headers_of(sheet).index("TARGET DIALOGUE") + 1
        for row_key in sorted(set(edits) | set(speeds), key=int):
            cell = sheet.cell(row=int(row_key), column=column)
            notes = []
            if row_key in edits:
                cell.value = edits[row_key]["text"]
                if edits[row_key].get("by") == "gemini":
                    notes.append("Rewritten by Gemini in AD Tool V2 to fit its slot.")
                notes.append(f"Script wording: {edits[row_key]['original']}")
            if row_key in speeds:
                notes.append(f"Line speed set to {speeds[row_key]:.2f}x in quality check "
                             f"(fitted speed was {fits.get(row_key, 1.0):.2f}x).")
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
    speeds = record.get("line_speeds") or {}   # set by the reviewer
    fits = {key: take["fit_speed"] for key, take in (record.get("takes") or {}).items()}
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
        takes = record.get("takes") or {}
        for p in placed:
            row = p.row
            key = str(row.row_number)
            slot = row.end_ms - row.start_ms
            info = takes.get(key) or {}
            take = take_path(folder, row.row_number)
            line = measure((take if take.is_file() else clip_path(folder, row.row_number)).read_bytes(), slot)
            played = measure(clip_path(folder, row.row_number).read_bytes(), slot)
            measured_take = {f"pass1_5_{k}": v for k, v in fields(line).items()}
            pass1 = info.get("pass1") or fields(line)   # generated here: the take is the first take
            reviewed = key in speeds
            status = ("MANUAL_REVIEW" if info.get("status") == "MANUAL_REVIEW" and not reviewed
                      else "OK" if played.within_target else "TCR_OUT_OVERRUN")
            original = edits[key]["original"] if key in edits else row.text
            records.append(dict(excel_row=row.row_number, episode=row.episode, character=row.character,
                                voice_id=row.voice_id, start_TCR=format_tcr(row.start_ms, fps),
                                end_TCR=format_tcr(row.end_ms, fps), fps=fps, start_ms=row.start_ms,
                                end_ms=row.end_ms, tcr_duration_ms=slot, original_dialogue=original,
                                final_dialogue=row.text, rewritten="yes" if row.text != original else "no",
                                rewrite_attempts=info.get("rewrite_attempts", 0),
                                leading_trim_ms=info.get("leading_trim_ms", ""),
                                **{f"pass1_{k}": v for k, v in pass1.items()}, **measured_take,
                                pass2_speed=format_sig(speeds.get(key, fits.get(key, 1.0))),
                                pass2_audio_duration_ms=played.audio_duration_ms, pass2_overrun_ms=played.overrun_ms,
                                pass2_percent_overrun=format_sig(played.percent_overrun),
                                within_target="yes" if played.within_target else "no", status=status,
                                note="" if reviewed else info.get("note", ""), track=names[row.group],
                                adjusted_start_TCR=format_tcr(p.start_ms, fps),
                                adjusted_end_TCR=format_tcr(p.end_ms, fps)))
        report = work / "processing_report.csv"
        write_report(report, sorted(records, key=lambda r: r["excel_row"]))
        files.append(report)

        stem = safe_component(Path(record["script"]["name"]).stem) or "script"
        workbook = work / f"{stem}_qc.xlsx"
        if (folder / "script.xlsx").is_file():
            edited_workbook(folder / "script.xlsx", workbook, record.get("sheet"), edits, speeds, fits)
            files.append(workbook)

        by_row = {row.row_number: row for row in rows}
        changes = []
        for key in sorted(set(edits) | set(speeds), key=int):
            row = by_row[int(key)]
            by = []
            if key in edits:
                by.append("Gemini (AD Tool V2)" if edits[key].get("by") == "gemini" else "reviewer")
            if key in speeds:
                by.append("reviewer (speed)")
            changes.append({"excel_row": row.row_number, "character": row.character,
                            "start_TCR": format_tcr(row.start_ms, fps), "changed_by": ", ".join(by),
                            "script_dialogue": edits[key]["original"] if key in edits else row.text,
                            "reviewed_dialogue": row.text,
                            "line_speed": f"{speeds[key]:.2f}x" if key in speeds else ""})
        change_file = work / "qc_changes.csv"
        write_csv(change_file, changes, CHANGE_FIELDS)
        files.append(change_file)

        out_path = Path(out_path)
        export_archive(out_path, files)
    return {"name": f"{episode_tag(episode)}_qc.zip", "changes": len(changes),
            "tracks": len(names), "workbook": workbook.name}


def fields(measurement):
    """A measurement as report values (speeds and percentages to 8 significant figures)."""
    return {"audio_duration_ms": measurement.audio_duration_ms, "overrun_ms": measurement.overrun_ms,
            "percent_overrun": format_sig(measurement.percent_overrun),
            "desired_speed": format_sig(measurement.desired_speed)}
