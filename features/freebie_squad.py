"""Freebie Squad: round up connections for a free event, with a carpool when someone can drive.

Availability replies hold only free or busy for that one window (rounded to 15 minutes) and a distance bucket,
and only from connections who turned on free/busy sharing. Plans move proposed -> confirmed -> expired or
cancelled. Exact pickup spots are sent only to confirmed members, after they confirm.
"""
from __future__ import annotations

import secrets

from core.util import fmt_time, parse_local, phx, round_15

MILES = {"near": 1, "medium": 5}


def bucket(miles: float) -> str:
    return "near" if miles < MILES["near"] else "medium" if miles < MILES["medium"] else "far"


def is_free(agent, start: float, end: float) -> bool:
    for s, e in agent.profile.get("busy", []):
        if parse_local(s) < end and start < parse_local(e):
            return False
    return True


def distance_to(agent, place: str) -> str:
    miles = agent.world.places.get(place, {}).get(agent.profile.get("zone", ""), 10)
    return bucket(miles)


async def start(agent, event: dict) -> dict:
    """event: {title, location, starts (iso), minutes, capacity}"""
    world = agent.world
    starts = parse_local(event["starts"])
    w_start, w_end = round_15(starts), round_15(starts + event.get("minutes", 60) * 60, up=True)
    qid = "q_" + secrets.token_hex(4)
    agent.queries[qid] = {"replies": {}}
    asked = [f for f in world.connections.active(agent.id) if world.connections.perm(f, agent.id, "share_freebusy")]
    world.event("squad", f"{agent.first}'s agent asks {len(asked)} connection(s): free for {event['title']}?")
    for f in asked:
        await agent.send("AVAILABILITY_QUERY", f, {
            "query_id": qid, "event_title": event["title"], "place": event["location"],
            "window_start": phx(w_start).isoformat(timespec="minutes"),
            "window_end": phx(w_end).isoformat(timespec="minutes")})
    replies = agent.queries[qid]["replies"]
    free = [f for f, r in replies.items() if r["status"] == "free"]
    order = {"far": 0, "medium": 1, "near": 2, None: 3}
    free.sort(key=lambda f: order[replies[f]["distance"]])    # farthest friends get the car seats first
    capacity = max(0, int(event.get("capacity", 4)) - 1)
    free = free[:capacity]
    seats = agent.profile.get("seats", 0) if agent.profile.get("has_car") else 0
    transport = "carpool" if seats > 0 and free else "walk"
    roles = {f: ("rider" if transport == "carpool" and i < seats else "walker") for i, f in enumerate(free)}
    plan = {"id": "p_" + secrets.token_hex(4), "title": event["title"], "location": event["location"],
            "starts": starts, "starts_label": fmt_time(starts), "state": "proposed", "host": agent.id,
            "host_name": agent.first, "transport": transport, "capacity": capacity, "invited": roles,
            "accepted": [], "declined": [], "role": "driver" if transport == "carpool" else "walker"}
    agent.plans[plan["id"]] = plan
    for f, role in roles.items():
        await agent.send("PLAN_INVITE", f, {"plan_id": plan["id"], "event_title": event["title"],
                                            "starts": phx(starts).isoformat(timespec="minutes"),
                                            "transport": transport, "role": role})
    if not free:
        agent.note(f"Nobody's free for {event['title']} right now")
    return plan


async def on_query(agent, msg) -> None:
    p = msg.payload
    status = "free" if is_free(agent, parse_local(p["window_start"]), parse_local(p["window_end"])) else "busy"
    host = agent.world.agents[msg.sender]
    place = p["place"]
    share = agent.world.connections.perm(agent.id, msg.sender, "share_distance")
    await agent.send("AVAILABILITY_REPLY", msg.sender, {
        "query_id": p["query_id"], "status": status, "distance": distance_to(agent, place) if share else None})
    agent.note(f"Told {host.first}'s agent you're {status} for {p['event_title']}")


async def on_reply(agent, msg) -> None:
    q = agent.queries.get(msg.payload["query_id"])
    if q is not None:
        q["replies"][msg.sender] = {"status": msg.payload["status"], "distance": msg.payload["distance"]}


async def on_invite(agent, msg) -> None:
    p = msg.payload
    host = agent.world.agents[msg.sender]
    agent.invites[p["plan_id"]] = {"id": p["plan_id"], "title": p["event_title"], "host": msg.sender,
                                   "host_name": host.first, "starts_label": fmt_time(parse_local(p["starts"])),
                                   "starts": parse_local(p["starts"]), "transport": p["transport"],
                                   "role": p["role"], "state": "invited", "meet": None, "msg_id": msg.id,
                                   "shared": p}


async def answer(agent, plan_id: str, accept: bool) -> tuple[bool, str]:
    inv = agent.invites.get(plan_id)
    if not inv:
        return False, "No such plan"
    inv["state"] = "accepted" if accept else "declined"
    return await agent.send("PLAN_REPLY", inv["host"], {"plan_id": plan_id, "answer": "accept" if accept else "decline"},
                            idempotency_key=f"reply:{plan_id}:{agent.id}")


async def on_plan_reply(agent, msg) -> None:
    plan = agent.plans.get(msg.payload["plan_id"])
    if not plan or msg.sender not in plan["invited"]:
        return
    if msg.payload["answer"] == "decline":
        plan["declined"].append(msg.sender)
        return
    if msg.sender in plan["accepted"]:
        return
    now = agent.world.clock.now()
    why = ("the plan was cancelled" if plan["state"] == "cancelled"
           else "the event already started" if now > plan["starts"] or plan["state"] == "expired"
           else "the plan is full" if not _has_room(plan) else None)
    if why:
        await agent.send("PLAN_UPDATE", msg.sender, {"plan_id": plan["id"], "state": "rejected",
                                                    "note": f"Too late: {why}", "meet": None})
        agent.world.event("squad", f"Late accept from {agent.world.agents[msg.sender].first} rejected: {why}")
        return
    plan["accepted"].append(msg.sender)
    if plan["state"] == "confirmed":                     # room left after confirming: send their spot now
        await _send_confirmed(agent, plan, msg.sender)


def _has_room(plan) -> bool:
    return len(plan["accepted"]) < plan["capacity"]


async def confirm(agent, plan_id: str) -> dict:
    """Lock the plan. Only confirmed members get the exact pickup or meeting spot."""
    world = agent.world
    plan = agent.plans[plan_id]
    if world.clock.now() > plan["starts"]:
        plan["state"] = "expired"
        return plan
    if not plan["accepted"]:
        plan["state"] = "cancelled"
        return plan
    plan["state"] = "confirmed"
    for member in plan["accepted"]:
        await _send_confirmed(agent, plan, member)
    world.event("squad", f"Plan confirmed: {plan['title']}, {plan['transport']}, {len(plan['accepted']) + 1} going")
    return plan


async def on_update(agent, msg) -> None:
    inv = agent.invites.get(msg.payload["plan_id"])
    if inv:
        inv["state"] = msg.payload["state"]
        inv["meet"] = msg.payload["meet"]
        inv["note"] = msg.payload["note"]
        inv["shared"] = msg.payload


async def _send_confirmed(agent, plan: dict, member: str) -> None:
    friend = agent.world.agents[member]
    if plan["invited"][member] == "rider":
        spot = agent.world.pickup_spots.get(friend.profile.get("zone", ""), "the main entrance")
        meet = f"{agent.first} picks you up at {spot}, {fmt_time(plan['starts'] - 20 * 60)}"
    else:
        meet = f"Meet at {plan['location']}, front entrance, {fmt_time(plan['starts'] - 10 * 60)}"
    await agent.send("PLAN_UPDATE", member, {"plan_id": plan["id"], "state": "confirmed",
                                             "note": f"{plan['title']} is on", "meet": meet})


def on_disconnect(agent, other: str) -> list[tuple[str, dict]]:
    """Cancel in flight plans that include the removed person. Returns notices to send to everyone else."""
    notices = []
    for plan in agent.plans.values():
        if plan["state"] in ("proposed", "confirmed") and other in plan["invited"]:
            plan["state"] = "cancelled"
            for member in plan["accepted"]:
                if member != other:
                    notices.append((member, {"plan_id": plan["id"], "state": "cancelled",
                                             "note": f"{plan['title']} was cancelled", "meet": None}))
    for inv in agent.invites.values():
        if inv["host"] == other and inv["state"] not in ("cancelled", "expired", "rejected"):
            inv["state"] = "cancelled"
            inv["meet"] = None
    return notices
