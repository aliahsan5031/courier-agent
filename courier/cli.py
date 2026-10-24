"""courier CLI."""
from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import time
import zipfile
from pathlib import Path

from .agent import Agent
from .audit import AuditLog, StopFlag
from .config import home_dir, load_config, space_home
from .providers import AllProvidersFailed, ProviderError, Router, default_providers, list_models


def _approve(cmd: str) -> bool:
    return input(f"\n  approve? {cmd}\n  [y/N] ").strip().lower() == "y"


def _events(kind, *a):
    if kind == "tool":
        print(f"  ⚙ {a[0]}({json.dumps(a[1], default=str)[:100]})", file=sys.stderr)
    elif kind == "reflect":
        print(f"  ✎ reflection: {str(a[0])[:100]}", file=sys.stderr)
    elif kind == "compact":
        print(f"  … compacted {a[0]} messages", file=sys.stderr)


def make_agent(args, approve=_approve):
    cfg = load_config()
    if cfg.get("_config_warning"):
        print("warning:", cfg["_config_warning"], file=sys.stderr)
    ag = Agent.create(cfg=cfg, approval="auto" if getattr(args, "yes", False) else "ask", approve_cb=approve,
                      on_event=_events, space=getattr(args, "space", None),
                      private=getattr(args, "private", False), safe=getattr(args, "safe", False),
                      style=getattr(args, "style", None))
    if getattr(args, "stream", False):
        ag.streamed = False

        def tok(t):
            ag.streamed = True
            print(t, end="", flush=True)
        ag.on_token = tok
    return ag


def _show(agent, text):
    """Print the answer unless it was already streamed token by token."""
    if getattr(agent, "streamed", False):
        print()
        agent.streamed = False
    else:
        print("\n" + text)


def _guarded_run(agent, text, args):
    """Ctrl-C = emergency stop: the loop halts before the next tool call and progress is saved."""
    import threading
    out = {}

    def work():
        try:
            out["r"] = agent.run(text, plan=getattr(args, "plan", False), verify=getattr(args, "verify", False))
        except Exception as e:
            out["r"] = f"error: {e}"

    t = threading.Thread(target=work, daemon=True)
    t.start()
    try:
        while t.is_alive():
            t.join(0.2)
    except KeyboardInterrupt:
        agent.ctx.stop.set()
        print("\n  ■ emergency stop requested; finishing the current step…", file=sys.stderr)
        t.join(30)
    return out.get("r", "(stopped)")


def cmd_chat(args):
    agent = make_agent(args)
    print("courier — /exit /memory /skills /mode concise|detailed /summary. Ctrl-C stops a running task.")
    print("providers:", " → ".join(p.name for p in agent.router.providers) if not agent.ctx.private else "local only (private mode)")
    n = agent.ctx.organizer.notifications()
    if n:
        print(f"({len(n)} unread notification(s): run `courier notifications`)")
    while True:
        try:
            text = input("\nyou› ").strip()
        except (EOFError, KeyboardInterrupt):
            break
        if text in ("/exit", "/quit"):
            break
        if text == "/memory":
            print(agent.ctx.memory.show("memory"), "\n--user--\n", agent.ctx.memory.show("user")); continue
        if text == "/skills":
            print(*[f"{s['name']}: {s['description']}" for s in agent.ctx.skills.index()] or ["(none)"], sep="\n"); continue
        if text.startswith("/mode"):
            agent.set_style(text.split()[-1] if text.split()[-1] in ("concise", "detailed") else "concise"); print("mode:", agent.style); continue
        if text == "/summary":
            print(agent.summarize_session() or "(too short to summarize)"); continue
        if text:
            _show(agent, _guarded_run(agent, text, args))
            print(f"  [{agent.last_status}] session {agent.ctx.session_id}", file=sys.stderr)
    try:
        if agent.summarize_session():
            print("session summary saved.")
    except Exception:
        pass


def cmd_run(args):
    agent = make_agent(args)
    _show(agent, _guarded_run(agent, " ".join(args.prompt), args))
    print(f"[{agent.last_status}] session {agent.ctx.session_id}", file=sys.stderr)
    return 0 if agent.last_status == "completed" else 2


def cmd_resume(args):
    cfg = load_config()
    ag = Agent.resume(args.session, cfg=cfg, approval="auto" if args.yes else "ask", approve_cb=_approve, on_event=_events)
    print(ag.continue_run())
    print(f"[{ag.last_status}]", file=sys.stderr)


def cmd_stop(args):
    StopFlag(home_dir()).touch_file()
    print("Stop signal sent. A running Courier task halts before its next step.")


def cmd_doctor(args):
    cfg = load_config()
    print(f"home: {home_dir()}")
    ok = False
    for name, p in default_providers(cfg).items():
        n = len(p.keys())
        extra = "local, no key needed" if p.local else f"keys={n} (env {p.key_env})"
        print(f"{name:11} {extra}  model={p.model}" + (f"  light={p.light_model}" if p.light_model else ""))
        usable = p.has_keys() and (not p.local or args.live)
        if args.live and p.has_keys():
            try:
                r = Router([p], retries=0).chat([{"role": "user", "content": "Reply with: pong"}], max_tokens=16)
                print(f"            live ok → {r.content.strip()[:40]!r}"); ok = True
            except Exception as e:
                print(f"            live FAILED → {e}")
        elif usable and not p.local:
            ok = True
    if not ok:
        print("\nNo usable provider. Set NVIDIA_API_KEY and/or OPENROUTER_API_KEY, or run Ollama and use --private.")
        return 1


def cmd_selftest(args):
    """Offline self-diagnostics: nothing here calls a model or the network."""
    import sqlite3
    import tempfile
    from .docs import KnowledgeBase, write_docx
    checks = []

    def check(name, fn):
        try:
            fn(); checks.append((name, True, ""))
        except Exception as e:
            checks.append((name, False, str(e)))
    d = Path(tempfile.mkdtemp())
    check("sqlite FTS5 available", lambda: sqlite3.connect(":memory:").execute("create virtual table t using fts5(a)"))
    check("home directory writable", lambda: (home_dir().mkdir(parents=True, exist_ok=True), (home_dir() / ".w").write_text("x"), (home_dir() / ".w").unlink()))
    check("config loads", lambda: load_config())
    check("tools build", lambda: Agent.create(cfg=load_config(), router=Router([]), home=d, workdir=d).tools)
    check("docx write/read round trip", lambda: (write_docx(d / "t.docx", ["hello"]), KnowledgeBase(d / "k.db").ingest(d / "t.docx")))
    check("provider keys present", lambda: (_ for _ in ()).throw(RuntimeError("none set")) if not any(p.has_keys() for p in default_providers(load_config()).values() if not p.local) else None)
    for n, ok, why in checks:
        print(("PASS " if ok else "FAIL ") + n + (f"  ({why})" if why else ""))
    return 0 if all(ok for _, ok, _ in checks) else 1


def cmd_models(args):
    p = default_providers(load_config())[args.provider]
    try:
        ids = list_models(p)
    except ProviderError as e:
        print(f"error: {e}"); return 1
    print("\n".join(sorted(i for i in ids if not args.free or i.endswith(":free"))))


def cmd_memory(args):
    from .memory import CuratedMemory
    m = CuratedMemory(space_home(home_dir(), args.space))
    a = args.action
    if a == "show":
        print("PAUSED" if m.paused() else "active")
        for t in ("memory", "user"):
            print(f"[{t}] {m.used(t)}/{m.limits[t]} chars\n{m.show(t)}\n")
    elif a == "export":
        Path(args.arg[0]).write_text(json.dumps(m.export(), indent=1)); print("exported to", args.arg[0])
    elif a == "edit":
        print(m.replace(args.arg[0], args.arg[1], args.arg[2]))
    elif a == "delete":
        print(m.remove(args.arg[0], args.arg[1]))
    elif a == "pause":
        m.pause(); print("memory paused: nothing is saved or recalled until you run `courier memory resume`")
    elif a == "resume":
        m.resume(); print("memory resumed")


def cmd_sessions(args):
    from .memory import SessionStore
    for r in SessionStore(home_dir()).search(" ".join(args.query)):
        print(f"{r['when']} {r['session']} {r['role']}: {r['snippet']}")


def cmd_kb(args):
    from .docs import KB_EXT, KnowledgeBase
    kb = KnowledgeBase(space_home(home_dir(), args.space) / "kb.db")
    from . import embed
    try:
        emb = embed.from_config(load_config())
    except Exception:
        emb = None
    if args.action == "ingest":
        p = Path(args.arg[0]).resolve()
        for f in ([p] if p.is_file() else [x for x in sorted(p.rglob("*")) if x.is_file() and x.suffix.lower() in KB_EXT]):
            try:
                print(kb.ingest(f))
            except Exception as e:
                print({"path": str(f), "status": f"skipped: {e}"})
        if emb:
            try:
                print("embedded chunks:", kb.embed_pending(emb))
            except embed.EmbedError as e:
                print("semantic indexing skipped:", e)
    else:
        try:
            hits = kb.search(" ".join(args.arg), embedder=emb)
        except embed.EmbedError as e:
            print("semantic search unavailable, using keywords:", e)
            hits = kb.search(" ".join(args.arg))
        for h in hits:
            print(f"{h['path']} | {h['where']} [{h['via']}]\n  {h['text'][:200]}")
    print(kb.stats())


def cmd_todo(args):
    from .sched import Organizer
    o = Organizer(home_dir())
    if args.action == "add":
        print("added #", o.todo_add(" ".join(args.arg), args.priority, args.due, args.project, args.recurring))
    elif args.action == "done":
        print(o.todo_done(int(args.arg[0])))
    else:
        for t in o.todo_list():
            print(f"#{t['id']} [{t['priority']}] {t['title']}" + (f"  due {t['due']}" if t["due"] else ""))


def cmd_jobs(args):
    from .sched import Organizer
    o = Organizer(home_dir())
    if args.action == "add":
        if len(args.arg) < 3:
            print('usage: courier jobs add NAME KIND "WHEN" [TEXT or PATH]\n  e.g. courier jobs add tea reminder "every 30m" drink water'); return 1
        name, kind, when, *text = args.arg
        params = {"path": text[0]} if kind in ("watch", "disk") and text else {}
        try:
            print("job #", o.job_add(name, kind, when, " ".join(text) if kind in ("reminder", "prompt") else "", params))
        except ValueError as e:
            print("error:", e); return 1
    elif args.action == "remove":
        print(o.job_remove(int(args.arg[0])))
    else:
        for j in o.job_list():
            nxt = time.strftime("%Y-%m-%d %H:%M", time.localtime(j["next_run"])) if j["next_run"] else "-"
            print(f"#{j['id']} {j['name']} [{j['kind']}] {j['schedule']} next {nxt} last {j['last_status']}" + ("" if j["enabled"] else " (disabled)"))


def cmd_daemon(args):
    """Runs due jobs. Prompt jobs run with approvals denied (unattended); results become notifications."""
    from .sched import Organizer
    o = Organizer(home_dir())

    def runner(prompt):
        ag = Agent.create(cfg=load_config(), approval="deny", private=args.private)
        out = ag.run(prompt, reflect=False)
        if ag.last_status != "completed":
            raise RuntimeError(f"task ended {ag.last_status}: {out[:120]}")
        return out[:500]
    print("courier daemon running (Ctrl-C to stop)")
    try:
        while True:
            for name, status in o.run_due(runner):
                print(time.strftime("%H:%M:%S"), name, status)
            for n in o.notifications(mark_read=True):
                print("🔔", n["when"], n["text"])
            if args.once:
                return
            time.sleep(30)
    except KeyboardInterrupt:
        pass


def cmd_notifications(args):
    from .sched import Organizer
    for n in Organizer(home_dir()).notifications(unread_only=not args.all, mark_read=True):
        print(n["when"], n["level"], n["text"])


def cmd_brief(args):
    from .sched import Organizer
    print(Organizer(home_dir()).brief())


def cmd_audit(args):
    for r in AuditLog(home_dir()).tail(args.n, args.kind):
        print(time.strftime("%m-%d %H:%M:%S", time.localtime(r["ts"])), r["kind"], r["name"], r["status"], r["args"][:80])


def cmd_usage(args):
    from .usage import Usage
    rows = Usage(home_dir()).summary(args.days)
    for r in rows or []:
        print(r)
    if not rows:
        print("no model calls recorded yet")


def cmd_privacy(args):
    """What is stored on this PC, and what has been sent to outside services."""
    from .usage import Usage
    home = home_dir()
    print(f"STORED LOCALLY under {home}:")
    for p in sorted(home.rglob("*")):
        if p.is_file():
            print(f"  {p.relative_to(home)}  ({p.stat().st_size} bytes)")
    print("\nSENT TO EXTERNAL SERVICES:")
    for r in Usage(home).summary(30):
        print(f"  model calls via {r['provider']} ({r['where']}): {r['calls']} in the last 30 days")
    hosts = {}
    for r in AuditLog(home).tail(10000, "net"):
        hosts[r["name"]] = hosts.get(r["name"], 0) + 1
    for h, n in sorted(hosts.items()):
        print(f"  web requests to {h}: {n}")
    print("\nAPI keys are read from environment variables and are not stored by Courier.")


def cmd_backup(args):
    home = home_dir()
    out = Path(args.file)
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
        for p in home.rglob("*"):
            if p.is_file() and p.name not in ("STOP",) and "checkpoints" not in p.parts:
                z.write(p, p.relative_to(home))
    print("backup written to", out)


def cmd_restore(args):
    home = home_dir()
    safety = home.with_name(home.name + ".before-restore")
    if home.exists():
        shutil.copytree(home, safety, dirs_exist_ok=True)
    with zipfile.ZipFile(args.file) as z:
        for n in z.namelist():
            if n.startswith("/") or ".." in Path(n).parts:
                raise SystemExit("unsafe path in backup")
        z.extractall(home)
    print(f"restored. Previous state kept at {safety}")


def cmd_init(args):
    from .skills import SkillStore
    from .skills_seed import SEEDS
    sk = SkillStore(home_dir())
    for n, body in SEEDS.items():
        print(n, "->", sk.create(n, body))


def _raw():
    from . import web
    return web.default_http_raw


def _unattended_agent(args=None):
    cfg = load_config()
    return Agent.create(cfg=cfg, approval="deny", on_event=lambda *a: None, private=False)


def cmd_telegram(args):
    from . import gateway
    tok = os.environ.get("TELEGRAM_BOT_TOKEN")
    if not tok:
        print("Set TELEGRAM_BOT_TOKEN (create a bot with @BotFather)."); return 1
    allowed = load_config().get("telegram_allowed_chats", [])
    if not allowed:
        print("Add your chat id(s) to telegram_allowed_chats in config.json first. Message the bot once; it will reply with your chat id."); 
    from . import web
    print("Telegram gateway running (approvals are denied; Ctrl-C to stop). Allowed chats:", allowed or "none")
    try:
        gateway.TelegramGateway(tok, web.default_http, _unattended_agent, allowed).run_forever()
    except KeyboardInterrupt:
        print("stopped")


def cmd_update(args):
    from . import updater, web, __version__
    cfg, pkg = load_config(), Path(__file__).parent
    try:
        if args.action == "rollback":
            print("restored", updater.rollback(home_dir(), pkg)); return 0
        rel = updater.check(cfg.get("update_repo", ""), __version__, web.default_http, os.environ.get("GITHUB_TOKEN"))
        if not rel:
            print(f"up to date ({__version__})"); return 0
        print(f"new version {rel['version']} available (you have {__version__})\n{rel['notes']}")
        if args.action == "check":
            print("run `courier update apply` to install (a rollback copy is kept)"); return 0
        data, digest, verified = updater.download_verified(rel, web.default_http_raw, args.allow_unverified)
        print("checksum verified" if verified else "WARNING: installing WITHOUT checksum verification")
        if input("install now? [y/N] ").strip().lower() != "y":
            print("cancelled"); return 0
        print("installed; rollback copy:", updater.install(data, pkg, home_dir(), __version__, rel["version"]))
        print("restart courier to use the new version; `courier update rollback` undoes it")
    except updater.UpdateError as e:
        print("update:", e); return 1


def cmd_workflow(args):
    from .workflows import WorkflowError, WorkflowStore
    store = WorkflowStore(home_dir())
    if args.action == "list":
        for w in store.list():
            print(f"{w['name']}  ({w['steps']} steps)  {w['description']}")
        return 0
    if args.action == "show":
        print(json.dumps(store.load(args.name), indent=1)); return 0
    if args.action == "delete":
        store.delete(args.name); print("deleted"); return 0
    variables = dict(kv.split("=", 1) for kv in args.var)
    agent = make_agent(args)
    from .tools import dispatch
    try:
        res = store.run(args.name, variables, lambda t, a: dispatch(agent.tools, t, a) if t in agent.tools else f"denied: tool {t} not available",
                        agent.ctx.stop, args.resume)
    except WorkflowError as e:
        print("workflow:", e); return 1
    print(json.dumps(res, indent=1))
    return 0 if res["status"] == "completed" else 2


def cmd_sync(args):
    from . import integrations as ig
    be = ig.make_backend(args.provider, _raw())
    dev = args.device or os.uname().nodename
    if args.action == "push":
        print(ig.sync_push(be, home_dir(), dev))
    else:
        r = ig.sync_pull(be, home_dir(), dev, False)
        print(r)
        if input("overwrite local Courier data with this snapshot (a safety copy is kept)? [y/N] ").strip().lower() == "y":
            print(ig.sync_pull(be, home_dir(), dev, True))


def cmd_security_scan(args):
    from . import secscan
    res = secscan.scan(Path(args.path))
    print(res["counts"], "in", res["scanned_files"], "files")
    for f in res["findings"][:30]:
        print(f"[{f['severity']}] {f['file']}:{f['line']} {f['rule']} - {f['message']}")
    if args.report:
        Path(args.report).write_text(secscan.report_markdown(res, args.path)); print("report:", args.report)


def cmd_cache(args):
    from .cache import ResponseCache
    print(f"cleared {ResponseCache(home_dir()).clear()} cached answers")


def cmd_ui(args):
    from . import ui
    ui.run(lambda approve: make_agent(args, approve))


def cmd_voice(args):
    from . import voice
    agent = make_agent(args)
    if args.wake:
        print("Wake-word mode (untested on real hardware). Say the wake word, then speak. Ctrl-C quits.")
        try:
            while True:
                voice.wake_listen(args.wake)
                heard = voice.listen(language=args.language)
                print("heard:", heard)
                reply = _guarded_run(agent, heard, args)
                print(reply)
                if not args.silent:
                    voice.speak(reply[:600])
        except voice.VoiceUnavailable as e:
            print("voice unavailable:", e); return 1
        except KeyboardInterrupt:
            return 0
    print("Push-to-talk: press Enter, speak for 6 seconds. Type q to quit. (voice support is untested; see voice.py)")
    while input("\n[Enter to talk] ").strip().lower() != "q":
        try:
            heard = voice.listen(language=args.language)
        except voice.VoiceUnavailable as e:
            print("voice unavailable:", e); return 1
        fix = input(f"heard: {heard!r}\n  Enter to accept, or type the correction: ").strip()
        reply = _guarded_run(agent, fix or heard, args)
        print("\n" + reply)
        if not args.silent:
            try:
                voice.speak(reply[:600])
            except voice.VoiceUnavailable as e:
                print("(speech output unavailable:", e, ")")


def main(argv=None):
    ap = argparse.ArgumentParser(prog="courier")
    sub = ap.add_subparsers(dest="cmd", required=True)

    def agent_flags(s):
        s.add_argument("--yes", action="store_true", help="auto-approve shell commands and overwrites")
        s.add_argument("--space", help="project memory space")
        s.add_argument("--private", action="store_true", help="local model only; web/outbound tools off")
        s.add_argument("--safe", action="store_true", help="safe mode: memory, skills and web only")
        s.add_argument("--style", choices=["concise", "detailed"])
        s.add_argument("--stream", action="store_true", help="print the answer as it is written")
        s.add_argument("--plan", action="store_true"); s.add_argument("--verify", action="store_true")
    s = sub.add_parser("chat"); agent_flags(s); s.set_defaults(fn=cmd_chat)
    s = sub.add_parser("run"); agent_flags(s); s.add_argument("prompt", nargs="+"); s.set_defaults(fn=cmd_run)
    s = sub.add_parser("ui"); agent_flags(s); s.set_defaults(fn=cmd_ui)
    s = sub.add_parser("voice"); agent_flags(s); s.add_argument("--language"); s.add_argument("--silent", action="store_true"); s.add_argument("--wake", nargs="?", const="hey_jarvis"); s.set_defaults(fn=cmd_voice)
    s = sub.add_parser("resume"); s.add_argument("session"); s.add_argument("--yes", action="store_true"); s.set_defaults(fn=cmd_resume)
    sub.add_parser("stop").set_defaults(fn=cmd_stop)
    s = sub.add_parser("doctor"); s.add_argument("--live", action="store_true"); s.set_defaults(fn=cmd_doctor)
    sub.add_parser("selftest").set_defaults(fn=cmd_selftest)
    s = sub.add_parser("models"); s.add_argument("provider", choices=["nvidia", "openrouter", "ollama"]); s.add_argument("--free", action="store_true"); s.set_defaults(fn=cmd_models)
    s = sub.add_parser("memory"); s.add_argument("action", choices=["show", "export", "edit", "delete", "pause", "resume"], nargs="?", default="show")
    s.add_argument("arg", nargs="*"); s.add_argument("--space"); s.set_defaults(fn=cmd_memory)
    s = sub.add_parser("sessions"); s.add_argument("query", nargs="+"); s.set_defaults(fn=cmd_sessions)
    s = sub.add_parser("kb"); s.add_argument("action", choices=["ingest", "search"]); s.add_argument("arg", nargs="+"); s.add_argument("--space"); s.set_defaults(fn=cmd_kb)
    s = sub.add_parser("todo"); s.add_argument("action", choices=["add", "list", "done"], nargs="?", default="list"); s.add_argument("arg", nargs="*")
    s.add_argument("--priority", default="med"); s.add_argument("--due"); s.add_argument("--project", default=""); s.add_argument("--recurring"); s.set_defaults(fn=cmd_todo)
    s = sub.add_parser("jobs"); s.add_argument("action", choices=["add", "list", "remove"], nargs="?", default="list"); s.add_argument("arg", nargs="*"); s.set_defaults(fn=cmd_jobs)
    s = sub.add_parser("daemon"); s.add_argument("--once", action="store_true"); s.add_argument("--private", action="store_true"); s.set_defaults(fn=cmd_daemon)
    s = sub.add_parser("notifications"); s.add_argument("--all", action="store_true"); s.set_defaults(fn=cmd_notifications)
    sub.add_parser("brief").set_defaults(fn=cmd_brief)
    s = sub.add_parser("audit"); s.add_argument("-n", type=int, default=20); s.add_argument("--kind"); s.set_defaults(fn=cmd_audit)
    s = sub.add_parser("usage"); s.add_argument("--days", type=int, default=1); s.set_defaults(fn=cmd_usage)
    sub.add_parser("privacy").set_defaults(fn=cmd_privacy)
    s = sub.add_parser("backup"); s.add_argument("file"); s.set_defaults(fn=cmd_backup)
    s = sub.add_parser("restore"); s.add_argument("file"); s.set_defaults(fn=cmd_restore)
    sub.add_parser("init").set_defaults(fn=cmd_init)
    sub.add_parser("telegram").set_defaults(fn=cmd_telegram)
    s = sub.add_parser("update"); s.add_argument("action", choices=["check", "apply", "rollback"], nargs="?", default="check")
    s.add_argument("--allow-unverified", action="store_true"); s.set_defaults(fn=cmd_update)
    s = sub.add_parser("workflow"); s.add_argument("action", choices=["list", "show", "run", "delete"]); s.add_argument("name", nargs="?")
    s.add_argument("--var", action="append", default=[], help="name=value"); s.add_argument("--resume"); agent_flags(s); s.set_defaults(fn=cmd_workflow)
    s = sub.add_parser("sync"); s.add_argument("action", choices=["push", "pull"]); s.add_argument("provider", choices=["dropbox", "webdav"]); s.add_argument("--device"); s.set_defaults(fn=cmd_sync)
    s = sub.add_parser("security-scan"); s.add_argument("path", nargs="?", default="."); s.add_argument("--report"); s.set_defaults(fn=cmd_security_scan)
    sub.add_parser("cache").set_defaults(fn=cmd_cache)
    args = ap.parse_args(argv)
    return args.fn(args) or 0


if __name__ == "__main__":
    sys.exit(main())
