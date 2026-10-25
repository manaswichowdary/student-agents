"""Opportunity Relay: a find that doesn't fit its owner goes to active connections it might fit.

Fit uses only the stated major, interests and class year. Relays only verified, unexpired, non scam listings.
Never re-relays something that arrived by relay. At most 5 outgoing relays a day. Recipients get a daily digest,
except deadlines within 24 hours, which go out right away.
"""
from __future__ import annotations

from core.llm import llm
from core.schema import Listing, SchemaError, parse_listing, scam_flags
from core.util import DAY, fmt_day

THRESHOLD = 0.5
DAILY_CAP = 5
YEARS = {"freshman", "sophomore", "junior", "senior", "graduate"}


def rule_fit(profile: dict, listing: Listing) -> tuple[float, str]:
    tags = set(listing.eligibility_tags)
    major = profile["major"].lower()
    year = profile["year"].lower()
    interests = {i.lower() for i in profile.get("interests", [])}
    years = tags & YEARS
    if years and year not in years:
        return 0.1, f"open to {', '.join(sorted(years))} only"
    if major in tags or any(t in major or major in t for t in tags - YEARS - {"any major"} if len(t) > 3):
        return 0.9, f"matches your {profile['major']} major"
    hit = interests & tags
    if hit:
        return 0.7, f"matches your interest in {sorted(hit)[0]}"
    if "any major" in tags:
        return 0.55, "open to any major"
    return 0.2, "doesn't match your major or interests"


def fit(agent, listing: Listing) -> tuple[float, str, str]:
    p = agent.profile
    student = {"major": p["major"], "year": p["year"], "interests": p.get("interests", [])}
    out, source = llm("fit", {"student": student, "listing": listing.to_dict()})
    if out:
        return float(out["score"]), out["reason"], source
    score, reason = rule_fit(p, listing)
    return score, reason, "rules"


def surface(agent, listing: Listing, *, from_name: str | None, verified: bool, reason: str, source: str,
            msg_id: str | None = None, shared: dict | None = None) -> None:
    now = agent.world.clock.now()
    closes = listing.expires(now)
    agent.feed.append({
        "id": listing.id, "kind": "opportunity", "title": listing.title, "org": listing.org, "type": listing.type,
        "url": listing.url, "pay": listing.pay_amount, "closes": fmt_day(closes),
        "line": (f"Forwarded by {from_name}, closes {fmt_day(closes)}" if from_name
                 else f"Found by your agent, closes {fmt_day(closes)}"),
        "verified": verified, "why": reason, "fit_source": source, "urgent": closes - now < DAY,
        "msg_id": msg_id, "shared": shared or {},
    })


async def find(agent, raw: dict) -> str:
    """The owner's agent found a listing (for example from Freebie Radar). Returns what happened."""
    from features import dead_link_detective as dld
    world = agent.world
    try:
        listing = parse_listing(raw)
    except SchemaError as e:
        agent.note(f"Skipped a listing with bad fields ({e})", "warn")
        return "invalid"
    if listing.id in agent.seen:
        return "duplicate"
    agent.seen.add(listing.id)
    agent.my_finds.add(listing.id)
    flags = scam_flags(listing, raw.get("raw_text", ""))
    if flags:
        agent.note(f"Flagged \"{listing.title}\": {flags[0]}. Not shared.", "warn")
        world.event("scam", f"{agent.first}'s agent flagged a listing: {flags[0]}", listing=listing.id)
        return "scam"
    if listing.expires(world.clock.now()) < world.clock.now():
        agent.note(f"\"{listing.title}\" already closed. Not shared.", "warn")
        return "expired"
    status, detail = await dld.verify_listing(agent, listing)
    if status != "open":
        agent.note(f"\"{listing.title}\": {detail}. Not shared.", "warn")
        return status
    score, reason, source = fit(agent, listing)
    if score >= THRESHOLD:
        surface(agent, listing, from_name=None, verified=True, reason=reason, source=source)
        return "kept"
    sent = await relay(agent, listing)
    return f"relayed to {sent}"


async def relay(agent, listing: Listing) -> int:
    world = agent.world
    now = world.clock.now()
    while agent.relay_times and agent.relay_times[0] <= now - DAY:
        agent.relay_times.popleft()
    targets = [f for f in world.connections.active(agent.id)
               if world.connections.perm(f, agent.id, "receive_relay")]
    urgent = listing.expires(now) - now < DAY
    queued = 0
    for friend in targets:
        if len(agent.relay_times) >= DAILY_CAP:
            agent.note("Reached 5 shares today. The rest wait until tomorrow.", "warn")
            break
        agent.relay_times.append(now)
        item = {"to": friend, "listing": listing.to_dict(), "closes": fmt_day(listing.expires(now)), "verified": True}
        if urgent:
            await _send_relay(agent, item)
        else:
            agent.relay_queue.append(item)
        queued += 1
    if queued:
        when = "now (closes within 24 hours)" if urgent else "in tonight's digest"
        agent.note(f"Sharing \"{listing.title}\" with {queued} connection(s) {when}")
    return queued


async def _send_relay(agent, item: dict) -> None:
    await agent.send("OPPORTUNITY_RELAY", item["to"],
                     {"listing": item["listing"], "closes": item["closes"], "verified": item["verified"]},
                     hop_count=1, idempotency_key=f"relay:{item['listing']['id']}:{item['to']}")


async def flush_digest(agent) -> int:
    queue, agent.relay_queue = agent.relay_queue, []
    for item in queue:
        await _send_relay(agent, item)
    return len(queue)


async def on_relay(agent, msg) -> None:
    world = agent.world
    listing = parse_listing(msg.payload["listing"])
    if listing.id in agent.seen:
        return                                      # dedupe; and relayed items are never relayed again
    agent.seen.add(listing.id)
    if listing.expires(world.clock.now()) < world.clock.now():
        return
    if scam_flags(listing):
        return
    score, reason, source = fit(agent, listing)
    sender = world.identity.account(msg.sender)["name"].split()[0]
    world.event("fit", f"{agent.first}'s agent scored \"{listing.title}\": {score:.2f} ({source})")
    if score >= THRESHOLD:
        surface(agent, listing, from_name=sender, verified=msg.payload["verified"], reason=reason, source=source,
                msg_id=msg.id, shared=msg.payload)


def on_disconnect(agent, other: str) -> None:
    agent.relay_queue = [i for i in agent.relay_queue if i["to"] != other]
