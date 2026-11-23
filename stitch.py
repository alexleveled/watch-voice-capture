"""Join recordings that are really one conversation.

The watch ends an auto recording after a couple of minutes of silence, so a conversation with a
5-minute pause arrives as two recordings. After a recording is transcribed the worker calls
`auto_merge`: if the previous recording from the same watch ended less than `merge.gap_min`
minutes earlier and the other voices are not clearly different people, the new one is folded
into it (segments shifted onto the earlier timeline, speakers matched by voiceprint) and the
combined conversation is re-summarized. The dashboard's "Merge with previous" button calls
`merge` directly for the cases the rule gets wrong.

Audio is already deleted at this point, so a merge works on segments, never on audio.
"""

from __future__ import annotations

from datetime import datetime

import numpy as np

import config
import db
import store

MERGEABLE = ("transcribed", "summarizing", "done")


def _ts(iso: str) -> datetime:
    return datetime.fromisoformat(iso)


def _speakers(c, rid: str) -> list[dict]:
    rows = c.execute("""select rs.label, rs.display, rs.speaker_id, rs.embedding, rs.speech_s, coalesce(s.is_self,0) is_self
                        from recording_speakers rs left join speakers s on s.id = rs.speaker_id
                        where rs.recording_id=?""", (rid,)).fetchall()
    return [{"label": r["label"], "display": r["display"], "speaker_id": r["speaker_id"],
             "emb": store.emb_load(r["embedding"]), "speech_s": r["speech_s"] or 0.0,
             "is_self": bool(r["is_self"])} for r in rows]


def _same(a: dict, b: dict, thr: float) -> bool:
    if a["speaker_id"] and b["speaker_id"]:
        return a["speaker_id"] == b["speaker_id"]
    if a["emb"] is not None and b["emb"] is not None:
        return float(np.dot(a["emb"], b["emb"])) >= thr
    return False


def previous(c, r) -> dict | None:
    """The recording just before r from the same device, if any."""
    return c.execute("""select * from recordings where id != ? and coalesce(device_id,'') = coalesce(?, '')
                        and trigger != 'upload' and started_at < ? order by started_at desc limit 1""",
                     (r["id"], r["device_id"], r["started_at"])).fetchone()


def should_merge(c, prev, new) -> tuple[bool, str]:
    gap_min = db.settings(c)["merge"]["gap_min"]
    if not gap_min:
        return False, "auto-merge off"
    if prev is None or prev["status"] not in MERGEABLE or new["trigger"] == "upload":
        return False, "no mergeable previous recording"
    gap = (_ts(new["started_at"]) - _ts(prev["ended_at"] or prev["started_at"])).total_seconds() / 60
    if gap > gap_min:
        return False, f"gap {gap:.1f} min > {gap_min}"
    thr = config.tunables()["speaker_match_threshold"]
    p_others = [s for s in _speakers(c, prev["id"]) if not s["is_self"]]
    n_others = [s for s in _speakers(c, new["id"]) if not s["is_self"]]
    if p_others and n_others and not any(_same(a, b, thr) for a in n_others for b in p_others):
        return False, f"gap {gap:.1f} min but different people"
    return True, f"gap {gap:.1f} min"


def merge(c, prev_id: str, new_id: str) -> None:
    """Fold recording new_id into prev_id (which must start earlier)."""
    prev = c.execute("select * from recordings where id=?", (prev_id,)).fetchone()
    new = c.execute("select * from recordings where id=?", (new_id,)).fetchone()
    if not prev or not new:
        raise ValueError("recording not found")
    if new["started_at"] < prev["started_at"]:
        prev, new = new, prev
    offset = (_ts(new["started_at"]) - _ts(prev["started_at"])).total_seconds()
    thr = config.tunables()["speaker_match_threshold"]
    p_spk, n_spk = _speakers(c, prev["id"]), _speakers(c, new["id"])
    used_labels = {s["label"] for s in p_spk}
    used_letters = {s["display"].removeprefix("Speaker ") for s in p_spk if s["display"].startswith("Speaker ")}
    free_letters = iter(ch for ch in "ABCDEFGHIJKLMNOPQRSTUVWXYZ" if ch not in used_letters)
    taken: set[str] = set()
    label_map: dict[str, str] = {}
    with db.tx(c):
        for s in sorted(n_spk, key=lambda x: -x["speech_s"]):
            match = next((p for p in p_spk if p["label"] not in taken and _same(s, p, thr)), None)
            if match:
                taken.add(match["label"])
                label_map[s["label"]] = match["label"]
                c.execute("update recording_speakers set speech_s = coalesce(speech_s,0) + ?, "
                          "speaker_id = coalesce(speaker_id, ?) where recording_id=? and label=?",
                          (s["speech_s"], s["speaker_id"], prev["id"], match["label"]))
                continue
            k, lab = 0, s["label"]
            while lab in used_labels:
                k += 1
                lab = f"{s['label']}_{k}"
            used_labels.add(lab)
            label_map[s["label"]] = lab
            c.execute("""insert into recording_speakers(recording_id, label, display, speaker_id, embedding, speech_s)
                         values(?,?,?,?,?,?)""",
                      (prev["id"], lab, f"Speaker {next(free_letters, 'X')}", s["speaker_id"],
                       store.emb_dump(s["emb"]) if s["emb"] is not None else None, s["speech_s"]))
        for seg in c.execute("select id, label, start_s, end_s from segments where recording_id=?", (new["id"],)).fetchall():
            c.execute("update segments set recording_id=?, label=?, start_s=?, end_s=? where id=?",
                      (prev["id"], label_map.get(seg["label"], seg["label"]), seg["start_s"] + offset,
                       seg["end_s"] + offset, seg["id"]))
        ended = max(prev["ended_at"] or prev["started_at"], new["ended_at"] or new["started_at"])
        c.execute("""update recordings set ended_at=?, duration_s=?, speech_s=?, words=?, status='transcribed',
                     error=null, attempts=0, updated_at=? where id=?""",
                  (ended, (prev["duration_s"] or 0) + (new["duration_s"] or 0),
                   (prev["speech_s"] or 0) + (new["speech_s"] or 0), (prev["words"] or 0) + (new["words"] or 0),
                   db.now_iso(), prev["id"]))
        merged = db.get_kv(c, "merged_into") or {}
        merged[new["id"]] = prev["id"]
        db.set_kv(c, "merged_into", merged)
        c.execute("delete from recordings where id=?", (new["id"],))
        store.refresh_transcript(c, prev["id"])
        db.mark_day_dirty(c, prev["day"])
        db.mark_day_dirty(c, new["day"])


def auto_merge(c, rid: str) -> str | None:
    """Called right after rid is transcribed. Returns the id it was merged into, or None."""
    new = c.execute("select * from recordings where id=?", (rid,)).fetchone()
    if not new or new["status"] != "transcribed":
        return None
    prev = previous(c, new)
    ok, why = should_merge(c, prev, new)
    if not ok:
        return None
    merge(c, prev["id"], rid)
    return f"{prev['id']} ({why})"
