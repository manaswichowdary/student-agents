"""StudentAgent: one per verified student. Holds a private profile that never leaves it."""
from __future__ import annotations

import secrets
from collections import deque
from typing import TYPE_CHECKING

from .bus import sign
from .schema import Message

if TYPE_CHECKING:
    from .world import World


class StudentAgent:
    def __init__(self, world: "World", email: str):
        self.world = world
        self.id = email
        self.key = secrets.token_bytes(32)            # per agent HMAC key, registered with the bus
        acc = world.identity.account(email)
        self.name = acc["name"]
        self.first = self.name.split()[0]
        self.settings: dict = {}                       # the student's own choices: campus, car, interests
        self.profile = {**world.identity.profile(email), **world.private_profiles.get(email, {})}
        self.feed: list[dict] = []                    # what the owner sees on their phone
        self.activity: deque = deque(maxlen=40)        # short notes for the owner
        self.seen: set[str] = set()                   # listing ids already handled (dedupe)
        self.my_finds: set[str] = set()
        self.relay_times: deque = deque()             # outgoing relays in the last day (cap 5)
        self.relay_queue: list[dict] = []             # relays waiting for the daily digest
        self.verifications: dict[str, dict] = {}
        self.queries: dict[str, dict] = {}
        self.plans: dict[str, dict] = {}              # plans this agent started
        self.invites: dict[str, dict] = {}            # plans this agent was invited to
        world.bus.register(email, self.key, self.handle)

    # ---------- saving and loading (so a restart doesn't lose a student's feed or plans) ----------
    SAVED = ("feed", "seen", "my_finds", "relay_times", "relay_queue", "plans", "invites", "activity", "settings")

    def to_state(self) -> dict:
        out = {}
        for k in self.SAVED:
            v = getattr(self, k)
            out[k] = sorted(v) if isinstance(v, set) else list(v) if isinstance(v, deque) else v
        return out

    def load_state(self, data: dict) -> None:
        for k in self.SAVED:
            if k not in data:
                continue
            cur = getattr(self, k)
            if isinstance(cur, set):
                setattr(self, k, set(data[k]))
            elif isinstance(cur, deque):
                setattr(self, k, deque(data[k], maxlen=cur.maxlen))
            else:
                setattr(self, k, data[k])
        self.profile.update(self.settings)

    def note(self, text: str, kind: str = "info") -> None:
        self.activity.appendleft({"ts": self.world.clock.now(), "text": text, "kind": kind})

    async def send(self, type_: str, to: str, payload: dict, hop_count: int = 0,
                   idempotency_key: str | None = None) -> tuple[bool, str]:
        msg = self.world.bus.new_message(type_, self.id, to, payload, hop_count, idempotency_key)
        msg.signature = sign(self.key, msg)
        return await self.world.bus.send(msg)

    async def handle(self, msg: Message) -> None:
        from features import dead_link_detective as dld
        from features import freebie_squad as squad
        from features import opportunity_relay as relay
        handlers = {
            "OPPORTUNITY_RELAY": relay.on_relay,
            "VERIFY_REQUEST": dld.on_verify_request,
            "VERIFY_RESULT": dld.on_verify_result,
            "AVAILABILITY_QUERY": squad.on_query,
            "AVAILABILITY_REPLY": squad.on_reply,
            "PLAN_INVITE": squad.on_invite,
            "PLAN_REPLY": squad.on_plan_reply,
            "PLAN_UPDATE": squad.on_update,
        }
        await handlers[msg.type](self, msg)

    def view(self) -> dict:
        c = self.world.connections
        return {"email": self.id, "name": self.name, "major": self.profile["major"], "year": self.profile["year"],
                "requests": c.incoming(self.id), "outgoing": c.outgoing(self.id), "friends": c.friends(self.id),
                "feed": self.feed[::-1], "activity": list(self.activity),
                "plans": [*self.plans.values(), *self.invites.values()]}
