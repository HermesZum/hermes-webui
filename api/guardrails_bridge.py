"""Guardrails bridge for the WebUI.

Serves the hermes-guardrails plugin audit store to the Memory panel under
``/api/guardrails`` — list audit entries, stats, blocked attempts, and
clear the log.

Design notes (same pattern as cognitive_bridge / forge_bridge):

- The Hermes plugin lives at ``~/.hermes/plugins/guardrails/``. Its
  ``__init__.py`` imports Hermes Agent internals, so this bridge loads only
  ``store.py`` under a synthetic package name — the WebUI process never
  imports Hermes Agent code.
- Profile-aware: resolves the active Hermes home.
- The plugin's AuditStore is thread-safe (RLock + WAL + busy_timeout).
- Findings are stored MASKED in the audit log — this bridge never exposes
  raw PII.
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
    return home / "plugins" / "guardrails"


def _db_path(home: Path) -> Path:
    return home / "guardrails" / "guardrails.db"


def _load_store_module(home: Path):
    """Load ``store`` from the plugin dir without running __init__.py."""
    plugin_dir = _plugin_dir(home)
    pkg_name = "_guardrails_webui"
    store_mod = sys.modules.get(pkg_name + ".store")
    if store_mod is not None:
        return store_mod

    pkg = types.ModuleType(pkg_name)
    pkg.__path__ = [str(plugin_dir)]  # type: ignore[attr-defined]
    sys.modules[pkg_name] = pkg

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
    """Return a connected AuditStore for the active profile (lazy, per-home)."""
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
                "Guardrails plugin not installed "
                f"(missing {plugin_dir})."
            )
        if not db_path.exists():
            raise RuntimeError(
                "Guardrails database not found at "
                f"{db_path}. Restart Hermes with guardrails enabled "
                "to initialize it."
            )
        store_module = _load_store_module(home)
        store = store_module.AuditStore(db_path)
        store.connect()
        _store_instances[key] = store
        return store


def _entry_to_payload(row: Dict[str, Any]) -> Dict[str, Any]:
    """Convert an audit row to a JSON-serializable payload."""
    import json as _json
    findings = row.get("findings_json")
    if isinstance(findings, str) and findings:
        try:
            findings = _json.loads(findings)
        except Exception:
            findings = []
    else:
        findings = []
    return {
        "id": row.get("id"),
        "timestamp": row.get("timestamp"),
        "tool_name": row.get("tool_name"),
        "action": row.get("action"),
        "severity": row.get("severity"),
        "findings": findings,
        "message": row.get("message", ""),
        "session_id": row.get("session_id", ""),
    }


def _derive_stats(rows: List[Dict[str, Any]], stats: Dict[str, int]) -> Dict[str, Any]:
    blocked = sum(1 for r in rows if r.get("action") == "blocked")
    warned = sum(1 for r in rows if r.get("action") == "warned")
    allowed = sum(1 for r in rows if r.get("action") == "allowed")
    scanned = sum(1 for r in rows if r.get("action") == "scanned")
    return {
        "total": len(rows),
        "blocked": stats.get("total_blocked", blocked),
        "warned": stats.get("total_warned", warned),
        "allowed": stats.get("total_allowed", allowed),
        "scanned": stats.get("total_scanned", scanned),
    }


# ── HTTP handlers ─────────────────────────────────────────────────────────────


def handle_guardrails_get(handler, parsed=None) -> None:
    """GET /api/guardrails → recent audit entries + stats."""
    try:
        store = _get_store()
    except Exception as exc:
        return j(handler, {"available": False, "reason": str(exc)})

    limit = 50
    if parsed is not None:
        try:
            qs = getattr(parsed, "query", None) or {}
            limit = max(1, min(int(qs.get("limit", 50)), 500))
        except Exception:
            limit = 50

    action_filter = ""
    if parsed is not None:
        qs = getattr(parsed, "query", None) or {}
        action_filter = qs.get("action", "") if isinstance(qs.get("action", ""), str) else ""

    rows = store.get_recent(limit, action_filter)
    entries = [_entry_to_payload(r) for r in rows]
    stats = store.get_stats()
    return j(handler, {
        "available": True,
        "db_path": str(_db_path(_resolve_hermes_home())),
        "entries": entries,
        "stats": _derive_stats(rows, stats),
    })


def handle_guardrails_post(handler, body) -> None:
    """POST /api/guardrails — actions: clear, view."""
    if not isinstance(body, dict):
        return bad(handler, "JSON body required")
    action = body.get("action", "")
    if action not in ("clear", "view"):
        return bad(handler, "action must be one of: clear, view")

    try:
        store = _get_store()
    except Exception as exc:
        return bad(handler, str(exc), 404)

    if action == "clear":
        deleted = store.clear()
        return j(handler, {"ok": True, "action": "clear", "deleted": deleted})

    # action == "view" — single entry by id
    entry_id = body.get("id")
    if entry_id is None:
        return bad(handler, "id is required")
    rows = store.get_recent(500)
    match = next((r for r in rows if r.get("id") == entry_id), None)
    if match is None:
        return bad(handler, "Entry not found", 404)
    return j(handler, {"ok": True, "entry": _entry_to_payload(match)})
