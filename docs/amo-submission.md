# Submitting Clipship to addons.mozilla.org

Step-by-step for publishing Clipship as a signed Firefox extension on
[addons.mozilla.org][amo] (AMO). Reuses the manifest and source as-is —
no per-distribution patching needed.

[amo]: https://addons.mozilla.org/

## 1. One-time setup

1. Create a Firefox account at <https://accounts.firefox.com/>.
2. Log into [addons.mozilla.org](https://addons.mozilla.org/) with that
   account.
3. Accept the developer agreement at
   <https://addons.mozilla.org/developers/>.

You do not need API keys for the manual upload flow described here. (If
you want CI-driven signing later, see "Automated signing" at the bottom.)

## 2. Build the package

```bash
cd extension
./build.sh
# → web-ext-artifacts/clipship-1.0.0.zip
```

This runs `web-ext lint` (which is configured via `web-ext-config.cjs`
to exclude `build.sh` and the artifacts directory from packaging), then
`web-ext build`. The expected lint output on a clean working tree is:

```
0 errors, 3 warnings
  WARN BACKGROUND_SERVICE_WORKER_IGNORED   (cross-browser background fallback)
  WARN UNSAFE_VAR_ASSIGNMENT × 2           (Readability.js, Mozilla's own code)
```

All three warnings are expected and do not block AMO approval:

- **`BACKGROUND_SERVICE_WORKER_IGNORED`** — the manifest declares both
  `background.service_worker` (Chrome) and `background.scripts` (Firefox)
  so a single bundle works on both. Firefox notes that it ignores the
  Chrome key; that is the correct behaviour.
- **`UNSAFE_VAR_ASSIGNMENT`** — two `innerHTML` assignments inside
  `Readability.js`. That file is Mozilla's own published library,
  vendored unmodified from
  <https://github.com/mozilla/readability>. Reviewers recognise it.

## 3. Upload via the AMO web flow

1. Go to <https://addons.mozilla.org/developers/addon/submit/distribution>.
2. Choose **On this site** (lists the extension on AMO and signs it).
   Pick **On your own** for self-hosted signed XPI without the listing.
3. Upload `web-ext-artifacts/clipship-1.0.0.zip`.
4. AMO will run validation. The same three warnings will appear; click
   continue.
5. Fill in the listing fields below.
6. Upload the source bundle if asked. AMO requires source for extensions
   that use minified or vendored libraries. The whole `extension/`
   directory zipped is the source (including `Readability.js`); the
   same artifact you uploaded works.
7. Submit for review. First reviews currently take 1–14 days; updates
   to an already-approved extension usually clear in hours.

## 4. Listing copy (paste into the AMO form)

**Name**: `Clipship`

**Summary** (max 250 chars):

> Self-hosted web clipper. Captures clean article content as Markdown
> and POSTs it, HMAC-signed, to your own server. No cloud, no account,
> no vendor.

**Description** (Markdown, ~500–1500 words is the sweet spot):

```markdown
Clipship is a minimal web clipper for people who want their reading
archive to live on their own server, not in someone else's cloud.

## What it does

- Extracts clean readable content from the current page using
  Mozilla's Readability.
- Converts it to Markdown with YAML frontmatter (title, source,
  author, site, timestamp, tags).
- Signs the payload with HMAC-SHA256 using a shared secret you
  configure once.
- POSTs it to a server endpoint you control. The server writes the
  Markdown to a folder, optionally downloads images so the clip is
  self-contained, and optionally extracts text from PDFs.

## What it does NOT do

- No accounts, no telemetry, no third-party services.
- The extension talks only to the endpoint you configure in its
  settings. There are no fallback URLs, analytics pings, or update
  servers contacted by the extension code.
- No data is uploaded anywhere on install or on update.

## Features

- Markdown extraction with auto-tagging from meta keywords and
  article tags, plus user-supplied tags per clip.
- PDF clipping: clipping a tab whose URL points at a PDF sends the
  URL to the server, which stores the PDF and (optionally) extracts
  its text.
- Optional end-to-end encryption: when enabled, the content is
  encrypted with AES-GCM-256 (passphrase-derived via PBKDF2-SHA256,
  600 000 iterations) before leaving the browser. The server stores
  ciphertext and cannot read your clips.
- Replay protection (signed timestamp window) and TLS recommended
  for transport security.

## Setup

You need to run the receiver server yourself. The source is at
<https://github.com/4ndrearossetti/clipship> — Python + Flask, no
database, runs from a venv behind Nginx or Caddy. See `docs/setup.md`
in the repository for the full walkthrough.

After installing the extension, click its icon, paste the endpoint
URL and the shared HMAC secret, and grant the requested host
permission for your endpoint. That's it.

## Open source

MIT-licensed. ~600 lines of code total across the extension and the
server. Source: <https://github.com/4ndrearossetti/clipship>.
```

**Categories**: `Bookmarks`, `Other` (Productivity, if listed).

**Support email**: your contact email.

**Support site / homepage**: <https://github.com/4ndrearossetti/clipship>

**License**: MIT

**Privacy policy**: link to `docs/privacy.md` in the repo, or paste its
content into the AMO privacy policy field. The short version:

> Clipship sends the content of the page you choose to clip to the
> server you configured in its settings. It sends nothing else, to
> nothing else. Your endpoint URL, shared HMAC secret, and optional
> encryption passphrase are stored in your browser's local extension
> storage and never transmitted.

## 5. After approval

AMO will email you when the review completes. You'll get:

- A listing URL: `https://addons.mozilla.org/firefox/addon/clipship/`
  (or similar slug).
- An auto-update URL Firefox uses to keep installs current. Nothing
  to do here — just bumping `version` in `manifest.json` and
  re-uploading triggers an update for all users.

## 6. Updating

For each new version:

1. Bump `version` in `extension/manifest.json` (semver).
2. `./build.sh`.
3. AMO dashboard → your extension → **Upload New Version**.
4. Upload the new `clipship-x.y.z.zip`.
5. Same listing form pre-filled; usually no fields to change.

Each update gets its own review, but updates that don't introduce new
permissions clear in hours.

---

## Optional: automated signing

If you'd rather sign from CI than upload by hand, AMO supports an API:

1. Generate an API key + secret at
   <https://addons.mozilla.org/developers/addon/api/key/>.
2. Export them:

   ```bash
   export WEB_EXT_API_KEY=user:00000000:000
   export WEB_EXT_API_SECRET=…
   ```

3. Sign and submit in one step:

   ```bash
   web-ext sign --channel=listed --source-dir=extension
   ```

   For self-hosted signed XPIs, use `--channel=unlisted` and grab the
   signed file from `web-ext-artifacts/`.

Do not commit the API key — keep it in your CI secret store.

## Known reviewer questions and answers

If a human reviewer asks:

- **"Why do you need optional host permissions for `<all_urls>`?"** —
  The extension lets the user configure their own server endpoint at
  any domain. Host permission is requested at runtime for only the
  one host the user has entered (`chrome.permissions.request`); it is
  never used for any URL the user has not explicitly set.

- **"What is in Readability.js?"** — Unmodified copy of
  <https://github.com/mozilla/readability>'s `Readability.js` at
  the version vendored at commit time. Used to extract the article
  body from the active page.

- **"Why is `service_worker` declared if Firefox ignores it?"** —
  The same source bundle is loaded into Chrome and Firefox. Chrome's
  MV3 requires `background.service_worker`; Firefox's MV3 requires
  `background.scripts`. Both keys are present so each browser uses
  the field it understands.

- **"What does the optional encryption do?"** — When the user enables
  it in settings, the markdown body is encrypted with AES-GCM-256 via
  Web Crypto using a passphrase-derived key (PBKDF2-SHA256, 600 000
  iterations) before the POST. The passphrase is stored only in
  `chrome.storage.local`. The encrypted-payload server side simply
  writes the ciphertext to disk.
