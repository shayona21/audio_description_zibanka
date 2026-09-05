# app.py
# Flask web UI for the Audio Description tool
# Upload a CSV or Excel file → generates a synchronized .wav file

import os
import re
import uuid
import threading
from pathlib import Path
from flask import Flask, render_template, request, jsonify, send_file
from dotenv import load_dotenv

from excel_parser import SUPPORTED_EXTENSIONS, detect_file_type, parse_file
from tts_client import AVAILABLE_VOICES, DEFAULT_VOICE, text_to_speech, pcm_to_wav
from audio_builder import build_master_timeline, export_wav
from audio_speed import adjust_audio_speed, check_speed_support
import audio_speed

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
    return render_template(
        "index.html",
        voices=AVAILABLE_VOICES,
        default_voice=DEFAULT_VOICE,
        max_speed_rate=audio_speed.MAX_SPEED_RATE,
    )


@app.route("/upload", methods=["POST"])
def upload():
    """
    Receive an uploaded CSV or Excel file, process it in a background thread,
    and return a job_id the browser can use to poll progress.
    """

    if "file" not in request.files:
        return jsonify({"error": "No file uploaded"}), 400

    file = request.files["file"]
    requested_name = request.form.get("output_name", "").strip()
    selected_voice = request.form.get("voice", DEFAULT_VOICE).strip()
    try:
        speed = check_speed_support(request.form.get("speed", "1.00"))
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    except RuntimeError as exc:
        return jsonify({"error": str(exc)}), 503

    upload_extension = Path(file.filename or "").suffix.lower()
    if upload_extension not in SUPPORTED_EXTENSIONS:
        return jsonify({
            "error": "Please upload a CSV or Excel file (.csv, .xlsx, .xls, or .xlsm)"
        }), 400

    # Keep the user's name readable while removing characters that are unsafe
    # or invalid in filenames across common operating systems.
    if requested_name.lower().endswith(".wav"):
        requested_name = requested_name[:-4].strip()
    output_stem = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "-", requested_name).strip(" .")
    output_stem = output_stem[:120].rstrip(" .")

    if not output_stem:
        return jsonify({"error": "Please enter an output file name"}), 400

    if selected_voice not in AVAILABLE_VOICES:
        return jsonify({"error": "Please select a valid Gemini voice"}), 400

    download_name = f"{output_stem}.wav"

    # Save the uploaded file with a unique name
    job_id   = uuid.uuid4().hex[:8]
    upload_path = UPLOAD_DIR / f"{job_id}{upload_extension}"
    file.save(upload_path)

    # Initialize job state
    jobs[job_id] = {
        "status":   "running",
        "progress": [],
        "total":    0,
        "current":  0,
        "output":   None,
        "download_name": download_name,
        "voice": selected_voice,
        "speed": speed,
        "error":    None
    }

    # Start processing in a background thread so the browser doesn't time out
    thread = threading.Thread(
        target=process_job,
        args=(job_id, str(upload_path), selected_voice, speed),
        daemon=True
    )
    thread.start()

    return jsonify({"job_id": job_id, "download_name": download_name})


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
        download_name=jobs[job_id]["download_name"]
    )


# ── Background processing ─────────────────────────────────────────────────────

def process_job(job_id, upload_path, voice=DEFAULT_VOICE, speed=1.0):
    """
    Full pipeline: parse CSV/Excel → TTS → build timeline → export .wav
    Runs in a background thread. Updates jobs[job_id] as it goes.
    """

    def report(message):
        """Send a processing message to both the browser job log and terminal."""
        log(job_id, message)

    try:
        speed = check_speed_support(speed)
        log(job_id, f"Speed dial: {speed:.2f}x (pitch preserved)")
        # Step 1: Detect and parse the uploaded tabular file.
        file_type = detect_file_type(upload_path)
        log(job_id, f"Parsing {file_type.upper()} file...")
        rows = parse_file(upload_path, progress_callback=report)
        jobs[job_id]["total"] = len(rows)
        log(job_id, f"Found {len(rows)} AD rows.")

        # Step 2: Convert each row to speech
        log(job_id, f"Converting rows to speech via Gemini TTS (voice: {voice})...")
        audio_clips = []

        for i, row in enumerate(rows):
            jobs[job_id]["current"] = i + 1
            log(job_id, f"Row {i + 1}/{len(rows)}: {row['text'][:50]}...")

            # Get raw PCM bytes from Gemini TTS
            pcm_bytes = text_to_speech(row["text"], voice=voice)

            # Convert PCM → WAV so pydub can read it
            wav_bytes = pcm_to_wav(pcm_bytes)
            wav_bytes = adjust_audio_speed(wav_bytes, speed)
            audio_clips.append(wav_bytes)

        # Step 3: Build the master timeline
        log(job_id, "Building master audio timeline...")
        episode_duration_ms = rows[-1]["start_ms"] + 120000
        master = build_master_timeline(
            rows,
            audio_clips,
            episode_duration_ms,
            progress_callback=report
        )

        # Step 4: Export
        output_path = str(OUTPUT_DIR / f"{job_id}_output.wav")
        log(job_id, "Exporting .wav file...")
        export_wav(master, output_path, progress_callback=report)

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
