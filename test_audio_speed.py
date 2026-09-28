import io
import os
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

from pydub import AudioSegment
from pydub.generators import Sine

import app as web
from audio_builder import build_master_timeline
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

    def test_timeline_keeps_scheduled_starts_even_with_overlap(self):
        rows = [dict(row_number=1, start_ms=100, text="one"),
                dict(row_number=2, start_ms=500, text="two")]
        for speed in (1.0, MAX_SPEED_RATE):
            clips = [adjust_audio_speed(wav(self.clip), speed)] * 2
            logs = []
            master = build_master_timeline(rows, clips, 4000, logs.append)
            expected = AudioSegment.silent(duration=4000, frame_rate=24000)
            for row, data in zip(rows, clips):
                expected = expected.overlay(AudioSegment.from_wav(io.BytesIO(data)), position=row["start_ms"])
            self.assertEqual(len(master), 4000)
            self.assertEqual(master.raw_data, expected.raw_data)
            self.assertTrue(any("keeping scheduled start at 500ms" in msg for msg in logs))


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
        rows = [dict(row_number=i + 1, start_ms=i * 2000, text=str(i)) for i in range(2)]
        original = Sine(440, sample_rate=24000).to_audio_segment(duration=1200)
        events = []

        def generate(text, voice, language, provider):
            events.append("generate")
            return original.raw_data

        def adjust(data, speed):
            events.append("adjust")
            return adjust_audio_speed(data, speed)

        lengths = []
        for speed in (1.0, MAX_SPEED_RATE):
            events.clear()
            web.jobs["test"] = {"progress": [], "download_name": "track.zip"}
            with patch.object(web, "parse_file", return_value=rows), \
                 patch.object(web, "text_to_speech", side_effect=generate), \
                 patch.object(web, "adjust_audio_speed", side_effect=adjust):
                web.process_job("test", "script.csv", speed=speed)
            self.assertEqual(events, ["generate", "adjust", "generate", "adjust"])
            self.assertEqual(web.jobs["test"]["status"], "done")
            response = self.client.get("/download/test")
            self.assertEqual(response.status_code, 200)
            with zipfile.ZipFile(io.BytesIO(response.data)) as archive:
                self.assertEqual(archive.namelist(), ["Row_1_2.wav"])
                lengths.append(
                    len(AudioSegment.from_wav(io.BytesIO(archive.read("Row_1_2.wav"))))
                )
            response.close()
        self.assertEqual(lengths, [3200, 3002])

    def test_worker_warns_when_generated_speech_exceeds_row_window(self):
        row = {
            "row_number": 5,
            "start_ms": 22120,
            "end_ms": 24160,
            "duration_ms": 2040,
            "text": "सरस्वती अपने केबिन में हिमांशु के साथ है।",
        }
        web.jobs["test"] = {"progress": []}
        pcm = b"\x00\x00" * (24000 * 3)
        with patch.object(web, "parse_file", return_value=[row]), \
               patch.object(web, "text_to_speech", return_value=pcm):
            web.process_job("test", "script.csv")

        self.assertEqual(web.jobs["test"]["status"], "done")
        warning = next(
            line for line in web.jobs["test"]["progress"]
            if "DURATION OVERRUN" in line
        )
        self.assertIn("Row 5", warning)
        self.assertIn("3.00s", warning)
        self.assertIn("2.04s", warning)
        self.assertIn("0.96s", warning)

    def test_worker_splits_zip_after_each_overrun_row(self):
        rows = [
            {"row_number": 1, "start_ms": 0, "end_ms": 1000, "duration_ms": 1000, "text": "1"},
            {"row_number": 2, "start_ms": 1000, "end_ms": 2000, "duration_ms": 1000, "text": "2"},
            {"row_number": 3, "start_ms": 2000, "end_ms": 4040, "duration_ms": 2040, "text": "3"},
            {"row_number": 4, "start_ms": 4040, "end_ms": 5040, "duration_ms": 1000, "text": "4"},
            {"row_number": 5, "start_ms": 5040, "end_ms": 6040, "duration_ms": 1000, "text": "5"},
        ]
        durations_ms = {"1": 500, "2": 700, "3": 3000, "4": 400, "5": 1500}
        web.jobs["test"] = {"progress": [], "download_name": "track.zip"}

        def generate(text, voice, language, provider):
            return b"\x00\x00" * (durations_ms[text] * 24)

        with patch.object(web, "parse_file", return_value=rows), \
             patch.object(web, "text_to_speech", side_effect=generate):
            web.process_job("test", "script.csv")

        self.assertEqual(web.jobs["test"]["status"], "done")
        response = self.client.get("/download/test")
        with zipfile.ZipFile(io.BytesIO(response.data)) as archive:
            self.assertEqual(archive.namelist(), ["Row_1_3.wav", "Row_4_5.wav"])
            segment_lengths = [
                len(AudioSegment.from_wav(io.BytesIO(archive.read(name))))
                for name in archive.namelist()
            ]
        self.assertEqual(segment_lengths, [5000, 2500])
        response.close()

    def test_worker_separates_actual_clip_collisions(self):
        rows = [
            {"row_number": 1, "start_ms": 0, "end_ms": 2000, "duration_ms": 2000, "text": "first"},
            {"row_number": 2, "start_ms": 1000, "end_ms": 3000, "duration_ms": 2000, "text": "second"},
        ]
        pcm = b"\x00\x00" * (1200 * 24)
        web.jobs["test"] = {"progress": [], "download_name": "track.zip"}
        with patch.object(web, "parse_file", return_value=rows), \
             patch.object(web, "text_to_speech", return_value=pcm):
            web.process_job("test", "script.csv")

        response = self.client.get("/download/test")
        with zipfile.ZipFile(io.BytesIO(response.data)) as archive:
            self.assertEqual(archive.namelist(), ["Row_1_1.wav", "Row_2_2.wav"])
        self.assertTrue(any(
            "OVERLAP DETECTED" in line for line in web.jobs["test"]["progress"]
        ))
        response.close()

    def test_processing_failure_marks_job_as_error(self):
        web.jobs["test"] = {"progress": []}
        with patch.object(web, "parse_file", return_value=[{"text": "one"}]), \
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
