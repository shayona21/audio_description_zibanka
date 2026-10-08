"""Independent local Flask app. Run from the repository: python -m v2.app."""

import io
import os
from pathlib import Path
import threading
import uuid

from dotenv import load_dotenv
from flask import Flask, jsonify, render_template, request, send_file
from openpyxl import Workbook

from .excel_parser import SUPPORTED_FPS, TEMPLATE_HEADERS, parse_excel
from .exporter import safe_component
from .pipeline import run_pipeline
from .tts_client import ElevenLabsClient


def create_app(config=None):
    load_dotenv(Path(__file__).with_name(".env"))
    app = Flask(__name__)
    app.config.update(MAX_CONTENT_LENGTH=20 * 1024 * 1024,
                      RUNTIME_DIR=Path(__file__).with_name("runtime"))
    if config:
        app.config.update(config)
    runtime = Path(app.config["RUNTIME_DIR"])
    jobs = {}
    app.extensions["ad_v2_jobs"] = jobs

    @app.get("/")
    def index():
        return render_template("index.html", frame_rates=SUPPORTED_FPS)

    @app.get("/template")
    def template():
        workbook = Workbook()
        sheet = workbook.active
        sheet.title = "English dialogue"
        sheet.append(TEMPLATE_HEADERS)
        sheet.freeze_panes = "A2"
        for column in "ABCDEFGH":
            sheet.column_dimensions[column].width = 24 if column != "G" else 60
        buffer = io.BytesIO()
        workbook.save(buffer)
        workbook.close()
        buffer.seek(0)
        return send_file(buffer, as_attachment=True, download_name="AD_v2_template.xlsx",
                         mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")

    @app.errorhandler(413)
    def too_large(_error):
        return jsonify(error="Workbook is too large; maximum upload size is 20 MB"), 413

    @app.post("/upload")
    def upload():
        file = request.files.get("file")
        if file is None or Path(file.filename or "").suffix.lower() != ".xlsx":
            return jsonify(error="Please upload an .xlsx workbook"), 400
        try:
            fps = int(request.form.get("fps", "25"))
            if fps not in SUPPORTED_FPS:
                raise ValueError("Unsupported frame rate")
        except ValueError as exc:
            return jsonify(error=str(exc)), 400
        output_name = request.form.get("output_name", "AD_v2_output").strip()
        if output_name.lower().endswith(".zip"):
            output_name = output_name[:-4].strip()
        if not output_name:
            return jsonify(error="Please enter an output file name"), 400
        download_name = safe_component(output_name) + ".zip"
        job_id = uuid.uuid4().hex
        job_dir = runtime / job_id
        job_dir.mkdir(parents=True)
        upload_path = job_dir / "input.xlsx"
        file.save(upload_path)
        sheet_name = request.form.get("sheet", "").strip() or None
        try:
            rows = parse_excel(upload_path, fps=fps, sheet_name=sheet_name)
        except Exception as exc:
            upload_path.unlink(missing_ok=True)
            job_dir.rmdir()
            return jsonify(error=f"Workbook validation failed: {exc}"), 400
        try:
            synthesize = app.config.get("SYNTHESIZE")
            if synthesize is None:
                synthesize = ElevenLabsClient()
        except ValueError as exc:
            upload_path.unlink(missing_ok=True)
            job_dir.rmdir()
            return jsonify(error=str(exc)), 503
        jobs[job_id] = {"status": "running", "progress": [], "error": None,
                        "rows": len(rows), "tracks": len({row.group for row in rows}),
                        "percent": 0, "phase": "Generating audio",
                        "download_name": download_name}

        def process():
            try:
                summary = run_pipeline(upload_path, job_dir / "AD_v2_output.zip",
                                       fps=fps, sheet_name=sheet_name, synthesize=synthesize,
                                       progress=jobs[job_id]["progress"].append,
                                       progress_state=lambda state: jobs[job_id].update(state))
                jobs[job_id].update(flagged_rows=summary["flagged_rows"], status="done",
                                    percent=100, phase="Complete")
            except Exception as exc:
                jobs[job_id].update(error=str(exc), status="error")

        threading.Thread(target=process, daemon=True).start()
        return jsonify(job_id=job_id, download_name=download_name), 202

    @app.get("/status/<job_id>")
    def status(job_id):
        if job_id not in jobs:
            return jsonify(error="Job not found"), 404
        return jsonify(jobs[job_id])

    @app.get("/download/<job_id>")
    def download(job_id):
        if job_id not in jobs:
            return jsonify(error="Job not found"), 404
        if jobs[job_id]["status"] != "done":
            return jsonify(error="Output is not ready"), 409
        return send_file((runtime / job_id / "AD_v2_output.zip").resolve(),
                         as_attachment=True, download_name=jobs[job_id]["download_name"],
                         mimetype="application/zip")

    return app


if __name__ == "__main__":
    app = create_app()
    app.run(host="127.0.0.1", port=int(os.environ.get("AD_V2_PORT", "5071")),
            debug=False, threaded=True)
