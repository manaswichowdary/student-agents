"""Dead-Link Detective: before a find is surfaced or relayed, 2 to 3 connections' agents check it.

Verification requests carry only the listing fields. Each verifier checks the link and the deadline, then the
LLM reasons over the evidence (with hard rules on top: past deadline = closed, login wall or timeout = unknown).
A result needs at least 2 distinct, independent verifiers agreeing. Votes are weighted by reputation.
Source trust = (confirmed + 1) / (total + 2), with older checks decaying 0.95 per week.
"""
from __future__ import annotations

import hashlib
import os
import secrets
import urllib.request

from core.llm import llm
from core.schema import Listing, parse_listing
from core.util import WEEK

SKIP_ABOVE = 0.85
FLAG_BELOW = 0.3
MAX_VERIFIERS = 3


# ------------------------------------------------------------------------------------------- trust book
class TrustBook:
    def __init__(self, store, clock):
        self.store, self.clock = store, clock

    def record(self, source: str, confirmed: bool, ts: float | None = None) -> None:
        self.store.x("INSERT INTO trust_checks VALUES (?,?,?)", (source, ts or self.clock.now(), int(confirmed)))

    def score(self, source: str) -> float:
        rows = self.store.all("SELECT ts, confirmed FROM trust_checks WHERE source=?", (source,))
        now = self.clock.now()
        total = confirmed = 0.0
        for r in rows:
            w = 0.95 ** max(0.0, (now - r["ts"]) / WEEK)
            total += w
            confirmed += w * r["confirmed"]
        return (confirmed + 1) / (total + 2)

    def label(self, source: str) -> str:
        if not self.store.one("SELECT 1 FROM trust_checks WHERE source=?", (source,)):
            return "unverified"
        s = self.score(source)
        return "trusted" if s > SKIP_ABOVE else "flagged" if s < FLAG_BELOW else "checking"

    def table(self) -> list[dict]:
        sources = [r["source"] for r in self.store.all("SELECT DISTINCT source FROM trust_checks ORDER BY source")]
        return [{"source": s, "score": round(self.score(s), 3), "label": self.label(s),
                 "checks": self.store.one("SELECT COUNT(*) c FROM trust_checks WHERE source=?", (s,))["c"]}
                for s in sources]

    def reputation(self, verifier: str) -> float:
        row = self.store.one("SELECT weight FROM reputation WHERE verifier=?", (verifier,))
        return row["weight"] if row else 1.0

    def set_reputation(self, verifier: str, weight: float) -> None:
        self.store.x("INSERT OR REPLACE INTO reputation VALUES (?,?)", (verifier, round(weight, 4)))


# ------------------------------------------------------------------------------------------- checking a link
def check_url(world, verifier: str, url: str) -> dict:
    """Mock web by default. REAL_HTTP=1 does a real GET with a 5 second timeout."""
    if world.shared_fetch and url in world.fetch_cache:
        return world.fetch_cache[url]                 # same underlying check: counts once
    if url not in world.web and os.environ.get("REAL_HTTP") == "1":
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "student-agents/0.1"})
            with urllib.request.urlopen(req, timeout=5) as r:
                body = r.read(20000).decode("utf-8", "replace").lower()
                page = {"status": r.status, "login_wall": "sign in" in body and "password" in body,
                        "timeout": False, "says_closed": any(w in body for w in ("no longer accepting", "applications are closed", "this study is full"))}
        except Exception as e:
            page = {"status": getattr(e, "code", 0) or 0, "login_wall": False, "timeout": "timed out" in str(e),
                    "says_closed": False}
    else:
        page = world.web.get(url, {"status": 0, "login_wall": False, "timeout": True, "says_closed": False})
    check_id = hashlib.sha256(f"{url}|{verifier}|{world.clock.now()}".encode()).hexdigest()[:12]
    result = {**page, "check_id": check_id}
    if world.shared_fetch:
        world.fetch_cache[url] = result
    return result


def hard_rules(listing: Listing, page: dict, now: float) -> str | None:
    deadline = listing.deadline_ts()
    if deadline and deadline < now:
        return "closed"
    if page["timeout"] or page["login_wall"]:
        return "unknown"
    if page["status"] in (404, 410):
        return "closed"
    if page["status"] != 200:
        return "unknown"
    return None


def rule_verdict(page: dict) -> tuple[str, str]:
    if page["says_closed"]:
        return "closed", "The page says it is no longer accepting people"
    return "open", "Page loads, deadline ahead, nothing says it's closed"


def describe(page: dict) -> str:
    if page["timeout"]:
        return "timeout"
    bits = [f"HTTP {page['status']}"]
    if page["login_wall"]:
        bits.append("login wall")
    if page["says_closed"]:
        bits.append("page says closed")
    return ", ".join(bits)


async def on_verify_request(agent, msg) -> None:
    world = agent.world
    listing = parse_listing(msg.payload["listing"])
    now = world.clock.now()
    if listing.id in agent.my_finds:            # never vote on your own owner's finds
        status, reason, http, check_id = "unknown", "recused: this is my owner's own find", "not checked", "recused"
    else:
        page = check_url(world, agent.id, listing.url)
        http, check_id = describe(page), page["check_id"]
        forced = hard_rules(listing, page, now)
        evidence = {"http": http, "deadline": listing.deadline or "none listed",
                    "deadline_passed": bool(listing.deadline_ts() and listing.deadline_ts() < now)}
        out, source = llm("verify", {"listing": listing.to_dict(), "evidence": evidence})
        if out:
            status, reason = out["status"], out["reason"]
        else:
            status, reason = rule_verdict(page)
        if forced:
            if forced != status:
                reason = {"closed": "Past deadline or page gone", "unknown": "Login wall or no response"}[forced]
            status = forced
        world.event("verify", f"{agent.first}'s agent checked \"{listing.title}\": {status} ({http}, {source})")
    deadline = listing.deadline or "none listed"
    await agent.send("VERIFY_RESULT", msg.sender, {
        "request_id": msg.payload["request_id"], "listing_id": listing.id, "status": status,
        "evidence": {"http": http, "deadline": deadline, "reason": reason[:200], "check_id": check_id}})


async def on_verify_result(agent, msg) -> None:
    job = agent.verifications.get(msg.payload["request_id"])
    if job and msg.sender in job["asked"] and msg.sender not in {r["by"] for r in job["results"]}:
        job["results"].append({"by": msg.sender, **msg.payload})


def consensus(results: list[dict], reputation) -> tuple[str, list[dict]]:
    """At least 2 distinct, independent verifiers must agree. Recused votes and repeated checks count once."""
    seen_checks, votes = set(), []
    for r in results:
        cid = r["evidence"]["check_id"]
        if cid == "recused" or cid in seen_checks:
            continue
        seen_checks.add(cid)
        votes.append(r)
    best, best_weight = "unknown", 0.0
    for status in ("open", "closed", "unknown"):
        group = [v for v in votes if v["status"] == status]
        weight = sum(reputation(v["by"]) for v in group)
        if len({v["by"] for v in group}) >= 2 and weight > best_weight:
            best, best_weight = status, weight
    return best, votes


async def verify_listing(agent, listing: Listing) -> tuple[str, str]:
    world = agent.world
    trust = world.trust
    score = trust.score(listing.source)
    if trust.label(listing.source) == "trusted":
        world.event("trust", f"{listing.source} is trusted ({score:.2f}), so no check needed")
        return "open", "trusted source"
    if score < FLAG_BELOW:
        return "flagged", f"source {listing.source} is flagged ({score:.2f})"
    friends = sorted(world.connections.active(agent.id), key=trust.reputation, reverse=True)[:MAX_VERIFIERS]
    if len(friends) < 2:
        return "unknown", "needs at least 2 connections to verify"
    rid = "v_" + secrets.token_hex(5)
    job = agent.verifications[rid] = {"listing": listing.id, "asked": set(friends), "results": []}
    world.event("verify", f"{agent.first}'s agent asks {len(friends)} agents to check \"{listing.title}\"")
    for friend in friends:
        await agent.send("VERIFY_REQUEST", friend, {"listing": listing.to_dict(), "request_id": rid})
    verdict, votes = consensus(job["results"], trust.reputation)
    if verdict != "unknown":
        for v in votes:
            w = trust.reputation(v["by"])
            trust.set_reputation(v["by"], min(1.0, w + 0.05) if v["status"] == verdict else w * 0.8)
        before = score
        trust.record(listing.source, verdict == "open")
        world.event("trust", f"{listing.source}: {before:.2f} → {trust.score(listing.source):.2f}", source=listing.source)
    return verdict, {"open": "verified open", "closed": "verifiers found it closed",
                     "unknown": "verifiers couldn't confirm it"}[verdict]
