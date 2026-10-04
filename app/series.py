"""Series ("Reihen"): episodes grouped in order, with a common thread.

Stored in ``<data>/series.json`` (the order of the parts lives only here)::

    [{"id": "s1a2b3c4", "title": "...", "arc": "the common thread", "episodes": [job ids in order],
      "created_at": "..."}]

An episode belongs to at most one series. The script of part n gets the arc, the
mini summaries of the earlier parts and the reference summary of the part right
before it, opens with a short "previously on" recap and ends with a teaser.
"""

from __future__ import annotations

import json
import secrets
import threading
from datetime import datetime, timezone
from pathlib import Path

from app import library

_lock = threading.Lock()


class SeriesError(ValueError):
    pass


class SeriesStore:
    def __init__(self, path: Path, episodes_dir: Path | None = None):
        self.path = path
        self.episodes_dir = episodes_dir

    # --- storage ------------------------------------------------------------------

    def _load(self) -> list[dict]:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return []
        return data if isinstance(data, list) else []

    def _save(self, data: list[dict]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(self.path)

    def _existing(self, ids: list[str]) -> list[str]:
        """Parts whose episode still exists (deleted episodes drop out of the series)."""
        if self.episodes_dir is None:
            return ids
        return [i for i in ids if (self.episodes_dir / i).is_dir()]

    # --- reading ------------------------------------------------------------------

    def all(self) -> list[dict]:
        return [{**s, "episodes": self._existing(s.get("episodes", []))} for s in self._load()]

    def get(self, series_id: str) -> dict | None:
        return next((s for s in self.all() if s["id"] == series_id), None)

    def of_episode(self, job_id: str) -> tuple[dict, int] | None:
        """The series an episode belongs to and its 1-based part number."""
        for s in self.all():
            if job_id in s["episodes"]:
                return s, s["episodes"].index(job_id) + 1
        return None

    def parts_by_episode(self) -> dict[str, tuple[dict, int]]:
        return {job_id: (s, i + 1) for s in self.all() for i, job_id in enumerate(s["episodes"])}

    # --- writing ------------------------------------------------------------------

    def create(self, title: str, arc: str = "", episodes: list[str] | None = None) -> dict:
        title = " ".join(title.split())[:120]
        if not title:
            raise SeriesError("Bitte einen Titel für die Reihe angeben.")
        with _lock:
            data = self._load()
            for job_id in episodes or []:
                for s in data:
                    if job_id in s["episodes"]:
                        s["episodes"].remove(job_id)
            series = {"id": "s" + secrets.token_hex(4), "title": title, "arc": arc.strip()[:3000],
                      "episodes": list(episodes or []),
                      "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds")}
            data.append(series)
            self._save(data)
        return series

    def update(self, series_id: str, title: str, arc: str) -> None:
        title = " ".join(title.split())[:120]
        if not title:
            raise SeriesError("Bitte einen Titel für die Reihe angeben.")
        with _lock:
            data = self._load()
            series = next((s for s in data if s["id"] == series_id), None)
            if series is None:
                raise SeriesError("Reihe nicht gefunden.")
            series["title"], series["arc"] = title, arc.strip()[:3000]
            self._save(data)

    def add_episode(self, series_id: str, job_id: str) -> None:
        """Append an episode as the next part (moving it out of any other series)."""
        with _lock:
            data = self._load()
            target = next((s for s in data if s["id"] == series_id), None)
            if target is None:
                raise SeriesError("Reihe nicht gefunden.")
            for s in data:
                if job_id in s["episodes"]:
                    s["episodes"].remove(job_id)
            target["episodes"].append(job_id)
            self._save(data)

    def remove_episode(self, job_id: str) -> None:
        with _lock:
            data = self._load()
            for s in data:
                if job_id in s["episodes"]:
                    s["episodes"].remove(job_id)
            self._save(data)

    def move(self, series_id: str, job_id: str, offset: int) -> None:
        with _lock:
            data = self._load()
            series = next((s for s in data if s["id"] == series_id), None)
            if series is None or job_id not in series["episodes"]:
                raise SeriesError("Folge nicht in dieser Reihe.")
            parts = series["episodes"]
            i = parts.index(job_id)
            j = max(0, min(len(parts) - 1, i + offset))
            parts.insert(j, parts.pop(i))
            self._save(data)

    def delete(self, series_id: str) -> None:
        """Remove the grouping; the episodes stay."""
        with _lock:
            self._save([s for s in self._load() if s["id"] != series_id])


def prompt_context(store: SeriesStore, episodes_dir: Path, job_id: str) -> dict | None:
    """What the prompts need to know about the episode's series, or None."""
    found = store.of_episode(job_id)
    if not found:
        return None
    series, part = found
    earlier = [e for e in (library.episode(episodes_dir, i) for i in series["episodes"][:part - 1]) if e]
    return {
        "id": series["id"],
        "title": series["title"],
        "arc": series["arc"],
        "part": part,
        "earlier": earlier,  # finished earlier parts, in order (mini summaries)
        "previous": earlier[-1] if earlier else None,  # its reference summary goes into the prompt in full
        "planned_after": len(series["episodes"]) - part,  # parts already queued after this one
    }
