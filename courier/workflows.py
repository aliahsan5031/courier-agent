"""Saved, reviewable workflows: an ordered list of tool calls with variables, retries and resume.

A workflow is data, not code: each step names an existing tool and its arguments. Approvals, allowlists and
the emergency stop still apply to every step because steps run through the normal tool dispatcher.
"""
from __future__ import annotations

import json
import re
import time
import uuid
from pathlib import Path

NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{1,40}$")
FORBIDDEN = {"delegate_task", "workflow_run", "workflow_save", "memory", "skill_manage"}  # no recursion or self-modification
FAIL_PREFIX = ("error", "denied", "blocked")
VAR = re.compile(r"\{\{\s*([\w.]+)\s*\}\}")


class WorkflowError(Exception):
    pass


class WorkflowStore:
    def __init__(self, home: Path):
        self.dir = Path(home) / "workflows"
        (self.dir / "runs").mkdir(parents=True, exist_ok=True)

    def _f(self, name):
        if not NAME_RE.match(name or ""):
            raise WorkflowError("workflow name: lowercase letters, digits, - and _ (2-41 chars)")
        return self.dir / f"{name}.json"

    def save(self, name, steps, description="", known_tools=()):
        if not isinstance(steps, list) or not steps:
            raise WorkflowError("steps must be a non-empty list")
        if len(steps) > 25:
            raise WorkflowError("at most 25 steps")
        seen = set()
        for i, s in enumerate(steps, 1):
            if s.get("tool") in FORBIDDEN:
                raise WorkflowError(f"step {i}: tool {s['tool']!r} cannot be used inside a workflow")
            if known_tools and s.get("tool") not in known_tools:
                raise WorkflowError(f"step {i}: unknown tool {s.get('tool')!r}")
            if s.get("on_fail", "stop") not in ("stop", "continue", "retry"):
                raise WorkflowError(f"step {i}: on_fail must be stop|continue|retry")
            if int(s.get("retries", 0)) > 3:
                raise WorkflowError(f"step {i}: retries max is 3")
            for ref in VAR.findall(json.dumps(s.get("args", {}))):
                if ref.startswith("steps.") and ref.split(".", 1)[1] not in seen:
                    raise WorkflowError(f"step {i}: refers to {ref} before it is produced")
            if s.get("save_as"):
                seen.add(s["save_as"])
        self._f(name).write_text(json.dumps({"name": name, "description": description, "steps": steps}, indent=1))
        return f"saved workflow {name} ({len(steps)} steps)"

    def load(self, name):
        f = self._f(name)
        if not f.exists():
            raise WorkflowError(f"no workflow named {name!r}")
        return json.loads(f.read_text())

    def list(self):
        out = []
        for f in sorted(self.dir.glob("*.json")):
            d = json.loads(f.read_text())
            out.append({"name": d["name"], "steps": len(d["steps"]), "description": d.get("description", "")})
        return out

    def delete(self, name):
        f = self._f(name)
        f.unlink(missing_ok=True)

    # ---- running
    def _subst(self, obj, ctx):
        if isinstance(obj, str):
            def rep(m):
                key = m.group(1)
                if key not in ctx:
                    raise WorkflowError(f"missing variable {key!r}")
                return str(ctx[key])
            return VAR.sub(rep, obj)
        if isinstance(obj, list):
            return [self._subst(x, ctx) for x in obj]
        if isinstance(obj, dict):
            return {k: self._subst(v, ctx) for k, v in obj.items()}
        return obj

    def run(self, name, variables, dispatch_fn, stop=None, run_id=None, audit=None, sleep=time.sleep):
        """dispatch_fn(tool, args) -> output string. Saves state after every step so `run_id` can resume."""
        wf = self.load(name)
        state_f = self.dir / "runs" / f"{run_id}.json" if run_id else None
        if state_f and state_f.exists():
            st = json.loads(state_f.read_text())
            if st["workflow"] != name:
                raise WorkflowError("run id belongs to a different workflow")
        else:
            run_id = run_id or uuid.uuid4().hex[:10]
            state_f = self.dir / "runs" / f"{run_id}.json"
            st = {"workflow": name, "vars": variables or {}, "results": [], "outputs": {}, "next": 0, "status": "running"}
        needed = {r for s in wf["steps"] for r in VAR.findall(json.dumps(s.get("args", {}))) if not r.startswith("steps.")}
        missing = needed - set(st["vars"])
        if missing:
            raise WorkflowError(f"missing variables: {', '.join(sorted(missing))}")
        ctx = {**st["vars"], **{f"steps.{k}": v for k, v in st["outputs"].items()}}
        for i in range(st["next"], len(wf["steps"])):
            step = wf["steps"][i]
            if stop and stop.is_set():
                st["status"] = "stopped"
                break
            args = self._subst(step.get("args", {}), ctx)
            attempts = 1 + (int(step.get("retries", 0)) if step.get("on_fail") == "retry" else 0)
            out = ""
            for a in range(attempts):
                out = dispatch_fn(step["tool"], args)
                failed = out.startswith(FAIL_PREFIX)
                if audit:
                    audit("workflow", f"{name}#{i + 1}:{step['tool']}", "error" if failed else "ok", out)
                if not failed or out.startswith("denied") or a == attempts - 1:
                    break
                sleep(min(2 ** a, 4))
            failed = out.startswith(FAIL_PREFIX)
            st["results"] = [r for r in st["results"] if r["step"] != i + 1]  # a resumed step replaces its earlier failure
            st["results"].append({"step": i + 1, "tool": step["tool"], "status": "failed" if failed else "ok", "output": out[:300]})
            if step.get("save_as") and not failed:
                st["outputs"][step["save_as"]] = out
                ctx[f"steps.{step['save_as']}"] = out
            if failed and step.get("on_fail", "stop") != "continue":
                st["status"] = "failed"  # `next` stays on this step so resume retries it
                state_f.write_text(json.dumps(st))
                break
            st["next"] = i + 1
            state_f.write_text(json.dumps(st))
        else:
            st["status"] = "completed" if all(r["status"] == "ok" for r in st["results"]) else "completed_with_errors"
        state_f.write_text(json.dumps(st))
        return {"run_id": run_id, "status": st["status"], "steps": st["results"]}
