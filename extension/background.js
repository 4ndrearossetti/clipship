// Service worker: orchestrates extraction + HMAC signing + POST.

const ext = (typeof browser !== "undefined") ? browser : chrome;

async function getActiveTab() {
  const [tab] = await ext.tabs.query({ active: true, currentWindow: true });
  return tab;
}

async function runExtraction(tabId) {
  // Inject Readability first, then the extractor. The extractor stores its
  // result on window and the final script returns it.
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

async function clipCurrentTab() {
  const { endpoint, secret } = await ext.storage.local.get(["endpoint", "secret"]);
  if (!endpoint || !secret) {
    return { ok: false, error: "endpoint and secret not configured" };
  }

  const tab = await getActiveTab();
  if (!tab || !tab.id) return { ok: false, error: "no active tab" };
  if (tab.url && /^(chrome|about|edge|moz-extension|chrome-extension):/i.test(tab.url)) {
    return { ok: false, error: "cannot clip browser internal pages" };
  }

  let extracted;
  try {
    extracted = await runExtraction(tab.id);
  } catch (e) {
    return { ok: false, error: "injection failed: " + e.message };
  }
  if (!extracted) return { ok: false, error: "no result from page" };
  if (extracted.error) return { ok: false, error: extracted.error };

  const body = JSON.stringify({
    filename: extracted.filename,
    content: extracted.content,
  });
  const timestamp = Math.floor(Date.now() / 1000).toString();
  const signature = await hmacSha256Hex(secret, timestamp + "." + body);

  let resp;
  try {
    resp = await fetch(endpoint, {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        "X-Clipship-Timestamp": timestamp,
        "X-Clipship-Signature": signature,
      },
      body,
    });
  } catch (e) {
    return { ok: false, error: "network: " + e.message };
  }

  let data = null;
  try { data = await resp.json(); } catch { /* ignore */ }

  if (!resp.ok) {
    const msg = (data && (data.error || data.message)) || `HTTP ${resp.status}`;
    return { ok: false, error: msg };
  }
  return { ok: true, file: (data && data.file) || extracted.filename };
}

ext.runtime.onMessage.addListener((msg, _sender, sendResponse) => {
  if (msg && msg.type === "clip") {
    clipCurrentTab().then(sendResponse).catch((e) =>
      sendResponse({ ok: false, error: e.message })
    );
    return true; // async response
  }
  return false;
});
