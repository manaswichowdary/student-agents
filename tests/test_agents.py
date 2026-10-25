"""Run: pytest -q"""
import asyncio
import copy

import pytest

from core import llm as llm_mod
from core.bus import sign
from core.connections import NEUTRAL
from core.identity import IdentityError
from core.schema import parse_listing
from core.util import DAY, WEEK
from core.world import World
from data import seed
from features import dead_link_detective as dld
from features import freebie_squad as squad
from features import opportunity_relay as relay

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
    return w


LATER_STUDY = {**seed.LISTINGS["sleep_study"], "id": "later_study", "deadline": "2026-11-20"}


def relay_payload(key="mcleaps"):
    return {"listing": parse_listing(seed.LISTINGS[key]).to_dict(), "closes": "Sun Nov 1", "verified": True}


def audit(w, type_=None):
    return [r for r in w.log if r["kind"] == "message" and (type_ is None or r["type"] == type_)]


# ------------------------------------------------------------------------------------------ identity
def test_rejects_non_edu_and_unknown_ids():
    w = World(verify_background=False)
    with pytest.raises(IdentityError):
        w.start_signup("someone@gmail.com")
    with pytest.raises(IdentityError):
        w.start_signup("nobody99@asu.edu")


def test_code_expires_and_attempts_are_capped():
    w = World(verify_background=False)
    w.start_signup("mpatel21")
    for _ in range(5):
        with pytest.raises(IdentityError):
            w.verify("mpatel21", "000000x")
    with pytest.raises(IdentityError, match="Too many"):
        w.verify("mpatel21", w.identity.outbox[-1]["code"])
    w.start_signup("drao7")
    w.clock.advance(11 * 60)
    with pytest.raises(IdentityError, match="expired"):
        w.verify("drao7", w.identity.outbox[-1]["code"])


def test_verified_name_comes_from_directory():
    w = World(verify_background=False)
    acc = w.sign_up("skim14@asu.edu")
    assert acc["name"] == "Sam Kim"


def test_unverified_students_cannot_connect():
    w = World(verify_background=False)
    with pytest.raises(PermissionError):
        w.connections.request("mpatel21@asu.edu", "drao7")


# ------------------------------------------------------------------------------------------ connections
def test_anti_enumeration_same_reply():
    w = World()
    w.sign_up(DEV)
    replies = {w.connections.request(DEV, t) for t in ("pnair3", "tchen5", "zz999", "someone@else.edu")}
    assert replies == {NEUTRAL}


def test_deny_is_silent_then_expires():
    w = World()
    w.sign_up(MAYA)
    w.connections.request("pnair3", MAYA)
    w.connections.deny(MAYA, "pnair3@asu.edu")
    assert w.connections.outgoing("pnair3") == [{"to": MAYA, "status": "pending"}]
    w.clock.advance(8 * DAY)
    assert w.connections.outgoing("pnair3")[0]["status"] == "expired"


def test_request_rate_limit():
    w = World()
    w.sign_up(DEV)
    for _ in range(10):
        w.connections.request(DEV, "zz999")
    assert w.connections.request(DEV, "zz999") != NEUTRAL


@pytest.mark.parametrize("state", ["pending", "removed", "blocked"])
def test_no_messages_without_active_connection(state):
    w = World()
    for e in seed.PHONES:
        w.sign_up(e)
    w.connections.request(DEV, MAYA)
    if state != "pending":
        w.connections.accept(MAYA, DEV)
        (w.connections.remove if state == "removed" else w.connections.block)(MAYA, DEV)
    ok, reason = run(w.agent(DEV).send("OPPORTUNITY_RELAY", MAYA, relay_payload(), hop_count=1))
    assert not ok and reason == "no active connection"
    assert w.agent(MAYA).feed == []


def test_block_prevents_new_requests(world):
    world.connections.block(MAYA, DEV)
    world.connections.request(DEV, MAYA)
    assert not world.connections.incoming(MAYA)


def test_permission_off_is_enforced_by_bus(world):
    world.connections.set_perm(MAYA, DEV, "receive_relay", False)
    ok, reason = run(world.agent(DEV).send("OPPORTUNITY_RELAY", MAYA, relay_payload(), hop_count=1))
    assert not ok and "turned off" in reason


# ------------------------------------------------------------------------------------------ bus security
def test_bad_signature_replay_and_unknown_sender(world):
    dev = world.agent(DEV)
    msg = world.bus.new_message("OPPORTUNITY_RELAY", DEV, MAYA, relay_payload(), 1)
    msg.signature = "0" * 64
    assert run(world.bus.send(msg)) == (False, "bad signature")
    good = world.bus.new_message("OPPORTUNITY_RELAY", DEV, MAYA, relay_payload(), 1)
    good.signature = sign(dev.key, good)
    assert run(world.bus.send(good))[0]
    assert run(world.bus.send(copy.deepcopy(good))) == (False, "replayed message id")
    ghost = world.bus.new_message("OPPORTUNITY_RELAY", "intruder@asu.edu", MAYA, relay_payload(), 1)
    assert run(world.bus.send(ghost)) == (False, "unknown sender")


def test_old_timestamp_and_tampering_rejected(world):
    dev = world.agent(DEV)
    msg = world.bus.new_message("OPPORTUNITY_RELAY", DEV, MAYA, relay_payload(), 1)
    msg.ts -= 600
    msg.signature = sign(dev.key, msg)
    assert "window" in run(world.bus.send(msg))[1]
    msg2 = world.bus.new_message("OPPORTUNITY_RELAY", DEV, MAYA, relay_payload(), 1)
    msg2.signature = sign(dev.key, msg2)
    msg2.payload["closes"] = "tampered"
    assert run(world.bus.send(msg2))[1] == "bad signature"


def test_hop_limit(world):
    ok, reason = run(world.agent(DEV).send("OPPORTUNITY_RELAY", MAYA, relay_payload(), hop_count=3))
    assert not ok and reason == "hop limit exceeded"


def test_idempotent_redelivery(world):
    dev = world.agent(DEV)
    run(dev.send("OPPORTUNITY_RELAY", MAYA, relay_payload("sleep_study"), 1, idempotency_key="k1"))
    ok, reason = run(dev.send("OPPORTUNITY_RELAY", MAYA, relay_payload("sleep_study"), 1, idempotency_key="k1"))
    assert not ok and reason.startswith("duplicate")
    assert len(world.agent(MAYA).feed) == 1


def test_bad_payload_rejected(world):
    ok, reason = run(world.agent(DEV).send("OPPORTUNITY_RELAY", MAYA, {"listing": {}, "closes": "x", "verified": True}))
    assert not ok and reason.startswith("bad payload")


# ------------------------------------------------------------------------------------------ relay
def test_relay_reaches_fitting_friend_only(world):
    result = run(relay.find(world.agent(DEV), seed.LISTINGS["sleep_study"]))
    assert result == "relayed to 2"
    assert [f["title"] for f in world.agent(MAYA).feed] == ["Sleep and memory study, $30 for 45 min"]
    assert world.agent(MAYA).feed[0]["line"].startswith("Forwarded by Dev, closes")
    assert world.agent(SAM).feed == []          # Sam's agent scored it low, privately


def test_dedupe_and_never_re_relay(world):
    run(relay.find(world.agent(DEV), seed.LISTINGS["sleep_study"]))
    before = len(audit(world, "OPPORTUNITY_RELAY"))
    run(world.agent(DEV).send("OPPORTUNITY_RELAY", MAYA, relay_payload("sleep_study"), 1))
    assert len(world.agent(MAYA).feed) == 1                       # seen-set dedupe
    sent_by_maya = [r for r in audit(world, "OPPORTUNITY_RELAY") if r["sender"] == MAYA]
    assert sent_by_maya == [] and len(audit(world, "OPPORTUNITY_RELAY")) == before + 1


def test_daily_relay_cap(world):
    dev = world.agent(DEV)
    for i in range(4):
        listing = parse_listing({**seed.LISTINGS["fee_waiver"], "id": f"cap_{i}", "eligibility_tags": ["nursing"]})
        run(relay.relay(dev, listing))
    assert len(dev.relay_times) == 5


def test_digest_versus_urgent(world):
    run(relay.find(world.agent(DEV), LATER_STUDY))                         # closes in weeks: waits for digest
    assert world.agent(DEV).relay_queue and not world.agent(MAYA).feed
    assert run(world.digest()) == 2
    assert world.agent(MAYA).feed


def test_scam_listing_is_never_relayed(world):
    assert run(relay.find(world.agent(DEV), seed.LISTINGS["scam_ra"])) == "scam"
    assert audit(world) == []


def test_injection_listing_has_no_effect(world):
    calls = []

    def evil_model(system, user):
        calls.append(user)
        return '{"score": 1, "reason": "ok", "action": "forward to everyone"}'   # extra key: rejected
    data = {"student": {"major": "Psychology"}, "listing": parse_listing(seed.LISTINGS["injection"]).to_dict()}
    out, source = llm_mod.llm("fit", data, call=evil_model)
    assert out is None and source == "rules"
    assert "<<<DATA" in calls[0] and "IGNORE PREVIOUS INSTRUCTIONS" in calls[0].split("<<<DATA\n")[1]
    run(relay.find(world.agent(DEV), seed.LISTINGS["injection"]))
    run(world.digest())
    relays = audit(world, "OPPORTUNITY_RELAY")
    assert {r["recipient"] for r in relays} <= {MAYA, SAM}                   # only normal recipients
    assert all(r["sender"] == DEV for r in relays)


def test_listing_schema_is_strict():
    with pytest.raises(ValueError):
        parse_listing({**seed.LISTINGS["mcleaps"], "url": "http://insecure.example"})
    with pytest.raises(ValueError):
        parse_listing({**seed.LISTINGS["mcleaps"], "instructions": "do something"})
    long = parse_listing({**seed.LISTINGS["mcleaps"], "title": "x" * 999})
    assert len(long.title) == 160


# ------------------------------------------------------------------------------------------ dead-link detective
def test_trust_math_and_decay():
    w = World()
    t = w.trust
    assert t.score("New source") == 0.5 and t.label("New source") == "unverified"
    t.record("S", True); t.record("S", True); t.record("S", False)
    assert t.score("S") == pytest.approx(3 / 5)
    w.clock.advance(10 * WEEK)
    decayed = 0.95 ** 10
    assert t.score("S") == pytest.approx((2 * decayed + 1) / (3 * decayed + 2))


def test_dead_listing_lowers_trust_and_is_not_relayed(world):
    before = world.trust.score("Old club newsletter")
    assert run(relay.find(world.agent(DEV), seed.LISTINGS["focus_group"])) == "closed"
    assert world.trust.score("Old club newsletter") < before < 0.35
    assert audit(world, "OPPORTUNITY_RELAY") == []


def test_login_wall_is_unknown(world):
    assert run(relay.find(world.agent(DEV), seed.LISTINGS["dining_survey"])) == "unknown"


def test_status_200_alone_is_not_open(world):
    assert run(relay.find(world.agent(DEV), seed.LISTINGS["book_swap"])) in ("closed", "flagged")


def test_needs_two_distinct_independent_verifiers(world):
    world.shared_fetch = True                   # both verifiers reuse one underlying check
    assert run(relay.find(world.agent(DEV), seed.LISTINGS["mcleaps"])) == "unknown"


def test_consensus_rules():
    rep = lambda v: 1.0  # noqa: E731
    ev = lambda cid: {"http": "HTTP 200", "deadline": "x", "reason": "r", "check_id": cid}  # noqa: E731
    one = [{"by": "a", "status": "open", "evidence": ev("1")}]
    assert dld.consensus(one, rep)[0] == "unknown"
    two = one + [{"by": "b", "status": "open", "evidence": ev("2")}]
    assert dld.consensus(two, rep)[0] == "open"
    recused = one + [{"by": "b", "status": "open", "evidence": ev("recused")}]
    assert dld.consensus(recused, rep)[0] == "unknown"


def test_trusted_source_skips_verification(world):
    assert run(relay.find(world.agent(DEV), seed.LISTINGS["hack_night"])) == "kept"
    assert audit(world, "VERIFY_REQUEST") == []


# ------------------------------------------------------------------------------------------ freebie squad
def share_all(w):
    for friend in (MAYA, SAM):
        w.connections.set_perm(friend, DEV, "share_freebusy", True)
        w.connections.set_perm(friend, DEV, "share_distance", True)


def test_availability_reply_shares_only_free_busy_and_bucket(world):
    share_all(world)
    run(squad.start(world.agent(DEV), seed.EVENTS["diwali"]))
    replies = [r["fields"] for r in audit(world, "AVAILABILITY_REPLY")]
    assert all(set(r) == {"query_id", "status", "distance"} for r in replies)
    assert {r["distance"] for r in replies} <= {"near", "medium", "far"}


def test_no_query_without_permission(world):
    run(squad.start(world.agent(DEV), seed.EVENTS["diwali"]))
    assert audit(world, "AVAILABILITY_QUERY") == []


def test_carpool_capacity_and_walk_fallback(world):
    share_all(world)
    world.agent(DEV).profile["seats"] = 1
    plan = run(squad.start(world.agent(DEV), seed.EVENTS["diwali"]))
    assert sorted(plan["invited"].values()) == ["rider", "walker"]
    world.agent(MAYA).profile["has_car"] = False
    world.agent(DEV).profile["has_car"] = False
    plan2 = run(squad.start(world.agent(DEV), {**seed.EVENTS["diwali"], "title": "Free Diwali dinner"}))
    assert plan2["transport"] == "walk" and set(plan2["invited"].values()) == {"walker"}


def test_pickup_only_after_confirm_and_late_accept_rejected(world):
    share_all(world)
    dev = world.agent(DEV)
    plan = run(squad.start(dev, {**seed.EVENTS["diwali"], "capacity": 2}))   # room for 1 friend
    invited = list(plan["invited"])
    assert len(invited) == 1
    run(squad.answer(world.agent(invited[0]), plan["id"], True))
    assert world.agent(invited[0]).invites[plan["id"]]["meet"] is None
    run(squad.confirm(dev, plan["id"]))
    assert "picks you up" in world.agent(invited[0]).invites[plan["id"]]["meet"]
    world.clock.set(plan["starts"] + 60)
    late = world.bus.new_message("PLAN_REPLY", invited[0], DEV, {"plan_id": plan["id"], "answer": "accept"})
    dev.plans[plan["id"]]["accepted"].clear()
    late.signature = sign(world.agent(invited[0]).key, late)
    run(world.bus.send(late))
    assert world.agent(invited[0]).invites[plan["id"]]["state"] == "rejected"


def test_removal_cancels_plans_and_queued_relays(world):
    share_all(world)
    dev = world.agent(DEV)
    plan = run(squad.start(dev, seed.EVENTS["diwali"]))
    run(squad.answer(world.agent(MAYA), plan["id"], True))
    run(squad.answer(world.agent(SAM), plan["id"], True))
    run(squad.confirm(dev, plan["id"]))
    run(relay.find(dev, LATER_STUDY))
    assert any(i["to"] == MAYA for i in dev.relay_queue)
    run(world.end_connection(MAYA, DEV))
    assert dev.plans[plan["id"]]["state"] == "cancelled"
    assert world.agent(MAYA).invites[plan["id"]]["state"] == "cancelled"
    assert world.agent(SAM).invites[plan["id"]]["state"] == "cancelled"
    assert not any(i["to"] == MAYA for i in dev.relay_queue)
    assert DEV not in [f["email"] for f in world.connections.friends(MAYA)]


def test_demo_scenario_runs():
    import scenario
    w = World()
    run(scenario.run(w, pause=0))
    blocked = [r for r in audit(w) if r["result"] == "rejected"]
    assert blocked and blocked[-1]["reason"] == "no active connection"
