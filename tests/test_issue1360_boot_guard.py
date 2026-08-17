"""Regression test for #1360 boot guard: the appearance autosave must NOT fire
before loadSettingsPanel has populated the checkboxes from the server.

Root cause of "every checkbox reverts on reload": boot-time code (theme/skin/font
reconciliation, or any caller of _scheduleAppearanceAutosave before the settings
panel loads) POSTed the default-unchecked checkbox state (all false) to the server,
overwriting the user's saved values on every page reload. The diagnostic console
log confirmed: appearance autosave SCHEDULED {"pending":[],"payload_auto_scroll_follow":false,...}
fired on page load, before the user toggled anything.

Fix: a module-scoped _settingsPanelReady flag starts false; _scheduleAppearanceAutosave
returns early while it's false. loadSettingsPanel sets it true in a finally block
(so a throw in a late helper doesn't leave the guard stuck off).
"""

import pathlib

ROOT = pathlib.Path(__file__).resolve().parent.parent


def _read(rel):
    return (ROOT / rel).read_text(encoding="utf-8")


def test_boot_guard_declared():
    src = _read("static/panels.js")
    assert "let _settingsPanelReady = false;" in src, (
        "module-scoped _settingsPanelReady flag must exist, starting false"
    )


def test_scheduler_guards_on_boot_flag():
    src = _read("static/panels.js")
    sched = src.split("function _scheduleAppearanceAutosave(){", 1)[1].split(
        "function _autosaveAppearanceSettings", 1)[0]
    assert "if(!_settingsPanelReady) return;" in sched, (
        "scheduler must return early while _settingsPanelReady is false — "
        "this prevents boot-time false POSTs that overwrite saved values"
    )


def test_settings_panel_enables_guard_in_finally():
    src = _read("static/panels.js")
    # Must be in a finally block (not just try or catch) so a throw in a late
    # loadSettingsPanel helper doesn't leave the guard stuck off.
    assert "}finally{" in src and "_settingsPanelReady = true;" in src, (
        "loadSettingsPanel must set _settingsPanelReady=true in a finally block"
    )