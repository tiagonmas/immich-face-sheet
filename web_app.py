#!/usr/bin/env python3
"""Small browser interface for the Immich Face Sheet command-line tool."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import uuid
from urllib.parse import urlparse

from flask import Flask, jsonify, render_template, request, send_file, url_for
from PIL import Image
import requests

import immich_face_sheet as face_sheet


app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 64 * 1024 * 1024

DATA_DIR = Path(os.environ.get("DATA_DIR", "./data")).expanduser().resolve()
SCRIPT_PATH = Path(__file__).with_name("immich_face_sheet.py")
JOB_POOL = ThreadPoolExecutor(max_workers=1, thread_name_prefix="face-sheet-job")
JOBS: dict[str, dict[str, object]] = {}
JOBS_LOCK = threading.Lock()
MAX_ACTIVE_JOBS = 1
MAX_RETAINED_JOBS = 25
MAX_LOG_LINES = 500


def normalize_base_url(value: str) -> str:
    base_url = value.strip().rstrip("/")
    parsed = urlparse(base_url)
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.netloc
        or parsed.path not in {"", "/"}
        or parsed.query
        or parsed.fragment
        or parsed.username
        or parsed.password
    ):
        raise ValueError("Enter a complete Immich URL, such as http://immich:2283.")
    return base_url


def configured_api_key(value: str | None) -> str:
    key = (value or "").strip() or os.environ.get("IMMICH_API_KEY", "").strip()
    if not key:
        raise ValueError("Enter an Immich API key or set IMMICH_API_KEY for the container.")
    return key


def positive_int(value: str, label: str, default: int, maximum: int) -> int:
    if not value:
        return default
    try:
        parsed = int(value)
    except ValueError as exc:
        raise ValueError(f"{label} must be a whole number.") from exc
    if not 1 <= parsed <= maximum:
        raise ValueError(f"{label} must be between 1 and {maximum}.")
    return parsed


def reserve_job(job_id: str, job: dict[str, object]) -> bool:
    with JOBS_LOCK:
        active_count = sum(
            1 for existing in JOBS.values()
            if existing.get("status") in {"queued", "running"}
        )
        if active_count >= MAX_ACTIVE_JOBS:
            return False
        JOBS[job_id] = job
        finished = [
            key for key, value in JOBS.items()
            if value.get("status") in {"complete", "failed"}
        ]
        while len(JOBS) > MAX_RETAINED_JOBS and finished:
            JOBS.pop(finished.pop(0), None)
        return True


def set_job(job_id: str, **values: object) -> None:
    with JOBS_LOCK:
        if job_id in JOBS:
            JOBS[job_id].update(values)


def append_job_log(job_id: str, line: str) -> None:
    with JOBS_LOCK:
        job = JOBS.get(job_id)
        if job is None:
            return
        logs = job["logs"]
        assert isinstance(logs, list)
        logs.append(line.rstrip())
        if len(logs) > MAX_LOG_LINES:
            del logs[:-MAX_LOG_LINES]


def run_cli_job(
    job_id: str,
    arguments: list[str],
    api_key: str,
    output_path: Path,
    output_name: str,
    job_directory: Path,
) -> None:
    set_job(job_id, status="running")
    environment = os.environ.copy()
    environment["IMMICH_API_KEY"] = api_key
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    command = [sys.executable, str(SCRIPT_PATH), *arguments]
    try:
        process = subprocess.Popen(
            command,
            cwd=DATA_DIR,
            env=environment,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
        )
        assert process.stdout is not None
        for line in process.stdout:
            append_job_log(job_id, line)
        return_code = process.wait()
        if return_code == 0 and output_path.is_file():
            set_job(
                job_id,
                status="complete",
                image_url=f"/api/jobs/{job_id}/image",
                download_url=f"/api/jobs/{job_id}/image?download=1",
                output_name=output_name,
            )
        else:
            if return_code == 0:
                append_job_log(job_id, "The script finished without creating the expected output image.")
            set_job(job_id, status="failed", error=f"Face Sheet exited with code {return_code}.")
    except Exception as exc:
        append_job_log(job_id, f"Could not start Face Sheet: {exc}")
        set_job(job_id, status="failed", error="Could not run the face sheet job.")


@app.get("/")
def index():
    return render_template(
        "index.html",
        default_base_url=os.environ.get("IMMICH_BASE_URL", ""),
        api_key_configured=bool(os.environ.get("IMMICH_API_KEY")),
    )


@app.post("/api/people")
def people():
    payload = request.get_json(silent=True) or {}
    try:
        base_url = normalize_base_url(str(payload.get("base_url", "")))
        api_key = configured_api_key(payload.get("api_key"))
    except ValueError as exc:
        return jsonify(error=str(exc)), 400

    try:
        found = face_sheet.fetch_people(f"{base_url}/api", api_key)
        result = [
            {
                "id": str(person["id"]),
                "name": str(person.get("name") or "(unnamed)"),
                "asset_count": person.get("assetCount", "?"),
            }
            for person in found
        ]
        result.sort(key=lambda person: person["name"].casefold())
        return jsonify(people=result)
    except requests.HTTPError as exc:
        status = exc.response.status_code if exc.response is not None else 502
        message = "Immich rejected the API key. Check its people and asset read permissions." if status in {401, 403} else f"Immich returned HTTP {status} while listing people."
        return jsonify(error=message), 502
    except (requests.RequestException, ValueError) as exc:
        return jsonify(error=f"Could not connect to Immich: {exc}"), 502
    finally:
        session = getattr(face_sheet.THREAD_LOCAL, "session", None)
        if session is not None:
            session.close()
            del face_sheet.THREAD_LOCAL.session


@app.post("/api/jobs")
def create_job():
    form = request.form
    try:
        base_url = normalize_base_url(form.get("base_url", ""))
        api_key = configured_api_key(form.get("api_key"))
        person_id = form.get("person_id", "").strip()
        person_name = form.get("person_name", "").strip()
        if not person_id or not person_name:
            raise ValueError("Load the Immich people list and select a person first.")

        mode = form.get("mode", "contact-sheet")
        if mode not in {"contact-sheet", "collage", "photomosaic"}:
            raise ValueError("Choose a valid output mode.")
        columns = positive_int(form.get("faces_per_row", "10"), "Faces per row", 10, 100)
        face_size = positive_int(form.get("face_size", "112"), "Face size", 112, 2000)
        workers = positive_int(form.get("workers", "6"), "Workers", 6, 32)
        spacing = int(form.get("face_spacing", "0"))
        max_faces_value = form.get("max_faces", "").strip()
        max_faces = positive_int(max_faces_value, "Maximum faces", 1, 1_000_000) if max_faces_value else None
        if not 0 <= spacing <= 1000:
            raise ValueError("Face spacing must be between 0 and 1000 pixels.")
        tile_size_value = form.get("photomosaic_tile_size", "").strip()
        mosaic_tile_size = positive_int(tile_size_value, "Photomosaic tile size", 1, 2000) if tile_size_value else None
        output_name = form.get("output_name", "").strip()
        if not output_name:
            prefix = face_sheet.person_name_slug(person_name)
            suffix = {"contact-sheet": "faces", "collage": "collage", "photomosaic": "photomosaic"}[mode]
            output_name = f"{prefix}_{suffix}.jpg"
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,99}\.(?:jpg|jpeg)", output_name, re.IGNORECASE):
            raise ValueError("Output filename must be a .jpg or .jpeg filename without folders.")

        target_upload = request.files.get("photomosaic_image")
        if mode == "photomosaic" and (target_upload is None or not target_upload.filename):
            raise ValueError("Choose a target image for photomosaic mode.")
        if mode != "photomosaic" and target_upload and target_upload.filename:
            raise ValueError("A target image can only be uploaded in photomosaic mode.")
    except (ValueError, TypeError) as exc:
        return jsonify(error=str(exc)), 400

    job_id = uuid.uuid4().hex
    job_directory = DATA_DIR / "results" / job_id
    job_directory.mkdir(parents=True, exist_ok=False)
    output_path = job_directory / output_name
    arguments = [
        "--base-url", base_url,
        "--person-name", person_name,
        "--person-id", person_id,
        "--mode", mode,
        "--output", str(output_path),
        "--faces-per-row", str(columns),
        "--face-size", str(face_size),
        "--face-spacing", str(spacing),
        "--workers", str(workers),
    ]
    if max_faces is not None:
        arguments.extend(["--max-faces", str(max_faces)])
    if form.get("include_metadata") == "on":
        arguments.append("--include-metadata")
    if form.get("save_crops") == "on":
        arguments.append("--save-crops")
    if form.get("force_fetch") == "on":
        arguments.append("--force-fetch")
    if mode == "photomosaic":
        target_path = job_directory / "photomosaic-target"
        assert target_upload is not None
        target_upload.save(target_path)
        try:
            with Image.open(target_path) as target:
                target.verify()
        except Exception:
            target_path.unlink(missing_ok=True)
            shutil.rmtree(job_directory, ignore_errors=True)
            return jsonify(error="The uploaded target is not a valid image."), 400
        arguments.extend(["--photomosaic-image", str(target_path)])
        if mosaic_tile_size is not None:
            arguments.extend(["--photomosaic-tile-size", str(mosaic_tile_size)])
        else:
            arguments.append("--auto-photomosaic-tile-size")

    if not reserve_job(job_id, {
        "status": "queued",
        "person_name": person_name,
        "mode": mode,
        "logs": [],
        "created_at": time.time(),
    }):
        shutil.rmtree(job_directory, ignore_errors=True)
        return jsonify(error="A job is already running. Wait for it to finish and try again."), 429
    JOB_POOL.submit(
        run_cli_job, job_id, arguments, api_key, output_path,
        output_name, job_directory,
    )
    return jsonify(job_id=job_id, status_url=url_for("job_status", job_id=job_id)), 202


@app.get("/api/jobs/<job_id>")
def job_status(job_id: str):
    with JOBS_LOCK:
        job = JOBS.get(job_id)
        if job is None:
            return jsonify(error="This job was not found. It may have expired or the app restarted."), 404
        result = dict(job)
        result.pop("created_at", None)
        result["logs"] = list(job["logs"])
    return jsonify(result)


@app.get("/api/jobs/<job_id>/image")
def job_image(job_id: str):
    with JOBS_LOCK:
        job = JOBS.get(job_id)
        if job is None or job.get("status") != "complete":
            return jsonify(error="The output image is not ready."), 404
        output_name = str(job["output_name"])
    path = DATA_DIR / "results" / job_id / output_name
    if not path.is_file():
        return jsonify(error="The output image is no longer available."), 404
    response = send_file(
        path,
        mimetype="image/jpeg",
        as_attachment=request.args.get("download") == "1",
        download_name=output_name,
        conditional=True,
    )
    response.headers["Cache-Control"] = "no-store"
    return response


@app.errorhandler(413)
def upload_too_large(_error):
    return jsonify(error="Uploaded images must be 64 MB or smaller."), 413


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", "8080")), threaded=True)
