# Clipship setup guide

End-to-end walkthrough: server, reverse proxy, extension.

## 1. Pick a domain and create the inbox

You need:

- A domain name (or subdomain) pointing at the server, e.g. `clip.example.com`.
- A directory on disk that will hold clipped Markdown files.

```bash
sudo mkdir -p /var/lib/clipship/inbox
sudo chown www-data:www-data /var/lib/clipship/inbox
```

## 2. Generate a shared secret

The same value is configured on the server and pasted into the extension.

```bash
python3 -c "import secrets; print(secrets.token_hex(32))"
```

Keep the output somewhere safe — you will paste it twice and then never see it again.

## 3. Install the server

```bash
sudo mkdir -p /opt/clipship
sudo chown $USER /opt/clipship
git clone https://github.com/yourhandle/clipship /opt/clipship
cd /opt/clipship/server

python3 -m venv venv
./venv/bin/pip install -r requirements.txt

cp config.py.example config.py
# Edit SECRET_KEY (paste the value from step 2) and OUTPUT_DIR (e.g.
# /var/lib/clipship/inbox).
${EDITOR:-nano} config.py
```

Quick smoke test before wiring up systemd:

```bash
./venv/bin/python receiver.py
# In another terminal:
curl http://127.0.0.1:5050/health
# → {"status":"ok"}
```

Stop the foreground process (Ctrl+C) before continuing.

## 4. Run under systemd

Edit `clipship.service` if your paths differ from the defaults, then:

```bash
sudo cp clipship.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now clipship
sudo systemctl status clipship
```

Logs: `journalctl -u clipship -f`.

## 5. Put Nginx (or Caddy) in front with TLS

The receiver binds to `127.0.0.1` and speaks plain HTTP. You must terminate TLS
in front of it.

**Nginx:**

```nginx
server {
    listen 443 ssl http2;
    server_name clip.example.com;

    ssl_certificate     /etc/letsencrypt/live/clip.example.com/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/clip.example.com/privkey.pem;

    location /clip {
        proxy_pass http://127.0.0.1:5050;
        proxy_set_header X-Real-IP $remote_addr;
        client_max_body_size 10m;
    }
}
```

**Caddy** (equivalent):

```
clip.example.com {
    reverse_proxy /clip 127.0.0.1:5050 {
        header_up X-Real-IP {remote_host}
    }
    request_body {
        max_size 10MB
    }
}
```

Reload your proxy and verify the endpoint responds publicly over HTTPS.

## 6. Install the extension

### Chrome / Chromium / Edge / Brave

1. Open `chrome://extensions`.
2. Toggle **Developer mode** on (top right).
3. Click **Load unpacked** and pick the `extension/` directory in this repo.
4. Pin the Clipship icon to the toolbar.
5. Click the icon → enter your endpoint (`https://clip.example.com/clip`) and the
   shared secret → **Save**. The browser will prompt you to grant the extension
   permission to talk to that host — accept it. (This is what lets the
   extension bypass CORS for your endpoint, so no `Access-Control-*` headers
   are needed on the server.)

### Firefox (temporary install)

1. Open `about:debugging` → **This Firefox**.
2. **Load Temporary Add-on…** → select `extension/manifest.json`.
3. Configure endpoint and secret the same way.

Temporary add-ons in Firefox are removed when the browser restarts. For a
permanent install, sign the extension with `web-ext sign --api-key=… --api-secret=…`
against a free Mozilla developer account, then install the signed `.xpi`.

## 7. Try it

Open any article. Click the Clipship icon → **Clip this page**. The status line
should walk through *Extracting → Saved: <filename>.md*. The file appears in
your inbox directory:

```bash
ls -lt /var/lib/clipship/inbox | head
```

Done.

## How clips are stored

Every successful clip writes one Markdown file to `OUTPUT_DIR`. When
`DOWNLOAD_ASSETS` is enabled (the default), every image referenced in the
clip is also downloaded into `OUTPUT_DIR/assets/`, and the Markdown is
rewritten to point at the local copy. The layout looks like this:

```
/var/lib/clipship/inbox/
├── 2026-05-31T14-22-09Z-my-article.md
├── 2026-05-31T14-22-09Z-my-article-img1.jpg   ← from assets/ subdir
└── assets/
    ├── 2026-05-31T14-22-09Z-my-article-img1.jpg
    └── 2026-05-31T14-22-09Z-my-article-img2.png
```

The Markdown references each image as `assets/<file>` (relative path), so
moving the inbox directory keeps the links intact. Asset names are
`<md-stem>-img<n>.<ext>`, with `n` counting unique URLs in the order they
appear — duplicates point at the same file. Extensions come from the
server's `Content-Type` response, falling back to the URL path.

If a download fails (host unreachable, SSRF block, size cap, timeout), the
original remote URL is preserved in the Markdown so you can still view the
image in a renderer that fetches remote content. The response payload
reports `assets_downloaded` and `assets_failed` counts.

Turn the whole feature off by setting `DOWNLOAD_ASSETS = False` in
`config.py` — the server then stops making any outbound HTTP requests.

## PDF clipping

Click the Clipship icon on a tab whose URL ends in `.pdf` and the extension
sends the URL — not the rendered page — to the server. The receiver
downloads the PDF (SSRF-guarded, capped by `MAX_PDF_BYTES`, default 100
MiB), stores it under `assets/<stem>.pdf`, and writes a stub Markdown file:

```markdown
---
title: "..."
source: "https://example.com/foo.pdf"
clipped: "..."
type: "pdf"
pdf: "assets/2026-...-foo.pdf"
tags: [...]
---

[Open PDF](assets/2026-...-foo.pdf)

---

<extracted text body, when pypdf is installed>
```

PDF text extraction needs the optional `pypdf` package:

```bash
./venv/bin/pip install -r requirements-extras.txt
```

Without `pypdf`, the PDF is still downloaded and linked — the body is just
empty after the link.

## Web UI

A read-only browser for the inbox: list, search, view, filter by tag.
Disabled by default. The receiver binds to `127.0.0.1` for safety, so you
need one of the access patterns below to view it from your laptop.

### Configure

```python
# config.py
WEB_UI_ENABLED     = True
WEB_UI_USERNAME    = "admin"
WEB_UI_PASSWORD    = "..."   # use a long random value
WEB_UI_HOST        = "127.0.0.1"
WEB_UI_PORT        = 5051
WEB_UI_TRUST_PROXY = True    # honour X-Forwarded-* from your reverse proxy
```

### Run it

Foreground:

```bash
./venv/bin/python web.py
```

systemd:

```bash
sudo cp clipship-web.service /etc/systemd/system/
sudo systemctl enable --now clipship-web
```

### Access pattern A — dedicated subdomain (recommended for daily use)

Set up `ui.clip.example.com` (or any subdomain you control) and reverse-proxy
the whole thing. Easiest because there's no URL-prefix rewriting:

```nginx
server {
    listen 443 ssl http2;
    server_name ui.clip.example.com;

    ssl_certificate     /etc/letsencrypt/live/ui.clip.example.com/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/ui.clip.example.com/privkey.pem;

    location / {
        proxy_pass http://127.0.0.1:5051;
        proxy_set_header Host              $host;
        proxy_set_header X-Real-IP         $remote_addr;
        proxy_set_header X-Forwarded-For   $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
    }
}
```

Then browse to `https://ui.clip.example.com` — Basic-auth prompt appears,
you log in, you're done.

### Access pattern B — subpath on your existing domain

If you already have `clip.example.com` for the receiver, add a `/ui/` path:

```nginx
location /ui/ {
    proxy_pass http://127.0.0.1:5051/;
    proxy_set_header Host              $host;
    proxy_set_header X-Real-IP         $remote_addr;
    proxy_set_header X-Forwarded-For   $proxy_add_x_forwarded_for;
    proxy_set_header X-Forwarded-Proto $scheme;
    proxy_set_header X-Forwarded-Prefix /ui;
}
```

The `X-Forwarded-Prefix` header tells the Flask app it's mounted at `/ui/`
so internal links and asset URLs come out as `/ui/clip/…` and
`/ui/assets/…` instead of `/clip/…`. Requires `WEB_UI_TRUST_PROXY = True`
in `config.py` (the default).

### Access pattern C — SSH port forward (quick, no nginx changes)

If you don't want to touch nginx, tunnel the port over SSH:

```bash
# On your laptop:
ssh -L 5051:127.0.0.1:5051 your-user@your-server
```

Leave that SSH session open and browse to <http://localhost:5051> on your
laptop. The traffic is encrypted by SSH; the server still only binds to
loopback. Closing the tunnel (or SSH session) closes the access.

This is the fastest way to try the web UI before committing to a proxy
config.

### What you get

Tags are clickable chips, PDF clips get a `pdf` badge, and `?q=…` does
full-text search across titles, tags, sources, and bodies.

## Troubleshooting

- **403 `invalid signature`** — the secret in the extension does not match
  `SECRET_KEY` in `config.py`. Save it again on both sides.
- **403 `timestamp outside accepted window`** — server clock drift. Confirm
  NTP is running on the server.
- **`cannot clip browser internal pages`** — extensions cannot read pages like
  `chrome://`, `about:`, the new-tab page, or the Chrome Web Store. Use a
  regular `http(s)://` page.
- **No content extracted** — Readability could not find an article. This is
  common on home pages, search-result pages, and apps that render with no
  semantic structure. Try a specific article URL.
- **CORS preflight error in the browser console** — you denied the
  host-permission prompt when saving, or you're running an older build of the
  extension. Open the popup, hit the gear, **Save** again, and accept the
  permission prompt this time. The extension does not need any
  `Access-Control-*` headers on the server when host permission is granted.
