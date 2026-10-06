# app.py
# Flask web UI for the Audio Description tool
# Upload CSV/Excel → split WAV segments in a ZIP or a full WAV track

import os
import re
import uuid
import threading
import io
import zipfile
from pathlib import Path
from flask import Flask, render_template, request, jsonify, send_file
from dotenv import load_dotenv

from excel_parser import SUPPORTED_EXTENSIONS, detect_file_type, parse_file
from tts_client import (
    DEFAULT_LANGUAGE, DEFAULT_PROVIDER, DEFAULT_VOICE, DEFAULT_GOOGLE_VOICE,
    GOOGLE_VOICES, LANGUAGES_BY_PROVIDER, PROVIDERS, text_to_speech, pcm_to_wav,
    validate_language, validate_provider,
    validate_provider_api_key, validate_voice,
)
from audio_builder import (
    audio_duration_ms, build_master_timeline, build_segment_timeline, export_wav_bytes,
)
from audio_speed import adjust_audio_speed, check_speed_support
from audio_overlap import repair_overlap
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


def validate_output_mode(value):
    if value not in ("split", "full"):
        raise ValueError("Choose split WAV segments or a full WAV track")
    return value


# ── Routes ───────────────────────────────────────────────────────────────────

@app.route("/")
def index():
    return render_template(
        "index.html",
        providers=PROVIDERS,
        default_provider=DEFAULT_PROVIDER,
        default_voice=DEFAULT_VOICE,
        default_google_voice=DEFAULT_GOOGLE_VOICE,
        google_voices=GOOGLE_VOICES,
        languages=LANGUAGES_BY_PROVIDER[DEFAULT_PROVIDER],
        languages_by_provider=LANGUAGES_BY_PROVIDER,
        default_language=DEFAULT_LANGUAGE,
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
    try:
        output_mode = validate_output_mode(request.form.get("output_mode", "split"))
        selected_provider = validate_provider(
            request.form.get("provider", DEFAULT_PROVIDER).strip()
        )
        selected_voice = validate_voice(
            request.form.get("voice", "").strip(), selected_provider
        )
        selected_language = validate_language(
            request.form.get("language", DEFAULT_LANGUAGE).strip(),
            selected_provider,
        )
        validate_provider_api_key(selected_provider)
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
    if requested_name.lower().endswith((".wav", ".zip")):
        requested_name = requested_name.rsplit(".", 1)[0].strip()
    output_stem = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "-", requested_name).strip(" .")
    output_stem = output_stem[:120].rstrip(" .")

    if not output_stem:
        return jsonify({"error": "Please enter an output file name"}), 400

    extension = "zip" if output_mode == "split" else "wav"
    download_name = f"{output_stem}.{extension}"

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
        "output_mode": output_mode,
        "provider": selected_provider,
        "voice": selected_voice,
        "language": selected_language,
        "speed": speed,
        "error":    None
    }

    # Start processing in a background thread so the browser doesn't time out
    thread = threading.Thread(
        target=process_job,
        args=(job_id, str(upload_path), selected_voice, speed),
        kwargs={
            "language": selected_language, "provider": selected_provider,
            "output_mode": output_mode,
        },
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
    Download the finished WAV track or ZIP of WAV segments.
    """

    if job_id not in jobs:
        return jsonify({"error": "Job not found"}), 404

    output_path = jobs[job_id].get("output")

    if not output_path or not os.path.exists(output_path):
        return jsonify({"error": "Output file not ready"}), 404

    return send_file(
        output_path,
        as_attachment=True,
        download_name=jobs[job_id]["download_name"],
        mimetype=("audio/wav" if jobs[job_id].get("output_mode") == "full"
                  else "application/zip"),
    )


# ── Background processing ─────────────────────────────────────────────────────

def process_job(job_id, upload_path, voice=None, speed=1.0,
                language=DEFAULT_LANGUAGE, provider=DEFAULT_PROVIDER,
                output_mode="split"):
    """
    Full pipeline: parse CSV/Excel → TTS → build timeline → export .wav
    Runs in a background thread. Updates jobs[job_id] as it goes.
    """

    def report(message):
        """Send a processing message to both the browser job log and terminal."""
        log(job_id, message)

    try:
        output_mode = validate_output_mode(output_mode)
        jobs[job_id]["output_mode"] = output_mode
        jobs[job_id]["repaired_rows"] = []
        jobs[job_id]["flagged_rows"] = []
        log(job_id, "Output: " + (
            "split WAV segments (ZIP)" if output_mode == "split"
            else "full WAV track (overlaps preserved)"
        ))
        provider = validate_provider(provider)
        voice = validate_voice(voice, provider)
        language = validate_language(language, provider)
        validate_provider_api_key(provider)
        log(job_id, f"Narration language: {language}")
        log(job_id, f"TTS provider: {PROVIDERS[provider]}")
        speed = check_speed_support(speed)
        log(job_id, f"Speed dial: {speed:.2f}x (pitch preserved)")
        # Step 1: Detect and parse the uploaded tabular file.
        file_type = detect_file_type(upload_path)
        log(job_id, f"Parsing {file_type.upper()} file...")
        rows = parse_file(upload_path, progress_callback=report)
        jobs[job_id]["total"] = len(rows)
        log(job_id, f"Found {len(rows)} AD rows.")

        # Step 2: Convert each row to speech
        log(job_id, f"Converting rows to speech via {PROVIDERS[provider]} (voice: {voice})...")
        segments = []
        segment_rows = []
        segment_clips = []
        latest_clip_end_ms = 0

        def close_segment():
            nonlocal segment_rows, segment_clips
            if segment_rows:
                segments.append((segment_rows, segment_clips))
                segment_rows = []
                segment_clips = []

        for i, row in enumerate(rows):
            jobs[job_id]["current"] = i + 1
            log(job_id, f"Row {i + 1}/{len(rows)}: {row['text'][:50]}...")

            pcm_bytes = text_to_speech(
                row["text"], voice=voice, language=language, provider=provider
            )

            # Measure the actual output after applying the selected speed.
            wav_bytes = pcm_to_wav(pcm_bytes)
            wav_bytes = adjust_audio_speed(wav_bytes, speed)
            output_length_ms = audio_duration_ms(wav_bytes)

            # The available window ends at the next dialogue's start, not
            # the spreadsheet end time. The final row has no next dialogue.
            predicted_length_ms = (
                rows[i + 1]["start_ms"] - row["start_ms"]
                if i + 1 < len(rows) else None
            )
            repair = repair_overlap(wav_bytes, predicted_length_ms)
            if repair.repaired:
                jobs[job_id]["repaired_rows"].append(row["row_number"])
                log(
                    job_id,
                    f"OVERLAP REPAIRED — Row {row['row_number']}: "
                    f"{output_length_ms / 1000:.3f}s → {repair.duration_ms / 1000:.3f}s "
                    f"at {repair.speed:.4f}x additional speed "
                    f"({speed * repair.speed:.4f}x total; pitch preserved).",
                )
            wav_bytes = repair.audio
            output_length_ms = repair.duration_ms
            latest_clip_end_ms = max(
                latest_clip_end_ms, row["start_ms"] + output_length_ms
            )
            duration_overrun = (
                predicted_length_ms is not None
                and output_length_ms > predicted_length_ms
            )
            if duration_overrun:
                jobs[job_id]["flagged_rows"].append(row["row_number"])
                overrun_ms = output_length_ms - predicted_length_ms
                log(
                    job_id,
                    f"OVERLAP DETECTED — DURATION OVERRUN — "
                    f"Row {row['row_number']} output length is "
                    f"{output_length_ms / 1000:.2f}s; predicted length "
                    f"(until Row {rows[i + 1]['row_number']} starts) is "
                    f"{predicted_length_ms / 1000:.2f}s "
                    f"(over by {overrun_ms / 1000:.3f}s). "
                    f"MANUAL FIX REQUIRED — {repair.reason}",
                )

            segment_rows.append(row)
            segment_clips.append(wav_bytes)

            if duration_overrun and output_mode == "split":
                log(job_id, f"Ending this WAV segment at Row {row['row_number']}.")
                close_segment()

        close_segment()

        if output_mode == "full":
            # Retain the original timeline from zero, including its end buffer,
            # and extend it if necessary so no generated speech is cut off.
            duration_ms = max(rows[-1]["end_ms"] + 5000, latest_clip_end_ms)
            full_rows, full_clips = segments[0]
            master = build_master_timeline(
                full_rows, full_clips, duration_ms, progress_callback=report
            )
            log(job_id, "Exporting full WAV track...")
            output_bytes = export_wav_bytes(master)
            extension = "wav"
            completion_message = "Complete! Your full WAV track is ready."
        else:
            archive_buffer = io.BytesIO()
            with zipfile.ZipFile(
                archive_buffer, "w", compression=zipfile.ZIP_DEFLATED
            ) as archive:
                for segment_rows, segment_clips in segments:
                    first_row = segment_rows[0]["row_number"]
                    last_row = segment_rows[-1]["row_number"]
                    wav_name = f"Row_{first_row}_{last_row}.wav"
                    log(job_id, f"Exporting {wav_name}...")
                    master = build_segment_timeline(
                        segment_rows,
                        segment_clips,
                        progress_callback=report,
                    )
                    archive.writestr(wav_name, export_wav_bytes(master))
            output_bytes = archive_buffer.getvalue()
            extension = "zip"
            completion_message = f"Complete! {len(segments)} WAV file(s) are ready in the ZIP."

        output_path = str(OUTPUT_DIR / f"{job_id}_output.{extension}")
        Path(output_path).write_bytes(output_bytes)

        # Mark job as done
        jobs[job_id]["status"] = "done"
        jobs[job_id]["output"] = output_path
        log(job_id, completion_message)

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
