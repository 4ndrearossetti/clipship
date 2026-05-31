"""Clipship receiver: verifies an HMAC-signed JSON payload and writes a Markdown file.

When DOWNLOAD_ASSETS is enabled, also fetches every image referenced in the
markdown, stores it under ASSETS_SUBDIR, and rewrites the markdown to point at
the local copy. Asset downloads use a custom opener with SSRF guards (no
private/loopback IPs, scheme allowlist, per-asset size and timeout caps).
"""
from __future__ import annotations

import hmac
import hashlib
import ipaddress
import json
import os
import re
import socket
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from flask import Flask, request, jsonify

import config

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = getattr(config, "MAX_BODY_BYTES", 10 * 1024 * 1024)

OUTPUT_DIR = Path(config.OUTPUT_DIR).resolve()
MAX_SKEW = int(getattr(config, "MAX_CLOCK_SKEW", 300))
SECRET = config.SECRET_KEY.encode("utf-8")

DOWNLOAD_ASSETS = bool(getattr(config, "DOWNLOAD_ASSETS", True))
ASSETS_SUBDIR = str(getattr(config, "ASSETS_SUBDIR", "assets"))
MAX_ASSET_BYTES = int(getattr(config, "MAX_ASSET_BYTES", 25 * 1024 * 1024))
MAX_ASSETS_PER_CLIP = int(getattr(config, "MAX_ASSETS_PER_CLIP", 100))
ASSET_TIMEOUT = float(getattr(config, "ASSET_TIMEOUT", 10))
ASSET_USER_AGENT = str(getattr(
    config,
    "ASSET_USER_AGENT",
    "Clipship/1.0 (+https://github.com/4ndrearossetti/clipship)",
))


def _err(status: int, message: str):
    return jsonify({"status": "error", "error": message}), status


# ---------------------------------------------------------------------------
# Filename hygiene
# ---------------------------------------------------------------------------

def _sanitize_filename(raw: str) -> str | None:
    """Reduce an untrusted filename to a safe basename ending in .md."""
    if not isinstance(raw, str) or not raw:
        return None
    name = os.path.basename(raw).strip()
    if not name or name in (".", ".."):
        return None
    if ".." in name or "/" in name or "\\" in name or "\x00" in name:
        return None
    name = re.sub(r"[^A-Za-z0-9._-]", "-", name)
    name = name.strip(".-") or "clip"
    if not name.lower().endswith(".md"):
        name += ".md"
    if len(name) > 200:
        stem, _, ext = name.rpartition(".")
        name = stem[: 200 - len(ext) - 1] + "." + ext
    return name


def _unique_path(directory: Path, filename: str) -> Path:
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


# ---------------------------------------------------------------------------
# Asset downloading (SSRF-hardened)
# ---------------------------------------------------------------------------

_PRIVATE_NETS = [
    ipaddress.ip_network(n) for n in (
        "0.0.0.0/8", "10.0.0.0/8", "100.64.0.0/10", "127.0.0.0/8",
        "169.254.0.0/16", "172.16.0.0/12", "192.0.0.0/24", "192.168.0.0/16",
        "198.18.0.0/15", "224.0.0.0/4", "240.0.0.0/4",
        "::1/128", "fc00::/7", "fe80::/10", "ff00::/8",
    )
]

# image/jpeg → jpg, video/mp4 → mp4, etc.
_CT_EXT = {
    "image/jpeg": "jpg", "image/jpg": "jpg", "image/png": "png",
    "image/gif": "gif", "image/webp": "webp", "image/avif": "avif",
    "image/svg+xml": "svg", "image/bmp": "bmp", "image/tiff": "tiff",
    "image/x-icon": "ico", "video/mp4": "mp4", "video/webm": "webm",
    "video/ogg": "ogv",
}

# Markdown image syntax. We deliberately don't try to handle every edge case
# (e.g. images inside link text) — Readability output is fairly regular.
_IMG_RE = re.compile(r'(!\[(?P<alt>[^\]]*)\])\((?P<url>[^)\s]+)(?P<title>\s+"[^"]*")?\)')


def _host_is_safe(host: str) -> bool:
    try:
        infos = socket.getaddrinfo(host, None)
    except OSError:
        return False
    if not infos:
        return False
    for info in infos:
        addr = info[4][0]
        try:
            ip = ipaddress.ip_address(addr)
        except ValueError:
            return False
        for net in _PRIVATE_NETS:
            if ip in net:
                return False
    return True


def _url_is_safe(url: str) -> bool:
    try:
        u = urllib.parse.urlparse(url)
    except ValueError:
        return False
    if u.scheme not in ("http", "https"):
        return False
    if not u.hostname:
        return False
    # Reject hosts that parse as literal private IPs without DNS.
    try:
        ip = ipaddress.ip_address(u.hostname)
        for net in _PRIVATE_NETS:
            if ip in net:
                return False
    except ValueError:
        pass  # not an IP literal — DNS resolution handles it below
    return _host_is_safe(u.hostname)


class _SafeRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if not _url_is_safe(newurl):
            raise urllib.error.URLError(f"unsafe redirect target: {newurl}")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


_opener = urllib.request.build_opener(_SafeRedirectHandler())


def _ext_for(content_type: str, url: str) -> str:
    ct = (content_type or "").split(";", 1)[0].strip().lower()
    if ct in _CT_EXT:
        return _CT_EXT[ct]
    # Fall back to the URL path extension if it looks like a real one.
    path = urllib.parse.urlparse(url).path
    _, _, tail = path.rpartition(".")
    if tail and 1 <= len(tail) <= 5 and tail.isalnum():
        return tail.lower()
    return "bin"


def _fetch_asset(url: str) -> tuple[bytes, str] | None:
    """Fetch a single asset with SSRF + size + timeout guards. Returns (bytes, ext) or None."""
    if not _url_is_safe(url):
        return None
    req = urllib.request.Request(url, headers={
        "User-Agent": ASSET_USER_AGENT,
        "Accept": "image/*,video/*,*/*;q=0.8",
    })
    try:
        with _opener.open(req, timeout=ASSET_TIMEOUT) as resp:
            ct = resp.headers.get("Content-Type", "")
            clen = resp.headers.get("Content-Length")
            if clen:
                try:
                    if int(clen) > MAX_ASSET_BYTES:
                        return None
                except ValueError:
                    pass
            data = resp.read(MAX_ASSET_BYTES + 1)
            if len(data) > MAX_ASSET_BYTES:
                return None
    except (urllib.error.URLError, OSError, ValueError):
        return None
    return data, _ext_for(ct, url)


def _localize_assets(md_stem: str, content: str) -> tuple[str, int, int]:
    """Download referenced images, store under ASSETS_SUBDIR, rewrite the markdown.

    Returns (rewritten_content, n_downloaded, n_failed).
    """
    matches = list(_IMG_RE.finditer(content))
    if not matches:
        return content, 0, 0

    assets_root = OUTPUT_DIR / ASSETS_SUBDIR
    assets_root.mkdir(parents=True, exist_ok=True)
    # Defense in depth: confirm assets_root stays under OUTPUT_DIR.
    if OUTPUT_DIR not in assets_root.resolve().parents and assets_root.resolve() != OUTPUT_DIR:
        return content, 0, 0

    url_to_local: dict[str, str] = {}
    counter = 0
    failed = 0
    seen: set[str] = set()

    for m in matches:
        url = m.group("url")
        if url in seen:
            continue
        seen.add(url)
        # Already a relative/local reference — skip.
        if url.startswith(("data:", "/", "./", "../")) or not urllib.parse.urlparse(url).scheme:
            continue
        if counter >= MAX_ASSETS_PER_CLIP:
            failed += 1
            continue

        result = _fetch_asset(url)
        if not result:
            failed += 1
            continue
        data, ext = result
        counter += 1
        local_name = f"{md_stem}-img{counter}.{ext}"
        try:
            (assets_root / local_name).write_bytes(data)
        except OSError:
            failed += 1
            continue
        url_to_local[url] = f"{ASSETS_SUBDIR}/{local_name}"

    if not url_to_local:
        return content, 0, failed

    def _repl(m: re.Match[str]) -> str:
        url = m.group("url")
        if url in url_to_local:
            return f'{m.group(1)}({url_to_local[url]})'
        return m.group(0)

    return _IMG_RE.sub(_repl, content), len(url_to_local), failed


# ---------------------------------------------------------------------------
# HTTP handlers
# ---------------------------------------------------------------------------

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

    raw_body = request.get_data(cache=False)
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
    if OUTPUT_DIR not in target.resolve().parents and target.resolve() != OUTPUT_DIR:
        return _err(400, "path traversal detected")

    assets_downloaded = 0
    assets_failed = 0
    if DOWNLOAD_ASSETS:
        content, assets_downloaded, assets_failed = _localize_assets(target.stem, content)

    target.write_text(content, encoding="utf-8")
    return jsonify({
        "status": "ok",
        "file": target.name,
        "assets_downloaded": assets_downloaded,
        "assets_failed": assets_failed,
    })


@app.errorhandler(413)
def _too_large(_e):
    return _err(413, "payload too large")


@app.get("/health")
def health():
    return jsonify({"status": "ok"})


if __name__ == "__main__":
    app.run(host=config.HOST, port=config.PORT)
