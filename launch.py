"""Start / stop / restart / status for Voice Capture (server + worker). Windows only.

    python launch.py start
    python launch.py stop
    python launch.py restart
    python launch.py status
    python launch.py token      # print the watch's INGEST_TOKEN (creates one if missing)

Any Python can run this file; server and worker always run on the app's .venv.
"""
import json
import os
import secrets
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

APP = Path(__file__).resolve().parent
ENV = APP / ".env"


def env_value(key: str, default: str) -> str:
    if ENV.exists():
        for ln in ENV.read_text(encoding="utf-8").splitlines():
            if ln.split("=", 1)[0].strip() == key and "=" in ln and ln.split("=", 1)[1].strip():
                return ln.split("=", 1)[1].strip()
    return default


PORT = int(env_value("PORT", "8808"))
HEALTH = f"http://127.0.0.1:{PORT}/api/health"
VENV_PY = APP / ".venv" / "Scripts" / "python.exe"
DATA = APP / "data"
PROCS = {"server": "server.py", "worker": "worker.py"}


def pid_file(name): return DATA / f"{name}.pid"
def log_file(name): return DATA / f"{name}.log"


def alive(pid: int) -> bool:
    out = subprocess.run(["tasklist", "/FI", f"PID eq {pid}", "/NH"], capture_output=True, text=True).stdout
    return str(pid) in out


def read_pid(name):
    try:
        pid = int(pid_file(name).read_text().strip())
        return pid if alive(pid) else None
    except Exception:
        return None


def health(timeout=2.0):
    try:
        with urllib.request.urlopen(HEALTH, timeout=timeout) as r:
            return json.loads(r.read().decode("utf-8"))
    except Exception:
        return None


def ensure_token() -> str:
    lines = ENV.read_text(encoding="utf-8").splitlines() if ENV.exists() else []
    for ln in lines:
        if ln.startswith("INGEST_TOKEN=") and ln.split("=", 1)[1].strip():
            return ln.split("=", 1)[1].strip()
    tok = secrets.token_urlsafe(32)
    lines.append(f"INGEST_TOKEN={tok}")
    ENV.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return tok


def start():
    DATA.mkdir(exist_ok=True)
    if not VENV_PY.exists():
        sys.exit(f"no venv at {VENV_PY}; see SETUP.md")
    ensure_token()
    flags = subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP
    env = {**os.environ, "PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8"}
    for name, script in PROCS.items():
        if read_pid(name):
            continue
        log = open(log_file(name), "a", encoding="utf-8")
        p = subprocess.Popen([str(VENV_PY), "-u", str(APP / script)], cwd=APP, stdout=log, stderr=subprocess.STDOUT,
                             creationflags=flags, env=env)
        pid_file(name).write_text(str(p.pid))
    for _ in range(60):
        h = health()
        if h and h["worker"]["alive"]:
            break
        time.sleep(1)
    status()


def stop():
    for name in PROCS:
        pid = read_pid(name)
        if pid:
            subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"], capture_output=True)
        pid_file(name).unlink(missing_ok=True)
    print(json.dumps({"ok": True, "state": "stopped"}))


def status():
    h = health()
    print(json.dumps({"ok": bool(h and h.get("ok")), "url": f"http://127.0.0.1:{PORT}",
                      "server_pid": read_pid("server"), "worker_pid": read_pid("worker"),
                      "health": h, "logs": [str(log_file(n)) for n in PROCS]}, indent=2))


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "status"
    if cmd == "start":
        start()
    elif cmd == "stop":
        stop()
    elif cmd == "restart":
        stop(); time.sleep(1); start()
    elif cmd == "token":
        print(ensure_token())
    else:
        status()
