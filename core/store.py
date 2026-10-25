"""SQLite storage for accounts, codes, connections, permissions, audit log and source trust."""
from __future__ import annotations

import json
import sqlite3
import threading

SCHEMA = """
CREATE TABLE IF NOT EXISTS accounts (email TEXT PRIMARY KEY, name TEXT, major TEXT, year TEXT,
  verified_at REAL);
CREATE TABLE IF NOT EXISTS codes (email TEXT PRIMARY KEY, code_hash TEXT, expires REAL, attempts INTEGER);
CREATE TABLE IF NOT EXISTS connections (a TEXT, b TEXT, state TEXT, requested_by TEXT, created REAL,
  updated REAL, blocked_by TEXT, PRIMARY KEY (a, b));
CREATE TABLE IF NOT EXISTS permissions (owner TEXT, other TEXT, perm TEXT, value INTEGER,
  PRIMARY KEY (owner, other, perm));
CREATE TABLE IF NOT EXISTS request_log (sender TEXT, ts REAL);
CREATE TABLE IF NOT EXISTS seen_messages (id TEXT PRIMARY KEY, ts REAL);
CREATE TABLE IF NOT EXISTS idempotency (key TEXT PRIMARY KEY, ts REAL);
CREATE TABLE IF NOT EXISTS audit (seq INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL, msg_id TEXT, type TEXT,
  sender TEXT, recipient TEXT, result TEXT, reason TEXT, fields TEXT);
CREATE TABLE IF NOT EXISTS trust_checks (source TEXT, ts REAL, confirmed INTEGER);
CREATE TABLE IF NOT EXISTS reputation (verifier TEXT PRIMARY KEY, weight REAL);
"""


class Store:
    def __init__(self, path: str = ":memory:"):
        self.db = sqlite3.connect(path, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.lock = threading.RLock()
        self.db.executescript(SCHEMA)

    def x(self, sql: str, args: tuple = ()) -> sqlite3.Cursor:
        with self.lock:
            cur = self.db.execute(sql, args)
            self.db.commit()
            return cur

    def one(self, sql: str, args: tuple = ()):
        return self.x(sql, args).fetchone()

    def all(self, sql: str, args: tuple = ()) -> list:
        return self.x(sql, args).fetchall()

    def audit(self, ts, msg_id, type_, sender, recipient, result, reason, fields) -> int:
        cur = self.x("INSERT INTO audit (ts,msg_id,type,sender,recipient,result,reason,fields) VALUES (?,?,?,?,?,?,?,?)",
                     (ts, msg_id, type_, sender, recipient, result, reason, json.dumps(fields, ensure_ascii=False)))
        return cur.lastrowid

    def audit_rows(self, since: int = 0) -> list[dict]:
        rows = self.all("SELECT * FROM audit WHERE seq > ? ORDER BY seq", (since,))
        return [{**dict(r), "fields": json.loads(r["fields"])} for r in rows]
