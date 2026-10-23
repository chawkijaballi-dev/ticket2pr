"""In-process job queue: a fixed pool of asyncio workers draining a FIFO.

Deliberately simple. Jobs are persisted in SQLite before they are queued: on start the
service re-enqueues the ``queued`` ones, and the runs a crash cut short (left ``running``)
are marked failed. A graceful stop cancels in-flight runs, which record themselves as
interrupted. Swapping this for Celery/RQ/Arq means re-implementing ``submit``, ``enqueue``,
``start`` and ``stop`` (restart recovery in ``server.start_queue`` calls ``enqueue`` directly).
"""

from __future__ import annotations

import asyncio
import logging
from contextlib import suppress

from ticket2pr.domain import Job, JobStatus
from ticket2pr.pipeline import Pipeline
from ticket2pr.store import JobStore

logger = logging.getLogger(__name__)


class JobQueue:
    def __init__(self, *, pipeline: Pipeline, store: JobStore, workers: int) -> None:
        self._pipeline = pipeline
        self._store = store
        self._workers = workers
        self._queue: asyncio.Queue[Job] = asyncio.Queue()
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

    def submit(self, ticket_key: str) -> Job | None:
        """Persist and enqueue a job. ``None`` when the ticket already has one in flight."""
        job = self._store.claim(ticket_key, None)
        if job is not None:
            self.enqueue(job)
        return job

    def enqueue(self, job: Job) -> None:
        """Queue an already persisted job (e.g. one left ``queued`` by a previous process)."""
        self._queue.put_nowait(job)

    async def join(self) -> None:
        await self._queue.join()

    async def _work(self) -> None:
        while True:
            job = await self._queue.get()
            try:
                await self._pipeline.run(job)
            except Exception as exc:  # Pipeline.run records expected failures; last resort
                logger.exception("worker crashed on job %s", job.id)
                job.status = JobStatus.FAILED
                job.error = f"internal error: {type(exc).__name__}"
                with suppress(Exception):
                    self._store.save(job)
            finally:
                self._queue.task_done()
