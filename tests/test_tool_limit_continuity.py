# Tests for the tool-limit terminal UX contract (revised 2026-08-27).
#
# Contract (per Clemente): a tool_limit_reached turn that DELIVERED a final
# answer is NOT an error — the agent's own closing text is the notice, and
# no terminal status card is attached (card-per-capped-turn read as alarm
# spam). The genuine-truncation path (no final answer) still surfaces the
# tool_limit_reached apperror with guidance. This file pins the apperror
# guidance; flow tests live in test_tool_limit_terminal_state.py.

import os

_SRC = os.path.join(os.path.dirname(__file__), "..")


def _read(path):
    with open(os.path.join(_SRC, path), encoding="utf-8") as f:
        return f.read()


def test_streaming_hint_mentions_branch():
    src = _read("api/streaming.py")
    idx = src.find("_err_type = 'tool_limit_reached'")
    assert idx != -1, "tool_limit_reached error type must exist"
    block = src[idx:idx + 600]
    assert "/branch" in block, "Hint should mention /branch for continuation"


def test_no_status_card_attached_on_graceful_tool_limit():
    """The graceful path (final answer delivered) must not attach a status
    card — the marking function was removed and the flow must not set
    _statusCard on the delivered assistant message."""
    src = _read("api/streaming.py")
    assert "_mark_latest_assistant_tool_limit_status" not in src, (
        "removed function must not linger as dead code"
    )
    assert "elif _tool_limit_reached and not _session_lacks_final_assistant_answer" not in src