"""Cross-session memory.

Layer 1  curated, bounded MEMORY.md + USER.md (frozen snapshot per session)
Layer 2  procedural memory lives in skills.py
Layer 3  SQLite + FTS5 session store for full-text recall of past chats
"""
from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import threading
import time
from pathlib import Path

SEP = "\n§\n"
LIMITS = {"memory": 2200, "user": 1375}
FILES = {"memory": "MEMORY.md", "user": "USER.md"}

_INJECTION = re.compile(
    r"(ignore (all |any )?(previous|prior|above) (instructions|prompts)|"
    r"disregard (the )?(system|previous)|you are now|reveal (your )?system prompt|"
    r"curl[^\n]*\|\s*(ba)?sh|rm\s+-rf\s+/|<\s*/?system\s*>)", re.I)


def scan(text: str) -> str | None:
    """Return a reason string if the text looks like prompt injection."""
    m = _INJECTION.search(text)
    return f"blocked: looks like prompt injection ({m.group(0)[:30]!r})" if m else None


class CuratedMemory:
    """Bounded, user-controllable notes. Per-entry metadata lives in meta.json (source, confidence,
    age, optional expiry). Pausing stops both writes and recall."""

    def __init__(self, home: Path, limits: dict | None = None):
        self.dir = Path(home) / "memories"
        self.dir.mkdir(parents=True, exist_ok=True)
        self.limits = {**LIMITS, **(limits or {})}
        self._lock = threading.Lock()
        self.meta_path = self.dir / "meta.json"
        self.pause_path = self.dir / ".paused"
        self._purge_expired()
        self._snapshot = {t: ([] if self.paused() else self._read(t)) for t in FILES}  # frozen for this session

    # -- control
    def paused(self) -> bool:
        return self.pause_path.exists()

    def pause(self):
        self.pause_path.write_text("paused")

    def resume(self):
        self.pause_path.unlink(missing_ok=True)

    # -- storage
    def _path(self, target):
        if target not in FILES:
            raise ValueError("target must be 'memory' or 'user'")
        return self.dir / FILES[target]

    def _read(self, target) -> list[str]:
        p = self._path(target)
        if not p.exists():
            return []
        return [e.strip() for e in p.read_text().split(SEP) if e.strip()]

    def _write(self, target, entries):
        self._path(target).write_text(SEP.join(entries))

    def _meta(self) -> dict:
        try:
            return json.loads(self.meta_path.read_text())
        except (OSError, ValueError):
            return {}

    def _save_meta(self, m):
        self.meta_path.write_text(json.dumps(m, indent=1))

    @staticmethod
    def _key(text):
        return hashlib.sha1(text.strip().lower().encode()).hexdigest()[:12]

    def _purge_expired(self):
        meta, now, changed = self._meta(), time.time(), False
        for t in FILES:
            entries = self._read(t)
            keep = [e for e in entries if not (meta.get(self._key(e), {}).get("expires") or 1e18) < now]
            if len(keep) != len(entries):
                self._write(t, keep)
                changed = True
        if changed:
            self._save_meta({k: v for k, v in meta.items() if (v.get("expires") or 1e18) >= now})

    def used(self, target) -> int:
        return len(SEP.join(self._read(target)))

    def snapshot(self) -> str:
        parts = []
        for t, title in (("memory", "MEMORY (agent notes)"), ("user", "USER PROFILE")):
            entries = self._snapshot[t]
            if entries:
                parts.append(f"## {title} [{len(SEP.join(entries))}/{self.limits[t]} chars]\n"
                             + "\n".join(f"- {e}" for e in entries))
        return "\n\n".join(parts)

    # -- operations (tool backend)
    def add(self, target, text, source="chat", confidence="medium", expires_days=None) -> str:
        if self.paused():
            return "error: memory is paused by the user"
        text = text.strip()
        if not text:
            return "error: empty entry"
        bad = scan(text)
        if bad:
            return "error: " + bad
        with self._lock:
            entries = self._read(target)
            if any(text.lower() == e.lower() for e in entries):
                return "error: duplicate entry"
            new_total = len(SEP.join(entries + [text]))
            limit = self.limits[target]
            if new_total > limit:
                return (f"error: would exceed {limit} chars ({new_total}). Merge or remove "
                        f"entries first. Current entries:\n" + "\n".join(f"- {e}" for e in entries))
            words = {w for w in re.findall(r"\w+", text.lower()) if len(w) > 3}
            clash = [e for e in entries if words and len(words & {w for w in re.findall(r"\w+", e.lower()) if len(w) > 3})
                     / max(1, len(words | {w for w in re.findall(r"\w+", e.lower()) if len(w) > 3})) >= 0.5]
            entries.append(text)
            self._write(target, entries)
            meta = self._meta()
            meta[self._key(text)] = {"added": time.time(), "source": source, "confidence": confidence,
                                     "expires": time.time() + expires_days * 86400 if expires_days else None}
            self._save_meta(meta)
        pct = int(100 * new_total / limit)
        warn = " Over 80% full: merge related entries before adding more." if pct > 80 else ""
        if clash:
            warn += f" Possible overlap or contradiction with: {clash[0][:80]!r}; consider replace."
        return f"saved to {target} ({pct}% full). Visible from the next session.{warn}"

    def replace(self, target, old, new) -> str:
        if self.paused():
            return "error: memory is paused by the user"
        bad = scan(new)
        if bad:
            return "error: " + bad
        with self._lock:
            entries = self._read(target)
            hits = [i for i, e in enumerate(entries) if old.lower() in e.lower()]
            if len(hits) != 1:
                return f"error: {len(hits)} entries match {old!r}; need exactly 1"
            meta = self._meta()
            old_meta = meta.pop(self._key(entries[hits[0]]), {"added": time.time(), "source": "chat", "confidence": "medium"})
            entries[hits[0]] = new.strip()
            if len(SEP.join(entries)) > self.limits[target]:
                return "error: replacement would exceed the limit"
            self._write(target, entries)
            meta[self._key(new)] = {**old_meta, "edited": time.time()}
            self._save_meta(meta)
        return "replaced"

    def remove(self, target, old) -> str:
        if self.paused():
            return "error: memory is paused by the user"
        with self._lock:
            entries = self._read(target)
            keep = [e for e in entries if old.lower() not in e.lower()]
            if len(entries) - len(keep) != 1:
                return f"error: {len(entries) - len(keep)} entries match {old!r}; need exactly 1"
            gone = [e for e in entries if e not in keep][0]
            self._write(target, keep)
            meta = self._meta(); meta.pop(self._key(gone), None); self._save_meta(meta)
        return "removed"

    def show(self, target) -> str:
        meta, lines = self._meta(), []
        for e in self._read(target):
            m = meta.get(self._key(e), {})
            age = f" (added {time.strftime('%Y-%m-%d', time.localtime(m['added']))}, {m.get('source', '?')}, " \
                  f"confidence {m.get('confidence', '?')})" if m.get("added") else ""
            lines.append(f"- {e}{age}")
        return "\n".join(lines) or "(empty)"

    def export(self) -> dict:
        meta = self._meta()
        return {t: [{"text": e, **meta.get(self._key(e), {})} for e in self._read(t)] for t in FILES}


class SessionStore:
    """All conversations in SQLite, searchable with FTS5."""

    def __init__(self, home: Path):
        Path(home).mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(str(Path(home) / "state.db"), check_same_thread=False)
        self._lock = threading.Lock()
        with self._lock:
            self.db.executescript("""
            create table if not exists sessions(id text primary key, started real, title text, parent text);
            create table if not exists messages(id integer primary key, session_id text, role text,
                                                content text, ts real);
            create virtual table if not exists messages_fts using fts5(content, content='messages', content_rowid='id');
            create trigger if not exists m_ai after insert on messages begin
              insert into messages_fts(rowid, content) values (new.id, new.content); end;
            """)

    def start(self, sid, title="", parent=None):
        with self._lock:
            self.db.execute("insert or ignore into sessions values(?,?,?,?)",
                            (sid, time.time(), title, parent))
            self.db.commit()

    def add(self, sid, role, content):
        if not content:
            return
        with self._lock:
            self.db.execute("insert into messages(session_id, role, content, ts) values(?,?,?,?)",
                            (sid, role, content, time.time()))
            self.db.commit()

    def search(self, query, limit=5, exclude_session=None):
        toks = re.findall(r"\w+", query)
        if not toks:
            return []
        q = " OR ".join(f'"{t}"' for t in toks)
        sql = ("select m.session_id, m.role, snippet(messages_fts, 0, '[', ']', '…', 24), s.started "
               "from messages_fts join messages m on m.id = messages_fts.rowid "
               "join sessions s on s.id = m.session_id where messages_fts match ? "
               + ("and m.session_id != ? " if exclude_session else "")
               + "and m.role in ('user','assistant','summary') order by rank limit ?")
        args = [q] + ([exclude_session] if exclude_session else []) + [limit]
        with self._lock:
            rows = self.db.execute(sql, args).fetchall()
        return [{"session": r[0], "role": r[1], "snippet": r[2],
                 "when": time.strftime("%Y-%m-%d %H:%M", time.localtime(r[3]))} for r in rows]
