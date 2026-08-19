# tts_client.py
# Sends one Hindi text string to Google TTS and returns audio bytes

from google.cloud import texttospeech

def text_to_speech(text):
    """
    Convert Hindi text to speech using Google Cloud TTS.

    Parameters:
        text : Hindi string to convert

    Returns:
        audio_bytes : raw WAV audio bytes
    """

    # Create the TTS client
    client = texttospeech.TextToSpeechClient()

    # Set the Hindi text input
    synthesis_input = texttospeech.SynthesisInput(text=text)

    # Hindi female Neural2 voice — most natural sounding
    voice = texttospeech.VoiceSelectionParams(
        language_code="hi-IN",
        name="hi-IN-Neural2-A",
        ssml_gender=texttospeech.SsmlVoiceGender.FEMALE
    )

    # WAV output at 48kHz — standard for video post-production
    audio_config = texttospeech.AudioConfig(
        audio_encoding=texttospeech.AudioEncoding.LINEAR16,
        sample_rate_hertz=48000
    )

    # Call the API
    response = client.synthesize_speech(
        input=synthesis_input,
        voice=voice,
        audio_config=audio_config
    )

    return response.audio_content