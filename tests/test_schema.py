"""Coverage for notion_app/kanban/schema.py — the additive board-schema
repair that creates the properties this app relies on (CreateRunId,
UpdateRunIds, ...) on a board that predates them.

Run: python3 -m pytest tests/test_schema.py
"""
from __future__ import annotations

import asyncio
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from fastapi.testclient import TestClient  # noqa: E402
from test_routes import FakeCtx  # noqa: E402

from notion_app import routes  # noqa: E402
from notion_app.kanban import schema as schema_mod  # noqa: E402
from notion_app.kanban.cards import KanbanBoard  # noqa: E402
from notion_app.kanban.client import NotionError  # noqa: E402
from notion_app.kanban.provenance import caller_run  # noqa: E402
from notion_app.plugin import NotionAppPlugin  # noqa: E402

DB = "db-123"


class SchemaClient:
    """Fake NotionClient: a mutable schema, PATCH /databases applies it."""

    configured = True

    def __init__(self, schema: dict | None = None, *, fail: Exception | None = None,
                 drop_on_patch: set[str] | None = None):
        self.schema = {k: dict(v) for k, v in (schema or {}).items()}
        self.fail = fail
        self.drop_on_patch = drop_on_patch or set()
        self.writes: list[tuple] = []

    def database_schema(self, db_id):
        if self.fail:
            raise self.fail
        return {k: dict(v) for k, v in self.schema.items()}

    def request(self, method, path, body=None):
        self.writes.append((method, path, body))
        for name, spec in body["properties"].items():
            if name in self.drop_on_patch:
                continue
            (ptype,) = spec.keys()
            self.schema[name] = {"type": ptype, ptype: spec[ptype]}
        return {}


def _full_schema() -> dict:
    s = {n: {"type": t} for n, t in schema_mod.MANAGED_PROPERTIES.items()}
    s["Name"] = {"type": "title"}
    s["Status"] = {"type": "status"}
    return s


def test_missing_properties_are_created_via_patch_databases():
    base = {"Nome": {"type": "title"}, "Status": {"type": "status"},
            "FindingKey": {"type": "rich_text"}}
    client = SchemaClient(base)

    r = schema_mod.ensure_schema(client, DB)

    assert r["ok"] is True and r["error"] is None
    assert "CreateRunId" in r["created"] and "UpdateRunIds" in r["created"]
    assert "FindingKey" in r["present"] and "FindingKey" not in r["created"]
    assert len(client.writes) == 1
    method, path, body = client.writes[0]
    assert (method, path) == ("PATCH", f"/databases/{DB}")
    assert body["properties"]["CreateRunId"] == {"rich_text": {}}
    assert body["properties"]["is_live"] == {"checkbox": {}}
    assert body["properties"]["LastSeenAt"] == {"date": {}}
    assert "FindingKey" not in body["properties"]  # never re-sent
    assert client.schema["UpdateRunIds"]["type"] == "rich_text"


def test_already_present_is_a_noop():
    client = SchemaClient(_full_schema())
    r = schema_mod.ensure_schema(client, DB)
    assert r["ok"] is True
    assert r["created"] == [] and r["missing"] == [] and r["conflicts"] == []
    assert client.writes == []


def test_wrong_type_is_a_conflict_and_left_untouched():
    schema = _full_schema()
    schema["UpdateRunIds"] = {"type": "number"}
    del schema["CreateRunId"]
    client = SchemaClient(schema)

    r = schema_mod.ensure_schema(client, DB)

    assert r["conflicts"] == [{"property": "UpdateRunIds", "expected": "rich_text",
                               "actual": "number"}]
    assert r["created"] == ["CreateRunId"]
    (_m, _p, body), = client.writes
    assert set(body["properties"]) == {"CreateRunId"}  # conflict never written
    assert client.schema["UpdateRunIds"]["type"] == "number"
    assert schema_mod.drift(r)["ok"] is False


def test_dry_run_writes_nothing():
    client = SchemaClient({"Status": {"type": "status"}})
    r = schema_mod.ensure_schema(client, DB, dry_run=True)
    assert r["ok"] is True and r["dry_run"] is True
    assert "CreateRunId" in r["missing"] and r["created"] == []
    assert client.writes == []
    assert "CreateRunId" in schema_mod.drift(r)["missing"]


def test_api_error_returns_ok_false_instead_of_raising():
    client = SchemaClient(fail=NotionError(404, '{"code":"object_not_found"}'))
    r = schema_mod.ensure_schema(client, DB)
    assert r["ok"] is False and "object_not_found" in r["error"]


def test_unexpected_exception_is_fail_open_too():
    client = SchemaClient(fail=RuntimeError("boom"))
    r = schema_mod.ensure_schema(client, DB)
    assert r["ok"] is False and "boom" in r["error"]


def test_property_that_did_not_land_is_reported():
    client = SchemaClient({"Status": {"type": "status"}}, drop_on_patch={"UpdateRunIds"})
    r = schema_mod.ensure_schema(client, DB)
    assert r["ok"] is False and "UpdateRunIds" in r["error"]
    assert "UpdateRunIds" not in r["created"]


def test_no_database_id_is_reported_not_raised():
    r = schema_mod.ensure_schema(SchemaClient(), "")
    assert r["ok"] is False and "not configured" in r["error"]


def test_create_card_works_after_ensure_on_an_old_board():
    """End to end at the board layer: the exact failure that motivated this."""
    from test_kanban import FakeClient  # noqa: E402
    from test_kanban import FakeCtx as KCtx

    from notion_app.kanban.config import KanbanConfig

    old = {"Name": {"type": "title"}, "Status": {"type": "select"},
           "FindingKey": {"type": "rich_text"}}
    sc = SchemaClient(old)
    fake = FakeClient({("GET", f"/databases/{DB}"): lambda _b: {"properties": sc.schema},
                       ("POST", "/pages"): {"id": "page-1", "url": "u"}})
    board = KanbanBoard(fake, KanbanConfig(KCtx({"kanban_database_id": DB})))
    with caller_run("run-1"):
        assert board.create_card(title="x")["ok"] is False

    assert schema_mod.ensure_schema(sc, DB)["ok"] is True
    with caller_run("run-1"):
        r = board.create_card(title="x")
    assert r["ok"] is True
    (_m, _p, body), = [c for c in fake.calls if c[:2] == ("POST", "/pages")]
    assert "CreateRunId" in body["properties"]


# ── wiring ──────────────────────────────────────────────────────────────

class _FakeRoutes:
    def register(self, app) -> None:
        pass


def test_activate_starts_the_schema_check(block_schema_background):
    with tempfile.TemporaryDirectory() as tmp:
        ctx = FakeCtx(package_dir=tmp)
        ctx.routes = _FakeRoutes()
        ctx.secrets.write("notion_token", "ntn_x")
        ctx.config = {"kanban_database_id": DB}
        asyncio.run(NotionAppPlugin().activate(ctx))
    assert [db for _c, db in block_schema_background] == [DB]


def test_activate_is_fail_open_when_the_schema_check_explodes(monkeypatch):
    def boom(client, db_id):
        raise RuntimeError("thread start failed")
    monkeypatch.setattr(schema_mod, "ensure_in_background", boom)
    with tempfile.TemporaryDirectory() as tmp:
        ctx = FakeCtx(package_dir=tmp)
        ctx.routes = _FakeRoutes()
        ctx.secrets.write("notion_token", "ntn_x")
        ctx.config = {"kanban_database_id": DB}
        asyncio.run(NotionAppPlugin().activate(ctx))  # must not raise


def test_config_change_rechecks_only_when_the_board_changed(block_schema_background):
    with tempfile.TemporaryDirectory() as tmp:
        ctx = FakeCtx(package_dir=tmp)
        ctx.routes = _FakeRoutes()
        ctx.secrets.write("notion_token", "ntn_x")
        ctx.config = {"kanban_database_id": DB}
        plugin = NotionAppPlugin()
        asyncio.run(plugin.activate(ctx))
        asyncio.run(plugin.on_config_changed(ctx))
        ctx.config = {"kanban_database_id": "db-new"}
        asyncio.run(plugin.on_config_changed(ctx))
    assert [db for _c, db in block_schema_background] == [DB, "db-new"]


def test_background_helper_skips_without_token_or_board():
    real = schema_mod.REAL_ENSURE_IN_BACKGROUND
    assert real(SchemaClient(), "") is None
    unconfigured = SchemaClient()
    unconfigured.configured = False
    assert real(unconfigured, DB) is None
    t = real(SchemaClient(_full_schema()), DB)
    t.join(5)
    assert schema_mod.last_result()["ok"] is True


def test_ensure_route_and_status_drift(monkeypatch):
    calls = []

    def fake_ensure(client, db_id, dry_run=False):
        calls.append((db_id, dry_run))
        return schema_mod._record({"ok": True, "dry_run": dry_run, "missing": ["CreateRunId"],
                                   "created": [], "present": [], "conflicts": [],
                                   "error": None})

    monkeypatch.setattr(schema_mod, "ensure_schema", fake_ensure)
    with tempfile.TemporaryDirectory() as tmp:
        ctx = FakeCtx(package_dir=tmp)
        ctx.config = {"kanban_database_id": DB}
        http = TestClient(routes.build_routes(ctx))
        r = http.post("/kanban/schema/ensure?dry_run=true")
        assert r.status_code == 200 and r.json()["dry_run"] is True
        assert calls == [(DB, True)]
        status = http.get("/status").json()
        assert status["kanban"]["schema"]["missing"] == ["CreateRunId"]
        assert status["kanban"]["schema"]["ok"] is False


def test_ensure_route_without_board_is_503():
    with tempfile.TemporaryDirectory() as tmp:
        http = TestClient(routes.build_routes(FakeCtx(package_dir=tmp)))
        r = http.post("/kanban/schema/ensure")
        assert r.status_code == 503 and r.json()["ok"] is False


def test_create_card_error_points_at_the_ensure_route():
    from test_kanban import FakeClient  # noqa: E402
    from test_kanban import FakeCtx as KCtx

    from notion_app.kanban.config import KanbanConfig
    board = KanbanBoard(FakeClient({("GET", f"/databases/{DB}"):
                                    {"properties": {"Name": {"type": "title"}}}}),
                        KanbanConfig(KCtx({"kanban_database_id": DB})))
    with caller_run("run-1"):
        r = board.create_card(title="x")
    assert "/kanban/schema/ensure" in r["error"]
