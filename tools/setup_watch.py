"""Install the watch app and point it at this PC. Run from the repo root, any Python 3.10+:

    python tools/setup_watch.py                       # Wi-Fi mode: http://<this PC's address>:8808
    python tools/setup_watch.py --url https://in.example.com    # tunnel mode
    python tools/setup_watch.py --config-only         # app already installed, just rewrite its config

Before running: on the watch turn on Developer options > Wireless debugging and connect it
(`adb pair <ip>:<port> <code>` once, then `adb connect <ip>:<port>`). See SETUP.md step 5.

What it does: installs the APK, grants the microphone and notification permissions, writes
files/config.json into the app's private storage (server address + upload token), opens the app.
Nothing is typed on the watch.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import socket
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PKG = "com.leveledai.voicecapture.open"
ACTIVITY = f"{PKG}/com.leveledai.voicecapture.MainActivity"
APK_PATHS = [ROOT / "dist" / "voice-capture-watch.apk",
             ROOT / "watch" / "app" / "build" / "outputs" / "apk" / "debug" / "app-debug.apk"]


def env_file() -> dict:
    out = {}
    p = ROOT / ".env"
    if p.exists():
        for ln in p.read_text(encoding="utf-8").splitlines():
            if "=" in ln and not ln.lstrip().startswith("#"):
                k, v = ln.split("=", 1)
                out[k.strip()] = v.strip()
    return out


def find_adb() -> str:
    exe = shutil.which("adb")
    if exe:
        return exe
    for base in (os.environ.get("ANDROID_HOME"), os.environ.get("ANDROID_SDK_ROOT"),
                 os.path.join(os.environ.get("LOCALAPPDATA", ""), "Android", "Sdk")):
        if base:
            cand = Path(base) / "platform-tools" / ("adb.exe" if os.name == "nt" else "adb")
            if cand.exists():
                return str(cand)
    sys.exit("adb not found. Install Android platform-tools (winget install Google.PlatformTools) and try again.")


def lan_ip() -> str:
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("10.255.255.255", 1))     # no packet is sent; this only picks the outgoing interface
        return s.getsockname()[0]
    except OSError:
        return "127.0.0.1"
    finally:
        s.close()


def main() -> None:
    ap = argparse.ArgumentParser(description="Install and configure the Voice Capture watch app")
    ap.add_argument("--url", help="server address the watch uploads to (default: this PC on the home network)")
    ap.add_argument("--serial", help="adb device serial, when more than one device is connected")
    ap.add_argument("--apk", help="path to the APK (default: dist/ or the gradle build output)")
    ap.add_argument("--device-id", default="galaxy-watch", help="name stored with each recording")
    ap.add_argument("--config-only", action="store_true", help="skip the install, only rewrite the config")
    a = ap.parse_args()

    env = env_file()
    token = env.get("INGEST_TOKEN")
    if not token:
        sys.exit("No INGEST_TOKEN in .env yet. Run `python launch.py token` first.")
    url = (a.url or f"http://{lan_ip()}:{env.get('PORT') or 8808}").rstrip("/")
    if "127.0.0.1" in url and not a.url:
        sys.exit("Could not find this PC's network address. Pass it yourself: --url http://192.168.x.x:8808")

    adb = find_adb()
    serial = a.serial
    if not serial:
        rows = [ln.split() for ln in subprocess.run([adb, "devices"], capture_output=True, text=True).stdout.splitlines()[1:]]
        ready = [r[0] for r in rows if len(r) >= 2 and r[1] == "device"]
        # A wirelessly connected watch often shows twice (ip:port and an mDNS name). Prefer ip:port.
        ip_ports = [d for d in ready if d.count(".") == 3 and ":" in d]
        if len(ip_ports) == 1:
            ready = ip_ports
        if not ready:
            sys.exit("No watch connected. On the watch: Developer options > Wireless debugging, then "
                     "`adb pair <ip>:<port> <code>` once and `adb connect <ip>:<port>`.")
        if len(ready) > 1:
            sys.exit(f"More than one device connected ({', '.join(ready)}). Pick one with --serial.")
        serial = ready[0]

    def run(*args: str, stdin: str | None = None, check: bool = True) -> str:
        p = subprocess.run([adb, "-s", serial, *args], input=stdin, capture_output=True, text=True)
        if check and p.returncode != 0:
            sys.exit(f"adb {' '.join(args)} failed: {(p.stderr or p.stdout).strip()}")
        return (p.stdout or "").strip()

    if not a.config_only:
        apk = Path(a.apk) if a.apk else next((p for p in APK_PATHS if p.exists()), None)
        if not apk or not apk.exists():
            sys.exit("No APK found. Download voice-capture-watch.apk from the Releases page into dist/, "
                     "or build it: cd watch && gradlew.bat assembleDebug")
        print(f"Installing {apk.name} on {serial} ...")
        run("install", "-r", str(apk))
        for perm in ("RECORD_AUDIO", "POST_NOTIFICATIONS"):
            run("shell", "pm", "grant", PKG, f"android.permission.{perm}", check=False)

    cfg = {"server_url": url, "ingest_token": token, "device_id": a.device_id}
    if env.get("CF_ACCESS_CLIENT_ID") and env.get("CF_ACCESS_CLIENT_SECRET"):   # Cloudflare Access service token
        cfg["cf_client_id"], cfg["cf_client_secret"] = env["CF_ACCESS_CLIENT_ID"], env["CF_ACCESS_CLIENT_SECRET"]
    run("shell", f"run-as {PKG} sh -c 'mkdir -p files && cat > files/config.json && chmod 600 files/config.json'",
        stdin=json.dumps(cfg))
    run("shell", "am", "force-stop", PKG, check=False)
    run("shell", "am", "start", "-n", ACTIVITY)
    print(json.dumps({"ok": True, "watch": serial, "server_url": url,
                      "next": "Open Voice Capture on the watch, tap Record, say a few words, tap Stop. "
                              "The conversation shows up in the dashboard within a minute or two."}, indent=2))


if __name__ == "__main__":
    main()
