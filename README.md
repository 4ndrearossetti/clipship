# Clipship

> A minimal, self-hosted web clipper. Captures clean article content from any page and delivers it as Markdown to a folder on your own server — no cloud, no account, no vendor.

---

## Quickstart

```bash
# Server
git clone https://github.com/4ndrearossetti/clipship.git
cd clipship/server
python3 -m venv venv && ./venv/bin/pip install -r requirements.txt
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
[`docs/security.md`](docs/security.md).

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
│   ├── manifest.json          # WebExtension manifest v3
│   ├── popup.html             # Extension popup UI
│   ├── popup.js               # Clip action, config UI, status feedback
│   ├── background.js          # Service worker: signs and POSTs payload
│   ├── content.js             # Injected into page: runs Readability, returns article
│   ├── Readability.js         # Mozilla Readability (vendored, unmodified)
│   └── icons/
│       ├── icon-16.png
│       ├── icon-48.png
│       └── icon-128.png
│
├── server/
│   ├── receiver.py            # Flask HTTP endpoint
│   ├── config.py              # Server configuration (endpoint, output path, secret)
│   ├── requirements.txt
│   └── clipship.service       # systemd unit file
│
├── docs/
│   ├── setup.md               # End-to-end setup guide
│   └── security.md            # Threat model and security decisions
│
├── README.md
└── LICENSE                    # MIT
```

---

## Extension

### manifest.json

Manifest V3. Permissions required:

- `activeTab` — read current page content on user action only
- `storage` — persist endpoint URL and HMAC secret locally
- `scripting` — inject content.js to extract article body

No `<all_urls>` blanket permission. Content script is injected on demand, not persistently.

### Content extraction (content.js + Readability.js)

Vendor Mozilla's `Readability.js` directly into the extension. No CDN, no runtime fetch.

On clip action:
1. Clone `document` to avoid mutating the live page
2. Run `new Readability(documentClone).parse()`
3. Return `{ title, byline, content (HTML), textContent, url, siteName }`
4. Convert `content` to Markdown using a minimal HTML-to-Markdown function (no external dependency needed for the subset Readability produces: headings, paragraphs, links, lists, blockquotes, code)

Output format per clipped file:

```markdown
---
title: {title}
source: {url}
author: {byline}
site: {siteName}
clipped: {ISO8601 timestamp}
---

{body in Markdown}
```

### Signing (background.js)

HMAC-SHA256 using the Web Crypto API (built into all modern browsers, no library needed):

```
signature = HMAC-SHA256(secret, timestamp + "." + body)
```

Request headers sent:

```
X-Clipship-Timestamp: {unix timestamp}
X-Clipship-Signature: {hex digest}
Content-Type: application/json
```

Timestamp is included in the signed payload to prevent replay attacks (server rejects requests where `|now - timestamp| > 300 seconds`).

### Popup UI (popup.html / popup.js)

First run: shows configuration fields (endpoint URL, HMAC secret). Saved to `chrome.storage.local`.

Subsequent runs: single "Clip this page" button. Shows inline status: extracting → signing → sending → saved / error.

No options page needed — config lives in the popup itself, accessible via a small gear icon.

---

## Server

### receiver.py

Flask application. Single route: `POST /clip`

**Request body (JSON):**

```json
{
  "filename": "2026-05-30T12-34-56-title-slug.md",
  "content": "---\ntitle: ...\n..."
}
```

**Verification logic:**

1. Read `X-Clipship-Timestamp` and `X-Clipship-Signature` from headers
2. Reject if `|now - timestamp| > 300` seconds
3. Recompute `HMAC-SHA256(secret, timestamp + "." + raw_body)`
4. Reject if signature does not match (constant-time comparison via `hmac.compare_digest`)
5. Sanitize filename: strip path separators, enforce `.md` extension, reject `..`
6. Write to configured output directory
7. Return `200 {"status": "ok", "file": filename}` or appropriate error

**config.py:**

```python
SECRET_KEY   = "your-secret-here"   # must match extension config
OUTPUT_DIR   = "/path/to/your/inbox"
HOST         = "127.0.0.1"          # bind to localhost; put Nginx in front
PORT         = 5050
```

### Running in production

The receiver should not be exposed directly. Put it behind Nginx or Caddy as a reverse proxy with TLS. The extension POSTs to `https://yourdomain.com/clip`.

**Nginx location block:**

```nginx
location /clip {
    proxy_pass http://127.0.0.1:5050;
    proxy_set_header X-Real-IP $remote_addr;
    client_max_body_size 10m;
}
```

**systemd unit (clipship.service):**

```ini
[Unit]
Description=Clipship receiver
After=network.target

[Service]
User=www-data
WorkingDirectory=/opt/clipship/server
ExecStart=/opt/clipship/server/venv/bin/python receiver.py
Restart=on-failure
RestartSec=5

[Install]
WantedBy=multi-user.target
```

### requirements.txt

```
flask>=3.0
```

No other dependencies. HMAC and file I/O are stdlib.

---

## Security model

### What is protected

- **Unauthorized writes:** Any POST without a valid HMAC signature is rejected with 403 before touching the filesystem.
- **Replay attacks:** Timestamp window of ±5 minutes. A captured request cannot be replayed after that window.
- **Path traversal:** Filename is sanitized server-side regardless of what the extension sends. Output is always confined to `OUTPUT_DIR`.
- **Oversized payloads:** Nginx enforces `client_max_body_size 10m`. Flask also enforces a request size limit.
- **Timing attacks on signature comparison:** `hmac.compare_digest` used, not `==`.

### What is not protected (and why that's acceptable)

- **Secret confidentiality:** The HMAC secret is stored in `chrome.storage.local`, which is accessible to the extension only — not to page scripts. It is not in source code and is never transmitted. The user is responsible for choosing a strong secret (document this clearly).
- **Content confidentiality:** The POST body is not encrypted beyond TLS. Anyone with a valid TLS MITM could read clipped content. For a personal self-hosted tool behind your own domain, this is acceptable. Document that the server must be HTTPS.
- **Extension code review:** Manifest V3, minimal permissions, no remote code execution, all dependencies vendored. Users should review the code before installing from source.

### Secret generation (documented for users)

```bash
python3 -c "import secrets; print(secrets.token_hex(32))"
```

---

## Setup guide (docs/setup.md)

### Server

```bash
git clone https://github.com/yourhandle/clipship
cd clipship/server
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt

# Edit config.py: set SECRET_KEY and OUTPUT_DIR
cp config.py.example config.py
nano config.py

# Install and start systemd service
sudo cp clipship.service /etc/systemd/system/
sudo systemctl enable --now clipship

# Add Nginx reverse proxy block (see README) and ensure TLS is active
```

### Extension (Chrome)

1. `chrome://extensions` → Enable Developer Mode
2. Load Unpacked → select `clipship/extension/`
3. Click the extension icon → enter your endpoint URL and secret → Save

### Extension (Firefox)

1. `about:debugging` → This Firefox → Load Temporary Add-on
2. Select `clipship/extension/manifest.json`
3. Same configuration step as above

For permanent Firefox install: sign via `web-ext sign` with a Mozilla account (free).

---

## Out of scope (v1)

- Browser-native PDF clipping (PDF content extraction is a separate problem)
- Bulk URL import
- Tagging or metadata beyond frontmatter
- A web UI for browsing clipped content
- End-to-end encryption of clip content
- Firefox signed release on AMO

These are all reasonable v2 additions. Keep v1 focused.

---

*MIT License. Built to be auditable: ~400 lines of code total across all components.*

