"""External services through their official APIs. Credentials come from environment variables only.

Every client takes an injectable transport so it can be tested without the network:
  http(method, url, headers=None, body=None) -> (status, text, headers)
  raw(method, url, headers=None, body=None)  -> (status, bytes, headers)
"""
from __future__ import annotations

import base64
import email
import imaplib
import io
import json
import math
import os
import re
import shutil
import smtplib
import sqlite3
import time
import urllib.parse
import xml.etree.ElementTree as ET
import zipfile
from datetime import date, datetime, timedelta, timezone
from email.header import decode_header, make_header
from email.message import EmailMessage
from pathlib import Path


class IntegrationError(Exception):
    pass


def need_env(name, hint=""):
    v = os.environ.get(name, "").strip()
    if not v:
        raise IntegrationError(f"{name} is not set. {hint}".strip())
    return v


def _json(status, text, what):
    if status >= 400:
        raise IntegrationError(f"{what}: HTTP {status} {str(text)[:200]}")
    return json.loads(text) if text and text.strip() else {}


# ------------------------------------------------------------------ GitHub
def github(http, action, repo="", **kw):
    h = {"Authorization": f"Bearer {need_env('GITHUB_TOKEN', 'Create a fine-grained token at github.com/settings/tokens.')}",
         "Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28", "Content-Type": "application/json"}
    base = "https://api.github.com"
    if action != "my_repos" and action != "search_repos" and not re.fullmatch(r"[\w.\-]+/[\w.\-]+", repo or ""):
        raise IntegrationError("repo must look like owner/name")
    if action == "my_repos":
        d = _json(*http("GET", f"{base}/user/repos?per_page=30&sort=updated", h)[:2], "github")
        return [{"repo": r["full_name"], "private": r["private"], "updated": r["updated_at"][:10], "about": r.get("description")} for r in d]
    if action == "search_repos":
        d = _json(*http("GET", f"{base}/search/repositories?q={urllib.parse.quote(kw['query'])}&per_page=10", h)[:2], "github")
        return [{"repo": r["full_name"], "stars": r["stargazers_count"], "about": r.get("description")} for r in d.get("items", [])]
    if action == "issues":
        d = _json(*http("GET", f"{base}/repos/{repo}/issues?state={kw.get('state', 'open')}&per_page=20", h)[:2], "github")
        return [{"number": i["number"], "title": i["title"], "pull_request": "pull_request" in i, "by": i["user"]["login"]} for i in d]
    if action == "create_issue":
        d = _json(*http("POST", f"{base}/repos/{repo}/issues", h, json.dumps({"title": kw["title"], "body": kw.get("body", "")}).encode())[:2], "github")
        return {"number": d["number"], "url": d["html_url"]}
    if action == "read_file":
        path = urllib.parse.quote(kw["path"].lstrip("/"))
        ref = f"?ref={urllib.parse.quote(kw['ref'])}" if kw.get("ref") else ""
        d = _json(*http("GET", f"{base}/repos/{repo}/contents/{path}{ref}", h)[:2], "github")
        if isinstance(d, list):
            return {"directory": [x["name"] + ("/" if x["type"] == "dir" else "") for x in d]}
        return {"path": d["path"], "text": base64.b64decode(d["content"]).decode("utf-8", "replace")[:12000]}
    raise IntegrationError("action: my_repos|search_repos|issues|create_issue|read_file")


# ------------------------------------------------------------------ email (IMAP read / SMTP send)
def default_imap():
    m = imaplib.IMAP4_SSL(need_env("EMAIL_IMAP_HOST", "e.g. imap.gmail.com (needs an app password)."))
    m.login(need_env("EMAIL_USER"), need_env("EMAIL_PASSWORD", "Use an app password, not your main password."))
    return m


def default_smtp():
    host, port = need_env("EMAIL_SMTP_HOST", "e.g. smtp.gmail.com"), int(os.environ.get("EMAIL_SMTP_PORT", "465"))
    s = smtplib.SMTP_SSL(host, port) if port == 465 else smtplib.SMTP(host, port)
    if port != 465:
        s.starttls()
    s.login(need_env("EMAIL_USER"), need_env("EMAIL_PASSWORD"))
    return s


def _dh(v):
    return str(make_header(decode_header(v or "")))


def _text_body(msg):
    part = None
    if msg.is_multipart():
        for p in msg.walk():
            if p.get_content_type() == "text/plain" and not p.get_filename():
                part = p
                break
        if part is None:
            for p in msg.walk():
                if p.get_content_type() == "text/html":
                    part = p
                    break
    else:
        part = msg
    if part is None:
        return ""
    raw = part.get_payload(decode=True) or b""
    text = raw.decode(part.get_content_charset() or "utf-8", "replace")
    return re.sub(r"\s+\n", "\n", re.sub(r"<[^>]+>", " ", text)) if part.get_content_type() == "text/html" else text


def email_list(imap_factory, n=10, folder="INBOX"):
    m = imap_factory()
    try:
        m.select(folder, readonly=True)  # never marks mail as read
        typ, data = m.uid("search", None, "ALL")
        uids = (data[0] or b"").split()[-n:][::-1]
        out = []
        for u in uids:
            typ, d = m.uid("fetch", u, "(BODY.PEEK[HEADER.FIELDS (FROM SUBJECT DATE)])")
            h = email.message_from_bytes(d[0][1])
            out.append({"uid": u.decode(), "from": _dh(h["From"]), "subject": _dh(h["Subject"]), "date": h["Date"]})
        return out
    finally:
        try:
            m.logout()
        except Exception:
            pass


def email_read(imap_factory, uid, folder="INBOX", max_chars=6000):
    m = imap_factory()
    try:
        m.select(folder, readonly=True)
        typ, d = m.uid("fetch", str(uid).encode(), "(BODY.PEEK[])")
        if not d or not d[0]:
            raise IntegrationError(f"no message with uid {uid}")
        msg = email.message_from_bytes(d[0][1])
        atts = [p.get_filename() for p in msg.walk() if p.get_filename()]
        return {"from": _dh(msg["From"]), "to": _dh(msg["To"]), "subject": _dh(msg["Subject"]), "date": msg["Date"],
                "attachments": atts, "body": _text_body(msg)[:max_chars]}
    finally:
        try:
            m.logout()
        except Exception:
            pass


def email_send(smtp_factory, to, subject, body, sender=None):
    if not re.fullmatch(r"[^@\s,;]+@[^@\s,;]+\.[^@\s,;]+", to or ""):
        raise IntegrationError("one valid recipient address required")
    msg = EmailMessage()
    msg["From"], msg["To"], msg["Subject"] = sender or os.environ.get("EMAIL_USER", ""), to, subject
    msg.set_content(body)
    s = smtp_factory()
    try:
        s.send_message(msg)
    finally:
        try:
            s.quit()
        except Exception:
            pass
    return f"sent to {to}"


# ------------------------------------------------------------------ calendar (ICS: local file + secret iCal URLs)
def _unfold(t):
    return re.sub(r"\r?\n[ \t]", "", t)


def _dt(value, params):
    value = value.strip()
    if "VALUE=DATE" in params or re.fullmatch(r"\d{8}", value):
        return datetime.strptime(value[:8], "%Y%m%d"), True
    if value.endswith("Z"):
        return datetime.strptime(value, "%Y%m%dT%H%M%SZ").replace(tzinfo=timezone.utc).astimezone().replace(tzinfo=None), False
    dt = datetime.strptime(value[:15], "%Y%m%dT%H%M%S")
    m = re.search(r"TZID=([^;:]+)", params)
    if m:
        try:
            from zoneinfo import ZoneInfo
            dt = dt.replace(tzinfo=ZoneInfo(m.group(1))).astimezone().replace(tzinfo=None)
        except Exception:
            pass
    return dt, False


def parse_ics(text):
    events, cur = [], None
    for line in _unfold(text).splitlines():
        if line == "BEGIN:VEVENT":
            cur = {}
        elif line == "END:VEVENT" and cur is not None:
            if "start" in cur:
                cur.setdefault("end", cur["start"] + (timedelta(days=1) if cur.get("all_day") else timedelta(hours=1)))
                events.append(cur)
            cur = None
        elif cur is not None and ":" in line:
            key, _, val = line.partition(":")
            name, _, params = key.partition(";")
            if name == "DTSTART":
                cur["start"], cur["all_day"] = _dt(val, params)
            elif name == "DTEND":
                cur["end"], _ = _dt(val, params)
            elif name == "SUMMARY":
                cur["summary"] = val.replace("\\,", ",").replace("\\n", " ")
            elif name == "LOCATION":
                cur["location"] = val.replace("\\,", ",")
            elif name == "RRULE":
                cur["rrule"] = dict(p.split("=", 1) for p in val.split(";") if "=" in p)
    return events


_DAYS = ["MO", "TU", "WE", "TH", "FR", "SA", "SU"]


def occurrences(ev, win_start, win_end):
    """Expand simple recurrence rules (DAILY/WEEKLY/MONTHLY/YEARLY, INTERVAL, COUNT, UNTIL, BYDAY). EXDATE is ignored."""
    dur = ev["end"] - ev["start"]
    r = ev.get("rrule")
    if not r:
        if ev["end"] > win_start and ev["start"] < win_end:
            yield ev["start"], ev["start"] + dur
        return
    freq, interval = r.get("FREQ"), int(r.get("INTERVAL", 1))
    count = int(r["COUNT"]) if "COUNT" in r else None
    until = _dt(r["UNTIL"], "")[0] if "UNTIL" in r else None
    byday = [_DAYS.index(d[-2:]) for d in r.get("BYDAY", "").split(",") if d[-2:] in _DAYS]
    d0, n, day = ev["start"].date(), 0, ev["start"].date()
    while datetime.combine(day, ev["start"].time()) < win_end and (day - d0).days < 366 * 5:
        delta = (day - d0).days
        hit = False
        if freq == "DAILY":
            hit = delta % interval == 0
        elif freq == "WEEKLY":
            hit = (day.weekday() in (byday or [d0.weekday()])) and (delta // 7) % interval == 0
        elif freq == "MONTHLY":
            hit = day.day == d0.day and ((day.year - d0.year) * 12 + day.month - d0.month) % interval == 0
        elif freq == "YEARLY":
            hit = (day.month, day.day) == (d0.month, d0.day) and (day.year - d0.year) % interval == 0
        if hit:
            s = datetime.combine(day, ev["start"].time())
            if until and s > until:
                return
            n += 1
            if count and n > count:
                return
            if s + dur > win_start:
                yield s, s + dur
        day += timedelta(days=1)


def agenda(events, start, end):
    items = [(s, e, ev) for ev in events for s, e in occurrences(ev, start, end)]
    items.sort(key=lambda x: x[0])
    return [{"when": s.strftime("%Y-%m-%d") if ev.get("all_day") else s.strftime("%Y-%m-%d %H:%M") + "-" + e.strftime("%H:%M"),
             "title": ev.get("summary", "(no title)"), "where": ev.get("location", "")} for s, e, ev in items]


def add_local_event(path: Path, summary, start: datetime, end: datetime, location=""):
    uid = f"{int(time.time() * 1000)}@courier"
    ev = ["BEGIN:VEVENT", f"UID:{uid}", f"DTSTAMP:{datetime.now():%Y%m%dT%H%M%S}", f"DTSTART:{start:%Y%m%dT%H%M%S}",
          f"DTEND:{end:%Y%m%dT%H%M%S}", "SUMMARY:" + summary.replace(",", "\\,").replace("\n", " ")]
    if location:
        ev.append("LOCATION:" + location.replace(",", "\\,"))
    ev.append("END:VEVENT")
    text = path.read_text() if path.exists() else "BEGIN:VCALENDAR\r\nVERSION:2.0\r\nPRODID:-//Courier//EN\r\nEND:VCALENDAR\r\n"
    path.write_text(text.replace("END:VCALENDAR", "\r\n".join(ev) + "\r\nEND:VCALENDAR"))
    return uid


# ------------------------------------------------------------------ Todoist
def todoist(http, action, base="https://api.todoist.com/rest/v2", **kw):
    """Check Todoist's current API docs: the base URL is configurable (todoist_base) because versions change."""
    h = {"Authorization": f"Bearer {need_env('TODOIST_TOKEN', 'Todoist Settings > Integrations > Developer.')}", "Content-Type": "application/json"}
    if action == "list":
        d = _json(*http("GET", f"{base}/tasks", h)[:2], "todoist")
        return [{"id": t["id"], "content": t["content"], "due": (t.get("due") or {}).get("string"), "priority": t.get("priority")} for t in d[:30]]
    if action == "add":
        body = {"content": kw["content"]}
        if kw.get("due"):
            body["due_string"] = kw["due"]
        if kw.get("priority"):
            body["priority"] = int(kw["priority"])
        d = _json(*http("POST", f"{base}/tasks", h, json.dumps(body).encode())[:2], "todoist")
        return {"id": d["id"], "content": d["content"]}
    if action == "close":
        st, tx, _ = http("POST", f"{base}/tasks/{kw['id']}/close", h)
        if st >= 400:
            raise IntegrationError(f"todoist: HTTP {st}")
        return {"closed": kw["id"]}
    raise IntegrationError("action: list|add|close")


# ------------------------------------------------------------------ cloud storage backends
class DropboxBackend:
    def __init__(self, raw, token=None):
        self.raw, self.token = raw, token or need_env("DROPBOX_TOKEN", "Create an app at dropbox.com/developers and generate an access token.")

    def _h(self, extra=None):
        return {"Authorization": f"Bearer {self.token}", **(extra or {})}

    def list(self, path=""):
        st, b, _ = self.raw("POST", "https://api.dropboxapi.com/2/files/list_folder", self._h({"Content-Type": "application/json"}),
                            json.dumps({"path": "" if path in ("", "/") else "/" + path.strip("/")}).encode())
        d = _json(st, b.decode(errors="replace"), "dropbox")
        return [e["name"] + ("/" if e[".tag"] == "folder" else "") for e in d.get("entries", [])]

    def get(self, path):
        st, b, _ = self.raw("POST", "https://content.dropboxapi.com/2/files/download",
                            self._h({"Dropbox-API-Arg": json.dumps({"path": "/" + path.strip("/")})}), b"")
        if st >= 400:
            raise IntegrationError(f"dropbox: HTTP {st} {b[:150]!r}")
        return b

    def put(self, path, data):
        st, b, _ = self.raw("POST", "https://content.dropboxapi.com/2/files/upload",
                            self._h({"Dropbox-API-Arg": json.dumps({"path": "/" + path.strip("/"), "mode": "overwrite", "mute": True}),
                                     "Content-Type": "application/octet-stream"}), data)
        if st >= 400:
            raise IntegrationError(f"dropbox: HTTP {st} {b[:150]!r}")


class WebDavBackend:
    def __init__(self, raw, url=None, user=None, password=None):
        self.raw = raw
        self.url = (url or need_env("WEBDAV_URL", "e.g. https://cloud.example.com/remote.php/dav/files/you")).rstrip("/")
        cred = f"{user or need_env('WEBDAV_USER')}:{password or need_env('WEBDAV_PASSWORD')}"
        self.auth = "Basic " + base64.b64encode(cred.encode()).decode()

    def _u(self, path):
        return self.url + "/" + urllib.parse.quote(path.strip("/"))

    def list(self, path=""):
        st, b, _ = self.raw("PROPFIND", self._u(path), {"Authorization": self.auth, "Depth": "1"}, None)
        if st >= 400:
            raise IntegrationError(f"webdav: HTTP {st}")
        text = b.decode(errors="replace")
        if "<!DOCTYPE" in text or "<!ENTITY" in text:
            raise IntegrationError("webdav: DTD/entities rejected")
        names = []
        for r in ET.fromstring(text).iter("{DAV:}href"):
            name = urllib.parse.unquote(r.text or "").rstrip("/").rsplit("/", 1)[-1]
            if name and name != path.strip("/").rsplit("/", 1)[-1]:
                names.append(name)
        return names

    def get(self, path):
        st, b, _ = self.raw("GET", self._u(path), {"Authorization": self.auth}, None)
        if st >= 400:
            raise IntegrationError(f"webdav: HTTP {st}")
        return b

    def put(self, path, data):
        parts = path.strip("/").split("/")[:-1]
        for i in range(1, len(parts) + 1):
            self.raw("MKCOL", self._u("/".join(parts[:i])), {"Authorization": self.auth}, None)  # 405 = exists
        st, b, _ = self.raw("PUT", self._u(path), {"Authorization": self.auth}, data)
        if st >= 400:
            raise IntegrationError(f"webdav: HTTP {st}")


def make_backend(provider, raw):
    if provider == "dropbox":
        return DropboxBackend(raw)
    if provider == "webdav":
        return WebDavBackend(raw)
    raise IntegrationError("provider: dropbox|webdav")


# ------------------------------------------------------------------ backup / manual sync through cloud storage
SYNC_SKIP = {"checkpoints", "STOP", "audit.jsonl", "usage.db", "sync-staging", "rollback", "cache.db"}


def make_zip(home: Path) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for p in Path(home).rglob("*"):
            rel = p.relative_to(home)
            if p.is_file() and not (set(rel.parts) & SYNC_SKIP):
                z.write(p, rel)
    return buf.getvalue()


def safe_extract(data: bytes, dest: Path, max_bytes=300_000_000):
    dest.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        if sum(i.file_size for i in z.infolist()) > max_bytes:
            raise IntegrationError("archive too large")
        for n in z.namelist():
            if n.startswith("/") or ".." in Path(n).parts:
                raise IntegrationError("unsafe path in archive")
        z.extractall(dest)


def sync_push(backend, home: Path, device: str):
    data, ts = make_zip(home), time.strftime("%Y%m%d-%H%M%S")
    backend.put(f"courier-sync/{device}/{ts}.zip", data)
    backend.put(f"courier-sync/{device}/latest.zip", data)
    return {"uploaded": f"courier-sync/{device}/{ts}.zip", "bytes": len(data)}


def sync_pull(backend, home: Path, device: str, apply=False):
    """Downloads into home/sync-staging/<device>. apply=True copies over the live data after saving a safety copy.
    This is a manual push/pull of whole snapshots, not automatic merging: the newest push wins."""
    data = backend.get(f"courier-sync/{device}/latest.zip")
    stage = Path(home) / "sync-staging" / device
    shutil.rmtree(stage, ignore_errors=True)
    safe_extract(data, stage)
    if apply:
        safety = Path(str(home) + ".before-sync")
        shutil.copytree(home, safety, dirs_exist_ok=True, ignore=shutil.ignore_patterns("sync-staging"))
        shutil.copytree(stage, home, dirs_exist_ok=True)
        return {"applied": True, "safety_copy": str(safety)}
    return {"applied": False, "staged_at": str(stage)}


# ------------------------------------------------------------------ messaging
def send_message(http, channel, text, to=None):
    text = text[:3500]
    if channel == "telegram":
        tok = need_env("TELEGRAM_BOT_TOKEN", "Create a bot with @BotFather.")
        st, tx, _ = http("POST", f"https://api.telegram.org/bot{tok}/sendMessage", {"Content-Type": "application/json"},
                         json.dumps({"chat_id": to, "text": text}).encode())
        _json(st, tx, "telegram")
    elif channel == "slack":
        st, tx, _ = http("POST", need_env("SLACK_WEBHOOK_URL", "Create an incoming webhook in Slack."), {"Content-Type": "application/json"},
                         json.dumps({"text": text}).encode())
        if st >= 400:
            raise IntegrationError(f"slack: HTTP {st}")
    elif channel == "discord":
        st, tx, _ = http("POST", need_env("DISCORD_WEBHOOK_URL", "Channel settings > Integrations > Webhooks."), {"Content-Type": "application/json"},
                         json.dumps({"content": text}).encode())
        if st >= 400:
            raise IntegrationError(f"discord: HTTP {st}")
    else:
        raise IntegrationError("channel: telegram|slack|discord")
    return f"sent via {channel}"


# ------------------------------------------------------------------ public datasets: weather, maps, papers
PUBLIC_HOSTS = {"api.open-meteo.com", "geocoding-api.open-meteo.com", "earthquake.usgs.gov", "api.worldbank.org",
                "api.crossref.org", "en.wikipedia.org", "export.arxiv.org", "api.nasa.gov", "data.gov.uk", "api.gbif.org"}


def geocode(http, name):
    st, tx, _ = http("GET", f"https://geocoding-api.open-meteo.com/v1/search?name={urllib.parse.quote(name)}&count=1&language=en&format=json")
    res = _json(st, tx, "geocoding").get("results")
    if not res:
        raise IntegrationError(f"place not found: {name}")
    r = res[0]
    return {"name": r["name"], "country": r.get("country", ""), "lat": r["latitude"], "lon": r["longitude"], "timezone": r.get("timezone", "")}


def weather(http, place, days=3):
    g = geocode(http, place)
    url = (f"https://api.open-meteo.com/v1/forecast?latitude={g['lat']}&longitude={g['lon']}&timezone=auto&forecast_days={int(days)}"
           "&current=temperature_2m,wind_speed_10m,precipitation&daily=temperature_2m_max,temperature_2m_min,precipitation_sum")
    d = _json(*http("GET", url)[:2], "weather")
    daily = d.get("daily", {})
    return {"place": f"{g['name']}, {g['country']}", "source": "Open-Meteo", "current": d.get("current"),
            "daily": [{"date": t, "max_c": daily["temperature_2m_max"][i], "min_c": daily["temperature_2m_min"][i],
                       "rain_mm": daily["precipitation_sum"][i]} for i, t in enumerate(daily.get("time", []))]}


def distance_km(a, b):
    p = math.pi / 180
    h = 0.5 - math.cos((b["lat"] - a["lat"]) * p) / 2 + math.cos(a["lat"] * p) * math.cos(b["lat"] * p) * (1 - math.cos((b["lon"] - a["lon"]) * p)) / 2
    return round(12742 * math.asin(math.sqrt(h)), 1)


def paper_search(http, query, n=5):
    st, tx, _ = http("GET", f"https://api.crossref.org/works?query={urllib.parse.quote(query)}&rows={int(n)}&select=title,author,issued,DOI,container-title")
    out = []
    for w in _json(st, tx, "crossref").get("message", {}).get("items", []):
        yr = (w.get("issued", {}).get("date-parts") or [[None]])[0][0]
        out.append({"title": (w.get("title") or [""])[0], "year": yr, "doi": "https://doi.org/" + w["DOI"],
                    "journal": (w.get("container-title") or [""])[0],
                    "authors": [f"{a.get('given', '')} {a.get('family', '')}".strip() for a in w.get("author", [])[:4]]})
    return out


# ------------------------------------------------------------------ read-only database access
def db_query(path: Path, sql: str, limit=200, max_ops=2_000_000):
    s = sql.strip().rstrip(";")
    if not re.match(r"(?is)^(select|with)\b", s) or ";" in s or not sqlite3.complete_statement(s + ";"):
        raise IntegrationError("only a single SELECT/WITH statement is allowed")
    con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        con.execute("PRAGMA query_only=1")
        ops = [0]
        con.set_progress_handler(lambda: (ops.__setitem__(0, ops[0] + 1) or ops[0] > max_ops // 1000) and 1 or 0, 1000)
        cur = con.execute(s)
        cols = [c[0] for c in cur.description or []]
        rows = cur.fetchmany(limit)
        return {"columns": cols, "rows": rows, "truncated": len(rows) == limit}
    except sqlite3.Error as e:
        raise IntegrationError(f"query failed: {e}")
    finally:
        con.close()
