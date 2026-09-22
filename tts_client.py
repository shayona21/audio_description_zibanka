# tts_client.py
# Sends narration text to Gemini TTS API and returns raw audio bytes
# Uses the high-quality gemini-3.1-flash-tts-preview model
# with the 30 available Gemini voices

import wave
import os
from google import genai
from google.genai import types
from dotenv import load_dotenv

load_dotenv()

# ── Available Gemini voices ───────────────────────────────────────────────────
# Voice characteristics are independent of the narration language.
AVAILABLE_VOICES = [
    "Aoede", "Achird", "Algenib", "Algieba", "Alnilam",
    "Autonoe", "Callirrhoe", "Charon", "Despina", "Enceladus",
    "Erinome", "Fenrir", "Gacrux", "Iapetus", "Kore",
    "Laomedeia", "Leda", "Orus", "Puck", "Pulcherrima",
    "Rasalgethi", "Sadachbia", "Sadaltager", "Schedar", "Sulafat",
    "Umbriel", "Vindemiatrix", "Zephyr", "Zubenelgenubi", "Achernar"
]

# Default voice — calm, clear, good for narration
DEFAULT_VOICE = "Kore"

# Supported Gemini TTS languages (shared by the UI and request validation).
# https://ai.google.dev/gemini-api/docs/speech-generation#supported-languages
AVAILABLE_LANGUAGES = sorted([
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
DEFAULT_LANGUAGE = "Hindi"

# Narration style instruction — added before each sentence
# Gemini TTS understands plain English instructions
NARRATION_STYLE = "Speak in a calm, clear, neutral {language} narration tone for audio description:"


def validate_language(language):
    """Reject unsupported languages before starting work or calling Gemini."""
    if language not in AVAILABLE_LANGUAGES:
        raise ValueError("Please select a supported narration language")
    return language


def text_to_speech(text, voice=DEFAULT_VOICE, language=DEFAULT_LANGUAGE):
    """
    Convert narration text to speech using Gemini TTS API.

    Parameters:
        text  : Script text in the selected language (not translated here)
        voice : Gemini voice name (default: Kore)
        language : Narration language (default: Hindi)

    Returns:
        audio_bytes : raw PCM audio bytes (24000Hz, 16-bit, mono)
    """

    if voice not in AVAILABLE_VOICES:
        raise ValueError(f"Unsupported Gemini voice: {voice}")
    language = validate_language(language)

    # Create the Gemini client using GEMINI_API_KEY from .env
    client = genai.Client(api_key=os.environ.get("GEMINI_API_KEY"))

    # Keep language local to this request so simultaneous jobs stay independent.
    prompt = f"{NARRATION_STYLE.format(language=language)} {text}"

    # Call the Gemini TTS API
    response = client.models.generate_content(
        model="gemini-3.1-flash-tts-preview",
        contents=prompt,
        config=types.GenerateContentConfig(
            response_modalities=["AUDIO"],
            speech_config=types.SpeechConfig(
                voice_config=types.VoiceConfig(
                    prebuilt_voice_config=types.PrebuiltVoiceConfig(
                        voice_name=voice
                    )
                )
            )
        )
    )

    # Extract the raw PCM audio bytes from the response
    audio_bytes = response.candidates[0].content.parts[0].inline_data.data
    return audio_bytes


def pcm_to_wav(pcm_bytes, sample_rate=24000):
    """
    Convert raw PCM bytes from Gemini TTS into a proper WAV file bytes object.
    Gemini returns raw PCM at 24000Hz, 16-bit, mono — we need to wrap it
    in a WAV header so pydub can read it.

    Parameters:
        pcm_bytes   : raw audio bytes from Gemini API
        sample_rate : Gemini TTS outputs at 24000Hz

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
