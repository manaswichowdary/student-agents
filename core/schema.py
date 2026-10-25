"""Strict schemas for listings and messages.

Listings are untrusted text from the web, email and chats. They are parsed into fixed fields, cleaned and size
limited. No raw listing text ever becomes part of an agent's instructions.
"""
from __future__ import annotations

import hashlib
import re
from dataclasses import asdict, dataclass, field
from datetime import datetime

from .util import DAY, parse_local

LISTING_TYPES = {"study", "scholarship", "food", "opportunity", "event"}
MAX_TEXT = 160
TAG = re.compile(r"^[a-z0-9 _-]{1,30}$")
CONTROL = re.compile(r"[\x00-\x1f\x7f​-‏‪-‮]")


class SchemaError(ValueError):
    pass


def clean_text(value, name: str, required: bool = True, limit: int = MAX_TEXT) -> str:
    if value is None or value == "":
        if required:
            raise SchemaError(f"'{name}' is required")
        return ""
    if not isinstance(value, str):
        raise SchemaError(f"'{name}' must be text")
    value = CONTROL.sub(" ", value)
    value = re.sub(r"\s+", " ", value).strip()
    return value[:limit]


@dataclass(frozen=True)
class Listing:
    id: str
    title: str
    org: str
    type: str
    deadline: str | None          # "2026-10-31" or "2026-10-31T17:00" in Phoenix time
    url: str
    source: str
    eligibility_tags: tuple[str, ...] = field(default_factory=tuple)
    pay_amount: float | None = None
    location: str = ""

    def to_dict(self) -> dict:
        d = asdict(self)
        d["eligibility_tags"] = list(self.eligibility_tags)
        return d

    def deadline_ts(self) -> float | None:
        if not self.deadline:
            return None
        ts = parse_local(self.deadline)
        return ts + DAY - 1 if "T" not in self.deadline else ts   # a date means the end of that day

    def expires(self, found_at: float) -> float:
        """TTL: free food lasts until the event starts (2 hours if no time is given); everything else until its
        deadline (or 14 days if none is given)."""
        if self.type == "food":
            return self.deadline_ts() or found_at + 2 * 3600
        return self.deadline_ts() or found_at + 14 * DAY


LISTING_KEYS = {"id", "title", "org", "type", "deadline", "url", "source", "eligibility_tags", "pay_amount",
                "location"}


def parse_listing(raw: dict) -> Listing:
    if not isinstance(raw, dict):
        raise SchemaError("a listing must be an object")
    extra = set(raw) - LISTING_KEYS - {"raw_text"}   # raw_text is allowed in, never used past this point
    if extra:
        raise SchemaError(f"unknown listing fields: {sorted(extra)}")
    type_ = clean_text(raw.get("type"), "type")
    if type_ not in LISTING_TYPES:
        raise SchemaError(f"type must be one of {sorted(LISTING_TYPES)}")
    url = clean_text(raw.get("url"), "url", limit=300)
    if not re.match(r"^https://[a-z0-9.-]+\.[a-z]{2,}(/\S*)?$", url, re.I):
        raise SchemaError("url must be an https link")
    deadline = raw.get("deadline")
    if deadline:
        try:
            datetime.fromisoformat(deadline)
        except (TypeError, ValueError):
            raise SchemaError("deadline must look like 2026-10-31 or 2026-10-31T17:00") from None
    tags = raw.get("eligibility_tags") or []
    if not isinstance(tags, list) or len(tags) > 10:
        raise SchemaError("eligibility_tags must be a list of up to 10 tags")
    tags = tuple(t.strip().lower() for t in tags if isinstance(t, str) and TAG.match(t.strip().lower()))
    pay = raw.get("pay_amount")
    if pay is not None and (not isinstance(pay, (int, float)) or isinstance(pay, bool) or not 0 <= pay <= 100_000):
        raise SchemaError("pay_amount must be a number or null")
    title = clean_text(raw.get("title"), "title")
    lid = raw.get("id") or "lst_" + hashlib.sha256(f"{url}|{title}".encode()).hexdigest()[:10]
    if not re.match(r"^[A-Za-z0-9_-]{3,40}$", str(lid)):
        raise SchemaError("bad listing id")
    return Listing(id=str(lid), title=title, org=clean_text(raw.get("org"), "org"), type=type_,
                   deadline=deadline or None, url=url, source=clean_text(raw.get("source"), "source", limit=60),
                   eligibility_tags=tags, pay_amount=float(pay) if pay is not None else None,
                   location=clean_text(raw.get("location"), "location", required=False))


# ---------------------------------------------------------------------------------------------- scam filter
SCAM = [
    (re.compile(r"\b(ssn|social security( number)?)\b", re.I), "asks for a Social Security number"),
    (re.compile(r"\b(bank (account|details|info)|routing number|account number)\b", re.I), "asks for bank info"),
    (re.compile(r"\b(buy|purchase|send|pay (with|in))\b[^.]{0,30}\bgift ?cards?\b", re.I), "asks you to buy or send gift cards"),
    (re.compile(r"\b(processing|application|registration|activation) fee\b|\bpay (a|the|an) [a-z ]*fee\b", re.I), "asks for a payment"),
    (re.compile(r"\b(venmo|zelle|cash ?app|wire) (me|us|the)\b", re.I), "asks for a payment"),
    (re.compile(r"\b(only|just) (contact|reach|text|dm)\b.*\b(telegram|whatsapp|signal|gmail)\b|\b(telegram|whatsapp) only\b", re.I),
     "off platform contact only"),
]


def scam_flags(listing: Listing, raw_text: str = "") -> list[str]:
    text = " ".join([listing.title, listing.org, listing.location, raw_text or ""])
    return sorted({why for rx, why in SCAM if rx.search(text)})


# ---------------------------------------------------------------------------------------------- messages
def _one_of(*opts):
    return lambda v: v in opts


def _text(limit=MAX_TEXT):
    return lambda v: isinstance(v, str) and 0 < len(v) <= limit


def _iso(v):
    try:
        datetime.fromisoformat(v)
        return True
    except (TypeError, ValueError):
        return False


def _listing(v):
    try:
        parse_listing(v)
        return True
    except SchemaError:
        return False


def _evidence(v):
    return (isinstance(v, dict) and set(v) == {"http", "deadline", "reason", "check_id"}
            and all(isinstance(x, str) and len(x) <= 200 for x in v.values()))


PAYLOADS: dict[str, dict] = {
    "OPPORTUNITY_RELAY": {"listing": _listing, "closes": _text(40), "verified": lambda v: isinstance(v, bool)},
    "VERIFY_REQUEST": {"listing": _listing, "request_id": _text(40)},
    "VERIFY_RESULT": {"request_id": _text(40), "listing_id": _text(40), "status": _one_of("open", "closed", "unknown"),
                      "evidence": _evidence},
    "AVAILABILITY_QUERY": {"query_id": _text(40), "event_title": _text(), "place": _text(80), "window_start": _iso,
                           "window_end": _iso},
    "AVAILABILITY_REPLY": {"query_id": _text(40), "status": _one_of("free", "busy"),
                           "distance": _one_of("near", "medium", "far", None)},
    "PLAN_INVITE": {"plan_id": _text(40), "event_title": _text(), "starts": _iso, "transport": _one_of("carpool", "walk"),
                    "role": _one_of("driver", "rider", "walker"), "later": lambda v: isinstance(v, bool)},
    "PLAN_REPLY": {"plan_id": _text(40), "answer": _one_of("accept", "decline")},
    "PLAN_UPDATE": {"plan_id": _text(40), "state": _one_of("confirmed", "rejected", "cancelled", "expired"),
                    "note": _text(200), "meet": lambda v: v is None or _text(120)(v)},
}


def validate_payload(type_: str, payload: dict) -> None:
    spec = PAYLOADS.get(type_)
    if spec is None:
        raise SchemaError(f"unknown message type {type_}")
    if not isinstance(payload, dict) or set(payload) != set(spec):
        raise SchemaError(f"{type_} needs exactly {sorted(spec)}")
    for key, check in spec.items():
        if not check(payload[key]):
            raise SchemaError(f"{type_}.{key} is not valid")


@dataclass
class Message:
    id: str
    type: str
    sender: str
    recipient: str
    payload: dict
    hop_count: int
    ts: float
    idempotency_key: str
    signature: str = ""

    def signing_dict(self) -> dict:
        d = asdict(self)
        d.pop("signature")
        return d
