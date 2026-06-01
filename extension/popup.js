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
  const { endpoint = "", secret = "" } = await ext.storage.local.get(["endpoint", "secret"]);
  $("endpoint").value = endpoint;
  $("secret").value = secret;
  return { endpoint, secret };
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

$("save-btn").addEventListener("click", async () => {
  const endpoint = $("endpoint").value.trim();
  const secret = $("secret").value.trim();
  if (!endpoint || !secret) {
    setStatus(configStatusEl, "Endpoint and secret are required.", "err");
    return;
  }
  let originPattern;
  try {
    originPattern = new URL(endpoint).origin + "/*";
  } catch {
    setStatus(configStatusEl, "Endpoint must be a valid URL.", "err");
    return;
  }

  // Save BEFORE requesting host permission. Chrome's permission dialog
  // steals focus from the popup and unloads it, so anything we tried to
  // persist after the prompt would be lost — and the next time the user
  // opened the popup the fields would be empty and they'd have to retype.
  // Storing first means the values survive the popup unload either way.
  await ext.storage.local.set({ endpoint, secret });

  // Request host permission so the service worker's fetch() bypasses CORS.
  // The Save click counts as the required user gesture.
  let granted = true;
  try {
    granted = await ext.permissions.request({ origins: [originPattern] });
  } catch (e) {
    setStatus(configStatusEl, "Saved, but permission request failed: " + e.message, "err");
    return;
  }
  if (!granted) {
    setStatus(
      configStatusEl,
      "Saved, but host permission was denied — clips will be blocked by CORS until you grant it.",
      "err",
    );
    return;
  }

  setStatus(configStatusEl, "Saved.", "ok");
  setTimeout(showClip, 600);
});

$("clip-btn").addEventListener("click", async () => {
  const { endpoint, secret } = await loadConfig();
  if (!endpoint || !secret) {
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
  const { endpoint, secret } = await loadConfig();
  if (!endpoint || !secret) showConfig();
})();
