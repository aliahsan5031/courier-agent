"""Optional response cache for tool-free model calls (off by default: stale answers are a real risk)."""
from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
import time
from pathlib import Path


class ResponseCache:
    def __init__(self, home: Path, ttl_hours=24):
        Path(home).mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(str(Path(home) / "cache.db"), check_same_thread=False)
        self._lock = threading.Lock()
        self.ttl = ttl_hours * 3600
        self.hits = self.misses = 0
        with self._lock:
            self.db.execute("create table if not exists cache(key text primary key, ts real, content text, model text)")

    @staticmethod
    def key(messages, task):
        return hashlib.sha256(json.dumps([messages, task], sort_keys=True, default=str).encode()).hexdigest()

    def get(self, key):
        with self._lock:
            row = self.db.execute("select ts, content, model from cache where key=?", (key,)).fetchone()
        if row and time.time() - row[0] <= self.ttl:
            self.hits += 1
            return row[1], row[2]
        self.misses += 1
        return None

    def put(self, key, content, model):
        with self._lock:
            self.db.execute("insert or replace into cache values(?,?,?,?)", (key, time.time(), content, model))
            self.db.commit()

    def clear(self):
        with self._lock:
            n = self.db.execute("select count(*) from cache").fetchone()[0]
            self.db.execute("delete from cache")
            self.db.commit()
        return n
