"""Pattern-based security review of a code folder. A fast first pass, NOT a replacement for tools like
bandit, semgrep or a dependency audit: it misses a lot and flags some harmless code."""
from __future__ import annotations

import re
import time
from pathlib import Path

SKIP_DIRS = {".git", "node_modules", "venv", ".venv", "__pycache__", "dist", "build", ".tox"}
EXTS = {".py", ".js", ".ts", ".jsx", ".tsx", ".php", ".rb", ".go", ".java", ".sh", ".yml", ".yaml", ".json", ".env", ".ini", ".toml", ".cfg", ".txt", ".html"}

# (id, severity, regex, extensions or None, message, fix)
RULES = [
    ("SECRET-KEY", "high", r"(AKIA[0-9A-Z]{16}|ghp_[A-Za-z0-9]{36}|sk-[A-Za-z0-9]{20,}|nvapi-[A-Za-z0-9_\-]{20,}|xox[bp]-[A-Za-z0-9\-]{20,})", None,
     "Looks like a real API key or token in source", "Revoke it, move it to an environment variable or secret store."),
    ("SECRET-PRIVKEY", "high", r"-----BEGIN (RSA |EC |OPENSSH |DSA )?PRIVATE KEY-----", None, "Private key committed to the repo", "Remove and rotate the key."),
    ("SECRET-ASSIGN", "medium", r"(?i)\b(password|passwd|secret|api_?key|token)\b\s*[:=]\s*['\"][^'\"\s]{8,}['\"]", {".py", ".js", ".ts", ".php", ".rb", ".go", ".java", ".yml", ".yaml", ".ini", ".toml", ".cfg", ".env"},
     "Hard-coded credential-like value", "Read it from the environment instead."),
    ("PY-EVAL", "high", r"\b(eval|exec)\s*\(", {".py"}, "eval/exec on dynamic data can run attacker code", "Use ast.literal_eval or an explicit dispatch table."),
    ("PY-SHELL", "high", r"subprocess\.\w+\([^)]*shell\s*=\s*True|os\.system\s*\(", {".py"}, "Shell command execution (injection risk if input is untrusted)", "Pass an argument list and shell=False."),
    ("PY-PICKLE", "high", r"\bpickle\.loads?\s*\(|\bmarshal\.loads?\s*\(", {".py"}, "Unpickling untrusted data executes code", "Use JSON or a safe format."),
    ("PY-YAML", "medium", r"yaml\.load\s*\((?![^)]*Loader\s*=\s*(yaml\.)?SafeLoader)", {".py"}, "yaml.load without SafeLoader", "Use yaml.safe_load."),
    ("PY-HASH", "low", r"hashlib\.(md5|sha1)\s*\(", {".py"}, "Weak hash (fine for checksums, not for passwords/signatures)", "Use sha256+ or bcrypt/argon2 for passwords."),
    ("TLS-OFF", "high", r"verify\s*=\s*False|rejectUnauthorized\s*:\s*false|CURLOPT_SSL_VERIFYPEER\s*,\s*(0|false)", None, "TLS certificate verification disabled", "Remove it; fix the certificate instead."),
    ("DEBUG-ON", "medium", r"(?i)\bdebug\s*=\s*True\b|app\.run\([^)]*debug\s*=\s*True", {".py"}, "Debug mode enabled", "Disable in production."),
    ("SQL-CONCAT", "high", r"(?i)\.execute\(\s*(f['\"]|['\"][^'\"]*(select|insert|update|delete)[^'\"]*['\"]\s*(\+|%))", {".py"}, "SQL built by string formatting (injection)", "Use parameterized queries."),
    ("TEMPFILE", "low", r"tempfile\.mktemp\s*\(", {".py"}, "mktemp is race-prone", "Use NamedTemporaryFile or mkstemp."),
    ("JS-EVAL", "high", r"\beval\s*\(|new Function\s*\(", {".js", ".ts", ".jsx", ".tsx", ".html"}, "eval on dynamic strings", "Avoid; parse data explicitly."),
    ("JS-XSS", "medium", r"\.innerHTML\s*=|document\.write\s*\(|dangerouslySetInnerHTML", {".js", ".ts", ".jsx", ".tsx", ".html"}, "Possible XSS sink", "Use textContent or sanitize."),
    ("JS-EXEC", "high", r"child_process\.(exec|execSync)\s*\(", {".js", ".ts"}, "Shell execution from Node", "Use execFile with an argument list."),
    ("CORS-ANY", "medium", r"(?i)access-control-allow-origin['\"]?\s*[:,]\s*['\"]?\*", None, "CORS allows any origin", "Restrict to known origins."),
]


def scan(root: Path, max_files=2000):
    root = Path(root)
    files = [root] if root.is_file() else [p for p in sorted(root.rglob("*")) if p.is_file() and not (set(p.parts) & SKIP_DIRS)
                                          and (p.suffix.lower() in EXTS or p.name.startswith(".env"))][:max_files]
    findings, scanned = [], 0
    for f in files:
        try:
            if f.stat().st_size > 400_000:
                continue
            lines = f.read_text(errors="replace").splitlines()
        except OSError:
            continue
        scanned += 1
        ext = ".env" if f.name.startswith(".env") else f.suffix.lower()
        for i, line in enumerate(lines, 1):
            if len(line) > 600:
                continue
            for rid, sev, rx, exts, msg, fix in RULES:
                if (exts is None or ext in exts) and re.search(rx, line):
                    snippet = re.sub(r"(['\"])[^'\"]{6,}(['\"])", r"\1…\2", line.strip())[:110] if rid.startswith("SECRET") else line.strip()[:110]
                    findings.append({"file": str(f.relative_to(root) if root.is_dir() else f.name), "line": i, "rule": rid,
                                     "severity": sev, "message": msg, "fix": fix, "code": snippet})
    root_dir = root if root.is_dir() else root.parent
    if (root_dir / ".env").exists():
        gi = (root_dir / ".gitignore").read_text() if (root_dir / ".gitignore").exists() else ""
        if ".env" not in gi:
            findings.append({"file": ".env", "line": 0, "rule": "ENV-TRACKED", "severity": "medium",
                             "message": ".env exists but is not listed in .gitignore", "fix": "Add .env to .gitignore.", "code": ""})
    for req in root_dir.glob("requirements*.txt"):
        loose = [l.strip() for l in req.read_text().splitlines() if l.strip() and not l.startswith(("#", "-")) and "==" not in l]
        if loose:
            findings.append({"file": req.name, "line": 0, "rule": "DEPS-UNPINNED", "severity": "low",
                             "message": f"{len(loose)} dependencies are not pinned (e.g. {loose[0]})", "fix": "Pin versions and run a dependency audit (pip-audit).", "code": ""})
    order = {"high": 0, "medium": 1, "low": 2}
    findings.sort(key=lambda x: (order[x["severity"]], x["file"], x["line"]))
    return {"scanned_files": scanned, "findings": findings,
            "counts": {s: sum(f["severity"] == s for f in findings) for s in order}}


def report_markdown(result, target):
    c = result["counts"]
    out = [f"# Security review: {target}", f"_Generated {time.strftime('%Y-%m-%d %H:%M')} by Courier's pattern scan._", "",
           f"Files scanned: {result['scanned_files']}. Findings: high {c['high']}, medium {c['medium']}, low {c['low']}.", "",
           "**Limits:** pattern matching only. It misses logic flaws, auth problems and vulnerable dependencies, and can flag safe code. "
           "Follow up with a real scanner (bandit, semgrep, pip-audit / npm audit) and a human review.", ""]
    for f in result["findings"]:
        out.append(f"- **{f['severity'].upper()}** `{f['file']}:{f['line']}` [{f['rule']}] {f['message']}  \n  Fix: {f['fix']}" + (f"  \n  `{f['code']}`" if f["code"] else ""))
    if not result["findings"]:
        out.append("No findings from the pattern rules.")
    return "\n".join(out) + "\n"
