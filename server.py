"""Voice Capture server (default 127.0.0.1:8808).

Two audiences on one port:
  /api/v1/*  the watch. Always needs the bearer INGEST_TOKEN.
  everything else: the dashboard.

Two ways to run it (see SETUP.md):
  Wi-Fi mode   BIND_HOST=0.0.0.0. The watch uploads straight to this PC on the home network.
               Other devices can only reach /api/v1/*; the dashboard answers only on this PC
               unless DASHBOARD_ON_LAN=1.
  Tunnel mode  BIND_HOST=127.0.0.1 behind a tunnel (Cloudflare Tunnel + Access, Tailscale...).
               Set INGEST_HOST to the hostname the watch uploads to: requests on that hostname
               can never reach a dashboard route. Protecting the dashboard hostname is the
               tunnel's job (e.g. a Cloudflare Access policy).

Heavy work (transcription, diarization, summaries) happens in worker.py, never here.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import re
import shutil
import subprocess
import sys
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

import calendar_feed
import config
import db
import stitch
import store
import summarize

config.ensure_dirs()
db.init()
app = FastAPI(title="Voice Capture")

SAFE_ID = re.compile(r"^[A-Za-z0-9_-]{6,64}$")
AUDIO_EXT = {".ogg", ".opus", ".m4a", ".aac", ".wav", ".mp3", ".webm", ".flac", ".amr", ".3gp", ".mp4"}


def con():
    return db.connect()


# ---- host gate ---------------------------------------------------------------

LOOPBACK = {"127.0.0.1", "::1"}   # a local tunnel client (cloudflared) also connects from here

@app.middleware("http")
async def no_stale_ui(request: Request, call_next):
    """Dashboard files: always revalidate, so a redesign shows up on the next load (ETag keeps it cheap)."""
    resp = await call_next(request)
    if not request.url.path.startswith("/api/"):
        resp.headers["Cache-Control"] = "no-cache"
    return resp


@app.middleware("http")
async def host_gate(request: Request, call_next):
    host = (request.headers.get("host") or "").split(":")[0].lower()
    path = request.url.path
    if not path.startswith("/api/v1/"):
        if config.INGEST_HOST and host == config.INGEST_HOST:
            return JSONResponse({"detail": "not found"}, status_code=404)
        client = request.client.host if request.client else ""
        if client not in LOOPBACK and not config.DASHBOARD_ON_LAN:
            return JSONResponse({"detail": "the dashboard only answers on the PC itself"}, status_code=403)
    if path.startswith("/api/v1/"):
        token = config.ingest_token()
        got = (request.headers.get("authorization") or "").removeprefix("Bearer ").strip()
        if not token or not hmac.compare_digest(got, token):
            return JSONResponse({"detail": "bad token"}, status_code=401)
    return await call_next(request)


# ---- watch API ---------------------------------------------------------------

@app.get("/api/v1/ping")
def ping():
    return {"ok": True, "server_time": db.now_iso()}


def _store_chunk(c, *, recording_id: str, idx: int, is_last: bool, started_at: str, chunk_started_at: str | None,
                 duration_s: float | None, trigger: str, device_id: str | None, sha: str, src: Path, ext: str) -> dict:
    existing = c.execute("select sha256 from chunks where recording_id=? and idx=?", (recording_id, idx)).fetchone()
    if existing:
        src.unlink(missing_ok=True)
        if existing["sha256"] == sha:
            return {"ok": True, "duplicate": True, "sha256": sha}
        raise HTTPException(409, "chunk already stored with a different checksum")
    rec = c.execute("select status from recordings where id=?", (recording_id,)).fetchone()
    if rec and rec["status"] not in ("receiving", "waiting"):
        # Late chunk for a recording already processed (it went stale first): start a continuation.
        recording_id = f"{recording_id}-c{idx}"
        rec = c.execute("select status from recordings where id=?", (recording_id,)).fetchone()
    dest_dir = config.AUDIO_DIR / recording_id
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / f"{idx:05d}{ext}"
    shutil.move(str(src), dest)
    now = db.now_iso()
    with db.tx(c):
        if not rec:
            c.execute("""insert into recordings(id, device_id, trigger, started_at, day, status, updated_at)
                         values(?,?,?,?,?, 'receiving', ?)""",
                      (recording_id, device_id, trigger, started_at, db.local_day(started_at), now))
        c.execute("""insert into chunks(recording_id, idx, started_at, duration_s, path, sha256, bytes, received_at)
                     values(?,?,?,?,?,?,?,?)""",
                  (recording_id, idx, chunk_started_at, duration_s, str(dest), sha, dest.stat().st_size, now))
        if is_last:
            end = None
            if chunk_started_at and duration_s:
                end = (datetime.fromisoformat(chunk_started_at) + timedelta(seconds=duration_s)).isoformat(timespec="seconds")
            c.execute("update recordings set last_idx=?, ended_at=coalesce(?, ended_at) where id=?",
                      (idx, end, recording_id))
        c.execute("update recordings set updated_at=? where id=?", (now, recording_id))
    return {"ok": True, "duplicate": False, "sha256": sha, "recording_id": recording_id}


def _norm_iso(s: str) -> str:
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        raise HTTPException(422, f"bad timestamp {s!r}")
    if dt.tzinfo is None:
        dt = dt.astimezone()
    return dt.astimezone(timezone.utc).isoformat(timespec="seconds")


async def _save_upload(file: UploadFile) -> tuple[Path, str, str]:
    ext = Path(file.filename or "").suffix.lower() or ".ogg"
    if ext not in AUDIO_EXT:
        raise HTTPException(415, f"unsupported audio type {ext}")
    tmp = config.DATA_DIR / f"incoming-{uuid.uuid4().hex}{ext}"
    h = hashlib.sha256()
    with tmp.open("wb") as f:
        while blk := await file.read(1 << 20):
            h.update(blk)
            f.write(blk)
    return tmp, h.hexdigest(), ext


@app.post("/api/v1/chunks")
async def ingest_chunk(
    file: UploadFile = File(...),
    recording_id: str = Form(...),
    idx: int = Form(...),
    is_last: bool = Form(False),
    started_at: str = Form(...),
    chunk_started_at: str | None = Form(None),
    duration_s: float | None = Form(None),
    trigger: str = Form("manual"),
    device_id: str | None = Form(None),
    sha256: str = Form(...),
):
    if not SAFE_ID.match(recording_id) or idx < 0:
        raise HTTPException(422, "bad recording_id/idx")
    if trigger not in ("manual", "auto", "calendar"):
        raise HTTPException(422, "bad trigger")
    tmp, sha, ext = await _save_upload(file)
    if sha != sha256.lower():
        tmp.unlink(missing_ok=True)
        raise HTTPException(400, "checksum mismatch, resend")
    c = con()
    try:
        return _store_chunk(c, recording_id=recording_id, idx=idx, is_last=is_last, started_at=_norm_iso(started_at),
                            chunk_started_at=_norm_iso(chunk_started_at) if chunk_started_at else None,
                            duration_s=duration_s, trigger=trigger, device_id=device_id, sha=sha, src=tmp, ext=ext)
    finally:
        c.close()


@app.get("/api/v1/schedule")
def schedule():
    c = con()
    try:
        s = db.settings(c)
    finally:
        c.close()
    meetings, cal_error = [], None
    if s["auto"]["calendar"]["enabled"]:
        try:
            meetings = calendar_feed.meetings()
        except Exception as e:  # calendar trouble must never block the watch
            cal_error = str(e)[:200]
    return {"server_time": db.now_iso(), "tz_offset_min": int(datetime.now().astimezone().utcoffset().total_seconds() // 60),
            "auto": s["auto"], "upload": s["upload"], "silence": s["silence"], "meetings": meetings, "calendar_error": cal_error}


# ---- dashboard API -----------------------------------------------------------

@app.get("/api/health")
def health():
    c = con()
    try:
        hb = db.get_kv(c, "worker_heartbeat") or {}
        counts = {r["status"]: r["n"] for r in c.execute("select status, count(*) n from recordings group by status")}
        summary_cfg = db.settings(c)["summary"]
    finally:
        c.close()
    alive = bool(hb.get("at")) and (datetime.now(timezone.utc) - datetime.fromisoformat(hb["at"])).total_seconds() < 90
    summary_label, summary_problem = summarize.status(summary_cfg)
    problems = []
    if not alive:
        problems.append("worker is not running")
    if not config.hf_token():
        problems.append("no Hugging Face token: speaker labels are off")
    if summary_problem:
        problems.append(summary_problem)
    if not config.ingest_token():
        problems.append("no INGEST_TOKEN: the watch can't upload")
    if counts.get("failed"):
        problems.append(f"{counts['failed']} recording(s) failed")
    venv = Path(sys.prefix).resolve() == (config.APP_DIR / ".venv").resolve()
    if not venv:
        problems.append("server is running on the wrong Python")
    return {"ok": not problems, "problems": problems, "venv": venv,
            "worker": {"alive": alive, "last_tick": hb.get("at"), "state": hb.get("state") or "unknown"},
            "hf_token": bool(config.hf_token()), "summary": summary_label, "calendar": bool(config.calendar_ics_url()),
            "queue": {k: counts.get(k, 0) for k in ("receiving", "waiting", "transcribing", "summarizing", "failed")}}


@app.get("/api/days")
def days(limit: int = 60):
    c = con()
    try:
        rows = c.execute("""select r.day, count(*) n, coalesce(sum(r.speech_s),0) speech, d.summary_json
                            from recordings r left join days d on d.day=r.day
                            where r.status != 'empty' group by r.day order by r.day desc limit ?""", (limit,)).fetchall()
        return [{"day": r["day"], "conversations": r["n"], "speech_minutes": round(r["speech"] / 60, 1),
                 "headline": (json.loads(r["summary_json"]) or {}).get("headline") if r["summary_json"] else None}
                for r in rows]
    finally:
        c.close()


@app.get("/api/days/{day}")
def day_view(day: str):
    if not re.match(r"^\d{4}-\d{2}-\d{2}$", day):
        raise HTTPException(422, "bad day")
    c = con()
    try:
        d = c.execute("select summary_json from days where day=?", (day,)).fetchone()
        convs = []
        for r in c.execute("select * from recordings where day=? order by started_at", (day,)):
            s = json.loads(r["summary_json"]) if r["summary_json"] else None
            convs.append({"id": r["id"], "started_at": r["started_at"], "ended_at": r["ended_at"],
                          "duration_s": r["duration_s"], "trigger": r["trigger"], "status": r["status"],
                          "title": r["title"], "summary_short": (s or {}).get("summary"),
                          "speakers": [v["display"] for v in store.speaker_names(c, r["id"]).values()]})
        return {"day": day, "summary": json.loads(d["summary_json"]) if d and d["summary_json"] else None,
                "conversations": convs}
    finally:
        c.close()


@app.get("/api/conversations/{rec_id}")
def get_conversation(rec_id: str):
    c = con()
    try:
        conv = store.conversation(c, rec_id)
    finally:
        c.close()
    if not conv:
        raise HTTPException(404, "no such conversation")
    return conv


@app.post("/api/conversations/{rec_id}/speakers/{label}")
async def name_speaker(rec_id: str, label: str, request: Request):
    body = await request.json()
    c = con()
    try:
        rs = c.execute("select * from recording_speakers where recording_id=? and label=?", (rec_id, label)).fetchone()
        if not rs:
            raise HTTPException(404, "no such speaker in this conversation")
        with db.tx(c):
            if body.get("speaker_id"):
                sid = int(body["speaker_id"])
                if not c.execute("select 1 from speakers where id=?", (sid,)).fetchone():
                    raise HTTPException(404, "no such speaker")
            else:
                name = (body.get("name") or "").strip()[:60]
                if not name:
                    raise HTTPException(422, "name required")
                row = c.execute("select id from speakers where lower(name)=lower(?)", (name,)).fetchone()
                sid = row["id"] if row else c.execute(
                    "insert into speakers(name, created_at) values(?,?)", (name, db.now_iso())).lastrowid
            first_time = rs["speaker_id"] != sid
            c.execute("update recording_speakers set speaker_id=? where recording_id=? and label=?", (sid, rec_id, label))
            if first_time:
                store.fold_into_speaker(c, sid, rs["embedding"])
            store.refresh_transcript(c, rec_id)
            # Re-summarize with real names; the worker waits ~60 s so naming several speakers costs one call.
            c.execute("""update recordings set status='transcribed', updated_at=? where id=? and status in ('done','failed')""",
                      (db.now_iso(), rec_id))
        return store.conversation(c, rec_id)
    finally:
        c.close()


@app.post("/api/conversations/{rec_id}/resummarize")
def resummarize(rec_id: str):
    c = con()
    try:
        n = c.execute("""update recordings set status='transcribed', error=null, attempts=0, summary_requested=1,
                         updated_at=? where id=? and transcript_text is not null""", (db.now_iso(), rec_id)).rowcount
    finally:
        c.close()
    if not n:
        raise HTTPException(409, "nothing to summarize yet")
    return {"ok": True}


@app.post("/api/conversations/{rec_id}/merge-previous")
def merge_previous(rec_id: str):
    """Manual stitch: fold this conversation into the one right before it (same watch)."""
    c = con()
    try:
        r = c.execute("select * from recordings where id=?", (rec_id,)).fetchone()
        if not r:
            raise HTTPException(404, "no such conversation")
        busy = ("receiving", "waiting", "transcribing")
        if r["status"] in busy:
            raise HTTPException(409, "still being transcribed, try again in a minute")
        prev = c.execute("""select * from recordings where id != ? and started_at < ? and status not in
                            ('receiving','waiting','transcribing') order by started_at desc limit 1""",
                         (rec_id, r["started_at"])).fetchone()
        if not prev:
            raise HTTPException(409, "there is no earlier conversation to merge into")
        stitch.merge(c, prev["id"], rec_id)
        return {"ok": True, "into": prev["id"]}
    finally:
        c.close()


@app.delete("/api/conversations/{rec_id}")
def delete_conversation(rec_id: str):
    c = con()
    try:
        r = c.execute("select day from recordings where id=?", (rec_id,)).fetchone()
        if not r:
            raise HTTPException(404, "no such conversation")
        with db.tx(c):
            c.execute("delete from recordings where id=?", (rec_id,))
            db.mark_day_dirty(c, r["day"])
        shutil.rmtree(config.AUDIO_DIR / rec_id, ignore_errors=True)
        return {"ok": True}
    finally:
        c.close()


SEARCH_TERM_RE = re.compile(r'"([^"]+)"|(\S+)')


def _terms(q: str) -> list[str]:
    """Keyword terms: words, or "quoted phrases". Every term must appear (any order)."""
    return [(a or b).strip().lower() for a, b in SEARCH_TERM_RE.findall(q or "") if (a or b).strip()][:8]


@app.get("/api/search")
def search(q: str = "", date_from: str | None = None, date_to: str | None = None, speaker: int | None = None):
    """Keyword search over every conversation (title, summary, transcript). No AI.
    Returns each matching conversation with every matching transcript line (time + speaker)."""
    terms = [t for t in _terms(q) if len(t) >= 2]
    if not terms:
        return {"results": [], "terms": []}
    where = ["r.status not in ('receiving','waiting','transcribing','empty')"]
    args: list = []
    for t in terms:
        where.append("lower(coalesce(r.title,'') || ' ' || coalesce(r.summary_text,'') || ' ' || coalesce(r.transcript_text,'')) like ?")
        args.append(f"%{t}%")
    if date_from and re.match(r"^\d{4}-\d{2}-\d{2}$", date_from):
        where.append("r.day >= ?"); args.append(date_from)
    if date_to and re.match(r"^\d{4}-\d{2}-\d{2}$", date_to):
        where.append("r.day <= ?"); args.append(date_to)
    if speaker:
        where.append("exists (select 1 from recording_speakers rs where rs.recording_id = r.id and rs.speaker_id = ?)")
        args.append(int(speaker))
    c = con()
    try:
        rows = c.execute(f"""select r.id, r.day, r.started_at, r.duration_s, r.trigger, r.title, r.summary_text
                             from recordings r where {' and '.join(where)}
                             order by r.started_at desc limit 100""", args).fetchall()
        out = []
        for r in rows:
            names = store.speaker_names(c, r["id"])
            t0 = datetime.fromisoformat(r["started_at"])
            hits = []
            for sg in c.execute("select start_s, label, text from segments where recording_id=? order by start_s", (r["id"],)):
                low = sg["text"].lower()
                if any(t in low for t in terms):
                    hits.append({"at": (t0 + timedelta(seconds=sg["start_s"])).isoformat(timespec="seconds"),
                                 "speaker": names.get(sg["label"], {}).get("display") or "Speaker", "text": sg["text"]})
            summ = (r["summary_text"] or "")
            sl = summ.lower()
            i = min([sl.find(t) for t in terms if t in sl] or [-1])
            summary_snip = None
            if i >= 0:
                a0, a1 = max(0, i - 60), i + 160
                summary_snip = (("... " if a0 > 0 else "") + summ[a0:a1].replace(chr(10), " ").strip()
                                + (" ..." if a1 < len(summ) else ""))
            out.append({"id": r["id"], "day": r["day"], "started_at": r["started_at"], "duration_s": r["duration_s"],
                        "trigger": r["trigger"], "title": r["title"], "speakers": [v["display"] for v in names.values()],
                        "summary_hit": summary_snip, "hit_count": len(hits), "hits": hits[:12]})
        return {"results": out, "terms": terms}
    finally:
        c.close()


@app.get("/api/speakers")
def speakers():
    c = con()
    try:
        return [{"id": r["id"], "name": r["name"], "is_self": bool(r["is_self"]), "samples": r["samples"],
                 "last_heard": r["last_heard"]} for r in c.execute("select * from speakers order by is_self desc, name")]
    finally:
        c.close()


@app.patch("/api/speakers/{sid}")
async def rename_speaker(sid: int, request: Request):
    name = ((await request.json()).get("name") or "").strip()[:60]
    if not name:
        raise HTTPException(422, "name required")
    c = con()
    try:
        try:
            n = c.execute("update speakers set name=? where id=?", (name, sid)).rowcount
        except Exception:
            raise HTTPException(409, "that name is taken")
        if not n:
            raise HTTPException(404, "no such speaker")
        for (rid,) in c.execute("select recording_id from recording_speakers where speaker_id=?", (sid,)).fetchall():
            store.refresh_transcript(c, rid)
        return {"ok": True}
    finally:
        c.close()


@app.delete("/api/speakers/{sid}")
def delete_speaker(sid: int):
    c = con()
    try:
        with db.tx(c):
            rids = [r[0] for r in c.execute("select recording_id from recording_speakers where speaker_id=?", (sid,))]
            c.execute("update recording_speakers set speaker_id=null where speaker_id=?", (sid,))
            c.execute("delete from speakers where id=?", (sid,))
            for rid in rids:
                store.refresh_transcript(c, rid)
        return {"ok": True}
    finally:
        c.close()


@app.post("/api/speakers/enroll")
async def enroll(file: UploadFile = File(...), name: str = Form("Me"), is_self: str = Form("1")):
    name = name.strip()[:60] or "Me"
    tmp, _, ext = await _save_upload(file)
    dest = config.ENROLL_DIR / f"{uuid.uuid4().hex}{ext}"
    shutil.move(str(tmp), dest)
    c = con()
    try:
        jid = c.execute("insert into jobs(kind, payload, created_at) values('enroll', ?, ?)",
                        (json.dumps({"path": str(dest), "name": name, "is_self": is_self in ("1", "true", "on")}),
                         db.now_iso())).lastrowid
    finally:
        c.close()
    return {"ok": True, "job_id": jid}


def _settings_out(s: dict) -> dict:
    """Settings for the dashboard. The API key itself never leaves the PC, only whether one is saved."""
    s["summary"]["api_key_set"] = bool(config.summary_api_key())
    s["secrets"] = {"hf_token_set": bool(config.hf_token()), "calendar_url_set": bool(config.calendar_ics_url())}
    return s


@app.get("/api/settings")
def get_settings():
    c = con()
    try:
        return _settings_out(db.settings(c))
    finally:
        c.close()


TIME_RE = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")


@app.put("/api/settings")
async def put_settings(request: Request):
    body = await request.json()
    c = con()
    try:
        s = db.settings(c)
        auto = body.get("auto") or {}
        if "always_while_worn" in auto:
            s["auto"]["always_while_worn"] = bool(auto["always_while_worn"])
        for d, wins in (auto.get("windows") or {}).items():
            if d not in s["auto"]["windows"]:
                raise HTTPException(422, f"bad weekday {d}")
            clean = []
            for w in wins or []:
                if len(w) != 2 or not all(TIME_RE.match(x or "") for x in w) or w[0] >= w[1]:
                    raise HTTPException(422, f"bad window {w} on {d} (start must be before end)")
                clean.append([w[0], w[1]])
            s["auto"]["windows"][d] = sorted(clean)
        cal = auto.get("calendar") or {}
        if "enabled" in cal:
            s["auto"]["calendar"]["enabled"] = bool(cal["enabled"])
        if "lead_min" in cal:
            s["auto"]["calendar"]["lead_min"] = max(0, min(30, int(cal["lead_min"])))
        sil = body.get("silence") or {}
        if "auto_end_min" in sil:
            s["silence"]["auto_end_min"] = max(1, min(30, int(sil["auto_end_min"])))
        if "manual_stop_min" in sil:
            s["silence"]["manual_stop_min"] = max(0, min(120, int(sil["manual_stop_min"])))
        if "gap_min" in (body.get("merge") or {}):
            s["merge"]["gap_min"] = max(0, min(60, int(body["merge"]["gap_min"])))
        if "battery_saver" in (body.get("upload") or {}):
            s["upload"]["battery_saver"] = bool(body["upload"]["battery_saver"])
        summ = body.get("summary") or {}
        changed = False
        if "provider" in summ:
            if summ["provider"] not in summarize.PROVIDERS:
                raise HTTPException(422, f"unknown summary provider {summ['provider']!r}")
            changed |= s["summary"]["provider"] != summ["provider"]
            s["summary"]["provider"] = summ["provider"]
        for k in ("model", "base_url"):
            if k in summ:
                v = str(summ[k] or "").strip()[:300]
                if k == "base_url" and v and not re.match(r"^https?://", v):
                    raise HTTPException(422, "base URL must start with http:// or https://")
                changed |= s["summary"][k] != v
                s["summary"][k] = v
        if "when" in summ:
            if summ["when"] not in ("now", "daily", "manual"):
                raise HTTPException(422, "summary schedule must be now, daily or manual")
            if summ["when"] == "daily" and s["summary"]["when"] != "daily":
                # Start counting from now, so switching to daily doesn't fire a batch on the spot.
                b = db.get_kv(c, "summary_batch") or {}
                db.set_kv(c, "summary_batch", {"active": bool(b.get("active")),
                                               "last_at": datetime.now().isoformat(timespec="seconds")})
            s["summary"]["when"] = summ["when"]
        if "daily_time" in summ:
            if not TIME_RE.match(str(summ["daily_time"] or "")):
                raise HTTPException(422, "daily summary time must look like 23:00")
            s["summary"]["daily_time"] = summ["daily_time"]
        if "pause_when_busy" in (body.get("system") or {}):
            s["system"]["pause_when_busy"] = bool(body["system"]["pause_when_busy"])
        if "api_key" in summ:      # "" keeps the saved key; clear_api_key removes it
            key = str(summ["api_key"] or "").strip()
            if key:
                config.set_env_value("SUMMARY_API_KEY", key)
                changed = True
        if summ.get("clear_api_key"):
            config.set_env_value("SUMMARY_API_KEY", "")
            changed = True
        # Secrets typed into Settings are written to .env. "" keeps what is saved; clear_* removes it.
        sec = body.get("secrets") or {}
        hf = str(sec.get("hf_token") or "").strip()
        if hf:
            if len(hf) > 300 or " " in hf:
                raise HTTPException(422, "that doesn't look like a Hugging Face token")
            config.set_env_value("HF_TOKEN", hf)
            changed = True
        if sec.get("clear_hf_token"):
            config.set_env_value("HF_TOKEN", "")
            if config.hf_token():          # an older HUGGINGFACE_TOKEN line is still set
                config.set_env_value("HUGGINGFACE_TOKEN", "")
            changed = True
        cal_url = str(sec.get("calendar_url") or "").strip()
        if cal_url:
            if len(cal_url) > 2000 or not re.match(r"^(https?|webcal)://\S+$", cal_url):
                raise HTTPException(422, "the calendar link must start with https:// or webcal://")
            config.set_env_value("CALENDAR_ICS_URL", cal_url)
            changed = True
        if sec.get("clear_calendar_url"):
            config.set_env_value("CALENDAR_ICS_URL", "")
            changed = True
        s.pop("secrets", None)
        db.set_kv(c, "settings", s)
        if changed:
            db.set_kv(c, "summary_changed_at", db.now_iso())   # the worker re-reads .env and retries paused summaries
        return _settings_out(s)
    finally:
        c.close()


@app.post("/api/summary/test")
def summary_test():
    """Run one tiny summary through the saved provider so the user can see it work (or why not)."""
    c = con()
    try:
        cfg = db.settings(c)["summary"]
    finally:
        c.close()
    try:
        return summarize.test(cfg)
    except Exception as e:
        return {"ok": False, "error": str(e)[:500]}


@app.post("/api/summary/run")
def summary_run():
    """Summarize everything that is waiting, now. Used by the daily and manual schedules."""
    c = con()
    try:
        n = c.execute("select count(*) from recordings where status='transcribed'").fetchone()[0]
        b = db.get_kv(c, "summary_batch") or {}
        db.set_kv(c, "summary_batch", {**b, "active": True})
        db.set_kv(c, "summary_changed_at", db.now_iso())   # also clears a 10 minute error pause
    finally:
        c.close()
    return {"ok": True, "pending": n}


@app.post("/api/upload")
async def upload(file: UploadFile = File(...)):
    tmp, sha, ext = await _save_upload(file)
    dur = None
    try:
        out = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(tmp)],
                             capture_output=True, text=True, timeout=30).stdout.strip()
        dur = float(out) if out else None
    except Exception:
        pass
    started = (datetime.now(timezone.utc) - timedelta(seconds=dur or 0)).isoformat(timespec="seconds")
    rid = "up-" + uuid.uuid4().hex[:16]
    c = con()
    try:
        _store_chunk(c, recording_id=rid, idx=0, is_last=True, started_at=started, chunk_started_at=started,
                     duration_s=dur, trigger="manual", device_id="dashboard", sha=sha, src=tmp, ext=ext)
        c.execute("update recordings set trigger='upload' where id=?", (rid,))
    finally:
        c.close()
    return {"ok": True, "recording_id": rid}


# ---- static ------------------------------------------------------------------

config.STATIC_DIR.mkdir(exist_ok=True)
# Mounted last so every /api route above wins; index.html is served at "/".
app.mount("/", StaticFiles(directory=config.STATIC_DIR, html=True), name="static")


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host=config.BIND_HOST, port=config.PORT, log_level="info")
