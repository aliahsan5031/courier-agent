"""Audit log, secret redaction and the emergency stop."""
from __future__ import annotations

import json
import re
import threading
import time
from pathlib import Path

_REDACT = re.compile(
    r"(nvapi-[\w\-]+|sk-or-[\w\-]+|sk-[A-Za-z0-9]{20,}|Bearer\s+[\w.\-]+|"
    r"(?i:(?:api[_-]?key|token|secret|password)[\"'=:\s]+)[\w\-]{12,})")


def redact(s: str) -> str:
    return _REDACT.sub("[REDACTED]", s)


class Stopped(Exception):
    pass


class AuditLog:
    """Append-only JSON lines: every tool call, network fetch and model call."""

    def __init__(self, home: Path):
        self.path = Path(home) / "audit.jsonl"
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()

    def log(self, session, kind, name, args=None, result="", status="ok", **extra):
        rec = {"ts": round(time.time(), 2), "session": session, "kind": kind, "name": name,
               "status": status, "args": redact(json.dumps(args, default=str))[:500] if args is not None else "",
               "result": redact(str(result))[:300], **extra}
        with self._lock, self.path.open("a") as f:
            f.write(json.dumps(rec) + "\n")

    def tail(self, n=20, kind=None) -> list[dict]:
        if not self.path.exists():
            return []
        rows = [json.loads(l) for l in self.path.read_text().splitlines() if l.strip()]
        if kind:
            rows = [r for r in rows if r["kind"] == kind]
        return rows[-n:]


class StopFlag:
    """Emergency stop. Set in-process (Ctrl-C, UI button) or from another terminal
    with `courier stop`, which touches a STOP file newer than the current run."""

    def __init__(self, home: Path):
        self.file = Path(home) / "STOP"
        self.ev = threading.Event()
        self.since = time.time()

    def begin_run(self):
        self.ev.clear()
        self.since = time.time()

    def set(self):
        self.ev.set()

    def touch_file(self):
        self.file.write_text(str(time.time()))

    def is_set(self) -> bool:
        if self.ev.is_set():
            return True
        try:
            return self.file.exists() and self.file.stat().st_mtime >= self.since
        except OSError:
            return False
