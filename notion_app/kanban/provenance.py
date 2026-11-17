"""Trusted per-request Kanban provenance context.

The value is populated only from the gateway-forwarded HTTP header at the
route boundary.  It is deliberately not a tool argument: a model must never
be able to claim another run's identity.
"""
from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar


CALLER_RUN_ID_HEADER = "X-Aw-Caller-Run-Id"
_caller_run_id: ContextVar[str] = ContextVar("kanban_caller_run_id", default="")


def current_run_id() -> str:
    return _caller_run_id.get()


@contextmanager
def caller_run(run_id: str | None):
    token = _caller_run_id.set((run_id or "").strip())
    try:
        yield
    finally:
        _caller_run_id.reset(token)


__all__ = ["CALLER_RUN_ID_HEADER", "caller_run", "current_run_id"]
