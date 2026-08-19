# app.py
# Flask web UI for the Audio Description tool
# Upload a CSV file → generates a synchronized .wav file

import os
import uuid
import threading
from pathlib import Path
from flask import Flask, render_template, request, jsonify, send_file
from dotenv import load_dotenv

from excel_parser import parse_csv
from tts_client import text_to_speech, pcm_to_wav
from audio_builder import build_master_timeline, export_wav

load_dotenv()

app = Flask(__name__)

# ── Folders ──────────────────────────────────────────────────────────────────
BASE_DIR   = Path(__file__).resolve().parent
UPLOAD_DIR = BASE_DIR / "uploads"
OUTPUT_DIR = BASE_DIR / "output"
UPLOAD_DIR.mkdir(exist_ok=True)
OUTPUT_DIR.mkdir(exist_ok=True)

# ── Job state tracker ─────────────────────────────────────────────────────────
# Stores progress for each running job so the browser can poll it
jobs = {}


# ── Routes ───────────────────────────────────────────────────────────────────

@app.route("/")
def index():
    return render_template("index.html")


@app.route("/upload", methods=["POST"])
def upload():
    """
    Receive the uploaded CSV file, start processing in a background thread,
    and return a job_id the browser can use to poll progress.
    """

    if "file" not in request.files:
        return jsonify({"error": "No file uploaded"}), 400

    file = request.files["file"]

    if not file.filename.lower().endswith(".csv"):
        return jsonify({"error": "Please upload a .csv file"}), 400

    # Save the uploaded file with a unique name
    job_id   = uuid.uuid4().hex[:8]
    csv_path = UPLOAD_DIR / f"{job_id}.csv"
    file.save(csv_path)

    # Initialize job state
    jobs[job_id] = {
        "status":   "running",
        "progress": [],
        "total":    0,
        "current":  0,
        "output":   None,
        "error":    None
    }

    # Start processing in a background thread so the browser doesn't time out
    thread = threading.Thread(
        target=process_job,
        args=(job_id, str(csv_path)),
        daemon=True
    )
    thread.start()

    return jsonify({"job_id": job_id})


@app.route("/status/<job_id>")
def status(job_id):
    """
    Return the current status of a job.
    The browser polls this every 2 seconds to update the progress log.
    """

    if job_id not in jobs:
        return jsonify({"error": "Job not found"}), 404

    return jsonify(jobs[job_id])


@app.route("/download/<job_id>")
def download(job_id):
    """
    Download the finished .wav file.
    """

    if job_id not in jobs:
        return jsonify({"error": "Job not found"}), 404

    output_path = jobs[job_id].get("output")

    if not output_path or not os.path.exists(output_path):
        return jsonify({"error": "Output file not ready"}), 404

    return send_file(
        output_path,
        as_attachment=True,
        download_name="audio_description.wav"
    )


# ── Background processing ─────────────────────────────────────────────────────

def process_job(job_id, csv_path):
    """
    Full pipeline: parse CSV → TTS → build timeline → export .wav
    Runs in a background thread. Updates jobs[job_id] as it goes.
    """

    try:
        # Step 1: Parse the CSV
        log(job_id, "Parsing CSV file...")
        rows = parse_csv(csv_path)
        jobs[job_id]["total"] = len(rows)
        log(job_id, f"Found {len(rows)} AD rows.")

        # Step 2: Convert each row to speech
        log(job_id, "Converting rows to speech via Google TTS...")
        audio_clips = []

        for i, row in enumerate(rows):
            jobs[job_id]["current"] = i + 1
            log(job_id, f"Row {i + 1}/{len(rows)}: {row['text'][:50]}...")

            # Get raw PCM bytes from Gemini TTS
            pcm_bytes = text_to_speech(row["text"])

            # Convert PCM → WAV so pydub can read it
            wav_bytes = pcm_to_wav(pcm_bytes)
            audio_clips.append(wav_bytes)

        # Step 3: Build the master timeline
        log(job_id, "Building master audio timeline...")
        episode_duration_ms = rows[-1]["start_ms"] + 120000
        master = build_master_timeline(rows, audio_clips, episode_duration_ms)

        # Step 4: Export
        output_path = str(OUTPUT_DIR / f"{job_id}_output.wav")
        log(job_id, "Exporting .wav file...")
        export_wav(master, output_path)

        # Mark job as done
        jobs[job_id]["status"] = "done"
        jobs[job_id]["output"] = output_path
        log(job_id, "Complete! Your file is ready to download.")

    except Exception as e:
        jobs[job_id]["status"] = "error"
        jobs[job_id]["error"]  = str(e)
        log(job_id, f"Error: {e}")


def log(job_id, message):
    """
    Append a message to the job's progress log.
    """
    jobs[job_id]["progress"].append(message)
    print(f"[{job_id}] {message}")


# ── Run ───────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5070, debug=False, threaded=True)
