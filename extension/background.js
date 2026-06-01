// Service worker: orchestrates extraction, HMAC signing, and POST.
// Also handles the PDF flow, which sends the URL to the server instead of
// an extracted article body.

const ext = (typeof browser !== "undefined") ? browser : chrome;

// PDF detection — match common URL patterns. Browsers serve PDFs in their
// built-in viewer for these, and content-script injection won't return useful
// data, so we route them through the server-side PDF flow instead.
const PDF_EXT_RE = /\.pdf(\?|#|$)/i;
// Path patterns that scholarly hosts use to serve PDFs without a .pdf
// extension (arxiv.org/pdf/<id>, openreview.net/pdf, biorxiv.org/.../pdf, ...).
const PDF_PATH_RE = /\/pdf(\/|$)/i;

async function getActiveTab() {
  const [tab] = await ext.tabs.query({ active: true, currentWindow: true });
  return tab;
}

function isLocalFile(tab) {
  return tab && tab.url && tab.url.toLowerCase().startsWith("file:");
}

function isPdfTab(tab) {
  if (!tab || !tab.url) return false;
  if (isLocalFile(tab)) return false; // can't fetch local files server-side
  if (PDF_EXT_RE.test(tab.url)) return true;
  // Common scholarly-PDF paths: arxiv.org/pdf/…, biorxiv.org/…/pdf, etc.
  try {
    const u = new URL(tab.url);
    if (PDF_PATH_RE.test(u.pathname)) return true;
  } catch { /* ignore */ }
  return false;
}

async function runExtraction(tabId, userTags) {
  // Push the user-supplied tags into the page before content.js runs so it
  // can merge them with auto-extracted meta tags into the frontmatter.
  await ext.scripting.executeScript({
    target: { tabId },
    func: (tags) => { window.__clipshipUserTags = tags || []; },
    args: [Array.isArray(userTags) ? userTags : []],
  });
  await ext.scripting.executeScript({
    target: { tabId },
    files: ["Readability.js", "content.js"],
  });
  const results = await ext.scripting.executeScript({
    target: { tabId },
    func: () => window.__clipshipResult,
  });
  return results && results[0] ? results[0].result : null;
}

function hexEncode(buffer) {
  const bytes = new Uint8Array(buffer);
  let out = "";
  for (let i = 0; i < bytes.length; i++) {
    out += bytes[i].toString(16).padStart(2, "0");
  }
  return out;
}

async function hmacSha256Hex(secret, message) {
  const enc = new TextEncoder();
  const key = await crypto.subtle.importKey(
    "raw",
    enc.encode(secret),
    { name: "HMAC", hash: "SHA-256" },
    false,
    ["sign"]
  );
  const sig = await crypto.subtle.sign("HMAC", key, enc.encode(message));
  return hexEncode(sig);
}

async function postSigned(endpoint, secret, bodyObj) {
  const body = JSON.stringify(bodyObj);
  const timestamp = Math.floor(Date.now() / 1000).toString();
  const signature = await hmacSha256Hex(secret, timestamp + "." + body);
  const resp = await fetch(endpoint, {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
      "X-Clipship-Timestamp": timestamp,
      "X-Clipship-Signature": signature,
    },
    body,
  });
  let data = null;
  try { data = await resp.json(); } catch { /* ignore */ }
  return { resp, data };
}

function slugify(s, max) {
  return (s || "untitled")
    .toLowerCase()
    .normalize("NFKD")
    .replace(/[̀-ͯ]/g, "")
    .replace(/[^a-z0-9]+/g, "-")
    .replace(/^-+|-+$/g, "")
    .slice(0, max || 60) || "untitled";
}

function isoFilename(d) {
  const pad = (n) => String(n).padStart(2, "0");
  return d.getUTCFullYear() + "-" +
    pad(d.getUTCMonth() + 1) + "-" +
    pad(d.getUTCDate()) + "T" +
    pad(d.getUTCHours()) + "-" +
    pad(d.getUTCMinutes()) + "-" +
    pad(d.getUTCSeconds()) + "Z";
}

async function clipCurrentTab(userTags) {
  const { endpoint, secret } = await ext.storage.local.get(["endpoint", "secret"]);
  if (!endpoint || !secret) {
    return { ok: false, error: "endpoint and secret not configured" };
  }

  const tab = await getActiveTab();
  if (!tab || !tab.id) return { ok: false, error: "no active tab" };
  if (tab.url && /^(chrome|about|edge|moz-extension|chrome-extension):/i.test(tab.url)) {
    return { ok: false, error: "cannot clip browser internal pages" };
  }
  if (isLocalFile(tab)) {
    return { ok: false, error: "cannot clip local files (file:// URLs); the server has no way to reach them" };
  }

  // PDF path: don't try to inject; send the URL for server-side download.
  if (isPdfTab(tab)) {
    return await clipAsPdf(endpoint, secret, tab, userTags);
  }

  let extracted;
  try {
    extracted = await runExtraction(tab.id, userTags);
  } catch (e) {
    return { ok: false, error: "injection failed: " + e.message };
  }
  if (!extracted) return { ok: false, error: "no result from page" };

  // Fallback: if Readability said "couldn't extract" AND the URL contains
  // /pdf/ in its path, the page is probably a PDF the URL pattern didn't
  // catch. Try the PDF flow as a second attempt.
  if (extracted.error) {
    try {
      const u = new URL(tab.url);
      if (PDF_PATH_RE.test(u.pathname)) {
        return await clipAsPdf(endpoint, secret, tab, userTags);
      }
    } catch { /* ignore */ }
    return { ok: false, error: extracted.error };
  }

  let resp, data;
  try {
    ({ resp, data } = await postSigned(endpoint, secret, {
      filename: extracted.filename,
      content: extracted.content,
    }));
  } catch (e) {
    return { ok: false, error: "network: " + e.message };
  }
  if (!resp.ok) {
    const msg = (data && (data.error || data.message)) || `HTTP ${resp.status}`;
    return { ok: false, error: msg };
  }
  return {
    ok: true,
    file: (data && data.file) || extracted.filename,
    assets_downloaded: (data && data.assets_downloaded) || 0,
    assets_failed: (data && data.assets_failed) || 0,
  };
}

async function clipAsPdf(endpoint, secret, tab, userTags) {
  const title = (tab.title || tab.url || "document").trim();
  const filename = `${isoFilename(new Date())}-${slugify(title)}.md`;
  let resp, data;
  try {
    ({ resp, data } = await postSigned(endpoint, secret, {
      filename,
      pdf_url: tab.url,
      title,
      tags: userTags || [],
    }));
  } catch (e) {
    return { ok: false, error: "network: " + e.message };
  }
  if (!resp.ok) {
    const msg = (data && (data.error || data.message)) || `HTTP ${resp.status}`;
    return { ok: false, error: msg };
  }
  return { ok: true, file: (data && data.file) || filename, pdf: true };
}

ext.runtime.onMessage.addListener((msg, _sender, sendResponse) => {
  if (msg && msg.type === "clip") {
    clipCurrentTab(msg.tags || []).then(sendResponse).catch((e) =>
      sendResponse({ ok: false, error: e.message })
    );
    return true; // async response
  }
  return false;
});
