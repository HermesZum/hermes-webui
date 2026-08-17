"""Regression test: the Appearance settings pane must have an explicit
"Save Settings" button (issue #1360-follow-up).

User reported that every appearance checkbox reverted to unchecked after reload
because Appearance relied solely on a debounced autosave that never POSTed in
their environment (server logs showed zero /api/settings POSTs). Preferences
already has a working Save Settings button that calls saveSettings(); Appearance
must have the same so changes are committed on an explicit user action rather
than depending on the autosave path.
"""

import pathlib

ROOT = pathlib.Path(__file__).resolve().parent.parent


def _read(rel):
    return (ROOT / rel).read_text(encoding="utf-8")


def test_appearance_pane_has_save_button():
    html = _read("static/index.html")
    ap_start = html.index("settingsPaneAppearance")
    ap_end = html.index("settingsPanePreferences")
    appearance_pane = html[ap_start:ap_end]
    assert 'onclick="saveSettings()"' in appearance_pane, (
        "Appearance pane must have an explicit Save Settings button calling saveSettings()"
    )
    assert "settingsAppearanceAutosaveStatus" in appearance_pane, (
        "Appearance pane must keep its autosave status element next to the Save button"
    )


def test_preferences_pane_has_save_button():
    # Regression guard: the working Preferences Save button must remain.
    html = _read("static/index.html")
    assert 'id="settingsPanePreferences"' in html
    assert html.count('onclick="saveSettings()"') >= 2, (
        "both Preferences and Appearance panes should expose a Save Settings button"
    )


def test_save_settings_posts_appearance_keys():
    # saveSettings() builds the same appearance payload and POSTs via the same
    # path as Preferences (which works for the user). Confirm the keys it writes
    # are the ones the autosave was failing to persist.
    src = _read("static/panels.js")
    for key in (
        "auto_scroll_follow",
        "render_user_markdown",
        "session_jump_buttons",
        "session_endless_scroll",
        "large_text_paste_as_attachment",
        "project_quick_create_buttons",
    ):
        assert ("body.%s=" % key) in src, "saveSettings must persist %s" % key
