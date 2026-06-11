"""Clipship web UI.

A read-only browsing interface for the clipped Markdown files in OUTPUT_DIR.
Lists clips with title/source/tags/date, renders individual clips with their
images, filters by tag, and does naive full-text search.

Encrypted clips show a badge and the metadata only — decryption stays in the
extension, where the passphrase lives.

Auth: HTTP Basic, credentials from config (WEB_UI_USERNAME, WEB_UI_PASSWORD).
Disabled by default (WEB_UI_ENABLED = False) so existing receiver-only
deployments are unaffected.
"""
from __future__ import annotations

import hmac
import html
import re
import time
from pathlib import Path
from urllib.parse import quote, urlparse

from flask import (
    Flask, Response, abort, redirect, request, send_from_directory, url_for,
)
from werkzeug.middleware.proxy_fix import ProxyFix

import config

WEB_UI_ENABLED = bool(getattr(config, "WEB_UI_ENABLED", False))
WEB_UI_USERNAME = str(getattr(config, "WEB_UI_USERNAME", "admin"))
WEB_UI_PASSWORD = str(getattr(config, "WEB_UI_PASSWORD", ""))
WEB_UI_HOST = str(getattr(config, "WEB_UI_HOST", "127.0.0.1"))
WEB_UI_PORT = int(getattr(config, "WEB_UI_PORT", 5051))
WEB_UI_TRUST_PROXY = bool(getattr(config, "WEB_UI_TRUST_PROXY", True))
PAGE_SIZE = int(getattr(config, "WEB_UI_PAGE_SIZE", 30))

OUTPUT_DIR = Path(config.OUTPUT_DIR).resolve()
ASSETS_SUBDIR = str(getattr(config, "ASSETS_SUBDIR", "assets"))

app = Flask(__name__)

if WEB_UI_TRUST_PROXY:
    # Honour X-Forwarded-Proto, X-Forwarded-Host, X-Forwarded-Prefix so the
    # app generates correct URLs when running behind nginx/caddy at a subpath
    # (e.g. https://example.com/ui/). Only safe when the receiver is bound to
    # 127.0.0.1 and exposed only via the proxy.
    app.wsgi_app = ProxyFix(
        app.wsgi_app, x_for=1, x_proto=1, x_host=1, x_prefix=1
    )


# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------

@app.before_request
def _require_basic_auth():
    if not WEB_UI_ENABLED:
        return Response("Web UI disabled.\n", status=503, mimetype="text/plain")
    if not WEB_UI_PASSWORD:
        return Response("Web UI password not configured.\n", status=503, mimetype="text/plain")
    auth = request.authorization
    if not auth or not auth.username or not auth.password:
        return _need_auth()
    user_ok = hmac.compare_digest(auth.username, WEB_UI_USERNAME)
    pass_ok = hmac.compare_digest(auth.password, WEB_UI_PASSWORD)
    if not (user_ok and pass_ok):
        return _need_auth()
    return None


def _need_auth():
    return Response(
        "Authentication required.\n",
        status=401,
        headers={"WWW-Authenticate": 'Basic realm="Clipship", charset="UTF-8"'},
        mimetype="text/plain",
    )


# ---------------------------------------------------------------------------
# Frontmatter parsing — intentionally minimal; we only need a few fields.
# ---------------------------------------------------------------------------

_FM_RE = re.compile(r"^---\s*\n(.*?)\n---\s*(?:\n|$)", re.DOTALL)
_KV_RE = re.compile(r'^(\w+)\s*:\s*(.*?)\s*$')
_LIST_RE = re.compile(r'^\[(.*)\]$')


def _unquote(s: str) -> str:
    s = s.strip()
    if (s.startswith('"') and s.endswith('"')) or (s.startswith("'") and s.endswith("'")):
        inner = s[1:-1]
        return inner.replace('\\"', '"').replace("\\\\", "\\")
    return s


def _parse_frontmatter(text: str) -> tuple[dict, str]:
    m = _FM_RE.match(text)
    if not m:
        return {}, text
    fm = {}
    lines = m.group(1).splitlines()
    i = 0
    while i < len(lines):
        line = lines[i].rstrip()
        i += 1
        if not line or line.startswith("#"):
            continue
        kv = _KV_RE.match(line)
        if not kv:
            continue
        key, raw = kv.group(1), kv.group(2)
        if raw == "":
            # Bare `key:` — peek ahead for a block-style YAML list
            # (Obsidian's preferred form):
            #     tags:
            #       - rag
            #       - llm-wiki
            items = []
            while i < len(lines):
                stripped = lines[i].lstrip()
                if not stripped.startswith("- "):
                    break
                items.append(_unquote(stripped[2:].strip()))
                i += 1
            if items:
                fm[key] = items
            continue
        list_m = _LIST_RE.match(raw)
        if list_m:
            inner = list_m.group(1)
            items = []
            # Split on top-level commas. Tag values are always double-quoted
            # strings in our writers, but be lenient on input.
            for part in inner.split(","):
                v = _unquote(part.strip())
                if v:
                    items.append(v)
            fm[key] = items
        elif raw.lower() in ("true", "false"):
            fm[key] = (raw.lower() == "true")
        elif raw.isdigit():
            fm[key] = int(raw)
        else:
            fm[key] = _unquote(raw)
    return fm, text[m.end():]


def _list_clips() -> list[dict]:
    out = []
    if not OUTPUT_DIR.exists():
        return out
    for path in OUTPUT_DIR.iterdir():
        if not path.is_file() or path.suffix.lower() != ".md":
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        fm, _ = _parse_frontmatter(text)
        tags = fm.get("tags") if isinstance(fm.get("tags"), list) else []
        out.append({
            "filename": path.name,
            "title": fm.get("title") or path.stem,
            "source": fm.get("source", ""),
            "site": fm.get("site", ""),
            "author": fm.get("author", ""),
            "clipped": fm.get("clipped", ""),
            "tags": tags,
            "is_pdf": fm.get("type") == "pdf",
            "mtime": path.stat().st_mtime,
        })
    # Newest first by frontmatter date if parsable, else by mtime.
    out.sort(key=lambda c: (c["clipped"] or "", c["mtime"]), reverse=True)
    return out


def _safe_subpath(filename: str) -> Path | None:
    """Return the absolute path of a markdown file inside OUTPUT_DIR or None."""
    if "/" in filename or "\\" in filename or ".." in filename or filename.startswith("."):
        return None
    p = (OUTPUT_DIR / filename).resolve()
    if OUTPUT_DIR not in p.parents:
        return None
    return p if p.exists() else None


# ---------------------------------------------------------------------------
# Templates — inline, tiny, no Jinja file overhead.
# ---------------------------------------------------------------------------

_BASE = """<!doctype html>
<html lang="en"><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{title}</title>
<style>
:root {{
  --bg: #ffffff; --fg: #1a1a1a; --muted: #6b7280; --border: #e5e7eb;
  --accent: #2563eb; --code-bg: #f3f4f6; --tag-bg: #eef2ff; --tag-fg: #4338ca;
}}
@media (prefers-color-scheme: dark) {{
  :root {{
    --bg: #0f0f10; --fg: #e7e7e9; --muted: #9ca3af; --border: #2a2a2d;
    --accent: #60a5fa; --code-bg: #18181b; --tag-bg: #1e1b4b; --tag-fg: #c7d2fe;
  }}
}}
* {{ box-sizing: border-box; }}
body {{
  background: var(--bg); color: var(--fg); margin: 0;
  font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
  font-size: 16px; line-height: 1.55;
}}
header {{
  border-bottom: 1px solid var(--border); padding: 14px 24px;
  display: flex; align-items: center; gap: 16px; flex-wrap: wrap;
}}
header h1 {{ margin: 0; font-size: 18px; font-weight: 600; }}
header h1 a {{ color: var(--fg); text-decoration: none; }}
header form {{ flex: 1; max-width: 360px; margin-left: auto; }}
header input[type="search"] {{
  width: 100%; padding: 6px 10px; font-size: 14px;
  background: var(--bg); color: var(--fg);
  border: 1px solid var(--border); border-radius: 6px;
}}
main {{ max-width: 880px; margin: 0 auto; padding: 24px; }}
a {{ color: var(--accent); }}
.muted {{ color: var(--muted); }}
.tag {{
  display: inline-block; padding: 1px 8px; margin: 0 4px 4px 0;
  background: var(--tag-bg); color: var(--tag-fg);
  border-radius: 999px; font-size: 12px; text-decoration: none;
}}
.tag:hover {{ filter: brightness(1.1); }}
.clip-row {{
  padding: 12px 0; border-bottom: 1px solid var(--border);
}}
.clip-row h2 {{ margin: 0 0 4px; font-size: 16px; font-weight: 500; }}
.clip-row h2 a {{ color: var(--fg); text-decoration: none; }}
.clip-row h2 a:hover {{ color: var(--accent); }}
.clip-meta {{ font-size: 13px; color: var(--muted); }}
.clip-meta .sep {{ margin: 0 6px; }}
.badge {{
  display: inline-block; padding: 0 6px; border-radius: 4px;
  background: var(--code-bg); color: var(--muted); font-size: 11px;
  letter-spacing: 0.5px; text-transform: uppercase; margin-left: 6px;
}}
.pagination {{ margin-top: 24px; text-align: center; }}
.pagination a {{ margin: 0 8px; }}
article img {{ max-width: 100%; height: auto; border-radius: 4px; }}
article pre {{
  background: var(--code-bg); padding: 12px 14px; border-radius: 6px;
  overflow-x: auto; font-size: 13px;
}}
article code {{ background: var(--code-bg); padding: 1px 4px; border-radius: 3px; font-size: 0.95em; }}
article pre code {{ background: transparent; padding: 0; }}
article blockquote {{
  margin: 0; padding: 4px 16px; border-left: 4px solid var(--border);
  color: var(--muted);
}}
.frontmatter {{
  margin-bottom: 24px; padding-bottom: 16px;
  border-bottom: 1px solid var(--border);
}}
.frontmatter h1 {{ margin: 0 0 8px; font-size: 24px; line-height: 1.25; }}
.empty {{ text-align: center; color: var(--muted); padding: 60px 0; }}
</style>
</head><body>
<header>
  <h1><a href="{home}">Clipship</a></h1>
  <form method="get" action="{home}"><input type="search" name="q" placeholder="Search…" value="{q}"></form>
</header>
<main>{body}</main>
</body></html>"""


def _render_page(title: str, body: str, q: str = "") -> str:
    return _BASE.format(
        title=html.escape(title),
        body=body,
        home=url_for("index"),
        q=html.escape(q or ""),
    )


def _render_tag_links(tags: list[str]) -> str:
    if not tags:
        return ""
    return "".join(
        f'<a class="tag" href="{url_for("tag_view", tag=t)}">{html.escape(t)}</a>'
        for t in tags
    )


def _render_clip_row(c: dict) -> str:
    meta_bits = []
    if c["clipped"]:
        meta_bits.append(html.escape(c["clipped"][:10]))
    if c["site"]:
        meta_bits.append(html.escape(c["site"]))
    elif c["source"]:
        host = urlparse(c["source"]).hostname or ""
        if host:
            meta_bits.append(html.escape(host))
    meta = '<span class="sep">·</span>'.join(meta_bits)
    badges = ""
    if c["is_pdf"]:
        badges += '<span class="badge">pdf</span>'
    return (
        '<div class="clip-row">'
        f'<h2><a href="{url_for("view_clip", filename=c["filename"])}">'
        f'{html.escape(c["title"])}</a>{badges}</h2>'
        f'<div class="clip-meta">{meta}</div>'
        f'<div>{_render_tag_links(c["tags"])}</div>'
        '</div>'
    )


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@app.get("/")
def index():
    clips = _list_clips()
    q = (request.args.get("q") or "").strip()
    if q:
        ql = q.lower()
        clips = [c for c in clips if _matches_query(c, ql)]
    try:
        page = max(int(request.args.get("page", "1")), 1)
    except ValueError:
        page = 1
    total = len(clips)
    start = (page - 1) * PAGE_SIZE
    page_clips = clips[start:start + PAGE_SIZE]

    if not page_clips:
        body = '<div class="empty">No clips yet.</div>' if not q else \
               f'<div class="empty">No clips match "{html.escape(q)}".</div>'
    else:
        body = "".join(_render_clip_row(c) for c in page_clips)
        if total > PAGE_SIZE:
            body += _render_pagination(page, total, q)
    return _render_page("Clipship", body, q=q)


def _matches_query(clip: dict, q: str) -> bool:
    if q in clip["title"].lower():
        return True
    if any(q in t.lower() for t in clip["tags"]):
        return True
    if q in (clip["source"] or "").lower():
        return True
    # Slow path: full-text grep.
    path = OUTPUT_DIR / clip["filename"]
    try:
        return q in path.read_text(encoding="utf-8", errors="replace").lower()
    except OSError:
        return False


def _render_pagination(page: int, total: int, q: str) -> str:
    last = (total + PAGE_SIZE - 1) // PAGE_SIZE
    bits = []
    if page > 1:
        bits.append(f'<a href="?page={page-1}{_q(q)}">← prev</a>')
    bits.append(f'<span class="muted">page {page} of {last}</span>')
    if page < last:
        bits.append(f'<a href="?page={page+1}{_q(q)}">next →</a>')
    return '<div class="pagination">' + "".join(bits) + '</div>'


def _q(q: str) -> str:
    return f"&q={quote(q)}" if q else ""


@app.get("/clip/<path:filename>")
def view_clip(filename: str):
    p = _safe_subpath(filename)
    if not p:
        abort(404)
    text = p.read_text(encoding="utf-8", errors="replace")
    fm, body_md = _parse_frontmatter(text)

    title = fm.get("title") or p.stem
    tag_html = _render_tag_links(fm.get("tags") if isinstance(fm.get("tags"), list) else [])

    meta_lines = []
    if fm.get("source"):
        meta_lines.append(
            f'<div class="muted">From <a href="{html.escape(fm["source"])}" rel="noopener">'
            f'{html.escape(fm["source"])}</a></div>'
        )
    if fm.get("author"):
        meta_lines.append(f'<div class="muted">by {html.escape(fm["author"])}</div>')
    if fm.get("clipped"):
        meta_lines.append(f'<div class="muted">clipped {html.escape(fm["clipped"])}</div>')

    head = '<div class="frontmatter">'
    head += f"<h1>{html.escape(title)}</h1>"
    head += "".join(meta_lines)
    if tag_html:
        head += f'<div style="margin-top:8px">{tag_html}</div>'
    head += '</div>'

    rendered = _render_markdown(body_md, filename)
    return _render_page(title, head + f'<article>{rendered}</article>')


def _render_markdown(text: str, source_md_name: str) -> str:
    import markdown as md_lib
    md = md_lib.Markdown(extensions=["fenced_code", "tables", "nl2br"])
    html_out = md.convert(text)
    # Rewrite image/link src/href that point at "assets/foo" so they hit our
    # asset route under the web UI URL space.
    def _rewrite(m):
        prefix, target = m.group(1), m.group(2)
        if target.startswith(("http://", "https://", "data:", "/")):
            return m.group(0)
        if target.startswith(f"{ASSETS_SUBDIR}/"):
            new = url_for("serve_asset", subpath=target[len(ASSETS_SUBDIR) + 1:])
            return f'{prefix}"{new}"'
        return m.group(0)
    return re.sub(r'(src=|href=)"([^"]+)"', _rewrite, html_out)


@app.get("/tag/<tag>")
def tag_view(tag: str):
    clips = [c for c in _list_clips() if tag.lower() in (t.lower() for t in c["tags"])]
    if not clips:
        return _render_page(f"tag: {tag}", f'<div class="empty">No clips tagged "{html.escape(tag)}".</div>')
    body = f'<h2 style="margin:0 0 16px;font-size:20px">Tag: {html.escape(tag)}</h2>'
    body += "".join(_render_clip_row(c) for c in clips)
    return _render_page(f"tag: {tag}", body)


@app.get("/assets/<path:subpath>")
def serve_asset(subpath: str):
    assets_root = (OUTPUT_DIR / ASSETS_SUBDIR).resolve()
    if not assets_root.exists():
        abort(404)
    # send_from_directory rejects traversal natively, but be explicit.
    if ".." in subpath or subpath.startswith("/"):
        abort(404)
    return send_from_directory(assets_root, subpath)


@app.get("/favicon.ico")
def favicon():
    return Response(status=204)


if __name__ == "__main__":
    app.run(host=WEB_UI_HOST, port=WEB_UI_PORT)
