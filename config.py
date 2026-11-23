"""Paths, secrets and tunables for Voice Capture.

Secrets and machine settings live in .env next to this file (gitignored). See .env.example.
Optional tunables (models, thresholds) can be overridden in config.json next to this file.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

from dotenv import load_dotenv

APP_DIR = Path(__file__).resolve().parent
DATA_DIR = APP_DIR / "data"
AUDIO_DIR = DATA_DIR / "audio"
ENROLL_DIR = DATA_DIR / "enroll"
DB_PATH = DATA_DIR / "voice.db"
STATIC_DIR = APP_DIR / "static"
ENV_PATH = APP_DIR / ".env"
TUNABLES_PATH = APP_DIR / "config.json"

load_dotenv(ENV_PATH)

PORT = int(os.environ.get("PORT") or 8808)
# 127.0.0.1 = this PC only (use with a tunnel). 0.0.0.0 = also reachable on your home network,
# which is what the watch needs in Wi-Fi mode.
BIND_HOST = (os.environ.get("BIND_HOST") or "127.0.0.1").strip()
# Wi-Fi mode: other devices may only reach the watch API unless this is 1.
DASHBOARD_ON_LAN = (os.environ.get("DASHBOARD_ON_LAN") or "").strip() == "1"
# Tunnel mode: the public hostname the watch uploads to. Requests on it can only reach /api/v1/*.
INGEST_HOST = (os.environ.get("INGEST_HOST") or "").strip().lower()

DEFAULTS = {
    "whisper_model": "large-v3-turbo",
    "language": "en",                 # null = auto-detect
    "device": "auto",                 # auto | cuda | cpu
    "compute_type": "float16",        # used on cuda; cpu always runs int8
    "batch_size": 16,
    "diarize_model": "pyannote/speaker-diarization-community-1",
    "speaker_match_threshold": 0.55,  # cosine similarity to a saved voiceprint
    "recording_stale_minutes": 120,   # process a recording whose last chunk never arrived
    "model_idle_unload_minutes": 10,
    "min_words_to_summarize": 25,
    "day_rebuild_debounce_minutes": 20,
    "keep_audio": False,              # delete audio once the transcript is saved
    "calendar_all_timed_events": False,  # true = every timed calendar event counts as a meeting
    "gpu_busy_percent": 50,           # "wait while the PC is busy": hold work at or above this GPU load...
    "cpu_busy_percent": 80,           # ...or this CPU load, sustained over a few seconds
    # Extra fields added to every request to an OpenAI-compatible endpoint, e.g. LM Studio's
    # idle unload: {"ttl": 300}. Leave empty for cloud services, which reject unknown fields.
    "summary_extra_body": {},
}


def tunables() -> dict:
    cfg = dict(DEFAULTS)
    if TUNABLES_PATH.exists():
        cfg.update(json.loads(TUNABLES_PATH.read_text(encoding="utf-8")))
    return cfg


def ingest_token() -> str:
    return (os.environ.get("INGEST_TOKEN") or "").strip()


def hf_token() -> str:
    return (os.environ.get("HF_TOKEN") or os.environ.get("HUGGINGFACE_TOKEN") or "").strip()


def summary_api_key() -> str:
    return (os.environ.get("SUMMARY_API_KEY") or "").strip()


def calendar_ics_url() -> str:
    return (os.environ.get("CALENDAR_ICS_URL") or "").strip()


def set_env_value(key: str, value: str) -> None:
    """Write KEY=value into .env (replacing an existing line) and into this process."""
    value = value.strip().replace("\n", "").replace("\r", "")
    lines = ENV_PATH.read_text(encoding="utf-8").splitlines() if ENV_PATH.exists() else []
    out, done = [], False
    for ln in lines:
        if ln.split("=", 1)[0].strip() == key:
            if not done:
                out.append(f"{key}={value}")
                done = True
        else:
            out.append(ln)
    if not done:
        out.append(f"{key}={value}")
    ENV_PATH.write_text("\n".join(out) + "\n", encoding="utf-8")
    os.environ[key] = value


def reload_env() -> None:
    """Pick up .env edits made by another process (the server writes keys, the worker reads them)."""
    load_dotenv(ENV_PATH, override=True)


def ensure_dirs() -> None:
    for d in (DATA_DIR, AUDIO_DIR, ENROLL_DIR):
        d.mkdir(parents=True, exist_ok=True)
