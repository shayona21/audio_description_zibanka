# Sends narration text to ElevenLabs TTS and returns raw PCM audio bytes.

import wave
import os
import json
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode
from urllib.request import Request, urlopen
from dotenv import load_dotenv

load_dotenv()

# ElevenLabs Flash v2.5 language support, keyed by the UI's display names.
LANGUAGE_CODES = {
    "Arabic": "ar", "Bulgarian": "bg", "Chinese (Mandarin)": "zh",
    "Croatian": "hr", "Czech": "cs", "Danish": "da", "Dutch": "nl",
    "English": "en", "Filipino": "fil", "Finnish": "fi", "French": "fr",
    "German": "de", "Greek": "el", "Hindi": "hi", "Hungarian": "hu",
    "Indonesian": "id", "Italian": "it", "Japanese": "ja", "Korean": "ko",
    "Malay": "ms", "Norwegian": "no", "Polish": "pl", "Portuguese": "pt",
    "Romanian": "ro", "Russian": "ru", "Slovak": "sk", "Spanish": "es",
    "Swedish": "sv", "Tamil": "ta", "Turkish": "tr", "Ukrainian": "uk",
    "Vietnamese": "vi",
}
AVAILABLE_LANGUAGES = sorted(LANGUAGE_CODES)
DEFAULT_LANGUAGE = "Hindi"
DEFAULT_VOICE = os.environ.get("ELEVENLABS_VOICE_ID", "").strip()
MODEL_ID = "eleven_flash_v2_5"
SAMPLE_RATE = 24000


def validate_language(language):
    """Reject languages unsupported by ElevenLabs Multilingual v2."""
    if language not in AVAILABLE_LANGUAGES:
        raise ValueError("Please select a supported narration language")
    return language


def text_to_speech(text, voice=DEFAULT_VOICE, language=DEFAULT_LANGUAGE):
    """
    Convert narration text to speech using the ElevenLabs text-to-speech API.

    Parameters:
        text  : Script text in the selected language (not translated here)
        voice : ElevenLabs voice ID (default: ELEVENLABS_VOICE_ID from .env)
        language : Narration language (default: Hindi)

    Returns:
        audio_bytes : raw PCM audio bytes (24000Hz, 16-bit, mono)
    """

    voice = (voice or DEFAULT_VOICE).strip()
    if not voice:
        raise ValueError("Set ELEVENLABS_VOICE_ID or enter an ElevenLabs voice ID")
    language = validate_language(language)

    api_key = os.environ.get("ELEVENLABS_API_KEY")
    if not api_key:
        raise RuntimeError("Set ELEVENLABS_API_KEY in your .env file")

    query = urlencode({"output_format": "pcm_24000"})
    url = f"https://api.elevenlabs.io/v1/text-to-speech/{quote(voice, safe='')}?{query}"
    payload = json.dumps({
        "text": text,
        "model_id": MODEL_ID,
        "language_code": LANGUAGE_CODES[language],
    }).encode("utf-8")
    request = Request(
        url,
        data=payload,
        headers={
            "Accept": "application/octet-stream",
            "Content-Type": "application/json",
            "xi-api-key": api_key,
        },
        method="POST",
    )

    try:
        with urlopen(request, timeout=120) as response:
            return response.read()
    except HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"ElevenLabs API error ({exc.code}): {detail}") from exc
    except URLError as exc:
        raise RuntimeError(f"Could not connect to ElevenLabs: {exc.reason}") from exc


def pcm_to_wav(pcm_bytes, sample_rate=24000):
    """
    Convert raw PCM bytes from ElevenLabs into a proper WAV file bytes object.
    ElevenLabs returns raw PCM at 24000Hz, 16-bit, mono — we need to wrap it
    in a WAV header so pydub can read it.

    Parameters:
        pcm_bytes   : raw audio bytes from ElevenLabs API
        sample_rate : ElevenLabs PCM output sample rate

    Returns:
        wav_bytes : properly formatted WAV bytes
    """
    import io

    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as wf:
        wf.setnchannels(1)       # mono
        wf.setsampwidth(2)       # 16-bit = 2 bytes
        wf.setframerate(sample_rate)
        wf.writeframes(pcm_bytes)

    buffer.seek(0)
    return buffer.read()
