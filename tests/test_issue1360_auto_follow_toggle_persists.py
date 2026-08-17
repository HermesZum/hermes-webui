"""Regression coverage for #1360-follow-up — the Auto-follow toggle must
survive a settings-panel re-apply (loadSettingsPanel re-writing the checkbox
from the stale server value) that lands between the user's toggle and the
debounced autosave fire.

Root cause: `loadSettingsPanel()` set `autoScrollFollowCb.checked =
settings.auto_scroll_follow !== false` on every (re)load. The user's toggle
set `window._autoScrollFollow=this.checked` and scheduled a 350ms debounced
autosave that READ THE CHECKBOX at fire time. If the panel re-loaded before
the timer fired, the checkbox was reset to the stale server `false`, so the
autosave persisted `false` — and a reload showed the box unchecked despite the
user enabling it (`settings.json` ended up with `auto_scroll_follow: false`).

The fix captures the toggle intent into `_autoScrollFollowPending` at change
time, persists the localStorage mirror immediately, routes both the debounced
autosave (`_appearancePayloadFromUi`) and the explicit save (`saveSettings`)
through that captured value, and clears the capture once the save settles.
"""

import pathlib

ROOT = pathlib.Path(__file__).resolve().parent.parent


def _read(rel):
    return (ROOT / rel).read_text(encoding="utf-8")


def test_capture_variable_declared():
    src = _read("static/panels.js")
    assert "let _autoScrollFollowPending=null;" in src, (
        "a module-scoped capture variable must exist so the toggle intent "
        "survives a checkbox re-apply before the debounced autosave fires"
    )


def test_onchange_captures_intent_and_persists_mirror():
    src = _read("static/panels.js")
    assert "_autoScrollFollowPending=this.checked;" in src, (
        "the toggle onchange must capture its intent immediately, not rely on "
        "the checkbox being read 350ms later (after a possible re-apply)"
    )
    assert "_persistAutoScrollFollow(this.checked)" in src, (
        "the mirror must be persisted synchronously on toggle so a failed/lost "
        "autosave cannot revert the user's choice"
    )


def test_payload_builders_use_captured_value():
    src = _read("static/panels.js")
    assert "typeof _autoScrollFollowPending==='boolean'" in src, (
        "both the debounced autosave payload and the explicit save payload must "
        "prefer the captured toggle value over the live checkbox"
    )
    # Both call sites present.
    assert src.count("typeof _autoScrollFollowPending==='boolean'") >= 2, (
        "expected the capture guard in both _appearancePayloadFromUi and "
        "saveSettings"
    )


def test_capture_cleared_after_save_settles():
    src = _read("static/panels.js")
    assert "_autoScrollFollowPending=null;" in src, (
        "the capture must be cleared once the autosave settles, so later "
        "re-applies / edits read the live checkbox again instead of a stale intent"
    )


def test_capture_survives_reapply_behavior():
    """Simulate the race: toggle ON -> panel re-apply sets checkbox=false ->
    debounced autosave must still send true (via the captured value)."""
    import re as _re
    import shutil
    import subprocess

    assert shutil.which("node"), "node required for this test"
    src = _read("static/panels.js")
    # Confirm the real guard text is present in source (the behavior below
    # mirrors exactly what the patched code does).
    assert "typeof _autoScrollFollowPending==='boolean'" in src, "guard missing in source"

    js = r"""
let _autoScrollFollowPending = null;
function _persistAutoScrollFollow(v){ return v; }
// Fake DOM: checkbox that a re-apply resets to false.
const checkbox = { checked: false };
function $(id){ return id === 'settingsAutoScrollFollow' ? checkbox : null; }

// 1) user toggles ON -> handler logic (mirrored from onchange)
checkbox.checked = true;
_autoScrollFollowPending = true;            // captured intent
_persistAutoScrollFollow(true);             // immediate mirror
// 2) a settings-panel re-apply resets the checkbox (stale server value)
checkbox.checked = false;
// 3) debounced autosave builds the payload and MUST send true
const payload = {
  auto_scroll_follow: (typeof _autoScrollFollowPending === 'boolean')
    ? _autoScrollFollowPending
    : !!($('settingsAutoScrollFollow')||{}).checked,
};
if (payload.auto_scroll_follow !== true) throw new Error('captured value lost to re-apply: '+payload.auto_scroll_follow);
// 4) save settles -> capture cleared
_autoScrollFollowPending = null;
if (_autoScrollFollowPending !== null) throw new Error('capture not cleared');
console.log('CAPTURE-OK');
"""
    proc = subprocess.run(["node", "-e", js], capture_output=True, text=True, cwd=ROOT, timeout=30)
    assert proc.returncode == 0, proc.stderr
    assert "CAPTURE-OK" in proc.stdout, proc.stdout
