"""Small ElevenLabs adapter; returns mono 24 kHz, 16-bit WAV."""

import io
import json
import os
import wave
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import Request, urlopen

SAMPLE_RATE = 24000
DEFAULT_MODEL = "eleven_multilingual_v2"


class ElevenLabsClient:
    def __init__(self, api_key=None, model_id=None):
        self.api_key = (api_key or os.environ.get("ELEVENLABS_API_KEY", "")).strip()
        self.model_id = (model_id or os.environ.get("AD_V2_MODEL_ID", DEFAULT_MODEL)).strip()
        if not self.api_key:
            raise ValueError("Set ELEVENLABS_API_KEY in v2/.env or the environment")

    def __call__(self, text, voice_id):
        request = Request(
            f"https://api.elevenlabs.io/v1/text-to-speech/{quote(voice_id, safe='')}?output_format=pcm_24000",
            data=json.dumps({"text": text, "model_id": self.model_id}).encode("utf-8"),
            headers={"Content-Type": "application/json", "Accept": "application/octet-stream",
                     "xi-api-key": self.api_key}, method="POST",
        )
        try:
            with urlopen(request, timeout=120) as response:
                pcm = response.read()
        except HTTPError as exc:
            # Avoid reflecting provider responses or credentials into the browser.
            raise RuntimeError(f"ElevenLabs returned HTTP {exc.code}; check voice access, model, key and quota") from exc
        except (URLError, TimeoutError) as exc:
            raise RuntimeError("ElevenLabs connection failed or timed out") from exc
        if not pcm or len(pcm) % 2:
            raise RuntimeError("ElevenLabs returned empty or invalid PCM audio")
        output = io.BytesIO()
        with wave.open(output, "wb") as wav:
            wav.setnchannels(1)
            wav.setsampwidth(2)
            wav.setframerate(SAMPLE_RATE)
            wav.writeframes(pcm)
        return output.getvalue()
