""".edu verification against a mock university directory.

Prototype stand-in: in production this is ASU SSO (SAML/OAuth) and real email delivery.
The verified name always comes from the directory, never from what the user types.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import re
import secrets
from pathlib import Path

from .store import Store
from .util import Clock

CODE_TTL = 10 * 60
MAX_ATTEMPTS = 5
EDU = re.compile(r"^[a-z0-9._-]{2,40}@([a-z0-9-]+\.)*[a-z0-9-]+\.edu$")


class IdentityError(Exception):
    pass


def normalize(email: str) -> str:
    email = (email or "").strip().lower()
    if "@" not in email:
        email += "@asu.edu"          # let students type just their ASURITE id
    return email


class Identity:
    def __init__(self, store: Store, clock: Clock, directory: dict[str, dict]):
        self.store, self.clock, self.directory = store, clock, directory
        self.outbox: list[dict] = []      # mock email: the UI and demo read codes from here

    @classmethod
    def from_file(cls, store: Store, clock: Clock, path: Path) -> "Identity":
        return cls(store, clock, json.loads(path.read_text(encoding="utf-8")))

    @staticmethod
    def _hash(email: str, code: str) -> str:
        return hashlib.sha256(f"{email}:{code}".encode()).hexdigest()

    def start(self, email: str) -> str:
        """Begin sign up. Only .edu addresses in the directory can ever verify."""
        email = normalize(email)
        if not EDU.match(email):
            raise IdentityError("Use your school .edu email, like asurite@asu.edu")
        if email not in self.directory:
            raise IdentityError("That ID isn't in the university directory")
        code = f"{secrets.randbelow(1_000_000):06d}"
        self.store.x("INSERT OR REPLACE INTO codes VALUES (?,?,?,0)",
                     (email, self._hash(email, code), self.clock.now() + CODE_TTL))
        self.outbox.append({"to": email, "code": code, "ts": self.clock.now()})
        return email

    def verify(self, email: str, code: str) -> dict:
        email = normalize(email)
        row = self.store.one("SELECT * FROM codes WHERE email=?", (email,))
        if not row:
            raise IdentityError("Ask for a code first")
        if row["attempts"] >= MAX_ATTEMPTS:
            raise IdentityError("Too many tries. Ask for a new code")
        if self.clock.now() > row["expires"]:
            raise IdentityError("That code expired. Ask for a new one")
        self.store.x("UPDATE codes SET attempts = attempts + 1 WHERE email=?", (email,))
        if not hmac.compare_digest(row["code_hash"], self._hash(email, (code or "").strip())):
            raise IdentityError("That code doesn't match")
        person = self.directory[email]
        self.store.x("INSERT OR REPLACE INTO accounts VALUES (?,?,?,?,?)",
                     (email, person["name"], person["major"], person["year"], self.clock.now()))
        self.store.x("DELETE FROM codes WHERE email=?", (email,))
        return self.account(email)

    def account(self, email: str) -> dict | None:
        row = self.store.one("SELECT * FROM accounts WHERE email=?", (normalize(email),))
        return dict(row) if row else None

    def is_verified(self, email: str) -> bool:
        return self.account(email) is not None

    def profile(self, email: str) -> dict:
        """Directory profile, used only inside the student's own agent."""
        return self.directory[normalize(email)]
