"""Tests for: counter-proposals in Freebie Squad, the link-check network guard and unverified sign ins."""
import asyncio
import socket
import urllib.error

import pytest

from core import llm as llm_mod
from core import netguard
from core.schema import SchemaError, validate_payload
from core.world import World
from data import seed
from features import freebie_squad as squad

MAYA, DEV, SAM = seed.PHONES
run = asyncio.run


@pytest.fixture(autouse=True)
def isolated_llm_cache(tmp_path, monkeypatch):
    monkeypatch.setattr(llm_mod, "CACHE", tmp_path / "cache.json")
    monkeypatch.setattr(llm_mod, "MISSES", tmp_path / "misses.json")
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)


@pytest.fixture
def world():
    w = World()
    for e in seed.PHONES:
        w.sign_up(e)
    c = w.connections
    c.request(DEV, MAYA); c.accept(MAYA, DEV)
    c.request(DEV, SAM); c.accept(SAM, DEV)
    c.request(MAYA, SAM); c.accept(SAM, MAYA)
    for friend in (MAYA, SAM):
        c.set_perm(friend, DEV, "share_freebusy", True)
        c.set_perm(friend, DEV, "share_distance", True)
    return w


def audit(w, type_):
    return [r for r in w.log if r["kind"] == "message" and r["type"] == type_]


# ------------------------------------------------------------------------------------ counter-proposals
def test_busy_friend_gets_a_later_arrival_instead_of_dropping_out(world):
    plan = run(squad.start(world.agent(DEV), seed.EVENTS["diwali"]))      # Sam's lab runs until 6:30
    assert plan["invited"][SAM] == "walker" and SAM in plan["late"] and MAYA not in plan["late"]
    invite = world.agent(SAM).invites[plan["id"]]
    assert invite["later"] is True and invite["starts_label"].endswith("6:30 PM")
    assert world.agent(MAYA).invites[plan["id"]]["later"] is False


def test_counter_proposal_only_asks_the_busy_friend_about_the_later_window(world):
    run(squad.start(world.agent(DEV), seed.EVENTS["diwali"]))
    queries = audit(world, "AVAILABILITY_QUERY")
    assert [q["recipient"] for q in queries].count(MAYA) == 1
    assert [q["recipient"] for q in queries].count(SAM) == 2
    later = [q for q in queries if q["recipient"] == SAM][-1]["fields"]
    assert later["window_start"] == "2026-10-26T18:30-07:00" and later["window_end"] == "2026-10-26T19:30-07:00"


def test_late_friend_gets_their_own_time_only_after_confirming_and_a_human_must_say_yes(world):
    dev = world.agent(DEV)
    plan = run(squad.start(dev, seed.EVENTS["diwali"]))
    sam = world.agent(SAM).invites[plan["id"]]
    assert sam["state"] == "invited" and sam["meet"] is None
    run(squad.answer(world.agent(SAM), plan["id"], True))
    run(squad.confirm(dev, plan["id"]))
    assert "6:20 PM" in world.agent(SAM).invites[plan["id"]]["meet"]
    assert "picks you up" not in world.agent(SAM).invites[plan["id"]]["meet"]


def test_late_friend_can_still_accept_after_the_event_starts_but_before_their_time(world):
    dev = world.agent(DEV)
    plan = run(squad.start(dev, seed.EVENTS["diwali"]))
    world.clock.set(plan["starts"] + 5 * 60)                                # 6:05, event started, Sam arrives 6:30
    run(squad.answer(world.agent(SAM), plan["id"], True))
    assert SAM in dev.plans[plan["id"]]["accepted"]


def test_no_counter_proposal_for_short_events_or_when_nobody_is_busy(world):
    plan = run(squad.start(world.agent(DEV), {**seed.EVENTS["diwali"], "minutes": 45, "title": "Quick bite"}))
    assert plan["late"] == {} and SAM not in plan["invited"]
    world.agent(SAM).profile["busy"] = []
    plan2 = run(squad.start(world.agent(DEV), {**seed.EVENTS["diwali"], "title": "Second dinner"}))
    assert plan2["late"] == {}


def test_counter_proposal_respects_group_capacity(world):
    plan = run(squad.start(world.agent(DEV), {**seed.EVENTS["diwali"], "capacity": 2}))   # room for one friend
    assert list(plan["invited"]) == [MAYA] and plan["late"] == {}


def test_late_window_math():
    start = 1_000_000_000 - (1_000_000_000 % 900)
    assert squad.late_window(start, 45) is None
    ls, le = squad.late_window(start, 90)
    assert ls == start + 1800 and le == start + 5400
    assert squad.late_window(start, 60) == (start + 1800, start + 3600)


def test_plan_invite_schema_requires_the_later_flag():
    ok = {"plan_id": "p_1", "event_title": "Dinner", "starts": "2026-10-26T18:00", "transport": "walk",
          "role": "walker", "later": False}
    validate_payload("PLAN_INVITE", ok)
    with pytest.raises(SchemaError):
        validate_payload("PLAN_INVITE", {k: v for k, v in ok.items() if k != "later"})
    with pytest.raises(SchemaError):
        validate_payload("PLAN_INVITE", {**ok, "later": "yes"})


# ------------------------------------------------------------------------------------ network guard
def fake_dns(monkeypatch, ip):
    family = socket.AF_INET6 if ":" in ip else socket.AF_INET
    monkeypatch.setattr(socket, "getaddrinfo", lambda host, port, **kw: [(family, socket.SOCK_STREAM, 6, "", (ip, port))])


@pytest.mark.parametrize("ip", ["127.0.0.1", "10.0.0.5", "192.168.1.9", "172.16.0.1", "169.254.169.254", "::1", "fd00::1", "0.0.0.0"])
def test_private_and_metadata_addresses_are_blocked(monkeypatch, ip):
    fake_dns(monkeypatch, ip)
    with pytest.raises(netguard.BlockedAddress):
        netguard._PublicHTTPSConnection("looks-public.example", timeout=1).connect()


def test_a_name_with_one_private_address_among_public_ones_is_blocked(monkeypatch):
    monkeypatch.setattr(socket, "getaddrinfo", lambda host, port, **kw: [
        (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", port)),
        (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("10.0.0.5", port))])
    with pytest.raises(netguard.BlockedAddress):
        netguard._PublicHTTPSConnection("mixed.example", timeout=1).connect()


def test_public_address_is_allowed_through(monkeypatch):
    fake_dns(monkeypatch, "93.184.216.34")
    reached = {}

    def fake_connect(addr, timeout=None):
        reached["addr"] = addr
        raise ConnectionRefusedError("stop here: the guard let it through")

    monkeypatch.setattr(socket, "create_connection", fake_connect)
    with pytest.raises(ConnectionRefusedError):
        netguard._PublicHTTPSConnection("public.example", timeout=1).connect()
    assert reached["addr"] == ("93.184.216.34", 443)


def test_redirect_to_plain_http_is_refused():
    handler = netguard._Redirects()
    req = netguard.urllib.request.Request("https://public.example/start")
    with pytest.raises(urllib.error.HTTPError):
        handler.redirect_request(req, None, 302, "Found", {}, "http://internal.example/admin")
    assert handler.max_redirections == 3


def test_fetch_goes_through_the_guard(monkeypatch):
    fake_dns(monkeypatch, "127.0.0.1")
    with pytest.raises(Exception) as e:
        netguard.fetch("https://looks-public.example/page", timeout=1)
    assert "non public" in str(e.value) or isinstance(getattr(e.value, "reason", None), netguard.BlockedAddress)


def test_check_url_reports_unknown_for_a_blocked_address(monkeypatch):
    from features import dead_link_detective as dld
    fake_dns(monkeypatch, "10.0.0.5")
    monkeypatch.setenv("REAL_HTTP", "1")
    w = World(verify_background=False)
    page = dld.check_url(w, "someone@asu.edu", "https://sneaky.example/page")
    assert page["status"] == 0 and not page["login_wall"]
    listing = dld.parse_listing({**seed.LISTINGS["mcleaps"], "url": "https://sneaky.example/page"})
    assert dld.hard_rules(listing, page, w.clock.now()) == "unknown"


# ------------------------------------------------------------------------------------ unverified sign ins
def test_screen_shown_code_marks_the_account_unverified():
    w = World(verify_background=False)
    w.identity.open_signup = True
    email = w.start_signup("newkid@asu.edu")
    w.identity.mark_shown_on_screen(email)
    acc = w.verify(email, w.identity.outbox[-1]["code"], "New Kid", "History", "senior")
    assert acc["proof"] == "screen"
    other = w.start_signup("realone@asu.edu")
    assert w.verify(other, w.identity.outbox[-1]["code"], "Real One", "History", "senior")["proof"] == "email"


def test_requests_and_friends_show_unverified_to_the_other_student():
    w = World(verify_background=False)
    w.identity.open_signup = True
    email = w.start_signup("newkid@asu.edu")
    w.identity.mark_shown_on_screen(email)
    w.verify(email, w.identity.outbox[-1]["code"], "New Kid", "History", "senior")
    w.sign_up(MAYA)
    w.connections.request("newkid@asu.edu", MAYA)
    assert w.connections.incoming(MAYA)[0]["unverified"] is True
    w.connections.accept(MAYA, "newkid@asu.edu")
    assert w.connections.friends(MAYA)[0]["unverified"] is True
    assert w.connections.friends("newkid@asu.edu")[0]["unverified"] is False   # Maya signed in the normal way


def test_old_database_files_get_the_proof_column(tmp_path):
    import sqlite3
    from core.store import Store
    path = str(tmp_path / "old.db")
    db = sqlite3.connect(path)
    db.execute("CREATE TABLE accounts (email TEXT PRIMARY KEY, name TEXT, major TEXT, year TEXT, verified_at REAL, "
               "self_reported INTEGER DEFAULT 0)")
    db.execute("INSERT INTO accounts VALUES ('a@asu.edu','A B','CS','senior',0,0)")
    db.commit(); db.close()
    store = Store(path)
    assert store.one("SELECT proof FROM accounts WHERE email='a@asu.edu'")["proof"] == "email"
