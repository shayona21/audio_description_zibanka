"""Dubbing Quality Check for one episode. Run from the repository root:

    python -m quality_check.app

Flow (one reviewer, one episode per session):
  POST /check-script              validate the Excel instantly; nothing is stored
  POST /sessions                  script + settings -> session; dubbing starts in the background
  POST /session/<id>/video        the video uploads while the episode is being dubbed
  GET  /session/<id>              record plus live dubbing progress (polled by the page)
  POST /session/<id>/retry        after an ElevenLabs error, resume from the failed line
  POST /session/<id>/lines/<row>  edit a line's text and re-dub it; only changed tracks are rebuilt
  POST /session/<id>/lines/<row>/speed   speed one line up (1.00x to 1.20x) from its dubbed take
  GET  /session/<id>/export       download the reviewed episode (AD version 2 layout + edited Excel)
  GET  /session/<id>/video | tracks/<n>.wav | peaks.json    media for the review screen
  DELETE /session/<id>            stop dubbing and remove the session
"""

from contextlib import contextmanager
from dataclasses import replace
import json
import os
from pathlib import Path
import re
import shutil
import tempfile
import threading
import time
import uuid

from dotenv import dotenv_values
from flask import Flask, abort, jsonify, render_template, request, send_file

from v2.audio_speed import MAX_SPEED_RATE, check_speed_support
from v2.excel_parser import SUPPORTED_FPS, parse_excel
from v2.tts_client import ElevenLabsClient

from .dubbing import DubCancelled, DubError, dub_episode, regenerate_line, set_line_speed
from .episode import summarize_episode
from .exporting import build_export

MB = 1024 * 1024
MAX_SCRIPT_BYTES = 20 * MB
MAX_VIDEO_BYTES = 500 * MB
VIDEO_TYPES = {".mp4": "video/mp4", ".m4v": "video/mp4",
               ".webm": "video/webm", ".mov": "video/quicktime"}
SESSION_MAX_AGE_SECONDS = 24 * 60 * 60
SESSION_ID = re.compile(r"[0-9a-f]{32}")
MAX_LINE_CHARS = 1500
HERE = Path(__file__).resolve().parent


class UploadError(ValueError):
    def __init__(self, message, status=400):
        super().__init__(message)
        self.status = status


def human_size(size):
    return f"{size / MB:.1f} MB" if size >= MB else f"{size / 1024:.1f} KB"


def create_app(config=None):
    # One ElevenLabs key for both tools: our own .env first, then AD version 2's. Empty values
    # (an untouched .env.example) must not hide the other file's key, so merge by hand.
    for env_file in (HERE / ".env", HERE.parent / "v2" / ".env"):
        for key, value in dotenv_values(env_file).items():
            if value and not os.environ.get(key):
                os.environ[key] = value
    app = Flask(__name__)
    app.config.update(MAX_CONTENT_LENGTH=MAX_VIDEO_BYTES + 5 * MB, RUNTIME_DIR=HERE / "runtime",
                      SYNTHESIZE=None)
    if config:
        app.config.update(config)
    runtime = Path(app.config["RUNTIME_DIR"])
    jobs = {}                 # session id -> live dubbing state (in memory, polled by the page)
    lock = threading.Lock()   # guards session.json read-modify-write and `jobs`
    line_locks = {}           # session id -> Lock: one regeneration at a time per session
    app.extensions["qc_jobs"] = jobs

    # ------------------------------------------------------------------ session storage
    def session_dir(session_id):
        if not SESSION_ID.fullmatch(session_id or ""):
            abort(404)
        path = runtime / session_id
        if not (path / "session.json").is_file():
            abort(404)
        return path

    def load_record(session_id):
        return json.loads((session_dir(session_id) / "session.json").read_text(encoding="utf-8"))

    def save_record(record):
        path = runtime / record["session_id"] / "session.json"
        temp = path.with_suffix(".tmp")
        temp.write_text(json.dumps(record, indent=1), encoding="utf-8")
        temp.replace(path)  # atomic: readers never see half a file

    def update_record(session_id, **changes):
        with lock:
            record = load_record(session_id)
            record.update(changes)
            save_record(record)
            return record

    def public(record):
        """The record plus live job progress and the URLs the page needs."""
        sid = record["session_id"]
        out = dict(record)
        job = jobs.get(sid)
        if record["status"] == "dubbing" and job is None:
            # The server restarted mid-run; finished clips are on disk, so a retry resumes.
            out["status"] = "error"
            out["error"] = "Dubbing stopped because the server restarted. Retry to continue from where it stopped."
        if job:
            out["progress"] = {k: job[k] for k in ("phase", "done", "total", "current")}
        out["video_url"] = f"/session/{sid}/video" if record.get("video") else None
        out["ready"] = record["status"] == "dubbed" and bool(record.get("video"))
        if record.get("audio"):
            edits = record.get("edits") or {}
            speeds = record.get("line_speeds") or {}
            out["peaks_url"] = f"/session/{sid}/peaks.json"
            out["audio"] = dict(
                record["audio"],
                tracks=[dict(t, url=f"/session/{sid}/tracks/{t['index']}.wav") for t in record["audio"]["tracks"]],
                lines=[dict(line, edited=str(line["row"]) in edits,
                            original=edits.get(str(line["row"]), {}).get("original"),
                            line_speed=speeds.get(str(line["row"]), 1.0))
                       for line in record["audio"]["lines"]])
        return out

    def remove_old_sessions():
        if not runtime.is_dir():
            return
        cutoff = time.time() - SESSION_MAX_AGE_SECONDS
        for path in runtime.iterdir():
            if path.is_dir() and SESSION_ID.fullmatch(path.name) and path.stat().st_mtime < cutoff \
                    and path.name not in jobs:
                shutil.rmtree(path, ignore_errors=True)

    # ------------------------------------------------------------------ request helpers
    def read_settings():
        try:
            fps = int(request.form.get("fps", "25"))
        except ValueError:
            fps = 0
        if fps not in SUPPORTED_FPS:
            raise UploadError(f"Frame rate must be one of {', '.join(map(str, SUPPORTED_FPS))}.")
        return fps, request.form.get("sheet", "").strip() or None

    def require_script():
        upload = request.files.get("script")
        if upload is None or Path(upload.filename or "").suffix.lower() != ".xlsx":
            raise UploadError("Choose the episode's Excel script (.xlsx).")
        return upload

    def save_limited(upload, path, limit, label):
        upload.save(path)
        size = path.stat().st_size
        if size > limit:
            raise UploadError(f"The {label} is larger than {limit // MB} MB.", 413)
        if size == 0:
            raise UploadError(f"The {label} file is empty.")
        return size

    def parse_script(path, fps, sheet_name):
        try:
            rows = parse_excel(path, fps=fps, sheet_name=sheet_name)
            return rows, summarize_episode(rows, fps)
        except ValueError as exc:  # WorkbookValidationError and EpisodeError
            raise UploadError(str(exc)) from None
        except Exception:
            raise UploadError("This file couldn't be read as an Excel workbook. Open it in Excel, "
                              "save it as .xlsx and try again.") from None

    def synthesizer():
        """The ElevenLabs client from V2 (or a test double). Fails early if the key is missing."""
        if app.config["SYNTHESIZE"] is not None:
            return app.config["SYNTHESIZE"]
        try:
            return ElevenLabsClient()
        except ValueError:
            raise UploadError("The ElevenLabs API key is not set. Add ELEVENLABS_API_KEY to "
                              "quality_check/.env (or v2/.env) and restart the app.", 503) from None

    @app.errorhandler(UploadError)
    def upload_error(exc):
        return jsonify(error=str(exc)), exc.status

    @app.errorhandler(413)
    def too_large(_error):
        return jsonify(error=f"Upload is too large. Limits: script {MAX_SCRIPT_BYTES // MB} MB, "
                             f"video {MAX_VIDEO_BYTES // MB} MB."), 413

    @contextmanager
    def line_change(session_id):
        """One line change at a time per session, and only once the episode is dubbed."""
        with lock:
            session_lock = line_locks.setdefault(session_id, threading.Lock())
        if not session_lock.acquire(blocking=False):
            raise UploadError("Another line is being updated. Wait for it to finish.", 409)
        try:
            record = load_record(session_id)
            if record["status"] != "dubbed":
                raise UploadError("The episode must finish dubbing before lines can be changed.", 409)
            yield record
        finally:
            session_lock.release()

    def episode_rows(record):
        """Script rows with the reviewer's text edits applied (the Excel itself is never changed)."""
        rows, summary = parse_script(runtime / record["session_id"] / "script.xlsx", record["fps"], record["sheet"])
        edits = record.get("edits") or {}
        rows = [replace(row, text=edits[str(row.row_number)]["text"]) if str(row.row_number) in edits else row
                for row in rows]
        return rows, summary

    # ------------------------------------------------------------------ dubbing job
    def start_dubbing(session_id):
        record = load_record(session_id)
        folder = runtime / session_id
        rows, summary = episode_rows(record)
        synthesize = synthesizer()
        job = {"phase": "dubbing", "done": 0, "total": len(rows), "current": "", "cancel": False}
        jobs[session_id] = job

        def run():
            try:
                audio = dub_episode(folder, rows, summary, record["fps"], record["speed"], synthesize,
                                    report=lambda **state: job.update(state),
                                    cancelled=lambda: job["cancel"])
                update_record(session_id, status="dubbed", audio=audio, error=None, failed_row=None)
                job.update(phase="done", done=len(rows))
            except DubCancelled:
                pass
            except DubError as exc:
                if folder.exists():
                    update_record(session_id, status="error", error=str(exc), failed_row=exc.row)
                job.update(phase="error")
            except Exception as exc:  # disk full, corrupt audio...: report, never crash silently
                if folder.exists():
                    update_record(session_id, status="error", error=f"Dubbing failed: {exc}", failed_row=None)
                job.update(phase="error")

        job["thread"] = threading.Thread(target=run, name=f"dub-{session_id[:8]}", daemon=True)
        job["thread"].start()

    # ------------------------------------------------------------------ pages and endpoints
    @app.get("/")
    def index():
        return render_template("index.html", frame_rates=SUPPORTED_FPS, max_speed=MAX_SPEED_RATE,
                               has_ffmpeg=shutil.which("ffmpeg") is not None,
                               max_video_mb=MAX_VIDEO_BYTES // MB, max_script_mb=MAX_SCRIPT_BYTES // MB)

    @app.post("/check-script")
    def check_script():
        script = require_script()
        fps, sheet_name = read_settings()
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "script.xlsx"
            save_limited(script, path, MAX_SCRIPT_BYTES, "script")
            _rows, summary = parse_script(path, fps, sheet_name)
        return jsonify(summary=summary)

    @app.post("/sessions")
    def create_session():
        script = require_script()
        fps, sheet_name = read_settings()
        try:
            speed = check_speed_support(request.form.get("speed", "1.00"))
        except ValueError as exc:
            raise UploadError(str(exc)) from None
        except RuntimeError as exc:  # FFmpeg missing for speeds above 1.00x
            raise UploadError(str(exc), 503) from None
        synthesizer()  # refuse before storing anything if the key is missing

        remove_old_sessions()
        session_id = uuid.uuid4().hex
        folder = runtime / session_id
        folder.mkdir(parents=True)
        try:
            size = save_limited(script, folder / "script.xlsx", MAX_SCRIPT_BYTES, "script")
            _rows, summary = parse_script(folder / "script.xlsx", fps, sheet_name)
            record = {"session_id": session_id, "status": "dubbing", "created": time.time(),
                      "fps": fps, "sheet": sheet_name, "speed": speed,
                      "script": {"name": script.filename, "size": human_size(size)},
                      "summary": summary, "audio": None, "video": None, "error": None, "failed_row": None,
                      "edits": {}, "line_speeds": {}}
            save_record(record)
            start_dubbing(session_id)
        except BaseException:
            shutil.rmtree(folder, ignore_errors=True)
            raise
        return jsonify(public(record)), 201

    @app.get("/session/<session_id>")
    def session_info(session_id):
        with lock:
            record = load_record(session_id)
        return jsonify(public(record))

    @app.post("/session/<session_id>/retry")
    def retry(session_id):
        session_dir(session_id)
        job = jobs.get(session_id)
        if job and job["thread"].is_alive():
            raise UploadError("This episode is still being dubbed.", 409)
        record = update_record(session_id, status="dubbing", error=None, failed_row=None)
        start_dubbing(session_id)
        return jsonify(public(record))

    @app.post("/session/<session_id>/lines/<int:row_number>")
    def regenerate(session_id, row_number):
        """Re-dub one line with new (or the same) text; the review keeps going with the result."""
        session_dir(session_id)
        body = request.get_json(silent=True) or {}
        text = " ".join(str(body.get("text", "")).split())  # one line of dialogue, no stray newlines
        if not text:
            raise UploadError("The line can't be empty.")
        if len(text) > MAX_LINE_CHARS:
            raise UploadError(f"The line is longer than {MAX_LINE_CHARS} characters.")
        with line_change(session_id) as record:
            rows, summary = episode_rows(record)
            original = next((row for row in rows if row.row_number == row_number), None)
            if original is None:
                abort(404)
            edits = dict(record.get("edits") or {})
            first_text = edits.get(str(row_number), {}).get("original", original.text)
            rows = [replace(row, text=text) if row.row_number == row_number else row for row in rows]
            try:
                audio, changed, moved = regenerate_line(runtime / session_id, rows, summary, record["fps"],
                                                        record["speed"], synthesizer(), row_number,
                                                        record["audio"])
            except DubError as exc:
                raise UploadError(str(exc), 502) from None
            if text == first_text:
                edits.pop(str(row_number), None)  # back to the script's own wording
            else:
                edits[str(row_number)] = {"text": text, "original": first_text}
            speeds = dict(record.get("line_speeds") or {})
            speeds.pop(str(row_number), None)  # a new take starts at 1.00x
            record = update_record(session_id, audio=audio, edits=edits, line_speeds=speeds)
        return jsonify(dict(public(record), changed_tracks=changed, moved=moved, row=row_number))

    @app.post("/session/<session_id>/lines/<int:row_number>/speed")
    def line_speed(session_id, row_number):
        """Speed one line up from its dubbed take, keeping its start time. No ElevenLabs request."""
        session_dir(session_id)
        body = request.get_json(silent=True) or {}
        try:
            factor = round(float(body.get("speed")), 2)
        except (TypeError, ValueError):
            raise UploadError("Line speed must be a number from 1.00 to 1.20.") from None
        if not 1.0 <= factor <= MAX_SPEED_RATE:
            raise UploadError(f"Line speed must be from 1.00x to {MAX_SPEED_RATE:.2f}x.")
        with line_change(session_id) as record:
            rows, summary = episode_rows(record)
            if not any(row.row_number == row_number for row in rows):
                abort(404)
            try:
                audio, changed, moved = set_line_speed(runtime / session_id, rows, summary, record["fps"],
                                                       record["speed"], row_number, factor, record["audio"])
            except RuntimeError as exc:  # FFmpeg missing or failed
                raise UploadError(str(exc), 503) from None
            speeds = dict(record.get("line_speeds") or {})
            if factor == 1.0:
                speeds.pop(str(row_number), None)
            else:
                speeds[str(row_number)] = factor
            record = update_record(session_id, audio=audio, line_speeds=speeds)
        return jsonify(dict(public(record), changed_tracks=changed, moved=moved, row=row_number))

    @app.get("/session/<session_id>/export")
    def export(session_id):
        """Package the episode as reviewed. Waits for no one: refuses while a line is being changed."""
        folder = session_dir(session_id)
        with line_change(session_id) as record:
            rows, _summary = episode_rows(record)
            info = build_export(folder, record, rows, folder / "export.zip")
        return send_file(folder / "export.zip", mimetype="application/zip", as_attachment=True,
                         download_name=info["name"], max_age=0)

    @app.post("/session/<session_id>/video")
    def attach_video(session_id):
        folder = session_dir(session_id)
        video = request.files.get("video")
        if video is None or not video.filename:
            raise UploadError("Choose the episode's video.")
        ext = Path(video.filename).suffix.lower()
        if ext not in VIDEO_TYPES:
            raise UploadError("The video must be .mp4, .m4v, .webm or .mov (MP4 with H.264 is recommended).")
        temp = folder / f"video-upload{ext}"
        try:
            size = save_limited(video, temp, MAX_VIDEO_BYTES, "video")
        except UploadError:
            temp.unlink(missing_ok=True)
            raise
        for old in folder.glob("video.*"):
            old.unlink()
        temp.replace(folder / f"video{ext}")
        record = update_record(session_id, video={"name": video.filename, "size": human_size(size),
                                                  "file": f"video{ext}", "mimetype": VIDEO_TYPES[ext]})
        return jsonify(public(record)), 201

    @app.delete("/session/<session_id>")
    def delete_session(session_id):
        folder = session_dir(session_id)
        job = jobs.pop(session_id, None)
        if job:
            job["cancel"] = True  # the worker stops before its next ElevenLabs request
        shutil.rmtree(folder, ignore_errors=True)
        return "", 204

    @app.get("/session/<session_id>/video")
    def session_video(session_id):
        folder = session_dir(session_id)
        video = load_record(session_id).get("video")
        if not video:
            abort(404)
        # conditional=True answers HTTP Range requests, so seeking never re-downloads the file.
        return send_file(folder / video["file"], mimetype=video["mimetype"], conditional=True, max_age=0)

    @app.get("/session/<session_id>/tracks/<int:index>.wav")
    def session_track(session_id, index):
        path = session_dir(session_id) / "tracks" / f"{index}.wav"
        if not path.is_file():
            abort(404)
        response = send_file(path, mimetype="audio/wav", conditional=True, max_age=0)
        response.headers["Cache-Control"] = "no-cache"  # tracks are rebuilt after line edits
        return response

    @app.get("/session/<session_id>/peaks.json")
    def session_peaks(session_id):
        path = session_dir(session_id) / "peaks.json"
        if not path.is_file():
            abort(404)
        response = send_file(path, mimetype="application/json", max_age=0)
        response.headers["Cache-Control"] = "no-cache"
        return response

    return app


if __name__ == "__main__":
    app = create_app()
    app.run(host="127.0.0.1", port=int(os.environ.get("QUALITY_CHECK_PORT", "5072")),
            debug=False, threaded=True)
