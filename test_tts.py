# test_tts.py
# Quick test to confirm Google Cloud TTS connection is working
# Sends one Hindi sentence and saves the output as a .wav file

import os
from dotenv import load_dotenv
from google.cloud import texttospeech

# Load credentials from .env file
load_dotenv()

def test_google_tts():
    """
    Send one Hindi sentence to Google TTS and save as test_output.wav
    """

    # Step 1: Create the TTS client
    client = texttospeech.TextToSpeechClient()

    # Step 2: Set the Hindi text to convert
    # Using the first line from the Raktanchal Excel file
    hindi_text = "एम एक्स ओरिजिनल का लोगो।"

    # Step 3: Set the input text
    synthesis_input = texttospeech.SynthesisInput(text=hindi_text)

    # Step 4: Set the voice — Hindi female Neural2 voice
    voice = texttospeech.VoiceSelectionParams(
        language_code="hi-IN",
        name="hi-IN-Neural2-A",   # female, very natural
        ssml_gender=texttospeech.SsmlVoiceGender.FEMALE
    )

    # Step 5: Set the output format to WAV (LINEAR16)
    audio_config = texttospeech.AudioConfig(
        audio_encoding=texttospeech.AudioEncoding.LINEAR16,
        sample_rate_hertz=48000    # standard for video post-production
    )

    # Step 6: Call the API
    print("Calling Google TTS API...")
    response = client.synthesize_speech(
        input=synthesis_input,
        voice=voice,
        audio_config=audio_config
    )

    # Step 7: Save the output as a .wav file
    output_file = "test_output.wav"
    with open(output_file, "wb") as f:
        f.write(response.audio_content)

    print(f"Success! Audio saved to {output_file}")
    print(f"File size: {len(response.audio_content)} bytes")

if __name__ == "__main__":
    test_google_tts()