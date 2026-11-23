"""Background worker: recordings -> transcripts -> summaries -> daily digests.

    .venv\\Scripts\\python.exe worker.py          (launch.py starts it)

Loop, every few seconds:
  1. heartbeat (the dashboard's health dot reads it)
  2. enrollment jobs (voiceprints)
  3. one ready recording: join chunks -> WhisperX -> speakers -> delete audio   [GPU]
  4. one transcribed recording: AI summary        [the provider chosen in Settings > Summaries]
  5. dirty days: daily digest                     [same provider]
  6. drop GPU models after idle
"""

from __future__ import annotations

import json
import shutil
import time
import traceback
from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np

import config
import db
import stitch
import store
import summarize
import transcribe

TICK_S = 5
MAX_ATTEMPTS = 3
RESUMMARIZE_SETTLE_S = 60


def log(msg: str) -> None:
    print(f"{datetime.now().strftime('%H:%M:%S')} {msg}", flush=True)


# ---- stage helpers --------------------------------------------------------------

def ready_recordings(c) -> list:
    stale = (datetime.now(timezone.utc) - timedelta(minutes=config.tunables()["recording_stale_minutes"])).isoformat()
    rows = c.execute("""select r.*, (select count(*) from chunks k where k.recording_id=r.id) n
                        from recordings r where r.status in ('receiving','waiting') order by r.started_at""").fetchall()
    out = []
    for r in rows:
        complete = r["last_idx"] is not None and r["n"] == r["last_idx"] + 1
        if r["n"] and (complete or r["updated_at"] < stale or r["status"] == "waiting"):
            out.append(r)
    return out


def match_speakers(c, rec_id: str, speakers: dict[str, dict]) -> None:
    thr = config.tunables()["speaker_match_threshold"]
    known = [(s["id"], store.emb_load(s["embedding"])) for s in c.execute("select id, embedding from speakers")]
    known = [(i, e) for i, e in known if e is not None]
    taken: set[int] = set()
    letters = iter("ABCDEFGHIJKLMNOPQRSTUVWXYZ")
    c.execute("delete from recording_speakers where recording_id=?", (rec_id,))
    for label, info in sorted(speakers.items(), key=lambda kv: -kv[1]["speech_s"]):
        emb = info["embedding"]
        sid, best = None, -1.0
        if emb is not None and known:
            e = emb / (np.linalg.norm(emb) or 1.0)
            for kid, kemb in known:
                sim = float(np.dot(e, kemb))
                if sim > best and kid not in taken:
                    sid, best = kid, sim
            if best < thr:
                sid = None
        if sid:
            taken.add(sid)
            if best >= thr + 0.1:  # only confident matches sharpen the voiceprint
                store.fold_into_speaker(c, sid, store.emb_dump(emb))
            else:
                c.execute("update speakers set last_heard=? where id=?", (db.now_iso(), sid))
        c.execute("""insert into recording_speakers(recording_id, label, display, speaker_id, embedding, speech_s)
                     values(?,?,?,?,?,?)""",
                  (rec_id, label, f"Speaker {next(letters, 'X')}", sid,
                   store.emb_dump(emb) if emb is not None else None, info["speech_s"]))


def process_recording(c, eng: transcribe.Engine, r) -> None:
    rid = r["id"]
    c.execute("update recordings set status='transcribing', updated_at=? where id=?", (db.now_iso(), rid))
    chunks = c.execute("select path from chunks where recording_id=? order by idx", (rid,)).fetchall()
    paths = [Path(k["path"]) for k in chunks if k["path"] and Path(k["path"]).exists()]
    if not paths:
        raise RuntimeError("audio files are missing")
    wav = transcribe.to_wav(paths, config.AUDIO_DIR / rid / "joined.wav")
    t0 = time.time()
    res = eng.run(wav)
    log(f"{rid}: {res.duration_s / 60:.1f} min audio -> {len(res.segments)} segments, "
        f"{len(res.speakers)} speaker(s) in {time.time() - t0:.0f}s")
    words = sum(len(s["text"].split()) for s in res.segments)
    ended = r["ended_at"] or (datetime.fromisoformat(r["started_at"]) + timedelta(seconds=res.duration_s)).isoformat(timespec="seconds")
    with db.tx(c):
        c.execute("delete from segments where recording_id=?", (rid,))
        c.executemany("insert into segments(recording_id, start_s, end_s, label, text) values(?,?,?,?,?)",
                      [(rid, s["start_s"], s["end_s"], s["label"], s["text"]) for s in res.segments])
        match_speakers(c, rid, res.speakers)
        store.refresh_transcript(c, rid)
        status = "transcribed" if words else "empty"
        c.execute("""update recordings set status=?, error=null, duration_s=?, speech_s=?, words=?, ended_at=?,
                     updated_at=? where id=?""",
                  (status, res.duration_s, sum(v["speech_s"] for v in res.speakers.values()), words, ended,
                   db.now_iso(), rid))
        db.mark_day_dirty(c, r["day"])
    if not config.tunables()["keep_audio"]:
        shutil.rmtree(config.AUDIO_DIR / rid, ignore_errors=True)
        c.execute("update chunks set path=null where recording_id=?", (rid,))


def meeting_title(r) -> str | None:
    if r["trigger"] != "calendar":
        return None
    try:
        import calendar_feed
        for m in calendar_feed.meetings():
            if m["start"] <= (r["ended_at"] or r["started_at"]) and m["end"] >= r["started_at"]:
                return m["title"]
    except Exception:
        pass
    return None


def wearer(c) -> str | None:
    """Name of the enrolled "this is me" voice, so summaries know who wore the watch."""
    row = c.execute("select name from speakers where is_self=1 limit 1").fetchone()
    return row["name"] if row else None


def summarize_recording(c, r) -> None:
    rid = r["id"]
    cfg = config.tunables()
    if (r["words"] or 0) < cfg["min_words_to_summarize"]:
        first = (r["transcript_text"] or "").split(":", 1)[-1].strip()
        title = (" ".join(first.split()[:6]) + "...") if first else "Short exchange"
        c.execute("update recordings set status='done', title=?, summary_json=null, summary_text=null, updated_at=? where id=?",
                  (title, db.now_iso(), rid))
        db.mark_day_dirty(c, r["day"])
        return
    c.execute("update recordings set status='summarizing', updated_at=? where id=?", (db.now_iso(), rid))
    when = datetime.fromisoformat(r["started_at"]).astimezone().strftime("%a %b %d %Y, %I:%M %p")
    s = summarize.conversation(r["transcript_text"], when, (r["duration_s"] or 0) / 60, r["trigger"], meeting_title(r),
                               db.settings(c)["summary"], wearer(c))
    with db.tx(c):
        c.execute("""update recordings set status='done', error=null, title=?, summary_json=?, summary_text=?, updated_at=?
                     where id=?""", (s.get("title"), json.dumps(s), store.summary_text(s), db.now_iso(), rid))
        db.mark_day_dirty(c, r["day"])
    log(f"{rid}: summarized: {s.get('title')}")


def rebuild_day(c, day: str) -> None:
    rows = c.execute("""select started_at, title, summary_json from recordings
                        where day=? and status='done' and summary_json is not null order by started_at""", (day,)).fetchall()
    if not rows:
        c.execute("update days set summary_json=null, summary_text=null, dirty=0, dirty_since=null, updated_at=? where day=?",
                  (db.now_iso(), day))
        return
    items = []
    for r in rows:
        s = json.loads(r["summary_json"])
        items.append({"time": datetime.fromisoformat(r["started_at"]).astimezone().strftime("%I:%M %p"),
                      "title": s.get("title") or r["title"] or "", "summary": s.get("summary", ""),
                      "key_points": s.get("key_points", []), "action_items": s.get("action_items", []),
                      "people": s.get("people", [])})
    label = datetime.strptime(day, "%Y-%m-%d").strftime("%A, %B %d %Y")
    s = summarize.day(label, items, db.settings(c)["summary"], wearer(c))
    text = "\n".join([s.get("headline", ""), s.get("summary", "")] + [f"- {k}" for k in s.get("key_points", [])])
    c.execute("update days set summary_json=?, summary_text=?, dirty=0, dirty_since=null, updated_at=? where day=?",
              (json.dumps(s), text, db.now_iso(), day))
    log(f"day {day}: digest rebuilt from {len(items)} conversation(s)")


def due_days(c) -> list[str]:
    debounce = (datetime.now(timezone.utc) - timedelta(minutes=config.tunables()["day_rebuild_debounce_minutes"])).isoformat()
    out = []
    for d in c.execute("select day, dirty_since from days where dirty=1").fetchall():
        pending = c.execute("""select count(*) from recordings where day=? and status in
                               ('receiving','waiting','transcribing','transcribed','summarizing')""", (d["day"],)).fetchone()[0]
        if not pending or (d["dirty_since"] or "") < debounce:
            out.append(d["day"])
    return out


def run_jobs(c, eng: transcribe.Engine) -> None:
    for j in c.execute("select * from jobs where status='queued' order by id").fetchall():
        p = json.loads(j["payload"])
        try:
            emb = eng.voiceprint(Path(p["path"]))
            with db.tx(c):
                row = c.execute("select id from speakers where lower(name)=lower(?)", (p["name"],)).fetchone()
                sid = row["id"] if row else c.execute("insert into speakers(name, created_at) values(?,?)",
                                                      (p["name"], db.now_iso())).lastrowid
                if p.get("is_self"):
                    c.execute("update speakers set is_self = (id = ?)", (sid,))
                store.fold_into_speaker(c, sid, store.emb_dump(emb))
                c.execute("update jobs set status='done', done_at=? where id=?", (db.now_iso(), j["id"]))
            log(f"voiceprint enrolled for {p['name']}")
        except Exception as e:
            c.execute("update jobs set status='failed', error=?, done_at=? where id=?", (str(e)[:500], db.now_iso(), j["id"]))
            log(f"enroll failed: {e}")
        finally:
            Path(p["path"]).unlink(missing_ok=True)


def fail(c, rid: str, stage: str, e: Exception, retry_status: str) -> None:
    attempts = c.execute("select attempts from recordings where id=?", (rid,)).fetchone()[0] + 1
    status = "failed" if attempts >= MAX_ATTEMPTS else retry_status
    c.execute("update recordings set status=?, attempts=?, error=?, updated_at=? where id=?",
              (status, attempts, f"{stage}: {e}"[:800], db.now_iso(), rid))
    log(f"{rid}: {stage} error ({attempts}/{MAX_ATTEMPTS}): {e}")


# ---- main loop ------------------------------------------------------------------

def main() -> None:
    config.ensure_dirs()
    db.init()
    c = db.connect()
    eng = transcribe.Engine()
    summary_wait_until = 0.0
    # A crash mid-step leaves rows in a working state; put them back in line.
    c.execute("update recordings set status='waiting' where status='transcribing'")
    c.execute("update recordings set status='transcribed' where status='summarizing'")
    log("worker started")
    while True:
        state = "idle"
        try:
            jobs = c.execute("select count(*) from jobs where status='queued'").fetchone()[0]
            if jobs:
                state = "enrolling"
                db.set_kv(c, "worker_heartbeat", {"at": db.now_iso(), "state": state})
                run_jobs(c, eng)

            ready = ready_recordings(c)
            if ready:
                r = ready[0]
                state = "transcribing"
                db.set_kv(c, "worker_heartbeat", {"at": db.now_iso(), "state": state})
                try:
                    process_recording(c, eng, r)
                    try:
                        into = stitch.auto_merge(c, r["id"])
                        if into:
                            log(f"{r['id']}: joined onto {into}")
                    except Exception as e:  # a failed stitch must never cost the transcript
                        traceback.print_exc()
                        log(f"{r['id']}: auto-merge skipped: {e}")
                except Exception as e:
                    traceback.print_exc()
                    fail(c, r["id"], "transcribe", e, "waiting")

            # A provider change or a new API key in Settings ends the pause right away.
            stamp = db.get_kv(c, "summary_changed_at")
            if stamp and stamp != getattr(main, "_seen_stamp", None):
                main._seen_stamp = stamp
                summary_wait_until = 0.0
                config.reload_env()
            if time.time() >= summary_wait_until:
                settle = (datetime.now(timezone.utc) - timedelta(seconds=RESUMMARIZE_SETTLE_S)).isoformat()
                todo = c.execute("""select * from recordings where status='transcribed' and updated_at < ?
                                    order by started_at limit 1""", (settle,)).fetchone()
                try:
                    if todo:
                        state = "summarizing"
                        db.set_kv(c, "worker_heartbeat", {"at": db.now_iso(), "state": state})
                        summarize_recording(c, todo)
                    else:
                        for day in due_days(c)[:1]:
                            state = "daily digest"
                            rebuild_day(c, day)
                except Exception as e:
                    if not isinstance(e, summarize.SummaryError):
                        traceback.print_exc()
                    log(f"summary step error: {e}; pausing summaries 10 min")
                    if todo:
                        fail(c, todo["id"], "summary", e, "transcribed")
                    summary_wait_until = time.time() + 600

            eng.maybe_unload()
        except Exception as e:
            traceback.print_exc()
            state = f"error: {e}"
        db.set_kv(c, "worker_heartbeat", {"at": db.now_iso(), "state": state, "models_loaded": eng.loaded()})
        time.sleep(TICK_S)


if __name__ == "__main__":
    main()
