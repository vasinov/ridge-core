"""Shared job handoff primitives and process-local ownership (not inherited via env)."""

import json
from contextvars import ContextVar
from datetime import UTC, datetime

in_job_worker: ContextVar[bool] = ContextVar("in_job_worker", default=False)
current_job: ContextVar[str | None] = ContextVar("current_job", default=None)

STARTUP_SECONDS = 30
GRACE_SECONDS = 5
KILL_SECONDS = 5
POLL_SECONDS = 0.05


def timestamp() -> str:
    return datetime.now(UTC).isoformat()


def encode_json(value: object) -> str:
    return json.dumps(value, separators=(",", ":"), sort_keys=True)
