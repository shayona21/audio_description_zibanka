"""Generate a sample for the configured ElevenLabs voice; run directly."""

from pathlib import Path

from tts_client import DEFAULT_LANGUAGE, DEFAULT_VOICE, pcm_to_wav, text_to_speech


def generate_voice_sample():
    audio = text_to_speech(
        "सरस्वती धीरे से कमरे में प्रवेश करती है और खिड़की के पास खड़ी हो जाती है।",
        voice=DEFAULT_VOICE,
        language=DEFAULT_LANGUAGE,
    )
    output_path = Path("voice_samples/elevenlabs_voice.wav")
    output_path.parent.mkdir(exist_ok=True)
    output_path.write_bytes(pcm_to_wav(audio))
    print(f"Saved voice sample to {output_path}")


if __name__ == "__main__":
    generate_voice_sample()