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
import csv
import json
import logging
import os
import re
import subprocess
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from api.helpers import bad, j

logger = logging.getLogger(__name__)

# ── Paths (overridable in tests) ──────────────────────────────────────────
VAULT_FX_DIR = Path("/root/vault/knowledge/fx")
FX_PROJECT_DIR = Path("/root/workspace/fx-invest-project")
# Demo trade ledger — the ONLY source for the graduation gate. The backtest
# artifact results/trades.csv (synthetic 2024 trades from scripts/backtest.py)
# must never feed the go-live verdict (audit 2026-09-03).
DEMO_LEDGER_REL = Path("journal") / "paper_trades.csv"
GUARD_SCRIPT = Path("/root/.hermes/scripts/fx_consistency_guard.sh")

# ── Usage instrumentation (audit 2026-09-03 P1) ───────────────────────────
# The server suppresses access logs, so panel usage was unmeasurable. Each
# /api/fx/* hit bumps a per-endpoint counter file (one line in the log too).
# Best-effort by design: any failure here must never affect the API response.
USAGE_LOCK = threading.Lock()
USAGE_FILE: Optional[Path] = None  # resolved lazily; overridable in tests


def _usage_path() -> Optional[Path]:
    global USAGE_FILE
    if USAGE_FILE is not None:
        return USAGE_FILE
    try:
        from api.config import STATE_DIR  # webui process context only

        USAGE_FILE = Path(STATE_DIR) / "fx_usage.json"
    except Exception:  # noqa: BLE001 — instrumentation must never be fatal
        USAGE_FILE = None
    return USAGE_FILE


def _bump_usage(endpoint: str) -> None:
    path = _usage_path()
    if path is None:
        return
    try:
        with USAGE_LOCK:
            data: Dict[str, Any] = {}
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                data = {}
            now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
            data[endpoint] = {"count": int(data.get(endpoint, {}).get("count", 0)) + 1,
                              "last": now}
            data["_updated"] = now
            tmp = path.with_suffix(".json.tmp")
            tmp.write_text(json.dumps(data, indent=1), encoding="utf-8")
            os.replace(tmp, path)
    except Exception as e:  # noqa: BLE001
        logger.debug("fx_bridge usage counter skipped: %s", e)
    logger.info("fx_api hit endpoint=%s", endpoint)

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
_gate_cache: Dict[str, Any] = {"t": 0.0, "gate": {}}
_actions_cache: Dict[str, Any] = {"t": 0.0, "actions": {}}
_position_cache: Dict[str, Any] = {"t": 0.0, "position": {}}
_calendar_cache: Dict[str, Any] = {"t": 0.0, "calendar": {}}

GRADUATION_N_MIN = 20        # journaled demo trades required
GRADUATION_PLAN_PCT = 85.0   # plan_followed threshold (journal-audited)
GRADUATION_SIZE = "1/4 size"


def _invalidate_caches() -> None:
    _notes_cache["t"] = 0.0
    _reports_cache["t"] = 0.0
    _health_cache["t"] = 0.0
    _gate_cache["t"] = 0.0
    _actions_cache["t"] = 0.0
    _position_cache["t"] = 0.0
    _calendar_cache["t"] = 0.0


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
    keys = ("briefing_line", "gate_pass", "gate_pass_applied", "survival_pass",
            "survival_evaluated_at", "generated", "n_paths", "n_trades",
            # per-symbol gate schema (authoritative since 2026-09-02 refactor)
            "gate_pass_persymbol", "authoritative_gate")
    for key in keys:
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


def _read_json(path: Path) -> Optional[Dict[str, Any]]:
    """Load a JSON artifact; None on missing/corrupt (probes degrade, never 500)."""
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        logger.info("fx_bridge: unreadable %s (%s)", path.name, e)
        return None


# ── Graduation gate ───────────────────────────────────────────────────────
def get_gate() -> Dict[str, Any]:
    """Graduation-gate progress from the LIVE demo ledger.

    Source: ``journal/paper_trades.csv`` (written by the paper trader /
    journal pipeline) plus ``data/paper_state.json`` for open-trade count.
    Criteria per the vault v2-runner approval: >= GRADUATION_N_MIN journaled
    demo trades, expectancy > 0 in R, plan_followed >= 85%. The percentage
    itself requires a journal audit (not derivable from the ledger), so it is
    reported as ``journal_audit_required`` — the panel must not fabricate it.
    Read-only: this computes over the ledger, it never writes.
    """
    now = time.time()
    if now - _gate_cache["t"] <= HEALTH_CACHE_TTL and _gate_cache["gate"]:
        return _gate_cache["gate"]
    try:
        rows: List[Dict[str, Any]] = []
        path = FX_PROJECT_DIR / DEMO_LEDGER_REL
        with path.open(encoding="utf-8", newline="") as fh:
            for r in csv.DictReader(fh):
                try:
                    rows.append({
                        "pair": (r.get("pair") or "").strip(),
                        "date": (r.get("entry_time") or r.get("date") or "").strip(),
                        "dir": (r.get("dir") or "").strip(),
                        "r_net": float(r.get("r_net") or 0.0),
                        "reason": (r.get("exit_reason") or r.get("reason") or "").strip(),
                    })
                except (TypeError, ValueError):
                    continue
        n = len(rows)
        exp = (sum(r["r_net"] for r in rows) / n) if n else 0.0
        wins = sum(1 for r in rows if r["r_net"] > 0)
        total_r = sum(r["r_net"] for r in rows)
        open_n = 0
        st = _read_json(FX_PROJECT_DIR / "data" / "paper_state.json")
        if st:
            open_n = len(st.get("positions") or {})
        ready_partial = n >= GRADUATION_N_MIN and exp > 0
        gate: Dict[str, Any] = {
            "available": True,
            "source": str(path),
            "n_min": GRADUATION_N_MIN,
            "plan_pct_required": GRADUATION_PLAN_PCT,
            "graduation_size": GRADUATION_SIZE,
            "n_trades": n,
            "n_open": open_n,
            "expectancy_R": round(exp, 3),
            "total_r": round(total_r, 2),
            "win_rate_pct": round(wins / n * 100, 1) if n else 0.0,
            "plan_followed_pct": None,
            "plan_followed_status": "journal_audit_required",
            "ready": ready_partial,
            "verdict": ("READY (confirm plan_followed from journal, then go live at 1/4 size)"
                        if ready_partial else
                        f"PENDING — {n}/{GRADUATION_N_MIN} demo trades journaled"
                        + ("" if exp >= 0 else f", expectancy {round(exp, 3)}R must be > 0")),
            "criteria": [
                {"label": f"Journaled trades >= {GRADUATION_N_MIN}", "value": n,
                 "pass": n >= GRADUATION_N_MIN},
                {"label": "Expectancy > 0 (R)", "value": round(exp, 3), "pass": exp > 0},
                {"label": f"plan_followed >= {GRADUATION_PLAN_PCT:.0f}%", "value": None,
                 "pass": None, "note": "requires journal audit"},
                {"label": "Go-live size", "value": GRADUATION_SIZE, "pass": None,
                 "note": "only after full gate confirmation"},
            ],
        }
        _gate_cache["gate"] = gate
        _gate_cache["t"] = now
        return gate
    except Exception as e:  # noqa: BLE001 — degrade, never 500
        logger.error("fx_bridge gate error: %s", e)
        return {"available": False, "error": str(e)}


# ── Actions (alert-router events + regime shifts) ─────────────────────────
def get_actions() -> Dict[str, Any]:
    now = time.time()
    if now - _actions_cache["t"] <= HEALTH_CACHE_TTL and _actions_cache["actions"]:
        return _actions_cache["actions"]
    events: List[Dict[str, Any]] = []
    ar = _read_json(FX_PROJECT_DIR / "results" / "fx_action_required.json")
    if ar is None:
        events.append({"source": "fx_action_required", "severity": "WATCH",
                       "category": "stale",
                       "text": "fx_action_required.json missing or unreadable — alert router may be down"})
    else:
        for ev in ar.get("events", []):
            events.append({"source": ev.get("source"), "severity": ev.get("severity"),
                           "category": ev.get("category"), "text": ev.get("text")})
    reg = _read_json(FX_PROJECT_DIR / "results" / "regime_monitor.json")
    if reg:
        changed = [p for p in (reg.get("changed_pairs") or []) if isinstance(p, str)]
        if changed:
            events.append({"source": "regime_monitor", "severity": "WATCH", "category": "regime",
                           "text": f"Regime changed on: {', '.join(changed)}"})
    sev_rank = {"HALT": 0, "ACTION": 1, "WATCH": 2}
    events.sort(key=lambda e: sev_rank.get(e.get("severity") or "WATCH", 2))
    out = {"available": True, "events": events,
           "severity": events[0]["severity"] if events else "CLEAR",
           "count": len(events)}
    _actions_cache["actions"] = out
    _actions_cache["t"] = now
    return out


# ── Live position (paper trader) ──────────────────────────────────────────
def get_position() -> Dict[str, Any]:
    now = time.time()
    if now - _position_cache["t"] <= HEALTH_CACHE_TTL and _position_cache["position"]:
        return _position_cache["position"]
    st = _read_json(FX_PROJECT_DIR / "data" / "paper_state.json")
    if st is None:
        out: Dict[str, Any] = {"available": False, "error": "paper_state.json unreadable"}
    else:
        positions = st.get("positions") or {}
        out = {"available": True, "halted": bool(st.get("halted", False)),
               "n_open": len(positions), "positions": []}
        for pair, p in sorted(positions.items()):
            if not isinstance(p, dict):
                continue
            out["positions"].append({
                "pair": pair,
                "trade_id": p.get("trade_id"),
                "dir": p.get("dir"),
                "entry": p.get("entry"),
                "stop": p.get("stop"),
                "entry_time": p.get("entry_time"),
                "atr_pips": p.get("atr_pips"),
                "banked_r": p.get("banked_r", 0.0),
                "half_closed": bool(p.get("half_closed", False)),
            })
        realized = st.get("realized_r") or []
        out["n_closed"] = len(realized)
        out["realized_r_total"] = round(sum(float(x) for x in realized), 3) if realized else 0.0
    _position_cache["position"] = out
    _position_cache["t"] = now
    return out


# ── Calendar (next high-impact events = order-time gate windows) ─────────
def get_calendar() -> Dict[str, Any]:
    now = time.time()
    if now - _calendar_cache["t"] <= HEALTH_CACHE_TTL and _calendar_cache["calendar"]:
        return _calendar_cache["calendar"]
    cal = _read_json(FX_PROJECT_DIR / "results" / "calendar_events.json")
    if cal is None:
        out = {"available": False, "error": "calendar_events.json missing or unreadable"}
    else:
        events = cal if isinstance(cal, list) else cal.get("events") or []
        now_iso = datetime.now(timezone.utc)
        upcoming = []
        for ev in events:
            if not isinstance(ev, dict):
                continue
            try:
                utc = datetime.fromisoformat(str(ev.get("utc", "")).replace("Z", "+00:00"))
            except ValueError:
                continue
            if utc.tzinfo is None:
                utc = utc.replace(tzinfo=timezone.utc)  # feed stores naive UTC
            if utc < now_iso:
                continue
            upcoming.append({"utc": ev.get("utc"), "title": ev.get("title"),
                             "currencies": ev.get("currencies"),
                             "impact": ev.get("impact"),
                             "hours_away": round((utc - now_iso).total_seconds() / 3600.0, 1)})
        upcoming.sort(key=lambda e: e["utc"] or "")
        out = {"available": True, "now_utc": now_iso.strftime("%Y-%m-%dT%H:%M:%SZ"),
               "count": len(upcoming), "events": upcoming[:8]}
    _calendar_cache["calendar"] = out
    _calendar_cache["t"] = now
    return out


# ── HTTP handlers (None = handled; never return a value) ─────────────────
def handle_fx_notes_get(handler, parsed) -> None:
    try:
        _bump_usage("notes")
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
        _bump_usage("reports")
        j(handler, list_reports())
    except Exception as e:  # noqa: BLE001
        logger.error("fx_bridge reports error: %s", e)
        bad(handler, f"fx reports error: {e}")


def handle_fx_health_get(handler, parsed) -> None:
    try:
        _bump_usage("health")
        j(handler, get_health())
    except Exception as e:  # noqa: BLE001
        logger.error("fx_bridge health error: %s", e)
        bad(handler, f"fx health error: {e}")


def handle_fx_gate_get(handler, parsed) -> None:
    try:
        _bump_usage("gate")
        j(handler, get_gate())
    except Exception as e:  # noqa: BLE001
        logger.error("fx_bridge gate error: %s", e)
        bad(handler, f"fx gate error: {e}")


def handle_fx_actions_get(handler, parsed) -> None:
    try:
        _bump_usage("actions")
        j(handler, get_actions())
    except Exception as e:  # noqa: BLE001
        logger.error("fx_bridge actions error: %s", e)
        bad(handler, f"fx actions error: {e}")


def handle_fx_position_get(handler, parsed) -> None:
    try:
        _bump_usage("position")
        j(handler, get_position())
    except Exception as e:  # noqa: BLE001
        logger.error("fx_bridge position error: %s", e)
        bad(handler, f"fx position error: {e}")


def handle_fx_calendar_get(handler, parsed) -> None:
    try:
        _bump_usage("calendar")
        j(handler, get_calendar())
    except Exception as e:  # noqa: BLE001
        logger.error("fx_bridge calendar error: %s", e)
        bad(handler, f"fx calendar error: {e}")
