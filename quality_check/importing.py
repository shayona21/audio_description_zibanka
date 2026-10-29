"""Read an AD version 2 output ZIP so one of its episodes can be reviewed without dubbing it again.

The ZIP layout is V2's (``v2/pipeline.py``): ``pass1/lines/`` and ``pass2/lines/`` (each row's clip
as ElevenLabs delivered it, and fitted to its slot), ``processing_report.csv`` (one record per row,
with its ``fit_speed``) and ``script.xlsx`` (the workbook the run was made from). Pass 1 becomes the
take and pass 2 is what plays. The tracks in the ZIP are not used: the session rebuilds them from the
pass 2 lines, exactly as V2 built them, so one line can later be changed and only its character's
track rebuilt.

A run on this server comes with its frame rate in ``run.json``. An uploaded ZIP does not, so the
frame rate is found from the report: the scripted durations it lists (TCR OUT - TCR IN, in ms) only
match the workbook when it is read at the frame rate the run used.
"""

import csv
import io
from pathlib import Path
import tempfile
import zipfile

from v2.audio_builder import read_clip
from v2.excel_parser import SUPPORTED_FPS, parse_excel
from v2.exporter import LINES_DIR, PASS1_DIR, PASS2_DIR, SCRIPT_NAME, line_name

REPORT_NAME = "processing_report.csv"


class RunError(ValueError):
    """The ZIP cannot be reviewed; the message says why, in the reviewer's terms."""


class V2Run:
    """One AD version 2 output ZIP, opened and checked against its own report."""

    def __init__(self, zip_path, fps_hint=25, sheet_name=None):
        self.zip_path = Path(zip_path)
        try:
            self.archive = zipfile.ZipFile(self.zip_path)
        except (zipfile.BadZipFile, OSError):
            raise RunError("This file is not a ZIP. Upload the ZIP that AD Tool V2 delivered.") from None
        try:
            names = set(self.archive.namelist())
            if SCRIPT_NAME not in names or REPORT_NAME not in names or not all(
                    any(name.startswith(f"{pass_dir}/{LINES_DIR}/") for name in names)
                    for pass_dir in (PASS1_DIR, PASS2_DIR)):
                raise RunError("This ZIP has no pass 1 and pass 2 line clips, so it was made by an earlier "
                               "AD Tool V2. Run the script through AD Tool V2 again and use the new ZIP.")
            self.report = self._read_report()
            self.script = self.archive.read(SCRIPT_NAME)
            self.fps, self.sheet, self.rows = self._parse_script(fps_hint, sheet_name)
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
        text = self.archive.read(REPORT_NAME).decode("utf-8-sig")
        reader = csv.DictReader(io.StringIO(text))
        if "fit_speed" not in (reader.fieldnames or []):
            raise RunError("This ZIP was made before AD Tool V2 fitted each line to its slot. "
                           "Run the script through AD Tool V2 again and use the new ZIP.")
        report = {}
        try:
            for record in reader:
                report[int(record["excel_row"])] = (int(record["duration"]), float(record["fit_speed"]))
        except (TypeError, ValueError):
            raise RunError("The ZIP's processing_report.csv could not be read.") from None
        return report

    def _parse_script(self, fps_hint, sheet_name):
        """Read the workbook at the frame rate the run used (checked against the report)."""
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / SCRIPT_NAME
            path.write_bytes(self.script)
            first_error = None
            for fps in [fps_hint] + [f for f in SUPPORTED_FPS if f != fps_hint]:
                try:
                    rows = parse_excel(path, fps=fps, sheet_name=sheet_name)
                except ValueError as exc:
                    first_error = first_error or exc
                    continue
                except Exception:
                    raise RunError("The ZIP's script.xlsx could not be read as an Excel workbook.") from None
                if {row.row_number for row in rows} == set(self.report) and all(
                        row.end_ms - row.start_ms == self.report[row.row_number][0] for row in rows):
                    return fps, sheet_name, rows
        if first_error is not None and "was not found" in str(first_error):
            raise RunError(f"{first_error} in the ZIP's script. Set the worksheet this run used under Change, "
                           "or leave it empty for the first worksheet.")
        raise RunError("The ZIP's script does not match its processing report, so its lines can't be placed. "
                       "If the run used a worksheet other than the first, set it under Change.")

    def episodes(self):
        return sorted({row.episode for row in self.rows})

    def episode_rows(self, episode):
        rows = [row for row in self.rows if row.episode == episode]
        if not rows:
            raise RunError(f"Episode {episode} is not in this run.")
        return rows

    def fits(self, rows):
        return {row.row_number: self.report[row.row_number][1] for row in rows}

    def clips(self, rows):
        """Each row's ``(pass 1, pass 2)`` clip bytes, checked; every row with a missing or unreadable
        clip is reported at once."""
        clips, missing = {}, []
        for row in rows:
            try:
                pair = tuple(self.archive.read(f"{pass_dir}/{LINES_DIR}/{line_name(row)}")
                             for pass_dir in (PASS1_DIR, PASS2_DIR))
                for data in pair:
                    read_clip(data)
                clips[row.row_number] = pair
            except Exception:  # not in the ZIP, or not a WAV
                missing.append(row.row_number)
        if missing:
            listed = ", ".join(map(str, missing[:10])) + (" …" if len(missing) > 10 else "")
            raise RunError(f"The ZIP is missing a playable clip for Excel row{'s' if len(missing) > 1 else ''} "
                           f"{listed}. Run the script through AD Tool V2 again.")
        return clips

    def save_script(self, target):
        Path(target).write_bytes(self.script)

