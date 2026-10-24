"""Document intelligence: readers, writers (DOCX/XLSX/CSV), FTS5 knowledge base, version diff."""
from __future__ import annotations

import difflib
import hashlib
import re
import shutil
import sqlite3
import subprocess
import threading
import zipfile
import xml.etree.ElementTree as ET
from pathlib import Path
from xml.sax.saxutils import escape

TEXT_EXT = {".txt", ".md", ".csv", ".tsv", ".json", ".py", ".js", ".html", ".xml", ".log", ".yaml", ".yml", ".toml", ".ini"}
KB_EXT = TEXT_EXT | {".docx", ".xlsx", ".pdf"}
IMAGE_EXT = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp"}
W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
S = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"


class DocError(Exception):
    pass


def _xml(zf, name):
    raw = zf.read(name).decode("utf-8", "replace")
    if "<!DOCTYPE" in raw or "<!ENTITY" in raw:
        raise DocError("document rejected: DTD/entities not allowed")
    return ET.fromstring(raw)


def _group(lines, size, label):
    return [(f"{label} {i + 1}-{min(i + size, len(lines))}", "\n".join(lines[i:i + size]))
            for i in range(0, len(lines), size)] or [(f"{label} 1-0", "")]


def _col(ref):
    n = 0
    for ch in re.match(r"[A-Z]+", ref).group(0):
        n = n * 26 + ord(ch) - 64
    return n - 1


def read_document(path: Path):
    """-> [(locator, text)] where locator is a page / sheet rows / paragraph range reference."""
    ext = path.suffix.lower()
    if not path.exists():
        raise DocError(f"no such file: {path.name}")
    if ext in IMAGE_EXT:
        raise DocError("image file: use describe_image")
    if ext in TEXT_EXT:
        return _group(path.read_text(errors="replace").splitlines(), 60, "lines")
    if ext == ".docx":
        try:
            with zipfile.ZipFile(path) as z:
                root = _xml(z, "word/document.xml")
        except (zipfile.BadZipFile, KeyError) as e:
            raise DocError(f"not a valid .docx: {e}")
        paras = ["".join(t.text or "" for t in p.iter(W + "t")) for p in root.iter(W + "p")]
        return _group([p for p in paras if p.strip()], 30, "paragraphs")
    if ext == ".xlsx":
        try:
            with zipfile.ZipFile(path) as z:
                shared = []
                if "xl/sharedStrings.xml" in z.namelist():
                    shared = ["".join(t.text or "" for t in si.iter(S + "t")) for si in _xml(z, "xl/sharedStrings.xml")]
                names = [s.get("name") for s in _xml(z, "xl/workbook.xml").iter(S + "sheet")]
                sheets = sorted(n for n in z.namelist() if re.match(r"xl/worksheets/sheet\d+\.xml", n))
                out = []
                for i, sn in enumerate(sheets):
                    rows = []
                    for row in _xml(z, sn).iter(S + "row"):
                        cells = {}
                        for c in row.iter(S + "c"):
                            v = c.find(S + "v")
                            if c.get("t") == "s" and v is not None:
                                val = shared[int(v.text)]
                            elif c.get("t") == "inlineStr":
                                val = "".join(t.text or "" for t in c.iter(S + "t"))
                            else:
                                val = v.text if v is not None else ""
                            cells[_col(c.get("r", "A1"))] = val
                        if cells:
                            rows.append("\t".join(cells.get(k, "") for k in range(max(cells) + 1)))
                    out += _group(rows, 50, f"sheet '{names[i] if i < len(names) else i + 1}' rows")
                return out
        except (zipfile.BadZipFile, KeyError) as e:
            raise DocError(f"not a valid .xlsx: {e}")
    if ext == ".pdf":
        if shutil.which("pdftotext"):
            r = subprocess.run(["pdftotext", "-layout", str(path), "-"], capture_output=True, text=True, timeout=60)
            if r.returncode == 0:
                return [(f"page {i + 1}", t) for i, t in enumerate(r.stdout.split("\f")) if t.strip()]
        try:
            from pypdf import PdfReader  # optional
            return [(f"page {i + 1}", p.extract_text() or "") for i, p in enumerate(PdfReader(str(path)).pages)]
        except ImportError:
            raise DocError("PDF reading needs poppler-utils (pdftotext) or `pip install pypdf`")
    raise DocError(f"unsupported file type: {ext}")


# ---------------- writers -----------------
def write_docx(path: Path, items):
    """items: strings, or {"h": text} for a heading. Minimal valid .docx."""
    body = []
    for it in items:
        if isinstance(it, dict) and "h" in it:
            body.append(f'<w:p><w:r><w:rPr><w:b/><w:sz w:val="32"/></w:rPr><w:t xml:space="preserve">{escape(str(it["h"]))}</w:t></w:r></w:p>')
        else:
            body.append(f'<w:p><w:r><w:t xml:space="preserve">{escape(str(it))}</w:t></w:r></w:p>')
    doc = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?><w:document xmlns:w="http://schemas.openxmlformats.org/'
           'wordprocessingml/2006/main"><w:body>' + "".join(body) + "</w:body></w:document>")
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("[Content_Types].xml", '<?xml version="1.0" encoding="UTF-8"?><Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"><Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/><Default Extension="xml" ContentType="application/xml"/><Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/></Types>')
        z.writestr("_rels/.rels", '<?xml version="1.0" encoding="UTF-8"?><Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="word/document.xml"/></Relationships>')
        z.writestr("word/document.xml", doc)


def _cell(ref, v):
    if isinstance(v, bool) or v is None:
        v = "" if v is None else str(v)
    if isinstance(v, (int, float)):
        return f'<c r="{ref}"><v>{v}</v></c>'
    return f'<c r="{ref}" t="inlineStr"><is><t xml:space="preserve">{escape(str(v))}</t></is></c>'


def _letters(i):
    s = ""
    i += 1
    while i:
        i, r = divmod(i - 1, 26)
        s = chr(65 + r) + s
    return s


def write_xlsx(path: Path, sheets: dict):
    """sheets: {name: [[cell, ...], ...]}. Strings stored inline, numbers as numbers. No formulas."""
    names = [re.sub(r"[\[\]:*?/\\]", "_", n)[:31] or f"Sheet{i + 1}" for i, n in enumerate(sheets)]
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as z:
        ct = "".join(f'<Override PartName="/xl/worksheets/sheet{i + 1}.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>' for i in range(len(names)))
        z.writestr("[Content_Types].xml", '<?xml version="1.0" encoding="UTF-8"?><Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"><Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/><Default Extension="xml" ContentType="application/xml"/><Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>' + ct + "</Types>")
        z.writestr("_rels/.rels", '<?xml version="1.0" encoding="UTF-8"?><Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/></Relationships>')
        z.writestr("xl/workbook.xml", '<?xml version="1.0" encoding="UTF-8"?><workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"><sheets>' + "".join(f'<sheet name="{escape(n)}" sheetId="{i + 1}" r:id="rId{i + 1}"/>' for i, n in enumerate(names)) + "</sheets></workbook>")
        z.writestr("xl/_rels/workbook.xml.rels", '<?xml version="1.0" encoding="UTF-8"?><Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">' + "".join(f'<Relationship Id="rId{i + 1}" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet{i + 1}.xml"/>' for i in range(len(names))) + "</Relationships>")
        for i, rows in enumerate(sheets.values()):
            xml = "".join(f'<row r="{r + 1}">' + "".join(_cell(f"{_letters(c)}{r + 1}", v) for c, v in enumerate(row)) + "</row>"
                          for r, row in enumerate(rows))
            z.writestr(f"xl/worksheets/sheet{i + 1}.xml", '<?xml version="1.0" encoding="UTF-8"?><worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"><sheetData>' + xml + "</sheetData></worksheet>")


# ---------------- diff -----------------
def diff_documents(a: Path, b: Path, max_lines=200):
    ta = "\n".join(t for _, t in read_document(a)).splitlines()
    tb = "\n".join(t for _, t in read_document(b)).splitlines()
    d = list(difflib.unified_diff(ta, tb, a.name, b.name, lineterm="", n=1))
    added = sum(1 for l in d if l.startswith("+") and not l.startswith("+++"))
    removed = sum(1 for l in d if l.startswith("-") and not l.startswith("---"))
    return {"added_lines": added, "removed_lines": removed, "identical": not d, "diff": "\n".join(d[:max_lines])}


# ---------------- knowledge base (RAG with FTS5; no embeddings) -----------------
def _chunks(text, size=1200, overlap=150):
    text = text.strip()
    if len(text) <= size:
        return [text] if text else []
    out, i = [], 0
    while i < len(text):
        j = min(len(text), i + size)
        if j < len(text):
            cut = max(text.rfind("\n", i, j), text.rfind(". ", i, j))
            j = cut + 1 if cut > i + size // 2 else j
        out.append(text[i:j].strip())
        if j >= len(text):
            break
        i = max(j - overlap, i + 1)
    return out


class KnowledgeBase:
    def __init__(self, path: Path):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(str(path), check_same_thread=False)
        self._lock = threading.Lock()
        with self._lock:
            self.db.executescript("""
            create table if not exists docs(id integer primary key, path text unique, sha text, n_chunks int);
            create table if not exists chunks(id integer primary key, doc_id int, locator text, text text);
            create virtual table if not exists chunks_fts using fts5(text);
            create table if not exists vecs(chunk_id integer primary key, vec blob);
            """)

    def ingest(self, file: Path, label=None) -> dict:
        label = label or str(file)
        raw = file.read_bytes()
        sha = hashlib.sha256(raw).hexdigest()
        with self._lock:
            row = self.db.execute("select id, sha from docs where path=?", (label,)).fetchone()
            if row and row[1] == sha:
                return {"path": label, "status": "unchanged"}
            dup = self.db.execute("select path from docs where sha=? and path!=?", (sha, label)).fetchone()
        segs = read_document(file)
        with self._lock:
            if row:
                ids = [r[0] for r in self.db.execute("select id from chunks where doc_id=?", (row[0],))]
                self.db.executemany("delete from chunks_fts where rowid=?", [(i,) for i in ids])
                self.db.executemany("delete from vecs where chunk_id=?", [(i,) for i in ids])
                self.db.execute("delete from chunks where doc_id=?", (row[0],))
                self.db.execute("delete from docs where id=?", (row[0],))
            cur = self.db.execute("insert into docs(path, sha, n_chunks) values(?,?,0)", (label, sha))
            doc_id, n = cur.lastrowid, 0
            for loc, text in segs:
                parts = _chunks(text)
                for k, part in enumerate(parts):
                    c = self.db.execute("insert into chunks(doc_id, locator, text) values(?,?,?)",
                                        (doc_id, loc + (f" (part {k + 1})" if len(parts) > 1 else ""), part))
                    self.db.execute("insert into chunks_fts(rowid, text) values(?,?)", (c.lastrowid, part))
                    n += 1
            self.db.execute("update docs set n_chunks=? where id=?", (n, doc_id))
            self.db.commit()
        out = {"path": label, "status": "updated" if row else "added", "chunks": n}
        if dup:
            out["duplicate_of"] = dup[0]
        return out

    def _fts(self, query, n):
        toks = re.findall(r"\w+", query)
        if not toks:
            return []
        q = " OR ".join(f'"{t}"' for t in toks)
        with self._lock:
            return self.db.execute(
                "select c.id, d.path, c.locator, c.text from chunks_fts f join chunks c on c.id=f.rowid "
                "join docs d on d.id=c.doc_id where chunks_fts match ? order by bm25(chunks_fts) limit ?", (q, n)).fetchall()

    def embed_pending(self, embedder, limit=500):
        """Embed chunks that have no vector yet. Returns how many were embedded."""
        from .embed import pack
        with self._lock:
            rows = self.db.execute("select id, text from chunks where id not in (select chunk_id from vecs) limit ?", (limit,)).fetchall()
        if not rows:
            return 0
        vecs = embedder.embed([r[1] for r in rows], "passage")
        with self._lock:
            self.db.executemany("insert or replace into vecs values(?,?)", [(r[0], pack(v)) for r, v in zip(rows, vecs)])
            self.db.commit()
        return len(rows)

    def _vector(self, query, n, embedder):
        from .embed import cosine, unpack
        with self._lock:
            vecs = self.db.execute("select chunk_id, vec from vecs").fetchall()
        if not vecs:
            return []
        qv = embedder.embed([query], "query")[0]
        top = sorted(((cosine(qv, unpack(b)), cid) for cid, b in vecs), reverse=True)[:n]
        ids = [cid for sc, cid in top if sc > 0.2]
        if not ids:
            return []
        with self._lock:
            got = {r[0]: r for r in self.db.execute(
                f"select c.id, d.path, c.locator, c.text from chunks c join docs d on d.id=c.doc_id where c.id in ({','.join('?' * len(ids))})", ids)}
        return [got[i] for i in ids if i in got]

    def search(self, query, k=5, embedder=None):
        """Keyword (FTS5) search; with an embedder also semantic search, fused by reciprocal rank."""
        fts = self._fts(query, max(k * 4, 20))
        sem = self._vector(query, max(k * 4, 20), embedder) if embedder else []
        score, rows, via = {}, {}, {}
        for tag, lst in (("keyword", fts), ("semantic", sem)):
            for rank, r in enumerate(lst):
                score[r[0]] = score.get(r[0], 0) + 1 / (60 + rank)
                rows[r[0]] = r
                via.setdefault(r[0], set()).add(tag)
        best = sorted(score, key=score.get, reverse=True)[:k]
        return [{"path": rows[i][1], "where": rows[i][2], "text": rows[i][3][:900], "via": "+".join(sorted(via[i]))} for i in best]

    def stats(self):
        with self._lock:
            d, c = self.db.execute("select count(*), coalesce(sum(n_chunks),0) from docs").fetchone()
        return {"documents": d, "chunks": c}
