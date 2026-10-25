"""Wires everything together: storage, clock, identity, connections, bus, trust and the agents."""
from __future__ import annotations

from pathlib import Path
from typing import Callable

from data import seed

from .agent import StudentAgent
from .bus import Bus
from .connections import Connections
from .identity import Identity
from .store import Store
from .util import Clock

DATA = Path(__file__).resolve().parent.parent / "data"


class World:
    def __init__(self, db: str = ":memory:", start: float = seed.DEMO_START, verify_background: bool = True):
        from features.dead_link_detective import TrustBook
        self.store = Store(db)
        self.clock = Clock(start)
        self.identity = Identity.from_file(self.store, self.clock, DATA / "directory.json")
        self.connections = Connections(self.store, self.clock, self.identity)
        self.bus = Bus(self.store, self.clock, self.identity, self.connections)
        self.trust = TrustBook(self.store, self.clock)
        self.private_profiles = seed.PRIVATE_PROFILES
        self.places, self.pickup_spots, self.web = seed.PLACES, seed.PICKUP_SPOTS, dict(seed.WEB)
        self.shared_fetch, self.fetch_cache = False, {}
        self.agents: dict[str, StudentAgent] = {}
        self.log: list[dict] = []                 # messages and agent events, in order, for the live log
        self.listeners: list[Callable[[dict], None]] = []
        self.bus.listeners.append(lambda row: self._emit({"kind": "message", **row}))
        self.connections.on_disconnect.append(self._on_disconnect)
        self._notices: list[tuple[str, str, dict]] = []
        for source, ts, ok in seed.trust_history(start):
            self.trust.record(source, ok, ts)
        if verify_background:
            for email in seed.BACKGROUND:
                self.sign_up(email)

    # ---------- events shown in the live log (agent reasoning, not messages) ----------
    def event(self, kind: str, text: str, **extra) -> None:
        self._emit({"kind": "event", "event": kind, "text": text, "ts": self.clock.now(), **extra})

    def _emit(self, row: dict) -> None:
        row["n"] = len(self.log) + 1
        self.log.append(row)
        for listen in self.listeners:
            listen(row)

    # ---------- identity ----------
    def start_signup(self, email: str) -> str:
        return self.identity.start(email)

    def verify(self, email: str, code: str) -> dict:
        acc = self.identity.verify(email, code)
        if acc["email"] not in self.agents:
            self.agents[acc["email"]] = StudentAgent(self, acc["email"])
        self.event("identity", f"{acc['name']} verified ({acc['email']}). Name comes from the directory.")
        return acc

    def sign_up(self, email: str) -> dict:
        """Shortcut used for background students and tests: start and finish with the mock code."""
        email = self.start_signup(email)
        return self.verify(email, self.identity.outbox[-1]["code"])

    def agent(self, email: str) -> StudentAgent:
        from .identity import normalize
        return self.agents[normalize(email)]

    # ---------- disconnects: cancel plans and drop queued relays right away ----------
    def _on_disconnect(self, user: str, other: str) -> None:
        from features import freebie_squad, opportunity_relay
        for me, them in ((user, other), (other, user)):
            a = self.agents.get(me)
            if not a:
                continue
            opportunity_relay.on_disconnect(a, them)
            for member, payload in freebie_squad.on_disconnect(a, them):
                self._notices.append((me, member, payload))

    async def end_connection(self, user: str, other: str, block: bool = False) -> None:
        (self.connections.block if block else self.connections.remove)(user, other)
        self.event("connection", f"{self.agent(user).first} {'blocked' if block else 'removed'} "
                                 f"{self.identity.account(other)['name'].split()[0]}. Plans with them are cancelled.")
        notices, self._notices = self._notices, []
        for sender, member, payload in notices:
            if self.connections.is_active(sender, member):
                await self.agents[sender].send("PLAN_UPDATE", member, payload)

    async def digest(self) -> int:
        from features import opportunity_relay
        return sum([await opportunity_relay.flush_digest(a) for a in self.agents.values()])
