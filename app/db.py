"""SQLite job store: the queue and index of episodes. Artifacts live in the job dirs."""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, fields
from datetime import datetime, timezone
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    id TEXT PRIMARY KEY,
    topic TEXT NOT NULL,
    origin TEXT NOT NULL DEFAULT 'web',
    status TEXT NOT NULL DEFAULT 'queued',
    stage TEXT NOT NULL DEFAULT '',
    message TEXT NOT NULL DEFAULT '',
    error TEXT NOT NULL DEFAULT '',
    from_stage TEXT,
    title TEXT NOT NULL DEFAULT '',
    duration_s INTEGER,
    cost_usd REAL,
    created_at TEXT NOT NULL,
    started_at TEXT,
    finished_at TEXT
);
CREATE INDEX IF NOT EXISTS jobs_status ON jobs(status, created_at);
"""

ACTIVE = ("queued", "running")


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@dataclass
class Job:
    id: str
    topic: str
    origin: str
    status: str  # queued | running | done | failed | cancelled
    stage: str
    message: str
    error: str
    from_stage: str | None
    title: str
    duration_s: int | None
    cost_usd: float | None
    created_at: str
    started_at: str | None
    finished_at: str | None

    @property
    def active(self) -> bool:
        return self.status in ACTIVE

    @property
    def display_title(self) -> str:
        return self.title or self.topic


class JobStore:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        with self._connect() as conn:
            conn.executescript(SCHEMA)

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        # Autocommit; claim_next uses an explicit transaction.
        conn = sqlite3.connect(self.path, timeout=10, isolation_level=None)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
        finally:
            conn.close()

    def _job(self, row) -> Job | None:
        return Job(**{f.name: row[f.name] for f in fields(Job)}) if row else None

    def add(self, job_id: str, topic: str, origin: str = "web") -> Job:
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO jobs (id, topic, origin, created_at) VALUES (?, ?, ?, ?)",
                (job_id, topic, origin, now()),
            )
        return self.get(job_id)

    def get(self, job_id: str) -> Job | None:
        with self._connect() as conn:
            return self._job(conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone())

    def list(self, limit: int = 50) -> list[Job]:
        with self._connect() as conn:
            rows = conn.execute("SELECT * FROM jobs ORDER BY created_at DESC, id DESC LIMIT ?", (limit,))
            return [self._job(r) for r in rows]

    def update(self, job_id: str, **values) -> None:
        if not values:
            return
        columns = ", ".join(f"{k} = ?" for k in values)
        with self._connect() as conn:
            conn.execute(f"UPDATE jobs SET {columns} WHERE id = ?", (*values.values(), job_id))

    def claim_next(self) -> Job | None:
        """Atomically move the oldest queued job to running."""
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT * FROM jobs WHERE status = 'queued' ORDER BY created_at, id LIMIT 1"
            ).fetchone()
            if row is None:
                conn.execute("COMMIT")
                return None
            conn.execute(
                "UPDATE jobs SET status = 'running', started_at = ?, error = '', message = '' WHERE id = ?",
                (now(), row["id"]),
            )
            conn.execute("COMMIT")
        return self.get(row["id"])

    def requeue_interrupted(self) -> int:
        """Jobs left 'running' by a crash or restart resume from their artifacts."""
        with self._connect() as conn:
            return conn.execute("UPDATE jobs SET status = 'queued' WHERE status = 'running'").rowcount

    def delete(self, job_id: str) -> None:
        with self._connect() as conn:
            conn.execute("DELETE FROM jobs WHERE id = ?", (job_id,))
