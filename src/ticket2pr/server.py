"""HTTP service: receives Jira webhooks and exposes job status.

A job starts when the trigger label is *added* to a ticket (or the ticket is created with it),
not on every edit of a labelled ticket, so routine updates never re-run the agent.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import shutil
import time
from collections import OrderedDict
from contextlib import asynccontextmanager
from typing import TYPE_CHECKING, Annotated, Any

from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request, status
from fastapi.responses import JSONResponse

from ticket2pr import __version__
from ticket2pr.config import Settings
from ticket2pr.domain import Job, JobStatus, is_ticket_key
from ticket2pr.errors import ConfigurationError
from ticket2pr.pipeline import KEEP_MARKER
from ticket2pr.runtime import Runtime, build_runtime
from ticket2pr.worker import JobQueue

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Callable

logger = logging.getLogger(__name__)

_EVENTS = {"jira:issue_created", "jira:issue_updated"}
_MAX_BODY_BYTES = 1_000_000
# Jira's signature covers the body only: a captured delivery stays valid. Accepted bodies
# are remembered (bounded, per process) and old events refused, so replays start nothing.
_MAX_REMEMBERED_DELIVERIES = 10_000
_MAX_EVENT_AGE_MS = 60 * 60 * 1000


def create_app(
    settings: Settings | None = None,
    *,
    runtime_factory: Callable[[Settings], Runtime] = build_runtime,
) -> FastAPI:
    settings = settings or Settings()
    secret = settings.jira_webhook_secret.get_secret_value().encode()
    if not secret:
        raise ConfigurationError("serve needs T2P_JIRA_WEBHOOK_SECRET to authenticate webhooks")

    # Built eagerly so a configuration error stops `serve` with a clear message, before the
    # server starts listening.
    runtime = runtime_factory(settings)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        queue = start_queue(runtime, workers=settings.workers)
        app.state.queue = queue
        try:
            yield
        finally:
            await queue.stop()
            await runtime.aclose()

    app = FastAPI(title="ticket2pr", version=__version__, lifespan=lifespan)
    app.state.runtime = runtime
    deliveries = _Deliveries()

    def require_api_token(authorization: Annotated[str | None, Header()] = None) -> None:
        token = settings.api_token.get_secret_value()
        if not token:
            raise HTTPException(status.HTTP_404_NOT_FOUND)  # API disabled
        expected = f"Bearer {token}".encode()
        if not authorization or not hmac.compare_digest(authorization.encode(), expected):
            raise HTTPException(
                status.HTTP_401_UNAUTHORIZED, headers={"WWW-Authenticate": "Bearer"}
            )

    @app.get("/healthz")
    async def healthz() -> dict[str, str]:
        return {"status": "ok", "version": __version__}

    @app.post("/webhooks/jira")
    async def jira_webhook(
        request: Request,
        x_hub_signature: Annotated[str | None, Header()] = None,
    ) -> JSONResponse:
        body = await read_limited(request, _MAX_BODY_BYTES)
        if not verify_signature(secret, body, x_hub_signature):
            raise HTTPException(status.HTTP_401_UNAUTHORIZED, "invalid signature")
        try:
            payload = json.loads(body)
        except ValueError as exc:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "body is not JSON") from exc

        key, reason = trigger_from_event(payload, settings.trigger_label)
        if key is not None and (reason := deliveries.refusal(body, payload)):
            key = None
        if key is not None:  # whether or not it starts a job: a replay later must not either
            deliveries.remember(body)
        if key is None:
            return JSONResponse({"accepted": False, "reason": reason}, status.HTTP_200_OK)
        queue: JobQueue = request.app.state.queue
        job = queue.submit(key)
        if job is None:
            return JSONResponse(
                {"accepted": False, "reason": f"{key} already has a job in progress"},
                status.HTTP_200_OK,
            )
        logger.info("queued job %s for %s", job.id, key)
        return JSONResponse(
            {"accepted": True, "job_id": job.id, "ticket": key}, status.HTTP_202_ACCEPTED
        )

    @app.get("/jobs", dependencies=[Depends(require_api_token)])
    async def list_jobs(
        request: Request,
        limit: Annotated[int, Query(ge=1, le=500)] = 50,
        job_status: Annotated[JobStatus | None, Query(alias="status")] = None,
    ) -> list[Job]:
        runtime: Runtime = request.app.state.runtime
        return runtime.store.list(limit=limit, status=job_status)

    @app.get("/jobs/{job_id}", dependencies=[Depends(require_api_token)])
    async def get_job(request: Request, job_id: int) -> Job:
        runtime: Runtime = request.app.state.runtime
        job = runtime.store.get(job_id)
        if job is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND)
        return job

    return app


def start_queue(runtime: Runtime, *, workers: int) -> JobQueue:
    """Recover what a previous process left behind, then start the workers."""
    interrupted = runtime.store.fail_interrupted()
    if interrupted:
        logger.warning("marked %d interrupted job(s) as failed", interrupted)
    # No job runs yet: what is on disk was left by a crash or a kill mid-cleanup, except the
    # workspaces kept on purpose (--keep-workspace).
    for stale in runtime.settings.workspaces_dir.glob("job-*"):
        if not (stale / KEEP_MARKER).exists():
            shutil.rmtree(stale, ignore_errors=True)
    queue = JobQueue(pipeline=runtime.pipeline, store=runtime.store, workers=workers)
    pending = runtime.store.list(limit=10_000, status=JobStatus.QUEUED)
    resumed = 0
    for job in reversed(pending):  # oldest first
        # The database may have been written by untrusted code (unsandboxed mode): never take
        # a repository from it, re-resolve it from T2P_REPOS when the job runs, and never
        # build a tracker URL from a key the webhook would have refused.
        if not is_ticket_key(job.ticket_key):
            job.status, job.error = JobStatus.FAILED, "invalid ticket key in the job database"
            runtime.store.save(job)
            continue
        job.repo = None
        queue.enqueue(job)
        resumed += 1
    if resumed:
        logger.info("re-enqueued %d queued job(s)", resumed)
    queue.start()
    return queue


async def read_limited(request: Request, limit: int) -> bytes:
    """Read the body, refusing it as soon as it exceeds ``limit`` (before buffering it all)."""
    declared = request.headers.get("content-length")
    if declared is not None and (not declared.isdigit() or int(declared) > limit):
        raise HTTPException(status.HTTP_413_CONTENT_TOO_LARGE)
    chunks: list[bytes] = []
    size = 0
    async for chunk in request.stream():
        size += len(chunk)
        if size > limit:
            raise HTTPException(status.HTTP_413_CONTENT_TOO_LARGE)
        chunks.append(chunk)
    return b"".join(chunks)


def verify_signature(secret: bytes, body: bytes, header: str | None) -> bool:
    """Check Jira's ``X-Hub-Signature: sha256=<hex HMAC of the raw body>`` header."""
    if not header or not header.startswith("sha256="):
        return False
    expected = hmac.new(secret, body, hashlib.sha256).hexdigest()
    # As bytes: compare_digest refuses non-ASCII str, which would turn junk into a 500.
    return hmac.compare_digest(header.removeprefix("sha256=").encode(), expected.encode())


class _Deliveries:
    """Signed, triggering bodies already received (bounded, per process)."""

    def __init__(self) -> None:
        self._accepted: OrderedDict[str, None] = OrderedDict()

    def refusal(self, body: bytes, payload: dict[str, Any]) -> str:
        """Why this delivery must not start a job, or ``""``."""
        if hashlib.sha256(body).hexdigest() in self._accepted:
            return "duplicate delivery"
        stamp = payload.get("timestamp")  # Jira stamps events in epoch milliseconds
        if isinstance(stamp, int) and time.time() * 1000 - stamp > _MAX_EVENT_AGE_MS:
            return "stale event"
        return ""

    def remember(self, body: bytes) -> None:
        self._accepted[hashlib.sha256(body).hexdigest()] = None
        if len(self._accepted) > _MAX_REMEMBERED_DELIVERIES:
            self._accepted.popitem(last=False)


def trigger_from_event(payload: Any, label: str) -> tuple[str | None, str]:
    """Return ``(ticket_key, "")`` when the event should start a job, else ``(None, reason)``."""
    if not isinstance(payload, dict):
        return None, "payload is not an object"
    event = payload.get("webhookEvent")
    if event not in _EVENTS:
        return None, f"ignored event {event!r}"
    issue = payload.get("issue") or {}
    key = issue.get("key")
    if not isinstance(key, str) or not is_ticket_key(key):
        return None, "missing or invalid issue key"
    labels = (issue.get("fields") or {}).get("labels") or []
    if label not in labels:
        return None, f"{key} lacks the {label!r} label"
    if event == "jira:issue_updated" and not _label_added(payload, label):
        return None, f"{label!r} was not added by this update"
    return key, ""


def _label_added(payload: dict[str, Any], label: str) -> bool:
    for item in (payload.get("changelog") or {}).get("items") or []:
        if item.get("field") != "labels":
            continue
        before = set((item.get("fromString") or "").split())
        after = set((item.get("toString") or "").split())
        if label in after - before:
            return True
    return False
