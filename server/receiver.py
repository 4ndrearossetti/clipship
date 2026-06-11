"""Clipship receiver: verifies an HMAC-signed JSON payload and writes a Markdown file.

When DOWNLOAD_ASSETS is enabled, also fetches every image referenced in the
markdown, stores it under ASSETS_SUBDIR, and rewrites the markdown to point at
the local copy. Asset downloads use a custom opener with SSRF guards (no
private/loopback IPs, scheme allowlist, per-asset size and timeout caps).
"""
from __future__ import annotations

import hmac
import hashlib
import io
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

MAX_BODY_BYTES = int(getattr(config, "MAX_BODY_BYTES", 10 * 1024 * 1024))

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = MAX_BODY_BYTES

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

# PDF clipping
DOWNLOAD_PDFS = bool(getattr(config, "DOWNLOAD_PDFS", True))
MAX_PDF_BYTES = int(getattr(config, "MAX_PDF_BYTES", 100 * 1024 * 1024))
PDF_TIMEOUT = float(getattr(config, "PDF_TIMEOUT", 30))
EXTRACT_PDF_TEXT = bool(getattr(config, "EXTRACT_PDF_TEXT", True))

# Which backend to use for PDF text extraction. One of:
#   "pypdf"          — pure-Python, fast, plain text (default).
#   "opendataloader" — Java-backed; richer Markdown with tables/structure.
#                      Requires Java 11+ and the opendataloader-pdf package.
#   "none"           — store + link the PDF only, no text in the markdown body.
# Legacy: EXTRACT_PDF_TEXT = False forces "none" regardless of PDF_EXTRACTOR.
PDF_EXTRACTOR = str(getattr(config, "PDF_EXTRACTOR", "pypdf")).lower().strip()
if PDF_EXTRACTOR not in ("pypdf", "opendataloader", "none"):
    PDF_EXTRACTOR = "pypdf"
if not EXTRACT_PDF_TEXT:
    PDF_EXTRACTOR = "none"


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


def _fetch_url(url: str, max_bytes: int, timeout: float, accept: str) -> tuple[bytes, str] | None:
    """Generic SSRF-guarded URL fetcher. Returns (bytes, content_type) or None."""
    data, ct, _err = _fetch_url_verbose(url, max_bytes, timeout, accept)
    if data is None:
        return None
    return data, ct


def _fetch_url_verbose(url: str, max_bytes: int, timeout: float, accept: str) -> tuple[bytes | None, str, str]:
    """Like _fetch_url, but also returns a short human-readable failure reason."""
    if not _url_is_safe(url):
        return None, "", "URL refused by SSRF guard (private IP, non-http scheme, or DNS failure)"
    req = urllib.request.Request(url, headers={
        "User-Agent": ASSET_USER_AGENT,
        "Accept": accept,
    })
    try:
        with _opener.open(req, timeout=timeout) as resp:
            ct = resp.headers.get("Content-Type", "")
            clen = resp.headers.get("Content-Length")
            if clen:
                try:
                    if int(clen) > max_bytes:
                        return None, ct, f"Content-Length {clen} exceeds {max_bytes}-byte cap"
                except ValueError:
                    pass
            data = resp.read(max_bytes + 1)
            if len(data) > max_bytes:
                return None, ct, f"body exceeded {max_bytes}-byte cap"
    except urllib.error.HTTPError as e:
        return None, "", f"upstream HTTP {e.code}"
    except urllib.error.URLError as e:
        return None, "", f"network: {e.reason}"
    except (OSError, ValueError) as e:
        return None, "", f"fetch error: {e}"
    return data, ct, ""


def _fetch_asset(url: str) -> tuple[bytes, str] | None:
    """Fetch a single asset. Returns (bytes, ext) or None."""
    res = _fetch_url(url, MAX_ASSET_BYTES, ASSET_TIMEOUT, "image/*,video/*,*/*;q=0.8")
    if not res:
        return None
    data, ct = res
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

# ---------------------------------------------------------------------------
# PDF handler
# ---------------------------------------------------------------------------

def _yaml_escape(s: str) -> str:
    return (s or "").replace("\\", "\\\\").replace('"', '\\"')


def _build_pdf_frontmatter(meta: dict, pdf_relpath: str) -> str:
    lines = ["---"]
    lines.append(f'title: "{_yaml_escape(meta.get("title", ""))}"')
    lines.append(f'source: "{_yaml_escape(meta.get("pdf_url", ""))}"')
    lines.append(f'clipped: "{_yaml_escape(meta.get("clipped", ""))}"')
    lines.append('type: "pdf"')
    lines.append(f'pdf: "{_yaml_escape(pdf_relpath)}"')
    tags = meta.get("tags") or []
    if isinstance(tags, list) and tags:
        # Block-style YAML list — matches what Obsidian writes when you edit
        # Properties, so manually-edited and clipship-written notes look
        # identical. The web UI parser accepts both forms.
        lines.append("tags:")
        for t in tags:
            lines.append(f'  - "{_yaml_escape(str(t))}"')
    lines.append("---")
    return "\n".join(lines)


def _iso_now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _handle_pdf(target: Path, payload: dict) -> tuple[str, dict]:
    """Download a PDF, save under assets/, render a stub .md, return (content, extras)."""
    pdf_url = payload.get("pdf_url", "")
    if not isinstance(pdf_url, str) or not pdf_url:
        raise ValueError("missing pdf_url")
    if not DOWNLOAD_PDFS:
        raise ValueError("PDF downloads are disabled on this server")

    data, ct, err = _fetch_url_verbose(
        pdf_url, MAX_PDF_BYTES, PDF_TIMEOUT, "application/pdf,*/*;q=0.1"
    )
    if data is None:
        raise ValueError(f"could not fetch PDF: {err}")
    # Light content-type sanity check — some servers omit it, so we don't
    # hard-fail when missing, but a clearly-non-PDF response is rejected.
    ct_lc = (ct or "").lower().split(";", 1)[0].strip()
    if ct_lc and not (ct_lc == "application/pdf" or ct_lc.endswith("/pdf")
                      or ct_lc == "application/octet-stream"):
        # Also accept a PDF magic-number prefix in case the server lied.
        if not data.startswith(b"%PDF-"):
            raise ValueError(f"upstream returned non-PDF content ({ct_lc or 'unknown type'})")

    assets_root = OUTPUT_DIR / ASSETS_SUBDIR
    assets_root.mkdir(parents=True, exist_ok=True)
    pdf_name = f"{target.stem}.pdf"
    (assets_root / pdf_name).write_bytes(data)
    pdf_relpath = f"{ASSETS_SUBDIR}/{pdf_name}"

    meta = {
        "title": payload.get("title") or target.stem,
        "pdf_url": pdf_url,
        "clipped": _iso_now(),
        "tags": payload.get("tags") or [],
    }
    body_parts = [_build_pdf_frontmatter(meta, pdf_relpath), ""]
    body_parts.append(f"[Open PDF]({pdf_relpath})")
    body_parts.append("")

    text = _extract_pdf_text(data, pdf_name)

    if text:
        body_parts.append("---")
        body_parts.append("")
        body_parts.append(text)
        body_parts.append("")

    return "\n".join(body_parts), {"pdf": pdf_name, "pdf_bytes": len(data)}


def _extract_pdf_text(data: bytes, pdf_name: str) -> str:
    """Run the configured PDF extractor; return body text or '' on any failure."""
    if PDF_EXTRACTOR == "none":
        return ""
    if PDF_EXTRACTOR == "opendataloader":
        return _extract_with_opendataloader(data, pdf_name)
    return _extract_with_pypdf(data)


def _extract_with_pypdf(data: bytes) -> str:
    try:
        import pypdf  # optional dependency
        reader = pypdf.PdfReader(io.BytesIO(data))
        chunks = []
        for page in reader.pages:
            try:
                chunks.append(page.extract_text() or "")
            except Exception:
                continue
        return "\n\n".join(c.strip() for c in chunks if c and c.strip())
    except ImportError:
        return ""
    except Exception:
        return ""


def _extract_with_opendataloader(data: bytes, pdf_name: str) -> str:
    """Drive the opendataloader-pdf Python package via a temp scratch dir.

    The package wraps a Java tool; it writes a Markdown file next to the input.
    We give it a fresh temp directory each call so we can find the result by
    extension without parsing its naming convention.
    """
    try:
        import tempfile
        import opendataloader_pdf  # optional dependency
    except ImportError:
        return ""

    try:
        with tempfile.TemporaryDirectory(prefix="clipship-odl-") as tmp:
            tmp_path = Path(tmp)
            in_path = tmp_path / pdf_name
            in_path.write_bytes(data)
            out_dir = tmp_path / "out"
            out_dir.mkdir()
            opendataloader_pdf.convert(
                input_path=[str(in_path)],
                output_dir=str(out_dir),
                format="markdown",
            )
            md_files = sorted(out_dir.rglob("*.md"))
            if not md_files:
                return ""
            return md_files[0].read_text(encoding="utf-8", errors="replace").strip()
    except Exception:
        return ""


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
    if not filename:
        return _err(400, "invalid filename")

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    target = _unique_path(OUTPUT_DIR, filename)
    if OUTPUT_DIR not in target.resolve().parents and target.resolve() != OUTPUT_DIR:
        return _err(400, "path traversal detected")

    # Branch on payload shape: pdf or plain markdown.
    is_pdf = bool(payload.get("pdf_url"))

    assets_downloaded = 0
    assets_failed = 0
    extra: dict = {}

    if is_pdf:
        try:
            content, extra = _handle_pdf(target, payload)
        except ValueError as e:
            return _err(400, str(e))
    else:
        content = payload.get("content")
        if not isinstance(content, str) or not content:
            return _err(400, "invalid content")
        if DOWNLOAD_ASSETS:
            content, assets_downloaded, assets_failed = _localize_assets(target.stem, content)

    target.write_text(content, encoding="utf-8")
    response = {
        "status": "ok",
        "file": target.name,
        "assets_downloaded": assets_downloaded,
        "assets_failed": assets_failed,
    }
    response.update(extra)
    if is_pdf:
        response["pdf"] = True
    return jsonify(response)


@app.errorhandler(413)
def _too_large(_e):
    return _err(413, "payload too large")


@app.get("/health")
def health():
    return jsonify({"status": "ok"})


if __name__ == "__main__":
    app.run(host=config.HOST, port=config.PORT)
