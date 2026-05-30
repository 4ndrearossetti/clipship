"""Clipship receiver: verifies an HMAC-signed JSON payload and writes a Markdown file."""
from __future__ import annotations

import hmac
import hashlib
import json
import os
import re
import time
from pathlib import Path

from flask import Flask, request, jsonify, abort

import config

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = getattr(config, "MAX_BODY_BYTES", 10 * 1024 * 1024)

OUTPUT_DIR = Path(config.OUTPUT_DIR).resolve()
MAX_SKEW = int(getattr(config, "MAX_CLOCK_SKEW", 300))
SECRET = config.SECRET_KEY.encode("utf-8")


def _err(status: int, message: str):
    return jsonify({"status": "error", "error": message}), status


def _sanitize_filename(raw: str) -> str | None:
    """Reduce an untrusted filename to a safe basename ending in .md."""
    if not isinstance(raw, str) or not raw:
        return None
    # Take only the basename — strips any path traversal.
    name = os.path.basename(raw).strip()
    if not name or name in (".", ".."):
        return None
    # Reject anything that still looks like traversal or hidden control chars.
    if ".." in name or "/" in name or "\\" in name or "\x00" in name:
        return None
    # Restrict to a conservative charset.
    name = re.sub(r"[^A-Za-z0-9._-]", "-", name)
    name = name.strip(".-") or "clip"
    if not name.lower().endswith(".md"):
        name += ".md"
    # Length cap.
    if len(name) > 200:
        stem, _, ext = name.rpartition(".")
        name = stem[: 200 - len(ext) - 1] + "." + ext
    return name


def _unique_path(directory: Path, filename: str) -> Path:
    """If `filename` already exists in `directory`, append -1, -2, … to the stem."""
    candidate = directory / filename
    if not candidate.exists():
        return candidate
    stem = candidate.stem
    suffix = candidate.suffix
    i = 1
    while True:
        candidate = directory / f"{stem}-{i}{suffix}"
        if not candidate.exists():
            return candidate
        i += 1


@app.post("/clip")
def clip():
    timestamp = request.headers.get("X-Clipship-Timestamp", "")
    signature = request.headers.get("X-Clipship-Signature", "")
    if not timestamp or not signature:
        return _err(400, "missing signature headers")

    try:
        ts_int = int(timestamp)
    except ValueError:
        return _err(400, "invalid timestamp")
    if abs(time.time() - ts_int) > MAX_SKEW:
        return _err(403, "timestamp outside accepted window")

    raw_body = request.get_data(cache=False)  # exact bytes the client signed
    message = (timestamp + ".").encode("utf-8") + raw_body
    expected = hmac.new(SECRET, message, hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, signature.lower()):
        return _err(403, "invalid signature")

    try:
        payload = json.loads(raw_body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return _err(400, "invalid JSON body")

    filename = _sanitize_filename(payload.get("filename"))
    content = payload.get("content")
    if not filename:
        return _err(400, "invalid filename")
    if not isinstance(content, str) or not content:
        return _err(400, "invalid content")

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    target = _unique_path(OUTPUT_DIR, filename)
    # Defense in depth: ensure resolved path is still inside OUTPUT_DIR.
    if OUTPUT_DIR not in target.resolve().parents and target.resolve() != OUTPUT_DIR:
        return _err(400, "path traversal detected")

    target.write_text(content, encoding="utf-8")
    return jsonify({"status": "ok", "file": target.name})


@app.errorhandler(413)
def _too_large(_e):
    return _err(413, "payload too large")


@app.get("/health")
def health():
    return jsonify({"status": "ok"})


if __name__ == "__main__":
    app.run(host=config.HOST, port=config.PORT)
