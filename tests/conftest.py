"""Shared harness for this app's tests.

**One autouse fixture, and it exists to stop a test from touching
production.** Since ``notion_app/apmt.py`` landed, ``POST /settings`` and
``POST /logout`` also talk to agents-platform-multitenant (through
aw-app-agents-platform-runners over loopback). This suite runs INSIDE a real
workspace container, where ``AW_WORKSPACE_API_KEY`` is in the environment and
that relay is installed and pointed at the live AP-MT — so a test calling
``/logout`` with the real code path would delete the real tenant's real
Notion token, and a test calling ``/settings`` would push a fake one over it.
That is not hypothetical: every existing test in ``test_routes.py`` calls one
of those two routes.

``block_apmt_network`` therefore neutralises the one function every outbound
call funnels through, for every test, by default. It raises
``ApmtNotConfigured`` — the same thing a workspace with no relay reports —
so the routes take their "there is nothing to sync" path, which is exactly
the shape these tests were written against.

A test that wants to exercise the sync path asks for the fixture and
overrides it (see ``test_apmt.py``), rather than the protection being
opt-in and forgotten.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from notion_app import apmt as apmt_mod  # noqa: E402


@pytest.fixture(autouse=True)
def block_apmt_network(monkeypatch):
    """No test makes a real call to the AP-MT relay unless it says so."""
    def _refuse(method, path, body=None):
        raise apmt_mod.ApmtNotConfigured(
            "blocked by tests/conftest.py — a test must not call the real "
            f"AP-MT relay ({method} {path})")

    monkeypatch.setattr(apmt_mod, "_call", _refuse)
    return monkeypatch


@pytest.fixture(autouse=True)
def block_schema_background(monkeypatch):
    """activate() and POST /settings kick off a background board-schema
    check against api.notion.com. No test may make that real call by
    accident; record the request instead. ``test_schema.py`` exercises the
    real function through ``schema_mod.REAL_ENSURE_IN_BACKGROUND``."""
    from notion_app.kanban import schema as schema_mod

    calls: list[tuple] = []
    monkeypatch.setattr(schema_mod, "REAL_ENSURE_IN_BACKGROUND",
                        schema_mod.ensure_in_background, raising=False)
    monkeypatch.setattr(schema_mod, "ensure_in_background",
                        lambda client, db_id: calls.append((client, db_id)))
    return calls
