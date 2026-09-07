"""Process ownership inside a background worker (never inherited through env)."""

from contextvars import ContextVar

in_job_worker: ContextVar[bool] = ContextVar("in_job_worker", default=False)
current_job: ContextVar[str | None] = ContextVar("current_job", default=None)
