"""Dubbing Quality Check for one episode. Run from the repository root:

    python -m quality_check.app

Flow (one reviewer, one episode per session). The audio comes from one of two sources:
generated here with ElevenLabs (as AD version 2 does), or taken from an AD version 2 run.
  POST /check-script              validate the Excel instantly; nothing is stored
  GET  /v2-runs                   finished AD version 2 runs on this server
  GET  /v2-runs/<run>             a run's episodes (summaries), to pick one
  POST /v2-runs                   upload an AD version 2 ZIP instead; answers like GET /v2-runs/<run>
  POST /sessions                  script + settings (or run + episode) -> session; the dub is built
                                  in the background
  POST /session/<id>/video        the video uploads while the episode is being dubbed
  GET  /session/<id>              record plus live dubbing progress (polled by the page)
  POST /session/<id>/retry        after an error, resume (generated: from the failed line)
  POST /session/<id>/lines/<row>  edit a line's text and re-dub it; only changed tracks are rebuilt
  POST /session/<id>/lines/<row>/speed   set one line's speed (0.70x to 1.50x) from its take
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

from v2.audio_speed import MAX_FIT_SPEED, MIN_FIT_SPEED, check_ffmpeg, validate_speed
from v2.excel_parser import SUPPORTED_FPS, parse_excel
from v2.exporter import episode_tag
from v2.tts_client import ElevenLabsClient

from .dubbing import (DubCancelled, DubError, assemble, clip_lengths, dub_episode, import_clips, regenerate_line,
                      set_line_speed)
from .episode import summarize_episode
from .exporting import build_export
from .importing import RunError, V2Run

MB = 1024 * 1024
MAX_SCRIPT_BYTES = 20 * MB
MAX_VIDEO_BYTES = 500 * MB
MAX_RUN_ZIP_BYTES = 500 * MB
VIDEO_TYPES = {".mp4": "video/mp4", ".m4v": "video/mp4",
               ".webm": "video/webm", ".mov": "video/quicktime"}
SESSION_MAX_AGE_SECONDS = 24 * 60 * 60
SESSION_ID = re.compile(r"[0-9a-f]{32}")
RUN_ID = re.compile(r"(up-)?[0-9a-f]{32}")  # an AD version 2 job id, or "up-" + an uploaded ZIP's id
V2_OUTPUT = "AD_v2_output.zip"              # V2 writes each run's ZIP under this name in its job folder
MAX_LISTED_RUNS = 30
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
    app.config.update(MAX_CONTENT_LENGTH=max(MAX_VIDEO_BYTES, MAX_RUN_ZIP_BYTES) + 5 * MB,
                      RUNTIME_DIR=HERE / "runtime",
                      # AD version 2's job folders; set AD_V2_RUNTIME_DIR if V2 runs from another checkout.
                      V2_RUNTIME_DIR=Path(os.environ.get("AD_V2_RUNTIME_DIR") or HERE.parent / "v2" / "runtime"),
                      SYNTHESIZE=None)
    if config:
        app.config.update(config)
    runtime = Path(app.config["RUNTIME_DIR"])
    uploads = runtime / "uploads"     # AD version 2 ZIPs uploaded for import, kept 24 hours
    v2_runtime = Path(app.config["V2_RUNTIME_DIR"])
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
            out["peaks_url"] = f"/session/{sid}/peaks.json"
            lines = []
            for line in record["audio"]["lines"]:
                key = str(line["row"])
                take = take_info(record, key)
                lines.append(dict(line, edited=key in edits, original=edits.get(key, {}).get("original"),
                                  fit_speed=take["fit_speed"], needed_speed=take["needed_speed"],
                                  line_speed=line_speed_of(record, key),
                                  speed_set=key in (record.get("line_speeds") or {})))
            out["audio"] = dict(
                record["audio"], lines=lines,
                tracks=[dict(t, url=f"/session/{sid}/tracks/{t['index']}.wav") for t in record["audio"]["tracks"]])
        return out

    def remove_old_sessions():
        if not runtime.is_dir():
            return
        cutoff = time.time() - SESSION_MAX_AGE_SECONDS
        for path in runtime.iterdir():
            if path.is_dir() and SESSION_ID.fullmatch(path.name) and path.stat().st_mtime < cutoff \
                    and path.name not in jobs:
                shutil.rmtree(path, ignore_errors=True)
        if uploads.is_dir():
            for path in uploads.iterdir():
                if path.stat().st_mtime < cutoff:
                    path.unlink(missing_ok=True)

    # ------------------------------------------------------------------ request helpers
    def read_settings(values=None):
        values = request.form if values is None else values
        try:
            fps = int(values.get("fps", "25"))
        except ValueError:
            fps = 0
        if fps not in SUPPORTED_FPS:
            raise UploadError(f"Frame rate must be one of {', '.join(map(str, SUPPORTED_FPS))}.")
        return fps, values.get("sheet", "").strip() or None

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

    def parse_script(path, fps, sheet_name, episode=None):
        """Rows and summary of one episode. ``episode`` picks it from a several-episode workbook
        (an AD version 2 run); without it the workbook must hold exactly one episode."""
        try:
            rows = parse_excel(path, fps=fps, sheet_name=sheet_name)
            if episode is not None:
                rows = [row for row in rows if row.episode == episode]
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

    # ------------------------------------------------------------------ AD version 2 runs
    def run_zip(run_id):
        """``(zip path, details)`` for a finished V2 run on this server or an uploaded ZIP."""
        if not RUN_ID.fullmatch(run_id or ""):
            raise UploadError("Choose an AD Tool V2 run.", 404)
        if run_id.startswith("up-"):
            path = uploads / f"{run_id[3:]}.zip"
            try:
                details = json.loads(path.with_suffix(".json").read_text(encoding="utf-8"))
            except (OSError, ValueError):
                details = None
        else:
            path = v2_runtime / run_id / V2_OUTPUT
            details = read_run_record(v2_runtime / run_id)
            if details is not None and details.get("status") != "done":
                details = None
        if details is None or not path.is_file():
            raise UploadError("This AD Tool V2 run is no longer available. Choose another run or upload its ZIP.", 404)
        return path, details

    def read_run_record(folder):
        try:
            return json.loads((folder / "run.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None

    def open_run(run_id, fps, sheet_name):
        """The run, read at its own frame rate and worksheet (an upload: the settings' as a hint)."""
        path, details = run_zip(run_id)
        if not run_id.startswith("up-"):
            fps, sheet_name = details.get("fps", fps), details.get("sheet")
        try:
            return V2Run(path, fps, sheet_name), details
        except RunError as exc:
            raise UploadError(str(exc)) from None

    def describe_run(run_id, fps, sheet_name):
        run, details = open_run(run_id, fps, sheet_name)
        with run:
            try:
                episodes = [summarize_episode(run.episode_rows(ep), run.fps) for ep in run.episodes()]
            except ValueError as exc:
                raise UploadError(str(exc)) from None
        return {"id": run_id, "name": details.get("download_name") or details.get("name"),
                "fps": run.fps, "sheet": run.sheet, "episodes": episodes}

    # ------------------------------------------------------------------ line speeds
    def take_info(record, key):
        """Speeds of a line's take (sessions from before pass 2: a take at 1.00x that fits)."""
        return (record.get("takes") or {}).get(key) or {"fit_speed": 1.0, "needed_speed": 1.0}

    def line_speed_of(record, key):
        """The speed the line plays at: the reviewer's choice, else pass 2's fit."""
        return (record.get("line_speeds") or {}).get(key, take_info(record, key)["fit_speed"])

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
        rows, summary = parse_script(runtime / record["session_id"] / "script.xlsx", record["fps"], record["sheet"],
                                     record.get("episode"))
        edits = record.get("edits") or {}
        rows = [replace(row, text=edits[str(row.row_number)]["text"]) if str(row.row_number) in edits else row
                for row in rows]
        return rows, summary

    # ------------------------------------------------------------------ dubbing job
    def start_dubbing(session_id):
        """Generated: dub (or resume) every line. Imported: the clips are in place; build the tracks."""
        record = load_record(session_id)
        folder = runtime / session_id
        rows, summary = episode_rows(record)
        imported = record.get("source") == "v2"
        synthesize = None if imported else synthesizer()
        job = {"phase": "building" if imported else "dubbing", "done": len(rows) if imported else 0,
               "total": len(rows), "current": "", "cancel": False}
        jobs[session_id] = job

        def run():
            try:
                if imported:
                    audio, takes = assemble(folder, rows, summary, record["fps"], clip_lengths(folder, rows)), None
                else:
                    audio, takes = dub_episode(folder, rows, summary, record["fps"], synthesize,
                                               report=lambda **state: job.update(state),
                                               cancelled=lambda: job["cancel"])
                    takes = {str(row): info for row, info in takes.items()}
                changes = dict(status="dubbed", audio=audio, error=None, failed_row=None)
                if takes is not None:
                    changes["takes"] = takes
                update_record(session_id, **changes)
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
        return render_template("index.html", frame_rates=SUPPORTED_FPS, min_speed=MIN_FIT_SPEED,
                               max_speed=MAX_FIT_SPEED, has_ffmpeg=shutil.which("ffmpeg") is not None,
                               max_video_mb=MAX_VIDEO_BYTES // MB, max_script_mb=MAX_SCRIPT_BYTES // MB,
                               max_run_mb=MAX_RUN_ZIP_BYTES // MB)

    @app.post("/check-script")
    def check_script():
        script = require_script()
        fps, sheet_name = read_settings()
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "script.xlsx"
            save_limited(script, path, MAX_SCRIPT_BYTES, "script")
            _rows, summary = parse_script(path, fps, sheet_name)
        return jsonify(summary=summary)

    @app.get("/v2-runs")
    def list_runs():
        """Finished AD version 2 runs on this server, newest first."""
        runs = []
        if v2_runtime.is_dir():
            for folder in v2_runtime.iterdir():
                details = read_run_record(folder) if SESSION_ID.fullmatch(folder.name) else None
                if details and details.get("status") == "done" and (folder / V2_OUTPUT).is_file():
                    runs.append({"id": folder.name, "name": details.get("download_name") or V2_OUTPUT,
                                 "finished": details.get("finished") or details.get("created") or 0,
                                 "rows": details.get("rows"), "fps": details.get("fps"),
                                 "episodes": details.get("episodes") or []})
        runs.sort(key=lambda run: run["finished"], reverse=True)
        return jsonify(runs=runs[:MAX_LISTED_RUNS])

    @app.get("/v2-runs/<run_id>")
    def run_details(run_id):
        fps, sheet_name = read_settings(request.args)
        return jsonify(describe_run(run_id, fps, sheet_name))

    @app.post("/v2-runs")
    def upload_run():
        """Upload an AD version 2 ZIP (a run made elsewhere); kept 24 hours, then removed."""
        upload = request.files.get("dub")
        if upload is None or Path(upload.filename or "").suffix.lower() != ".zip":
            raise UploadError("Choose the ZIP that AD Tool V2 delivered (.zip).")
        fps, sheet_name = read_settings()
        remove_old_sessions()
        uploads.mkdir(parents=True, exist_ok=True)
        token = uuid.uuid4().hex
        path = uploads / f"{token}.zip"
        try:
            size = save_limited(upload, path, MAX_RUN_ZIP_BYTES, "ZIP")
            path.with_suffix(".json").write_text(json.dumps({"name": Path(upload.filename).name,
                                                             "size": human_size(size)}), encoding="utf-8")
            info = describe_run(f"up-{token}", fps, sheet_name)
        except BaseException:
            path.unlink(missing_ok=True)
            path.with_suffix(".json").unlink(missing_ok=True)
            raise
        return jsonify(info), 201

    @app.post("/sessions")
    def create_session():
        if request.form.get("source") == "v2":
            return create_import_session()
        script = require_script()
        fps, sheet_name = read_settings()
        try:
            check_ffmpeg()  # pass 2 fits every line to its slot
        except RuntimeError as exc:
            raise UploadError(str(exc), 503) from None
        synthesizer()  # refuse before storing anything if the key is missing

        remove_old_sessions()
        session_id = uuid.uuid4().hex
        folder = runtime / session_id
        folder.mkdir(parents=True)
        try:
            size = save_limited(script, folder / "script.xlsx", MAX_SCRIPT_BYTES, "script")
            _rows, summary = parse_script(folder / "script.xlsx", fps, sheet_name)
            record = {"session_id": session_id, "status": "dubbing", "created": time.time(), "source": "generate",
                      "fps": fps, "sheet": sheet_name,
                      "script": {"name": script.filename, "size": human_size(size)},
                      "summary": summary, "audio": None, "video": None, "error": None, "failed_row": None,
                      "takes": {}, "edits": {}, "line_speeds": {}}
            save_record(record)
            start_dubbing(session_id)
        except BaseException:
            shutil.rmtree(folder, ignore_errors=True)
            raise
        return jsonify(public(record)), 201

    def create_import_session():
        """One episode of an AD version 2 run: its fitted clips become the takes, no ElevenLabs."""
        fps, sheet_name = read_settings()
        run_id, episode = request.form.get("run", ""), request.form.get("episode", "")
        run, details = open_run(run_id, fps, sheet_name)
        remove_old_sessions()
        session_id = uuid.uuid4().hex
        folder = runtime / session_id
        with run:
            try:
                rows = run.episode_rows(episode)
                summary = summarize_episode(rows, run.fps)
                clips, fits = run.clips(rows), run.fits(rows)
            except ValueError as exc:  # RunError and EpisodeError
                raise UploadError(str(exc)) from None
            folder.mkdir(parents=True)
            try:
                run.save_script(folder / "script.xlsx")
                takes = import_clips(folder, rows, clips, fits)
                name = details.get("download_name") or details.get("name") or V2_OUTPUT
                record = {"session_id": session_id, "status": "dubbing", "created": time.time(), "source": "v2",
                          "run": {"id": run_id, "name": name}, "episode": episode,
                          "fps": run.fps, "sheet": run.sheet,
                          "script": {"name": f"{Path(name).stem}_{episode_tag(episode)}.xlsx",
                                     "size": human_size(len(run.script))},
                          "summary": summary, "audio": None, "video": None, "error": None, "failed_row": None,
                          "takes": {str(row): info for row, info in takes.items()}, "edits": {}, "line_speeds": {}}
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
                check_ffmpeg()  # the new take is fitted to its slot
                audio, changed, moved, take = regenerate_line(runtime / session_id, rows, summary, record["fps"],
                                                              synthesizer(), row_number, record["audio"])
            except RuntimeError as exc:  # DubError (ElevenLabs or the fit) or FFmpeg missing
                raise UploadError(str(exc), 502 if isinstance(exc, DubError) else 503) from None
            if text == first_text:
                edits.pop(str(row_number), None)  # back to the script's own wording
            else:
                edits[str(row_number)] = {"text": text, "original": first_text}
            speeds = dict(record.get("line_speeds") or {})
            speeds.pop(str(row_number), None)  # a new take plays at its own fit speed
            takes = dict(record.get("takes") or {}, **{str(row_number): take})
            record = update_record(session_id, audio=audio, edits=edits, line_speeds=speeds, takes=takes)
        return jsonify(dict(public(record), changed_tracks=changed, moved=moved, row=row_number))

    @app.post("/session/<session_id>/lines/<int:row_number>/speed")
    def line_speed(session_id, row_number):
        """Set one line's speed (relative to the natural take), keeping its start time. No ElevenLabs
        request. Choosing the line's fit speed goes back to pass 2's result."""
        session_dir(session_id)
        body = request.get_json(silent=True) or {}
        try:
            speed = round(validate_speed(body.get("speed")), 2)
        except ValueError as exc:
            raise UploadError(f"{exc}.") from None
        with line_change(session_id) as record:
            rows, summary = episode_rows(record)
            if not any(row.row_number == row_number for row in rows):
                abort(404)
            key = str(row_number)
            take = take_info(record, key)
            try:
                audio, changed, moved = set_line_speed(runtime / session_id, rows, summary, record["fps"],
                                                       row_number, speed, record["audio"])
            except RuntimeError as exc:  # FFmpeg missing or failed
                raise UploadError(str(exc), 503) from None
            speeds = dict(record.get("line_speeds") or {})
            if speed == take["fit_speed"]:
                speeds.pop(key, None)
            else:
                speeds[key] = speed
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
