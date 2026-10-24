"""Organizer: to-dos, scheduled jobs, folder/disk watchers, notifications, system status."""
from __future__ import annotations

import json
import os
import platform
import re
import shutil
import sqlite3
import subprocess
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path

PRIORITY = {"high": 0, "med": 1, "low": 2}
MAX_FAILS = 3


def parse_when(s: str, now: float) -> float:
    """'2026-10-25 09:00' (local) -> timestamp."""
    return datetime.strptime(s.strip(), "%Y-%m-%d %H:%M").timestamp()


def next_run(schedule: str, after: float):
    """'every 30m|2h|1d' | 'daily HH:MM' | 'once YYYY-MM-DD HH:MM'. Returns None when finished."""
    s = schedule.strip().lower()
    m = re.fullmatch(r"every (\d+)\s*([mhd])", s)
    if m:
        return after + int(m.group(1)) * {"m": 60, "h": 3600, "d": 86400}[m.group(2)]
    m = re.fullmatch(r"daily (\d{1,2}):(\d{2})", s)
    if m:
        t = datetime.fromtimestamp(after).replace(hour=int(m.group(1)), minute=int(m.group(2)), second=0, microsecond=0)
        if t.timestamp() <= after:
            t += timedelta(days=1)
        return t.timestamp()
    m = re.fullmatch(r"once (.+)", s)
    if m:
        t = parse_when(m.group(1), after)
        return t if t > after else None
    raise ValueError("schedule must be 'every 30m|2h|1d', 'daily HH:MM' or 'once YYYY-MM-DD HH:MM'")


class Organizer:
    def __init__(self, home: Path):
        Path(home).mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(str(Path(home) / "organizer.db"), check_same_thread=False)
        self._lock = threading.Lock()
        with self._lock:
            self.db.executescript("""
            create table if not exists todos(id integer primary key, title text, priority text, due text,
                project text, recurring text, status text default 'open', created real, done_at real);
            create table if not exists jobs(id integer primary key, name text, kind text, schedule text,
                prompt text, params text, next_run real, last_run real, last_status text, fails int default 0,
                enabled int default 1);
            create table if not exists notifications(id integer primary key, ts real, level text, text text, read int default 0);
            create table if not exists sent(key text primary key, ts real, result text);
            """)

    # ---- to-dos
    def todo_add(self, title, priority="med", due=None, project="", recurring=None):
        if priority not in PRIORITY:
            raise ValueError("priority: high|med|low")
        if recurring not in (None, "", "daily", "weekly", "monthly"):
            raise ValueError("recurring: daily|weekly|monthly")
        with self._lock:
            c = self.db.execute("insert into todos(title,priority,due,project,recurring,created) values(?,?,?,?,?,?)",
                                (title, priority, due, project, recurring or None, time.time()))
            self.db.commit()
            return c.lastrowid

    def todo_list(self, status="open", project=None):
        q, a = "select id,title,priority,due,project,recurring,status from todos where status=?", [status]
        if project:
            q += " and project=?"
            a.append(project)
        with self._lock:
            rows = self.db.execute(q, a).fetchall()
        rows.sort(key=lambda r: (PRIORITY[r[2]], r[3] or "9999"))
        return [dict(zip(("id", "title", "priority", "due", "project", "recurring", "status"), r)) for r in rows]

    def todo_done(self, tid):
        with self._lock:
            r = self.db.execute("select title,priority,due,project,recurring from todos where id=? and status='open'", (tid,)).fetchone()
            if not r:
                return "error: no open todo with that id"
            self.db.execute("update todos set status='done', done_at=? where id=?", (time.time(), tid))
            self.db.commit()
        if r[4] and r[2]:
            d = datetime.strptime(r[2][:10], "%Y-%m-%d")
            d += {"daily": timedelta(days=1), "weekly": timedelta(weeks=1), "monthly": timedelta(days=30)}[r[4]]
            nid = self.todo_add(r[0], r[1], d.strftime("%Y-%m-%d"), r[3], r[4])
            return f"done; next recurring item #{nid} due {d:%Y-%m-%d}"
        return "done"

    # ---- jobs
    def job_add(self, name, kind, schedule, prompt="", params=None, now=None):
        if kind not in ("reminder", "prompt", "watch", "disk"):
            raise ValueError("kind: reminder|prompt|watch|disk")
        now = now or time.time()
        nxt = next_run(schedule, now)
        if nxt is None:
            raise ValueError("that time is in the past")
        with self._lock:
            c = self.db.execute("insert into jobs(name,kind,schedule,prompt,params,next_run) values(?,?,?,?,?,?)",
                                (name, kind, schedule, prompt, json.dumps(params or {}), nxt))
            self.db.commit()
            return c.lastrowid

    def job_list(self):
        with self._lock:
            rows = self.db.execute("select id,name,kind,schedule,next_run,last_run,last_status,fails,enabled from jobs").fetchall()
        return [dict(zip(("id", "name", "kind", "schedule", "next_run", "last_run", "last_status", "fails", "enabled"), r)) for r in rows]

    def job_remove(self, jid):
        with self._lock:
            n = self.db.execute("delete from jobs where id=?", (jid,)).rowcount
            self.db.commit()
        return "removed" if n else "error: no such job"

    # ---- notifications
    def notify(self, text, level="info"):
        with self._lock:
            self.db.execute("insert into notifications(ts,level,text) values(?,?,?)", (time.time(), level, text))
            self.db.commit()

    def notifications(self, unread_only=True, mark_read=False):
        with self._lock:
            rows = self.db.execute("select id,ts,level,text from notifications" + (" where read=0" if unread_only else "") + " order by id").fetchall()
            if mark_read and rows:
                self.db.execute("update notifications set read=1 where read=0")
                self.db.commit()
        return [{"id": r[0], "when": time.strftime("%Y-%m-%d %H:%M", time.localtime(r[1])), "level": r[2], "text": r[3]} for r in rows]

    # ---- idempotency for outbound actions
    def sent_get(self, key):
        with self._lock:
            r = self.db.execute("select result from sent where key=?", (key,)).fetchone()
        return r[0] if r else None

    def sent_put(self, key, result):
        with self._lock:
            self.db.execute("insert or replace into sent values(?,?,?)", (key, time.time(), result[:500]))
            self.db.commit()

    # ---- scheduler
    def run_due(self, runner=None, now=None):
        """Run every due job once. The next run is saved BEFORE running, so a crash cannot double-fire."""
        now = now or time.time()
        with self._lock:
            due = self.db.execute("select id,name,kind,schedule,prompt,params,fails from jobs where enabled=1 and next_run<=?", (now,)).fetchall()
        results = []
        for jid, name, kind, sched, prompt, params, fails in due:
            params = json.loads(params or "{}")
            try:
                nxt = next_run(sched, now)
            except ValueError:
                nxt = None
            with self._lock:
                self.db.execute("update jobs set next_run=?, enabled=? where id=?", (nxt or 0, 1 if nxt else 0, jid))
                self.db.commit()
            try:
                msg = self._run_job(kind, name, prompt, params, runner)
                status, fails = "ok", 0
                if msg:
                    self.notify(f"[{name}] {msg}")
            except Exception as e:
                fails += 1
                status = f"failed: {e}"
                self.notify(f"[{name}] failed ({fails}/{MAX_FAILS}): {e}", "error")
                if fails >= MAX_FAILS:
                    with self._lock:
                        self.db.execute("update jobs set enabled=0 where id=?", (jid,))
                    self.notify(f"[{name}] disabled after {MAX_FAILS} failures", "error")
                elif nxt:
                    with self._lock:
                        self.db.execute("update jobs set next_run=? where id=?", (now + 300, jid))
            with self._lock:
                self.db.execute("update jobs set last_run=?, last_status=?, fails=? where id=?", (now, status, fails, jid))
                self.db.commit()
            results.append((name, status))
        return results

    def _run_job(self, kind, name, prompt, params, runner):
        if kind == "reminder":
            return prompt or name
        if kind == "prompt":
            if runner is None:
                raise RuntimeError("no agent available to run prompt jobs")
            return runner(prompt)
        if kind == "disk":
            u = shutil.disk_usage(params.get("path", "."))
            free = 100 * u.free / u.total
            return f"low disk space: {free:.0f}% free on {params.get('path', '.')}" if free < params.get("min_free_pct", 10) else ""
        if kind == "watch":
            folder = Path(params["path"])
            if not folder.is_dir():
                raise FileNotFoundError(f"watch folder missing: {folder}")
            seen = set(params.get("seen", []))
            now_files = {p.name for p in folder.iterdir() if p.is_file()}
            if "seen" not in params:  # first run records a baseline instead of flooding
                new = set()
            else:
                new = now_files - seen
            with self._lock:
                row = self.db.execute("select params from jobs where name=? and kind='watch'", (name,)).fetchone()
                p = json.loads(row[0]); p["seen"] = sorted(now_files)
                self.db.execute("update jobs set params=? where name=? and kind='watch'", (json.dumps(p), name))
                self.db.commit()
            return f"{len(new)} new file(s): " + ", ".join(sorted(new)[:10]) if new else ""
        raise ValueError(kind)

    def brief(self):
        today = time.strftime("%Y-%m-%d")
        todos = self.todo_list()
        overdue = [t for t in todos if t["due"] and t["due"][:10] < today]
        due_today = [t for t in todos if t["due"] and t["due"][:10] == today]
        lines = [f"Briefing for {today}"]
        lines.append(f"Overdue ({len(overdue)}): " + "; ".join(t["title"] for t in overdue) if overdue else "Overdue: none")
        lines.append(f"Due today ({len(due_today)}): " + "; ".join(t["title"] for t in due_today) if due_today else "Due today: none")
        lines.append(f"Open to-dos: {len(todos)} (high priority: {sum(t['priority'] == 'high' for t in todos)})")
        n = self.notifications()
        lines.append(f"Unread notifications: {len(n)}" + ("".join(f"\n  - {x['text']}" for x in n[:5])))
        return "\n".join(lines)


def system_status(what="summary", path="."):
    info = {"platform": platform.platform(), "python": platform.python_version(), "cpus": os.cpu_count()}
    u = shutil.disk_usage(path)
    info["disk"] = {"total_gb": round(u.total / 1e9, 1), "free_gb": round(u.free / 1e9, 1), "free_pct": round(100 * u.free / u.total, 1)}
    try:
        info["load_avg"] = [round(x, 2) for x in os.getloadavg()]
    except (AttributeError, OSError):
        pass
    try:
        mem = dict(l.split(":") for l in Path("/proc/meminfo").read_text().splitlines()[:3])
        info["memory_mb"] = {k.strip(): int(v.split()[0]) // 1024 for k, v in mem.items()}
    except OSError:
        pass  # non-Linux: install psutil for memory/battery details
    if what == "processes":
        cmd = ["tasklist"] if platform.system() == "Windows" else ["ps", "-eo", "pid,pcpu,pmem,comm", "--sort=-pcpu"]
        try:
            info["processes"] = subprocess.run(cmd, capture_output=True, text=True, timeout=10).stdout.splitlines()[:15]
        except (OSError, subprocess.TimeoutExpired) as e:
            info["processes"] = [f"unavailable: {e}"]
    return info
