"""SQLite store (WAL). The server and the worker are separate processes sharing this file.

A "conversation" is one watch recording: the watch closes a recording after ~60 s of silence
(auto mode) or when the button stops it (manual), so the recording boundary is the
conversation boundary. Chunks are ~2-minute audio files belonging to a recording.
"""

from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone

import config

SCHEMA = """
create table if not exists recordings (
  id              text primary key,          -- watch-generated uuid
  device_id       text,
  trigger         text not null default 'manual',   -- manual | auto | calendar | upload
  started_at      text not null,             -- iso utc
  ended_at        text,
  day             text,                      -- local YYYY-MM-DD of started_at
  last_idx        integer,                   -- idx of the chunk flagged is_last, if received
  status          text not null default 'receiving',
  error           text,
  attempts        integer not null default 0,
  duration_s      real,
  speech_s        real,
  words           integer,
  title           text,
  summary_json    text,
  summary_text    text,                      -- flattened summary, for search
  transcript_text text,                      -- "Name: text" lines, for search
  updated_at      text not null
);
create index if not exists recordings_day on recordings(day);
create index if not exists recordings_status on recordings(status);

create table if not exists chunks (
  id           integer primary key,
  recording_id text not null references recordings(id) on delete cascade,
  idx          integer not null,
  started_at   text,
  duration_s   real,
  path         text,
  sha256       text not null,
  bytes        integer,
  received_at  text not null,
  unique(recording_id, idx)
);

create table if not exists segments (
  id           integer primary key,
  recording_id text not null references recordings(id) on delete cascade,
  start_s      real not null,
  end_s        real not null,
  label        text,                         -- diarization label, e.g. SPEAKER_00
  text         text not null
);
create index if not exists segments_rec on segments(recording_id);

create table if not exists speakers (
  id         integer primary key,
  name       text not null unique,
  is_self    integer not null default 0,
  embedding  text,                           -- json float list, running mean
  samples    integer not null default 0,
  last_heard text,
  created_at text not null
);

create table if not exists recording_speakers (
  recording_id text not null references recordings(id) on delete cascade,
  label        text not null,
  display      text not null,                -- "Speaker A" until named
  speaker_id   integer references speakers(id) on delete set null,
  embedding    text,
  speech_s     real,
  primary key (recording_id, label)
);

create table if not exists days (
  day          text primary key,
  summary_json text,
  summary_text text,
  dirty        integer not null default 1,
  dirty_since  text,
  updated_at   text
);

create table if not exists jobs (
  id         integer primary key,
  kind       text not null,                  -- enroll
  payload    text not null,
  status     text not null default 'queued', -- queued | done | failed
  error      text,
  created_at text not null,
  done_at    text
);

create table if not exists kv (
  key   text primary key,
  value text not null
);
"""

DEFAULT_SETTINGS = {
    "auto": {
        "always_while_worn": False,
        "windows": {d: [] for d in ("mon", "tue", "wed", "thu", "fri", "sat", "sun")},
        "calendar": {"enabled": False, "lead_min": 2},   # needs CALENDAR_ICS_URL in .env
    },
    "upload": {"battery_saver": False},
    # Silence handling on the watch. auto_end_min ends an auto recording; manual_stop_min stops a
    # button-started one (0 = never). merge.gap_min joins recordings this close together (0 = off).
    "silence": {"auto_end_min": 2, "manual_stop_min": 10},
    "merge": {"gap_min": 10},
    # Which AI writes the summaries. provider: claude_subscription | codex_subscription | openai_compatible.
    # model "" = the provider's default. base_url is only used by openai_compatible. The API key lives in .env.
    # when: now (a minute after each conversation) | daily (one batch at daily_time) | manual (only on request).
    "summary": {"provider": "claude_subscription", "model": "", "base_url": "", "when": "now", "daily_time": "23:00"},
    # Hold transcription and local-model summaries while something else is loading the GPU or CPU.
    "system": {"pause_when_busy": False},
}


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def local_day(iso: str) -> str:
    return datetime.fromisoformat(iso).astimezone().strftime("%Y-%m-%d")


def connect() -> sqlite3.Connection:
    config.ensure_dirs()
    con = sqlite3.connect(config.DB_PATH, timeout=30, isolation_level=None)
    con.row_factory = sqlite3.Row
    con.execute("pragma journal_mode=wal")
    con.execute("pragma foreign_keys=on")
    con.execute("pragma busy_timeout=30000")
    return con


@contextmanager
def tx(con: sqlite3.Connection):
    con.execute("begin immediate")
    try:
        yield con
        con.execute("commit")
    except BaseException:
        con.execute("rollback")
        raise


def init() -> None:
    con = connect()
    con.executescript(SCHEMA)
    cols = {r["name"] for r in con.execute("pragma table_info(recordings)")}
    if "summary_requested" not in cols:   # "summarize this one now", for the daily and manual schedules
        con.execute("alter table recordings add column summary_requested integer not null default 0")
    con.close()


def get_kv(con, key: str, default=None):
    row = con.execute("select value from kv where key=?", (key,)).fetchone()
    return json.loads(row["value"]) if row else default


def set_kv(con, key: str, value) -> None:
    con.execute("insert into kv(key,value) values(?,?) on conflict(key) do update set value=excluded.value",
                (key, json.dumps(value)))


def settings(con) -> dict:
    s = get_kv(con, "settings")
    if not s:
        return json.loads(json.dumps(DEFAULT_SETTINGS))
    merged = json.loads(json.dumps(DEFAULT_SETTINGS))
    merged["auto"].update(s.get("auto", {}))
    merged["auto"]["windows"] = {**DEFAULT_SETTINGS["auto"]["windows"], **s.get("auto", {}).get("windows", {})}
    merged["auto"]["calendar"] = {**DEFAULT_SETTINGS["auto"]["calendar"], **s.get("auto", {}).get("calendar", {})}
    merged["upload"].update(s.get("upload", {}))
    merged["silence"].update(s.get("silence", {}))
    merged["merge"].update(s.get("merge", {}))
    merged["summary"].update(s.get("summary", {}))
    merged["system"].update(s.get("system", {}))
    return merged


def mark_day_dirty(con, day: str | None) -> None:
    if not day:
        return
    con.execute("""insert into days(day, dirty, dirty_since) values(?,1,?)
                   on conflict(day) do update set
                     dirty_since=case when days.dirty=1 then coalesce(days.dirty_since, excluded.dirty_since)
                                      else excluded.dirty_since end,
                     dirty=1""", (day, now_iso()))
