"""Read an AD version 2 output ZIP so one of its episodes can be reviewed without dubbing it again.

Only these are used from the ZIP:

  processing_report.csv   one record per row: its timing in ms, frame rate, character, voice, the
                          script's dialogue and the dialogue spoken (a Gemini rewrite or the same),
                          and the pass 2 speed
  pass1/lines/            the take of every row that was not rewritten (ElevenLabs, trimmed)
  pass2/lines/            the take of every row Gemini rewrote (the rewritten take before its speed
                          change is not in the ZIP, so the delivered clip becomes the take, at 1.0x)

Everything needed to place a line is in the report, so the workbook is not read: no worksheet or
frame-rate settings are needed. The clip that plays is the take at its speed: for a row that was
not rewritten, the report's pass 2 speed (the same FFmpeg call V2 made, so it matches V2's pass 2
audio); for a rewritten row, 1.0x, which is V2's pass 2 clip itself.
The session rebuilds the tracks from these clips with V2's rule. The ZIP's script.xlsx is copied
only so the export can write reviewed lines back into it.
"""

import csv
import io
from pathlib import Path
import zipfile

from v2.audio_builder import read_clip
from v2.audio_speed import sig
from v2.exporter import LINES_DIR, PASS1_DIR, PASS2_DIR, SCRIPT_NAME, line_name
from v2.models import DialogueRow

REPORT_NAME = "processing_report.csv"
REQUIRED_FIELDS = ("excel_row", "episode", "character", "voice_id", "fps", "start_ms", "end_ms",
                   "original_dialogue", "final_dialogue", "rewritten", "rewrite_attempts", "leading_trim_ms",
                   "pass1_audio_duration_ms", "pass1_overrun_ms", "pass1_percent_overrun", "pass1_desired_speed",
                   "pass1_5_desired_speed", "pass2_speed", "pass2_audio_duration_ms", "tcr_duration_ms",
                   "status", "note")
REMAKE = "Run the script through AD Tool V2 again and use the new ZIP."


class RunError(ValueError):
    """The ZIP cannot be reviewed; the message says why, in the reviewer's terms."""


class V2Run:
    """One AD version 2 output ZIP, opened and checked against its own report."""

    def __init__(self, zip_path):
        self.zip_path = Path(zip_path)
        try:
            self.archive = zipfile.ZipFile(self.zip_path)
        except (zipfile.BadZipFile, OSError):
            raise RunError("This file is not a ZIP. Upload the ZIP that AD Tool V2 delivered.") from None
        try:
            names = set(self.archive.namelist())
            if REPORT_NAME not in names or not all(any(n.startswith(f"{folder}/{LINES_DIR}/") for n in names)
                                                   for folder in (PASS1_DIR, PASS2_DIR)):
                raise RunError(f"This ZIP has no pass1 and pass2 line clips, so it was made by an earlier "
                               f"AD Tool V2. {REMAKE}")
            self.records = self._read_report()
            self.fps = self.records[0]["fps"]
            self.rows = [DialogueRow(r["excel_row"], r["episode"], r["character"], r["start_ms"], r["end_ms"],
                                     r["original_dialogue"], r["voice_id"]) for r in self.records]
            self.by_row = {r["excel_row"]: r for r in self.records}
            self.script = self.archive.read(SCRIPT_NAME) if SCRIPT_NAME in names else None
        except BaseException:
            self.archive.close()
            raise

    def close(self):
        self.archive.close()

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        self.close()

    def _read_report(self):
        reader = csv.DictReader(io.StringIO(self.archive.read(REPORT_NAME).decode("utf-8-sig")))
        if not set(REQUIRED_FIELDS) <= set(reader.fieldnames or []):
            raise RunError(f"This ZIP's report was made by an earlier AD Tool V2. {REMAKE}")
        records = []
        try:
            for record in reader:
                record = {key: unquote(value) for key, value in record.items()}
                for key in ("excel_row", "fps", "start_ms", "end_ms", "pass2_audio_duration_ms", "tcr_duration_ms"):
                    record[key] = int(record[key])
                for key in ("pass1_5_desired_speed", "pass2_speed"):
                    record[key] = float(record[key])
                if record["end_ms"] <= record["start_ms"]:
                    raise ValueError
                records.append(record)
        except (TypeError, ValueError):
            raise RunError("The ZIP's processing_report.csv could not be read.") from None
        if not records:
            raise RunError("The ZIP's processing_report.csv has no dialogue rows.")
        if len({r["fps"] for r in records}) != 1:
            raise RunError("The ZIP's processing_report.csv mixes frame rates.")
        return records

    def episodes(self):
        return sorted({row.episode for row in self.rows})

    def episode_rows(self, episode):
        rows = [row for row in self.rows if row.episode == episode]
        if not rows:
            raise RunError(f"Episode {episode} is not in this run.")
        return rows

    def takes(self, rows):
        """Per row, what the session keeps about its take: pass 2's speed (the suggestion), the speed
        it would need with no limits, AD Tool V2's status and note, and pass 1 as measured by V2."""
        takes = {}
        for row in rows:
            r = self.by_row[row.row_number]
            if rewritten(r):   # the take is V2's pass 2 clip: it plays at 1.0x
                fit, needed = 1.0, sig(r["pass2_audio_duration_ms"] / r["tcr_duration_ms"])
            else:
                fit, needed = r["pass2_speed"], r["pass1_5_desired_speed"]
            takes[row.row_number] = {
                "fit_speed": fit, "needed_speed": needed,
                "status": r["status"], "note": r["note"], "rewrite_attempts": r["rewrite_attempts"],
                "leading_trim_ms": r["leading_trim_ms"],
                "pass1": {key: r[f"pass1_{key}"] for key in
                          ("audio_duration_ms", "overrun_ms", "percent_overrun", "desired_speed")}}
        return takes

    def rewrites(self, rows):
        """Lines Gemini rewrote, as session edits: the script's wording stays the original."""
        edits = {}
        for row in rows:
            r = self.by_row[row.row_number]
            if r["final_dialogue"] != r["original_dialogue"]:
                edits[str(row.row_number)] = {"text": r["final_dialogue"], "original": r["original_dialogue"],
                                              "by": "gemini"}
        return edits

    def clips(self, rows):
        """Each row's take bytes (pass 1, or pass 2 for a rewritten row), checked; every row with a
        missing or unreadable clip is reported at once."""
        clips, missing = {}, []
        for row in rows:
            folder = PASS2_DIR if rewritten(self.by_row[row.row_number]) else PASS1_DIR
            try:
                data = self.archive.read(f"{folder}/{LINES_DIR}/{line_name(row)}")
                read_clip(data)
                clips[row.row_number] = data
            except Exception:  # not in the ZIP, or not a WAV
                missing.append(row.row_number)
        if missing:
            listed = ", ".join(map(str, missing[:10])) + (" …" if len(missing) > 10 else "")
            raise RunError(f"The ZIP is missing a playable clip for Excel row{'s' if len(missing) > 1 else ''} "
                           f"{listed}. {REMAKE}")
        return clips

    def save_script(self, target):
        if self.script is not None:
            Path(target).write_bytes(self.script)


def rewritten(record):
    """Pass 1.5 kept a new take (even if Gemini's words happened to match the script's)."""
    return record["rewritten"] == "yes"


def unquote(value):
    """Undo the report's spreadsheet-formula guard (a leading ' before =, +, - or @)."""
    if isinstance(value, str) and value.startswith("'") and value[1:2] in ("=", "+", "-", "@"):
        return value[1:]
    return value
