#!/usr/bin/env python3
"""FastAPI server.

  pip install -r requirements.txt && python server.py      then open http://localhost:8000

  /        the real website: each student signs in and acts only as themselves (live.py)
  /demo    the scripted demo: one page plays Maya, Dev and Sam side by side
"""
from __future__ import annotations

import asyncio
import os
from pathlib import Path

import uvicorn
from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse
from pydantic import BaseModel

import scenario
from core.identity import IdentityError, normalize
from core.util import fmt_time
from core.world import World
from data import seed
from features import freebie_squad as squad
from features import opportunity_relay as relay

ROOT = Path(__file__).resolve().parent
from fastapi import APIRouter

app = FastAPI(title="Student Agents")
demo_api = APIRouter(prefix="/demo")
clients: set[asyncio.Queue] = set()
state: dict = {}


def fresh() -> None:
    world = World()
    world.listeners.append(lambda row: [q.put_nowait(row) for q in list(clients)])
    state.update(world=world, running=False)


fresh()


def W() -> World:
    return state["world"]


class Body(BaseModel):
    user: str = ""
    email: str = ""
    code: str = ""
    target: str = ""
    other: str = ""
    perm: str = ""
    value: bool = False
    plan_id: str = ""
    accept: bool = False
    listing: str = ""


def guard(fn):
    try:
        return fn()
    except (IdentityError, ValueError, PermissionError, KeyError) as e:
        raise HTTPException(400, str(e).strip("'"))


def phone(email: str) -> dict:
    world = W()
    if email in world.agents:
        return {"verified": True, **world.agents[email].view()}
    pending = next((o for o in reversed(world.identity.outbox) if o["to"] == email), None)
    return {"verified": False, "email": email, "code_sent": bool(pending), "mock_code": pending["code"] if pending else None}


@demo_api.get("")
def demo_page():
    return FileResponse(ROOT / "web" / "demo.html")


@demo_api.get("/api/state")
def get_state():
    world = W()
    return {"clock": fmt_time(world.clock.now()), "running": state["running"],
            "phones": [phone(e) for e in seed.PHONES], "trust": world.trust.table(), "log": world.log[-300:],
            "listings": [{"key": k, "title": v["title"], "source": v["source"]} for k, v in seed.LISTINGS.items()],
            "event": seed.EVENTS["diwali"],
            "names": {e: a.first for e, a in world.agents.items()}}


@demo_api.post("/api/signup")
def signup(b: Body):
    return {"email": guard(lambda: W().start_signup(b.email))}


@demo_api.post("/api/verify")
def verify(b: Body):
    return guard(lambda: W().verify(b.email, b.code))


@demo_api.post("/api/connect")
def connect(b: Body):
    return {"message": guard(lambda: W().connections.request(b.user, b.target))}


@demo_api.post("/api/accept")
def accept(b: Body):
    guard(lambda: W().connections.accept(b.user, b.other))
    W().event("connection", f"{W().agent(b.user).first} accepted a request")
    return {"ok": True}


@demo_api.post("/api/deny")
def deny(b: Body):
    guard(lambda: W().connections.deny(b.user, b.other))
    return {"ok": True}


@demo_api.post("/api/remove")
async def remove(b: Body):
    await W().end_connection(normalize(b.user), normalize(b.other))
    return {"ok": True}


@demo_api.post("/api/block")
async def block(b: Body):
    await W().end_connection(normalize(b.user), normalize(b.other), block=True)
    return {"ok": True}


@demo_api.post("/api/perm")
def perm(b: Body):
    guard(lambda: W().connections.set_perm(b.user, b.other, b.perm, b.value))
    W().event("permission", f"{W().agent(b.user).first} turned {'on' if b.value else 'off'} "
                            f"{b.perm.replace('_', ' ')} for {W().agent(b.other).first}")
    return {"ok": True}


@demo_api.post("/api/find")
async def find(b: Body):
    raw = seed.LISTINGS.get(b.listing)
    if not raw:
        raise HTTPException(400, "Unknown listing")
    return {"result": await relay.find(W().agent(b.user), raw)}


@demo_api.post("/api/squad")
async def start_squad(b: Body):
    plan = await squad.start(W().agent(b.user), seed.EVENTS["diwali"])
    return {"plan": plan["id"]}


@demo_api.post("/api/plan/answer")
async def plan_answer(b: Body):
    ok, reason = await squad.answer(W().agent(b.user), b.plan_id, b.accept)
    return {"ok": ok, "reason": reason}


@demo_api.post("/api/plan/confirm")
async def plan_confirm(b: Body):
    plan = await squad.confirm(W().agent(b.user), b.plan_id)
    return {"state": plan["state"]}


@demo_api.post("/api/digest")
async def digest():
    return {"sent": await W().digest()}


@demo_api.post("/api/reset")
def reset():
    fresh()
    for q in list(clients):
        q.put_nowait({"kind": "reset"})
    return {"ok": True}


@demo_api.post("/api/demo")
async def demo():
    if state["running"]:
        return {"ok": False}
    reset()
    state["running"] = True

    async def go():
        try:
            await scenario.run(W(), pause=float(os.environ.get("DEMO_PAUSE", "1.4")))
        finally:
            state["running"] = False
            for q in list(clients):
                q.put_nowait({"kind": "done"})
    asyncio.create_task(go())
    return {"ok": True}


@demo_api.websocket("/ws")
async def ws(socket: WebSocket):
    await socket.accept()
    q: asyncio.Queue = asyncio.Queue()
    clients.add(q)
    try:
        while True:
            await socket.send_json(await q.get())
    except (WebSocketDisconnect, RuntimeError):
        pass
    finally:
        clients.discard(q)


app.include_router(demo_api)

from live import router as live_router  # noqa: E402

app.include_router(live_router)

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", 8000)), proxy_headers=True,
                forwarded_allow_ips="*")
