# voice_tester.py
# Generates one Hindi sample audio file for each Gemini voice
# So you can listen to all 30 and rate them

import os
import time
from tts_client import text_to_speech, pcm_to_wav
from dotenv import load_dotenv

load_dotenv()

# Same sentence for all voices — pick something warm and natural
TEST_TEXT = "सरस्वती धीरे से कमरे में प्रवेश करती है और खिड़की के पास खड़ी हो जाती है।"

VOICES = [
    "Achernar", "Achird", "Algenib", "Algieba", "Alnilam",
    "Aoede", "Autonoe", "Callirrhoe", "Charon", "Despina",
    "Enceladus", "Erinome", "Fenrir", "Gacrux", "Iapetus",
    "Kore", "Laomedeia", "Leda", "Orus", "Puck",
    "Pulcherrima", "Rasalgethi", "Sadachbia", "Sadaltager", "Schedar",
    "Sulafat", "Umbriel", "Vindemiatrix", "Zephyr", "Zubenelgenubi"
]

# Create output folder
os.makedirs("voice_samples", exist_ok=True)

for i, voice in enumerate(VOICES):
    print(f"Generating {i+1}/30: {voice}...")
    try:
        pcm = text_to_speech(TEST_TEXT, voice=voice)
        wav = pcm_to_wav(pcm)
        filename = f"voice_samples/{i+1:02d}_{voice}.wav"
        with open(filename, "wb") as f:
            f.write(wav)
        print(f"  Saved: {filename}")
        time.sleep(0.5)  # avoid rate limiting
    except Exception as e:
        print(f"  Error: {e}")

print("\nDone! Listen to all files in voice_samples/ folder")