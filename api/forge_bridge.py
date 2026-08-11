"""Tool forge bridge for the WebUI.

Serves the hermes-tool-forge plugin store to the Memory panel under
``/api/forge`` — list forged tools with their status, view code, test output,
promote to skill, or delete.

Design notes (same pattern as cognitive_bridge):

- The Hermes plugin lives at ``~/.hermes/plugins/tool_forge/``. Its
  ``__init__.py`` imports Hermes Agent internals, so this bridge loads only
  ``store.py`` under a synthetic package name — the WebUI process never
  imports Hermes Agent code.
- Profile-aware: resolves the active Hermes home.
- The plugin's ForgeStore is thread-safe (RLock + PRAGMA busy_timeout).
"""

from __future__ import annotations

import importlib.util
import logging
import sys
import threading
import types
from pathlib import Path
from typing import Any, Dict, List, Optional

from api.helpers import bad, j

logger = logging.getLogger(__name__)

_store_lock = threading.Lock()
_store_instances: Dict[str, Any] = {}


def _resolve_hermes_home() -> Path:
    """Return the active profile's Hermes home (fall back to ~/.hermes)."""
    try:
        from api.profiles import get_active_hermes_home

        home = get_active_hermes_home()
        if home:
            return Path(home)
    except Exception:
        logger.debug("get_active_hermes_home failed; using ~/.hermes", exc_info=True)
    return Path.home() / ".hermes"


def _plugin_dir(home: Path) -> Path:
    return home / "plugins" / "tool_forge"


def _db_path(home: Path) -> Path:
    return home / "tool_forge" / "forge.db"


def _load_store_module(home: Path):
    """Load ``store`` from the plugin dir without running __init__.py."""
    plugin_dir = _plugin_dir(home)
    pkg_name = "_forge_webui"
    store_mod = sys.modules.get(pkg_name + ".store")
    if store_mod is not None:
        return store_mod

    pkg = types.ModuleType(pkg_name)
    pkg.__path__ = [str(plugin_dir)]  # type: ignore[attr-defined]
    sys.modules[pkg_name] = pkg

    # store.py does `from .__init__ import ...` — but we can't load __init__.
    # Instead, we import store.py directly. It only uses stdlib (json, sqlite3,
    # threading, time, os, uuid, hashlib) — no Hermes internals.
    store_mod = _load_single_module(pkg_name + ".store", plugin_dir / "store.py")
    return store_mod


def _load_single_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise ImportError(f"cannot load {path}")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


def _get_store():
    """Return a connected ForgeStore for the active profile (lazy, per-home)."""
    home = _resolve_hermes_home()
    key = str(home)
    with _store_lock:
        cached = _store_instances.get(key)
        if cached is not None:
            return cached
        db_path = _db_path(home)
        plugin_dir = _plugin_dir(home)
        if not plugin_dir.is_dir():
            raise RuntimeError(
                "Tool forge plugin not installed "
                f"(missing {plugin_dir})."
            )
        if not db_path.exists():
            raise RuntimeError(
                "Tool forge database not found at "
                f"{db_path}. Restart Hermes with tool_forge enabled "
                "to initialize it."
            )
        store_module = _load_store_module(home)
        store = store_module.ForgeStore(db_path)
        store.connect()
        _store_instances[key] = store
        return store


def _row_to_payload(row: Dict[str, Any]) -> Dict[str, Any]:
    """Convert a store row to a JSON-serializable payload."""
    import json as _json
    params_schema = row.get("params_schema")
    if isinstance(params_schema, str):
        try:
            params_schema = _json.loads(params_schema)
        except Exception:
            params_schema = {}
    metadata = row.get("metadata")
    if isinstance(metadata, str):
        try:
            metadata = _json.loads(metadata)
        except Exception:
            metadata = {}
    return {
        "id": row.get("id"),
        "name": row.get("name"),
        "description": row.get("description"),
        "params_schema": params_schema or {},
        "python_code": row.get("python_code", ""),
        "toolset": row.get("toolset", "forged"),
        "judge_verdict": row.get("judge_verdict"),
        "judge_approved": bool(row.get("judge_approved")),
        "test_passed": bool(row.get("test_passed")),
        "test_output": row.get("test_output"),
        "created_at": row.get("created_at"),
        "last_used_at": row.get("last_used_at"),
        "use_count": int(row.get("use_count", 0)),
        "promoted": bool(row.get("promoted")),
        "promoted_path": row.get("promoted_path"),
        "session_id": row.get("session_id"),
        "metadata": metadata or {},
    }


def _stats(rows: List[Dict[str, Any]]) -> Dict[str, Any]:
    total = len(rows)
    approved = sum(1 for r in rows if r.get("judge_approved"))
    tested = sum(1 for r in rows if r.get("test_passed"))
    promoted = sum(1 for r in rows if r.get("promoted"))
    return {
        "total": total,
        "approved": approved,
        "tested": tested,
        "promoted": promoted,
    }


# ── HTTP handlers ─────────────────────────────────────────────────────────────


def handle_forge_get(handler, parsed=None) -> None:
    """GET /api/forge → list all forged tools + stats."""
    try:
        store = _get_store()
    except Exception as exc:
        return j(handler, {"available": False, "reason": str(exc)})

    rows = store.list_all()
    tools = [_row_to_payload(r) for r in rows]
    return j(handler, {
        "available": True,
        "db_path": str(_db_path(_resolve_hermes_home())),
        "tools": tools,
        "stats": _stats(rows),
    })


def handle_forge_post(handler, body) -> None:
    """POST /api/forge — actions: delete, view, promote_test."""
    if not isinstance(body, dict):
        return bad(handler, "JSON body required")
    action = body.get("action", "")
    if action not in ("delete", "view", "promote_test"):
        return bad(handler, "action must be one of: delete, view, promote_test")

    try:
        store = _get_store()
    except Exception as exc:
        return bad(handler, str(exc), 404)

    if action == "delete":
        tool_id = body.get("id")
        if not isinstance(tool_id, str) or not tool_id:
            return bad(handler, "id is required")
        ok = store.remove(tool_id)
        if not ok:
            return bad(handler, "Tool not found", 404)
        return j(handler, {"ok": True, "action": "delete", "id": tool_id})

    if action == "view":
        tool_id = body.get("id")
        if not isinstance(tool_id, str) or not tool_id:
            return bad(handler, "id is required")
        tool = store.get(tool_id)
        if not tool:
            return bad(handler, "Tool not found", 404)
        return j(handler, {"ok": True, "tool": _row_to_payload(tool)})

    # action == "promote_test" — dry-run the promote path to preview the SKILL.md
    tool_id = body.get("id")
    if not isinstance(tool_id, str) or not tool_id:
        return bad(handler, "id is required")
    tool = store.get(tool_id)
    if not tool:
        return bad(handler, "Tool not found", 404)
    payload = _row_to_payload(tool)
    # Preview what the SKILL.md would look like
    import json as _json
    schema_str = _json.dumps(payload["params_schema"], indent=2)
    preview = (
        f"---\n"
        f"name: {payload['name']}\n"
        f"description: {payload.get('description', '')}\n"
        f"---\n\n"
        f"## Parameters\n\n"
        f"```json\n{schema_str}\n```\n\n"
        f"## Code\n\n"
        f"```python\n{payload.get('python_code', '')}\n```\n"
    )
    return j(handler, {"ok": True, "preview": preview})