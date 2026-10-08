"""The learning path of a series as a tree diagram (inline SVG, drawn on the server).

Nodes are the parts of a series (existing episodes first, then the planned ones);
an edge A → B means "B builds on A". Parts are placed in rows by the longest chain
of prerequisites above them, so everything a part builds on sits higher up.
"""

from __future__ import annotations

import textwrap
from html import escape

NODE_W, NODE_H = 210, 58
GAP_X, GAP_Y, PAD = 22, 46, 12
CHARS_PER_LINE = 28


def nodes_for(series: dict, jobs: dict) -> list[dict]:
    """[{number, title, status, href, builds_on}] for the existing and the planned parts."""
    plan = series.get("plan") or {}
    assigned = plan.get("assigned") or {}
    nodes = []
    for job_id in series["episodes"]:
        job = jobs.get(job_id)
        planned = assigned.get(job_id) or {}
        status = "done" if job and job.status == "done" else "created"
        nodes.append({"title": (job.display_title if job else "") or planned.get("title") or job_id,
                      "status": status, "href": f"/episodes/{job_id}", "builds_on": planned.get("builds_on") or []})
    for item in plan.get("items") or []:
        nodes.append({"title": item["title"], "status": "planned", "href": "", "builds_on": item.get("builds_on") or []})
    for number, node in enumerate(nodes, 1):
        node["number"] = number
        node["builds_on"] = sorted({b for b in node["builds_on"] if isinstance(b, int) and 1 <= b < number})
    return nodes


def is_linear(nodes: list[dict]) -> bool:
    """True when every part builds at most on the one right before it (a plain list says it all)."""
    return all(n["builds_on"] in ([], [n["number"] - 1]) for n in nodes)


def render(series: dict, jobs: dict) -> dict | None:
    """{"svg": markup, "branched": bool}, or None when there is nothing worth drawing."""
    nodes = nodes_for(series, jobs)
    if len(nodes) < 3 or not any(n["builds_on"] for n in nodes):
        return None
    level = {}
    for n in nodes:  # parents always have smaller numbers, so one pass suffices
        level[n["number"]] = max((level[b] + 1 for b in n["builds_on"]), default=0)
    rows: dict[int, list[dict]] = {}
    for n in nodes:
        rows.setdefault(level[n["number"]], []).append(n)
    widest = max(len(r) for r in rows.values())
    width = widest * NODE_W + (widest - 1) * GAP_X + 2 * PAD
    height = len(rows) * NODE_H + (len(rows) - 1) * GAP_Y + 2 * PAD
    pos = {}
    for depth, row in rows.items():
        row_w = len(row) * NODE_W + (len(row) - 1) * GAP_X
        x0 = (width - row_w) / 2
        for i, n in enumerate(row):
            pos[n["number"]] = (x0 + i * (NODE_W + GAP_X), PAD + depth * (NODE_H + GAP_Y))

    parts = [f'<svg class="series-graph" viewBox="0 0 {width:.0f} {height:.0f}" width="{width:.0f}" '
             f'height="{height:.0f}" role="img" aria-label="Lernpfad der Reihe: welche Folge auf welcher aufbaut">']
    for n in nodes:
        x2, y2 = pos[n["number"]]
        for b in n["builds_on"]:
            x1, y1 = pos[b]
            sx, sy, ex, ey = x1 + NODE_W / 2, y1 + NODE_H, x2 + NODE_W / 2, y2
            mid = (sy + ey) / 2
            parts.append(f'<path class="edge" d="M{sx:.0f},{sy:.0f} C{sx:.0f},{mid:.0f} {ex:.0f},{mid:.0f} '
                         f'{ex:.0f},{ey - 4:.0f}" marker-end="url(#arrow-{series["id"]})"/>')
    parts.append(f'<defs><marker id="arrow-{series["id"]}" viewBox="0 0 8 8" refX="7" refY="4" markerWidth="7" '
                 'markerHeight="7" orient="auto"><path class="arrow" d="M0,0 L8,4 L0,8 z"/></marker></defs>')
    labels = {"done": "fertig", "created": "angelegt", "planned": "geplant"}
    for n in nodes:
        x, y = pos[n["number"]]
        wrapped = textwrap.wrap(f'{n["number"]}. {n["title"]}', CHARS_PER_LINE) or [""]
        lines = wrapped[:2]
        if len(wrapped) > 2:
            lines[1] = lines[1][: CHARS_PER_LINE - 1].rstrip() + "…"
        top = y + (NODE_H / 2 + 5 if len(lines) == 1 else 24)
        text = "".join(f'<tspan x="{x + 10:.0f}" dy="{0 if i == 0 else 16}">{escape(line)}</tspan>'
                       for i, line in enumerate(lines))
        box = (f'<g class="node {n["status"]}"><title>{escape(n["title"])} ({labels[n["status"]]})</title>'
               f'<rect x="{x:.0f}" y="{y:.0f}" width="{NODE_W}" height="{NODE_H}" rx="8"/>'
               f'<text x="{x + 10:.0f}" y="{top:.0f}">{text}</text></g>')
        parts.append(f'<a href="{escape(n["href"])}">{box}</a>' if n["href"] else box)
    parts.append("</svg>")
    return {"svg": "".join(parts), "branched": not is_linear(nodes)}
