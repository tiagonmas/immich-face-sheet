#!/usr/bin/env python3
"""Small browser interface for the Immich Face Sheet command-line tool."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import uuid
import zipfile
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
            if existing.get("status") in {"queued", "running", "generating"}
        )
        if active_count >= MAX_ACTIVE_JOBS:
            return False
        JOBS[job_id] = job
        finished = [
            key for key, value in JOBS.items()
            if value.get("status") in {"complete", "failed", "review"}
        ]
        while len(JOBS) > MAX_RETAINED_JOBS and finished:
            JOBS.pop(finished.pop(0), None)
        return True


def set_job(job_id: str, **values: object) -> None:
    with JOBS_LOCK:
        if job_id in JOBS:
            JOBS[job_id].update(values)
            persist_run_metadata(JOBS[job_id])


def persist_run_metadata(job: dict[str, object]) -> None:
    metadata_path = job.get("_metadata_path")
    if not metadata_path:
        return
    outputs = list(job.get("outputs") or [])
    payload = {
        "job_id": str(job.get("job_id") or Path(str(metadata_path)).parent.name),
        "created_at": job.get("created_at"),
        "person_name": job.get("person_name", "Person"),
        "mode": job.get("mode", "contact-sheet"),
        "output_name": job.get("output_name", ""),
        "status": "complete" if outputs else job.get("status", "unknown"),
        "crop_count": job.get("crop_count"),
        "outputs": outputs,
    }
    path = Path(str(metadata_path))
    try:
        temporary_path = path.with_suffix(".tmp")
        temporary_path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        temporary_path.replace(path)
    except OSError:
        # Job progress should continue even if run-history metadata cannot be saved.
        pass


def append_job_log(job_id: str, line: str) -> None:
    with JOBS_LOCK:
        job = JOBS.get(job_id)
        if job is None:
            return
        progress = re.search(r"Processed (\d+)/(\d+) assets; (\d+) face crops", line)
        if progress:
            job["progress_current"] = int(progress.group(1))
            job["progress_total"] = int(progress.group(2))
            job["progress_crops"] = int(progress.group(3))
        logs = job["logs"]
        assert isinstance(logs, list)
        logs.append(line.rstrip())
        if len(logs) > MAX_LOG_LINES:
            del logs[:-MAX_LOG_LINES]


def run_cli_job(
    job_id: str,
    arguments: list[str],
    api_key: str,
    preview_directory: Path,
) -> None:
    set_job(job_id, status="running", progress_current=0, progress_total=0, progress_crops=0)
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
        manifest_path = preview_directory / "manifest.json"
        if return_code == 0 and manifest_path.is_file():
            records = json.loads(manifest_path.read_text(encoding="utf-8"))
            set_job(job_id, status="review", crop_count=len(records))
        else:
            if return_code == 0:
                append_job_log(job_id, "The script finished without creating the crop review manifest.")
            set_job(job_id, status="failed", error=f"Face Sheet exited with code {return_code}.")
    except Exception as exc:
        append_job_log(job_id, f"Could not start Face Sheet: {exc}")
        set_job(job_id, status="failed", error="Could not run the face sheet job.")


def run_generation_job(job_id: str, selected_ids: list[int]) -> None:
    set_job(job_id, status="generating")
    try:
        with JOBS_LOCK:
            job = JOBS.get(job_id)
            if job is None:
                raise RuntimeError("This review has expired.")
            preview_directory = Path(str(job["_preview_directory"]))
            settings = dict(job["_generation_settings"])

        records = json.loads((preview_directory / "manifest.json").read_text(encoding="utf-8"))
        records_by_id = {int(record["id"]): record for record in records}
        if not selected_ids or any(crop_id not in records_by_id for crop_id in selected_ids):
            raise ValueError("Select at least one valid crop to continue.")

        crops: list[tuple[Image.Image, str]] = []
        for index, crop_id in enumerate(selected_ids, start=1):
            record = records_by_id[crop_id]
            with Image.open(preview_directory / f"{crop_id}.jpg") as image:
                crop = image.convert("RGB").copy()
            label = ""
            if settings["include_metadata"] and settings["mode"] == "contact-sheet":
                date = str(record.get("date") or "")[:10]
                label = f"{index:04d} {date}\n{record['filename']}"
            crops.append((crop, label))

        output_path = Path(str(settings["output_path"]))
        mode = str(settings["mode"])
        if mode == "contact-sheet":
            face_sheet.validate_sheet_dimensions(
                len(crops), int(settings["columns"]), int(settings["tile_size"]),
                int(settings["spacing"]), bool(settings["include_metadata"]),
            )
            face_sheet.build_sheet(
                crops, output_path, int(settings["columns"]), int(settings["tile_size"]),
                int(settings["spacing"]), bool(settings["include_metadata"]),
            )
        elif mode == "collage":
            face_sheet.build_collage(
                crops, output_path, int(settings["columns"]), int(settings["tile_size"]),
                int(settings["spacing"]),
            )
        else:
            target_path = Path(str(settings["target_path"]))
            with Image.open(target_path) as target_file:
                target = face_sheet.ImageOps.exif_transpose(target_file).convert("RGB")
            tile_size = settings["mosaic_tile_size"]
            if tile_size is None:
                tile_size = face_sheet.minimum_photomosaic_tile_size(target, len(crops))
            face_sheet.validate_photomosaic_dimensions(target, int(tile_size), int(settings["spacing"]))
            face_sheet.build_photomosaic(crops, target, output_path, int(tile_size), int(settings["spacing"]))

        if settings["save_crops"]:
            face_sheet.save_crops(crops, DATA_DIR / "_crops", str(settings["person_name"]))
        with JOBS_LOCK:
            job = JOBS.get(job_id)
            if job is not None:
                outputs = list(job.get("outputs") or [])
                outputs.append({
                    "output_name": str(settings["output_name"]),
                    "mode": str(settings["mode"]),
                    "created_at": time.time(),
                })
                job.update(
                    status="complete",
                    image_url=f"/api/jobs/{job_id}/image",
                    download_url=f"/api/jobs/{job_id}/image?download=1",
                    output_name=str(settings["output_name"]),
                    outputs=outputs,
                )
                persist_run_metadata(job)
    except Exception as exc:
        append_job_log(job_id, f"Could not create the selected output: {exc}")
        set_job(job_id, status="failed", error=f"Could not create the selected output: {exc}")


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
        result = []
        for person in found:
            name = str(person.get("name") or "").strip()
            if not name:
                continue
            result.append({
                "id": str(person["id"]),
                "name": name,
                "asset_count": person.get("assetCount", "?"),
            })
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
        workers = positive_int(form.get("workers", "6"), "Workers", 6, 32)
        max_faces_value = form.get("max_faces", "").strip()
        max_faces = positive_int(max_faces_value, "Maximum faces", 1, 1_000_000) if max_faces_value else None
    except (ValueError, TypeError) as exc:
        return jsonify(error=str(exc)), 400

    job_id = uuid.uuid4().hex
    job_directory = DATA_DIR / "results" / job_id
    job_directory.mkdir(parents=True, exist_ok=False)
    preview_directory = job_directory / "crop-previews"
    arguments = [
        "--base-url", base_url,
        "--person-name", person_name,
        "--person-id", person_id,
        "--mode", "contact-sheet",
        "--workers", str(workers),
        "--crop-preview-dir", str(preview_directory),
    ]
    if max_faces is not None:
        arguments.extend(["--max-faces", str(max_faces)])
    if form.get("force_fetch") == "on":
        arguments.append("--force-fetch")

    if not reserve_job(job_id, {
        "status": "queued",
        "job_id": job_id,
        "person_name": person_name,
        "mode": "contact-sheet",
        "output_name": "",
        "outputs": [],
        "logs": [],
        "created_at": time.time(),
        "_metadata_path": str(job_directory / "run.json"),
        "_preview_directory": str(preview_directory),
    }):
        shutil.rmtree(job_directory, ignore_errors=True)
        return jsonify(error="A job is already running. Wait for it to finish and try again."), 429
    with JOBS_LOCK:
        persist_run_metadata(JOBS[job_id])
    JOB_POOL.submit(
        run_cli_job, job_id, arguments, api_key, preview_directory,
    )
    return jsonify(job_id=job_id, status_url=url_for("job_status", job_id=job_id)), 202


@app.get("/api/jobs/<job_id>")
def job_status(job_id: str):
    with JOBS_LOCK:
        job = JOBS.get(job_id)
        if job is None:
            return jsonify(error="This job was not found. It may have expired or the app restarted."), 404
        result = {key: value for key, value in job.items() if not key.startswith("_") and key != "created_at"}
        result["logs"] = list(job["logs"])
    return jsonify(result)


@app.get("/api/jobs/<job_id>/crops")
def job_crops(job_id: str):
    with JOBS_LOCK:
        job = JOBS.get(job_id)
        if job is None or job.get("status") != "review":
            return jsonify(error="The crop review is not ready."), 404
        preview_directory = Path(str(job["_preview_directory"]))
    try:
        records = json.loads((preview_directory / "manifest.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return jsonify(error="The crop review is no longer available."), 404
    crops = []
    for record in records:
        crop_id = int(record["id"])
        crop_path = preview_directory / f"{crop_id}.jpg"
        try:
            file_size = crop_path.stat().st_size
            with Image.open(crop_path) as image:
                width, height = image.size
        except (OSError, ValueError):
            return jsonify(error="A crop preview is missing or unreadable."), 404
        crops.append({
            "id": crop_id,
            "filename": str(record.get("filename") or "Unknown file"),
            "date": str(record.get("date") or ""),
            "file_size_bytes": file_size,
            "width": width,
            "height": height,
            "image_url": url_for("job_crop_image", job_id=job_id, crop_id=crop_id),
        })
    return jsonify(crops=crops)


def crop_archive_response(preview_directory: Path):
    try:
        records = json.loads((preview_directory / "manifest.json").read_text(encoding="utf-8"))
        temporary = tempfile.NamedTemporaryFile(prefix="face-crops-", suffix=".zip", delete=False)
        archive_path = Path(temporary.name)
        temporary.close()
        with zipfile.ZipFile(archive_path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            for record in records:
                crop_id = int(record["id"])
                crop_path = preview_directory / f"{crop_id}.jpg"
                if not crop_path.is_file():
                    raise OSError("A crop preview is missing.")
                original_name = Path(str(record.get("filename") or "crop")).name
                safe_stem = re.sub(r"[^A-Za-z0-9._-]+", "_", Path(original_name).stem).strip("._-") or "crop"
                archive.write(crop_path, arcname=f"{crop_id + 1:06d}_{safe_stem}.jpg")
    except (OSError, ValueError, TypeError, KeyError, json.JSONDecodeError):
        if "archive_path" in locals():
            archive_path.unlink(missing_ok=True)
        return jsonify(error="Could not prepare the face crop archive."), 404

    response = send_file(
        archive_path,
        mimetype="application/zip",
        as_attachment=True,
        download_name="face-crops.zip",
        conditional=True,
    )
    response.call_on_close(lambda: archive_path.unlink(missing_ok=True))
    return response


@app.get("/api/jobs/<job_id>/crops.zip")
def download_job_crops(job_id: str):
    with JOBS_LOCK:
        job = JOBS.get(job_id)
        if job is None or job.get("status") not in {"review", "queued", "generating", "complete"}:
            return jsonify(error="These face crops are not available."), 404
        preview_directory = Path(str(job["_preview_directory"]))
    return crop_archive_response(preview_directory)


@app.get("/api/runs/<job_id>/crops.zip")
def download_previous_run_crops(job_id: str):
    if not re.fullmatch(r"[a-f0-9]{32}", job_id):
        return jsonify(error="Run not found."), 404
    preview_directory = DATA_DIR / "results" / job_id / "crop-previews"
    if not (preview_directory / "manifest.json").is_file():
        return jsonify(error="This run has no saved crop previews."), 404
    return crop_archive_response(preview_directory)


@app.get("/api/jobs/<job_id>/crops/<int:crop_id>")
def job_crop_image(job_id: str, crop_id: int):
    with JOBS_LOCK:
        job = JOBS.get(job_id)
        if job is None or job.get("status") not in {"review", "queued", "generating", "complete"}:
            return jsonify(error="This crop is not available."), 404
        preview_directory = Path(str(job["_preview_directory"]))
    path = preview_directory / f"{crop_id}.jpg"
    if not path.is_file():
        return jsonify(error="This crop was not found."), 404
    response = send_file(path, mimetype="image/jpeg", conditional=True)
    response.headers["Cache-Control"] = "no-store"
    return response


@app.post("/api/jobs/<job_id>/generate")
def generate_selected(job_id: str):
    try:
        selected = json.loads(request.form.get("selected_ids", "null"))
    except json.JSONDecodeError:
        selected = None
    if not isinstance(selected, list) or not selected or any(
        isinstance(value, bool) or not isinstance(value, int) for value in selected
    ):
        return jsonify(error="Select at least one crop to continue."), 400
    if len(set(selected)) != len(selected):
        return jsonify(error="The selection contains duplicate crops."), 400

    with JOBS_LOCK:
        job = JOBS.get(job_id)
        if job is None:
            return jsonify(error="This job was not found. It may have expired or the app restarted."), 404
        if job.get("status") not in {"review", "complete", "failed"}:
            return jsonify(error="This crop review has already been submitted."), 409
        preview_directory = Path(str(job["_preview_directory"]))
    try:
        records = json.loads((preview_directory / "manifest.json").read_text(encoding="utf-8"))
        available = {int(record["id"]) for record in records}
    except (OSError, json.JSONDecodeError, KeyError, TypeError, ValueError):
        return jsonify(error="The crop review is no longer available."), 404
    if any(crop_id not in available for crop_id in selected):
        return jsonify(error="The selection contains a crop that is no longer available."), 400

    form = request.form
    target_path = None
    try:
        mode = form.get("mode", "contact-sheet")
        if mode not in {"contact-sheet", "collage", "photomosaic"}:
            raise ValueError("Choose a valid output mode.")
        columns = positive_int(form.get("faces_per_row", "10"), "Faces per row", 10, 100)
        face_size = positive_int(form.get("face_size", "112"), "Face size", 112, 2000)
        spacing = int(form.get("face_spacing", "0"))
        if not 0 <= spacing <= 1000:
            raise ValueError("Face spacing must be between 0 and 1000 pixels.")
        tile_size_value = form.get("photomosaic_tile_size", "").strip()
        mosaic_tile_size = positive_int(tile_size_value, "Photomosaic tile size", 1, 2000) if tile_size_value else None
        output_name = form.get("output_name", "").strip()
        if not output_name:
            prefix = face_sheet.person_name_slug(str(job["person_name"]))
            suffix = {"contact-sheet": "faces", "collage": "collage", "photomosaic": "photomosaic"}[mode]
            output_name = f"{prefix}_{suffix}.jpg"
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,99}\.(?:jpg|jpeg)", output_name, re.IGNORECASE):
            raise ValueError("Output filename must be a .jpg or .jpeg filename without folders.")

        job_directory = preview_directory.parent
        used_names = {str(output.get("output_name") or "") for output in job.get("outputs", [])}
        output_path = job_directory / output_name
        if output_name in used_names or output_path.exists():
            suffix = Path(output_name).suffix
            stem = Path(output_name).stem
            index = 2
            while True:
                marker = f"_{index}"
                unique_stem = f"{stem[:100 - len(marker)].rstrip('._-')}{marker}"
                candidate_name = f"{unique_stem}{suffix}"
                candidate_path = job_directory / candidate_name
                if candidate_name not in used_names and not candidate_path.exists():
                    output_name = candidate_name
                    output_path = candidate_path
                    break
                index += 1
        target_upload = request.files.get("photomosaic_image")
        if mode == "photomosaic":
            if target_upload is None or not target_upload.filename:
                raise ValueError("Choose a target image for photomosaic mode.")
            target_path = job_directory / f"{Path(output_name).stem}-target"
            target_upload.save(target_path)
            try:
                with Image.open(target_path) as target:
                    target.verify()
            except Exception as exc:
                target_path.unlink(missing_ok=True)
                target_path = None
                raise ValueError("The uploaded target is not a valid image.") from exc
        elif target_upload and target_upload.filename:
            raise ValueError("A target image can only be uploaded in photomosaic mode.")
    except (ValueError, TypeError) as exc:
        if target_path is not None:
            target_path.unlink(missing_ok=True)
        return jsonify(error=str(exc)), 400

    settings = {
        "mode": mode,
        "person_name": str(job["person_name"]),
        "output_path": str(output_path),
        "output_name": output_name,
        "columns": columns,
        "tile_size": face_size,
        "spacing": spacing,
        "include_metadata": form.get("include_metadata") == "on" and mode == "contact-sheet",
        "save_crops": form.get("save_crops") == "on",
        "target_path": str(target_path) if target_path else None,
        "mosaic_tile_size": mosaic_tile_size,
    }
    conflict = False
    with JOBS_LOCK:
        job = JOBS.get(job_id)
        if job is None or job.get("status") not in {"review", "complete", "failed"}:
            conflict = True
        else:
            job["_generation_settings"] = settings
            job["mode"] = mode
            job["output_name"] = output_name
            job["status"] = "queued"
            persist_run_metadata(job)
    if conflict:
        if target_path is not None:
            target_path.unlink(missing_ok=True)
        return jsonify(error="This crop review has already been submitted."), 409
    JOB_POOL.submit(run_generation_job, job_id, selected)
    return jsonify(status="queued"), 202


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


def run_directory_usage(directory: Path) -> tuple[int, int]:
    file_count = 0
    total_bytes = 0
    for root, _directories, filenames in os.walk(directory, followlinks=False):
        for filename in filenames:
            path = Path(root) / filename
            try:
                if path.is_symlink() or not path.is_file():
                    continue
                file_count += 1
                total_bytes += path.stat().st_size
            except OSError:
                continue
    return file_count, total_bytes


def previous_run_records() -> list[dict[str, object]]:
    results_directory = DATA_DIR / "results"
    if not results_directory.is_dir():
        return []
    records: list[dict[str, object]] = []
    for directory in results_directory.iterdir():
        if not directory.is_dir():
            continue
        metadata_path = directory / "run.json"
        try:
            if metadata_path.is_file():
                metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
                if metadata.get("status") != "complete":
                    continue
                outputs = metadata.get("outputs")
                if not isinstance(outputs, list) or not outputs:
                    outputs = [{
                        "output_name": metadata.get("output_name"),
                        "mode": metadata.get("mode"),
                        "created_at": metadata.get("created_at"),
                    }]
                created_at = metadata.get("created_at") or directory.stat().st_mtime
                file_count, size_bytes = run_directory_usage(directory)
                crop_manifest = directory / "crop-previews" / "manifest.json"
                for output in outputs:
                    if not isinstance(output, dict):
                        continue
                    output_name = str(output.get("output_name") or "")
                    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,99}\.(?:jpg|jpeg)", output_name, re.IGNORECASE):
                        continue
                    if not (directory / output_name).is_file():
                        continue
                    records.append({
                        "job_id": directory.name,
                        "created_at": output.get("created_at") or created_at,
                        "person_name": str(metadata.get("person_name") or "Person"),
                        "mode": str(output.get("mode") or metadata.get("mode") or "image"),
                        "output_name": output_name,
                        "file_count": file_count,
                        "size_bytes": size_bytes,
                        "download_url": url_for("previous_run_download", job_id=directory.name, output=output_name),
                        "crop_download_url": url_for("download_previous_run_crops", job_id=directory.name) if crop_manifest.is_file() else None,
                    })
                continue
            # Keep showing completed images made before run-history metadata was added.
            images = sorted(
                path for path in directory.iterdir()
                if path.is_file() and path.suffix.lower() in {".jpg", ".jpeg"}
            )
            file_count, size_bytes = run_directory_usage(directory)
            crop_manifest = directory / "crop-previews" / "manifest.json"
            for output_path in images:
                stem = output_path.stem
                if stem.endswith("_photomosaic"):
                    mode = "Photomosaic"
                elif stem.endswith("_collage"):
                    mode = "Collage"
                elif stem.endswith("_faces"):
                    mode = "Contact sheet"
                else:
                    mode = "Image"
                records.append({
                    "job_id": directory.name,
                    "created_at": output_path.stat().st_mtime,
                    "person_name": stem.rsplit("_", 1)[0].replace("_", " ").title(),
                    "mode": mode,
                    "output_name": output_path.name,
                    "file_count": file_count,
                    "size_bytes": size_bytes,
                    "download_url": url_for("previous_run_download", job_id=directory.name, output=output_path.name),
                    "crop_download_url": url_for("download_previous_run_crops", job_id=directory.name) if crop_manifest.is_file() else None,
                })
        except (OSError, json.JSONDecodeError, TypeError, ValueError):
            continue
    records.sort(key=lambda record: float(record.get("created_at") or 0), reverse=True)
    return records


@app.get("/api/runs")
def previous_runs():
    runs = previous_run_records()
    unique_runs = {str(run["job_id"]): run for run in runs}
    return jsonify(
        runs=runs,
        total_files=sum(int(run["file_count"]) for run in unique_runs.values()),
        total_bytes=sum(int(run["size_bytes"]) for run in unique_runs.values()),
    )


@app.get("/api/runs/<job_id>/download")
def previous_run_download(job_id: str):
    if not re.fullmatch(r"[a-f0-9]{32}", job_id):
        return jsonify(error="Run not found."), 404
    directory = DATA_DIR / "results" / job_id
    output_path = None
    metadata_path = directory / "run.json"
    try:
        if metadata_path.is_file():
            metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
            if metadata.get("status") == "complete":
                requested_name = request.args.get("output", "").strip()
                outputs = metadata.get("outputs")
                if isinstance(outputs, list) and outputs:
                    valid_names = [
                        str(output.get("output_name") or "")
                        for output in outputs if isinstance(output, dict)
                    ]
                    name = requested_name if requested_name in valid_names else (valid_names[-1] if not requested_name and valid_names else "")
                else:
                    name = requested_name or str(metadata.get("output_name") or "")
                if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,99}\.(?:jpg|jpeg)", name, re.IGNORECASE):
                    output_path = directory / name
        else:
            images = sorted(
                path for path in directory.iterdir()
                if path.is_file() and path.suffix.lower() in {".jpg", ".jpeg"}
            ) if directory.is_dir() else []
            requested_name = request.args.get("output", "").strip()
            output_path = next((path for path in images if path.name == requested_name), None) if requested_name else (images[0] if images else None)
    except (OSError, json.JSONDecodeError, TypeError, ValueError):
        output_path = None
    if output_path is None or not output_path.is_file() or output_path.parent != directory:
        return jsonify(error="Run result not found."), 404
    return send_file(
        output_path,
        mimetype="image/jpeg",
        as_attachment=True,
        download_name=output_path.name,
        conditional=True,
    )


@app.post("/api/runs/<job_id>/delete")
def delete_previous_run(job_id: str):
    if not re.fullmatch(r"[a-f0-9]{32}", job_id):
        return jsonify(error="Run not found."), 404
    directory = DATA_DIR / "results" / job_id
    payload = request.get_json(silent=True) or {}
    output_name = str(payload.get("output_name") or "").strip()
    with JOBS_LOCK:
        job = JOBS.get(job_id)
        if job is not None and job.get("status") in {"queued", "running", "generating", "review"}:
            return jsonify(error="This run is still active and cannot be deleted yet."), 409
        if not directory.is_dir():
            return jsonify(error="Run not found."), 404
        if output_name:
            if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,99}\.(?:jpg|jpeg)", output_name, re.IGNORECASE):
                return jsonify(error="Output not found."), 404
            metadata_path = directory / "run.json"
            try:
                metadata = json.loads(metadata_path.read_text(encoding="utf-8")) if metadata_path.is_file() else {}
                outputs = metadata.get("outputs")
                if isinstance(outputs, list) and outputs:
                    remaining = [
                        item for item in outputs
                        if isinstance(item, dict) and str(item.get("output_name") or "") != output_name
                    ]
                    if len(remaining) == len(outputs):
                        return jsonify(error="Run result not found."), 404
                    (directory / output_name).unlink(missing_ok=True)
                    (directory / f"{Path(output_name).stem}-target").unlink(missing_ok=True)
                    if remaining:
                        metadata["outputs"] = remaining
                        latest = remaining[-1]
                        metadata["output_name"] = latest.get("output_name", "")
                        metadata["mode"] = latest.get("mode", metadata.get("mode", "contact-sheet"))
                        metadata_path.write_text(json.dumps(metadata, ensure_ascii=False), encoding="utf-8")
                        if job is not None:
                            job["outputs"] = remaining
                            job["output_name"] = metadata["output_name"]
                            job["mode"] = metadata["mode"]
                            job["status"] = "complete"
                            persist_run_metadata(job)
                        return jsonify(deleted=True, deleted_output=output_name, deleted_run=False)
                else:
                    existing_name = str(metadata.get("output_name") or "")
                    if existing_name and existing_name != output_name:
                        return jsonify(error="Run result not found."), 404
            except (OSError, json.JSONDecodeError, TypeError, ValueError):
                return jsonify(error="Could not read this run's metadata."), 500
        try:
            shutil.rmtree(directory)
        except OSError:
            return jsonify(error="Could not delete this run's stored files."), 500
        JOBS.pop(job_id, None)
    return jsonify(deleted=True, deleted_output=output_name or None, deleted_run=True)


@app.post("/api/runs/delete-all")
def delete_previous_runs():
    with JOBS_LOCK:
        protected_ids = {
            key for key, job in JOBS.items()
            if job.get("status") in {"queued", "running", "generating", "review"}
        }
        active_job_exists = any(
            job.get("status") in {"queued", "running", "generating"}
            for job in JOBS.values()
        )
        results_directory = DATA_DIR / "results"
        deleted = 0
        if results_directory.is_dir():
            for directory in results_directory.iterdir():
                if directory.is_dir() and directory.name not in protected_ids:
                    shutil.rmtree(directory, ignore_errors=True)
                    if not directory.exists():
                        deleted += 1
        shared_crops_directory = DATA_DIR / "_crops"
        deleted_shared_crops = False
        if not active_job_exists and shared_crops_directory.is_dir():
            shutil.rmtree(shared_crops_directory, ignore_errors=True)
            deleted_shared_crops = not shared_crops_directory.exists()
        for job_id in list(JOBS):
            if job_id not in protected_ids:
                JOBS.pop(job_id, None)
    return jsonify(
        deleted_runs=deleted,
        deleted_shared_crops=deleted_shared_crops,
        shared_crops_kept=active_job_exists,
    )


@app.errorhandler(413)
def upload_too_large(_error):
    return jsonify(error="Uploaded images must be 64 MB or smaller."), 413


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", "8080")), threaded=True)
