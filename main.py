# main.py
# Ties everything together:
# 1. Parse Excel
# 2. Convert each row to speech via Google TTS
# 3. Place each clip on master timeline
# 4. Export as .wav

import os
import time
from dotenv import load_dotenv
from excel_parser import parse_file
from tts_client import text_to_speech, pcm_to_wav
from audio_builder import build_master_timeline, export_wav
from audio_speed import adjust_audio_speed, check_speed_support

load_dotenv()

def run(excel_path, output_path="output_AD.wav", speed=1.0):
    """
    Full pipeline: Excel/CSV → .wav
    """

    speed = check_speed_support(speed)

    # Step 1: Parse the Excel file
    print("Step 1: Parsing CSV or Excel AD...")
    rows = parse_file(excel_path)

    # Step 2: Get episode duration from last row's end time
    episode_duration_ms = rows[-1]["end_ms"] + 5000  # add 5 sec buffer at end
    print(f"Episode duration: {episode_duration_ms / 1000 / 60:.1f} minutes")

    # Step 3: Convert each row to speech
    print(f"\nStep 2: Converting {len(rows)} rows to speech...")
    audio_clips = []

    for _, row in enumerate(rows[:5]):
        print(f"  Row {row['row_number']}/{len(rows)}: {row['text'][:40]}...")

        # Get raw PCM bytes from Gemini TTS
        pcm_bytes = text_to_speech(row["text"])

        # Convert PCM → WAV so pydub can read it
        wav_bytes = pcm_to_wav(pcm_bytes)
        wav_bytes = adjust_audio_speed(wav_bytes, speed)
        audio_clips.append(wav_bytes)

        time.sleep(0.1)

    # Step 4: Build master timeline
    print(f"\nStep 3: Building master timeline...")
    master = build_master_timeline(rows, audio_clips, episode_duration_ms)

    # Step 5: Export
    print(f"\nStep 4: Exporting...")
    export_wav(master, output_path)

    print(f"\n Complete! Output saved to {output_path}")


if __name__ == "__main__":
    run("raktanchal_s3_ep7_ad.csv")
