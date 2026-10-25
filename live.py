"""The real website's API. Every request acts as the signed in student only (cookie session).

Data lives in a SQLite file (DB_PATH, default data/live.db). Sign in codes are emailed when RESEND_API_KEY is
set; otherwise the site runs in demo mode and shows the code on screen.
"""
from __future__ import annotations

import asyncio
import hashlib
import os
import secrets
import time
from collections import defaultdict, deque
from pathlib import Path

from fastapi import APIRouter, HTTPException, Request, Response
from fastapi.responses import FileResponse
from pydantic import BaseModel

from core.identity import IdentityError, normalize
from core.mailer import email_enabled, send_code
from core.schema import SchemaError, parse_listing
from core.util import fmt_time
from core.world import World
from features import campus_feed, demo_friends
from features import freebie_squad as squad
from features import opportunity_relay as relay

ROOT = Path(__file__).resolve().parent
COOKIE = "sa_session"
SESSION_DAYS = 14
router = APIRouter()
LOCK = asyncio.Lock()
_world: dict = {}
_starts: dict[str, deque] = defaultdict(deque)


def world() -> World:
    if "w" not in _world:
        _world["w"] = World(db=os.environ.get("DB_PATH", str(ROOT / "data" / "live.db")), live=True)
    return _world["w"]


def fail(e: Exception, code: int = 400):
    raise HTTPException(code, str(e).strip("'\""))


# ------------------------------------------------------------------------------------------ sessions
def _hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def current_user(request: Request) -> str | None:
    token = request.cookies.get(COOKIE)
    if not token:
        return None
    row = world().store.one("SELECT email, created FROM sessions WHERE token_hash=?", (_hash(token),))
    if not row or time.time() - row["created"] > SESSION_DAYS * 86400:
        return None
    return row["email"] if row["email"] in world().agents else None


def need_user(request: Request) -> str:
    user = current_user(request)
    if not user:
        raise HTTPException(401, "Sign in first")
    return user


def start_session(response: Response, request: Request, email: str) -> None:
    token = secrets.token_urlsafe(32)
    world().store.x("INSERT INTO sessions VALUES (?,?,?)", (_hash(token), email, time.time()))
    https = request.headers.get("x-forwarded-proto", request.url.scheme) == "https"
    response.set_cookie(COOKIE, token, max_age=SESSION_DAYS * 86400, httponly=True, samesite="lax", secure=https)


# ------------------------------------------------------------------------------------------ pages
@router.get("/")
def home():
    return FileResponse(ROOT / "web" / "index.html")


# ------------------------------------------------------------------------------------------ auth
class Auth(BaseModel):
    email: str = ""
    code: str = ""
    name: str = ""
    major: str = ""
    year: str = ""


@router.post("/api/auth/start")
async def auth_start(b: Auth, request: Request):
    ip = request.headers.get("x-forwarded-for", request.client.host if request.client else "?").split(",")[0]
    q = _starts[ip]
    while q and q[0] < time.time() - 3600:
        q.popleft()
    if len(q) >= 10:
        raise HTTPException(429, "Too many codes from this network. Try again in an hour.")
    if demo_friends.is_bot(normalize(b.email)):
        raise HTTPException(400, "That's one of the demo friends. Sign in with your own ASU email.")
    async with LOCK:
        w = world()
        try:
            email = w.start_signup(b.email)
        except IdentityError as e:
            fail(e)
        q.append(time.time())
        code = w.identity.outbox.pop()["code"]
        try:
            emailed = send_code(email, code)
        except RuntimeError as e:
            fail(e, 502)
        if not emailed:      # demo mode: the code goes on screen, so this sign in doesn't prove who owns the inbox
            w.identity.mark_shown_on_screen(email)
    return {"email": email, "emailed": emailed, "demo_code": None if emailed else code,
            "in_directory": w.identity.in_directory(email)}


@router.post("/api/auth/check")
async def auth_check(b: Auth):
    """Checks the code. Students in the directory are signed in by /verify; others fill in a profile first."""
    w = world()
    try:
        w.identity.check_code(b.email, b.code)
    except IdentityError as e:
        fail(e)
    email = normalize(b.email)
    known = w.identity.in_directory(email) or w.identity.is_verified(email)
    prof = w.identity.profile(email) if known else None
    return {"needs_profile": not known, "profile": prof}


@router.post("/api/auth/verify")
async def auth_verify(b: Auth, request: Request, response: Response):
    async with LOCK:
        w = world()
        email = normalize(b.email)
        try:
            if w.identity.is_verified(email) and not w.identity.in_directory(email):
                acc = w.identity.account(email)          # returning student: keep their saved profile
                w.verify(email, b.code, acc["name"], acc["major"], acc["year"])
            else:
                w.verify(email, b.code, b.name, b.major, b.year)
        except IdentityError as e:
            fail(e)
        start_session(response, request, email)
        # Auto-add demo friends for demo accounts so testers land on a ready profile
        from data.seed import DEMO_ACCOUNTS
        if email in DEMO_ACCOUNTS:
            demo_friends.befriend_all(w, email)
        try:
            await asyncio.wait_for(campus_feed.scan(w, email), timeout=15)
        except Exception:  # noqa: BLE001  never block sign in on the feed
            pass
        w.save()
    return {"ok": True}


@router.post("/api/auth/logout")
def auth_logout(request: Request, response: Response):
    token = request.cookies.get(COOKIE)
    if token:
        world().store.x("DELETE FROM sessions WHERE token_hash=?", (_hash(token),))
    response.delete_cookie(COOKIE)
    return {"ok": True}


# ------------------------------------------------------------------------------------------ the student's view
@router.get("/api/me")
def me(request: Request):
    w = world()
    user = current_user(request)
    if not user:
        return {"signed_in": False, "email_mode": "email" if email_enabled() else "demo"}
    a = w.agent(user)
    acc = w.identity.account(user)
    view = a.view()
    bots = set(demo_friends.BOTS)
    for f in view["friends"]:
        f["demo"] = f["email"] in bots
    view["plans"] = sorted(view["plans"], key=lambda p: -p.get("starts", 0))
    now = w.clock.now()
    a.feed = [f for f in a.feed if f.get("closes_ts", now + 1) > now]      # drop events that already started
    view["feed"] = sorted(a.feed, key=lambda f: f.get("closes_ts", 0))     # soonest first
    return {"signed_in": True, "email_mode": "email" if email_enabled() else "demo", "self_reported": bool(acc["self_reported"]),
            "unverified": acc.get("proof") == "screen",
            "can_scan": campus_feed.eligible(user),
            "settings": a.settings, "has_demo_friends": any(w.connections.is_active(user, b) for b in bots),
            "now": fmt_time(w.clock.now()), "places": sorted(w.places), "suggested_event": demo_friends.suggested_event(w),
            **view}


@router.get("/api/timeline")
def timeline(request: Request):
    """Every message this student's agent sent or received, delivered or blocked, plus its notes."""
    user = need_user(request)
    w = world()
    rows = w.store.all("SELECT * FROM audit WHERE sender=? OR recipient=? ORDER BY seq DESC LIMIT 80", (user, user))
    names = {e: a.first for e, a in w.agents.items()}
    out = []
    import json as _json
    for r in rows:
        out.append({"ts": r["ts"], "when": fmt_time(r["ts"]), "type": r["type"], "result": r["result"],
                    "reason": r["reason"], "direction": "sent" if r["sender"] == user else "received",
                    "other": names.get(r["recipient"] if r["sender"] == user else r["sender"], "someone"),
                    "fields": _json.loads(r["fields"])})
    return {"messages": out}


@router.get("/api/trust")
def trust():
    return {"sources": world().trust.table()}


# ------------------------------------------------------------------------------------------ actions
class Act(BaseModel):
    target: str = ""
    other: str = ""
    perm: str = ""
    value: bool = False
    plan_id: str = ""
    accept: bool = False
    title: str = ""
    org: str = ""
    type: str = ""
    deadline: str = ""
    url: str = ""
    source: str = ""
    tags: str = ""
    pay: float | None = None
    location: str = ""
    starts: str = ""
    capacity: int = 6
    zone: str = ""
    has_car: bool = False
    seats: int = 0
    interests: str = ""


async def act(request: Request, fn):
    async with LOCK:
        user = need_user(request)
        w = world()
        try:
            out = fn(w, user)
            if asyncio.iscoroutine(out):
                out = await out
        except (IdentityError, ValueError, PermissionError, SchemaError, KeyError) as e:
            fail(e)
        finally:
            w.save()
        return out if isinstance(out, dict) else {"ok": True, "result": out}


@router.post("/api/settings")
async def settings(b: Act, request: Request):
    def go(w, user):
        a = w.agent(user)
        zone = b.zone if b.zone in w.pickup_spots else ""
        a.settings = {"zone": zone, "has_car": b.has_car, "seats": max(0, min(b.seats, 6)) if b.has_car else 0,
                      "interests": [i.strip().lower()[:30] for i in b.interests.split(",") if i.strip()][:8],
                      "busy": a.settings.get("busy", [])}
        a.profile.update(a.settings)
        return "Saved"
    return await act(request, go)


@router.post("/api/friends/request")
async def friend_request(b: Act, request: Request):
    def go(w, user):
        msg = w.connections.request(user, b.target)
        target = normalize(b.target)
        if demo_friends.is_bot(target) and w.connections.incoming(target):
            demo_friends.befriend(w, user, target)
        return {"message": msg}
    return await act(request, go)


@router.post("/api/friends/{action}")
async def friend_action(action: str, b: Act, request: Request):
    async def go(w, user):
        other = normalize(b.other)
        if action == "accept":
            w.connections.accept(user, other)
        elif action == "deny":
            w.connections.deny(user, other)
        elif action in ("remove", "block"):
            await w.end_connection(user, other, block=action == "block")
        elif action == "perm":
            w.connections.set_perm(user, other, b.perm, b.value)
        else:
            raise ValueError("Unknown action")
        return "done"
    return await act(request, go)


@router.post("/api/demo-friends")
async def add_demo_friends(request: Request):
    return await act(request, lambda w, user: demo_friends.befriend_all(w, user) or "Added Maya, Dev and Sam")


@router.post("/api/demo-find")
async def demo_find(request: Request):
    return await act(request, lambda w, user: demo_friends.friend_shares_a_find(w, user))


@router.post("/api/scan")
async def scan_now(request: Request):
    return await act(request, lambda w, user: campus_feed.scan(w, user))


async def scan_everyone() -> None:
    """Every 30 minutes each ASU student's agent checks the campus feed on its own."""
    while True:
        await asyncio.sleep(30 * 60)
        try:
            async with LOCK:
                w = world()
                for email in list(w.agents):
                    if not demo_friends.is_bot(email) and campus_feed.eligible(email):
                        await campus_feed.scan(w, email)
                w.save()
        except Exception as e:  # noqa: BLE001  keep the loop alive
            print("campus scan failed:", e)


@router.post("/api/share")
async def share(b: Act, request: Request):
    async def go(w, user):
        raw = {"title": b.title, "org": b.org, "type": b.type, "deadline": b.deadline or None, "url": b.url.strip(),
               "source": b.source, "eligibility_tags": [t.strip().lower() for t in b.tags.split(",") if t.strip()],
               "pay_amount": b.pay, "location": b.location}
        parse_listing(raw)      # clear errors before the agent runs
        result = await relay.find(w.agent(user), raw)
        await relay.flush_digest(w.agent(user)) if os.environ.get("INSTANT_DIGEST", "1") == "1" else None
        return {"result": result}
    return await act(request, go)


@router.post("/api/squad/start")
async def squad_start(b: Act, request: Request):
    async def go(w, user):
        if not b.title.strip() or not b.starts:
            raise ValueError("Add the event name and start time")
        event = {"title": b.title.strip()[:120], "location": b.location or "Memorial Union", "starts": b.starts,
                 "minutes": 90, "capacity": max(2, min(b.capacity, 12))}
        if w.connections.is_active(user, demo_friends.BOTS[2]):
            demo_friends.stage_conflict(w, event)
        plan = await squad.start(w.agent(user), event)
        await demo_friends.answer_invites(w, plan)
        return {"plan": plan["id"], "invited": len(plan["invited"]), "transport": plan["transport"],
                "later": len(plan["late"])}
    return await act(request, go)


@router.post("/api/squad/answer")
async def squad_answer(b: Act, request: Request):
    async def go(w, user):
        ok, reason = await squad.answer(w.agent(user), b.plan_id, b.accept)
        if not ok:
            raise ValueError(reason)
        return "sent"
    return await act(request, go)


@router.post("/api/squad/confirm")
async def squad_confirm(b: Act, request: Request):
    async def go(w, user):
        plan = await squad.confirm(w.agent(user), b.plan_id)
        return {"state": plan["state"]}
    return await act(request, go)
