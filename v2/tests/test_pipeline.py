import csv
import io
import json
from pathlib import Path
import struct
import tempfile
import unittest
from unittest.mock import Mock, patch
from urllib.error import HTTPError
import wave
import zipfile

from openpyxl import Workbook, load_workbook
from pydub import AudioSegment
from pydub.generators import Sine

from v2.app import create_app
from v2.audio_speed import MAX_SPEED, MIN_SPEED, change_speed, fit_speed, needed_speed, speech_span
from v2.excel_parser import TEMPLATE_HEADERS, WorkbookValidationError, parse_excel, timecode_to_ms
from v2.exporter import format_tcr, line_name, track_names, timeline_names
from v2.models import DialogueRow
from v2.pipeline import run_pipeline
from v2.timing import place_dialogue
from v2.tts_client import MODEL_ID, ElevenLabsClient, prepare_speech_text


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
        # No live Gemini calls: by default every rewrite returns the line unchanged.
        for target in ("v2.pipeline.GeminiRewriter", "v2.app.GeminiRewriter"):
            patcher = patch(target, return_value=lambda request: request.text)
            patcher.start()
            self.addCleanup(patcher.stop)

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
            dialogue(character=""),
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
            (TEMPLATE_HEADERS[:-2], [], "Missing required header: TARGET DIALOGUE"),
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
        # Deliberately unsorted; different characters may start simultaneously.
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
        summary = run_pipeline(path, output, synthesize=synthesize, progress=lambda _: None,
                               fit_lines=False)
        self.assertEqual(calls, [("late", "alice-voice"), ("bob", "bob-voice"),
                                 ("early", "alice-voice"), ("episode2", "alice-voice")])
        self.assertEqual(summary["tracks"], 3)
        self.assertEqual(summary["flagged_rows"], 1)
        with zipfile.ZipFile(output) as archive:
            audio = {"tracks/ep001_alice.wav", "tracks/ep001_bob.wav", "tracks/ep002_alice.wav",
                     "lines/ep001_alice_row2.wav", "lines/ep001_bob_row3.wav", "lines/ep001_alice_row4.wav",
                     "lines/ep002_alice_row5.wav"}
            lines = {name for name in audio if name.startswith("lines/")}
            self.assertEqual(set(archive.namelist()), {f"pass1/{name}" for name in audio} | {f"pass2/{name}" for name in audio} | {
                "ep001_timeline.csv", "ep002_timeline.csv", "processing_report.csv", "script.xlsx"})
            self.assertEqual(archive.read("script.xlsx"), path.read_bytes())
            for name in ("ep001_alice.wav", "ep001_bob.wav"):
                with wave.open(io.BytesIO(archive.read(f"pass2/tracks/{name}"))) as wav:
                    self.assertEqual(wav.getnframes(), 3400 * 24)
                    self.assertEqual(wav.getframerate(), 24000)
                    self.assertEqual(wav.getnchannels(), 1)
                    pcm = wav.readframes(wav.getnframes())
                    first_start = 1000
                    self.assertEqual(pcm[:first_start * 24 * 2], bytes(first_start * 24 * 2))
                    self.assertEqual(struct.unpack_from("<h", pcm, first_start * 24 * 2)[0], 1000)
                    if "alice" in name:
                        self.assertEqual(struct.unpack_from("<h", pcm, 3300 * 24 * 2)[0], 1000)
            with wave.open(io.BytesIO(archive.read("pass2/tracks/ep002_alice.wav"))) as wav:
                self.assertEqual(wav.getnframes(), 1000 * 24)
            report = list(csv.DictReader(io.StringIO(archive.read("processing_report.csv").decode("utf-8-sig"))))
            self.assertEqual(report[0]["pass2_overrun_ms"], "240")
            self.assertEqual(report[1]["status"], "OK")

    def test_same_character_collision_uses_actual_end_not_script_end(self):
        path = self.workbook([dialogue(), dialogue(start="00:00:01:04")])
        output = self.root / "overlap.zip"
        run_pipeline(path, output, synthesize=lambda *_: wav_bytes(400), progress=lambda _: None,
                     fit_lines=False)
        with zipfile.ZipFile(output) as archive:
            rows = list(csv.DictReader(io.StringIO(archive.read("processing_report.csv").decode("utf-8-sig"))))
        self.assertEqual(rows[1]["status"], "OK")
        self.assertEqual(rows[1]["adjusted_start_TCR"], "00:00:01:10")
        self.assertEqual(rows[1]["start_TCR"], "00:00:01:04")
        self.assertEqual(rows[1]["pass2_overrun_ms"], "-440")      # 400 ms of audio in an 840 ms slot

    def test_character_local_delays_match_audio_and_combined_script_exports(self):
        # Unsorted characters and episodes still have independent schedules.
        path = self.workbook([
            dialogue(character="A", start="00:00:40:00", end="00:00:42:00", text="Why?", voice="a"),
            dialogue(episode="002", character="A", start="00:00:01:00", end="00:00:02:00",
                     text="Other episode", voice="a"),
            dialogue(character="B", start="00:00:11:00", end="00:00:14:00", text="[laughs]", voice="b"),
            dialogue(character="A", start="00:00:00:00", end="00:00:10:00", text="Walk?", voice="a"),
        ])
        durations = {"Walk?": 45000, "[laughs]": 3000, "Why?": 2000, "Other episode": 1000}
        values = {"Walk?": 1000, "[laughs]": 2000, "Why?": 3000, "Other episode": 4000}
        synthesize = Mock(side_effect=lambda text, voice: wav_bytes(durations[text], values[text]))
        output = self.root / "character-timing.zip"
        run_pipeline(path, output, synthesize=synthesize, progress=lambda _: None,
                     fit_lines=False)
        self.assertEqual(synthesize.call_count, 4)
        expected = {
            "ep001_a": [(0, 45000, "Walk?", 0), (45000, 47000, "Why?", 5000)],
            "ep001_b": [(11000, 14000, "[laughs]", 0)],
            "ep002_a": [(1000, 2000, "Other episode", 0)],
        }
        with zipfile.ZipFile(output) as archive:
            for stem, expected_rows in expected.items():
                with self.subTest(track=stem):
                    with wave.open(io.BytesIO(archive.read(f"pass2/tracks/{stem}.wav"))) as audio:
                        duration = 47000 if stem.startswith("ep001") else 2000
                        self.assertEqual(audio.getnframes(), duration * 24)
                        actual = audio.readframes(audio.getnframes())
                    expected_pcm = bytearray(duration * 24 * 2)
                    for start, end, text, _ in expected_rows:
                        expected_pcm[start * 24 * 2:end * 24 * 2] = struct.pack("<h", values[text]) * ((end - start) * 24)
                    self.assertEqual(actual, bytes(expected_pcm))
            timeline = list(csv.reader(io.StringIO(
                archive.read("ep001_timeline.csv").decode("utf-8-sig")
            )))
            self.assertEqual(timeline, [
                ["Dialogue", "Original time start", "Original time end",
                 "Adjusted time start", "Adjusted time end", "Delay"],
                ["A: Walk?", "00:00:00.000", "00:00:10.000", "00:00:00.000", "00:00:45.000", "0.000 s"],
                ["B: [laughs]", "00:00:11.000", "00:00:14.000", "00:00:11.000", "00:00:14.000", "0.000 s"],
                ["A: Why?", "00:00:40.000", "00:00:42.000", "00:00:45.000", "00:00:47.000", "5.000 s"],
            ])
            other = list(csv.DictReader(io.StringIO(
                archive.read("ep002_timeline.csv").decode("utf-8-sig")
            )))
            self.assertEqual(len(other), 1)
            self.assertEqual(other[0]["Dialogue"], "A: Other episode")
            self.assertEqual(other[0]["Delay"], "0.000 s")
            report = list(csv.DictReader(io.StringIO(
                archive.read("processing_report.csv").decode("utf-8-sig")
            )))
            self.assertEqual(report[0]["start_TCR"], "00:00:40:00")
            self.assertEqual(report[0]["adjusted_start_TCR"], "00:00:45:00")
            self.assertEqual(report[0]["tcr_duration_ms"], "2000")
            self.assertEqual(report[0]["pass2_overrun_ms"], "0")

    def test_available_silence_absorbs_delays_within_each_character(self):
        path = self.workbook([
            dialogue(start="00:00:00:00", end="00:00:10:00", text="first"),
            dialogue(start="00:00:11:00", end="00:00:14:00", text="short"),
            dialogue(start="00:00:20:00", end="00:00:22:00", text="long"),
            dialogue(character="Bob", voice="b", start="00:00:12:00", end="00:00:13:00", text="bob"),
            dialogue(start="00:00:25:00", end="00:00:27:00", text="last"),
        ])
        durations = {"first": 15000, "short": 1000, "long": 6000, "bob": 500, "last": 500}
        output = self.root / "recovered-timing.zip"
        run_pipeline(path, output, synthesize=lambda text, voice: wav_bytes(durations[text]), progress=lambda _: None,
                     fit_lines=False)
        with zipfile.ZipFile(output) as archive:
            records = list(csv.DictReader(io.StringIO(
                archive.read("processing_report.csv").decode("utf-8-sig")
            )))
            self.assertEqual([r["adjusted_start_TCR"] for r in records],
                             ["00:00:00:00", "00:00:15:00", "00:00:20:00", "00:00:12:00", "00:00:26:00"])
            self.assertEqual([r["adjusted_end_TCR"] for r in records],
                             ["00:00:15:00", "00:00:16:00", "00:00:26:00", "00:00:12:13", "00:00:26:13"])
            self.assertEqual([int(r["pass2_overrun_ms"]) for r in records], [5000, -2000, 4000, -500, -1500])
            timeline = list(csv.DictReader(io.StringIO(archive.read("ep001_timeline.csv").decode("utf-8-sig"))))
            self.assertEqual([r["Dialogue"] for r in timeline],
                             ["Alice: first", "Bob: bob", "Alice: short", "Alice: long", "Alice: last"])
            self.assertEqual([r["Delay"] for r in timeline], ["0.000 s", "0.000 s", "4.000 s", "0.000 s", "1.000 s"])
            for name in ("ep001_alice.wav", "ep001_bob.wav"):
                with wave.open(io.BytesIO(archive.read(f"pass2/tracks/{name}"))) as audio:
                    self.assertEqual(audio.getnframes(), 27000 * 24)

    def test_timeline_preserves_millisecond_precision_and_csv_dialogue(self):
        text = '+Hello, "world"\nनमस्ते'
        path = self.workbook([
            dialogue(start="00:00:00:01", end="00:00:01:01", text=text),
            dialogue(start="00:00:01:12", end="00:00:02:12", text="second"),
        ])
        output = self.root / "precision.zip"
        run_pipeline(path, output, fps=24, synthesize=lambda *_: wav_bytes(1501), progress=lambda _: None,
                     fit_lines=False)
        with zipfile.ZipFile(output) as archive:
            records = list(csv.DictReader(io.StringIO(
                archive.read("ep001_timeline.csv").decode("utf-8-sig")
            )))
        self.assertEqual(records[0]["Adjusted time start"], "00:00:00.042")
        self.assertEqual(records[0]["Adjusted time end"], "00:00:01.543")
        self.assertEqual(records[1]["Adjusted time start"], "00:00:01.543")
        self.assertEqual(records[1]["Delay"], "0.043 s")
        self.assertEqual(records[0]["Dialogue"], "Alice: " + text)

    def test_only_actual_collisions_delay_a_character(self):
        rows = [
            DialogueRow(2, "1", "A", 0, 10000, "first", "shared-voice"),
            DialogueRow(3, "1", "A", 40000, 42000, "next", "shared-voice"),
            DialogueRow(4, "1", "A", 44000, 45000, "last", "shared-voice"),
            DialogueRow(5, "1", "B", 11000, 14000, "other character", "shared-voice"),
            DialogueRow(6, "2", "A", 1000, 2000, "other episode", "shared-voice"),
        ]
        for first_duration, next_start, last_start in (
            (9000, 40000, 44000),
            (15000, 40000, 44000),
            (40000, 40000, 44000),
            (40001, 40001, 44000),
            (45000, 45000, 47000),
        ):
            with self.subTest(first_duration=first_duration):
                durations = {2: first_duration, 3: 2000, 4: 500, 5: 60000, 6: 1000}
                placed = {p.row.row_number: p for p in place_dialogue(list(reversed(rows)), durations)}
                self.assertEqual([placed[n].start_ms for n in (2, 3, 4, 5, 6)],
                                 [0, next_start, last_start, 11000, 1000])
                self.assertTrue(all(p.start_ms >= p.row.start_ms for p in placed.values()))
                self.assertEqual(placed[3].end_ms, next_start + 2000)
                self.assertEqual(placed[3].delay_ms, next_start - 40000)

    def test_safe_filenames_remain_unique(self):
        names = track_names({("001", "A/B"), ("001", "A:B"), ("001", "a_b"), ("../../", "...")})
        self.assertEqual(len({name.casefold() for name in names.values()}), 4)
        self.assertTrue(all("/" not in name and "\\" not in name for name in names.values()))
        names = timeline_names({"A/B", "A:B", "a_b", "../../"})
        self.assertEqual(len({name.casefold() for name in names.values()}), 4)
        self.assertTrue(all("/" not in name and "\\" not in name for name in names.values()))

    def test_report_exact_schema_and_frame_rate_calculations(self):
        measured = ("audio_duration_ms", "overrun_ms", "percent_overrun", "desired_speed")
        headers = ["excel_row", "episode", "character", "voice_id", "start_TCR", "end_TCR", "fps", "start_ms", "end_ms",
                   "tcr_duration_ms", "original_dialogue", "final_dialogue", "rewritten", "rewrite_attempts",
                   "leading_trim_ms", *(f"pass1_{f}" for f in measured), *(f"pass1_5_{f}" for f in measured),
                   "pass2_speed", "pass2_audio_duration_ms", "pass2_overrun_ms", "pass2_percent_overrun",
                   "within_target", "status", "note", "track", "adjusted_start_TCR", "adjusted_end_TCR"]
        expected_ends = {24: "00:00:01:05", 25: "00:00:01:05", 30: "00:00:01:06",
                         50: "00:00:01:10", 60: "00:00:01:11"}
        path = self.workbook([dialogue(start="00:00:00:01", end="00:00:01:01")])
        for fps, end in expected_ends.items():
            with self.subTest(fps=fps):
                output = self.root / f"report-{fps}.zip"
                run_pipeline(path, output, fps=fps, synthesize=lambda *_: wav_bytes(1173), progress=lambda _: None,
                     fit_lines=False)
                with zipfile.ZipFile(output) as archive:
                    records = list(csv.reader(io.StringIO(archive.read("processing_report.csv").decode("utf-8-sig"))))
                    start_ms, end_ms = timecode_to_ms("00:00:00:01", fps), timecode_to_ms("00:00:01:01", fps)
                    measured_values = ["1173", "173", "17.300000", "1.1730000"]
                    self.assertEqual(records, [headers, [
                        "2", "001", "Alice", "alice-voice", "00:00:00:01", "00:00:01:01", str(fps), str(start_ms), str(end_ms),
                        "1000", "Hello", "Hello", "no", "0", "0", *measured_values, *measured_values,
                        "1.0000000", "1173", "173", "17.300000", "no", "TCR_OUT_OVERRUN", "", "ep001_alice.wav",
                        "00:00:00:01", end,
                    ]])
                    with wave.open(io.BytesIO(archive.read("pass2/tracks/ep001_alice.wav"))) as audio:
                        exact_end = timecode_to_ms("00:00:00:01", fps) + 1173
                        self.assertEqual(audio.getnframes(), exact_end * 24)
                        self.assertAlmostEqual(timecode_to_ms(end, fps), exact_end, delta=500 / fps + 0.5)

    def test_tcr_rounding_rollover_and_frame_roundtrip(self):
        for fps in (24, 25, 30, 50, 60):
            with self.subTest(fps=fps):
                for frame in range(fps):
                    original = f"01:02:03:{frame:02d}"
                    self.assertEqual(format_tcr(timecode_to_ms(original, fps), fps), original)
                self.assertEqual(format_tcr(999, fps), "00:00:01:00")
                self.assertEqual(format_tcr(59999, fps), "00:01:00:00")
                self.assertEqual(format_tcr(3599999, fps), "01:00:00:00")
        self.assertEqual(format_tcr(20, 25), "00:00:00:01")
        self.assertEqual(format_tcr(19, 25), "00:00:00:00")

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
            self.assertIn("pass2/tracks/ep001_alice.wav", archive.namelist())
            self.assertIn("pass2/lines/ep001_alice_row2.wav", archive.namelist())
            self.assertIn("ep001_timeline.csv", archive.namelist())
            self.assertIn("script.xlsx", archive.namelist())
        response.close()
        # run.json lets the Dubbing Quality Check list and import finished runs.
        record = json.loads((self.root / "runtime" / job_id / "run.json").read_text())
        self.assertEqual((record["status"], record["fps"], record["episodes"]), ("done", 25, ["001"]))
        self.assertEqual(record["download_name"], "English dialogue.zip")
        self.assertEqual(client.get("/download/unknown").status_code, 404)

    def test_web_rejects_invalid_workbook_without_starting_generation(self):
        synthesize = Mock()
        app = create_app({"TESTING": True, "RUNTIME_DIR": self.root / "runtime", "SYNTHESIZE": synthesize})
        client = app.test_client()
        path = self.workbook([dialogue(character="")])
        response = client.post("/upload", data={"file": (io.BytesIO(path.read_bytes()), "input.xlsx")})
        self.assertEqual(response.status_code, 400)
        self.assertIn("CHARACTERS", response.json["error"])
        synthesize.assert_not_called()
        response = client.post("/upload", data={"file": (io.BytesIO(b"broken"), "input.xlsx")})
        self.assertEqual(response.status_code, 400)

    # ---- pass 2: fit each line to its slot with FFmpeg --------------------------------------------
    @staticmethod
    def spoken_clip(speech_ms, lead_ms=150, tail_ms=200, freq=440):
        """Speech surrounded by silence, as ElevenLabs returns it."""
        speech = Sine(freq, sample_rate=24000).to_audio_segment(duration=speech_ms).set_channels(1)
        clip = (AudioSegment.silent(lead_ms, frame_rate=24000) + speech
                + AudioSegment.silent(tail_ms, frame_rate=24000))
        return clip.set_sample_width(2).export(io.BytesIO(), format="wav").getvalue()

    def test_fit_speed_measures_speech_and_holds_limits(self):
        # Clips here start on their first sound, as they do after the leading-silence trim.
        self.assertEqual(speech_span(self.spoken_clip(1000)), (150, 1150, 1350))
        self.assertEqual(fit_speed(self.spoken_clip(2300, lead_ms=0), 2000), 1.15)      # too long: faster
        self.assertEqual(fit_speed(self.spoken_clip(900, lead_ms=0), 1000), 0.9)        # too short: slower
        self.assertEqual(fit_speed(self.spoken_clip(1020, lead_ms=0), 1000), 1.0)       # within 2%: normal
        self.assertEqual(fit_speed(self.spoken_clip(4000, lead_ms=0), 1000), MAX_SPEED)  # held at 1.2x
        self.assertEqual(fit_speed(self.spoken_clip(200, lead_ms=0), 1000), MIN_SPEED)   # held at 0.8x
        self.assertEqual(needed_speed(self.spoken_clip(4000, lead_ms=0), 1000), 4.0)    # unlimited
        clip = self.spoken_clip(500)
        with patch("v2.audio_speed.shutil.which", return_value=None):
            self.assertIs(change_speed(clip, 1.0), clip)                            # 1.00x needs no FFmpeg
            with self.assertRaisesRegex(RuntimeError, "FFmpeg"):
                change_speed(clip, 1.2)
        for bad in ("", "bad", "nan", 0.79, 1.21):
            with self.assertRaises(ValueError):
                change_speed(clip, bad)

    def test_pipeline_rewrites_fits_and_flags_lines(self):
        bob = dict(character="Bob", voice="b")
        path = self.workbook([
            dialogue(start="00:00:00:00", end="00:00:02:00", text="long"),               # 1.15x: pass 2 only
            dialogue(start="00:00:03:00", end="00:00:04:00", text="short"),              # 0.90x: pass 2 only
            dialogue(start="00:00:05:00", end="00:00:06:00", text="fits"),               # within 2%: untouched
            dialogue(**bob, start="00:00:00:00", end="00:00:02:00", text="way too long"),          # 1.5x: rewritten
            dialogue(**bob, start="00:00:03:00", end="00:00:05:00", text="much too short here"),   # 0.2x: rewritten
            dialogue(**bob, start="00:00:06:00", end="00:00:08:00", text="this one is stubborn"),  # 2.5x: rewrites fail
            dialogue(**bob, start="00:00:09:00", end="00:00:11:00", text="[Gasps]"),        # no words: skipped
            dialogue(**bob, start="00:00:12:00", end="00:00:14:00", text="[Angry] Get out!"),  # 2 words, 1.5x: skipped
            dialogue(**bob, start="00:00:16:00", end="00:00:18:00", text="No."),             # 1 word, 0.2x: skipped
        ])
        speech = {"long": 2300, "short": 900, "fits": 1010, "way too long": 3000, "shorter": 2100,
                  "shortest": 2000, "much too short here": 400, "padded": 1500, "padded more": 1600,
                  "this one is stubborn": 5000, "still long": 4000, "still long 2": 3000, "[Gasps]": 500,
                  "[Angry] Get out!": 3000, "No.": 400}
        rewrites = {"way too long": ["shorter", "shortest"], "much too short here": ["padded", "padded more"],
                    "this one is stubborn": ["still long", "still long 2"]}
        requests = []

        def rewrite(request):
            requests.append(request)
            return rewrites[request.text][len(request.previous_attempts)]

        synthesized = []

        def synthesize(text, _voice):
            synthesized.append(text)
            return self.spoken_clip(speech[text])

        output = self.root / "rewritten.zip"
        log = []
        summary = run_pipeline(path, output, synthesize=synthesize, rewrite=rewrite, progress=log.append)
        self.assertEqual((summary["rewritten_rows"], summary["manual_review_rows"], summary["skipped_rows"],
                          summary["fitted_rows"]), (3, 1, 3, 3))
        self.assertFalse(summary["success"])                         # 7 of 9 within target
        self.assertEqual(summary["success_percent"], "77.777778")
        self.assertIn("Success: 77.777778% of rows within target", "\n".join(log))
        self.assertIn("3 short rows not rewritten (SKIPPED_REWRITE)", "\n".join(log))
        self.assertIn("WARNING", "\n".join(log))                    # 1 of 9 rows (11%) for manual review
        # Gemini: 2 attempts per rewritten row, each told how the earlier ones came out. Lines of one or
        # two words (or only tags) are never sent, whether too long or too short.
        self.assertEqual([r.text for r in requests],
                         ["way too long"] * 2 + ["much too short here"] * 2 + ["this one is stubborn"] * 2)
        first, second = requests[:2]
        self.assertEqual((first.duration_ms, first.target_ms, first.previous_attempts), (3000, 2000, ()))
        self.assertEqual([(a.text, a.duration_ms) for a in second.previous_attempts], [("shorter", 2100)])
        self.assertEqual(first.context_lines,                      # Bob's other lines
                         ("much too short here", "this one is stubborn", "[Gasps]", "[Angry] Get out!", "No."))
        self.assertEqual(len(synthesized), 9 + 6)                    # pass 1 + one take per rewrite
        with zipfile.ZipFile(output) as archive:
            names = archive.namelist()
            self.assertFalse([n for n in names if n.startswith("pass1_5/")])     # not in the download
            self.assertEqual(len([n for n in names if n.startswith("pass1/lines/")]), 9)
            self.assertEqual(len([n for n in names if n.startswith("pass2/lines/")]), 9)
            self.assertEqual(len([n for n in names if n.startswith("pass2/tracks/")]), 2)
            self.assertEqual(sorted(n for n in names if n.startswith("pass1/tracks/")),
                             ["pass1/tracks/ep001_alice.wav", "pass1/tracks/ep001_bob.wav"])
            report = {int(r["excel_row"]): r for r in csv.DictReader(io.StringIO(
                archive.read("processing_report.csv").decode("utf-8-sig")))}
            pick = lambda row, *keys: tuple(report[row][k] for k in keys)
            # Rows within the limits: pass 2 only. Short lines have negative overruns.
            self.assertEqual(pick(2, "pass1_desired_speed", "pass2_speed", "rewritten", "status"),
                             ("1.1500000", "1.1500000", "no", "OK"))
            self.assertEqual(pick(3, "pass1_overrun_ms", "pass1_percent_overrun", "pass2_speed"),
                             ("-100", "-10.000000", "0.90000000"))
            self.assertEqual(pick(4, "pass2_speed", "status"), ("1.0000000", "OK"))
            self.assertEqual(archive.read("pass2/lines/ep001_alice_row4.wav"),
                             archive.read("pass1/lines/ep001_alice_row4.wav"))
            for row, slot in ((2, 2000), (3, 1000)):
                spoken = speech_span(archive.read(f"pass2/lines/ep001_alice_row{row}.wav"))[1]
                self.assertAlmostEqual(spoken, slot, delta=40)
                self.assertEqual(report[row]["leading_trim_ms"], "150")
            # Rewritten until within 2%: attempt 2 kept, no speed change. Pass 1 keeps the original dub.
            self.assertEqual(pick(5, "final_dialogue", "rewritten", "rewrite_attempts", "pass1_desired_speed",
                                  "pass1_5_desired_speed", "pass2_speed", "status"),
                             ("shortest", "yes", "2", "1.5000000", "1.0000000", "1.0000000", "OK"))
            self.assertEqual(speech_span(archive.read("pass2/lines/ep001_bob_row5.wav"))[1], 2000)
            self.assertEqual(speech_span(archive.read("pass1/lines/ep001_bob_row5.wav"))[1], 3000)
            # Rewritten to within the limits: the closer attempt is kept and pass 2 finishes the fit.
            self.assertEqual(pick(6, "final_dialogue", "pass1_5_desired_speed", "pass2_speed", "status"),
                             ("padded more", "0.80000000", "0.80000000", "OK"))
            # Never within the limits: best rewrite kept at 1.0x for manual review.
            self.assertEqual(pick(7, "final_dialogue", "pass1_5_desired_speed", "pass2_speed", "status"),
                             ("still long 2", "1.5000000", "1.0000000", "MANUAL_REVIEW"))
            self.assertIn("kept at 1.0x", report[7]["note"])
            # One or two words (or none): not rewritten, kept at 1.0x, too long or too short alike.
            for row, text in ((8, "[Gasps]"), (9, "[Angry] Get out!"), (10, "No.")):
                self.assertEqual(pick(row, "final_dialogue", "rewritten", "rewrite_attempts", "pass2_speed", "status",
                                      "note"),
                                 (text, "no", "0", "1.0000000", "SKIPPED_REWRITE", "skipped AI rewriting due to length"))
                self.assertEqual(archive.read(f"pass2/lines/ep001_bob_row{row}.wav"),
                                 archive.read(f"pass1/lines/ep001_bob_row{row}.wav"))
            timeline = archive.read("ep001_timeline.csv").decode("utf-8-sig")
            self.assertIn("Bob: shortest", timeline)
            self.assertNotIn("way too long", timeline)
            # Tracks are built from exactly the clips in pass2/lines/, at their placements.
            alice = AudioSegment.from_wav(io.BytesIO(archive.read("pass2/tracks/ep001_alice.wav")))
            line = AudioSegment.from_wav(io.BytesIO(archive.read("pass2/lines/ep001_alice_row3.wav")))
            offset = timecode_to_ms(report[3]["adjusted_start_TCR"]) * 24 * 2
            self.assertEqual(alice.raw_data[offset:offset + len(line.raw_data)], line.raw_data)

            # Pass 1 tracks are built from exactly the clips in pass1/lines/, placed with the same rule
            # but their own lengths: Bob's original row 5 (3.0 s + 0.2 s tail) runs past row 6's TCR IN
            # at 3 s, so in pass 1 row 6 starts at 3.2 s; in pass 2 the rewrite ends in time.
            def at(track, line_path, ms):
                track = AudioSegment.from_wav(io.BytesIO(archive.read(track))).raw_data
                clip = AudioSegment.from_wav(io.BytesIO(archive.read(line_path))).raw_data
                return track[ms * 48:ms * 48 + len(clip)] == clip
            self.assertTrue(at("pass1/tracks/ep001_alice.wav", "pass1/lines/ep001_alice_row3.wav", 3000))
            self.assertTrue(at("pass1/tracks/ep001_bob.wav", "pass1/lines/ep001_bob_row5.wav", 0))
            self.assertTrue(at("pass1/tracks/ep001_bob.wav", "pass1/lines/ep001_bob_row6.wav", 3200))
            self.assertTrue(at("pass2/tracks/ep001_bob.wav", "pass2/lines/ep001_bob_row6.wav", 3000))
            self.assertNotEqual(archive.read("pass1/tracks/ep001_bob.wav"), archive.read("pass2/tracks/ep001_bob.wav"))
            # Both passes' tracks for an episode cover the same length of time.
            for name in ("ep001_alice.wav", "ep001_bob.wav"):
                pass1 = AudioSegment.from_wav(io.BytesIO(archive.read(f"pass1/tracks/{name}")))
                pass2 = AudioSegment.from_wav(io.BytesIO(archive.read(f"pass2/tracks/{name}")))
                self.assertGreaterEqual(len(pass1), 18000)
                self.assertGreaterEqual(len(pass2), 18000)

    def test_failed_rewrites_keep_the_pass1_take_for_manual_review(self):
        path = self.workbook([dialogue(start="00:00:00:00", end="00:00:02:00", text="far too long")])

        def rewrite(_request):
            raise RuntimeError("Gemini returned HTTP 503 (model test)")

        synthesize = Mock(side_effect=lambda *_: self.spoken_clip(4000))
        summary = run_pipeline(path, self.root / "gemini-down.zip", synthesize=synthesize, rewrite=rewrite,
                               progress=lambda _: None)
        self.assertEqual(synthesize.call_count, 1)                   # nothing regenerated
        self.assertEqual(summary["manual_review_rows"], 1)
        with zipfile.ZipFile(self.root / "gemini-down.zip") as archive:
            record = next(csv.DictReader(io.StringIO(archive.read("processing_report.csv").decode("utf-8-sig"))))
        self.assertEqual((record["rewritten"], record["rewrite_attempts"], record["final_dialogue"], record["status"]),
                         ("no", "2", "far too long", "MANUAL_REVIEW"))
        self.assertIn("HTTP 503", record["note"])

    def test_missing_gemini_key_fails_before_any_generation(self):
        path = self.workbook([dialogue()])
        synthesize = Mock()
        with patch("v2.pipeline.GeminiRewriter", side_effect=ValueError("Set GEMINI_API_KEY in v2/.env")):
            with self.assertRaisesRegex(ValueError, "GEMINI_API_KEY"):
                run_pipeline(path, self.root / "no-key.zip", synthesize=synthesize)
        synthesize.assert_not_called()

    def test_trim_leading_silence_cuts_only_the_blank_start(self):
        from v2.audio_speed import trim_leading_silence
        trimmed, removed = trim_leading_silence(self.spoken_clip(1000, lead_ms=180, tail_ms=200))
        self.assertEqual(removed, 180)
        self.assertEqual(speech_span(trimmed), (0, 1000, 1200))           # speech and tail untouched
        clip = wav_bytes(300)                                               # already starts with sound
        self.assertEqual(trim_leading_silence(clip)[1], 0)
        blank = wav_bytes(500, value=0)                                     # all blank: left as delivered
        self.assertEqual(trim_leading_silence(blank), (blank, 0))

    def test_file_names_are_lowercase_with_an_episode_prefix(self):
        row = DialogueRow(12, "EP01", "ONSCREEN TEXT", 0, 1000, "Hi", "v")
        self.assertEqual(line_name(row), "ep01_onscreen_text_row12.wav")
        self.assertEqual(line_name(DialogueRow(3, "1", "Kuhu", 0, 1000, "Hi", "v")), "ep1_kuhu_row3.wav")
        self.assertEqual(track_names({("1", "KUHU"), ("1", "Kuhu"), ("EP 2", "Guard 1(M)")}),
                         {("1", "KUHU"): "ep1_kuhu.wav", ("1", "Kuhu"): "ep1_kuhu_2.wav",
                          ("EP 2", "Guard 1(M)"): "ep2_guard_1(m).wav"})
        self.assertEqual(timeline_names({"1"}), {"1": "ep1_timeline.csv"})

    def test_missing_ffmpeg_is_refused_before_any_generation(self):
        synthesize = Mock()
        runtime = self.root / "runtime"
        app = create_app({"TESTING": True, "RUNTIME_DIR": runtime, "SYNTHESIZE": synthesize})
        path = self.workbook([dialogue()])
        with patch("v2.audio_speed.shutil.which", return_value=None), patch("v2.app.threading.Thread") as thread:
            response = app.test_client().post("/upload", data={
                "file": (io.BytesIO(path.read_bytes()), "input.xlsx")})
            self.assertEqual(response.status_code, 503)
            self.assertIn("FFmpeg", response.json["error"])
            with self.assertRaisesRegex(RuntimeError, "FFmpeg"):
                run_pipeline(path, self.root / "missing.zip", synthesize=synthesize)
            thread.assert_not_called()
        synthesize.assert_not_called()
        self.assertFalse(runtime.exists())
        self.assertNotIn('name="speed"', app.test_client().get("/").get_data(as_text=True))

    def test_old_runs_are_removed_but_running_and_recent_runs_kept(self):
        import os, time
        from v2.app import RUN_MAX_AGE_SECONDS, remove_old_runs
        runtime = self.root / "runtime"
        old, recent, running = "a" * 32, "b" * 32, "c" * 32
        stale = time.time() - RUN_MAX_AGE_SECONDS - 60
        for name in (old, recent, running):
            (runtime / name).mkdir(parents=True)
            (runtime / name / "AD_v2_output.zip").write_bytes(b"zip")
        (runtime / "notes").mkdir()                           # not a run folder: never touched
        for name in (old, running):
            for path in (runtime / name, runtime / name / "AD_v2_output.zip"):
                os.utime(path, (stale, stale))
        jobs = {old: {"status": "done"}, running: {"status": "running"}}
        self.assertEqual(remove_old_runs(runtime, jobs), 1)
        self.assertEqual(sorted(p.name for p in runtime.iterdir()), sorted([recent, running, "notes"]))
        self.assertNotIn(old, jobs)

    def test_upload_refused_when_storage_is_nearly_full(self):
        synthesize = Mock()
        runtime = self.root / "runtime"
        app = create_app({"TESTING": True, "RUNTIME_DIR": runtime, "SYNTHESIZE": synthesize,
                          "MIN_FREE_BYTES": 10 ** 18})
        path = self.workbook([dialogue()])
        with patch("v2.app.threading.Thread") as thread:
            response = app.test_client().post("/upload", data={
                "file": (io.BytesIO(path.read_bytes()), "input.xlsx")})
        self.assertEqual(response.status_code, 507)
        self.assertIn("Not enough server storage", response.json["error"])
        thread.assert_not_called()
        synthesize.assert_not_called()
        self.assertEqual(list(runtime.iterdir()), [])

    def test_run_size_estimate_counts_dialogue_and_one_track(self):
        from v2.app import estimate_run_bytes
        rows = parse_excel(self.workbook([
            dialogue(start="00:00:00:00", end="00:00:01:00"),
            dialogue(character="Bob", voice="b", start="00:44:59:00", end="00:45:00:00"),
        ]))
        # 2 s of lines: 2x as work files + 4x in the ZIP; plus one 45 min track being built; 48 bytes/ms, x1.25
        self.assertEqual(estimate_run_bytes(rows), int((7 * 2000 + 2_700_000) * 48 * 1.25))

    def test_fit_failure_discards_output(self):
        path = self.workbook([dialogue()])
        output = self.root / "failed-fit.zip"
        with patch("v2.pipeline.apply_pass2", side_effect=RuntimeError("Audio speed adjustment timed out")):
            with self.assertRaisesRegex(RuntimeError, "Excel row 2: Audio speed adjustment timed out"):
                run_pipeline(path, output, synthesize=lambda *_: wav_bytes(), progress=lambda _: None)
        self.assertFalse(output.exists())
        self.assertEqual(list(self.root.glob("ad-v2-*")), [])

    def test_blank_voice_rows_are_skipped_before_validation_and_generation(self):
        path = self.workbook([
            dialogue(voice=None, start="invalid", text="Skip empty cell"),
            dialogue(voice="", character="", text="Skip empty string"),
            dialogue(voice=" \t ", text="Skip whitespace"),
            dialogue(text="Generate this"),
        ])
        rows = parse_excel(path)
        self.assertEqual([row.row_number for row in rows], [5])
        synthesize = Mock(return_value=wav_bytes())
        summary = run_pipeline(path, self.root / "skipped.zip", synthesize=synthesize,
                               progress=lambda _: None)
        # Only the voiced row is generated (its short take is also regenerated by pass 1.5).
        self.assertEqual({call.args for call in synthesize.call_args_list}, {("Generate this", "alice-voice")})
        self.assertEqual(summary["rows"], 1)
        self.assertEqual(summary["tracks"], 1)

    def test_all_blank_voices_produce_no_api_calls(self):
        path = self.workbook([dialogue(voice=None), dialogue(voice="  ")])
        synthesize = Mock()
        with self.assertRaisesRegex(WorkbookValidationError, "No dialogue rows found"):
            run_pipeline(path, self.root / "empty.zip", synthesize=synthesize)
        synthesize.assert_not_called()

    def test_elevenlabs_request_uses_row_voice_and_converts_pcm(self):
        response = Mock()
        response.read.return_value = b"\x00\x00" * 240
        response.__enter__ = Mock(return_value=response)
        response.__exit__ = Mock(return_value=False)
        with patch("v2.tts_client.urlopen", return_value=response) as request, \
                patch.dict("os.environ", {"AD_V2_MODEL_ID": "ignored-legacy-override"}):
            result = ElevenLabsClient(api_key="test-key")("English dialogue", "voice/id")
        sent = request.call_args.args[0]
        self.assertIn("voice%2Fid?output_format=pcm_24000", sent.full_url)
        self.assertEqual(json.loads(sent.data), {
            "text": "English dialogue", "model_id": MODEL_ID,
            "voice_settings": {"stability": 0.5, "similarity_boost": 0.75},
        })
        with wave.open(io.BytesIO(result)) as wav:
            self.assertEqual(wav.getnframes(), 240)
            self.assertEqual(wav.getframerate(), 24000)

    def test_tag_only_rows_get_punctuation_without_added_dialogue(self):
        samples = (
            ("[Gasps]", "[Gasps] ..."),
            ("[LAUGHING]", "[LAUGHING] ..."),
            ("[sighs] [laughs]", "[sighs] [laughs] ..."),
            ("[Gasps] Oh no.", "[Gasps] Oh no."),
        )
        for index, (text, expected) in enumerate(samples):
            with self.subTest(text=text):
                path = self.workbook([dialogue(text=text)])
                response = Mock()
                response.read.return_value = b"\x00\x00" * 240
                response.__enter__ = Mock(return_value=response)
                response.__exit__ = Mock(return_value=False)
                with patch("v2.tts_client.urlopen", return_value=response) as request:
                    run_pipeline(path, self.root / f"sample-{index}.zip",
                                 synthesize=ElevenLabsClient(api_key="test-key"),
                                 progress=lambda _: None)
                self.assertEqual(json.loads(request.call_args.args[0].data)["text"], expected)

    def test_tag_punctuation_is_idempotent_and_preserves_spoken_text(self):
        for text in ("Hello.", "[Gasps] Oh no.", "[laughing] ...", "[Unclosed tag", ""):
            with self.subTest(text=text):
                self.assertEqual(prepare_speech_text(text), text)
        self.assertEqual(prepare_speech_text("  [LAUGHING] \n"), "  [LAUGHING] ...")
        prepared = prepare_speech_text("[Gasps]")
        self.assertEqual(prepare_speech_text(prepared), prepared)

    def test_provider_error_reason_is_reported_with_row_and_credentials_redacted(self):
        path = self.workbook([dialogue(text="[Gasps]")])
        body = json.dumps({"detail": {"status": "example_provider_code",
                                     "message": "Example reason test-key"}}).encode()
        error = HTTPError("https://api.elevenlabs.io", 400, "Bad Request", {}, io.BytesIO(body))
        with patch("v2.tts_client.urlopen", side_effect=error) as request:
            with self.assertRaises(RuntimeError) as caught:
                run_pipeline(path, self.root / "error.zip",
                             synthesize=ElevenLabsClient(api_key="test-key"),
                             progress=lambda _: None)
        message = str(caught.exception)
        self.assertIn("Excel row 2", message)
        self.assertIn("HTTP 400", message)
        self.assertIn("example_provider_code: Example reason [redacted]", message)
        self.assertNotIn("test-key", message)
        self.assertEqual(request.call_count, 1)

    def test_non_json_error_response_is_not_reflected(self):
        error = HTTPError("https://api.elevenlabs.io", 502, "Bad Gateway", {},
                          io.BytesIO(b"<html>Unexpected proxy response</html>"))
        with patch("v2.tts_client.urlopen", side_effect=error):
            with self.assertRaisesRegex(RuntimeError, "HTTP 502.*no structured error details"):
                ElevenLabsClient(api_key="test-key")("[Gasps]", "voice-id")


if __name__ == "__main__":
    unittest.main()
