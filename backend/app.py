from flask import Flask, request, jsonify
from werkzeug.utils import secure_filename
import os
from pathlib import Path
import subprocess
import shutil
from tempfile import NamedTemporaryFile
from flask import send_file, abort
from io import BytesIO

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
    # Expect multipart/form-data with fields: file_type (string) and file (file)
    if "file" not in request.files:
        return jsonify({"error": "missing_file", "message": "No file part in the request"}), 400

    file = request.files["file"]
    # The client may supply `file_type` as an extension (e.g. 'png' or '.png').
    # If not provided, we try to infer the extension from the uploaded filename.
    raw_file_type = request.form.get("file_type")

    extension = None
    if raw_file_type:
        # normalize: remove leading dot and lower
        extension = raw_file_type.lower().lstrip('.')

    if not extension:
        # try to infer from filename
        suffix = Path(file.filename).suffix
        if suffix:
            extension = suffix.lstrip('.').lower()

    if not extension:
        return jsonify({"error": "missing_extension", "message": "Could not determine file extension. Provide `file_type` (extension) or include an extension in the filename."}), 400

    if extension not in SUPPORTED_EXTENSIONS:
        return jsonify({
            "error": "unsupported_extension",
            "message": "Unsupported file extension",
            "supported_extensions": sorted(list(SUPPORTED_EXTENSIONS.keys()))
        }), 400

    # Map extension to category (image/video/audio)
    file_type = SUPPORTED_EXTENSIONS[extension]

    # Optional target conversion extension (e.g. 'png', 'mp3').
    convert_to_raw = request.form.get("convert_to")
    convert_to = None
    if convert_to_raw:
        convert_to = convert_to_raw.lower().lstrip('.')

    # If a conversion target is provided, validate it's supported and in same category
    if convert_to:
        if convert_to not in SUPPORTED_EXTENSIONS:
            return jsonify({
                "error": "unsupported_target_extension",
                "message": "Requested conversion target is not supported",
                "supported_extensions": sorted(list(SUPPORTED_EXTENSIONS.keys()))
            }), 400

        target_type = SUPPORTED_EXTENSIONS[convert_to]
        if target_type != file_type:
            return jsonify({
                "error": "mismatched_target_type",
                "message": "Requested target extension is not the same media type as the uploaded file",
            }), 400

    if file.filename == "":
        return jsonify({"error": "empty_filename", "message": "Uploaded file has no filename"}), 400

    filename = secure_filename(file.filename)
    dest_dir = UPLOAD_ROOT / file_type
    dest_dir.mkdir(parents=True, exist_ok=True)

    save_path = dest_dir / filename
    file.save(save_path)

    converted_info = None
    # Perform conversion if requested and different extension
    if convert_to and convert_to != extension:
        converted_filename = Path(filename).stem + "." + convert_to
        converted_path = dest_dir / converted_filename

        try:
            if file_type == "image":
                # Use Pillow for image conversion
                from PIL import Image

                with Image.open(save_path) as img:
                    # For formats like JPEG, ensure RGB
                    if convert_to in ("jpg", "jpeg") and img.mode in ("RGBA", "LA"):
                        background = Image.new("RGB", img.size, (255, 255, 255))
                        background.paste(img, mask=img.split()[3])
                        background.save(converted_path, format="JPEG")
                    else:
                        img.save(converted_path)

            elif file_type in ("audio", "video"):
                # Use ffmpeg CLI for audio/video conversion
                # Verify ffmpeg exists
                if not shutil.which("ffmpeg"):
                    return jsonify({"error": "ffmpeg_missing", "message": "ffmpeg is required for audio/video conversion but was not found on the system PATH."}), 500

                # Build ffmpeg command: input save_path -> converted_path
                # Use -y to overwrite if exists
                cmd = [
                    "ffmpeg",
                    "-y",
                    "-i",
                    str(save_path),
                    str(converted_path),
                ]

                # Run and capture output; limit runtime by timeout to avoid hangs
                proc = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=120)
                if proc.returncode != 0:
                    return jsonify({"error": "conversion_failed", "message": "ffmpeg conversion failed", "details": proc.stderr}), 500

            else:
                # For 'document' or 'other' we don't support conversion currently
                return jsonify({"error": "conversion_not_supported", "message": f"Conversion for type {file_type} is not supported."}), 400

            converted_info = {
                "converted_filename": converted_filename,
                "converted_path": str(converted_path.relative_to(Path(__file__).resolve().parent))
            }
        except Exception as e:
            return jsonify({"error": "conversion_exception", "message": str(e)}), 500

    resp = {
        "status": "success",
        "file_type": file_type,
        "filename": filename,
        "saved_path": str(save_path.relative_to(Path(__file__).resolve().parent))
    }
    if converted_info:
        resp["converted"] = converted_info

    return jsonify(resp), 201


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=5000, debug=True)


@app.route('/download', methods=['GET'])
def download_file():
    """
    Query params:
      - filename: the original filename the client uploaded (required)
      - convert_to: optional target extension (e.g. png, mp3)

    Behavior:
      - locate the file under uploads/<type>/<filename>
      - if convert_to is provided and different from original extension, perform conversion
        - images: Pillow
        - audio/video: ffmpeg (requires ffmpeg on PATH)
      - stream the file as an attachment; use a temporary file for conversions and ensure cleanup
    """
    filename = request.args.get('filename')
    if not filename:
        return jsonify({'error': 'missing_filename', 'message': 'Query parameter `filename` is required.'}), 400

    # Try to find the file in the uploads by checking each type folder
    found = None
    original_ext = None
    for category in ALLOWED_FILE_TYPES:
        candidate = UPLOAD_ROOT / category / secure_filename(filename)
        if candidate.exists():
            found = candidate
            original_ext = candidate.suffix.lstrip('.').lower()
            file_type = category
            break

    if not found:
        return jsonify({'error': 'file_not_found', 'message': 'File not found in uploads.'}), 404

    convert_to_raw = request.args.get('convert_to')
    convert_to = None
    if convert_to_raw:
        convert_to = convert_to_raw.lower().lstrip('.')

    # If conversion requested, validate
    if convert_to:
        if convert_to not in SUPPORTED_EXTENSIONS:
            return jsonify({'error': 'unsupported_target_extension', 'message': 'Requested conversion target is not supported', 'supported_extensions': sorted(list(SUPPORTED_EXTENSIONS.keys()))}), 400

        target_type = SUPPORTED_EXTENSIONS[convert_to]
        # ensure same category
        if target_type != SUPPORTED_EXTENSIONS.get(original_ext):
            return jsonify({'error': 'mismatched_target_type', 'message': 'Requested target extension is not the same media type as the stored file'}), 400

    # No conversion or same extension -> send file directly
    if not convert_to or convert_to == original_ext:
        try:
            return send_file(str(found), as_attachment=True, download_name=found.name)
        except Exception as e:
            return jsonify({'error': 'send_failed', 'message': str(e)}), 500

    # Perform conversion to a temporary file and send it
    converted_filename = Path(found.stem).name + '.' + convert_to
    try:
        if file_type == 'image':
            from PIL import Image
            with Image.open(found) as img:
                tmp = NamedTemporaryFile(delete=False, suffix='.' + convert_to)
                tmp.close()
                # Handle RGBA->JPEG
                if convert_to in ('jpg', 'jpeg') and img.mode in ('RGBA', 'LA'):
                    background = Image.new('RGB', img.size, (255, 255, 255))
                    background.paste(img, mask=img.split()[3])
                    background.save(tmp.name, format='JPEG')
                else:
                    img.save(tmp.name)

                return send_file(tmp.name, as_attachment=True, download_name=converted_filename)

        elif file_type in ('audio', 'video'):
            if not shutil.which('ffmpeg'):
                return jsonify({'error': 'ffmpeg_missing', 'message': 'ffmpeg is required for audio/video conversion but was not found on the system PATH.'}), 500

            tmp = NamedTemporaryFile(delete=False, suffix='.' + convert_to)
            tmp.close()

            cmd = [
                'ffmpeg',
                '-y',
                '-i',
                str(found),
                str(tmp.name),
            ]
            proc = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=120)
            if proc.returncode != 0:
                # cleanup
                try:
                    os.unlink(tmp.name)
                except Exception:
                    pass
                return jsonify({'error': 'conversion_failed', 'message': 'ffmpeg conversion failed', 'details': proc.stderr}), 500

            # Stream the temp file and then remove it
            try:
                response = send_file(tmp.name, as_attachment=True, download_name=converted_filename)
                return response
            finally:
                try:
                    os.unlink(tmp.name)
                except Exception:
                    pass

        else:
            return jsonify({'error': 'conversion_not_supported', 'message': f'Conversion for type {file_type} is not supported.'}), 400

    except Exception as e:
        return jsonify({'error': 'conversion_exception', 'message': str(e)}), 500
