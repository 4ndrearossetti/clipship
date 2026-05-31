#!/usr/bin/env python3
"""Bulk-import URLs as Clipship Markdown files.

Reads URLs from a file (or stdin), fetches each via the server's existing
SSRF-guarded asset fetcher, runs readability-lxml to extract the article body,
converts it to Markdown with html2text, and writes the result directly into
OUTPUT_DIR — bypassing the HTTP receiver since this runs locally on the
server.

Usage:
    python bulk_import.py urls.txt
    cat urls.txt | python bulk_import.py -
    python bulk_import.py urls.txt --tags reading-list,2026

Options:
    --tags TAG1,TAG2    Add these tags to every imported clip.
    --dry-run           Don't write files; just print what would be written.
    --jobs N            Parallel workers (default 4).
"""
from __future__ import annotations

import argparse
import concurrent.futures
import re
import sys
import time
from pathlib import Path

# The receiver module owns the SSRF-safe URL fetcher and the asset
# localiser. Reuse them so bulk imports get the same protections as
# extension-driven clips.
import receiver  # noqa: E402  (intentional: shares config + helpers)

OUTPUT_DIR = receiver.OUTPUT_DIR


def _slugify(s: str, maxlen: int = 60) -> str:
    s = (s or "untitled").strip().lower()
    s = re.sub(r"[^a-z0-9]+", "-", s).strip("-")
    return (s[:maxlen] or "untitled").strip("-") or "untitled"


def _iso_filename(t: float) -> str:
    return time.strftime("%Y-%m-%dT%H-%M-%SZ", time.gmtime(t))


def _yaml_escape(s: str) -> str:
    return (s or "").replace("\\", "\\\\").replace('"', '\\"')


def _build_frontmatter(meta: dict) -> str:
    lines = ["---"]
    lines.append(f'title: "{_yaml_escape(meta.get("title", ""))}"')
    lines.append(f'source: "{_yaml_escape(meta.get("url", ""))}"')
    if meta.get("byline"):
        lines.append(f'author: "{_yaml_escape(meta["byline"])}"')
    if meta.get("siteName"):
        lines.append(f'site: "{_yaml_escape(meta["siteName"])}"')
    lines.append(f'clipped: "{meta.get("clipped", "")}"')
    tags = meta.get("tags") or []
    if tags:
        inner = ", ".join('"' + _yaml_escape(str(t)) + '"' for t in tags)
        lines.append(f"tags: [{inner}]")
    lines.append("---")
    return "\n".join(lines)


def _extract(html: str, url: str) -> tuple[str, str, str]:
    """Return (title, content_html, byline). Requires readability-lxml."""
    from readability import Document
    doc = Document(html)
    return doc.short_title() or url, doc.summary(html_partial=True), ""


def _html_to_markdown(html: str) -> str:
    import html2text
    h = html2text.HTML2Text()
    h.body_width = 0
    h.ignore_emphasis = False
    h.ignore_links = False
    h.ignore_images = False
    return h.handle(html).strip()


def _site_from(url: str) -> str:
    from urllib.parse import urlparse
    host = urlparse(url).hostname or ""
    return host[4:] if host.startswith("www.") else host


def _import_one(url: str, tags: list[str], dry_run: bool) -> tuple[str, str]:
    """Returns (filename_or_message, status)."""
    res = receiver._fetch_url(
        url,
        max_bytes=receiver.MAX_BODY_BYTES,
        timeout=receiver.ASSET_TIMEOUT,
        accept="text/html,*/*;q=0.5",
    )
    if not res:
        return f"{url}: fetch failed (SSRF block, size cap, or timeout)", "FAIL"
    data, _ct = res
    try:
        html = data.decode("utf-8", errors="replace")
    except Exception:
        return f"{url}: could not decode body as text", "FAIL"

    try:
        title, content_html, byline = _extract(html, url)
    except Exception as e:
        return f"{url}: extraction failed ({e})", "FAIL"
    if not content_html.strip():
        return f"{url}: extractor returned empty content", "FAIL"

    body = _html_to_markdown(content_html)
    now = time.time()
    meta = {
        "title": title,
        "url": url,
        "byline": byline,
        "siteName": _site_from(url),
        "clipped": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now)),
        "tags": tags,
    }
    md_content = _build_frontmatter(meta) + "\n\n" + body + "\n"
    filename = f"{_iso_filename(now)}-{_slugify(title)}.md"
    if dry_run:
        return filename, "DRY"

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    target = receiver._unique_path(OUTPUT_DIR, filename)
    if receiver.DOWNLOAD_ASSETS:
        md_content, _, _ = receiver._localize_assets(target.stem, md_content)
    target.write_text(md_content, encoding="utf-8")
    return target.name, "OK"


def _check_deps() -> list[str]:
    missing = []
    try:
        import readability  # noqa: F401
    except ImportError:
        missing.append("readability-lxml")
    try:
        import html2text  # noqa: F401
    except ImportError:
        missing.append("html2text")
    return missing


def main(argv: list[str]) -> int:
    p = argparse.ArgumentParser(description="Bulk-import URLs into Clipship.")
    p.add_argument("urls_file", help="File of URLs, one per line. Use '-' for stdin.")
    p.add_argument("--tags", default="", help="Comma-separated tags applied to every clip.")
    p.add_argument("--dry-run", action="store_true", help="Don't write files.")
    p.add_argument("--jobs", type=int, default=4, help="Parallel fetches (default 4).")
    args = p.parse_args(argv)

    missing = _check_deps()
    if missing:
        print(
            f"Missing dependencies: {', '.join(missing)}\n"
            f"Install with: pip install {' '.join(missing)}",
            file=sys.stderr,
        )
        return 2

    if args.urls_file == "-":
        raw = sys.stdin.read().splitlines()
    else:
        raw = Path(args.urls_file).read_text(encoding="utf-8").splitlines()
    urls = [u.strip() for u in raw if u.strip() and not u.strip().startswith("#")]
    if not urls:
        print("No URLs to import.", file=sys.stderr)
        return 1

    tags = [t.strip() for t in args.tags.split(",") if t.strip()]
    print(f"Importing {len(urls)} URLs with {args.jobs} parallel workers…", file=sys.stderr)

    ok = fail = 0
    with concurrent.futures.ThreadPoolExecutor(max_workers=args.jobs) as pool:
        futures = {pool.submit(_import_one, u, tags, args.dry_run): u for u in urls}
        for fut in concurrent.futures.as_completed(futures):
            url = futures[fut]
            try:
                msg, status = fut.result()
            except Exception as e:
                msg, status = f"{url}: {e}", "FAIL"
            print(f"[{status}] {msg}")
            if status in ("OK", "DRY"):
                ok += 1
            else:
                fail += 1
    print(f"\nDone: {ok} succeeded, {fail} failed.", file=sys.stderr)
    return 0 if fail == 0 else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
