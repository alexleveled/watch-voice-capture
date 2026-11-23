"""Read/shape helpers shared by the server and the worker."""

from __future__ import annotations

import json

import numpy as np

import db


def speaker_names(con, rec_id: str) -> dict[str, dict]:
    rows = con.execute("""select rs.label, rs.display, rs.speaker_id, s.name, coalesce(s.is_self,0) is_self
                          from recording_speakers rs left join speakers s on s.id = rs.speaker_id
                          where rs.recording_id=? order by rs.display""", (rec_id,)).fetchall()
    return {r["label"]: {"label": r["label"], "display": r["name"] or r["display"],
                         "speaker_id": r["speaker_id"], "is_self": bool(r["is_self"])} for r in rows}


def render_transcript(con, rec_id: str) -> str:
    names = speaker_names(con, rec_id)
    lines, last, buf = [], None, []
    for s in con.execute("select label, text from segments where recording_id=? order by start_s", (rec_id,)):
        who = names.get(s["label"], {}).get("display") or "Speaker"
        if who != last and buf:
            lines.append(f"{last}: {' '.join(buf)}")
            buf = []
        last = who
        buf.append(s["text"].strip())
    if buf:
        lines.append(f"{last}: {' '.join(buf)}")
    return "\n".join(lines)


def refresh_transcript(con, rec_id: str) -> None:
    con.execute("update recordings set transcript_text=? where id=?", (render_transcript(con, rec_id), rec_id))


def summary_text(s: dict | None) -> str | None:
    if not s:
        return None
    parts = [s.get("title") or "", s.get("summary") or ""]
    for k in ("key_points", "decisions"):
        parts += [f"- {x}" for x in s.get(k) or []]
    parts += [f"- TODO {a.get('item')}" for a in s.get("action_items") or []]
    return "\n".join(p for p in parts if p)


def conversation(con, rec_id: str) -> dict | None:
    r = con.execute("select * from recordings where id=?", (rec_id,)).fetchone()
    if not r:
        return None
    names = speaker_names(con, rec_id)
    segs = []
    from datetime import datetime, timedelta
    t0 = datetime.fromisoformat(r["started_at"])
    for s in con.execute("select start_s, end_s, label, text from segments where recording_id=? order by start_s", (rec_id,)):
        segs.append({"start_s": s["start_s"], "end_s": s["end_s"],
                     "at": (t0 + timedelta(seconds=s["start_s"])).isoformat(timespec="seconds"),
                     "label": s["label"], "display": names.get(s["label"], {}).get("display") or "Speaker",
                     "text": s["text"]})
    return {"id": r["id"], "started_at": r["started_at"], "ended_at": r["ended_at"], "duration_s": r["duration_s"],
            "trigger": r["trigger"], "status": r["status"], "error": r["error"], "title": r["title"],
            "summary": json.loads(r["summary_json"]) if r["summary_json"] else None,
            "speakers": list(names.values()), "segments": segs}


def emb_load(txt: str | None) -> np.ndarray | None:
    if not txt:
        return None
    v = np.asarray(json.loads(txt), dtype=np.float32)
    n = np.linalg.norm(v)
    return v / n if n else None


def emb_dump(v: np.ndarray) -> str:
    return json.dumps([round(float(x), 6) for x in v])


def fold_into_speaker(con, speaker_id: int, emb_txt: str | None) -> None:
    """Running mean of normalized embeddings = the speaker's voiceprint."""
    new = emb_load(emb_txt)
    row = con.execute("select embedding, samples from speakers where id=?", (speaker_id,)).fetchone()
    if new is None or row is None:
        return
    old = emb_load(row["embedding"])
    n = row["samples"] or 0
    mean = new if old is None else (old * n + new) / (n + 1)
    con.execute("update speakers set embedding=?, samples=?, last_heard=? where id=?",
                (emb_dump(mean / np.linalg.norm(mean)), n + 1, db.now_iso(), speaker_id))
