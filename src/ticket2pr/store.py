"""SQLite-backed job history. Thread-safe; one connection per store."""

from __future__ import annotations

import sqlite3
import threading
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Self

from ticket2pr.domain import Job, JobStatus, Usage, utcnow

if TYPE_CHECKING:
    from types import TracebackType

_SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    ticket_key    TEXT NOT NULL,
    repo          TEXT,
    status        TEXT NOT NULL,
    branch        TEXT,
    pr_url        TEXT,
    usage_json    TEXT NOT NULL,
    tests_passed  INTEGER,
    error         TEXT,
    created_at    TEXT NOT NULL,
    updated_at    TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS jobs_ticket ON jobs (ticket_key, status);
"""

_ACTIVE = (JobStatus.QUEUED.value, JobStatus.RUNNING.value)


class JobStore:
    def __init__(self, path: Path | str) -> None:
        if str(path) != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(path), check_same_thread=False, isolation_level=None)
        self._conn.row_factory = sqlite3.Row
        self._lock = threading.Lock()
        with self._lock:
            self._conn.executescript(_SCHEMA)

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.close()

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    def claim(self, ticket_key: str, repo: str | None) -> Job | None:
        """Atomically create a queued job, unless one is already queued or running for the ticket.

        Returns ``None`` when the ticket is already being handled, which makes webhook
        redeliveries and duplicate events idempotent.
        """
        now = utcnow().isoformat()
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                busy = self._conn.execute(
                    "SELECT 1 FROM jobs WHERE ticket_key = ? AND status IN (?, ?)",
                    (ticket_key, *_ACTIVE),
                ).fetchone()
                if busy:
                    self._conn.execute("ROLLBACK")
                    return None
                cursor = self._conn.execute(
                    "INSERT INTO jobs (ticket_key, repo, status, usage_json, created_at, "
                    "updated_at) VALUES (?, ?, ?, ?, ?, ?)",
                    (ticket_key, repo, JobStatus.QUEUED.value, Usage().model_dump_json(), now, now),
                )
                self._conn.execute("COMMIT")
            except BaseException:
                self._conn.execute("ROLLBACK")
                raise
        job_id = cursor.lastrowid
        assert job_id is not None  # noqa: S101 - sqlite always returns it after INSERT
        job = self.get(job_id)
        assert job is not None  # noqa: S101
        return job

    def save(self, job: Job) -> Job:
        if job.id is None:
            raise ValueError("save() needs a persisted job; use claim() to create one")
        job.updated_at = utcnow()
        with self._lock:
            self._conn.execute(
                "UPDATE jobs SET repo = ?, status = ?, branch = ?, pr_url = ?, usage_json = ?, "
                "tests_passed = ?, error = ?, updated_at = ? WHERE id = ?",
                (
                    job.repo,
                    job.status.value,
                    job.branch,
                    job.pr_url,
                    job.usage.model_dump_json(),
                    None if job.tests_passed is None else int(job.tests_passed),
                    job.error,
                    job.updated_at.isoformat(),
                    job.id,
                ),
            )
        return job

    def get(self, job_id: int) -> Job | None:
        with self._lock:
            row = self._conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
        return _to_job(row) if row else None

    def list(self, *, limit: int = 50, status: JobStatus | None = None) -> list[Job]:
        query = "SELECT * FROM jobs"
        params: tuple[object, ...] = ()
        if status is not None:
            query += " WHERE status = ?"
            params = (status.value,)
        query += " ORDER BY id DESC LIMIT ?"
        with self._lock:
            rows = self._conn.execute(query, (*params, limit)).fetchall()
        return [_to_job(row) for row in rows]

    def fail_interrupted(self, reason: str = "interrupted by a service restart") -> int:
        """Mark jobs a previous process left ``running`` as failed. Returns the count.

        Queued jobs are untouched: the service re-enqueues them on start.
        """
        with self._lock:
            cursor = self._conn.execute(
                "UPDATE jobs SET status = ?, error = ?, updated_at = ? WHERE status = ?",
                (JobStatus.FAILED.value, reason, utcnow().isoformat(), JobStatus.RUNNING.value),
            )
        return cursor.rowcount


def _to_job(row: sqlite3.Row) -> Job:
    tests = row["tests_passed"]
    return Job(
        id=row["id"],
        ticket_key=row["ticket_key"],
        repo=row["repo"],
        status=JobStatus(row["status"]),
        branch=row["branch"],
        pr_url=row["pr_url"],
        usage=Usage.model_validate_json(row["usage_json"]),
        tests_passed=None if tests is None else bool(tests),
        error=row["error"],
        created_at=datetime.fromisoformat(row["created_at"]),
        updated_at=datetime.fromisoformat(row["updated_at"]),
    )
