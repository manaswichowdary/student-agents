""".edu verification against a mock university directory.

Prototype stand-in: in production this is ASU SSO (SAML/OAuth) and real email delivery.
The verified name always comes from the directory, never from what the user types.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import secrets
from pathlib import Path

from .store import Store
from .util import Clock

CODE_TTL = 10 * 60
MAX_ATTEMPTS = 5
EDU = re.compile(r"^[a-z0-9._-]{2,40}@([a-z0-9-]+\.)*[a-z0-9-]+\.edu$")
YEARS = {"freshman", "sophomore", "junior", "senior", "graduate"}


class IdentityError(Exception):
    pass


def normalize(email: str) -> str:
    email = (email or "").strip().lower()
    if "@" not in email:
        email += "@asu.edu"          # let students type just their ASURITE id
    return email


class Identity:
    def __init__(self, store: Store, clock: Clock, directory: dict[str, dict], open_signup: bool = False):
        self.store, self.clock, self.directory = store, clock, directory
        # open_signup: students outside the mock directory can join with a self reported profile.
        # ALLOWED_DOMAINS can limit which schools, e.g. "asu.edu"; the default "*" means any .edu address.
        self.open_signup = open_signup
        self.domains = {d.strip().lower() for d in os.environ.get("ALLOWED_DOMAINS", "*").split(",") if d.strip()}
        self.outbox: list[dict] = []      # mock email: the UI and demo read codes from here
        self.screen_shown: set[str] = set()   # emails whose code was shown on screen, not sent to their inbox

    @classmethod
    def from_file(cls, store: Store, clock: Clock, path: Path, open_signup: bool = False) -> "Identity":
        return cls(store, clock, json.loads(path.read_text(encoding="utf-8")), open_signup)

    def in_directory(self, email: str) -> bool:
        return normalize(email) in self.directory

    @staticmethod
    def _hash(email: str, code: str) -> str:
        return hashlib.sha256(f"{email}:{code}".encode()).hexdigest()

    def mark_shown_on_screen(self, email: str) -> None:
        """The server showed this code on screen (no email key). Whoever signs in is not proven to own the inbox."""
        self.screen_shown.add(normalize(email))

    def start(self, email: str) -> str:
        """Begin sign up. Only .edu addresses in the directory can ever verify."""
        from data.seed import DEMO_ACCOUNTS
        email = normalize(email)
        if not EDU.match(email):
            raise IdentityError("Use your school .edu email, like asurite@asu.edu")
        if email not in self.directory:
            if not self.open_signup:
                raise IdentityError("That ID isn't in the university directory")
            if "*" not in self.domains and email.split("@", 1)[1] not in self.domains:
                raise IdentityError(f"Use your {' or '.join(sorted(self.domains))} email")
        code = DEMO_ACCOUNTS.get(email) or f"{secrets.randbelow(1_000_000):06d}"
        self.store.x("INSERT OR REPLACE INTO codes VALUES (?,?,?,0)",
                     (email, self._hash(email, code), self.clock.now() + CODE_TTL))
        self.outbox.append({"to": email, "code": code, "ts": self.clock.now()})
        return email

    def verify(self, email: str, code: str, name: str = "", major: str = "", year: str = "") -> dict:
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
        if email in self.directory:
            person, self_reported = self.directory[email], 0
        else:
            person, self_reported = self._self_reported(name, major, year), 1
        proof = "screen" if email in self.screen_shown else "email"
        self.screen_shown.discard(email)
        self.store.x("INSERT OR REPLACE INTO accounts (email,name,major,year,verified_at,self_reported,proof) "
                     "VALUES (?,?,?,?,?,?,?)",
                     (email, person["name"], person["major"], person["year"], self.clock.now(), self_reported, proof))
        self.store.x("DELETE FROM codes WHERE email=?", (email,))
        return self.account(email)

    def check_code(self, email: str, code: str) -> None:
        """Validate a code without using it (so a new student can then fill in their profile)."""
        row = self.store.one("SELECT * FROM codes WHERE email=?", (normalize(email),))
        if not row or row["attempts"] >= MAX_ATTEMPTS or self.clock.now() > row["expires"]:
            raise IdentityError("That code expired. Ask for a new one")
        if not hmac.compare_digest(row["code_hash"], self._hash(normalize(email), (code or "").strip())):
            self.store.x("UPDATE codes SET attempts = attempts + 1 WHERE email=?", (normalize(email),))
            raise IdentityError("That code doesn't match")

    @staticmethod
    def _self_reported(name: str, major: str, year: str) -> dict:
        clean = lambda v: re.sub(r"\s+", " ", re.sub(r"[^\w .'&-]", "", v or "")).strip()[:60]  # noqa: E731
        name, major, year = clean(name), clean(major), clean(year).lower()
        if len(name.split()) < 2:
            raise IdentityError("Add your first and last name")
        if len(major) < 2:
            raise IdentityError("Add your major")
        if year not in YEARS:
            raise IdentityError("Pick your class year")
        return {"name": name.title() if name.islower() else name, "major": major, "year": year}

    def account(self, email: str) -> dict | None:
        row = self.store.one("SELECT * FROM accounts WHERE email=?", (normalize(email),))
        return dict(row) if row else None

    def is_verified(self, email: str) -> bool:
        return self.account(email) is not None

    def profile(self, email: str) -> dict:
        """Directory profile (or the self reported one), used only inside the student's own agent."""
        email = normalize(email)
        if email in self.directory:
            return self.directory[email]
        acc = self.account(email)
        return {"name": acc["name"], "major": acc["major"], "year": acc["year"]}
