"""Campus feed scan: each ASU student's agent checks SunDevilCentral, ASU's public club event feed, for free food.

The feed is public (no sign in) and has no "free food" tag, so this reads each event's title and description
for food words, the same way our Track 1 Skill (Freebie Radar) does. The feed is fetched at most once every
30 minutes and shared by all agents; each agent then decides on its own what fits its student (campus, diet).
SunDevilCentral is a trusted source, so its events skip the two-agent link check.
"""
from __future__ import annotations

import asyncio
import hashlib
import os
import quopri
import re
import time
import urllib.request
from datetime import datetime, timedelta, timezone

from . import opportunity_relay as relay

FEED = "https://sundevilcentral.eoss.asu.edu/ical/arizonau/ical_arizonau.ics"
ARIZONA = timezone(timedelta(hours=-7))
CACHE_SECONDS = 30 * 60
CAMPUS_TAG = {"Tempe north": "Tempe Campus", "Tempe south": "Tempe Campus", "Downtown Phoenix": "Downtown Phoenix Campus"}
FOOD = re.compile(
    r"free (food|pizza|lunch|dinner|breakfast|snacks?|boba|drinks|tacos|donuts|cookies|ice cream)"
    r"|(food|pizza|lunch|dinner|breakfast|snacks?|refreshments|drinks)( and \w+)? (will be |is |are )?(provided|served)"
    r"|\bpizza\b|\bsnacks\b|\brefreshments\b|\bboba\b|\bdonuts\b|\bcatered\b",
    re.IGNORECASE)
PAID = re.compile(r"\$\s?\d+|tickets? (are|cost)|admission fee|cost to attend", re.IGNORECASE)
_cache: dict = {"at": 0.0, "events": []}


def _unescape(v: str) -> str:
    return v.replace("\\n", " ").replace("\\N", " ").replace("\\,", ",").replace("\\;", ";").replace("\\\\", "\\").strip()


def parse(text: str) -> list[dict]:
    text = re.sub(r"\r?\n[ \t]", "", text)
    events = []
    for block in text.split("BEGIN:VEVENT")[1:]:
        f: dict = {}
        for line in block.splitlines():
            if ":" not in line:
                continue
            head, value = line.split(":", 1)
            name, *params = head.split(";")
            if name == "SUMMARY" and "ENCODING=QUOTED-PRINTABLE" in params:
                value = quopri.decodestring(value.encode()).decode("utf-8", errors="replace")
            if name == "CATEGORIES" and "X-CG-CATEGORY=event_tags" in params:
                name = "TAGS"
            if name == "ORGANIZER":
                value = next((p[4:].strip('"') for p in params if p.startswith("CN=")), "")
            f.setdefault(name, value)
        events.append(f)
    return events


def _start(v: str) -> float | None:
    try:
        if v.endswith("Z"):
            return datetime.strptime(v, "%Y%m%dT%H%M%SZ").replace(tzinfo=timezone.utc).timestamp()
        return datetime.strptime(v[:15], "%Y%m%dT%H%M%S").replace(tzinfo=ARIZONA).timestamp()
    except ValueError:
        return None


def _fetch_text() -> str:
    path = os.environ.get("CAMPUS_FEED_FILE")      # tests and offline demos
    if path:
        return open(path, encoding="utf-8").read()
    req = urllib.request.Request(FEED, headers={"User-Agent": "student-agents/1.0 (hackathon prototype)"})
    with urllib.request.urlopen(req, timeout=60) as r:
        return r.read().decode("utf-8", errors="replace")


async def events() -> list[dict]:
    """All feed events, refreshed at most every 30 minutes and shared by every agent."""
    if time.time() - _cache["at"] > CACHE_SECONDS or not _cache["events"]:
        text = await asyncio.to_thread(_fetch_text)
        _cache.update(at=time.time(), events=parse(text))
    return _cache["events"]


def food_listings(evts: list[dict], now: float, hours: int = 48, campus: str | None = None, limit: int = 6) -> list[dict]:
    out = []
    for e in evts:
        start = _start(e.get("DTSTART", ""))
        if not start or not (now + 15 * 60 <= start <= now + hours * 3600):
            continue
        if campus and "Campus" in e.get("TAGS", "") and campus not in e.get("TAGS", ""):
            continue
        title = _unescape(e.get("SUMMARY", ""))[:160]
        text = f"{title}. {_unescape(e.get('DESCRIPTION', ''))}"
        if not FOOD.search(text) or PAID.search(text):
            continue
        url = e.get("URL", "").strip()
        if not url.startswith("https://"):
            continue
        where = _unescape(e.get("LOCATION", ""))
        uid = e.get("UID") or url
        out.append({
            "id": "sdc_" + hashlib.sha256(uid.encode()).hexdigest()[:12], "title": title or "Club event with food",
            "org": (e.get("ORGANIZER") or "ASU club")[:160], "type": "food",
            "deadline": datetime.fromtimestamp(start, ARIZONA).isoformat(timespec="minutes"),
            "url": url[:300], "source": "SunDevilCentral", "eligibility_tags": ["any major"], "pay_amount": None,
            "location": "" if "sign in" in where.lower() else where[:160],
        })
    out.sort(key=lambda x: x["deadline"])
    return out[:limit]


def eligible(email: str) -> bool:
    return email.endswith("@asu.edu")


async def scan(world, user: str) -> dict:
    """The student's agent checks the campus feed and keeps new free food events that fit."""
    if not eligible(user):
        return {"added": 0, "message": "Campus feed scanning is ASU only for now. Friends can still share finds with you."}
    agent = world.agent(user)
    try:
        evts = await events()
    except Exception as e:  # noqa: BLE001
        agent.note(f"Couldn't reach SunDevilCentral right now ({type(e).__name__}). Will try again later.", "warn")
        return {"added": 0, "message": "Couldn't reach SunDevilCentral right now. Try again in a few minutes."}
    campus = CAMPUS_TAG.get(agent.profile.get("zone", ""))
    before = len(agent.feed)
    for raw in food_listings(evts, world.clock.now(), campus=campus):
        if raw["id"] in agent.seen:
            continue
        await relay.find(agent, raw)
    added = len(agent.feed) - before
    if added:
        agent.note(f"Found {added} free food event{'s' if added != 1 else ''} on SunDevilCentral")
        world.event("scan", f"{agent.first}'s agent found {added} free food event(s) on SunDevilCentral")
    return {"added": added, "message": f"Found {added} new free food event{'s' if added != 1 else ''} on SunDevilCentral"
            if added else "No new free food on SunDevilCentral in the next 2 days. Your agent will keep checking."}
