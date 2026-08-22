"""Token Telemetry bridge for the Hermes WebUI.

Serves the hermes-token-telemetry store to the Memory panel under
``/api/token-telemetry`` — global stats (tokens, cache-hit rate), per-session
rollups, recent activity, model breakdown, and top tool-result offenders.

Design (mirrors ``cognitive_bridge.py``):
- The Hermes plugin lives at ``~/.hermes/plugins/token_telemetry/``. Its
  ``__init__.py`` imports Hermes Agent internals, so this bridge loads ONLY
  ``store.py`` under a synthetic package name — the WebUI process never
  imports Hermes Agent code and the plugin's side effects never run here.
  ``store.py`` itself is pure stdlib (sqlite3/pathlib/typing), so loading it
  is safe.
- The active Hermes home is resolved profile-aware (``api.profiles``), so
  the bridge reads the same SQLite DB the agent plugin writes.
- Concurrency: TokenStore sets ``PRAGMA busy_timeout`` so the WebUI process
  and the Hermes agent process can share the DB safely.

This bridge is READ-ONLY: it never mutates the store. All handlers return
``None`` and respond via ``api.helpers.j``/``bad`` (never return ``False``).
"""

from __future__ import annotations

import importlib.util
import logging
import sys
import threading
import types
from pathlib import Path
from typing import Any, Dict, Optional

from api.helpers import bad, j

logger = logging.getLogger(__name__)

# Cache the loaded store module per resolved Hermes home so profile switches
# get their own DB connection (mirrors cognitive_bridge).
_store_lock = threading.Lock()
_store_instances: Dict[str, Any] = {}


def _resolve_hermes_home() -> Path:
    try:
        from api.profiles import get_active_hermes_home

        home = get_active_hermes_home()
        if home:
            return Path(home)
    except Exception:
        logger.debug("get_active_hermes_home failed; using ~/.hermes", exc_info=True)
    return Path.home() / ".hermes"


def _plugin_dir(home: Path) -> Path:
    return home / "plugins" / "token_telemetry"


def _db_path(home: Path) -> Path:
    return home / "token_telemetry" / "tokens.db"


def _load_store_module(home: Path):
    """Load ``store.py`` from the plugin dir under a synthetic package name.

    The plugin's ``__init__.py`` is NOT executed (it imports Hermes Agent
    internals). ``store.py`` itself is pure stdlib, so this is safe.
    """
    plugin_dir = _plugin_dir(home)
    pkg_name = "_token_telemetry_webui"
    cached = sys.modules.get(pkg_name + ".store")
    if cached is not None:
        return cached

    pkg = types.ModuleType(pkg_name)
    pkg.__path__ = [str(plugin_dir)]  # type: ignore[attr-defined]
    sys.modules[pkg_name] = pkg

    path = plugin_dir / "store.py"
    spec = importlib.util.spec_from_file_location(pkg_name + ".store", path)
    if spec is None or spec.loader is None:
        raise ImportError(f"cannot load {path}")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[pkg_name + ".store"] = mod
    spec.loader.exec_module(mod)
    return mod


def _get_store():
    """Return a connected TokenStore for the active profile (lazy, per-home)."""
    home = _resolve_hermes_home()
    key = str(home)
    with _store_lock:
        cached = _store_instances.get(key)
        if cached is not None:
            return cached
        store_mod = _load_store_module(home)
        store = store_mod.TokenStore(_db_path(home))
        _store_instances[key] = store
        return store


def _query_params(parsed) -> Dict[str, str]:
    """Return a dict of query params from a ParsedRequest.

    In the real webui, ``parsed.query`` is the RAW query string (e.g.
    ``"limit=50&id=abc"``), not a dict — the standard pattern in routes.py is
    ``parse_qs(parsed.query)``. Some synthetic/test handlers pass a dict.
    Accept both.
    """
    q = getattr(parsed, "query", "")
    if isinstance(q, dict):
        return {str(k): str(v) for k, v in q.items()}
    if isinstance(q, str):
        from urllib.parse import parse_qs

        parsed_qs = parse_qs(q)
        return {k: (v[0] if v else "") for k, v in parsed_qs.items()}
    return {}


def _normalize_limit(parsed, default: int, lo: int, hi: int) -> int:
    try:
        params = _query_params(parsed)
        return max(lo, min(int(params.get("limit", default)), hi))
    except (TypeError, ValueError):
        return default


def handle_token_telemetry_get(handler, parsed) -> None:
    """GET /api/token-telemetry — global stats, sessions, tools, models, activity."""
    try:
        store = _get_store()
    except Exception as exc:
        return bad(handler, f"store unavailable: {exc}", 404)

    limit = _normalize_limit(parsed, 50, 1, 500)
    sessions = store.recent_sessions(limit)
    try:
        global_stats = store.global_stats()
        top_tools = store.top_tool_result_offenders(10)
        models = store.model_breakdown()
        recent = store.recent_events(limit)
    except Exception as exc:
        logger.error("token-telemetry read failed", exc_info=True)
        return bad(handler, f"read failed: {exc}", 500)

    return j(handler, {
        "available": True,
        "db_path": str(store.get_db_path()),
        "global": global_stats,
        "sessions": sessions,
        "top_tools": top_tools,
        "models": models,
        "recent": recent,
    })


def handle_token_telemetry_session_get(handler, parsed) -> None:
    """GET /api/token-telemetry/session?id=<session_id> — one session detail."""
    try:
        store = _get_store()
    except Exception as exc:
        return bad(handler, f"store unavailable: {exc}", 404)

    sid = _query_params(parsed).get("id") or ""
    if not sid:
        return bad(handler, "id query param is required")

    limit = _normalize_limit(parsed, 200, 1, 1000)
    detail = store.session_detail(sid, limit=limit)
    if detail["summary"] is None:
        return bad(handler, f"no telemetry for session {sid}", 404)
    return j(handler, {"available": True, **detail})
