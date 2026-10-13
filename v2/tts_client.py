"""Small ElevenLabs adapter; returns mono 24 kHz, 16-bit WAV."""

import io
import json
import os
import re
import wave
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import Request, urlopen

SAMPLE_RATE = 24000
# Single source of model selection for the V2 pipeline.
MODEL_ID = "eleven_v4"
VOICE_SETTINGS = {"stability": 0.5, "similarity_boost": 0.75}


def prepare_speech_text(text):
    """Add pacing punctuation to tag-only rows without inventing dialogue.

    The API can reject tags alone as input_text_empty after stripping tags.
    An ellipsis supplies non-tag content; live generation quality still needs
    review because delivery-only tags do not necessarily describe a sound.
    """
    if re.fullmatch(r"\s*(?:\[[^\[\]\r\n]+\]\s*)+", text):
        return text.rstrip() + " ..."
    return text


def provider_error_detail(error, api_key):
    """Extract the provider's structured reason without exposing credentials."""
    try:
        payload = json.loads(error.read(8192).decode("utf-8"))
    except (OSError, UnicodeError, ValueError):
        return ""
    if not isinstance(payload, dict):
        return ""
    detail = payload.get("detail", {})
    if isinstance(detail, str):
        parts = [detail]
    elif isinstance(detail, dict):
        parts = [detail.get("status"), detail.get("message")]
    else:
        parts = []
    message = ": ".join(part for part in parts if isinstance(part, str) and part.strip())
    if api_key:
        message = message.replace(api_key, "[redacted]")
    return " ".join(message.split())[:600]


class ElevenLabsClient:
    def __init__(self, api_key=None):
        self.api_key = (api_key or os.environ.get("ELEVENLABS_API_KEY", "")).strip()
        if not self.api_key:
            raise ValueError("Set ELEVENLABS_API_KEY in v2/.env or the environment")

    def __call__(self, text, voice_id):
        request = Request(
            f"https://api.elevenlabs.io/v1/text-to-speech/{quote(voice_id, safe='')}?output_format=pcm_24000",
            data=json.dumps({
                "text": prepare_speech_text(text),
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
            detail = provider_error_detail(exc, self.api_key)
            reason = f": {detail}" if detail else "; no structured error details returned"
            raise RuntimeError(f"ElevenLabs returned HTTP {exc.code} (model {MODEL_ID}){reason}") from exc
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
