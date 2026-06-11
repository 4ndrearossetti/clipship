#!/usr/bin/env python3
"""Interactive Clipship setup wizard.

Walks the user through:
  1. Deployment mode  — local (single machine) or remote (production).
  2. PDF extractor    — none / pypdf / opendataloader.
  3. Web UI           — optional, with generated password.
  4. SECRET_KEY       — generated or pasted.
  5. OUTPUT_DIR       — inbox folder, created if missing.

Then writes config.py, creates a venv if one is missing, installs only the
dependencies needed for the choices above, and prints the endpoint URL and
secret you paste into the extension.

Runs on stdlib only so it works before any pip install. Targets Python 3.10+.
"""
from __future__ import annotations

import json
import os
import secrets
import shutil
import subprocess
import sys
import textwrap
from pathlib import Path

SERVER_DIR = Path(__file__).resolve().parent
REPO_ROOT = SERVER_DIR.parent
CONFIG_PATH = SERVER_DIR / "config.py"
EXAMPLE_PATH = SERVER_DIR / "config.py.example"
VENV_DIR = SERVER_DIR / "venv"


def heading(s: str) -> None:
    print()
    print(s)
    print("-" * len(s))


def ask(prompt: str, default: str | None = None) -> str:
    suffix = f" [{default}]" if default is not None else ""
    while True:
        raw = input(f"{prompt}{suffix}: ").strip()
        if raw:
            return raw
        if default is not None:
            return default


def ask_choice(prompt: str, choices: list[tuple[str, str]], default_key: str) -> str:
    """choices = [(key, description), ...]; return chosen key."""
    print(prompt)
    keys = []
    for i, (key, desc) in enumerate(choices, 1):
        marker = "*" if key == default_key else " "
        print(f"  {i}) {marker} {key:<16} {desc}")
        keys.append(key)
    while True:
        raw = input(f"Choose 1-{len(choices)} [{default_key}]: ").strip().lower()
        if not raw:
            return default_key
        if raw.isdigit() and 1 <= int(raw) <= len(choices):
            return keys[int(raw) - 1]
        if raw in keys:
            return raw
        print("  ↳ not a valid option, try again.")


def ask_yes_no(prompt: str, default: bool) -> bool:
    d = "Y/n" if default else "y/N"
    while True:
        raw = input(f"{prompt} [{d}]: ").strip().lower()
        if not raw:
            return default
        if raw in ("y", "yes"):
            return True
        if raw in ("n", "no"):
            return False


def have_java_11_plus() -> bool:
    if not shutil.which("java"):
        return False
    try:
        out = subprocess.run(
            ["java", "-version"], capture_output=True, text=True, timeout=5
        )
        # `java -version` prints to stderr.
        blob = (out.stderr or "") + (out.stdout or "")
        # Lines look like:  openjdk version "21.0.2" 2024-01-16
        for line in blob.splitlines():
            if "version" in line and '"' in line:
                ver = line.split('"', 2)[1]
                major = int(ver.split(".", 1)[0])
                # Java 1.8 → "1.8.x"; anything ≥ 11 is fine.
                if major == 1:
                    return False
                return major >= 11
    except Exception:
        return False
    return False


def render_config(values: dict) -> str:
    """Patch config.py.example with our chosen values.

    Stays line-based so comments and structure from the example are preserved.
    String values go through json.dumps so that quotes, backslashes, and
    non-ASCII characters in user-supplied passwords don't corrupt config.py.
    """
    text = EXAMPLE_PATH.read_text(encoding="utf-8")

    def pystr(s: str) -> str:
        # json string literals are valid Python string literals.
        return json.dumps(s, ensure_ascii=False)

    overrides = {
        "SECRET_KEY":          pystr(values["secret"]),
        "OUTPUT_DIR":          pystr(values["output_dir"]),
        "HOST":                pystr(values["host"]),
        "PORT":                str(values["port"]),
        "PDF_EXTRACTOR":       pystr(values["pdf_extractor"]),
        "EXTRACT_PDF_TEXT":    "True" if values["pdf_extractor"] != "none" else "False",
        "WEB_UI_ENABLED":      "True" if values["web_ui"] else "False",
        "WEB_UI_USERNAME":     pystr(values["web_user"]),
        "WEB_UI_PASSWORD":     pystr(values["web_password"]),
    }

    out_lines = []
    for line in text.splitlines():
        stripped = line.lstrip()
        replaced = False
        for key, new_val in overrides.items():
            if stripped.startswith(key + " ") or stripped.startswith(key + "="):
                indent = line[: len(line) - len(stripped)]
                out_lines.append(f"{indent}{key} = {new_val}")
                replaced = True
                break
        if not replaced:
            out_lines.append(line)
    return "\n".join(out_lines) + "\n"


def ensure_venv() -> Path:
    """Return path to venv pip, creating the venv if needed."""
    pip_bin = VENV_DIR / ("Scripts" if os.name == "nt" else "bin") / "pip"
    if pip_bin.exists():
        print(f"  ↳ reusing venv at {VENV_DIR}")
        return pip_bin
    print(f"  ↳ creating venv at {VENV_DIR}")
    subprocess.check_call([sys.executable, "-m", "venv", str(VENV_DIR)])
    return pip_bin


def pip_install(pip_bin: Path, args: list[str]) -> None:
    print(f"  ↳ pip install {' '.join(args)}")
    subprocess.check_call([str(pip_bin), "install", *args])


def _verify_written_config(expected_secret: str,
                           expected_user: str | None,
                           expected_password: str | None) -> bool:
    """Import config.py in a fresh subprocess and check the values match.

    Uses a subprocess so we don't pollute (or get fooled by) the wizard's own
    sys.modules. Prints a one-line OK or the offending mismatch.
    """
    probe = (
        "import json, sys, importlib.util\n"
        f"spec = importlib.util.spec_from_file_location('config', {str(CONFIG_PATH)!r})\n"
        "m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)\n"
        "print(json.dumps({\n"
        "  'secret':   getattr(m,'SECRET_KEY',''),\n"
        "  'user':     getattr(m,'WEB_UI_USERNAME',''),\n"
        "  'password': getattr(m,'WEB_UI_PASSWORD',''),\n"
        "  'enabled':  bool(getattr(m,'WEB_UI_ENABLED', False)),\n"
        "}))\n"
    )
    try:
        out = subprocess.run(
            [sys.executable, "-c", probe],
            capture_output=True, text=True, timeout=10, check=True,
        )
    except subprocess.CalledProcessError as e:
        print(f"  ✗ config.py failed to import: {e.stderr.strip()}")
        return False
    try:
        got = json.loads(out.stdout)
    except json.JSONDecodeError:
        print(f"  ✗ probe produced no JSON: {out.stdout!r}")
        return False

    if got["secret"] != expected_secret:
        print(f"  ✗ SECRET_KEY mismatch: wrote {expected_secret!r}, file has {got['secret']!r}")
        return False
    if expected_user is not None and got["user"] != expected_user:
        print(f"  ✗ WEB_UI_USERNAME mismatch: wrote {expected_user!r}, file has {got['user']!r}")
        return False
    if expected_password is not None and got["password"] != expected_password:
        print(f"  ✗ WEB_UI_PASSWORD mismatch: wrote {expected_password!r}, file has {got['password']!r}")
        return False
    print("  ↳ verified: server will read back the exact values printed below.")
    return True


def main() -> int:
    print(textwrap.dedent("""
        ┌──────────────────────────────────────────────┐
        │  Clipship setup                              │
        │  Writes config.py, installs deps, prints the │
        │  endpoint + secret you paste in the popup.   │
        └──────────────────────────────────────────────┘
    """).rstrip())

    if not EXAMPLE_PATH.exists():
        print(f"\nERROR: {EXAMPLE_PATH} is missing — run this from the cloned repo.")
        return 1

    if CONFIG_PATH.exists():
        if not ask_yes_no(
            f"\nconfig.py already exists at {CONFIG_PATH}. Overwrite?", default=False
        ):
            print("Aborted — nothing changed.")
            return 1

    # --- 1. Deployment mode -------------------------------------------------
    heading("1. Deployment mode")
    mode = ask_choice(
        "Where will this Clipship instance run?",
        [
            ("local",  "Single machine — extension talks to 127.0.0.1, no TLS."),
            ("remote", "Production server behind nginx/Caddy + TLS + systemd."),
        ],
        default_key="local",
    )

    # --- 2. PDF extractor ---------------------------------------------------
    heading("2. PDF text extraction")
    java_ok = have_java_11_plus()
    odl_label = "Java-backed, structured Markdown (tables, headings)."
    if not java_ok:
        odl_label += " ⚠  Java 11+ NOT detected — you'll need to install it."
    pdf_extractor = ask_choice(
        "How should the server extract text from clipped PDFs?",
        [
            ("pypdf",          "Pure Python, fast, plain text. (Recommended.)"),
            ("opendataloader", odl_label),
            ("none",           "Don't extract — just store and link the PDF."),
        ],
        default_key="pypdf",
    )

    # --- 3. Inbox + host/port ----------------------------------------------
    heading("3. Inbox folder + bind address")
    default_inbox = (
        str(Path.home() / "clipship-inbox")
        if mode == "local"
        else "/var/lib/clipship/inbox"
    )
    output_dir = ask("Folder where clipped Markdown files will be written", default_inbox)
    output_dir = str(Path(output_dir).expanduser().resolve())

    default_host = "127.0.0.1"  # safe in both modes — remote uses a proxy
    host = ask("Bind address (keep 127.0.0.1 unless you know why)", default_host)
    port = ask("Port", "5050")

    # --- 4. Web UI ----------------------------------------------------------
    heading("4. Web UI (optional, read-only browser for the inbox)")
    web_ui = ask_yes_no("Enable the web UI?", default=False)
    web_user = "admin"
    web_password = ""
    if web_ui:
        web_user = ask("Web UI username", "admin")
        web_password = ask("Web UI password (leave blank to generate one)", "")
        if not web_password:
            web_password = secrets.token_urlsafe(24)
            print(f"  ↳ generated password: {web_password}")

    # --- 5. Secret ----------------------------------------------------------
    heading("5. Shared secret (HMAC key for the extension)")
    generate = ask_yes_no("Generate a fresh secret now?", default=True)
    secret = (
        secrets.token_hex(32) if generate else ask("Paste a 64-hex-char secret", "")
    )
    if len(secret) < 32:
        print("ERROR: secret looks too short — aborting.")
        return 1

    # --- 6. Write config ----------------------------------------------------
    heading("6. Writing config.py")
    Path(output_dir).mkdir(parents=True, exist_ok=True)
    print(f"  ↳ ensured inbox exists: {output_dir}")
    config_text = render_config({
        "secret":         secret,
        "output_dir":     output_dir,
        "host":           host,
        "port":           int(port),
        "pdf_extractor":  pdf_extractor,
        "web_ui":         web_ui,
        "web_user":       web_user,
        "web_password":   web_password,
    })
    CONFIG_PATH.write_text(config_text, encoding="utf-8")
    print(f"  ↳ wrote {CONFIG_PATH}")

    # Verify the file we just wrote actually parses and the credentials match
    # what we intended — catches any quoting/encoding mishap before the user
    # discovers it as a 401/403 in the browser.
    if not _verify_written_config(secret, web_user if web_ui else None,
                                  web_password if web_ui else None):
        print("ERROR: written config.py did not round-trip. Aborting before deps.")
        return 1

    # --- 7. Install dependencies -------------------------------------------
    heading("7. Installing dependencies")
    pip_bin = ensure_venv()
    pip_install(pip_bin, ["-r", str(SERVER_DIR / "requirements.txt")])
    if pdf_extractor == "pypdf":
        pip_install(pip_bin, ["-r", str(SERVER_DIR / "requirements-extras.txt")])
    elif pdf_extractor == "opendataloader":
        pip_install(pip_bin, ["-r", str(SERVER_DIR / "requirements-opendataloader.txt")])
        if not java_ok:
            print(
                "\n  ⚠  Java 11+ was not detected on PATH. Install it before clipping\n"
                "     PDFs, e.g.:  sudo apt install default-jre-headless"
            )

    # --- 8. Confirmation ----------------------------------------------------
    print()
    print("=" * 60)
    print("  Setup complete.")
    print("=" * 60)
    endpoint = (
        f"http://{host}:{port}/clip"
        if mode == "local"
        else f"https://<your-domain>/clip"
    )
    # Each copy-paste value goes on its own line with no leading whitespace,
    # so triple-click in a terminal selects exactly the value.
    print()
    print("  Endpoint URL (paste in the extension):")
    print(endpoint)
    print()
    print("  Shared secret (paste in the extension):")
    print(secret)
    if web_ui:
        print()
        print(f"  Web UI URL:  http://{host}:5051/")
        print(f"  Web UI user: {web_user}")
        print( "  Web UI password:")
        print(web_password)
    print()
    print(f"  Inbox folder:    {output_dir}")
    print(f"  PDF extractor:   {pdf_extractor}")
    print()
    if mode == "remote":
        # Most common gotcha after editing config.py: the running daemon still
        # has the old secret in memory, so clipping and the web UI continue
        # to 401/403 until the service is restarted.
        print("  ⚠  IMPORTANT: config.py was rewritten. If clipship is already")
        print("     running under systemd, restart it now or the new SECRET_KEY")
        print("     and WEB_UI_PASSWORD will NOT take effect:")
        print()
        print("       sudo systemctl restart clipship clipship-web")
        print()
        print("     Then re-open the extension popup, paste the secret printed")
        print("     above into the secret field, and click Save.")
        print()
        print("     You also need nginx/Caddy in front for TLS and the systemd")
        print("     units — see docs/setup.md sections 4 and 5.")
    else:
        print("  Start the receiver with:")
        print(f"    {VENV_DIR / ('Scripts' if os.name == 'nt' else 'bin') / 'python'} "
              f"{SERVER_DIR / 'receiver.py'}")
        print()
        print("  Then re-open the extension popup, paste the secret above,")
        print("  and click Save. If you previously saved a different secret,")
        print("  the new one only takes effect after you click Save again.")
    print()
    return 0


def check_existing_config() -> int:
    """Re-print what the server will load from the current config.py.

    Handy for diagnosing "my secret looks right but auth fails": this shows
    the exact SECRET_KEY / WEB_UI_USERNAME / WEB_UI_PASSWORD the server sees,
    delimited so trailing whitespace or invisible characters are obvious.
    """
    if not CONFIG_PATH.exists():
        print(f"No config.py at {CONFIG_PATH}")
        return 1
    probe = (
        "import json, importlib.util\n"
        f"spec = importlib.util.spec_from_file_location('config', {str(CONFIG_PATH)!r})\n"
        "m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)\n"
        "print(json.dumps({\n"
        "  'SECRET_KEY':      getattr(m,'SECRET_KEY',''),\n"
        "  'WEB_UI_ENABLED':  bool(getattr(m,'WEB_UI_ENABLED', False)),\n"
        "  'WEB_UI_USERNAME': getattr(m,'WEB_UI_USERNAME',''),\n"
        "  'WEB_UI_PASSWORD': getattr(m,'WEB_UI_PASSWORD',''),\n"
        "  'HOST':            getattr(m,'HOST',''),\n"
        "  'PORT':            getattr(m,'PORT',0),\n"
        "}))\n"
    )
    try:
        out = subprocess.run(
            [sys.executable, "-c", probe],
            capture_output=True, text=True, timeout=10, check=True,
        )
    except subprocess.CalledProcessError as e:
        print(f"config.py failed to import:\n{e.stderr.strip()}")
        return 1
    got = json.loads(out.stdout)
    print("What the server will load from config.py:")
    for k, v in got.items():
        print(f"  {k} = [{v!r}]   len={len(str(v))}")
    print("\nIf the value in the brackets differs from what you pasted into the")
    print("extension (whitespace, smart quotes, missing chars), that's the bug.")
    return 0


if __name__ == "__main__":
    try:
        if "--check" in sys.argv[1:]:
            sys.exit(check_existing_config())
        sys.exit(main())
    except KeyboardInterrupt:
        print("\nAborted.")
        sys.exit(130)
