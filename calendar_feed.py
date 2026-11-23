"""Upcoming meetings for the watch's calendar trigger. Optional and read-only.

Set CALENDAR_ICS_URL in .env to your calendar's private iCal link:
  Google Calendar: Settings > your calendar > "Secret address in iCal format"
  Outlook:         Settings > Calendar > Shared calendars > Publish a calendar > ICS link

A calendar block counts as a meeting when it has at least one attendee or a video-call link
(Zoom, Meet, Teams, Webex). All-day and cancelled events are skipped, so "gym" or "pay rent"
blocks never record. Some providers strip attendees from the feed; set
"calendar_all_timed_events": true in config.json to count every timed event instead.
"""

from __future__ import annotations

import re
import time
from datetime import date, datetime, timedelta, timezone

import httpx

import config

VIDEO_LINK = re.compile(r"zoom\.us/|meet\.google\.com/|teams\.microsoft\.com/|teams\.live\.com/|webex\.com/", re.I)

_cache: tuple[float, str, list] = (0.0, "", [])


def _is_meeting(ev, count_all: bool) -> bool:
    if str(ev.get("STATUS", "")).upper() == "CANCELLED":
        return False
    start = ev.get("DTSTART")
    if start is None or not isinstance(start.dt, datetime):   # a plain date = all-day event
        return False
    if count_all:
        return True
    if ev.get("ATTENDEE"):
        return True
    text = " ".join(str(ev.get(k, "")) for k in ("LOCATION", "DESCRIPTION", "URL", "X-GOOGLE-CONFERENCE"))
    return bool(VIDEO_LINK.search(text))


def _utc(dt) -> datetime:
    if isinstance(dt, date) and not isinstance(dt, datetime):
        dt = datetime(dt.year, dt.month, dt.day)
    if dt.tzinfo is None:
        dt = dt.astimezone()          # floating time: treat as this PC's local time
    return dt.astimezone(timezone.utc)


def parse(ics_text: str, hours_ahead: int = 36) -> list[dict]:
    """[{start, end, title}] in iso utc, from now-1h to now+hours_ahead. Recurring events are expanded."""
    import icalendar
    import recurring_ical_events

    cal = icalendar.Calendar.from_ical(ics_text)
    now = datetime.now(timezone.utc)
    count_all = bool(config.tunables().get("calendar_all_timed_events"))
    out = []
    for ev in recurring_ical_events.of(cal).between(now - timedelta(hours=1), now + timedelta(hours=hours_ahead)):
        if not _is_meeting(ev, count_all):
            continue
        s = _utc(ev["DTSTART"].dt)
        e = _utc(ev["DTEND"].dt) if ev.get("DTEND") else s + timedelta(hours=1)
        out.append({"start": s.isoformat(timespec="seconds"), "end": e.isoformat(timespec="seconds"),
                    "title": str(ev.get("SUMMARY") or "Meeting")})
    out.sort(key=lambda m: m["start"])
    return out


def meetings(hours_ahead: int = 36, cache_s: int = 600) -> list[dict]:
    """Cached 10 min. Returns [] when no calendar is set up."""
    global _cache
    url = config.calendar_ics_url()
    if not url:
        return []
    if url.startswith("webcal://"):
        url = "https://" + url[len("webcal://"):]
    if _cache[1] == url and time.time() - _cache[0] < cache_s:
        return _cache[2]
    r = httpx.get(url, timeout=30, follow_redirects=True)
    r.raise_for_status()
    out = parse(r.text, hours_ahead)
    _cache = (time.time(), url, out)
    return out
