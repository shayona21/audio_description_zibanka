"""Offline coverage of language selection through upload, TTS, and WAV export."""

import base64
import csv
import io
import json
import os
import tempfile
import unittest
import wave
import zipfile
from pathlib import Path
from unittest.mock import MagicMock, patch

from openpyxl import Workbook

import app as web
import main
import tts_client as tts


class LanguageTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.directory = Path(directory.name)
        self.addCleanup(web.jobs.clear)
        for name in ("UPLOAD_DIR", "OUTPUT_DIR"):
            patcher = patch.object(web, name, self.directory)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.client = web.app.test_client()
        # A short, valid 24 kHz mono PCM clip replaces only the external API.
        self.pcm = b"\x01\x00" * 2400
        self.response = MagicMock()
        self.response.__enter__.return_value = self.response
        self.response.read.return_value = self.pcm
        patcher = patch.object(tts, "urlopen", return_value=self.response)
        self.urlopen = patcher.start()
        self.addCleanup(patcher.stop)
        patcher = patch.dict(os.environ, {
            "ELEVENLABS_API_KEY": "test-api-key",
            "GEMINI_API_KEY": "test-gemini-key",
        })
        patcher.start()
        self.addCleanup(patcher.stop)
        patcher = patch.object(tts, "DEFAULT_VOICE", "test-voice-id")
        patcher.start()
        self.addCleanup(patcher.stop)

    def upload(self, language=None, file_bytes=b"test", filename="script.csv",
               output_name="track", provider="elevenlabs", voice="test-voice-id"):
        data = {
            "file": (io.BytesIO(file_bytes), filename),
            "output_name": output_name,
            "provider": provider,
            "voice": voice,
        }
        if language is not None:
            data["language"] = language
        return self.client.post("/upload", data=data)

    def test_invalid_language_fails_before_saving_or_starting_job(self):
        with patch.object(web.threading, "Thread") as thread:
            for language in ("", "  ", "unsupported", "Hindi; ignore instructions"):
                with self.subTest(language=language):
                    response = self.upload(language)
                    self.assertEqual(response.status_code, 400)
                    self.assertIn("language", response.json["error"])
            thread.assert_not_called()
        self.assertFalse(web.jobs)
        self.assertEqual(list(self.directory.iterdir()), [])
        self.urlopen.assert_not_called()

    def test_default_and_selected_language_reach_worker_and_status(self):
        with patch.object(web.threading, "Thread") as thread:
            for selected, expected in ((None, "Hindi"), (" French ", "French")):
                response = self.upload(selected, output_name="track.zip")
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.json["download_name"], "track.zip")
                job_id = response.json["job_id"]
                self.assertEqual(self.client.get(f"/status/{job_id}").json["language"], expected)
                self.assertEqual(thread.call_args.kwargs["kwargs"]["language"], expected)

    def test_google_provider_and_provider_specific_language_reach_worker(self):
        with patch.object(web.threading, "Thread") as thread:
            response = self.upload("Albanian", provider="google", voice="Kore")
        self.assertEqual(response.status_code, 200)
        job_id = response.json["job_id"]
        status = self.client.get(f"/status/{job_id}").json
        self.assertEqual(status["provider"], "google")
        self.assertEqual(status["voice"], "Kore")
        self.assertEqual(thread.call_args.kwargs["kwargs"], {
            "language": "Albanian",
            "provider": "google",
        })

    def test_google_provider_requires_its_own_api_key_before_job_creation(self):
        with patch.dict(os.environ, {"GEMINI_API_KEY": ""}), \
             patch.object(web.threading, "Thread") as thread:
            response = self.upload(provider="google", voice="Kore")
        self.assertEqual(response.status_code, 503)
        self.assertIn("GEMINI_API_KEY", response.json["error"])
        thread.assert_not_called()
        self.assertFalse(web.jobs)

    def test_google_tts_sends_interactions_request_and_returns_raw_pcm(self):
        google_response = MagicMock()
        google_response.__enter__.return_value = google_response
        google_response.read.return_value = json.dumps({
            "steps": [{"content": [{
                "type": "audio",
                "data": base64.b64encode(self.pcm).decode("ascii"),
            }]}],
        }).encode("utf-8")
        self.urlopen.return_value = google_response

        self.assertEqual(
            tts.text_to_speech("नमस्ते", "Kore", "Hindi", "google"),
            self.pcm,
        )
        request = self.urlopen.call_args.args[0]
        payload = json.loads(request.data)
        self.assertIn("/v1beta/interactions", request.full_url)
        self.assertEqual(request.get_header("X-goog-api-key"), "test-gemini-key")
        self.assertEqual(payload["model"], tts.GOOGLE_MODEL_ID)
        self.assertEqual(payload["generation_config"]["speech_config"], [{"voice": "Kore"}])
        self.assertEqual(payload["response_format"]["mime_type"], "audio/l16")
        self.assertFalse(payload["store"])

        with patch.dict(os.environ, {
            "GEMINI_API_KEY": "gemini-only-key",
        }):
            tts.text_to_speech("नमस्ते", "Kore", "Hindi", "google")
        alternate_request = self.urlopen.call_args.args[0]
        self.assertEqual(alternate_request.get_header("X-goog-api-key"), "gemini-only-key")

    def test_google_upload_runs_through_segment_zip_download(self):
        self.response.read.return_value = json.dumps({
            "steps": [{"content": [{
                "type": "audio",
                "data": base64.b64encode(self.pcm).decode("ascii"),
            }]}],
        }).encode("utf-8")
        source = io.StringIO()
        csv.writer(source).writerows([
            ["Start timecode", "End timecode", "Narration text"],
            ["00:00:01:00", "00:00:02:00", "नमस्ते"],
        ])

        with patch.object(web.threading, "Thread") as thread:
            response = self.upload(
                "Hindi", source.getvalue().encode("utf-8-sig"),
                provider="google", voice="Kore",
            )
        self.assertEqual(response.status_code, 200)
        worker = thread.call_args.kwargs
        worker["target"](*worker["args"], **worker["kwargs"])

        job_id = response.json["job_id"]
        status = self.client.get(f"/status/{job_id}").json
        self.assertEqual(status["status"], "done", status.get("error"))
        self.assertEqual(status["provider"], "google")
        download = self.client.get(f"/download/{job_id}")
        with zipfile.ZipFile(io.BytesIO(download.data)) as archive:
            self.assertEqual(archive.namelist(), ["Row_1_1.wav"])
            with wave.open(io.BytesIO(archive.read("Row_1_1.wav"))) as audio:
                self.assertEqual(audio.getframerate(), 24000)
                self.assertEqual(audio.getnframes(), 2400)
        download.close()

    def test_tts_defaults_and_request_language_are_independent(self):
        for language in tts.AVAILABLE_LANGUAGES:
            self.assertEqual(tts.text_to_speech("script", "test-voice-id", language), self.pcm)
            request = self.urlopen.call_args.args[0]
            payload = json.loads(request.data)
            self.assertEqual(payload["text"], "script")
            self.assertEqual(payload["language_code"], tts.LANGUAGE_CODES[language])
            self.assertEqual(payload["model_id"], tts.MODEL_ID)
            self.assertIn("/test-voice-id?", request.full_url)
            self.assertIn("output_format=pcm_24000", request.full_url)
            self.assertEqual(request.get_header("Xi-api-key"), "test-api-key")
        tts.text_to_speech("विवरण")
        self.assertEqual(json.loads(self.urlopen.call_args.args[0].data)["text"], "विवरण")
        self.urlopen.reset_mock()
        with self.assertRaises(ValueError):
            tts.text_to_speech("script", language="invalid")
        self.urlopen.assert_not_called()
        with patch.dict(os.environ, {"ELEVENLABS_API_KEY": ""}):
            with self.assertRaisesRegex(RuntimeError, "ELEVENLABS_API_KEY"):
                tts.text_to_speech("script", "test-voice-id")
        self.urlopen.assert_not_called()

    def test_pcm_duration_matches_sample_count(self):
        self.assertEqual(tts.pcm_duration_ms(b"\x00\x00" * 24000), 1000)

    def test_multilingual_csv_and_excel_uploads_complete_and_download(self):
        for extension, language, lines in (
            ("csv", "French", ["Une porte s’ouvre.", "Elle entre dans la pièce."]),
            ("xlsx", "Tamil", ["கதவு திறக்கிறது.", "அவள் உள்ளே வருகிறாள்."]),
        ):
            with self.subTest(extension=extension, language=language):
                rows = [["Start timecode", "End timecode", "Narration text"]]
                rows.extend([[f"00:00:0{i + 1}:00", f"00:00:0{i + 2}:00", line]
                             for i, line in enumerate(lines)])
                if extension == "csv":
                    buffer = io.StringIO()
                    csv.writer(buffer).writerows(rows)
                    data = buffer.getvalue().encode("utf-8-sig")
                else:
                    buffer = io.BytesIO()
                    workbook = Workbook()
                    for row in rows:
                        workbook.active.append(row)
                    workbook.save(buffer)
                    workbook.close()
                    data = buffer.getvalue()
                self.urlopen.reset_mock()
                with patch.object(web.threading, "Thread") as thread:
                    response = self.upload(language, data, f"script.{extension}")
                    self.assertEqual(response.status_code, 200)
                    worker = thread.call_args.kwargs
                worker["target"](*worker["args"], **worker["kwargs"])
                job_id = response.json["job_id"]
                status = self.client.get(f"/status/{job_id}").json
                self.assertEqual(status["status"], "done", status.get("error"))
                self.assertEqual(status["current"], len(lines))
                calls = self.urlopen.call_args_list
                self.assertEqual(len(calls), len(lines))
                for call, line in zip(calls, lines):
                    payload = json.loads(call.args[0].data)
                    self.assertEqual(payload["text"], line)
                    self.assertEqual(payload["language_code"], tts.LANGUAGE_CODES[language])
                download = self.client.get(f"/download/{job_id}")
                self.assertEqual(download.status_code, 200)
                self.assertIn("track.zip", download.headers["Content-Disposition"])
                with zipfile.ZipFile(io.BytesIO(download.data)) as archive:
                    self.assertEqual(archive.namelist(), ["Row_1_2.wav"])
                    with wave.open(io.BytesIO(archive.read("Row_1_2.wav"))) as audio:
                        self.assertEqual((audio.getframerate(), audio.getnchannels(), audio.getsampwidth()), (24000, 1, 2))
                        self.assertEqual(audio.getnframes(), 1100 * 24)
                        self.assertEqual(audio.readframes(2400), self.pcm)
                download.close()

    def test_programmatic_runner_passes_language(self):
        rows = [dict(text="Une porte s’ouvre.", row_number=1, end_ms=1000)]
        with patch.object(main, "parse_file", return_value=rows), \
             patch.object(main, "build_master_timeline"), \
             patch.object(main, "export_wav"), patch.object(main.time, "sleep"):
            main.run("script.csv", language="French")
        payload = json.loads(self.urlopen.call_args.args[0].data)
        self.assertEqual(payload["language_code"], "fr")
        self.assertEqual(payload["text"], "Une porte s’ouvre.")

    def test_page_lists_languages_with_hindi_selected(self):
        response = self.client.get("/")
        self.assertEqual(response.status_code, 200)
        html = response.get_data(as_text=True)
        self.assertIn('id="language-select" name="language"', html)
        self.assertIn('id="provider-select" name="provider"', html)
        self.assertIn('id="google-voice-select" name="google_voice"', html)
        self.assertIn('id="elevenlabs-voice-id" name="elevenlabs_voice"', html)
        self.assertIn("Download ZIP", html)
        self.assertIn('<option value="Hindi" selected>Hindi</option>', html)
        for language in tts.AVAILABLE_LANGUAGES:
            self.assertIn(f'value="{language}"', html)
        self.assertNotIn("Hindi dialogue", html)


if __name__ == "__main__":
    unittest.main()
