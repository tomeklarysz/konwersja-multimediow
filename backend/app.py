from flask import Flask, request, jsonify
from werkzeug.utils import secure_filename
import os
from pathlib import Path
import subprocess
import shutil
from tempfile import NamedTemporaryFile
from flask import send_file, abort
from io import BytesIO
import threading
import uuid
import time

# Configuration
UPLOAD_ROOT = Path(__file__).resolve().parent / "uploads"
ALLOWED_FILE_TYPES = {"image", "document", "audio", "video", "other"}

# Supported extensions (lowercase, without dot) mapped to a category
SUPPORTED_EXTENSIONS = {
    # images
    "jpg": "image",
    "jpeg": "image",
    "png": "image",
    "bmp": "image",
    # videos
    "flv": "video",
    "mov": "video",
    "mp4": "video",
    "avi": "video",
    # audio
    "wav": "audio",
    "mp3": "audio",
    # include both 3gp and 3gg since the spec used "3GG"
    "3gp": "audio",
    "3gg": "audio",
    # midi variations
    "mid": "audio",
    "midi": "audio",
}
MAX_CONTENT_LENGTH = 50 * 1024 * 1024  # 50 MB


app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = MAX_CONTENT_LENGTH

# Status endpoint
@app.route("/status/<job_id>", methods=["GET"])
def get_job_status(job_id):
    with JOBS_LOCK:
        job = JOBS.get(job_id)
        if not job:
            return jsonify({"error": "not_found", "message": "Job not found", "job_id": job_id}), 404
        return jsonify({
            "job_id": job_id,
            "status": job["status"],
            "progress": job["progress"]
        })

# Job management
JOBS = {}  # job_id: {status, progress, src_path, dst_path, error, ...}
JOBS_LOCK = threading.Lock()

# Job statuses
STATUS_QUEUED = "QUEUED"
STATUS_IN_PROGRESS = "IN_PROGRESS"
STATUS_COMPLETED = "COMPLETED"
STATUS_FAILED = "FAILED"


@app.after_request
def add_cors_headers(response):
    # Allow simple CORS for local development (adjust for production)
    response.headers['Access-Control-Allow-Origin'] = '*'
    response.headers['Access-Control-Allow-Methods'] = 'GET,POST,OPTIONS'
    response.headers['Access-Control-Allow-Headers'] = 'Content-Type,Authorization'
    return response

# Ensure upload root exists
UPLOAD_ROOT.mkdir(parents=True, exist_ok=True)


@app.route("/", methods=["GET"])
def index():
    return jsonify({"status": "ok", "message": "Flask upload API"})



@app.route("/upload", methods=["POST"])
def upload_file():
    # Expect multipart/form-data with fields: file (file) and convert_to (target extension)
    if "file" not in request.files:
        return jsonify({"error": "missing_file", "message": "No file part in the request"}), 400

    file = request.files["file"]
    convert_to_raw = request.form.get("convert_to")
    if not convert_to_raw:
        return jsonify({"error": "missing_convert_to", "message": "No target format (convert_to) provided"}), 400
    convert_to = convert_to_raw.lower().lstrip('.')

    # Infer extension from filename
    extension = Path(file.filename).suffix.lstrip('.').lower()
    if not extension:
        return jsonify({"error": "missing_extension", "message": "Could not determine file extension from filename."}), 400
    if extension not in SUPPORTED_EXTENSIONS:
        return jsonify({"error": "unsupported_extension", "message": "Unsupported file extension", "supported_extensions": sorted(list(SUPPORTED_EXTENSIONS.keys()))}), 400
    if convert_to not in SUPPORTED_EXTENSIONS:
        return jsonify({"error": "unsupported_target_extension", "message": "Requested conversion target is not supported", "supported_extensions": sorted(list(SUPPORTED_EXTENSIONS.keys()))}), 400

    file_type = SUPPORTED_EXTENSIONS[extension]
    target_type = SUPPORTED_EXTENSIONS[convert_to]
    if target_type != file_type:
        return jsonify({"error": "mismatched_target_type", "message": "Requested target extension is not the same media type as the uploaded file"}), 400

    if file.filename == "":
        return jsonify({"error": "empty_filename", "message": "Uploaded file has no filename"}), 400

    filename = secure_filename(file.filename)
    dest_dir = UPLOAD_ROOT / file_type
    dest_dir.mkdir(parents=True, exist_ok=True)
    save_path = dest_dir / filename
    file.save(save_path)

    # Generate job_id and store job info
    job_id = uuid.uuid4().hex
    converted_filename = Path(filename).stem + "." + convert_to
    converted_path = dest_dir / converted_filename
    with JOBS_LOCK:
        JOBS[job_id] = {
            "status": STATUS_QUEUED,
            "progress": 0,
            "src_path": str(save_path),
            "dst_path": str(converted_path),
            "file_type": file_type,
            "extension": extension,
            "convert_to": convert_to,
            "error": None,
            "filename": filename,
            "converted_filename": converted_filename
        }

    # Start background conversion
    threading.Thread(target=process_conversion_job, args=(job_id,), daemon=True).start()

    return jsonify({
        "job_id": job_id,
        "status": STATUS_QUEUED,
        "message": "Conversion started."
    }), 202

# Background job processor
def process_conversion_job(job_id):
    with JOBS_LOCK:
        job = JOBS.get(job_id)
        if not job:
            return
        job["status"] = STATUS_IN_PROGRESS
        job["progress"] = 10

    try:
        src_path = job["src_path"]
        dst_path = job["dst_path"]
        file_type = job["file_type"]
        convert_to = job["convert_to"]
        extension = job["extension"]

        # Simulate progress
        for p in [20, 40]:
            time.sleep(0.2)
            with JOBS_LOCK:
                job["progress"] = p

        if convert_to == extension:
            # No conversion needed, just copy
            shutil.copyfile(src_path, dst_path)
        elif file_type == "image":
            from PIL import Image
            with Image.open(src_path) as img:
                if convert_to in ("jpg", "jpeg") and img.mode in ("RGBA", "LA"):
                    background = Image.new("RGB", img.size, (255, 255, 255))
                    background.paste(img, mask=img.split()[3])
                    background.save(dst_path, format="JPEG")
                else:
                    img.save(dst_path)
        elif file_type in ("audio", "video"):
            if not shutil.which("ffmpeg"):
                raise RuntimeError("ffmpeg is required for audio/video conversion but was not found on the system PATH.")
            cmd = [
                "ffmpeg",
                "-y",
                "-i",
                str(src_path),
                str(dst_path),
            ]
            proc = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=120)
            if proc.returncode != 0:
                raise RuntimeError(f"ffmpeg conversion failed: {proc.stderr}")
        else:
            raise RuntimeError(f"Conversion for type {file_type} is not supported.")

        with JOBS_LOCK:
            job["status"] = STATUS_COMPLETED
            job["progress"] = 100
    except Exception as e:
        with JOBS_LOCK:
            job["status"] = STATUS_FAILED
            job["error"] = str(e)
            job["progress"] = 100



# Download endpoint
@app.route("/download/<job_id>", methods=["GET"])
def download_converted_file(job_id):
    with JOBS_LOCK:
        job = JOBS.get(job_id)
        if not job:
            return jsonify({"error": "not_found", "message": "Job not found", "job_id": job_id}), 404
        if job["status"] != STATUS_COMPLETED:
            return jsonify({"error": "not_ready", "message": f"Job status is {job['status']}, file not available yet.", "job_id": job_id}), 400
        dst_path = job["dst_path"]
        converted_filename = job["converted_filename"]
        # Guess mimetype from extension (basic)
        ext = Path(converted_filename).suffix.lower().lstrip('.')
        mimetype = None
        if ext in ("jpg", "jpeg"):
            mimetype = "image/jpeg"
        elif ext == "png":
            mimetype = "image/png"
        elif ext == "bmp":
            mimetype = "image/bmp"
        elif ext == "mp4":
            mimetype = "video/mp4"
        elif ext == "avi":
            mimetype = "video/x-msvideo"
        elif ext == "flv":
            mimetype = "video/x-flv"
        elif ext == "mov":
            mimetype = "video/quicktime"
        elif ext == "mp3":
            mimetype = "audio/mpeg"
        elif ext == "wav":
            mimetype = "audio/wav"
        elif ext in ("3gp", "3gg"):
            mimetype = "audio/3gpp"
        elif ext in ("mid", "midi"):
            mimetype = "audio/midi"
        # else: leave as None for Flask to guess
    try:
        return send_file(dst_path, as_attachment=True, download_name=converted_filename, mimetype=mimetype)
    except Exception as e:
        return jsonify({"error": "send_failed", "message": str(e)}), 500

if __name__ == "__main__":
    app.run(host="127.0.0.1", port=5000, debug=True)