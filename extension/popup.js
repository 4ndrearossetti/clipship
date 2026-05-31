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
    setStatus(configStatusEl, "Both fields are required.", "err");
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

  await ext.storage.local.set({ endpoint, secret });
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

  $("clip-btn").disabled = true;
  try {
    setStatus(statusEl, "Extracting…", "");
    const response = await ext.runtime.sendMessage({ type: "clip" });
    if (response && response.ok) {
      setStatus(statusEl, `Saved: ${response.file}`, "ok");
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
