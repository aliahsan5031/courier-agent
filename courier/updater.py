"""Update distribution through GitHub Releases: check, download, verify SHA-256, install with rollback.

Safety: a release without a checksum file is refused unless allow_unverified=True. Releases are NOT
cryptographically signed (a compromised repo could ship a matching checksum); pin the repo you trust.
"""
from __future__ import annotations

import hashlib
import io
import json
import re
import shutil
import time
import zipfile
from pathlib import Path


class UpdateError(Exception):
    pass


def parse_ver(s):
    nums = re.findall(r"\d+", str(s).split("-")[0])
    return tuple(int(n) for n in nums[:4]) or (0,)


def check(repo, current, http, token=None):
    """-> release dict if a newer version exists, else None."""
    if not re.fullmatch(r"[\w.\-]+/[\w.\-]+", repo or ""):
        raise UpdateError("set update_repo in config.json to owner/name")
    h = {"Accept": "application/vnd.github+json"}
    if token:
        h["Authorization"] = f"Bearer {token}"
    st, tx, _ = http("GET", f"https://api.github.com/repos/{repo}/releases/latest", h)
    if st == 404:
        raise UpdateError("no releases published for that repository")
    if st >= 400:
        raise UpdateError(f"GitHub HTTP {st}")
    rel = json.loads(tx)
    if parse_ver(rel["tag_name"]) <= parse_ver(current):
        return None
    return {"version": rel["tag_name"].lstrip("v"), "notes": (rel.get("body") or "")[:600],
            "assets": {a["name"]: a["browser_download_url"] for a in rel.get("assets", [])}}


def download_verified(rel, raw, allow_unverified=False):
    """Returns the zip bytes after verifying SHA-256 against SHA256SUMS or <asset>.sha256."""
    zips = [n for n in rel["assets"] if n.endswith(".zip")]
    if not zips:
        raise UpdateError("release has no .zip asset")
    name = zips[0]
    st, data, _ = raw("GET", rel["assets"][name], {"Accept": "application/octet-stream"}, None)
    if st >= 400:
        raise UpdateError(f"download failed: HTTP {st}")
    digest = hashlib.sha256(data).hexdigest()
    sums = None
    for cand in ("SHA256SUMS", "SHA256SUMS.txt", name + ".sha256"):
        if cand in rel["assets"]:
            s2, body, _ = raw("GET", rel["assets"][cand], {}, None)
            if s2 < 400:
                sums = body.decode(errors="replace")
                break
    if sums is None:
        if not allow_unverified:
            raise UpdateError("release has no checksum file; refusing to install an unverified update")
        return data, digest, False
    expected = {m.group(2): m.group(1).lower() for m in re.finditer(r"([0-9a-fA-F]{64})\s+\*?(\S+)", sums)}
    want = expected.get(name) or (re.match(r"\s*([0-9a-fA-F]{64})", sums).group(1).lower() if name + ".sha256" in rel["assets"] else None)
    if want != digest:
        raise UpdateError("CHECKSUM MISMATCH: download rejected")
    return data, digest, True


def install(data: bytes, pkg_dir: Path, home: Path, old_version: str, new_version: str):
    """Replace the courier package directory with the one inside the zip; keep a rollback copy."""
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        names = z.namelist()
        if any(n.startswith("/") or ".." in Path(n).parts for n in names):
            raise UpdateError("unsafe path in update")
        root = next((n.rsplit("courier/__init__.py", 1)[0] for n in names if n.endswith("courier/__init__.py")), None)
        if root is None:
            raise UpdateError("update does not contain a courier package")
        stage = Path(home) / "update-staging"
        shutil.rmtree(stage, ignore_errors=True)
        z.extractall(stage)
    new_pkg = stage / root / "courier"
    bak = Path(home) / "rollback" / f"courier-{old_version}-{int(time.time())}"
    bak.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(pkg_dir, bak)
    try:
        for p in pkg_dir.glob("*.py"):
            p.unlink()
        for p in new_pkg.glob("*.py"):
            shutil.copy2(p, pkg_dir / p.name)
        compile_ok = all(compile(p.read_text(), str(p), "exec") for p in pkg_dir.glob("*.py"))
        if not compile_ok:
            raise UpdateError("new files failed to compile")
    except Exception:
        rollback(home, pkg_dir)
        raise
    finally:
        shutil.rmtree(stage, ignore_errors=True)
    return str(bak)


def rollback(home: Path, pkg_dir: Path):
    backups = sorted((Path(home) / "rollback").glob("courier-*"), key=lambda p: p.stat().st_mtime)
    if not backups:
        raise UpdateError("no rollback copy available")
    for p in pkg_dir.glob("*.py"):
        p.unlink()
    for p in backups[-1].glob("*.py"):
        shutil.copy2(p, pkg_dir / p.name)
    return backups[-1].name
