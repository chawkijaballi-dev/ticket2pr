"""In-process job queue: a fixed pool of asyncio workers draining a FIFO.

Deliberately simple. Jobs are persisted in SQLite before they are queued, so a crash loses at
most the in-flight runs, which ``JobStore.fail_interrupted`` marks as failed on restart.
Swapping this for Celery/RQ/Arq only means re-implementing ``submit``.
"""

from __future__ import annotations

import asyncio
import logging
from contextlib import suppress

from ticket2pr.domain import Job, JobStatus
from ticket2pr.pipeline import Pipeline, RunOptions
from ticket2pr.store import JobStore

logger = logging.getLogger(__name__)


class JobQueue:
    def __init__(self, *, pipeline: Pipeline, store: JobStore, workers: int) -> None:
        self._pipeline = pipeline
        self._store = store
        self._workers = workers
        self._queue: asyncio.Queue[tuple[Job, RunOptions]] = asyncio.Queue()
        self._tasks: list[asyncio.Task[None]] = []

    def start(self) -> None:
        if self._tasks:
            raise RuntimeError("queue already started")
        self._tasks = [
            asyncio.create_task(self._work(), name=f"ticket2pr-worker-{index}")
            for index in range(self._workers)
        ]

    async def stop(self) -> None:
        for task in self._tasks:
            task.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        self._tasks = []

    def submit(self, ticket_key: str, *, repo: str | None = None) -> Job | None:
        """Persist and enqueue a job. ``None`` when the ticket already has one in flight."""
        job = self._store.claim(ticket_key, repo)
        if job is not None:
            self._queue.put_nowait((job, RunOptions()))
        return job

    async def join(self) -> None:
        await self._queue.join()

    async def _work(self) -> None:
        while True:
            job, options = await self._queue.get()
            try:
                await self._pipeline.run(job, options)
            except Exception as exc:  # Pipeline.run records expected failures; last resort
                logger.exception("worker crashed on job %s", job.id)
                job.status = JobStatus.FAILED
                job.error = f"internal error: {type(exc).__name__}"
                with suppress(Exception):
                    self._store.save(job)
            finally:
                self._queue.task_done()
