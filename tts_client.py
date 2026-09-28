# Sends narration text to a supported TTS provider and returns raw PCM bytes.

import base64
import wave
import os
import json
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode
from urllib.request import Request, urlopen
from dotenv import load_dotenv

load_dotenv()

ELEVENLABS_PROVIDER = "elevenlabs"
GOOGLE_PROVIDER = "google"
DEFAULT_PROVIDER = ELEVENLABS_PROVIDER
PROVIDERS = {
    ELEVENLABS_PROVIDER: "ElevenLabs",
    GOOGLE_PROVIDER: "Google Gemini",
}

# ElevenLabs Flash v2.5 language support, keyed by the UI's display names.
ELEVENLABS_LANGUAGE_CODES = {
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
GOOGLE_LANGUAGES = sorted([
    "Afrikaans", "Albanian", "Amharic", "Arabic", "Armenian", "Azerbaijani",
    "Bangla", "Basque", "Belarusian", "Bulgarian", "Burmese", "Catalan",
    "Cebuano", "Chinese (Mandarin)", "Croatian", "Czech", "Danish", "Dutch",
    "English", "Estonian", "Filipino", "Finnish", "French", "Galician",
    "Georgian", "German", "Greek", "Gujarati", "Haitian Creole", "Hebrew",
    "Hindi", "Hungarian", "Icelandic", "Indonesian", "Italian", "Japanese",
    "Javanese", "Kannada", "Konkani", "Korean", "Lao", "Latin", "Latvian",
    "Lithuanian", "Luxembourgish", "Macedonian", "Maithili", "Malagasy",
    "Malay", "Malayalam", "Marathi", "Mongolian", "Nepali",
    "Norwegian (Bokmål)", "Norwegian (Nynorsk)", "Odia", "Pashto", "Persian",
    "Polish", "Portuguese", "Punjabi", "Romanian", "Russian", "Serbian",
    "Sindhi", "Sinhala", "Slovak", "Slovenian", "Spanish", "Swahili",
    "Swedish", "Tamil", "Telugu", "Thai", "Turkish", "Ukrainian", "Urdu",
    "Vietnamese",
])
ELEVENLABS_LANGUAGES = sorted(ELEVENLABS_LANGUAGE_CODES)
LANGUAGE_CODES = ELEVENLABS_LANGUAGE_CODES
LANGUAGES_BY_PROVIDER = {
    ELEVENLABS_PROVIDER: ELEVENLABS_LANGUAGES,
    GOOGLE_PROVIDER: GOOGLE_LANGUAGES,
}
AVAILABLE_LANGUAGES = ELEVENLABS_LANGUAGES
DEFAULT_LANGUAGE = "Hindi"
DEFAULT_VOICE = os.environ.get("ELEVENLABS_VOICE_ID", "").strip()
DEFAULT_GOOGLE_VOICE = "Kore"
GOOGLE_VOICES = [
    "Aoede", "Achird", "Algenib", "Algieba", "Alnilam",
    "Autonoe", "Callirrhoe", "Charon", "Despina", "Enceladus",
    "Erinome", "Fenrir", "Gacrux", "Iapetus", "Kore",
    "Laomedeia", "Leda", "Orus", "Puck", "Pulcherrima",
    "Rasalgethi", "Sadachbia", "Sadaltager", "Schedar", "Sulafat",
    "Umbriel", "Vindemiatrix", "Zephyr", "Zubenelgenubi", "Achernar",
]
MODEL_ID = "eleven_flash_v2_5"
GOOGLE_MODEL_ID = "gemini-3.8-flash-lite-tts"
SAMPLE_RATE = 24000


def validate_provider(provider):
    if provider not in PROVIDERS:
        raise ValueError("Please select Google Gemini or ElevenLabs")
    return provider


def default_voice_for_provider(provider):
    validate_provider(provider)
    return DEFAULT_GOOGLE_VOICE if provider == GOOGLE_PROVIDER else DEFAULT_VOICE


def validate_voice(voice, provider):
    provider = validate_provider(provider)
    voice = (voice or default_voice_for_provider(provider)).strip()
    if provider == GOOGLE_PROVIDER and voice not in GOOGLE_VOICES:
        raise ValueError("Please select a valid Google Gemini voice")
    if provider == ELEVENLABS_PROVIDER and not voice:
        raise ValueError("Set ELEVENLABS_VOICE_ID or enter an ElevenLabs voice ID")
    return voice


def validate_provider_api_key(provider):
    provider = validate_provider(provider)
    if provider == GOOGLE_PROVIDER:
        if not os.environ.get("GEMINI_API_KEY", "").strip():
            raise RuntimeError("Set GEMINI_API_KEY in your .env file")
    elif not os.environ.get("ELEVENLABS_API_KEY", "").strip():
        raise RuntimeError("Set ELEVENLABS_API_KEY in your .env file")


def provider_api_key(provider):
    validate_provider(provider)
    if provider == GOOGLE_PROVIDER:
        return os.environ["GEMINI_API_KEY"].strip()
    return os.environ["ELEVENLABS_API_KEY"].strip()


def validate_language(language, provider=DEFAULT_PROVIDER):
    """Reject languages not offered by the selected provider."""
    provider = validate_provider(provider)
    if language not in LANGUAGES_BY_PROVIDER[provider]:
        raise ValueError(f"Please select a language supported by {PROVIDERS[provider]}")
    return language


def pcm_duration_ms(pcm_bytes):
    """Return the duration of mono 16-bit PCM at the shared sample rate."""
    bytes_per_second = SAMPLE_RATE * 2
    return len(pcm_bytes) * 1000 / bytes_per_second


def text_to_speech(
    text, voice=None, language=DEFAULT_LANGUAGE, provider=DEFAULT_PROVIDER
):
    """
    Convert narration text to speech and return mono 24 kHz 16-bit PCM.

    Parameters:
        text  : Script text in the selected language (not translated here)
        voice : Provider voice name or ID
        language : Narration language (default: Hindi)
        provider : "elevenlabs" or "google"

    Returns:
        audio_bytes : raw PCM audio bytes (24000Hz, 16-bit, mono)
    """

    provider = validate_provider(provider)
    voice = validate_voice(voice, provider)
    language = validate_language(language, provider)
    validate_provider_api_key(provider)

    if provider == GOOGLE_PROVIDER:
        request = Request(
            "https://generativelanguage.googleapis.com/v1beta/interactions",
            data=json.dumps({
                "model": GOOGLE_MODEL_ID,
                "input": [{
                    "type": "user_input",
                    "content": [{
                        "type": "text",
                        "text": text,
                        "annotations": [{
                            "type": "speech_metadata",
                            "style": "calm, clear, neutral narration",
                        }],
                    }],
                }],
                "response_format": {
                    "type": "audio",
                    "mime_type": "audio/l16",
                    "sample_rate": SAMPLE_RATE,
                },
                "generation_config": {"speech_config": [{"voice": voice}]},
                "store": False,
            }).encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                "x-goog-api-key": provider_api_key(provider),
            },
            method="POST",
        )
        try:
            with urlopen(request, timeout=120) as response:
                interaction = json.loads(response.read())
        except HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            raise RuntimeError(f"Google Gemini API error ({exc.code}): {detail}") from exc
        except URLError as exc:
            raise RuntimeError(f"Could not connect to Google Gemini: {exc.reason}") from exc

        output_audio = interaction.get("output_audio")
        if isinstance(output_audio, dict) and output_audio.get("data"):
            encoded_audio = output_audio["data"]
        else:
            encoded_audio = next((
                content["data"]
                for step in interaction.get("steps", [])
                for content in step.get("content", [])
                if content.get("type") == "audio" and content.get("data")
            ), None)
        if not encoded_audio:
            raise RuntimeError("Google Gemini returned no audio data")
        return base64.b64decode(encoded_audio)

    api_key = provider_api_key(provider)

    query = urlencode({"output_format": "pcm_24000"})
    url = f"https://api.elevenlabs.io/v1/text-to-speech/{quote(voice, safe='')}?{query}"
    payload = json.dumps({
        "text": text,
        "model_id": MODEL_ID,
        "language_code": ELEVENLABS_LANGUAGE_CODES[language],
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
    Convert provider PCM bytes into a proper WAV file bytes object.
    Both providers return raw PCM at 24000Hz, 16-bit, mono — wrap it
    in a WAV header so pydub can read it.

    Parameters:
        pcm_bytes   : raw audio bytes from ElevenLabs API
        sample_rate : PCM output sample rate

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
