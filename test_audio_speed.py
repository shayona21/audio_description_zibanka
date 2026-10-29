import io
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from pydub import AudioSegment
from pydub.generators import Sine

import app as web
from audio_builder import audio_duration_ms, build_master_timeline
from audio_overlap import MAX_FIX_SPEED, SEQUENTIAL_GAP_MS, fix_overlaps
from audio_speed import MAX_SPEED_RATE, adjust_audio_speed, validate_speed


def wav(clip):
    return clip.export(io.BytesIO(), format="wav").getvalue()


class AudioSpeedTests(unittest.TestCase):
    def setUp(self):
        self.clip = Sine(440, sample_rate=24000).to_audio_segment(duration=2000)

    def test_default_is_byte_exact_and_needs_no_ffmpeg(self):
        original = wav(self.clip)
        with patch("audio_speed.shutil.which", return_value=None):
            self.assertIs(adjust_audio_speed(original), original)
            with self.assertRaisesRegex(RuntimeError, "FFmpeg"):
                adjust_audio_speed(original, MAX_SPEED_RATE)

    def test_duration_pitch_and_format_at_every_slider_value(self):
        for value in range(101, round(MAX_SPEED_RATE * 100) + 1):
            speed = value / 100
            with self.subTest(speed=speed):
                clip = AudioSegment.from_wav(io.BytesIO(adjust_audio_speed(wav(self.clip), speed)))
                self.assertAlmostEqual(len(clip), 2000 / speed, delta=40)
                self.assertEqual((clip.frame_rate, clip.channels, clip.sample_width), (24000, 1, 2))
                # Count positive zero crossings in a central second of steady tone.
                samples = clip[300:1300].get_array_of_samples()
                crossings = sum(a <= 0 < b for a, b in zip(samples, samples[1:]))
                self.assertAlmostEqual(crossings, 440, delta=2)

    def test_invalid_speeds(self):
        for value in ("", "bad", "nan", "inf", "-inf", 0, 0.99, MAX_SPEED_RATE + 0.001, None):
            with self.subTest(value=value), self.assertRaises(ValueError):
                validate_speed(value)

    def test_timeline_places_clips_at_their_start_times_and_never_cuts_speech(self):
        rows = [dict(row_number=1, start_ms=100, text="one"),
                dict(row_number=2, start_ms=500, text="two")]
        clips = [wav(self.clip)] * 2
        master = build_master_timeline(rows, clips, [100, 2600], 4000, lambda _m: None)
        expected = AudioSegment.silent(duration=4600, frame_rate=24000)   # 2.6 s + 2 s > 4 s
        for start in (100, 2600):
            expected = expected.overlay(self.clip, position=start)
        self.assertEqual(master.raw_data, expected.raw_data)


class OverlapRuleTests(unittest.TestCase):
    """audio_overlap.fix_overlaps: speed up below 1.5x, else play after the previous line."""

    @staticmethod
    def clip(duration_ms):
        return wav(Sine(440, sample_rate=24000).to_audio_segment(duration=duration_ms))

    def run_rule(self, starts, durations):
        rows = [dict(row_number=i + 1, start_ms=start) for i, start in enumerate(starts)]
        logs = []
        clips, placed = fix_overlaps(rows, [self.clip(d) for d in durations], logs.append)
        return [audio_duration_ms(c) for c in clips], placed, logs

    def test_no_overlap_changes_nothing(self):
        lengths, starts, logs = self.run_rule([0, 1000], [1000, 500])   # ends exactly at 1 s
        self.assertEqual((lengths, starts, logs), ([1000, 500], [0, 1000], []))

    def test_overlap_below_the_limit_is_sped_up_and_keeps_its_start(self):
        lengths, starts, logs = self.run_rule([0, 1000], [1200, 500])   # needs 1.20x
        self.assertEqual(starts, [0, 1000])
        self.assertLessEqual(lengths[0], 1000)
        self.assertGreater(lengths[0], 950)
        self.assertEqual(logs, ["OVERLAP FIXED — Row 1 sped up 1.20x to end before the next line (pitch preserved)."])

    def test_overlap_at_or_above_the_limit_plays_sequentially(self):
        for duration, needed in ((1500, "1.50x"), (1600, "1.60x")):          # 1.5x is not below the limit
            with self.subTest(duration=duration):
                lengths, starts, logs = self.run_rule([0, 1000], [duration, 500])
                self.assertEqual(lengths, [duration, 500])                     # normal speed
                self.assertEqual(starts, [0, duration + SEQUENTIAL_GAP_MS])    # 0.5 s after it ends
                self.assertIn(f"would need {needed}", logs[0])
                self.assertTrue(logs[1].startswith("OVERLAP — Row 2 placed after the previous line"))

    def test_a_pushed_line_measures_its_overlap_from_where_it_now_starts(self):
        # Row 2 is pushed to 2.1 s, which leaves it 0.9 s before row 3 at 3 s: needs 1.11x.
        lengths, starts, logs = self.run_rule([0, 1000, 3000], [1600, 1000, 300])
        self.assertEqual(starts, [0, 2100, 3000])
        self.assertLessEqual(lengths[1], 900)
        self.assertTrue(any(line.startswith("OVERLAP FIXED — Row 2 sped up 1.11x") for line in logs))

    def test_same_start_or_missing_ffmpeg_plays_sequentially(self):
        lengths, starts, logs = self.run_rule([0, 0], [800, 500])          # no time at all
        self.assertEqual(starts, [0, 800 + SEQUENTIAL_GAP_MS])
        self.assertIn("unlimited speed-up", logs[0])
        with patch("audio_speed.subprocess.run", side_effect=OSError("no ffmpeg")):
            lengths, starts, logs = self.run_rule([0, 1000], [1200, 500])
        self.assertEqual(starts, [0, 1700])
        self.assertIn("speed-up unavailable", logs[0])

    def test_limit_value(self):
        self.assertEqual(MAX_FIX_SPEED, 1.5)


class WebSpeedTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.addCleanup(web.jobs.clear)
        for name in ("UPLOAD_DIR", "OUTPUT_DIR"):
            patcher = patch.object(web, name, Path(self.directory.name))
            patcher.start()
            self.addCleanup(patcher.stop)
        self.client = web.app.test_client()
        patcher = patch.dict(os.environ, {
            "ELEVENLABS_API_KEY": "test-api-key",
            "GEMINI_API_KEY": "test-gemini-key",
        })
        patcher.start()
        self.addCleanup(patcher.stop)
        voice_patcher = patch("tts_client.DEFAULT_VOICE", "test-voice-id")
        voice_patcher.start()
        self.addCleanup(voice_patcher.stop)

    def upload(self, speed=None):
        data = {
            "file": (io.BytesIO(b"test"), "script.csv"),
            "output_name": "track",
            "voice": "test-voice-id",
        }
        if speed is not None:
            data["speed"] = speed
        return self.client.post("/upload", data=data)

    def run_rows(self, rows, durations_ms, speed=1.0):
        """Run the worker on parsed rows with silent clips of the given lengths; return its job."""
        web.jobs["test"] = {"progress": [], "download_name": "track.wav"}
        pcm = [b"\x00\x00" * (duration * 24) for duration in durations_ms]
        with patch.object(web, "parse_file", return_value=rows), \
             patch.object(web, "text_to_speech", side_effect=pcm):
            web.process_job("test", "script.csv", speed=speed)
        self.assertEqual(web.jobs["test"]["status"], "done", web.jobs["test"].get("error"))
        return web.jobs["test"]

    def test_upload_generates_and_downloads_one_wav(self):
        script = (
            "Start timecode,End timecode,Dialogue\n"
            "00:00:01:00,00:00:01:10,first\n"
            "00:00:02:00,00:00:02:10,last\n"
        ).encode()
        # The first line has 1 s before the last one starts: 1.2 s is sped up (1.20x);
        # 9 s would need 9x, so it plays in full and the last line follows 0.5 s later.
        for first_duration, last_start in ((1200, 2000), (9000, 10500)):
            with self.subTest(first_duration=first_duration):
                clips = [
                    Sine(440, sample_rate=24000).to_audio_segment(duration=first_duration),
                    Sine(660, sample_rate=24000).to_audio_segment(duration=1000),
                ]
                with patch.object(web.threading, "Thread") as thread:
                    response = self.client.post("/upload", data={
                        "file": (io.BytesIO(script), "script.csv"),
                        "output_name": "track.zip", "voice": "test-voice-id",
                    })
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.json["download_name"], "track.wav")
                worker = thread.call_args.kwargs
                self.assertNotIn("output_mode", worker["kwargs"])
                with patch.object(web, "text_to_speech", side_effect=[c.raw_data for c in clips]) as tts:
                    worker["target"](*worker["args"], **worker["kwargs"])
                self.assertEqual(tts.call_count, 2)
                job_id = response.json["job_id"]
                status = self.client.get(f"/status/{job_id}").json
                self.assertEqual(status["status"], "done", status.get("error"))
                self.assertTrue(any(f"Placed row 2 at {last_start}ms" in line for line in status["progress"]))
                download = self.client.get(f"/download/{job_id}")
                try:
                    self.assertEqual(download.status_code, 200)
                    self.assertEqual(download.mimetype, "audio/wav")
                    self.assertIn("track.wav", download.headers["Content-Disposition"])
                    actual = AudioSegment.from_wav(io.BytesIO(download.data))
                    self.assertEqual(len(actual), max(2400 + 5000, last_start + 1000))
                    self.assertEqual(actual[last_start:last_start + 1000].raw_data, clips[1].raw_data)
                    if first_duration == 9000:
                        self.assertEqual(actual[1000:10000].raw_data, clips[0].raw_data)   # normal speed
                finally:
                    download.close()

    def test_upload_validates_and_passes_speed_to_worker(self):
        with patch.object(web.threading, "Thread") as thread:
            for value in ("nan", "inf", "0", str(MAX_SPEED_RATE + 0.01), "bad", ""):
                self.assertEqual(self.upload(value).status_code, 400)
            thread.assert_not_called()
            for value, expected in ((None, 1.0), (str(MAX_SPEED_RATE), MAX_SPEED_RATE)):
                response = self.upload(value)
                self.assertEqual(response.status_code, 200)
                job_id = response.json["job_id"]
                self.assertEqual(web.jobs[job_id]["speed"], expected)
                self.assertEqual(thread.call_args.kwargs["args"][-1], expected)

    def test_missing_ffmpeg_is_reported_before_job_creation(self):
        with patch("audio_speed.shutil.which", return_value=None):
            self.assertEqual(self.upload(str(MAX_SPEED_RATE)).status_code, 503)
            self.assertFalse(web.jobs)

    def test_worker_adjusts_each_line_before_next_generation_and_downloads(self):
        rows = [dict(row_number=i + 1, start_ms=i * 2000, end_ms=i * 2000 + 500, text=str(i)) for i in range(2)]
        original = Sine(440, sample_rate=24000).to_audio_segment(duration=1200)
        events = []

        def generate(text, voice, language, provider):
            events.append("generate")
            return original.raw_data

        def adjust(data, speed):
            events.append("adjust")
            return adjust_audio_speed(data, speed)

        for speed in (1.0, MAX_SPEED_RATE):
            events.clear()
            web.jobs["test"] = {"progress": [], "download_name": "track.wav"}
            with patch.object(web, "parse_file", return_value=rows), \
                 patch.object(web, "text_to_speech", side_effect=generate), \
                 patch.object(web, "adjust_audio_speed", side_effect=adjust):
                web.process_job("test", "script.csv", speed=speed)
            self.assertEqual(events, ["generate", "adjust", "generate", "adjust"])
            self.assertEqual(web.jobs["test"]["status"], "done")
            response = self.client.get("/download/test")
            self.assertEqual(response.status_code, 200)
            self.assertEqual(len(AudioSegment.from_wav(io.BytesIO(response.data))), 2500 + 5000)
            response.close()

    def test_worker_logs_each_moved_line(self):
        rows = [
            {"row_number": 5, "start_ms": 22120, "end_ms": 26120, "text": "सरस्वती अपने केबिन में हिमांशु के साथ है।"},
            {"row_number": 6, "start_ms": 24160, "end_ms": 25160, "text": "next line"},
        ]
        # Row 5 has 2.04 s before row 6: 3.2 s would need 1.57x, so it plays in full until
        # 25.32 s and row 6 starts 0.5 s later, at 25.82 s (1.66 s after its own 24.16 s).
        job = self.run_rows(rows, [3200, 1000])
        moved = [line for line in job["progress"] if line.startswith("OVERLAP")]
        self.assertEqual(moved, [
            "OVERLAP — Row 5 would need 1.57x (limit below 1.5x): kept at normal speed; "
            "the next line is placed after it.",
            "OVERLAP — Row 6 placed after the previous line: starts 1.66s later than its start timecode.",
        ])
        self.assertTrue(any("Placed row 6 at 25820ms" in line for line in job["progress"]))

    def test_worker_reads_csv_and_excel_and_moves_only_real_collisions(self):
        from openpyxl import Workbook

        source_rows = [
            ["Start timecode", "End timecode", "Dialogue"],
            ["00:00:01:00", "00:00:01:10", "first"],
            ["00:00:03:00", "00:00:03:10", "last"],
        ]
        csv_path = Path(self.directory.name) / "script.csv"
        csv_path.write_text(
            "\n".join(",".join(row) for row in source_rows), encoding="utf-8"
        )
        excel_path = Path(self.directory.name) / "script.xlsx"
        workbook = Workbook()
        for row in source_rows:
            workbook.active.append(row)
        workbook.save(excel_path)
        workbook.close()

        # Speech may run past its end timecode and still finish before the next line starts
        # (at 3 s). Ending exactly at 3 s is not an overlap; 2001 and 2300 ms are sped up;
        # 3100 ms would need 1.55x, so the next line is placed after it.
        for path in (csv_path, excel_path):
            for duration_ms in (1200, 2000, 2001, 2300, 3100):
                with self.subTest(path=path.name, duration_ms=duration_ms):
                    web.jobs["test"] = {"progress": []}
                    pcm = b"\x00\x00" * (duration_ms * 24)
                    with patch.object(web, "text_to_speech", return_value=pcm):
                        web.process_job("test", str(path))
                    self.assertEqual(web.jobs["test"]["status"], "done")
                    progress = web.jobs["test"]["progress"]
                    fixed = sum(line.startswith("OVERLAP FIXED") for line in progress)
                    moved = sum(line.startswith("OVERLAP — Row 2 placed after") for line in progress)
                    self.assertEqual((fixed, moved), (int(2000 < duration_ms < 3000), int(duration_ms == 3100)))
                    self.assertTrue(web.jobs["test"]["output"].endswith(".wav"))

    def test_collision_uses_output_length_after_speed_adjustment(self):
        rows = [
            dict(row_number=1, start_ms=0, end_ms=500, text="first"),
            dict(row_number=2, start_ms=1100, end_ms=1600, text="last"),
        ]
        pcm = Sine(440, sample_rate=24000).to_audio_segment(duration=1200).raw_data
        # 1.2 s at 1.00x overlaps the 1.1 s start (fixed at 1.09x); at 1.20x it lasts 1.0 s and fits.
        for speed, fixed in ((1.0, 1), (MAX_SPEED_RATE, 0)):
            with self.subTest(speed=speed):
                web.jobs["test"] = {"progress": []}
                with patch.object(web, "parse_file", return_value=rows), \
                     patch.object(web, "text_to_speech", return_value=pcm):
                    web.process_job("test", "script.csv", speed=speed)
                self.assertEqual(web.jobs["test"]["status"], "done")
                self.assertEqual(sum(line.startswith("OVERLAP FIXED") for line in web.jobs["test"]["progress"]), fixed)

    def test_single_row_keeps_end_buffer_and_never_overlaps(self):
        job = self.run_rows([dict(row_number=1, start_ms=1000, end_ms=1500, text="only")], [3000])
        self.assertFalse(any("OVERLAP" in line for line in job["progress"]))
        self.assertEqual(len(AudioSegment.from_wav(job["output"])), 1500 + 5000)

    def test_rows_out_of_time_order_are_placed_by_start_time(self):
        # The client's file is used as given: rows are sorted by start time, not changed.
        rows = [
            dict(row_number=1, start_ms=5000, end_ms=6000, text="late"),
            dict(row_number=2, start_ms=1000, end_ms=2000, text="early"),
        ]
        job = self.run_rows(rows, [600, 700])  # TTS is called in time order: early, then late
        self.assertTrue(any("Placed row 2 at 1000ms" in line for line in job["progress"]))
        self.assertTrue(any("Placed row 1 at 5000ms" in line for line in job["progress"]))
        self.assertFalse(any(line.startswith("OVERLAP") for line in job["progress"]))

    def test_lines_beyond_the_limit_push_a_chain_and_later_lines_return_to_their_start(self):
        rows = [
            {"row_number": 1, "start_ms": 0, "end_ms": 1000, "text": "1"},
            {"row_number": 2, "start_ms": 1000, "end_ms": 2000, "text": "2"},
            {"row_number": 3, "start_ms": 2000, "end_ms": 4040, "text": "3"},
            {"row_number": 4, "start_ms": 9000, "end_ms": 10000, "text": "4"},
        ]
        # Row 1 needs 1.5x (not below the limit): row 2 follows at 2.0 s. Row 2 then has no time
        # before row 3 (also 2.0 s), so row 3 follows at 3.2 s. Row 4 is clear at 9 s.
        job = self.run_rows(rows, [1500, 700, 3000, 400])
        progress = job["progress"]
        for row, start in ((1, 0), (2, 2000), (3, 3200), (4, 9000)):
            self.assertTrue(any(f"Placed row {row} at {start}ms" in line for line in progress), (row, start))
        self.assertEqual(len(AudioSegment.from_wav(job["output"])), 10000 + 5000)

    def test_processing_failure_marks_job_as_error(self):
        web.jobs["test"] = {"progress": []}
        with patch.object(web, "parse_file", return_value=[{"text": "one", "start_ms": 0, "end_ms": 500}]), \
             patch.object(web, "text_to_speech", return_value=b"\x00\x00"), \
             patch.object(web, "adjust_audio_speed", side_effect=RuntimeError("Audio speed adjustment timed out")):
            web.process_job("test", "script.csv", speed=MAX_SPEED_RATE)
        self.assertEqual(web.jobs["test"]["status"], "error")
        self.assertIn("timed out", web.jobs["test"]["error"])

    def test_page_renders_speed_control(self):
        response = self.client.get("/")
        self.assertEqual(response.status_code, 200)
        self.assertIn(f'name="speed" min="1.00" max="{MAX_SPEED_RATE:.2f}" step="0.01" value="1.00"'.encode(), response.data)
        self.assertIn(b'formData.append("speed", speedDial.value)', response.data)


if __name__ == "__main__":
    unittest.main()
