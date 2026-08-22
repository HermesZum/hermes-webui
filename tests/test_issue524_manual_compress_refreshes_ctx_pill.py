"""Regression coverage for #524-followup: manual /compress must refresh the
context-window pill.

The manual compression path recomputes ``last_prompt_tokens`` on the session
(the post-compression token estimate, far smaller than pre-compress), but the
composer Context-Window pill is driven by ``S.lastUsage`` — a cache that path
never refreshed. The pill therefore kept showing the pre-compression percentage
(and the "needs compress" hint) until the next agent turn.

The fix rebuilds ``S.lastUsage`` from the freshly-compressed session and calls
``_syncCtxIndicator`` immediately inside ``_applyManualCompressionResult``.
"""

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
COMMANDS_JS = (ROOT / "static" / "commands.js").read_text(encoding="utf-8")


def _apply_result_block() -> str:
    start = COMMANDS_JS.find("function _applyManualCompressionResult")
    assert start != -1, "manual compression result handler not found"
    end = COMMANDS_JS.find("function resumeManualCompressionForSession", start)
    assert end != -1, "resume handler after apply handler not found"
    return COMMANDS_JS[start:end]


def test_apply_manual_compression_rebuilds_last_usage_from_session():
    block = _apply_result_block()
    assert "S.lastUsage={" in block, "S.lastUsage must be rebuilt from session"
    assert "S.session.last_prompt_tokens" in block, \
        "must prefer the compressed session token count"
    assert "S.session.post_compression_context_tokens_estimate" in block, \
        "must carry the post-compression estimate"
    assert "S.session.context_length" in block
    assert "S.session.threshold_tokens" in block


def test_apply_manual_compression_resyncs_ctx_pill():
    block = _apply_result_block()
    # The pill must be re-synced right after the compressed session lands so
    # both the composer pill and the mobile context gadget update immediately.
    assert "_syncCtxIndicator(S.lastUsage);" in block, \
        "must call _syncCtxIndicator with the refreshed usage"
    # It must sit inside the S.session-guarded refresh, not just the
    # setCompressionUi block that only builds the divider card.
    assert "if(typeof _syncCtxIndicator==='function' && S.session){" in block
