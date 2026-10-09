"""Small ElevenLabs adapter; returns mono 24 kHz, 16-bit WAV."""

import io
import json
import os
import wave
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import Request, urlopen

SAMPLE_RATE = 24000
# Single source of model selection for the V2 pipeline.
MODEL_ID = "eleven_v4"
VOICE_SETTINGS = {"stability": 0.5, "similarity_boost": 0.75}


class ElevenLabsClient:
    def __init__(self, api_key=None):
        self.api_key = (api_key or os.environ.get("ELEVENLABS_API_KEY", "")).strip()
        if not self.api_key:
            raise ValueError("Set ELEVENLABS_API_KEY in v2/.env or the environment")

    def __call__(self, text, voice_id):
        request = Request(
            f"https://api.elevenlabs.io/v1/text-to-speech/{quote(voice_id, safe='')}?output_format=pcm_24000",
            data=json.dumps({
                "text": text,
                "model_id": MODEL_ID,
                "voice_settings": VOICE_SETTINGS,
            }).encode("utf-8"),
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
