"""Flask web application for automatic portrait retouching."""

import os
import uuid
import time

import cv2
import numpy as np
from flask import Flask, render_template, request, jsonify, send_from_directory
from werkzeug.utils import secure_filename

from retouch import RetouchPipeline

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 50 * 1024 * 1024  # 50 MB max
app.config["UPLOAD_FOLDER"] = os.path.join(os.path.dirname(__file__), "static", "uploads")
app.config["RESULT_FOLDER"] = os.path.join(os.path.dirname(__file__), "static", "results")

ALLOWED_EXTENSIONS = {"png", "jpg", "jpeg", "tiff", "bmp", "webp"}

os.makedirs(app.config["UPLOAD_FOLDER"], exist_ok=True)
os.makedirs(app.config["RESULT_FOLDER"], exist_ok=True)


def allowed_file(filename: str) -> bool:
    return "." in filename and filename.rsplit(".", 1)[1].lower() in ALLOWED_EXTENSIONS


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/upload", methods=["POST"])
def upload():
    if "photo" not in request.files:
        return jsonify({"error": "No file uploaded"}), 400

    file = request.files["photo"]
    if file.filename == "":
        return jsonify({"error": "No file selected"}), 400

    if not allowed_file(file.filename):
        return jsonify({"error": f"Invalid file type. Allowed: {', '.join(ALLOWED_EXTENSIONS)}"}), 400

    ext = file.filename.rsplit(".", 1)[1].lower()
    filename = f"{uuid.uuid4().hex}.{ext}"
    filepath = os.path.join(app.config["UPLOAD_FOLDER"], filename)
    file.save(filepath)

    return jsonify({
        "success": True,
        "filename": filename,
        "url": f"/static/uploads/{filename}",
    })


@app.route("/retouch", methods=["POST"])
def retouch():
    data = request.get_json()
    if not data or "filename" not in data:
        return jsonify({"error": "No filename provided"}), 400

    filename = secure_filename(data["filename"])
    filepath = os.path.join(app.config["UPLOAD_FOLDER"], filename)

    if not os.path.exists(filepath):
        return jsonify({"error": "File not found"}), 404

    settings = data.get("settings", {})

    image = cv2.imread(filepath, cv2.IMREAD_COLOR)
    if image is None:
        return jsonify({"error": "Could not read image"}), 400

    pipeline = RetouchPipeline(
        skin_smoothing=float(settings.get("skin_smoothing", 0.5)),
        blemish_removal=float(settings.get("blemish_removal", 0.5)),
        skin_tone=float(settings.get("skin_tone", 0.3)),
        background_clean=float(settings.get("background_clean", 0.5)),
        clothing_lint=float(settings.get("clothing_lint", 0.5)),
        clothing_wrinkles=float(settings.get("clothing_wrinkles", 0.3)),
        clothing_sharpness=float(settings.get("clothing_sharpness", 0.3)),
        contrast=float(settings.get("contrast", 0.3)),
        color_balance=float(settings.get("color_balance", 0.2)),
        exposure=float(settings.get("exposure", 0.2)),
        sharpness=float(settings.get("sharpness", 0.3)),
        eye_enhance=float(settings.get("eye_enhance", 0.3)),
        studio_polish=float(settings.get("studio_polish", 0.2)),
    )

    start_time = time.time()
    result = pipeline.process(image)
    processing_time = time.time() - start_time

    result_filename = f"retouched_{filename.rsplit('.', 1)[0]}.jpg"
    result_path = os.path.join(app.config["RESULT_FOLDER"], result_filename)
    cv2.imwrite(result_path, result, [cv2.IMWRITE_JPEG_QUALITY, 98])

    return jsonify({
        "success": True,
        "url": f"/static/results/{result_filename}",
        "filename": result_filename,
        "processing_time": round(processing_time, 2),
    })


@app.route("/download/<filename>")
def download(filename):
    filename = secure_filename(filename)
    return send_from_directory(
        app.config["RESULT_FOLDER"],
        filename,
        as_attachment=True,
    )


if __name__ == "__main__":
    app.run(debug=True, host="0.0.0.0", port=5000)
