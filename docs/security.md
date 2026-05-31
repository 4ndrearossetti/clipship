# Clipship security model

Clipship's threat model assumes a personal, self-hosted deployment behind a
TLS-terminating reverse proxy. The endpoint is reachable from the public
internet but is only ever written to by the user's own browser.

## What is protected

- **Unauthorized writes** — every `POST /clip` is rejected with `403` unless it
  carries a valid HMAC-SHA256 signature over `timestamp + "." + raw_body`. The
  filesystem is never touched before signature verification.
- **Replay attacks** — requests outside a ±5 minute window from the server
  clock are rejected. A captured request stops working after that window.
- **Path traversal** — the server reduces the client-supplied filename to its
  basename, strips path separators, rejects `..`, normalises to
  `[A-Za-z0-9._-]`, and enforces a `.md` extension. A double check confirms
  the resolved path is still inside `OUTPUT_DIR`.
- **Filename collisions** — if `foo.md` exists, the next clip is written as
  `foo-1.md`, `foo-2.md`, … rather than overwriting prior content.
- **Oversized payloads** — Flask rejects bodies above `MAX_BODY_BYTES`
  (10 MiB by default) with `413`. Configure your reverse proxy with a matching
  `client_max_body_size` limit so the cap is also enforced at the edge.
- **Timing attacks** — signature comparison uses `hmac.compare_digest`, not `==`.
- **SSRF via asset downloads** — when `DOWNLOAD_ASSETS = True` the server
  fetches every image URL referenced in a clip. Each URL is validated before
  the request: scheme must be `http(s)`, the resolved IP cannot be in any
  private / loopback / link-local / CGNAT / multicast / reserved range, and
  redirects are re-validated against the same rules. Per-asset size and
  timeout caps are enforced (`MAX_ASSET_BYTES`, `ASSET_TIMEOUT`), and the
  total number of fetches per clip is capped (`MAX_ASSETS_PER_CLIP`). Disable
  the feature entirely (`DOWNLOAD_ASSETS = False`) if you do not want the
  receiver making outbound HTTP at all.

## What is not protected (and why that is acceptable here)

- **Secret confidentiality on the client.** The HMAC secret is stored in
  `chrome.storage.local`, accessible to the extension only — not to page
  scripts, not to other extensions without explicit permission, and not
  synced to any cloud. It is never sent in plaintext as a header or query
  string. The user is responsible for choosing a strong secret (see below).
- **Content confidentiality.** By default, the body is JSON, signed but not
  encrypted at the application layer; TLS on the wire is sufficient for a
  personal tool whose attacker model does not include a compromised CA. If
  that does describe your model, enable the opt-in end-to-end encryption (see
  below) — the server then stores ciphertext it cannot read.
- **Server clock integrity.** The replay window depends on the server clock
  being roughly correct. Run NTP.
- **Denial of service.** A peer with internet access can hit the endpoint as
  fast as they like. Bodies above the size cap are rejected cheaply, and
  unsigned requests are rejected before any disk I/O — but if abuse becomes
  an issue, rate-limit at the reverse proxy (`limit_req` in Nginx).
- **Extension review.** Users self-install from source. There is no review
  process beyond the user's own. Manifest V3 minimal permissions and vendored
  dependencies make this tractable: the only network destination is the
  user-configured endpoint, and the only stored data is `{endpoint, secret}`.

## End-to-end encryption (opt-in)

Off by default. When enabled in the extension settings, every clip is
encrypted client-side before being POSTed:

- **Key derivation:** PBKDF2-HMAC-SHA256, 600 000 iterations, 16-byte
  random salt per clip.
- **Cipher:** AES-GCM, 256-bit key, 12-byte random nonce per clip,
  16-byte authentication tag.
- **Plaintext input:** the complete Markdown body (frontmatter + content).
- **Wire format:** the JSON payload carries `encrypted: true` plus the
  algorithm, KDF, iteration count, salt, IV and base64 ciphertext. No
  `content` field is sent.
- **On disk:** the server writes a Markdown file whose frontmatter records
  every parameter needed to decrypt, and whose body is the base64
  ciphertext.

What encryption gives you:

- A server compromise yields ciphertext only — no clip body is readable
  without the passphrase.
- A TLS interception that gets past your CA still surfaces only
  ciphertext bound by an authenticated GCM tag.
- The web UI and the asset downloader correctly refuse to operate on
  encrypted clips, so nothing leaks through them.

What it does not protect against:

- **Device compromise.** The passphrase lives in `chrome.storage.local`
  for UX; an attacker with the machine has the key. Re-enter the
  passphrase in a fresh browser if that matters.
- **Brute force on a weak passphrase.** PBKDF2 makes guessing more
  expensive per attempt but cannot rescue a low-entropy passphrase.
  Treat it like a password manager master password.
- **Image confidentiality.** When encryption is on, server-side asset
  download is automatically disabled (the server cannot see image URLs
  inside ciphertext). Images stay as remote references in the
  ciphertext, fetched by whatever client decrypts the clip later.

## Web UI

The optional web UI exposes the inbox over HTTP Basic auth (constant-time
comparison via `hmac.compare_digest`). The systemd unit runs the service
with `ReadOnlyPaths=<inbox>`, so even a compromised UI process cannot
modify the data it serves. Encrypted clips are never decrypted by the UI —
the server doesn't have the passphrase — so a UI compromise still cannot
recover encrypted clip bodies.

## Secret strength

Use at least 256 bits of entropy. The documented one-liner produces 32 random
bytes hex-encoded:

```bash
python3 -c "import secrets; print(secrets.token_hex(32))"
```

Do not reuse a password or anything memorable. The secret is read once by you,
pasted into the extension's settings, and read once by the server config — it
never needs to be typed again.

## Reporting

If you find a vulnerability, open an issue describing the impact (no need for
a proof of concept that affects real users) and propose a fix. Clipship is
small enough that most issues will have a one-line patch.
