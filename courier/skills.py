"""Procedural memory: SKILL.md files the agent writes and reloads on demand.

Progressive disclosure: only name + description go in the system prompt;
the full body is loaded with skill_view when relevant.
"""
from __future__ import annotations

import re
from pathlib import Path

from .memory import scan

NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]{1,48}$")
MAX_BYTES = 20_000


def _parse(text):
    meta, body = {}, text
    if text.startswith("---"):
        end = text.find("\n---", 3)
        if end != -1:
            for line in text[3:end].strip().splitlines():
                if ":" in line:
                    k, v = line.split(":", 1)
                    meta[k.strip()] = v.strip()
            body = text[end + 4:].lstrip("\n")
    return meta, body


class SkillStore:
    def __init__(self, home: Path):
        self.dir = Path(home) / "skills"
        self.dir.mkdir(parents=True, exist_ok=True)

    def _file(self, name):
        if not NAME_RE.match(name or ""):
            raise ValueError("skill name: lowercase letters, digits, hyphens (2-49 chars)")
        return self.dir / name / "SKILL.md"

    def index(self) -> list[dict]:
        out = []
        for f in sorted(self.dir.glob("*/SKILL.md")):
            meta, _ = _parse(f.read_text())
            out.append({"name": meta.get("name", f.parent.name),
                        "description": meta.get("description", "")})
        return out

    def view(self, name) -> str:
        f = self._file(name)
        return f.read_text() if f.exists() else f"error: no skill {name!r}"

    def create(self, name, content) -> str:
        f = self._file(name)
        if f.exists():
            return "error: exists; use patch"
        meta, body = _parse(content)
        if not meta.get("description") or not body.strip():
            return "error: need frontmatter with description, plus a body"
        if len(content.encode()) > MAX_BYTES:
            return "error: too large"
        bad = scan(content)
        if bad:
            return "error: " + bad
        if "name" not in meta:
            content = content.replace("---", f"---\nname: {name}", 1)
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(content)
        return f"created skill {name}"

    def patch(self, name, old, new) -> str:
        f = self._file(name)
        if not f.exists():
            return f"error: no skill {name!r}"
        text = f.read_text()
        if text.count(old) != 1:
            return f"error: old text found {text.count(old)} times; need exactly 1"
        bad = scan(new)
        if bad:
            return "error: " + bad
        f.write_text(text.replace(old, new))
        return "patched"

    def delete(self, name) -> str:
        f = self._file(name)
        if not f.exists():
            return "error: not found"
        f.unlink()
        f.parent.rmdir()
        return "deleted"
