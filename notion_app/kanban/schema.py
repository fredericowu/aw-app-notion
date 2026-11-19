"""Board-schema validation + additive repair for the Kanban database.

The app reads and writes a set of named properties on the board (provenance,
dedup, sweep markers, deploy markers, ...). Nothing created them: a board
set up before a given release simply lacked the newer columns, and the first
symptom was ``create_kanban_task`` refusing with "board schema is missing
managed provenance properties" while comments / ``set_kanban_property``
failed on the ``UpdateRunIds`` stamp.

:func:`ensure_schema` closes that gap. It compares the live board against
:data:`MANAGED_PROPERTIES` and creates whatever is missing. It is strictly
**additive**: it never deletes, renames or retypes an existing property — a
property that exists under a managed name with a different type is reported
as a conflict and left exactly as it is, because the data in it belongs to
the board owner, not to this app.

Write path: :class:`~notion_app.kanban.client.NotionClient` pins
``Notion-Version: 2022-06-28``, under which a database's properties are
updated with ``PATCH /databases/{id}`` (the data-source split only applies
from ``2025-09-03``). Verified against the live board.

Deliberately NOT managed here:

* ``Status`` and the title property — every board has them already, and a
  board without them is not a Kanban board this app can repair.
* ``ParentCard`` / ``ChildCards`` — a two-way self relation. Creating one
  means choosing the relation's synced-property shape for someone else's
  board; the code already treats it as optional, so it stays a manual step.
"""
from __future__ import annotations

import logging
import threading
import time
from typing import Any

from .client import NotionError

log = logging.getLogger("aw_apps.notion.kanban")

# name → Notion property type. Order is cosmetic (Notion appends new columns
# in request order). Only properties the app's own code reads or writes.
MANAGED_PROPERTIES: dict[str, str] = {
    # provenance — create_card / _record_update_run refuse without these
    "CreateRunId": "rich_text",
    "UpdateRunIds": "rich_text",
    # card creation / dedup / routing (cards.py create_card, get_card)
    "FindingKey": "rich_text",
    "Source": "rich_text",
    "AgentSlug": "rich_text",
    "WorkflowSlug": "rich_text",
    "TargetSlug": "rich_text",
    "PlanSlug": "rich_text",
    "CheckHint": "rich_text",
    "Priority": "select",
    "Tags": "multi_select",
    "OccurrenceCount": "number",
    "LastSeenAt": "date",
    # QA + sweep bookkeeping (set_qa_status, list_cards_need_attention)
    "QAStatus": "select",
    "LastSweptAt": "date",
    "LastHumanPingAt": "date",
    "HumanPingCount": "number",
    # deploy bookkeeping (set_kanban_property + kanban_check_deployed)
    "DeployComponent": "rich_text",
    "DeployToVersion": "rich_text",
    "is_live": "checkbox",
    "is_deployment_needed": "checkbox",
}

# Select options seeded on creation only. Existing options are never touched.
_SELECT_OPTIONS: dict[str, list[str]] = {
    "Priority": ["High", "Medium", "Low"],
    "QAStatus": ["need_human", "ready_to_deploy", "done"],
}

_last_result: dict[str, Any] = {}
_last_lock = threading.Lock()


def _property_spec(name: str, ptype: str) -> dict:
    if ptype in ("select", "multi_select"):
        return {ptype: {"options": [{"name": o} for o in _SELECT_OPTIONS.get(name, [])]}}
    if ptype == "number":
        return {"number": {"format": "number"}}
    return {ptype: {}}


def _record(result: dict) -> dict:
    with _last_lock:
        _last_result.clear()
        _last_result.update(result, checked_at=time.time())
    return result


def last_result() -> dict:
    """The most recent :func:`ensure_schema` outcome in this process, or
    ``{}`` if it never ran. Cheap — what ``GET /status`` reports, so polling
    the status widget never costs a Notion call."""
    with _last_lock:
        return dict(_last_result)


def ensure_schema(client, db_id: str, dry_run: bool = False) -> dict:
    """Bring the board's schema up to :data:`MANAGED_PROPERTIES`, additively.

    Returns ``{ok, dry_run, present, missing, created, conflicts, error}``.
    ``missing`` is what was absent before this call; ``created`` is what this
    call actually added (always empty on ``dry_run``). Never raises — it runs
    from ``activate()``, which must not be blocked by a Notion outage.
    """
    result: dict[str, Any] = {"ok": False, "dry_run": dry_run, "database_id": db_id or "",
                              "present": [], "missing": [], "created": [],
                              "conflicts": [], "error": None}
    if not db_id:
        result["error"] = "kanban_database_id is not configured"
        return _record(result)
    try:
        schema = client.database_schema(db_id)
        for name, want in MANAGED_PROPERTIES.items():
            spec = schema.get(name)
            if spec is None:
                result["missing"].append(name)
            elif spec.get("type") != want:
                result["conflicts"].append(
                    {"property": name, "expected": want, "actual": spec.get("type")})
            else:
                result["present"].append(name)

        if result["missing"] and not dry_run:
            body = {"properties": {n: _property_spec(n, MANAGED_PROPERTIES[n])
                                   for n in result["missing"]}}
            client.request("PATCH", f"/databases/{db_id}", body)
            # Confirm against the live board rather than trusting a 200.
            after = client.database_schema(db_id)
            result["created"] = [n for n in result["missing"]
                                 if after.get(n, {}).get("type") == MANAGED_PROPERTIES[n]]
            not_landed = sorted(set(result["missing"]) - set(result["created"]))
            if not_landed:
                result["error"] = "properties not present after update: " + ", ".join(not_landed)
                return _record(result)
        result["ok"] = True
    except NotionError as exc:
        result["error"] = str(exc)
    except Exception as exc:  # noqa: BLE001 — fail-open by contract
        log.warning("kanban: ensure_schema crashed", exc_info=True)
        result["error"] = f"{type(exc).__name__}: {exc}"
    if result["created"]:
        log.info("kanban: board schema repaired, created %s", result["created"])
    if result["conflicts"]:
        log.warning("kanban: board schema conflicts left untouched: %s", result["conflicts"])
    if result["error"]:
        log.warning("kanban: ensure_schema failed: %s", result["error"])
    return _record(result)


def ensure_in_background(client, db_id: str) -> threading.Thread | None:
    """Fire-and-forget :func:`ensure_schema` on a daemon thread, so a slow or
    unreachable Notion never delays boot or a request. ``None`` when there is
    nothing to check (no board id or no token)."""
    if not db_id or not getattr(client, "configured", False):
        return None
    t = threading.Thread(target=ensure_schema, args=(client, db_id),
                         name="notion-kanban-ensure-schema", daemon=True)
    t.start()
    return t


def drift(result: dict) -> dict:
    """Compact view for status/doctor: what is still wrong with the board."""
    if not result:
        return {"checked": False}
    missing = sorted(set(result.get("missing", [])) - set(result.get("created", [])))
    # A dry run reports ok=True while properties are still missing — drift is
    # about the board, not about whether the check itself succeeded.
    return {"checked": True,
            "ok": bool(result.get("ok")) and not missing and not result.get("conflicts"),
            "missing": missing, "conflicts": result.get("conflicts", []),
            "error": result.get("error"), "checked_at": result.get("checked_at")}


__all__ = ["MANAGED_PROPERTIES", "drift", "ensure_in_background", "ensure_schema",
           "last_result"]
