"""Tests for the FX tracker bridge (api/fx_bridge.py).

Runs the REAL bridge module with monkeypatched paths and a fake handler —
no HTTP server, per the cognitive-bridge verification recipe.
"""

import importlib.util
import json
import sys
import time
import types
import unittest
from pathlib import Path

REPO = Path(__file__).parent.parent


def _load_bridge(tmp: Path):
    """Load api/fx_bridge.py fresh with paths pointed at a temp fixture dir."""
    name = "_fx_bridge_test"
    src = REPO / "api" / "fx_bridge.py"
    spec = importlib.util.spec_from_file_location(name, src)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)

    vault = tmp / "vault" / "knowledge" / "fx"
    results = tmp / "project" / "results"
    vault.mkdir(parents=True)
    results.mkdir(parents=True)
    mod.VAULT_FX_DIR = vault
    mod.FX_PROJECT_DIR = tmp / "project"
    mod.GUARD_SCRIPT = tmp / "guard.sh"
    mod._invalidate_caches()
    return mod, vault, results


NOTE_A = """---
title: "Decision A"
tags: [fx/decision, governance]
status: active
type: decision
created: 2026-08-20
updated: 2026-08-27
source: session
aliases: [dec-a]
---

Body of decision A.
"""

NOTE_B = """---
title: "Audit B"
tags: [fx/reference]
status: active
type: reference
created: 2026-08-21
updated: 2026-08-26
---

Body B.
"""

NOTE_NO_TYPE = """---
title: "Mystery note"
status: draft
---

no type field → excluded.
"""

NOTE_NO_FM = "just a body, no frontmatter"


class FakeHandler:
    """Captures what the bridge responds with."""

    def __init__(self):
        self.payload = None
        self.status = None

    # helpers used by api.helpers.j / bad in monkeypatched versions
    def _set(self, obj, status=None):
        self.payload = obj
        self.status = status


class _Query(str):
    """String-like object that also supports .get() — pins the raw-string pitfall."""

    def get(self, key, default=None):
        return default


def _make_parsed(query):
    parsed = types.SimpleNamespace()
    parsed.query = query
    parsed.path = "/api/fx/notes"
    return parsed


class FxBridgeNotes(unittest.TestCase):
    def setUp(self):
        import tempfile
        self.tmp = Path(tempfile.mkdtemp())
        self.mod, self.vault, self.results = _load_bridge(self.tmp)

    def test_parse_frontmatter_valid(self):
        fm = self.mod.parse_frontmatter(NOTE_A)
        self.assertEqual(fm["title"], "Decision A")
        self.assertEqual(fm["type"], "decision")
        self.assertEqual(fm["tags"], ["fx/decision", "governance"])
        self.assertEqual(fm["aliases"], ["dec-a"])
        self.assertIn("Body of decision A", fm["_body"])

    def test_parse_frontmatter_none(self):
        self.assertIsNone(self.mod.parse_frontmatter(NOTE_NO_FM))

    def test_list_notes_filters_types(self):
        (self.vault / "a.md").write_text(NOTE_A, encoding="utf-8")
        (self.vault / "b.md").write_text(NOTE_B, encoding="utf-8")
        (self.vault / "x.md").write_text(NOTE_NO_TYPE, encoding="utf-8")
        (self.vault / "y.md").write_text(NOTE_NO_FM, encoding="utf-8")
        self.mod._invalidate_caches()
        out = self.mod.list_notes()
        self.assertTrue(out["available"])
        self.assertEqual(out["count"], 2)
        titles = [n["title"] for n in out["notes"]]
        self.assertIn("Decision A", titles)
        self.assertIn("Audit B", titles)

    def test_list_notes_kind_filter(self):
        (self.vault / "a.md").write_text(NOTE_A, encoding="utf-8")
        (self.vault / "b.md").write_text(NOTE_B, encoding="utf-8")
        self.mod._invalidate_caches()
        out = self.mod.list_notes(kind="decision")
        self.assertEqual(out["count"], 1)
        self.assertEqual(out["notes"][0]["title"], "Decision A")

    def test_sorted_by_updated_desc(self):
        (self.vault / "a.md").write_text(NOTE_A, encoding="utf-8")
        (self.vault / "b.md").write_text(NOTE_B, encoding="utf-8")
        self.mod._invalidate_caches()
        out = self.mod.list_notes()
        self.assertEqual(out["notes"][0]["title"], "Decision A")  # 08-27 > 08-26

    def test_empty_vault(self):
        out = self.mod.list_notes()
        self.assertEqual((out["available"], out["count"]), (True, 0))


class FxBridgeReports(unittest.TestCase):
    def setUp(self):
        import tempfile
        self.tmp = Path(tempfile.mkdtemp())
        self.mod, self.vault, self.results = _load_bridge(self.tmp)

    def test_reports_available_with_gates(self):
        (self.results / "killcriteria_sim.json").write_text(json.dumps({
            "gate_pass": False, "gate_pass_applied": True,
            "applied_config_gate": {"status": "ok"},
            "briefing_line": "MC: P(pause)=8.4%", "n_paths": 10000,
        }), encoding="utf-8")
        self.mod._invalidate_caches()
        out = self.mod.list_reports()
        self.assertTrue(out["available"])
        cards = {c["key"]: c for c in out["reports"]}
        self.assertTrue(cards["killcriteria_sim"]["available"])
        self.assertEqual(cards["killcriteria_sim"]["data"]["gate_pass"], False)
        self.assertEqual(cards["killcriteria_sim"]["data"]["gate_pass_applied"], True)
        self.assertEqual(cards["killcriteria_sim"]["data"]["briefing_line"], "MC: P(pause)=8.4%")

    def test_corrupt_json_degrades_card(self):
        (self.results / "mc_report.json").write_text("{corrupt", encoding="utf-8")
        self.mod._invalidate_caches()
        out = self.mod.list_reports()
        cards = {c["key"]: c for c in out["reports"]}
        self.assertFalse(cards["mc_report"]["available"])
        self.assertIn("corrupt", cards["mc_report"]["reason"])

    def test_missing_file_reason(self):
        self.mod._invalidate_caches()
        out = self.mod.list_reports()
        cards = {c["key"]: c for c in out["reports"]}
        self.assertFalse(cards["wfa_report"]["available"])
        self.assertIn("not found", cards["wfa_report"]["reason"])

    def test_sorted_by_mtime_desc(self):
        a = self.results / "killcriteria_sim.json"
        b = self.results / "mc_report.json"
        a.write_text(json.dumps({"gate_pass": True}), encoding="utf-8")
        b.write_text(json.dumps({"gate_pass": True}), encoding="utf-8")
        os_time = time.time() + 100
        import os
        os.utime(a, (os_time, os_time))
        self.mod._invalidate_caches()
        out = self.mod.list_reports()
        self.assertEqual(out["reports"][0]["key"], "killcriteria_sim")


class FxBridgeHealth(unittest.TestCase):
    def setUp(self):
        import tempfile
        self.tmp = Path(tempfile.mkdtemp())
        self.mod, self.vault, self.results = _load_bridge(self.tmp)

    def test_guard_ok(self):
        guard = self.tmp / "guard.sh"
        guard.write_text("#!/bin/bash\nexit 0\n", encoding="utf-8")
        self.mod._invalidate_caches()
        h = self.mod.get_health()
        self.assertTrue(h["guard"]["ok"])

    def test_guard_fail_closed_with_contradictions(self):
        guard = self.tmp / "guard.sh"
        guard.write_text("#!/bin/bash\necho 'CONTRADICTION: A vs B'\nexit 1\n", encoding="utf-8")
        self.mod._invalidate_caches()
        h = self.mod.get_health()
        self.assertFalse(h["guard"]["ok"])
        self.assertEqual(h["guard"]["exit_code"], 1)
        self.assertTrue(any("CONTRADICTION" in l for l in h["guard"]["contradictions"]))

    def test_guard_missing_script(self):
        self.mod._invalidate_caches()
        h = self.mod.get_health()
        self.assertFalse(h["guard"]["ok"])
        self.assertIn("missing", h["guard"]["error"])

    def test_paper_probe_fields(self):
        data = self.tmp / "project" / "data"
        data.mkdir(parents=True)
        (data / "paper_state.json").write_text(json.dumps({
            "halted": False,
            "positions": {"GBPUSD": {"dir": "short"}},
            "realized_r": [0.5, -1.0, 2.0],
        }), encoding="utf-8")
        self.mod._invalidate_caches()
        h = self.mod.get_health()
        self.assertEqual(h["paper"]["open"], 1)
        self.assertEqual(h["paper"]["n_closed"], 3)
        self.assertAlmostEqual(h["paper"]["realized_r"], 1.5)
        self.assertFalse(h["paper"]["halted"])

    def test_probes_independent(self):
        # no files at all — every probe degrades, none raises
        self.mod._invalidate_caches()
        h = self.mod.get_health()
        self.assertIn("error", h["paper"])
        self.assertIn("error", h["ctrader"])
        self.assertIn("error", h["risk"])
        self.assertFalse(h["guard"]["ok"])  # guard script missing → fail-closed


class FxBridgeHandlers(unittest.TestCase):
    def setUp(self):
        import tempfile
        self.tmp = Path(tempfile.mkdtemp())
        self.mod, self.vault, self.results = _load_bridge(self.tmp)
        # monkeypatch api.helpers j/bad to capture payloads
        import api.helpers as helpers  # noqa
        self._orig_j, self._orig_bad = self.mod.j, self.mod.bad
        self.mod.j = lambda h, obj: h._set(obj)
        self.mod.bad = lambda h, msg, status=400: h._set({"error": msg}, status)

    def test_notes_handler_raw_string_query(self):
        (self.vault / "a.md").write_text(NOTE_A, encoding="utf-8")
        self.mod._invalidate_caches()
        h = FakeHandler()
        # RAW string query — the production shape (Pitfall #16)
        self.mod.handle_fx_notes_get(h, _make_parsed(_Query("kind=decision&limit=10")))
        self.assertIsNone(h.status)
        self.assertEqual(h.payload["count"], 1)

    def test_notes_handler_bad_limit(self):
        h = FakeHandler()
        self.mod.handle_fx_notes_get(h, _make_parsed(_Query("limit=nan")))
        self.assertIsNotNone(h.status)  # error response, not a crash

    def test_health_handler(self):
        guard = self.tmp / "guard.sh"
        guard.write_text("#!/bin/bash\nexit 0\n", encoding="utf-8")
        self.mod._invalidate_caches()
        h = FakeHandler()
        self.mod.handle_fx_health_get(h, _make_parsed(_Query("")))
        self.assertTrue(h.payload["guard"]["ok"])

    def test_reports_handler(self):
        h = FakeHandler()
        self.mod.handle_fx_reports_get(h, _make_parsed(_Query("")))
        self.assertTrue(h.payload["available"])


class FxBridgeGate(unittest.TestCase):
    def setUp(self):
        import tempfile
        self.tmp = Path(tempfile.mkdtemp())
        self.mod, self.vault, self.results = _load_bridge(self.tmp)

    def _write_trades(self, rows):
        lines = ["pair,date,dir,r_net,reason"]
        lines += [",".join(r) for r in rows]
        (self.results / "trades.csv").write_text("\r\n".join(lines) + "\r\n", encoding="utf-8")

    def test_gate_ready_when_n_and_expectancy_pass(self):
        rows = [(f"E{i}", f"2026-08-{(i % 28) + 1:02d}", "long", "1.5", "trail/be") for i in range(25)]
        self._write_trades(rows)
        self.mod._invalidate_caches()
        g = self.mod.get_gate()
        self.assertTrue(g["available"])
        self.assertEqual(g["n_trades"], 25)
        self.assertTrue(g["ready"])
        self.assertIn("READY", g["verdict"])

    def test_gate_pending_when_expectancy_negative(self):
        rows = [(f"E{i}", f"2026-08-{(i % 28) + 1:02d}", "short", "-1.0", "initial-stop") for i in range(25)]
        self._write_trades(rows)
        self.mod._invalidate_caches()
        g = self.mod.get_gate()
        self.assertFalse(g["ready"])
        self.assertEqual(g["verdict"], "PENDING")

    def test_gate_plan_followed_never_fabricated(self):
        rows = [(f"E{i}", f"2026-08-{(i % 28) + 1:02d}", "long", "2.0", "data-end") for i in range(30)]
        self._write_trades(rows)
        self.mod._invalidate_caches()
        g = self.mod.get_gate()
        self.assertIsNone(g["plan_followed_pct"])
        self.assertEqual(g["plan_followed_status"], "journal_audit_required")
        plan_rows = [c for c in g["criteria"] if "plan_followed" in c["label"]]
        self.assertEqual(len(plan_rows), 1)
        self.assertIsNone(plan_rows[0]["pass"])  # unknown, not pass/fail

    def test_gate_malformed_r_net_skipped(self):
        rows = [("E1", "2026-08-01", "long", "abc", "stop"), ("E2", "2026-08-02", "long", "1.0", "be")]
        self._write_trades(rows)
        self.mod._invalidate_caches()
        g = self.mod.get_gate()
        self.assertEqual(g["n_trades"], 1)  # bad row skipped, not fatal
        self.assertEqual(g["expectancy_R"], 1.0)

    def test_gate_missing_file_degrades(self):
        g = self.mod.get_gate()
        self.assertFalse(g["available"])
        self.assertIn("error", g)


class FxBridgeActions(unittest.TestCase):
    def setUp(self):
        import tempfile
        self.tmp = Path(tempfile.mkdtemp())
        self.mod, self.vault, self.results = _load_bridge(self.tmp)

    def test_actions_severity_ordering(self):
        (self.results / "fx_action_required.json").write_text(json.dumps({
            "events": [
                {"source": "a", "severity": "WATCH", "category": "feed", "text": "minor"},
                {"source": "b", "severity": "HALT", "category": "kill", "text": "halted"},
                {"source": "c", "severity": "ACTION", "category": "shock", "text": "review"},
            ]}), encoding="utf-8")
        self.mod._invalidate_caches()
        a = self.mod.get_actions()
        self.assertEqual(a["severity"], "HALT")
        self.assertEqual([e["severity"] for e in a["events"]], ["HALT", "ACTION", "WATCH"])

    def test_actions_regime_shift_appended(self):
        (self.results / "fx_action_required.json").write_text(json.dumps({"events": []}), encoding="utf-8")
        (self.results / "regime_monitor.json").write_text(json.dumps({"changed_pairs": ["EURUSD"]}), encoding="utf-8")
        self.mod._invalidate_caches()
        a = self.mod.get_actions()
        self.assertTrue(any(e["category"] == "regime" for e in a["events"]))

    def test_actions_clear_when_no_events(self):
        (self.results / "fx_action_required.json").write_text(json.dumps({"events": []}), encoding="utf-8")
        self.mod._invalidate_caches()
        a = self.mod.get_actions()
        self.assertEqual(a["severity"], "CLEAR")
        self.assertEqual(a["count"], 0)

    def test_actions_unreadable_artifact_flagged_not_fatal(self):
        (self.results / "fx_action_required.json").write_text("{corrupt", encoding="utf-8")
        self.mod._invalidate_caches()
        a = self.mod.get_actions()
        self.assertTrue(a["available"])
        self.assertTrue(any(e.get("category") == "stale" for e in a["events"]))


class FxBridgePosition(unittest.TestCase):
    def setUp(self):
        import tempfile
        self.tmp = Path(tempfile.mkdtemp())
        self.mod, self.vault, self.results = _load_bridge(self.tmp)

    def _write_state(self, obj):
        data = self.tmp / "project" / "data"
        data.mkdir(parents=True, exist_ok=True)
        (data / "paper_state.json").write_text(json.dumps(obj), encoding="utf-8")

    def test_position_full_shape(self):
        self._write_state({
            "halted": False,
            "positions": {"GBPUSD": {"trade_id": "P-1", "dir": "short", "entry": 1.35,
                                     "stop": 1.36, "atr_pips": 21.8, "banked_r": 0.5}},
            "realized_r": [0.5, -1.0],
        })
        self.mod._invalidate_caches()
        p = self.mod.get_position()
        self.assertTrue(p["available"])
        self.assertEqual(p["n_open"], 1)
        self.assertEqual(p["positions"][0]["pair"], "GBPUSD")
        self.assertEqual(p["positions"][0]["dir"], "short")
        self.assertEqual(p["n_closed"], 2)
        self.assertAlmostEqual(p["realized_r_total"], -0.5)

    def test_position_flat(self):
        self._write_state({"halted": False, "positions": {}, "realized_r": []})
        self.mod._invalidate_caches()
        p = self.mod.get_position()
        self.assertEqual(p["n_open"], 0)
        self.assertEqual(p["positions"], [])

    def test_position_unreadable_degrades(self):
        p = self.mod.get_position()
        self.assertFalse(p["available"])
        self.assertIn("error", p)


class FxBridgeCalendar(unittest.TestCase):
    def setUp(self):
        import tempfile
        self.tmp = Path(tempfile.mkdtemp())
        self.mod, self.vault, self.results = _load_bridge(self.tmp)

    def test_calendar_filters_past_and_keeps_order(self):
        from datetime import datetime, timedelta, timezone
        now = datetime.now(timezone.utc)
        past = (now - timedelta(hours=2)).strftime("%Y-%m-%dT%H:%M:%S")
        soon = (now + timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%S")
        later = (now + timedelta(hours=30)).strftime("%Y-%m-%dT%H:%M:%S")
        (self.results / "calendar_events.json").write_text(json.dumps({"events": [
            {"utc": past, "title": "Old", "currencies": ["USD"], "impact": "high"},
            {"utc": later, "title": "Later", "currencies": ["EUR"], "impact": "medium"},
            {"utc": soon, "title": "Soon", "currencies": ["GBP"], "impact": "high"},
            {"utc": "garbage", "title": "Bad", "currencies": [], "impact": "high"},
        ]}), encoding="utf-8")
        self.mod._invalidate_caches()
        c = self.mod.get_calendar()
        self.assertTrue(c["available"])
        self.assertEqual([e["title"] for e in c["events"]], ["Soon", "Later"])  # past+malformed dropped, chronological
        self.assertAlmostEqual(c["events"][0]["hours_away"], 1.0, delta=0.1)

    def test_calendar_naive_timestamps_accepted(self):
        # feed stores naive UTC — must not crash comparing to aware now (E2E bug found 2026-08-28)
        (self.results / "calendar_events.json").write_text(json.dumps({"events": [
            {"utc": "2030-01-01T12:30:00", "title": "FOMC", "currencies": ["USD"], "impact": "high"},
        ]}), encoding="utf-8")
        self.mod._invalidate_caches()
        c = self.mod.get_calendar()
        self.assertTrue(c["available"])
        self.assertEqual(c["count"], 1)

    def test_calendar_all_past(self):
        from datetime import datetime, timedelta, timezone
        past = (datetime.now(timezone.utc) - timedelta(hours=5)).strftime("%Y-%m-%dT%H:%M:%S")
        (self.results / "calendar_events.json").write_text(json.dumps({"events": [
            {"utc": past, "title": "Old", "currencies": ["USD"], "impact": "high"}]}), encoding="utf-8")
        self.mod._invalidate_caches()
        c = self.mod.get_calendar()
        self.assertTrue(c["available"])
        self.assertEqual(c["count"], 0)

    def test_calendar_missing_degrades(self):
        c = self.mod.get_calendar()
        self.assertFalse(c["available"])
        self.assertIn("error", c)


if __name__ == "__main__":
    unittest.main()
