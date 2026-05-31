// Cross-browser shim: prefer `browser` (Firefox), fall back to `chrome`.
const ext = (typeof browser !== "undefined") ? browser : chrome;

const $ = (id) => document.getElementById(id);
const clipView = $("clip-view");
const configView = $("config-view");
const statusEl = $("status");
const configStatusEl = $("config-status");

function setStatus(el, msg, kind) {
  el.textContent = msg || "";
  el.className = kind || "";
}

async function loadConfig() {
  const cfg = await ext.storage.local.get([
    "endpoint", "secret", "encryption_enabled", "encryption_passphrase",
  ]);
  $("endpoint").value = cfg.endpoint || "";
  $("secret").value = cfg.secret || "";
  $("encryption-enabled").checked = !!cfg.encryption_enabled;
  $("encryption-passphrase").value = cfg.encryption_passphrase || "";
  $("encryption-passphrase-row").classList.toggle("hidden", !cfg.encryption_enabled);
  return cfg;
}

function showConfig() {
  clipView.classList.add("hidden");
  configView.classList.remove("hidden");
  setStatus(configStatusEl, "", "");
}

function showClip() {
  configView.classList.add("hidden");
  clipView.classList.remove("hidden");
}

$("toggle-config").addEventListener("click", async () => {
  await loadConfig();
  if (configView.classList.contains("hidden")) showConfig();
  else showClip();
});

$("cancel-btn").addEventListener("click", showClip);

$("encryption-enabled").addEventListener("change", (e) => {
  $("encryption-passphrase-row").classList.toggle("hidden", !e.target.checked);
});

$("save-btn").addEventListener("click", async () => {
  const endpoint = $("endpoint").value.trim();
  const secret = $("secret").value.trim();
  const encryption_enabled = $("encryption-enabled").checked;
  const encryption_passphrase = $("encryption-passphrase").value;
  if (!endpoint || !secret) {
    setStatus(configStatusEl, "Endpoint and secret are required.", "err");
    return;
  }
  if (encryption_enabled && encryption_passphrase.length < 8) {
    setStatus(configStatusEl, "Encryption passphrase must be at least 8 characters.", "err");
    return;
  }
  let originPattern;
  try {
    originPattern = new URL(endpoint).origin + "/*";
  } catch {
    setStatus(configStatusEl, "Endpoint must be a valid URL.", "err");
    return;
  }

  // Request host permission for the endpoint so fetch() bypasses CORS.
  // This call requires a user gesture, which the Save click provides.
  let granted = true;
  try {
    granted = await ext.permissions.request({ origins: [originPattern] });
  } catch (e) {
    setStatus(configStatusEl, "Permission request failed: " + e.message, "err");
    return;
  }
  if (!granted) {
    setStatus(configStatusEl, "Host permission denied — clips will be blocked by CORS.", "err");
    return;
  }

  await ext.storage.local.set({
    endpoint,
    secret,
    encryption_enabled,
    encryption_passphrase: encryption_enabled ? encryption_passphrase : "",
  });
  setStatus(configStatusEl, "Saved.", "ok");
  setTimeout(showClip, 600);
});

$("clip-btn").addEventListener("click", async () => {
  const cfg = await loadConfig();
  if (!cfg.endpoint || !cfg.secret) {
    setStatus(statusEl, "Configure endpoint and secret first.", "err");
    showConfig();
    return;
  }

  const userTags = ($("tags").value || "")
    .split(",")
    .map(s => s.trim())
    .filter(Boolean);

  $("clip-btn").disabled = true;
  try {
    setStatus(statusEl, "Extracting…", "");
    const response = await ext.runtime.sendMessage({ type: "clip", tags: userTags });
    if (response && response.ok) {
      let msg = response.pdf ? `Saved PDF: ${response.file}` : `Saved: ${response.file}`;
      if (response.encrypted) msg += " 🔒";
      if (response.assets_downloaded) {
        msg += ` (+${response.assets_downloaded} image${response.assets_downloaded === 1 ? "" : "s"})`;
      }
      if (response.assets_failed) {
        msg += ` — ${response.assets_failed} image${response.assets_failed === 1 ? "" : "s"} failed`;
      }
      setStatus(statusEl, msg, "ok");
      $("tags").value = "";
    } else {
      setStatus(statusEl, `Error: ${response?.error || "unknown"}`, "err");
    }
  } catch (err) {
    setStatus(statusEl, `Error: ${err.message}`, "err");
  } finally {
    $("clip-btn").disabled = false;
  }
});

// First run: if no config saved, open the config panel automatically.
(async () => {
  const cfg = await loadConfig();
  if (!cfg.endpoint || !cfg.secret) showConfig();
})();
