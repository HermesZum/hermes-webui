# Tests for the tool-limit status card continuity affordance.
#
# Verifies that:
# 1. The backend status card carries terminalState for the frontend.
# 2. The "Next step" row guides the user to continue or fork.
# 3. _statusCardHtml renders a fork-with-context button for tool_limit_reached.
# 4. branchCurrentSession() bridge delegates to cmdBranch.
# 5. The apperror hint mentions /branch.

import os

_SRC = os.path.join(os.path.dirname(__file__), "..")
ROOT = os.path.dirname(_SRC)


def _read(path):
    with open(os.path.join(_SRC, path), encoding="utf-8") as f:
        return f.read()


# ── Backend: status card carries terminalState ──────────────────────────────

def test_status_card_includes_terminal_state():
    from api import streaming
    messages = [
        {"role": "user", "content": "Do the long task."},
        {"role": "assistant", "content": "I reached the limit; here is the summary."},
    ]
    assert streaming._mark_latest_assistant_tool_limit_status(messages) is True
    card = messages[-1]["_statusCard"]
    assert card["terminalState"] == "tool_limit_reached"


def test_status_card_next_step_guides_continue_or_fork():
    from api import streaming
    messages = [
        {"role": "user", "content": "Do the long task."},
        {"role": "assistant", "content": "Summary here."},
    ]
    streaming._mark_latest_assistant_tool_limit_status(messages)
    rows = messages[-1]["_statusCard"]["rows"]
    next_step = next(r["value"] for r in rows if r["label"] == "Next step")
    assert "continue" in next_step.lower()
    assert "fork" in next_step.lower() or "/branch" in next_step


# ── Frontend: _statusCardHtml renders continuity button ──────────────────────

def test_ui_js_renders_continuity_button_for_tool_limit():
    src = _read("static/ui.js")
    assert "terminalState==='tool_limit_reached'" in src
    assert "branchCurrentSession()" in src
    assert "function branchCurrentSession()" in src
    assert "cmdBranch('')" in src


def test_ui_js_branch_current_session_guards_cmdBranch():
    src = _read("static/ui.js")
    assert "typeof cmdBranch==='function'" in src


def test_style_css_has_continuity_styles():
    src = _read("static/style.css")
    assert ".status-card-continuity" in src
    assert ".status-card-continuity-action" in src
    assert ".status-card-continuity-hint" in src


# ── Backend: apperror hint mentions /branch ──────────────────────────────────

def test_streaming_hint_mentions_branch():
    src = _read("api/streaming.py")
    idx = src.find("_err_type = 'tool_limit_reached'")
    assert idx != -1, "tool_limit_reached error type must exist"
    block = src[idx:idx + 600]
    assert "/branch" in block, "Hint should mention /branch for continuation"