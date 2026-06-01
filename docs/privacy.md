# Clipship privacy policy

Clipship is a self-hosted tool. It has no servers of its own. This page
documents exactly what data the extension and the server handle, where
they send it, and what they keep.

## What the extension stores

The browser extension persists the following in `chrome.storage.local`
(local to your browser; not synced):

- `endpoint` — the URL of your receiver, as you typed it.
- `secret` — the HMAC-SHA256 shared secret, as you typed it.

That is the complete list. No telemetry, analytics IDs, install IDs,
session tokens, or page-visit history is stored or transmitted by the
extension.

## What the extension sends

When you click **Clip this page**:

- The extension runs Mozilla's Readability on the page locally, converts
  the result to Markdown, signs `timestamp + "." + body` with HMAC-SHA256
  using the configured secret, and POSTs the body to the configured
  endpoint. Headers include the timestamp and signature.
- If you have entered tags in the popup, they are included in the body
  alongside auto-extracted tags from page meta elements.
- If the tab is a PDF, the page content is **not** read; instead, the PDF
  URL itself is sent so the server can download it.

No other network requests are made. The extension does not contact any
analytics service, update server, or third-party domain.

## What the server stores

The Clipship server writes received clips to a folder on disk
(`OUTPUT_DIR` in `config.py`). One Markdown file per clip; if
`DOWNLOAD_ASSETS` is enabled, referenced images are downloaded and stored
under `assets/`. Nothing else is persisted: no access log of clips beyond
what your OS keeps, no metadata in any database, no user records.

You control the storage entirely. Deleting the folder deletes the data.

## What the server transmits

The server can make outbound HTTP requests to:

- Image URLs found inside clipped Markdown, when `DOWNLOAD_ASSETS` is on.
- PDF URLs sent by the extension's PDF clip flow, when `DOWNLOAD_PDFS`
  is on.

Each outbound request is SSRF-validated: schemes other than `http(s)`
are refused; hosts that resolve to private, loopback, link-local, CGNAT,
multicast or reserved IP ranges are refused, including after redirects.

The server does not send the contents of your clips anywhere.

## Third parties

There are no third parties. The extension talks only to the endpoint
you configured. The server talks only to image and PDF hosts that are
referenced by clips you initiated.

## Telemetry

There is none.
