"""The podcast's memory: what earlier episodes covered, so new ones can refer to them.

After an episode is finished, the ``memory`` stage stores ``memory.json`` in its job
directory: a one-sentence mini summary, a longer reference summary written for
referring back, key concepts and up to five follow-up suggestions.

Prompts only ever include the **mini summaries** of all earlier episodes. The long
summaries are copied into ``<job_dir>/library/<id>.md`` so that Claude can read the
few it actually refers to (``Read`` works inside the job directory only)::

    library/index.json        [{id, title, date, mini}, ...]
    library/<episode-id>.md   the reference summary of one earlier episode
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

MEMORY_FILE = "memory.json"
LIBRARY_DIR = "library"
MAX_EPISODES = 60  # mini summaries in a prompt (one line each, ~50 tokens)


def _read(path: Path) -> dict | None:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def load_memory(job_dir: Path) -> dict | None:
    return _read(job_dir / MEMORY_FILE)


def _entry(job_dir: Path) -> dict | None:
    memory = load_memory(job_dir)
    episode = _read(job_dir / "episode.json")
    if not memory or not episode:
        return None  # not finished, or made before episodes had a memory
    return {
        "id": job_dir.name,
        "title": episode.get("title", ""),
        "date": (episode.get("created_at") or "")[:10],
        "language": episode.get("language", ""),
        "mini": memory.get("mini_summary", ""),
        "reference": memory.get("reference_summary", ""),
        "key_concepts": memory.get("key_concepts", []),
        "references": memory.get("references", []),
        "follows": memory.get("follows", ""),
    }


def episode(episodes_dir: Path, episode_id: str) -> dict | None:
    """One finished episode with a memory, or None."""
    if not episode_id or "/" in episode_id or episode_id.startswith("."):
        return None
    return _entry(episodes_dir / episode_id)


def entries(episodes_dir: Path, exclude: str = "", limit: int = MAX_EPISODES) -> list[dict]:
    """Finished episodes with a memory, newest first (job IDs start with a timestamp)."""
    if not episodes_dir.exists():
        return []
    found = []
    for job_dir in sorted((d for d in episodes_dir.iterdir() if d.is_dir()), key=lambda d: d.name, reverse=True):
        if job_dir.name == exclude:
            continue
        entry = _entry(job_dir)
        if entry:
            found.append(entry)
            if len(found) >= limit:
                break
    return found


def snapshot(job_dir: Path, episodes_dir: Path) -> list[dict]:
    """Copy the library into the job directory; returns the entries (newest first)."""
    library = entries(episodes_dir, exclude=job_dir.name)
    target = job_dir / LIBRARY_DIR
    shutil.rmtree(target, ignore_errors=True)
    if not library:
        return []
    target.mkdir(parents=True)
    for e in library:
        (target / f"{e['id']}.md").write_text(
            f"# {e['title']} ({e['date']})\n\n{e['reference'].strip()}\n", encoding="utf-8")
    index = [{k: e[k] for k in ("id", "title", "date", "mini")} for e in library]
    (target / "index.json").write_text(json.dumps(index, ensure_ascii=False, indent=2), encoding="utf-8")
    return library


def links(episodes_dir: Path, episode_id: str) -> dict:
    """Links of one episode: the episodes it builds on or mentions, and those that mention it."""
    own = load_memory(episodes_dir / episode_id) or {}
    outgoing = []
    seen = set()
    if own.get("follows"):
        outgoing.append({"id": own["follows"], "how": "", "kind": "follows"})
        seen.add(own["follows"])
    for ref in own.get("references", []):
        if ref.get("id") not in seen:
            outgoing.append({**ref, "kind": "mentions"})
            seen.add(ref.get("id"))
    incoming = []
    for e in entries(episodes_dir, exclude=episode_id, limit=10_000):
        if e["follows"] == episode_id:
            incoming.append({"id": e["id"], "title": e["title"], "how": "", "kind": "follows"})
        else:
            how = next((r.get("how", "") for r in e["references"] if r.get("id") == episode_id), None)
            if how is not None:
                incoming.append({"id": e["id"], "title": e["title"], "how": how, "kind": "mentions"})
    for link in outgoing:
        target = _read(episodes_dir / link["id"] / "episode.json") if "/" not in link["id"] else None
        link["title"] = (target or {}).get("title", "")
    return {"outgoing": [o for o in outgoing if o["title"]], "incoming": incoming}
