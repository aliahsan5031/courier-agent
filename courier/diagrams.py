"""Flowcharts and process diagrams as SVG (no external renderer) plus Mermaid source."""
from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from xml.sax.saxutils import escape


def parse_mermaid(text):
    """Small Mermaid flowchart subset: `A[Start] --> B{Ok?}`, `B -->|yes| C(Done)`."""
    nodes, edges = {}, []
    node_re = r"([A-Za-z_][\w]*)(?:\[([^\]]*)\]|\(([^)]*)\)|\{([^}]*)\})?"
    for line in text.splitlines():
        line = line.strip().rstrip(";")
        if not line or line.lower().startswith(("flowchart", "graph", "%%")):
            continue
        m = re.fullmatch(node_re + r"\s*(?:--+>|-->)\s*(?:\|([^|]*)\|\s*)?" + node_re, line)
        if not m:
            raise ValueError(f"unsupported Mermaid line: {line!r}")
        a, al1, al2, al3, label, b, bl1, bl2, bl3 = m.groups()
        for nid, (sq, rd, cu) in ((a, (al1, al2, al3)), (b, (bl1, bl2, bl3))):
            shape = "diamond" if cu is not None else "round" if rd is not None else "box"
            lab = next((x for x in (sq, rd, cu) if x), None)
            if nid not in nodes or lab:
                nodes[nid] = {"id": nid, "label": lab or nodes.get(nid, {}).get("label", nid), "shape": shape if lab else nodes.get(nid, {}).get("shape", "box")}
        edges.append({"from": a, "to": b, "label": label or ""})
    return list(nodes.values()), edges


def to_mermaid(nodes, edges, direction="TD"):
    def fmt(n):
        l = n["label"].replace("[", "(").replace("]", ")")
        return {"diamond": f"{n['id']}{{{l}}}", "round": f"{n['id']}({l})"}.get(n.get("shape"), f"{n['id']}[{l}]")
    by = {n["id"]: n for n in nodes}
    lines = [f"flowchart {direction}"]
    for e in edges:
        lab = f"|{e['label']}|" if e.get("label") else ""
        lines.append(f"    {fmt(by[e['from']])} -->{lab} {fmt(by[e['to']])}")
    return "\n".join(lines) + "\n"


def _layers(nodes, edges):
    ids = [n["id"] for n in nodes]
    out = {i: [] for i in ids}
    for e in edges:
        if e["from"] in out and e["to"] in out:
            out[e["from"]].append(e["to"])
    state, back = {}, set()

    def dfs(u):
        state[u] = 1
        for v in out[u]:
            if state.get(v) == 1:
                back.add((u, v))
            elif v not in state:
                dfs(v)
        state[u] = 2
    indeg = {i: 0 for i in ids}
    for u in ids:
        for v in out[u]:
            indeg[v] += 1
    for i in [x for x in ids if indeg[x] == 0] + ids:
        if i not in state:
            dfs(i)
    layer = {i: 0 for i in ids}
    for _ in range(len(ids)):  # longest-path layering ignoring back edges
        for u in ids:
            for v in out[u]:
                if (u, v) not in back:
                    layer[v] = max(layer[v], layer[u] + 1)
    return layer


def to_svg(nodes, edges, direction="TB"):
    if not nodes:
        raise ValueError("no nodes")
    ids = {n["id"] for n in nodes}
    for e in edges:
        if e["from"] not in ids or e["to"] not in ids:
            raise ValueError(f"edge references unknown node: {e['from']} -> {e['to']}")
    layer = _layers(nodes, edges)
    rows = {}
    for n in nodes:
        rows.setdefault(layer[n["id"]], []).append(n)
    W = {n["id"]: max(90, 8 * len(n["label"]) + 28) for n in nodes}
    H, GX, GY, PAD = 44, 40, 70, 30
    pos, horiz = {}, direction.upper() in ("LR", "RL")
    if not horiz:
        span = max(sum(W[n["id"]] + GX for n in r) - GX for r in rows.values())
        for li, row in sorted(rows.items()):
            x = PAD + (span - (sum(W[n["id"]] + GX for n in row) - GX)) / 2
            for n in row:
                pos[n["id"]] = (x + W[n["id"]] / 2, PAD + li * (H + GY) + H / 2)
                x += W[n["id"]] + GX
    else:
        colw = max(W.values())
        for li, row in sorted(rows.items()):
            for j, n in enumerate(row):
                pos[n["id"]] = (PAD + colw / 2 + li * (colw + GY), PAD + H / 2 + j * (H + GX))
    xs = [p[0] + W[k] / 2 for k, p in pos.items()]
    ys = [p[1] + H / 2 for p in pos.values()]
    width, height = int(max(xs) + PAD), int(max(ys) + PAD)
    out = [f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" viewBox="0 0 {width} {height}" font-family="Arial,sans-serif" font-size="13">',
           '<defs><marker id="arr" markerWidth="10" markerHeight="8" refX="9" refY="4" orient="auto"><path d="M0,0 L10,4 L0,8 z" fill="#444"/></marker></defs>',
           f'<rect width="{width}" height="{height}" fill="#ffffff"/>']
    for e in edges:
        (x1, y1), (x2, y2) = pos[e["from"]], pos[e["to"]]
        if horiz:
            x1, x2 = x1 + W[e["from"]] / 2, x2 - W[e["to"]] / 2
        else:
            y1, y2 = y1 + H / 2, y2 - H / 2
        out.append(f'<line x1="{x1:.0f}" y1="{y1:.0f}" x2="{x2:.0f}" y2="{y2:.0f}" stroke="#444" stroke-width="1.5" marker-end="url(#arr)"/>')
        if e.get("label"):
            out.append(f'<text x="{(x1 + x2) / 2 + 4:.0f}" y="{(y1 + y2) / 2:.0f}" fill="#555">{escape(e["label"])}</text>')
    for n in nodes:
        cx, cy = pos[n["id"]]
        w = W[n["id"]]
        if n.get("shape") == "diamond":
            out.append(f'<polygon points="{cx:.0f},{cy - H / 2 - 8:.0f} {cx + w / 2 + 10:.0f},{cy:.0f} {cx:.0f},{cy + H / 2 + 8:.0f} {cx - w / 2 - 10:.0f},{cy:.0f}" fill="#fff4d6" stroke="#b58900"/>')
        else:
            rx = 22 if n.get("shape") == "round" else 4
            out.append(f'<rect x="{cx - w / 2:.0f}" y="{cy - H / 2:.0f}" width="{w}" height="{H}" rx="{rx}" fill="#e8f1ff" stroke="#2b6cb0"/>')
        out.append(f'<text x="{cx:.0f}" y="{cy + 4:.0f}" text-anchor="middle" fill="#111">{escape(n["label"])}</text>')
    out.append("</svg>")
    svg = "\n".join(out)
    ET.fromstring(svg)  # must be well-formed
    return svg
