// Service worker: orchestrates extraction, optional encryption, HMAC signing,
// and POST. Also handles the PDF flow, which sends the URL to the server
// instead of an extracted article body.

const ext = (typeof browser !== "undefined") ? browser : chrome;

const PDF_URL_RE = /\.pdf(\?|#|$)/i;

async function getActiveTab() {
  const [tab] = await ext.tabs.query({ active: true, currentWindow: true });
  return tab;
}

function isPdfTab(tab) {
  if (!tab || !tab.url) return false;
  return PDF_URL_RE.test(tab.url);
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

function bytesToBase64(buffer) {
  const bytes = new Uint8Array(buffer);
  let binary = "";
  for (let i = 0; i < bytes.length; i++) binary += String.fromCharCode(bytes[i]);
  return btoa(binary);
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

// --- E2E encryption ---------------------------------------------------------
// Derive a 256-bit AES-GCM key from a passphrase via PBKDF2-SHA256.
// PBKDF2_ITERATIONS matches the receiver's expected default.
const PBKDF2_ITERATIONS = 600000;

async function deriveKey(passphrase, salt) {
  const enc = new TextEncoder();
  const material = await crypto.subtle.importKey(
    "raw",
    enc.encode(passphrase),
    { name: "PBKDF2" },
    false,
    ["deriveKey"]
  );
  return crypto.subtle.deriveKey(
    { name: "PBKDF2", salt, iterations: PBKDF2_ITERATIONS, hash: "SHA-256" },
    material,
    { name: "AES-GCM", length: 256 },
    false,
    ["encrypt"]
  );
}

async function encryptPayload(plaintext, passphrase) {
  const enc = new TextEncoder();
  const salt = crypto.getRandomValues(new Uint8Array(16));
  const iv = crypto.getRandomValues(new Uint8Array(12));
  const key = await deriveKey(passphrase, salt);
  const ct = await crypto.subtle.encrypt({ name: "AES-GCM", iv }, key, enc.encode(plaintext));
  return {
    encrypted: true,
    algorithm: "AES-GCM-256",
    kdf: "PBKDF2-SHA256",
    kdf_iterations: PBKDF2_ITERATIONS,
    salt: bytesToBase64(salt),
    iv: bytesToBase64(iv),
    ciphertext: bytesToBase64(ct),
  };
}

// ---------------------------------------------------------------------------

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
  const cfg = await ext.storage.local.get([
    "endpoint", "secret", "encryption_enabled", "encryption_passphrase",
  ]);
  const { endpoint, secret } = cfg;
  if (!endpoint || !secret) {
    return { ok: false, error: "endpoint and secret not configured" };
  }
  if (cfg.encryption_enabled && !cfg.encryption_passphrase) {
    return { ok: false, error: "encryption enabled but no passphrase set" };
  }

  const tab = await getActiveTab();
  if (!tab || !tab.id) return { ok: false, error: "no active tab" };
  if (tab.url && /^(chrome|about|edge|moz-extension|chrome-extension):/i.test(tab.url)) {
    return { ok: false, error: "cannot clip browser internal pages" };
  }

  // PDF path: don't try to inject; send the URL for server-side download.
  if (isPdfTab(tab)) {
    const title = (tab.title || tab.url || "document").trim();
    const filename = `${isoFilename(new Date())}-${slugify(title)}.md`;
    const body = {
      filename,
      pdf_url: tab.url,
      title,
      tags: userTags || [],
    };
    let resp, data;
    try {
      ({ resp, data } = await postSigned(endpoint, secret, body));
    } catch (e) {
      return { ok: false, error: "network: " + e.message };
    }
    if (!resp.ok) {
      const msg = (data && (data.error || data.message)) || `HTTP ${resp.status}`;
      return { ok: false, error: msg };
    }
    return { ok: true, file: (data && data.file) || filename, pdf: true };
  }

  let extracted;
  try {
    extracted = await runExtraction(tab.id, userTags);
  } catch (e) {
    return { ok: false, error: "injection failed: " + e.message };
  }
  if (!extracted) return { ok: false, error: "no result from page" };
  if (extracted.error) return { ok: false, error: extracted.error };

  // Build the wire body, optionally encrypting the content.
  let body;
  if (cfg.encryption_enabled) {
    const blob = await encryptPayload(extracted.content, cfg.encryption_passphrase);
    body = { filename: extracted.filename, ...blob };
  } else {
    body = { filename: extracted.filename, content: extracted.content };
  }

  let resp, data;
  try {
    ({ resp, data } = await postSigned(endpoint, secret, body));
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
    encrypted: !!cfg.encryption_enabled,
  };
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
