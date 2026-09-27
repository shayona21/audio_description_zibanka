"""Manual ElevenLabs API smoke test; run directly after configuring .env."""

from pathlib import Path

from tts_client import DEFAULT_LANGUAGE, DEFAULT_VOICE, pcm_to_wav, text_to_speech


def run_elevenlabs_smoke_test():
    audio = text_to_speech(
        "एम एक्स ओरिजिनल का लोगो।",
        voice=DEFAULT_VOICE,
        language=DEFAULT_LANGUAGE,
    )
    output_path = Path("test_output.wav")
    output_path.write_bytes(pcm_to_wav(audio))
    print(f"Success! Audio saved to {output_path}")


if __name__ == "__main__":
    run_elevenlabs_smoke_test()