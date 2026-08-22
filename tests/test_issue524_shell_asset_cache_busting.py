"""Regression coverage: the `?v=` cache-bust token must cover every app-shell
JS asset, so browser-immutable-cached copies of static/*.js are re-fetched when
any of them changes on disk.

Background (#524-followup): the manual-/compress context-ring fix lives in
static/commands.js, but `_static_version_token()` hashed only a hardcoded 6-file
list that omitted commands.js. Because static/*.js is served with
`Cache-Control: public, max-age=31536000, immutable`, a browser that cached the
pre-fix commands.js kept serving it — the ring/pill never refreshed until a
manual hard-refresh, even though the server had the fix.

This test asserts every `<script src="static/*.js?v=...">` reference in
index.html is present in the hash list of _static_version_token, so a future
shell asset can't be silently left out again.
"""

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ROUTES_PY = (ROOT / "api" / "routes.py").read_text(encoding="utf-8")
INDEX_HTML = (ROOT / "static" / "index.html").read_text(encoding="utf-8")


def _version_token_hash_list() -> str:
    start = ROUTES_PY.find("def _static_version_token")
    assert start != -1, "_static_version_token not found"
    end = ROUTES_PY.find("_suffix =", start)
    assert end != -1, "hash loop tail not found"
    return ROUTES_PY[start:end]


def test_all_shell_js_assets_are_cache_busted():
    block = _version_token_hash_list()
    # Every shell JS asset referenced with ?v=__WEBUI_VERSION__ must be hashed
    # into the token. (pwa-startup.js is preloaded with the same token.)
    refs = set(
        re.findall(r"static/([a-z0-9_.-]+)\.js\?v=__WEBUI_VERSION__", INDEX_HTML)
    )
    assert refs, "no ?v=__WEBUI_VERSION__ JS references found"
    # shell refs that use the version token
    for asset in sorted(refs):
        assert asset in block, (
            f"{asset} is referenced with ?v=__WEBUI_VERSION__ in index.html but is "
            f"missing from _static_version_token's hash list — its changes will "
            f"not invalidate the immutable browser cache."
        )


def test_commands_js_is_cache_busted():
    # The specific regression: the /compress context-ring fix lives in commands.js.
    assert "commands.js" in _version_token_hash_list(), (
        "commands.js must be hashed by _static_version_token so the /compress "
        "context-ring fix reaches clients without a manual hard-refresh."
    )
