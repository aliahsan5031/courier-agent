"""Model-call accounting: tokens, latency, failures, local vs remote."""
from __future__ import annotations

import sqlite3
import threading
import time
from pathlib import Path


class Usage:
    def __init__(self, home: Path):
        Path(home).mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(str(Path(home) / "usage.db"), check_same_thread=False)
        self._lock = threading.Lock()
        with self._lock:
            self.db.execute("create table if not exists calls(ts real, provider text, model text, local int, "
                            "prompt_tokens int, completion_tokens int, latency real, ok int, status text)")

    def record(self, e: dict):
        u = e.get("usage") or {}
        with self._lock:
            self.db.execute("insert into calls values(?,?,?,?,?,?,?,?,?)",
                            (time.time(), e["provider"], e["model"], int(e["local"]), u.get("prompt_tokens", 0),
                             u.get("completion_tokens", 0), e["latency"], int(e["ok"]), str(e.get("status", ""))))
            self.db.commit()

    def summary(self, days=1):
        since = time.time() - days * 86400
        with self._lock:
            rows = self.db.execute(
                "select provider, local, count(*), sum(ok), sum(prompt_tokens), sum(completion_tokens), avg(latency), "
                "sum(status='429') from calls where ts>=? group by provider, local", (since,)).fetchall()
        return [{"provider": r[0], "where": "local" if r[1] else "remote", "calls": r[2], "ok": r[3],
                 "prompt_tokens": r[4], "completion_tokens": r[5], "avg_latency_s": round(r[6], 2),
                 "rate_limited": r[7]} for r in rows]
