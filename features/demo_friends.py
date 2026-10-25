"""Demo friends for the live website, so one person can try every feature alone.

Maya, Dev and Sam are agents with no human behind them. They accept connection requests, verify links, share
finds and say yes to plans. Everything still goes through the same bus, consent and permission checks.
"""
from __future__ import annotations

import random
from datetime import timedelta

from core.util import parse_local, phx
from data import seed

from . import freebie_squad as squad
from . import opportunity_relay as relay

BOTS = list(seed.PHONES)            # Maya, Dev, Sam
BOT_PROFILES = {
    "mpatel21@asu.edu": {"zone": "Tempe north", "interests": ["research", "mental health"]},
    "drao7@asu.edu": {"zone": "Tempe north", "interests": ["software", "hackathons"], "has_car": True, "seats": 3},
    "skim14@asu.edu": {"zone": "Downtown Phoenix", "interests": ["pre-med", "volunteering"]},
}


def is_bot(email: str) -> bool:
    return email in BOTS


def seed(world) -> None:
    """Run once on a fresh database: verify the demo friends and connect them to each other."""
    for email in BOTS:
        if not world.identity.is_verified(email):
            world.sign_up(email)
        agent = world.agent(email)
        agent.settings = {**BOT_PROFILES[email], "busy": []}
        agent.profile.update(agent.settings)
    c = world.connections
    for a, b in ((BOTS[0], BOTS[1]), (BOTS[0], BOTS[2]), (BOTS[1], BOTS[2])):
        if not c.is_active(a, b):
            c.request(a, b)
            c.accept(b, a)
        for x, y in ((a, b), (b, a)):
            c.set_perm(x, y, "share_freebusy", True)


def befriend(world, user: str, bot: str) -> None:
    """A demo friend accepts right away and shares free/busy and distance with the student."""
    c = world.connections
    if c.is_active(user, bot):
        return
    c.request(user, bot)
    c.accept(bot, user)
    c.set_perm(bot, user, "share_freebusy", True)
    c.set_perm(bot, user, "share_distance", True)
    world.event("connection", f"{world.agent(bot).first} accepted {world.agent(user).first}'s request")


def befriend_all(world, user: str) -> None:
    for bot in BOTS:
        befriend(world, user, bot)


async def friend_shares_a_find(world, user: str) -> str:
    """A demo friend's agent finds something that fits the student better than itself, and shares it.

    The friend's agent asks its connections (including the student's agent) to verify the link first,
    then relays it. Only the student's own agent decides if it fits.
    """
    me = world.agent(user)
    major = me.profile["major"].lower()
    friends = [b for b in BOTS if world.connections.is_active(user, b) and world.agent(b).profile["major"].lower() != major]
    if not friends:
        return "Add the demo friends first"
    bot = world.agent(random.choice(friends))
    now = world.clock.now()
    soon = phx(now + 20 * 3600).isoformat(timespec="minutes")
    week = phx(now + 9 * 24 * 3600).date().isoformat()
    picks = [
        {"title": f"Paid {me.profile['major']} research study, $25 for 30 min", "org": "ASU research lab", "type": "study",
         "deadline": soon, "url": "https://psychology.asu.edu/research/undergraduate-research-opportunities", "source": "Psych Dept Discord",
         "eligibility_tags": [major[:30], "research"], "pay_amount": 25, "location": "Tempe campus"},
        {"title": f"{me.profile['major']} student leadership award", "org": "ASU college pages", "type": "scholarship",
         "deadline": week, "url": "https://publicservice.asu.edu/impact/mcleaps-internships", "source": "ASU college pages",
         "eligibility_tags": [major[:30]], "pay_amount": 1500, "location": ""},
        {"title": f"Free lunch with {me.profile['major']} alumni", "org": "Career services", "type": "food",
         "deadline": soon, "url": "https://eoss.asu.edu/home", "source": "SunDevilCentral",
         "eligibility_tags": [major[:30]], "pay_amount": None, "location": "Memorial Union"},
    ]
    raw = random.choice(picks)
    raw["id"] = f"demo_{int(now)}_{random.randint(100, 999)}"
    bot.relay_times.clear()      # demo friends skip the 5 a day cap so visitors can try this more than once
    world.event("relay", f"{bot.first}'s agent found \"{raw['title']}\"")
    result = await relay.find(bot, raw)
    await relay.flush_digest(bot)       # demo friends send their digest right away
    return f"{bot.first}'s agent found \"{raw['title']}\": {result}"


async def answer_invites(world, plan: dict) -> None:
    """Demo friends say yes to plans they're invited to."""
    for member in plan["invited"]:
        if is_bot(member):
            await squad.answer(world.agent(member), plan["id"], True)


def stage_conflict(world, event: dict) -> None:
    """Make demo friend Sam busy for the first 30 minutes of the student's event, so the counter-proposal shows.

    Staged on purpose and logged as such: a lone visitor has friends with empty calendars, which would never
    show how Freebie Squad handles a conflict.
    """
    if int(event.get("minutes", 60)) < squad.MIN_EVENT_FOR_COUNTER:
        return
    sam = world.agent(BOTS[2])
    starts = parse_local(event["starts"])
    block = [phx(starts - 30 * 60).isoformat(timespec="minutes"), phx(starts + squad.LATE_BY).isoformat(timespec="minutes")]
    sam.settings["busy"] = [block]
    sam.profile["busy"] = [block]
    world.event("squad", "Sam has a class that runs 30 minutes into the event")


def suggested_event(world) -> dict:
    start = phx(world.clock.now() + timedelta(hours=5).total_seconds()).replace(minute=0, second=0, microsecond=0)
    return {"title": "Free Diwali dinner", "location": "Memorial Union", "starts": start.isoformat(timespec="minutes"),
            "minutes": 90, "capacity": 6}
