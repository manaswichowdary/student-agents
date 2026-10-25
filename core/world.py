"""Wires everything together: storage, clock, identity, connections, bus, trust and the agents."""
from __future__ import annotations

import json
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
    def __init__(self, db: str = ":memory:", start: float | None = seed.DEMO_START, verify_background: bool = True,
                 live: bool = False):
        """live=True: the public website. Real clock, open sign up, state saved to the database file,
        demo friends seeded once. live=False: the in memory demo and tests."""
        from features.dead_link_detective import TrustBook
        self.live = live
        self.store = Store(db)
        self.clock = Clock(None if live else start)
        self.identity = Identity.from_file(self.store, self.clock, DATA / "directory.json", open_signup=live)
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
        if live:
            self._boot_live()
            return
        for source, ts, ok in seed.trust_history(start):
            self.trust.record(source, ok, ts)
        if verify_background:
            for email in seed.BACKGROUND:
                self.sign_up(email)

    # ---------- the live website ----------
    def _boot_live(self) -> None:
        from features import demo_friends
        seeded = self.store.one("SELECT value FROM meta WHERE key='seeded'")
        for row in self.store.all("SELECT email FROM accounts"):
            self._make_agent(row["email"])
        if not seeded:
            for source, ts, ok in seed.trust_history(self.clock.now()):
                self.trust.record(source, ok, ts)
            demo_friends.seed(self)
            self.store.x("INSERT OR REPLACE INTO meta VALUES ('seeded', '1')")
            self.save()

    def _make_agent(self, email: str) -> StudentAgent:
        agent = self.agents.get(email) or StudentAgent(self, email)
        self.agents[email] = agent
        row = self.store.one("SELECT state FROM agent_state WHERE email=?", (email,))
        if row:
            agent.load_state(json.loads(row["state"]))
        return agent

    def save(self) -> None:
        for email, agent in self.agents.items():
            self.store.x("INSERT OR REPLACE INTO agent_state VALUES (?,?)",
                         (email, json.dumps(agent.to_state(), ensure_ascii=False)))

    # ---------- events shown in the live log (agent reasoning, not messages) ----------
    def event(self, kind: str, text: str, **extra) -> None:
        self._emit({"kind": "event", "event": kind, "text": text, "ts": self.clock.now(), **extra})

    def _emit(self, row: dict) -> None:
        row["n"] = (self.log[-1]["n"] + 1) if self.log else 1
        self.log.append(row)
        if self.live and len(self.log) > 1000:
            del self.log[:500]
        for listen in self.listeners:
            listen(row)

    # ---------- identity ----------
    def start_signup(self, email: str) -> str:
        return self.identity.start(email)

    def verify(self, email: str, code: str, name: str = "", major: str = "", year: str = "") -> dict:
        acc = self.identity.verify(email, code, name, major, year)
        if acc["email"] not in self.agents:
            self._make_agent(acc["email"])
        how = "self reported profile" if acc.get("self_reported") else "name comes from the directory"
        self.event("identity", f"{acc['name']} verified ({acc['email']}), {how}.")
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
