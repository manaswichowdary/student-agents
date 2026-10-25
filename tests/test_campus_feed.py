"""The campus feed scan, against a small sample feed (no network)."""
import asyncio
import time
from datetime import datetime, timedelta, timezone

import pytest

from core.world import World
from features import campus_feed

AZ = timezone(timedelta(hours=-7))


def ics(now: float) -> str:
    def ev(uid, hours, title, desc, tags="Tempe Campus", loc="Memorial Union 202"):
        start = datetime.fromtimestamp(now + hours * 3600, AZ).strftime("%Y%m%dT%H%M%S")
        return (f"BEGIN:VEVENT\nUID:{uid}\nDTSTART:{start}\nSUMMARY:{title}\nDESCRIPTION:{desc}\n"
                f"LOCATION:{loc}\nURL:https://sundevilcentral.eoss.asu.edu/event/{uid}\n"
                f"CATEGORIES;X-CG-CATEGORY=event_tags:{tags}\nORGANIZER;CN=\"Club {uid}\":mailto:x@asu.edu\nEND:VEVENT\n")
    return ("BEGIN:VCALENDAR\n" + ev("a1", 5, "Hack night", "Free pizza and drinks")
            + ev("a2", 6, "Gala", "Dinner will be provided. Tickets are $15")
            + ev("a3", 7, "Chess club", "Bring a board")
            + ev("a4", 8, "Study break", "Snacks provided", tags="Downtown Phoenix Campus")
            + ev("a5", 80, "Next week pizza", "Free pizza") + "END:VCALENDAR\n")


@pytest.fixture
def feed_file(tmp_path, monkeypatch):
    p = tmp_path / "feed.ics"
    p.write_text(ics(time.time()))
    monkeypatch.setenv("CAMPUS_FEED_FILE", str(p))
    campus_feed._cache.update(at=0.0, events=[])
    return p


def test_finds_free_food_only(feed_file):
    evts = campus_feed.parse(feed_file.read_text())
    titles = [l["title"] for l in campus_feed.food_listings(evts, time.time())]
    assert titles == ["Hack night", "Study break"]          # not paid, not foodless, not 3 days out
    tempe = [l["title"] for l in campus_feed.food_listings(evts, time.time(), campus="Tempe Campus")]
    assert tempe == ["Hack night"]


def test_scan_adds_to_feed_once(feed_file, tmp_path):
    w = World(db=str(tmp_path / "x.db"), live=True)
    email = w.start_signup("newkid@asu.edu")
    code = w.identity.outbox[-1]["code"]
    w.verify(email, code, "New Kid", "Biology", "junior")
    first = asyncio.run(campus_feed.scan(w, email))
    again = asyncio.run(campus_feed.scan(w, email))
    assert first["added"] == 2 and again["added"] == 0
    item = w.agent(email).feed[0]
    assert item["line"].startswith("Found by your agent on SunDevilCentral, starts")


def test_other_schools_can_sign_up_but_not_scan(feed_file, tmp_path):
    w = World(db=str(tmp_path / "y.db"), live=True)
    email = w.start_signup("pat@umich.edu")
    w.verify(email, w.identity.outbox[-1]["code"], "Pat Doe", "History", "senior")
    assert asyncio.run(campus_feed.scan(w, email))["added"] == 0
