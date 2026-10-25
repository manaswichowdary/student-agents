"""The message bus. Every rule is enforced here, at send time, not just in agent code.

Rejected: unknown or unverified sender, bad signature, stale timestamp, replayed id, bad payload, hop count > 2,
no active connection, a permission the recipient turned off, and rate limits. Redelivery with the same
idempotency key is a harmless no-op. Every attempt goes to the audit log, delivered or not.
"""
from __future__ import annotations

import hmac
import secrets
from collections import defaultdict, deque
from hashlib import sha256
from typing import Awaitable, Callable

from .connections import Connections
from .identity import Identity
from .schema import Message, SchemaError, validate_payload
from .store import Store
from .util import Clock, canonical

WINDOW = 5 * 60
MAX_HOPS = 2
RATE = {"AVAILABILITY_QUERY": (3, 3600)}    # 3 per hour per sender per recipient
GLOBAL_RATE = (60, 60)                      # any type: 60 per minute per sender per recipient

Handler = Callable[[Message], Awaitable[None]]


def sign(key: bytes, msg: Message) -> str:
    return hmac.new(key, canonical(msg.signing_dict()), sha256).hexdigest()


class Bus:
    def __init__(self, store: Store, clock: Clock, identity: Identity, connections: Connections):
        self.store, self.clock, self.identity, self.connections = store, clock, identity, connections
        self.keys: dict[str, bytes] = {}
        self.handlers: dict[str, Handler] = {}
        self.sent: dict[tuple, deque] = defaultdict(deque)
        self.listeners: list[Callable[[dict], None]] = []

    def register(self, agent_id: str, key: bytes, handler: Handler) -> None:
        self.keys[agent_id] = key
        self.handlers[agent_id] = handler

    def new_message(self, type_: str, sender: str, recipient: str, payload: dict, hop_count: int = 0,
                    idempotency_key: str | None = None) -> Message:
        return Message(id="m_" + secrets.token_hex(8), type=type_, sender=sender, recipient=recipient,
                       payload=payload, hop_count=hop_count, ts=self.clock.now(),
                       idempotency_key=idempotency_key or secrets.token_hex(8))

    async def send(self, msg: Message) -> tuple[bool, str]:
        ok, reason = self._check(msg)
        fields = msg.payload if ok or reason.startswith("duplicate") else {}
        row = {"ts": self.clock.now(), "msg_id": msg.id, "type": msg.type, "sender": msg.sender,
               "recipient": msg.recipient, "result": "delivered" if ok else "rejected", "reason": reason,
               "fields": fields}
        row["seq"] = self.store.audit(*row.values())
        for listen in self.listeners:
            listen(row)
        if ok:
            await self.handlers[msg.recipient](msg)
        return ok, reason

    def _check(self, msg: Message) -> tuple[bool, str]:
        now = self.clock.now()
        key = self.keys.get(msg.sender)
        if key is None or not self.identity.is_verified(msg.sender):
            return False, "unknown sender"
        if not isinstance(msg.signature, str) or not hmac.compare_digest(sign(key, msg), msg.signature):
            return False, "bad signature"
        if abs(now - msg.ts) > WINDOW:
            return False, "timestamp outside the 5 minute window"
        if self.store.one("SELECT 1 FROM seen_messages WHERE id=?", (msg.id,)):
            return False, "replayed message id"
        self.store.x("INSERT INTO seen_messages VALUES (?,?)", (msg.id, now))
        try:
            validate_payload(msg.type, msg.payload)
        except SchemaError as e:
            return False, f"bad payload: {e}"
        if not isinstance(msg.hop_count, int) or msg.hop_count > MAX_HOPS or msg.hop_count < 0:
            return False, "hop limit exceeded"
        if msg.recipient not in self.handlers:
            return False, "unknown recipient"
        if not self.connections.is_active(msg.sender, msg.recipient):
            return False, "no active connection"
        perm_needed = {"OPPORTUNITY_RELAY": "receive_relay", "AVAILABILITY_QUERY": "share_freebusy"}.get(msg.type)
        if perm_needed and not self.connections.perm(msg.recipient, msg.sender, perm_needed):
            return False, f"{msg.recipient.split('@')[0]} turned off {perm_needed.replace('_', ' ')}"
        if msg.type == "AVAILABILITY_REPLY" and msg.payload["distance"] is not None \
                and not self.connections.perm(msg.sender, msg.recipient, "share_distance"):
            return False, "distance not shared: permission is off"
        buckets = [("*", GLOBAL_RATE)] + ([(msg.type, RATE[msg.type])] if msg.type in RATE else [])
        for name, (limit, span) in buckets:
            q = self.sent[(name, msg.sender, msg.recipient)]
            while q and q[0] <= now - span:
                q.popleft()
            if len(q) >= limit:
                return False, "rate limit"
        for name, _ in buckets:
            self.sent[(name, msg.sender, msg.recipient)].append(now)
        idem = f"{msg.sender}|{msg.idempotency_key}"
        if self.store.one("SELECT 1 FROM idempotency WHERE key=?", (idem,)):
            return False, "duplicate (already delivered, ignored)"
        self.store.x("INSERT INTO idempotency VALUES (?,?)", (idem, now))
        return True, "ok"
