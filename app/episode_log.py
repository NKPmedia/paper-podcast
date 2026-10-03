"""Reading an episode's ``log.jsonl``: token totals and a readable process timeline."""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

TOKEN_FIELDS = ("input", "output", "cache_read", "cache_write")
STAGE_LABELS = {"research": "Recherche", "script": "Skript", "handout": "Handout", "tts": "Sprachausgabe",
                "audio": "Audio"}


def read_log(job_dir: Path) -> list[dict]:
    path = job_dir / "log.jsonl"
    if not path.exists():
        return []
    entries = []
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            entries.append(json.loads(line))
        except json.JSONDecodeError:
            continue  # a line cut short by a crash
    return entries


def call_tokens(entry: dict) -> dict[str, int]:
    tokens = entry.get("tokens") or {}
    row = {key: int(tokens.get(key) or 0) for key in TOKEN_FIELDS}
    row["total"] = sum(row.values())
    return row


def token_totals(entries: list[dict]) -> dict[str, int]:
    """Summed token usage of all Claude calls; ``calls`` counts calls that reported tokens."""
    totals = {key: 0 for key in (*TOKEN_FIELDS, "total")}
    calls = 0
    for entry in entries:
        if entry.get("event") != "claude":
            continue
        row = call_tokens(entry)
        if row["total"]:
            calls += 1
        for key, value in row.items():
            totals[key] += value
    totals["calls"] = calls
    return totals


def episode_tokens(job_dir: Path) -> int | None:
    totals = token_totals(read_log(job_dir))
    return totals["total"] if totals["calls"] else None


def fmt_tokens(value: int | None) -> str:
    if not value:
        return "0"
    if value >= 1_000_000:
        return f"{value / 1_000_000:.2f}".replace(".", ",") + " Mio."
    if value >= 10_000:
        return f"{value / 1000:.0f} Tsd."
    return f"{value:,}".replace(",", ".")


def _describe(entry: dict) -> tuple[str, str, str]:
    """(kind, text, detail) for one log entry; kind drives the styling."""
    event = entry.get("event")
    stage = STAGE_LABELS.get(entry.get("stage", ""), entry.get("stage", ""))
    if event == "stage_start":
        return "stage", f"{stage} gestartet", ""
    if event == "stage_done":
        return "done", f"{stage} fertig nach {_seconds(entry.get('seconds'))}", ""
    if event == "stage_skipped":
        return "muted", f"{stage} übersprungen (bereits fertig)", ""
    if event == "stage_error":
        return "error", f"{stage} fehlgeschlagen", entry.get("error", "")
    if event == "progress":
        return "info", entry.get("message", ""), ""
    if event == "claude":
        step = entry.get("step", "")
        step = step.replace("scout:", "Scout ") if step else ""
        if entry.get("attempt", 1) and entry.get("attempt", 1) > 1:
            step = f"{step} Versuch {entry['attempt']}".strip()
        tokens = call_tokens(entry)
        parts = [f"{entry.get('turns', 0)} Runden"]
        if tokens["total"]:
            parts.append(f"{fmt_tokens(tokens['total'])} Tokens")
        for key, label in (("candidates", "Kandidaten"), ("selected", "ausgewählt"), ("sources", "Quellen"),
                           ("words", "Wörter")):
            if entry.get(key) is not None:
                parts.append(f"{entry[key]} {label}")
        if entry.get("skills_used"):
            parts.append("Skills: " + ", ".join(entry["skills_used"]))
        detail = "\n".join(entry.get("problems") or [])
        title = f"Claude ({entry.get('model', '')}) · {stage}" + (f" · {step}" if step else "")
        return "claude", f"{title}: " + ", ".join(parts), detail
    if event == "clarify":
        count = entry.get("questions", 0)
        text = f"Claude hat {count} Rückfrage(n) gestellt" if count else "Keine Rückfragen nötig – das Thema ist klar"
        return ("warn" if count else "info"), text, entry.get("reason", "")
    if event == "waiting":
        return "warn", "Pausiert: wartet auf deine Antworten", ""
    if event == "answers":
        answered, total = entry.get("answered", 0), entry.get("questions", 0)
        text = (f"Antworten erhalten ({answered} von {total})" if answered
                else "Ohne Antworten fortgesetzt – Claude entscheidet selbst")
        return "done", text, ""
    if event == "scout_failed":
        return "error", f"Scout {entry.get('angle', '')} fehlgeschlagen", entry.get("error", "")
    if event == "downloads":
        failed = entry.get("failed") or []
        text = f"{entry.get('ok', 0)} Volltext(e) geladen" + (f", {len(failed)} nicht verfügbar" if failed else "")
        return ("warn" if failed else "info"), text, "\n".join(f"{f['id']}: {f['error']}" for f in failed)
    if event == "tts":
        text = f"Sprachausgabe mit {entry.get('provider', '')}: {entry.get('clips', 0)} Clips"
        return ("warn" if entry.get("note") else "info"), text, entry.get("note", "")
    if event == "handout":
        problems = list((entry.get("plot_errors") or {}).values()) + list(entry.get("latex_errors") or [])
        text = "Handout gesetzt" + (f" nach {len(problems)} Korrektur(en)" if problems else "")
        return ("warn" if problems else "info"), text, "\n\n".join(problems)
    if event == "episode":
        return "done", f"Episode fertig: {_seconds(entry.get('duration_seconds'))} Audio", ""
    return "muted", str(event), ""


def _seconds(value) -> str:
    if value is None:
        return "?"
    value = int(round(float(value)))
    return f"{value // 60} min {value % 60:02d} s" if value >= 60 else f"{value} s"


def timeline(entries: list[dict], tz=None) -> list[dict]:
    """Log entries as display rows. Drops the progress message that only repeats a stage start."""
    rows = []
    for i, entry in enumerate(entries):
        nxt = entries[i + 1] if i + 1 < len(entries) else {}
        if (entry.get("event") == "progress" and nxt.get("event") == "stage_start"
                and nxt.get("stage") == entry.get("stage")):
            continue
        kind, text, detail = _describe(entry)
        ts = entry.get("ts", "")
        try:
            moment = datetime.fromisoformat(ts)
            time = (moment.astimezone(tz) if tz else moment).strftime("%H:%M:%S")
        except ValueError:
            time = ""
        rows.append({"time": time, "kind": kind, "text": text, "detail": detail})
    return rows
