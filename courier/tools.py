"""Tool registry, toolsets and built-in tools."""
from __future__ import annotations

import json
import re
import subprocess
import sys
import tempfile
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

MAX_OUT = 8000

SAFE_CMD = re.compile(r"^\s*(ls|cat|pwd|echo|grep|rg|find|head|tail|wc|date|which|git (status|log|diff|show)|python3? -V)\b")
DANGEROUS = re.compile(r"(rm\s+-rf\s+(/|~)|mkfs|dd\s+if=|:\(\)\s*\{|>\s*/dev/sd|shutdown|reboot|chmod\s+-R\s+777\s+/)")


@dataclass
class Tool:
    name: str
    toolset: str
    description: str
    parameters: dict
    fn: Callable[[dict], str]

    def schema(self):
        return {"type": "function", "function": {
            "name": self.name, "description": self.description, "parameters": self.parameters}}


@dataclass
class Ctx:
    home: Path
    workdir: Path
    memory: object
    store: object
    skills: object
    session_id: str
    router: object = None
    cfg: dict = field(default_factory=dict)
    approval: str = "ask"  # ask | auto | deny
    approve_cb: Callable[[str], bool] | None = None
    depth: int = 0
    audit: object = None
    stop: object = None
    organizer: object = None
    kb: object = None
    usage: object = None
    http: object = None  # injectable web transport (tests)
    resolver: object = None  # injectable DNS resolver (tests)
    private: bool = False
    space: str = "default"
    extras: dict = field(default_factory=dict)  # injectables (tests) and per-run state


def _obj(props, required=()):
    return {"type": "object", "properties": props, "required": list(required)}


def _s(desc, **kw):
    return {"type": "string", "description": desc, **kw}


def _safe_path(ctx: Ctx, p: str) -> Path:
    full = (ctx.workdir / p).resolve()
    if ctx.workdir.resolve() not in (full, *full.parents):
        raise PermissionError(f"path outside workspace: {p}")
    return full


def clip(s: str, n=MAX_OUT) -> str:
    return s if len(s) <= n else s[: n // 2] + f"\n…[{len(s) - n} chars truncated]…\n" + s[-n // 2:]


def build_tools(ctx: Ctx) -> dict[str, Tool]:
    T: dict[str, Tool] = {}

    def reg(name, toolset, desc, params, fn):
        T[name] = Tool(name, toolset, desc, params, fn)

    # ---- memory -------------------------------------------------------
    def memory_fn(a):
        t, act = a.get("target", "memory"), a.get("action")
        if act == "add":
            return ctx.memory.add(t, a.get("text", ""), source=a.get("source", "chat"),
                                  confidence=a.get("confidence", "medium"), expires_days=a.get("expires_days"))
        if act == "replace":
            return ctx.memory.replace(t, a.get("match", ""), a.get("text", ""))
        if act == "remove":
            return ctx.memory.remove(t, a.get("match", ""))
        if act == "show":
            return ctx.memory.show(t)
        return "error: action must be add|replace|remove|show"

    reg("memory", "memory",
        "Curate persistent memory. target 'memory' = your notes (environment, conventions, lessons); "
        "'user' = the user's profile and preferences. Bounded: keep entries short, merge duplicates. "
        "Changes appear in future sessions.",
        _obj({"action": _s("add|replace|remove|show", enum=["add", "replace", "remove", "show"]),
              "target": _s("memory|user", enum=["memory", "user"]),
              "text": _s("entry text (add/replace)"),
              "match": _s("substring identifying the one entry to replace/remove"),
              "source": _s("where the fact came from, e.g. 'user said'"),
              "confidence": _s("low|medium|high", enum=["low", "medium", "high"]),
              "expires_days": {"type": "integer", "description": "auto-forget after N days (temporary facts)"}},
             ["action"]), memory_fn)

    def search_fn(a):
        rows = ctx.store.search(a.get("query", ""), int(a.get("limit", 5)), exclude_session=ctx.session_id)
        return json.dumps(rows, indent=1) if rows else "no matches in past sessions"

    reg("session_search", "memory", "Full-text search over all past conversations.",
        _obj({"query": _s("keywords"), "limit": {"type": "integer"}}, ["query"]), search_fn)

    # ---- skills -------------------------------------------------------
    reg("skill_view", "skills", "Load the full text of a skill listed in the skills index.",
        _obj({"name": _s("skill name")}, ["name"]), lambda a: ctx.skills.view(a["name"]))

    def skill_manage(a):
        act, n = a.get("action"), a.get("name", "")
        try:
            if act == "create":
                return ctx.skills.create(n, a.get("content", ""))
            if act == "patch":
                return ctx.skills.patch(n, a.get("old", ""), a.get("new", ""))
            if act == "delete":
                return ctx.skills.delete(n)
        except ValueError as e:
            return f"error: {e}"
        return "error: action must be create|patch|delete"

    reg("skill_manage", "skills",
        "Create/patch/delete reusable skills (SKILL.md with frontmatter name+description, then steps). "
        "Create one after solving a non-trivial task you may meet again; prefer patch over rewriting.",
        _obj({"action": _s("create|patch|delete", enum=["create", "patch", "delete"]),
              "name": _s("kebab-case name"), "content": _s("full SKILL.md (create)"),
              "old": _s("exact text to replace (patch)"), "new": _s("replacement (patch)")},
             ["action", "name"]), skill_manage)

    # ---- files --------------------------------------------------------
    def read_file(a):
        return clip(_safe_path(ctx, a["path"]).read_text(errors="replace"))

    def confirm_overwrite(p, label):
        """Overwriting an existing file is destructive: needs approval unless the policy is auto."""
        if p.exists() and ctx.approval != "auto":
            if not (ctx.approval == "ask" and ctx.approve_cb and ctx.approve_cb(f"overwrite existing file {label}")):
                return "denied: file exists; overwriting needs approval"
        return None

    def write_file(a):
        p = _safe_path(ctx, a["path"])
        denied = confirm_overwrite(p, a["path"])
        if denied:
            return denied
        p.parent.mkdir(parents=True, exist_ok=True)
        content = a.get("content", "")
        p.write_text(content)
        if p.read_text() != content:  # verify before claiming success
            return f"error: wrote {a['path']} but read-back does not match"
        return f"wrote and verified {len(content)} chars in {a['path']}"

    def list_dir(a):
        p = _safe_path(ctx, a.get("path", "."))
        return "\n".join(sorted(x.name + ("/" if x.is_dir() else "") for x in p.iterdir())) or "(empty)"

    reg("read_file", "files", "Read a text file in the workspace.", _obj({"path": _s("relative path")}, ["path"]), read_file)
    reg("write_file", "files", "Write (overwrite) a text file in the workspace.",
        _obj({"path": _s("relative path"), "content": _s("file content")}, ["path", "content"]), write_file)
    reg("list_dir", "files", "List a workspace directory.", _obj({"path": _s("relative path, default .")}), list_dir)

    # ---- terminal / code ---------------------------------------------
    def allowed(cmd):
        if DANGEROUS.search(cmd):
            return False, "blocked: dangerous command"
        if ctx.approval == "auto" or SAFE_CMD.match(cmd):
            return True, ""
        if ctx.approval == "ask" and ctx.approve_cb and ctx.approve_cb(cmd):
            return True, ""
        return False, "denied: command needs approval (run with --yes or approve interactively)"

    def terminal(a):
        cmd = a["command"]
        ok, why = allowed(cmd)
        if not ok:
            return why
        try:
            r = subprocess.run(cmd, shell=True, cwd=ctx.workdir, capture_output=True, text=True,
                               timeout=int(a.get("timeout", 60)))
        except subprocess.TimeoutExpired:
            return "error: timed out"
        return clip(f"exit={r.returncode}\n{r.stdout}{('[stderr]' + r.stderr) if r.stderr else ''}")

    reg("terminal", "terminal", "Run a shell command in the workspace (approval policy applies).",
        _obj({"command": _s("shell command"), "timeout": {"type": "integer"}}, ["command"]), terminal)

    def execute_code(a):
        code = a["code"]
        ok, why = allowed("python3 <code>")
        if not ok:
            return why
        with tempfile.NamedTemporaryFile("w", suffix=".py", delete=False, dir=tempfile.gettempdir()) as f:
            f.write(code)
        try:
            r = subprocess.run([sys.executable, "-I", f.name], cwd=ctx.workdir, capture_output=True,
                               text=True, timeout=int(a.get("timeout", 60)))
        except subprocess.TimeoutExpired:
            return "error: timed out"
        finally:
            Path(f.name).unlink(missing_ok=True)
        return clip(f"exit={r.returncode}\n{r.stdout}{('[stderr]' + r.stderr) if r.stderr else ''}")

    reg("execute_code", "code", "Run a Python script (isolated mode) to compute or transform data in one step.",
        _obj({"code": _s("python source"), "timeout": {"type": "integer"}}, ["code"]), execute_code)

    # ---- delegation ---------------------------------------------------
    def delegate_task(a):
        from .agent import Agent  # lazy: avoids import cycle
        dcfg = ctx.cfg.get("delegation", {})
        if ctx.depth >= dcfg.get("max_depth", 1):
            return "error: subagents cannot delegate further"
        tasks = a.get("tasks") or ([{"goal": a["goal"], "context": a.get("context", "")}] if a.get("goal") else [])
        if not tasks:
            return "error: provide goal or tasks"
        tasks = tasks[: dcfg.get("max_tasks", 8)]
        child_sets = dcfg.get("child_toolsets", ["files", "terminal", "code"])
        roles = ctx.cfg.get("roles", {})
        bad = [t.get("role") for t in tasks if t.get("role") and t["role"] not in roles]
        if bad:
            return f"error: unknown role {bad[0]!r}. Roles: {', '.join(roles)}"

        def run(t):
            role = roles.get(t.get("role"), {})
            want = role.get("toolsets") or t.get("toolsets") or child_sets
            sets = [s for s in want if s in child_sets] or child_sets
            child = Agent.child(ctx, toolsets=sets, max_iters=dcfg.get("child_max_iters", 15),
                                role_prompt=role.get("prompt", ""))
            prompt = f"Goal: {t['goal']}\n\nContext from parent:\n{t.get('context', '(none)')}\n\n" \
                     "Work independently. End with a concise report of results and any files you changed."
            try:
                return {"goal": t["goal"], "status": "ok", "result": child.run(prompt)}
            except Exception as e:  # keep siblings alive
                return {"goal": t["goal"], "status": "error", "result": str(e)}

        with ThreadPoolExecutor(max_workers=dcfg.get("max_concurrent", 3)) as ex:
            results = list(ex.map(run, tasks))
        return clip(json.dumps(results, indent=1))

    reg("delegate_task", "delegation",
        "Spawn isolated subagents (fresh context, restricted tools) to work on independent subtasks in "
        "parallel. Give each a self-contained goal and the context it needs; only their final reports return.",
        _obj({"tasks": {"type": "array", "items": _obj(
            {"goal": _s("self-contained goal"), "context": _s("facts the child needs"),
             "role": _s("optional specialist: researcher|coder|writer|verifier"),
             "toolsets": {"type": "array", "items": {"type": "string"}}}, ["goal"])},
              "goal": _s("single-task shortcut"), "context": _s("context for the shortcut")}), delegate_task)
    from .tools_ext import add_ext  # lazy: avoids import cycle
    add_ext(ctx, reg, _obj, _s, _safe_path, confirm_overwrite, allowed)
    from .tools_more import add_more
    add_more(ctx, reg, T, _obj, _s, _safe_path, confirm_overwrite, allowed)
    return T


DEFAULT_TOOLSETS = ["memory", "skills", "files", "terminal", "code", "delegation",
                    "web", "docs", "organizer", "system", "vision", "net",
                    "integrations", "media", "dev", "workflow", "speech"]
DESKTOP_TOOLSETS = ["desktop"]  # added only when config desktop_enabled is true
SAFE_TOOLSETS = ["memory", "skills", "web"]  # safe mode: no files, shell, code or outbound actions


def select(tools: dict[str, Tool], toolsets):
    return {n: t for n, t in tools.items() if t.toolset in toolsets}


def dispatch(tools: dict[str, Tool], name: str, args: dict) -> str:
    t = tools.get(name)
    if not t:
        return f"error: unknown tool {name!r}. Available: {', '.join(tools)}"
    if "_parse_error" in args:
        return "error: arguments were not valid JSON; resend the call"
    try:
        return clip(str(t.fn(args)))
    except KeyError as e:
        return f"error: missing argument {e}"
    except Exception as e:
        return f"error: {type(e).__name__}: {e}"
