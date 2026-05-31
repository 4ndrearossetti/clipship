# Clipship

> A minimal, self-hosted web clipper. Captures clean article content from any page and delivers it as Markdown to a folder on your own server — no cloud, no account, no vendor.

## Features

- **Clean Markdown clips** — Mozilla's Readability + a small HTML-to-Markdown
  converter, with YAML frontmatter (title, source, author, site, date, tags).
- **Tags** — type comma-separated tags in the popup, or let Clipship auto-extract
  them from the page's `meta[name=keywords]`, `meta[property=article:tag]` and
  `<a rel="tag">` elements. Both are merged and deduped.
- **Self-contained clips** — the server downloads every image referenced in the
  Markdown into `assets/`, with SSRF guards, size and timeout caps. No more
  broken images when viewing offline or in privacy-preserving renderers.
- **PDF clipping** — click the icon on a `.pdf` tab and the server downloads
  the file, stores it next to the Markdown, and extracts the text body (with
  `pypdf`).
- **Bulk URL import** — `server/bulk_import.py` ingests a file of URLs in
  parallel, runs the same extractor server-side, writes straight to the inbox.
- **Web UI** — optional Flask read-only browser: list, search, filter by tag,
  view rendered Markdown with images. HTTP-Basic-auth-gated, off by default.
- **Optional end-to-end encryption** — AES-GCM-256, passphrase-derived via
  PBKDF2-SHA256 (600 000 iterations). The server stores ciphertext and cannot
  read encrypted clips.
- **Signed payloads** — HMAC-SHA256 over `timestamp + "." + body`, ±5 minute
  replay window, constant-time signature comparison server-side.
- **No third parties** — the extension talks only to your endpoint; the server
  only fetches images and PDFs you've referenced. No analytics, no telemetry.

---

## Quickstart

```bash
# Server
git clone https://github.com/4ndrearossetti/clipship.git
cd clipship/server
python3 -m venv venv && ./venv/bin/pip install -r requirements.txt
# Optional: PDF text extraction + bulk import support
./venv/bin/pip install -r requirements-extras.txt
cp config.py.example config.py
# Generate a secret:
python3 -c "import secrets; print(secrets.token_hex(32))"
# Paste it into config.py as SECRET_KEY; set OUTPUT_DIR to your inbox path.
./venv/bin/python receiver.py
```

Put Nginx or Caddy in front for TLS (see `docs/setup.md`). Then load the
extension from `extension/` via `chrome://extensions` (Developer Mode → Load
Unpacked) or Firefox `about:debugging`, click the icon, paste the endpoint
URL and the same secret, and clip away.

Full walkthrough: [`docs/setup.md`](docs/setup.md). Security model:
[`docs/security.md`](docs/security.md). AMO publishing:
[`docs/amo-submission.md`](docs/amo-submission.md). Privacy:
[`docs/privacy.md`](docs/privacy.md).

---

## What it does

A browser extension (Chrome + Firefox) that:

1. Extracts clean readable content from the current page using Mozilla's Readability.js
2. Signs the payload with a shared HMAC-SHA256 secret
3. POSTs it as Markdown to a user-configured HTTP endpoint on their own server

A server-side receiver that:

1. Verifies the HMAC signature
2. Writes a `.md` file to a configured output folder
3. Returns a confirmation to the extension

No database. No user accounts. No third-party services. One folder, one secret, one endpoint.

---

## Project structure

```
clipship/
├── extension/
│   ├── manifest.json              # MV3, minimal permissions
│   ├── popup.html / popup.js      # UI: config + tags + clip + status
│   ├── background.js              # Signs + POSTs + handles PDF / E2E
│   ├── content.js                 # Readability + HTML→Markdown + tag extraction
│   ├── Readability.js             # Mozilla Readability (vendored)
│   ├── build.sh                   # Lint + package via web-ext
│   └── icons/
│
├── server/
│   ├── receiver.py                # Flask /clip endpoint (HMAC, assets, PDFs, E2E)
│   ├── web.py                     # Optional web UI (read-only, basic auth)
│   ├── bulk_import.py             # CLI: import a file of URLs in parallel
│   ├── test_clipship.py           # Unittest suite (19 tests)
│   ├── config.py                  # Your config (gitignored)
│   ├── config.py.example          # Template
│   ├── requirements.txt           # Flask + markdown
│   ├── requirements-extras.txt    # pypdf, readability-lxml, html2text
│   ├── clipship.service           # systemd unit for the receiver
│   └── clipship-web.service       # systemd unit for the web UI
│
├── docs/
│   ├── setup.md                   # End-to-end install + every feature
│   ├── security.md                # Threat model
│   ├── privacy.md                 # What is stored / sent / never sent
│   └── amo-submission.md          # Step-by-step Firefox AMO publishing
│
├── README.md
└── LICENSE                        # MIT
```

---

## How it works

```
┌───────────── Browser ─────────────┐         ┌────── Your server ──────┐
│  popup.js (config + tags + clip)  │         │   Nginx / Caddy (TLS)   │
│            │                       │  POST   │           │             │
│            ▼                       │ ──────▶ │           ▼             │
│  background.js                     │  HTTPS  │   receiver.py (Flask)   │
│   • inject Readability + content   │         │   1. verify HMAC + time │
│   • read window.__clipshipResult   │         │   2. sanitize filename  │
│   • optional AES-GCM encrypt       │         │   3. download images    │
│   • HMAC-sign + fetch              │         │      → assets/          │
│            │                       │         │   4. write .md          │
│            ▼  (page DOM stays here)│         │           │             │
│  content.js (one-shot, in page)    │         │           ▼             │
│   • Readability.parse              │         │   OUTPUT_DIR/*.md       │
│   • HTML → Markdown                │         │   OUTPUT_DIR/assets/    │
│   • auto-extract meta tags         │         │                         │
│   • build frontmatter w/ user tags │         │   web.py (optional)     │
│            │                       │         │   • Basic-auth          │
│            ▼  (returns via msg)    │         │   • lists clips         │
└────────────────────────────────────┘         │   • renders markdown    │
                                                │   • tag filter + search │
                                                └─────────────────────────┘
```

The wire format is one JSON POST per clip:

```http
POST /clip
X-Clipship-Timestamp: 1717180000
X-Clipship-Signature: <hex HMAC-SHA256(secret, ts + "." + raw_body)>
Content-Type: application/json

{ "filename": "...md", "content": "---\ntitle: ...\n..." }
```

Three other payload shapes:

| Shape | Trigger | Extra fields |
|---|---|---|
| Plain markdown | normal clip | `content` |
| Encrypted | `encryption_enabled = true` | `encrypted`, `algorithm`, `kdf`, `kdf_iterations`, `salt`, `iv`, `ciphertext` (no `content`) |
| PDF | tab URL matches `.pdf(?\|#\|$)` | `pdf_url`, `title`, `tags` (no `content`) |

Detailed reference: [`docs/setup.md`](docs/setup.md).
[`docs/security.md`](docs/security.md) describes the threat model.

## Tests

```bash
cd server
./venv/bin/pip install -r requirements.txt -r requirements-extras.txt
./venv/bin/python -m unittest test_clipship.py
```

19 tests: HMAC, replay window, path traversal, asset localization with
mocked fetch, SSRF blocklist, encrypted payload accept/reject, web UI auth,
tag filtering, search, asset serving, traversal blocking under `/assets`.

## Publishing to Firefox AMO

```bash
cd extension
./build.sh
# → web-ext-artifacts/clipship-x.y.z.zip
```

The build script runs `web-ext lint` then `web-ext build`. Step-by-step
upload walkthrough, listing copy template and answers to likely reviewer
questions: [`docs/amo-submission.md`](docs/amo-submission.md).

---

*MIT License.*

