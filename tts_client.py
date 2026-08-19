# tts_client.py
# Sends Hindi text to Gemini TTS API and returns raw audio bytes
# Uses the high-quality gemini-3.1-flash-tts-preview model
# with the 30 available Gemini voices

import wave
import os
from google import genai
from google.genai import types
from dotenv import load_dotenv

load_dotenv()

# ── Available Gemini voices ───────────────────────────────────────────────────
# Pick any of these — they all support Hindi automatically
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

# Narration style instruction — added before each sentence
# Gemini TTS understands plain English instructions
NARRATION_STYLE = "Speak in a calm, clear, neutral Hindi narration tone for audio description:"


def text_to_speech(text, voice=DEFAULT_VOICE):
    """
    Convert Hindi text to speech using Gemini TTS API.

    Parameters:
        text  : Hindi string to convert
        voice : Gemini voice name (default: Kore)

    Returns:
        audio_bytes : raw PCM audio bytes (24000Hz, 16-bit, mono)
    """

    # Create the Gemini client using GEMINI_API_KEY from .env
    client = genai.Client(api_key=os.environ.get("GEMINI_API_KEY"))

    # Add narration style instruction before the Hindi text
    prompt = f"{NARRATION_STYLE} {text}"

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