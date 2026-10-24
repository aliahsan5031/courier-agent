import json
import os
import shutil
from pathlib import Path

DEFAULTS = {
    "route": ["nvidia", "openrouter"],
    "nvidia": {"base_url": "https://integrate.api.nvidia.com/v1", "model": "meta/llama-3.3-70b-instruct",
               "light_model": "meta/llama-3.1-8b-instruct", "vision_model": "meta/llama-3.2-90b-vision-instruct"},
    "openrouter": {"base_url": "https://openrouter.ai/api/v1", "model": "meta-llama/llama-3.3-70b-instruct:free",
                   "vision_model": "meta-llama/llama-3.2-11b-vision-instruct:free"},
    "ollama": {"base_url": "http://localhost:11434/v1", "model": "qwen2.5:7b-instruct"},
    "style": "concise",  # concise | detailed
    "max_iterations": 25,
    "reflect_after_tool_calls": 5,
    "memory_limits": {"memory": 2200, "user": 1375},
    "apps_allowlist": {},  # e.g. {"notepad": ["notepad.exe"], "calculator": ["calc.exe"]}
    "http_allowlist": [],  # hosts http_request may call, e.g. ["localhost", "n8n.example.com"]
    "delegation": {"max_concurrent": 3, "max_tasks": 8, "max_depth": 1, "child_max_iters": 15,
                   "child_toolsets": ["files", "terminal", "code", "web", "docs", "media", "dev"]},
    "toolsets": None,  # None = all default toolsets
    "desktop_enabled": False, "desktop_max_actions": 50,
    "db_allowlist": [], "telegram_allowed_chats": [], "public_data_hosts": [],
    "update_repo": "", "todoist_base": "https://api.todoist.com/rest/v2",
    "embeddings": {"provider": None},
    "cache": {"enabled": False, "ttl_hours": 24},
    "roles": {
        "workflow": {"toolsets": ["workflow", "files", "docs", "web"],
                     "prompt": "You design reusable workflows: small, ordered tool-call lists with variables. Save with workflow_save, show the steps to the user, test with workflow_run, and report results honestly."},
        "researcher": {"toolsets": ["web", "docs", "files"],
                       "prompt": "You are a research specialist. Gather evidence from several sources, record URL and date for every claim, flag conflicts and uncertainty, never invent sources."},
        "coder": {"toolsets": ["files", "terminal", "code"],
                  "prompt": "You are a coding specialist. Make small changes, run tests, and report the exact commands and results. Do not claim success without evidence."},
        "writer": {"toolsets": ["docs", "files"],
                   "prompt": "You are a document specialist. Produce the requested file, then read it back to verify it exists and has the right content."},
        "verifier": {"toolsets": ["files", "docs", "code"],
                     "prompt": "You are a verification specialist. Check the claimed results against the actual files and data. Report PASS or FAIL per requirement with evidence. Do not modify anything."},
    },
}


def home_dir() -> Path:
    return Path(os.environ.get("COURIER_HOME", Path.home() / ".courier"))


def load_config() -> dict:
    """Load config.json over defaults. A corrupt file falls back to the last good copy (config.json.bak)."""
    cfg = json.loads(json.dumps(DEFAULTS))
    p = home_dir() / "config.json"
    user = {}
    if p.exists():
        try:
            user = json.loads(p.read_text())
            bak = p.with_suffix(".json.bak")
            if not bak.exists() or bak.read_text() != p.read_text():
                shutil.copyfile(p, bak)
        except ValueError:
            bak = p.with_suffix(".json.bak")
            cfg["_config_warning"] = "config.json is corrupt; " + ("restored from backup" if bak.exists() else "using defaults")
            user = json.loads(bak.read_text()) if bak.exists() else {}
    for k, v in user.items():
        if isinstance(v, dict) and isinstance(cfg.get(k), dict):
            cfg[k].update(v)
        else:
            cfg[k] = v
    return cfg


def space_home(home: Path, space: str | None) -> Path:
    """Separate memory + knowledge base per project/context. 'default' keeps the original location."""
    if not space or space == "default":
        return Path(home)
    safe = "".join(c for c in space if c.isalnum() or c in "-_")[:40]
    if not safe:
        raise ValueError("space name: letters, digits, - and _")
    d = Path(home) / "spaces" / safe
    d.mkdir(parents=True, exist_ok=True)
    return d
