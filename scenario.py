"""The scripted demo story. Used by the "Run demo scenario" button and by demo.py.

Dev's agent finds a paid psych study -> two agents verify it (and catch a dead listing, so that source's trust
drops) -> it relays to Maya (a psychology major) -> Maya sees it -> a Freebie Squad plan with a carpool ->
Maya removes Dev, and the next message from Dev's agent is blocked by the bus.
"""
from __future__ import annotations

import asyncio

from core.world import World
from data import seed
from features import freebie_squad as squad
from features import opportunity_relay as relay

MAYA, DEV, SAM = seed.PHONES


async def run(world: World, pause: float = 1.2) -> None:
    async def step(text: str, wait: float = pause) -> None:
        world.event("step", text)
        await asyncio.sleep(wait)

    c = world.connections
    await step("1. Three students sign up with their .edu IDs", pause / 2)
    for email in (MAYA, DEV, SAM):
        if not world.identity.is_verified(email):
            world.start_signup(email)
            code = world.identity.outbox[-1]["code"]
            world.event("identity", f"Code sent to {email} (mock email): {code}")
            await asyncio.sleep(pause / 3)
            world.verify(email, code)
            await asyncio.sleep(pause / 3)

    await step("2. Connections need consent from both sides")
    for target in ("tchen5", "zz999"):
        reply = c.request(DEV, target)
        world.event("connection", f"Dev requests {target}@asu.edu → \"{reply}\" (same reply either way)")
    await asyncio.sleep(pause / 2)
    c.request(DEV, MAYA)
    c.request(DEV, SAM)
    c.request(MAYA, SAM)
    world.event("connection", "Dev sends requests to Maya and Sam. Maya sends one to Sam.")
    await asyncio.sleep(pause)
    c.accept(MAYA, DEV)
    c.accept(SAM, DEV)
    c.accept(SAM, MAYA)
    c.request("pnair3", MAYA)
    c.deny(MAYA, "pnair3@asu.edu")
    world.event("connection", "Maya and Sam accept. Maya quietly denies Priya: Priya still just sees \"pending\".")

    await step("3. Dev's agent finds listings (from Freebie Radar)")
    dev = world.agent(DEV)
    result = await relay.find(dev, seed.LISTINGS["scam_ra"])
    world.event("relay", f"Scam check → {result}: never shared")
    await asyncio.sleep(pause)
    await step("4. Dead-Link Detective: two agents check a listing from an old newsletter")
    result = await relay.find(dev, seed.LISTINGS["focus_group"])
    world.event("relay", f"Paid focus group → {result}. Not shared.")
    await asyncio.sleep(pause)

    await step("5. A paid psych study: verified, then relayed to friends it might fit")
    result = await relay.find(dev, seed.LISTINGS["sleep_study"])
    world.event("relay", f"Sleep study → {result}. Each friend's agent decides privately if it fits.")
    await asyncio.sleep(pause)

    await step("6. Maya and Sam let Dev's agent ask if they're free (their choice, per connection)")
    c.set_perm(MAYA, DEV, "share_freebusy", True)
    c.set_perm(MAYA, DEV, "share_distance", True)
    c.set_perm(SAM, DEV, "share_freebusy", True)
    await asyncio.sleep(pause / 2)
    await step("7. Freebie Squad: Dev plans the free Diwali dinner, with a carpool")
    plan = await squad.start(dev, seed.EVENTS["diwali"])
    await asyncio.sleep(pause)
    await squad.answer(world.agent(MAYA), plan["id"], True)
    await asyncio.sleep(pause / 2)
    await squad.answer(world.agent(SAM), plan["id"], True)
    await asyncio.sleep(pause / 2)
    await squad.confirm(dev, plan["id"])
    world.event("squad", "Pickup spots go only to confirmed riders, after they confirm.")
    await asyncio.sleep(pause)

    await step("8. Maya removes Dev")
    await world.end_connection(MAYA, DEV)
    await asyncio.sleep(pause)
    world.event("step", "9. Dev's agent tries an old route to Maya. The bus blocks it.")
    stale = relay.parse_listing(seed.LISTINGS["travel_grant"]).to_dict()
    await dev.send("OPPORTUNITY_RELAY", MAYA, {"listing": stale, "closes": "Fri Nov 20", "verified": True}, hop_count=1)
    world.event("step", "Done. Every message above is in the audit log, delivered or blocked.")
