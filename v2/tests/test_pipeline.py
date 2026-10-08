import csv
import io
import json
from pathlib import Path
import struct
import tempfile
import unittest
from unittest.mock import Mock, patch
import wave
import zipfile

from openpyxl import Workbook, load_workbook

from v2.app import create_app
from v2.excel_parser import TEMPLATE_HEADERS, WorkbookValidationError, parse_excel, timecode_to_ms
from v2.exporter import track_names
from v2.pipeline import run_pipeline
from v2.tts_client import ElevenLabsClient


def wav_bytes(duration_ms=200, value=1000):
    output = io.BytesIO()
    with wave.open(output, "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(24000)
        wav.writeframes(struct.pack("<h", value) * (duration_ms * 24))
    return output.getvalue()


def dialogue(episode="001", character="Alice", start="00:00:01:00", end="00:00:02:00",
             text="Hello", voice="alice-voice"):
    return [episode, 1, start, end, character, voice, text, "Done"]


class PipelineTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def workbook(self, rows, headers=TEMPLATE_HEADERS):
        path = self.root / "input.xlsx"
        book = Workbook()
        book.active.append(list(headers))
        for row in rows:
            book.active.append(row)
        book.save(path)
        book.close()
        return path

    def test_headers_are_mapped_by_name_and_ignored_columns_do_not_filter_rows(self):
        headers = list(reversed(TEMPLATE_HEADERS))
        headers[headers.index("EP NO")] = " ep  no "
        path = self.workbook([list(reversed(dialogue()))], headers)
        rows = parse_excel(path)
        self.assertEqual(rows[0].episode, "001")
        self.assertEqual(rows[0].character, "Alice")
        self.assertEqual(rows[0].voice_id, "alice-voice")
        self.assertEqual(rows[0].text, "Hello")
        self.assertEqual(rows[0].row_number, 2)

    def test_validation_reports_all_bad_rows_before_any_api_call(self):
        path = self.workbook([
            dialogue(),
            dialogue(voice=""),
            dialogue(start="00:00:01:25"),
            dialogue(end="00:00:00:00"),
        ])
        synthesize = Mock()
        with self.assertRaises(WorkbookValidationError) as caught:
            run_pipeline(path, self.root / "out.zip", synthesize=synthesize)
        self.assertIn("Row 3", str(caught.exception))
        self.assertIn("Row 4", str(caught.exception))
        self.assertIn("Row 5", str(caught.exception))
        synthesize.assert_not_called()

    def test_conflicting_voices_are_rejected_with_source_row(self):
        path = self.workbook([dialogue(), dialogue(voice="different")])
        with self.assertRaisesRegex(WorkbookValidationError, "Row 3: conflicting VOICE ID"):
            parse_excel(path)

    def test_missing_duplicate_headers_empty_sheet_and_formulas(self):
        for headers, rows, message in [
            (TEMPLATE_HEADERS[:-2], [], "Missing required header: ENGLISH DIALOGUES"),
            (TEMPLATE_HEADERS + ("EP NO",), [], "Duplicate header"),
            (TEMPLATE_HEADERS, [], "No dialogue rows"),
            (TEMPLATE_HEADERS, [dialogue(text='="Hello"')], "paste values"),
        ]:
            with self.subTest(message=message):
                path = self.workbook(rows, headers)
                with self.assertRaisesRegex(WorkbookValidationError, message):
                    parse_excel(path)

    def test_blank_rows_preserve_excel_row_numbers(self):
        path = self.workbook([[None] * 8, dialogue()])
        self.assertEqual(parse_excel(path)[0].row_number, 3)

    def test_frame_conversion_and_sheet_selection(self):
        self.assertEqual(timecode_to_ms("00:00:18:08"), 18320)
        self.assertEqual(timecode_to_ms("00:00:00:12", 24), 500)
        path = self.workbook([dialogue()])
        with self.assertRaisesRegex(WorkbookValidationError, "was not found"):
            parse_excel(path, sheet_name="Missing")
        self.assertEqual(len(parse_excel(path, sheet_name="Sheet")), 1)

    def test_output_grouping_voice_routing_alignment_and_overrun_preservation(self):
        # Deliberately unsorted; overlap across characters is permitted.
        path = self.workbook([
            dialogue(start="00:00:03:00", end="00:00:03:04", text="late"),
            dialogue(character="Bob", start="00:00:01:00", text="bob", voice="bob-voice"),
            dialogue(text="early"),
            dialogue(episode="002", start="00:00:00:00", end="00:00:01:00", text="episode2"),
        ])
        calls = []

        def synthesize(text, voice):
            calls.append((text, voice))
            return wav_bytes(400 if text == "late" else 200)

        output = self.root / "result.zip"
        summary = run_pipeline(path, output, synthesize=synthesize, progress=lambda _: None)
        self.assertEqual(calls, [("late", "alice-voice"), ("bob", "bob-voice"),
                                 ("early", "alice-voice"), ("episode2", "alice-voice")])
        self.assertEqual(summary["tracks"], 3)
        self.assertEqual(summary["flagged_rows"], 1)
        with zipfile.ZipFile(output) as archive:
            self.assertEqual(set(archive.namelist()), {"Episode_001_Alice.wav", "Episode_001_Bob.wav",
                                                       "Episode_002_Alice.wav", "processing_report.csv"})
            for name in ("Episode_001_Alice.wav", "Episode_001_Bob.wav"):
                with wave.open(io.BytesIO(archive.read(name))) as wav:
                    self.assertEqual(wav.getnframes(), 3400 * 24)
                    self.assertEqual(wav.getframerate(), 24000)
                    self.assertEqual(wav.getnchannels(), 1)
                    pcm = wav.readframes(wav.getnframes())
                    self.assertEqual(pcm[:1000 * 24 * 2], bytes(1000 * 24 * 2))
                    self.assertEqual(struct.unpack_from("<h", pcm, 1000 * 24 * 2)[0], 1000)
                    if "Alice" in name:
                        self.assertEqual(struct.unpack_from("<h", pcm, 3300 * 24 * 2)[0], 1000)
            with wave.open(io.BytesIO(archive.read("Episode_002_Alice.wav"))) as wav:
                self.assertEqual(wav.getnframes(), 1000 * 24)
            report = list(csv.DictReader(io.StringIO(archive.read("processing_report.csv").decode("utf-8-sig"))))
            self.assertEqual(report[0]["overrun_ms"], "240")
            self.assertEqual(report[1]["status"], "OK")

    def test_same_character_overlap_is_flagged(self):
        path = self.workbook([dialogue(), dialogue(start="00:00:01:04")])
        output = self.root / "overlap.zip"
        run_pipeline(path, output, synthesize=lambda *_: wav_bytes(400), progress=lambda _: None)
        with zipfile.ZipFile(output) as archive:
            rows = list(csv.DictReader(io.StringIO(archive.read("processing_report.csv").decode("utf-8-sig"))))
        self.assertEqual(rows[1]["status"], "SAME_CHARACTER_OVERLAP")
        self.assertEqual(rows[1]["same_character_overlap_ms"], "240")

    def test_safe_filenames_remain_unique(self):
        names = track_names({("001", "A/B"), ("001", "A:B"), ("001", "a_b"), ("../../", "...")})
        self.assertEqual(len({name.casefold() for name in names.values()}), 4)
        self.assertTrue(all("/" not in name and "\\" not in name for name in names.values()))

    def test_failed_generation_does_not_publish_partial_archive(self):
        path = self.workbook([dialogue(), dialogue(text="second")])
        output = self.root / "failed.zip"
        synthesize = Mock(side_effect=[wav_bytes(), RuntimeError("Provider failure")])
        with self.assertRaisesRegex(RuntimeError, "Excel row 3: Provider failure"):
            run_pipeline(path, output, synthesize=synthesize, progress=lambda _: None)
        self.assertFalse(output.exists())
        self.assertEqual(list(self.root.glob("ad-v2-*")), [])

    def test_existing_output_is_not_overwritten(self):
        path = self.workbook([dialogue()])
        output = self.root / "existing.zip"
        output.write_bytes(b"existing")
        synthesize = Mock()
        with self.assertRaisesRegex(ValueError, "already exists"):
            run_pipeline(path, output, synthesize=synthesize)
        synthesize.assert_not_called()
        self.assertEqual(output.read_bytes(), b"existing")

    def test_web_upload_status_download_and_template(self):
        app = create_app({"TESTING": True, "RUNTIME_DIR": self.root / "runtime",
                          "SYNTHESIZE": lambda *_: wav_bytes()})
        client = app.test_client()
        self.assertEqual(client.get("/").status_code, 200)
        for asset in ("style.css", "app.js", "zibanka_logo.jpeg"):
            response = client.get(f"/static/{asset}")
            self.assertEqual(response.status_code, 200)
            response.close()
        template = client.get("/template")
        book = load_workbook(io.BytesIO(template.data))
        self.assertEqual(tuple(cell.value for cell in book.active[1]), TEMPLATE_HEADERS)
        book.close()
        path = self.workbook([dialogue()])
        with patch("v2.app.threading.Thread") as thread:
            thread.side_effect = lambda *, target, daemon: Mock(start=target)
            response = client.post("/upload", data={"file": (io.BytesIO(path.read_bytes()), "input.xlsx"),
                                                   "fps": "25", "output_name": "English dialogue.zip"})
        self.assertEqual(response.status_code, 202)
        job_id = response.json["job_id"]
        status = client.get(f"/status/{job_id}").json
        self.assertEqual(status["status"], "done")
        self.assertEqual(status["percent"], 100)
        self.assertEqual(status["download_name"], "English dialogue.zip")
        response = client.get(f"/download/{job_id}")
        self.assertEqual(response.status_code, 200)
        self.assertIn('filename="English dialogue.zip"', response.headers["Content-Disposition"])
        with zipfile.ZipFile(io.BytesIO(response.data)) as archive:
            self.assertIn("Episode_001_Alice.wav", archive.namelist())
        response.close()
        self.assertEqual(client.get("/download/unknown").status_code, 404)

    def test_web_rejects_invalid_workbook_without_starting_generation(self):
        synthesize = Mock()
        app = create_app({"TESTING": True, "RUNTIME_DIR": self.root / "runtime", "SYNTHESIZE": synthesize})
        client = app.test_client()
        path = self.workbook([dialogue(voice="")])
        response = client.post("/upload", data={"file": (io.BytesIO(path.read_bytes()), "input.xlsx")})
        self.assertEqual(response.status_code, 400)
        self.assertIn("VOICE ID", response.json["error"])
        synthesize.assert_not_called()
        response = client.post("/upload", data={"file": (io.BytesIO(b"broken"), "input.xlsx")})
        self.assertEqual(response.status_code, 400)

    def test_elevenlabs_request_uses_row_voice_and_converts_pcm(self):
        response = Mock()
        response.read.return_value = b"\x00\x00" * 240
        response.__enter__ = Mock(return_value=response)
        response.__exit__ = Mock(return_value=False)
        with patch("v2.tts_client.urlopen", return_value=response) as request:
            result = ElevenLabsClient(api_key="test-key", model_id="eleven_multilingual_v2")("English dialogue", "voice/id")
        sent = request.call_args.args[0]
        self.assertIn("voice%2Fid?output_format=pcm_24000", sent.full_url)
        self.assertEqual(json.loads(sent.data), {"text": "English dialogue", "model_id": "eleven_multilingual_v2"})
        with wave.open(io.BytesIO(result)) as wav:
            self.assertEqual(wav.getnframes(), 240)
            self.assertEqual(wav.getframerate(), 24000)


if __name__ == "__main__":
    unittest.main()
