"""FX tracker bridge for the WebUI.

Serves the FX project's state to the dedicated FX panel under three
endpoints — ``/api/fx/notes`` (vault decisions/audits), ``/api/fx/reports``
(analyzer reports), ``/api/fx/health`` (guard verdict + trader state).

Design notes:

- Read-only by design. The vault (``/root/vault/knowledge/fx/*.md``) is the
  decision authority; ``/root/workspace/fx-invest-project`` is the code and
  report authority. This bridge surfaces them; it never writes.
- Pure stdlib. No Hermes Agent internals are imported — safe to load inside
  the WebUI process.
- Vault frontmatter is validator-enforced (rigid ``key: value`` blocks with
  bracketed lists), so a small parser is sufficient and PyYAML is not a
  dependency. Malformed notes are skipped and logged, never fatal.
- Every probe in ``get_health()`` is independently try/except-wrapped so one
  missing file degrades one field, not the panel. The consistency-guard
  subprocess is fail-closed: any error surfaces as ``ok: false``.
- Paths are module constants (single-host deployment); overridable in tests
  via module attributes.
"""

from __future__ import annotations

import ast
import json
import logging
import os
import re
import subprocess
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from api.helpers import bad, j

logger = logging.getLogger(__name__)

# ── Paths (overridable in tests) ──────────────────────────────────────────
VAULT_FX_DIR = Path("/root/vault/knowledge/fx")
FX_PROJECT_DIR = Path("/root/workspace/fx-invest-project")
GUARD_SCRIPT = Path("/root/.hermes/scripts/fx_consistency_guard.sh")

NOTE_TYPES = ("decision", "reference", "strategy", "plan", "incident")
NOTES_CACHE_TTL = 60.0          # seconds
REPORTS_CACHE_TTL = 60.0
HEALTH_CACHE_TTL = 30.0
GUARD_TIMEOUT_S = 10

# Report artifacts shown as cards, in display order.
REPORT_FILES: List[Dict[str, str]] = [
    {"key": "killcriteria_sim", "label": "Kill Criteria MC", "file": "killcriteria_sim.json"},
    {"key": "mc_report", "label": "MC Bootstrap", "file": "mc_report.json"},
    {"key": "black_swan_report", "label": "Black Swan Stress", "file": "black_swan_report.json"},
    {"key": "wfa_report", "label": "Walk-Forward Analysis", "file": "wfa_report.json"},
    {"key": "matrix_report", "label": "Matrix Stress", "file": "matrix_report.json"},
    {"key": "expansion_symbols_report", "label": "Expansion Symbols", "file": "expansion_symbols_report.json"},
    {"key": "mc_expansion_symbols", "label": "Expansion MC", "file": "mc_expansion_symbols.json"},
]

# ── Caches ────────────────────────────────────────────────────────────────
_notes_cache: Dict[str, Any] = {"t": 0.0, "notes": []}
_reports_cache: Dict[str, Any] = {"t": 0.0, "reports": []}
_health_cache: Dict[str, Any] = {"t": 0.0, "health": {}}


def _invalidate_caches() -> None:
    _notes_cache["t"] = 0.0
    _reports_cache["t"] = 0.0
    _health_cache["t"] = 0.0


# ── Frontmatter parsing ───────────────────────────────────────────────────
_FM_KEY_RE = re.compile(r"^([A-Za-z_][A-Za-z0-9_-]*)\s*:\s*(.*)$")


def parse_frontmatter(text: str) -> Optional[Dict[str, Any]]:
    """Parse the validator-enforced frontmatter block.

    Returns dict with fields + ``_body`` (markdown body), or None when the
    note has no parseable frontmatter (skipped by callers, never fatal).
    """
    if not text.startswith("---"):
        return None
    end = text.find("\n---", 3)
    if end == -1:
        return None
    block = text[3:end].strip("\n")
    fields: Dict[str, Any] = {}
    for line in block.splitlines():
        m = _FM_KEY_RE.match(line.strip())
        if not m:
            continue
        key, raw = m.group(1).lower(), m.group(2).strip()
        if raw.startswith("[") and raw.endswith("]"):
            try:
                val = ast.literal_eval(raw)
                if not isinstance(val, list):
                    val = [str(val)]
            except (ValueError, SyntaxError):
                val = [p.strip(" \"'") for p in raw[1:-1].split(",") if p.strip()]
        else:
            val = raw.strip("\"'")
        fields[key] = val
    if not fields.get("title"):
        return None
    fields["_body"] = text[end + 4:].strip()
    return fields


def _list_notes_uncached() -> List[Dict[str, Any]]:
    notes: List[Dict[str, Any]] = []
    if not VAULT_FX_DIR.is_dir():
        return notes
    for p in sorted(VAULT_FX_DIR.glob("*.md")):
        try:
            text = p.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as e:
            logger.info("fx_bridge: skipping unreadable note %s (%s)", p.name, e)
            continue
        fm = parse_frontmatter(text)
        if fm is None:
            logger.info("fx_bridge: skipping note without frontmatter: %s", p.name)
            continue
        ntype = str(fm.get("type", "")).strip().lower()
        if ntype not in NOTE_TYPES:
            continue
        updated = str(fm.get("updated") or fm.get("created") or "")
        notes.append({
            "title": str(fm.get("title", p.stem)),
            "type": ntype,
            "status": str(fm.get("status", "")),
            "tags": fm.get("tags", []) if isinstance(fm.get("tags"), list) else [str(fm.get("tags", ""))],
            "created": str(fm.get("created", "")),
            "updated": updated,
            "path": str(p),
            "aliases": fm.get("aliases", []) if isinstance(fm.get("aliases"), list) else [],
        })
    notes.sort(key=lambda n: n["updated"], reverse=True)
    return notes


def list_notes(kind: Optional[str] = None, limit: int = 200) -> Dict[str, Any]:
    now = time.time()
    if now - _notes_cache["t"] > NOTES_CACHE_TTL:
        _notes_cache["notes"] = _list_notes_uncached()
        _notes_cache["t"] = now
    notes = _notes_cache["notes"]
    if kind:
        kinds = {k.strip().lower() for k in kind.split(",") if k.strip()}
        notes = [n for n in notes if n["type"] in kinds]
    return {"available": True, "count": len(notes), "notes": notes[: max(1, min(int(limit), 500))]}


# ── Reports ───────────────────────────────────────────────────────────────
def _extract_report_data(data: Dict[str, Any]) -> Dict[str, Any]:
    out: Dict[str, Any] = {}
    for key in ("briefing_line", "gate_pass", "gate_pass_applied", "survival_pass",
                "survival_evaluated_at", "generated", "n_paths", "n_trades"):
        if key in data:
            out[key] = data[key]
    # common param shapes
    for holder in ("params", "config", "population"):
        h = data.get(holder)
        if isinstance(h, dict):
            out["params"] = {k: h[k] for k in list(h)[:12]}
            break
    if "applied_config_gate" in data and isinstance(data["applied_config_gate"], dict):
        out["applied_config_gate"] = {
            "status": data["applied_config_gate"].get("status"),
        }
    return out


def _list_reports_uncached() -> List[Dict[str, Any]]:
    results = FX_PROJECT_DIR / "results"
    reports: List[Dict[str, Any]] = []
    for spec in REPORT_FILES:
        path = results / spec["file"]
        card: Dict[str, Any] = {"key": spec["key"], "label": spec["label"], "available": False}
        if not path.is_file():
            card["reason"] = f"{spec['file']} not found"
            reports.append(card)
            continue
        try:
            card["mtime"] = int(path.stat().st_mtime)
            card["data"] = _extract_report_data(json.loads(path.read_text(encoding="utf-8")))
            card["available"] = True
        except (json.JSONDecodeError, UnicodeDecodeError) as e:
            card["reason"] = f"corrupt JSON: {e}"
        except OSError as e:
            card["reason"] = f"unreadable: {e}"
        reports.append(card)
    reports.sort(key=lambda c: c.get("mtime", 0), reverse=True)
    return reports


def list_reports() -> Dict[str, Any]:
    now = time.time()
    if now - _reports_cache["t"] > REPORTS_CACHE_TTL:
        _reports_cache["reports"] = _list_reports_uncached()
        _reports_cache["t"] = now
    return {"available": True, "reports": _reports_cache["reports"]}


# ── Health ────────────────────────────────────────────────────────────────
def _guard_probe() -> Dict[str, Any]:
    if not GUARD_SCRIPT.is_file():
        return {"ok": False, "error": f"guard script missing: {GUARD_SCRIPT}"}
    try:
        r = subprocess.run(
            ["bash", str(GUARD_SCRIPT)],
            capture_output=True, text=True, timeout=GUARD_TIMEOUT_S,
        )
        lines = [l for l in (r.stdout or "").splitlines() if l.strip()]
        contradictions = [l for l in lines if "CONTRADICTION" in l.upper() or "FAIL" in l.upper() or "✗" in l]
        return {"ok": r.returncode == 0, "exit_code": r.returncode,
                "contradictions": contradictions if r.returncode != 0 else []}
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": f"guard timed out after {GUARD_TIMEOUT_S}s"}
    except OSError as e:
        return {"ok": False, "error": f"guard failed to run: {e}"}


def _paper_probe() -> Dict[str, Any]:
    path = FX_PROJECT_DIR / "data" / "paper_state.json"
    try:
        st = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        return {"error": f"paper_state unreadable: {e}"}
    positions = st.get("positions") or {}
    realized = st.get("realized_r") or []
    return {
        "halted": bool(st.get("halted", False)),
        "open": len(positions),
        "pairs": sorted(positions.keys()),
        "n_closed": len(realized),
        "realized_r": round(sum(float(x) for x in realized), 3) if realized else 0.0,
    }


def _ctrader_probe() -> Dict[str, Any]:
    path = FX_PROJECT_DIR / "watch" / "ctrader_status.json"
    try:
        st = json.loads(path.read_text(encoding="utf-8"))
        return {"status": st.get("status", "unknown"), "since": st.get("since") or st.get("updated")}
    except (OSError, json.JSONDecodeError) as e:
        return {"error": f"ctrader_status unreadable: {e}"}


def _risk_probe() -> Dict[str, Any]:
    path = FX_PROJECT_DIR / "config" / "risk_config.json"
    try:
        cfg = json.loads(path.read_text(encoding="utf-8"))
        return {"risk_pct": cfg.get("risk_pct"), "pause_threshold_R": cfg.get("pause_threshold_R")}
    except (OSError, json.JSONDecodeError) as e:
        return {"error": f"risk_config unreadable: {e}"}


def _health_uncached() -> Dict[str, Any]:
    return {
        "available": True,
        "guard": _guard_probe(),
        "paper": _paper_probe(),
        "ctrader": _ctrader_probe(),
        "risk": _risk_probe(),
    }


def get_health() -> Dict[str, Any]:
    now = time.time()
    if now - _health_cache["t"] > HEALTH_CACHE_TTL:
        _health_cache["health"] = _health_uncached()
        _health_cache["t"] = now
    return _health_cache["health"]


# ── HTTP handlers (None = handled; never return a value) ─────────────────
def handle_fx_notes_get(handler, parsed) -> None:
    try:
        q = parsed.query
        if isinstance(q, str):
            from urllib.parse import parse_qs
            q = parse_qs(q)
        kind = (q.get("kind") or [None])[0]
        limit = int((q.get("limit") or [200])[0])
        j(handler, list_notes(kind=kind, limit=limit))
    except Exception as e:  # noqa: BLE001 — panel must degrade, never 500
        logger.error("fx_bridge notes error: %s", e)
        bad(handler, f"fx notes error: {e}")


def handle_fx_reports_get(handler, parsed) -> None:
    try:
        j(handler, list_reports())
    except Exception as e:  # noqa: BLE001
        logger.error("fx_bridge reports error: %s", e)
        bad(handler, f"fx reports error: {e}")


def handle_fx_health_get(handler, parsed) -> None:
    try:
        j(handler, get_health())
    except Exception as e:  # noqa: BLE001
        logger.error("fx_bridge health error: %s", e)
        bad(handler, f"fx health error: {e}")
