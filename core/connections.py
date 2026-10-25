"""Consent based connections.

pending -> active -> removed, plus expired (7 days with no answer) and blocked.
Deny is silent: the requester keeps seeing "pending" until it expires.
Agents can exchange nothing unless the state is active; the bus checks this at send time.
"""
from __future__ import annotations

from typing import Callable

from .identity import Identity, normalize
from .store import Store
from .util import DAY, Clock

REQUEST_TTL = 7 * DAY
DAILY_REQUESTS = 10
NEUTRAL = "If this student exists, they've been notified."
LIMIT = "You've sent 10 requests today. Try again tomorrow."
PERMS = {"receive_relay": True, "share_freebusy": False, "share_distance": False}


def pair(a: str, b: str) -> tuple[str, str]:
    return (a, b) if a < b else (b, a)


class Connections:
    def __init__(self, store: Store, clock: Clock, identity: Identity):
        self.store, self.clock, self.identity = store, clock, identity
        self.on_disconnect: list[Callable[[str, str], None]] = []

    # ---------- requests ----------
    def request(self, sender: str, target: str) -> str:
        """Always returns the same neutral text, whether or not the target exists (anti enumeration)."""
        sender, target = normalize(sender), normalize(target)
        if not self.identity.is_verified(sender):
            raise PermissionError("Verify your .edu account first")
        since = self.clock.now() - DAY
        sent = self.store.one("SELECT COUNT(*) c FROM request_log WHERE sender=? AND ts>?", (sender, since))["c"]
        if sent >= DAILY_REQUESTS:
            return LIMIT
        self.store.x("INSERT INTO request_log VALUES (?,?)", (sender, self.clock.now()))
        if target == sender or not self.identity.is_verified(target):
            return NEUTRAL
        a, b = pair(sender, target)
        row = self._row(a, b)
        if row and row["state"] in ("blocked", "active"):
            return NEUTRAL
        if row and row["state"] in ("pending", "denied") and self.clock.now() < row["created"] + REQUEST_TTL:
            return NEUTRAL
        now = self.clock.now()
        self.store.x("INSERT OR REPLACE INTO connections VALUES (?,?,?,?,?,?,NULL)",
                     (a, b, "pending", sender, now, now))
        return NEUTRAL

    def incoming(self, user: str) -> list[dict]:
        """Request cards: the requester's verified name, major and year, straight from the directory."""
        user = normalize(user)
        out = []
        for r in self.store.all("SELECT * FROM connections WHERE (a=? OR b=?) AND state='pending' AND requested_by!=?",
                                (user, user, user)):
            if self._expired(r):
                continue
            acc = self.identity.account(r["requested_by"])
            out.append({"from": acc["email"], "name": acc["name"], "major": acc["major"], "year": acc["year"]})
        return out

    def outgoing(self, user: str) -> list[dict]:
        """What a requester sees: only pending or expired. Never 'denied', never the name before acceptance."""
        user = normalize(user)
        out = []
        for r in self.store.all("SELECT * FROM connections WHERE requested_by=? AND state IN ('pending','denied')",
                                (user,)):
            other = r["b"] if r["a"] == user else r["a"]
            out.append({"to": other, "status": "expired" if self._expired(r) else "pending"})
        return out

    def accept(self, user: str, requester: str) -> None:
        self._answer(user, requester, "active")

    def deny(self, user: str, requester: str) -> None:
        self._answer(user, requester, "denied")

    def _answer(self, user: str, requester: str, state: str) -> None:
        user, requester = normalize(user), normalize(requester)
        a, b = pair(user, requester)
        row = self._row(a, b)
        if not row or row["state"] != "pending" or row["requested_by"] != requester or self._expired(row):
            raise ValueError("No open request from that student")
        self.store.x("UPDATE connections SET state=?, updated=? WHERE a=? AND b=?", (state, self.clock.now(), a, b))

    # ---------- ending a connection ----------
    def remove(self, user: str, other: str) -> None:
        self._end(user, other, "removed")

    def block(self, user: str, other: str) -> None:
        self._end(user, other, "blocked", force=True)

    def _end(self, user: str, other: str, state: str, force: bool = False) -> None:
        user, other = normalize(user), normalize(other)
        a, b = pair(user, other)
        row = self._row(a, b)
        if not row and not force:
            raise ValueError("You're not connected")
        if row:
            self.store.x("UPDATE connections SET state=?, updated=?, blocked_by=? WHERE a=? AND b=?",
                         (state, self.clock.now(), user if state == "blocked" else None, a, b))
        else:
            self.store.x("INSERT INTO connections VALUES (?,?,?,?,?,?,?)",
                         (a, b, state, user, self.clock.now(), self.clock.now(), user))
        self.store.x("DELETE FROM permissions WHERE (owner=? AND other=?) OR (owner=? AND other=?)",
                     (user, other, other, user))
        for hook in self.on_disconnect:      # cancel plans, drop queued relays, right away
            hook(user, other)

    # ---------- queries ----------
    def is_active(self, x: str, y: str) -> bool:
        row = self._row(*pair(normalize(x), normalize(y)))
        return bool(row) and row["state"] == "active"

    def active(self, user: str) -> list[str]:
        user = normalize(user)
        rows = self.store.all("SELECT * FROM connections WHERE (a=? OR b=?) AND state='active'", (user, user))
        return [r["b"] if r["a"] == user else r["a"] for r in rows]

    def friends(self, user: str) -> list[dict]:
        out = []
        for other in self.active(user):
            acc = self.identity.account(other)
            out.append({"email": other, "name": acc["name"], "major": acc["major"], "year": acc["year"],
                        "perms": {p: self.perm(user, other, p) for p in PERMS}})
        return out

    # ---------- permissions (each student controls their own side) ----------
    def perm(self, owner: str, other: str, name: str) -> bool:
        row = self.store.one("SELECT value FROM permissions WHERE owner=? AND other=? AND perm=?",
                             (normalize(owner), normalize(other), name))
        return bool(row["value"]) if row else PERMS[name]

    def set_perm(self, owner: str, other: str, name: str, value: bool) -> None:
        if name not in PERMS:
            raise ValueError("Unknown permission")
        if not self.is_active(owner, other):
            raise ValueError("You're not connected")
        self.store.x("INSERT OR REPLACE INTO permissions VALUES (?,?,?,?)",
                     (normalize(owner), normalize(other), name, int(bool(value))))

    def _row(self, a: str, b: str):
        return self.store.one("SELECT * FROM connections WHERE a=? AND b=?", (a, b))

    def _expired(self, row) -> bool:
        return row["state"] in ("pending", "denied") and self.clock.now() > row["created"] + REQUEST_TTL
