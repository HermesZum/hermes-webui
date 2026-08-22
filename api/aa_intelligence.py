"""Artificial Analysis Intelligence Index integration (optional, fail-safe).

The Hermes WebUI model picker can sort free / available models by a *real*
measured intelligence score instead of only a curated proxy. Artificial
Analysis publishes an independent "Intelligence Index" (a composite of 9
evaluations) via a free Data API.

Design constraints (see docs/proposed-model-picker-free-intelligence-sort.md):
- The provider APIs (OpenRouter / kilocode) expose NO intelligence score, so
  this is the only real signal available.
- The free API is rate-limited (1,000 req/day) and requires attribution, so we
  fetch ONCE per hour, cache to disk, and never call per-request.
- On ANY failure (no key, offline, 4xx/5xx, rate-limit, parse error) we return
  an empty map and the picker silently falls back to the curated proxy sort.
  The picker must never block or error because AA is unavailable.
- An intelligence score measures raw capability, NOT agent tool-fit. It does
  NOT replace the tool-support gate (Phase 0).

Env / config:
- Enable by setting ARTIFICIAL_ANALYSIS_API_KEY in the Hermes .env (the running
  process reads it; no restart of config files needed, just the server).
- Optional: model_picker.intelligence_source = "artificialanalysis" | "none".
  If the key is present we auto-enable; "none" forces the proxy even with a key.
"""

from __future__ import annotations

import json
import os
import re
import threading
import time
import urllib.request
import urllib.error

__all__ = ["get_intelligence_map", "lookup_intelligence"]

_AA_API_URL = "https://artificialanalysis.ai/api/v2/data/llms/models"
_AA_API_KEY_ENV = "ARTIFICIAL_ANALYSIS_API_KEY"
_CACHE_TTL_SECONDS = 3600  # 1 hour — respects the 1,000 req/day free limit
_CACHE_FILENAME = "aa_intelligence_cache.json"

_lock = threading.Lock()
_cache: dict | None = None
_cache_loaded_at: float = 0.0


def _cache_path() -> str:
    home = os.getenv("HERMES_HOME") or os.path.expanduser("~/.hermes")
    state_dir = os.path.join(home, "webui")
    try:
        os.makedirs(state_dir, exist_ok=True)
    except OSError:
        pass
    return os.path.join(state_dir, _CACHE_FILENAME)


def _normalize_key(value: str) -> str:
    """Canonicalize a model id/name for cross-provider matching.

    Both Artificial Analysis keys and our Hermes model ids are run through this
    so they become comparable. We:
      - lowercase
      - fold / . : _ and spaces into '-'
      - drop the literal 'free' token (our ids use ':free'; AA never encodes
        free-ness in the slug/name)
      - collapse repeated '-' and trim.
    """
    if not value:
        return ""
    v = value.strip().lower()
    # Remove ':qualifier' tokens entirely (e.g. ':free', ':cloud', ':latest',
    # ':q4_k_m', ':instruct'). These are deployment/variant suffixes, NOT part of
    # the model identity. Artificial Analysis NEVER encodes a colon in its slugs
    # (its ~2.1k keys are hyphenated names or UUIDs -- verified: 0 contain ':'),
    # so any ':' in a Hermes model id is a non-identity qualifier that must be
    # dropped before matching. This is the general form of the earlier free/cloud
    # whitelist: a model_name:* suffix is never used to look up the intelligence
    # score, whatever the qualifier happens to be.
    v = re.sub(r":[^/:]*", "", v)
    for ch in ("/", ".", " ", "_"):
        v = v.replace(ch, "-")
    v = re.sub(r"-+", "-", v).strip("-")
    return v


def _enabled() -> bool:
    # Auto-enable when a key is present. model_picker.intelligence_source == "none"
    # can force-disable even with a key.
    if os.getenv("HERMES_MODEL_PICKER_INTELLIGENCE_SOURCE", "").strip().lower() == "none":
        return False
    return bool(os.getenv(_AA_API_KEY_ENV, "").strip())


def _fetch_from_api() -> dict:
    """Return {normalized_key: intelligence_index} from the live AA API.

    Raises on any failure so the caller can fall back to cache/empty.
    """
    api_key = os.getenv(_AA_API_KEY_ENV, "").strip()
    req = urllib.request.Request(_AA_API_URL, headers={"x-api-key": api_key})
    with urllib.request.urlopen(req, timeout=10.0) as resp:
        payload = json.loads(resp.read().decode())
    items = payload.get("data", []) if isinstance(payload, dict) else []
    out: dict[str, float] = {}
    for item in items:
        if not isinstance(item, dict):
            continue
        intel = (item.get("evaluations") or {}).get("artificial_analysis_intelligence_index")
        if intel is None:
            continue
        try:
            intel = float(intel)
        except (TypeError, ValueError):
            continue
        # Index by several normalized keys so our provider/model ids can match.
        for raw in (item.get("slug"), item.get("name"), item.get("id")):
            if isinstance(raw, str) and raw.strip():
                out[_normalize_key(raw)] = intel
        creator = item.get("model_creator") or {}
        cslug = creator.get("slug") if isinstance(creator, dict) else None
        name = item.get("name")
        if isinstance(name, str) and isinstance(cslug, str):
            out[_normalize_key(f"{cslug} {name}")] = intel
    return out


def _load_cache_file() -> dict:
    try:
        with open(_cache_path(), "r", encoding="utf-8") as fh:
            data = json.load(fh)
        if isinstance(data, dict) and "map" in data:
            return data.get("map", {})
    except (OSError, ValueError):
        pass
    return {}


def _save_cache_file(map_data: dict) -> None:
    try:
        with open(_cache_path(), "w", encoding="utf-8") as fh:
            json.dump({"saved_at": time.time(), "map": map_data}, fh)
    except OSError:
        pass


def get_intelligence_map() -> dict:
    """Return {normalized_key: intelligence_index:float}. Cached + fail-safe."""
    global _cache, _cache_loaded_at
    with _lock:
        now = time.time()
        if _cache is not None and (now - _cache_loaded_at) < _CACHE_TTL_SECONDS:
            return _cache
        # Try live fetch first when enabled.
        if _enabled():
            try:
                live = _fetch_from_api()
                if live:
                    _cache = live
                    _cache_loaded_at = now
                    _save_cache_file(live)
                    return _cache
            except Exception:
                # fall through to disk cache / empty
                pass
        # Disk cache (may be stale but still useful).
        disk = _load_cache_file()
        if disk:
            _cache = disk
            _cache_loaded_at = now
            return _cache
        _cache = {}
        _cache_loaded_at = now
        return _cache


def lookup_intelligence(model_id: str, provider: str | None = None) -> float | None:
    """Best-effort intelligence lookup for a Hermes model id like 'openrouter/z-ai/glm-5.2:free'.

    Match strategy (in order), all on normalized keys:
      1. Progressive shorter path suffixes ('openrouter/nvidia/x' -> 'nvidia/x'
         -> 'x') so routing-provider/vendor prefixes don't block a match.
      2. Family-prefix fallback: match the longest AA key that shares this
         model's leading family, where everything AFTER the family in BOTH ids
         consists only of size/quant/version/variant tokens (so we only accept
         the match when it's the SAME base model in a different size/mode, never
         a different model). Example: 'liquid/lfm-2.5-2.6b:free' -> AA 'lfm2-2-6b'
         (same LFM2 family, 2.6B). This recovers the many free models that carry
         a size/version our id encodes but AA indexes under a base name, instead
         of leaving them as 'Undefined' and defeating the intelligence ranking.
         Misattribution is prevented by requiring the non-family remainder to be
         exclusively size/variant tokens on both sides.
    """
    if not model_id:
        return None
    m = get_intelligence_map()
    if not m:
        return None

    # Strip Hermes *routing prefixes* that wrap the real model id. The picker
    # stores custom-provider / proxied models as '@custom:kilocode:tencent/hy3'
    # or '@openrouter:nvidia/nemotron-3-ultra...'. The normalizer folds ':' into
    # '-', fusing '@custom:kilocode:nvidia' into one token and hiding the real
    # vendor family ('nvidia-') from the matcher -- so these models wrongly show
    # as 'unrated' even though OpenRouter (bare ids) score fine. Removing the
    # routing wrapper exposes the canonical id the AA catalog actually keys on.
    # This is a pure prefix-strip: it never touches the trailing vendor/model.
    # Heuristic: the real model id always uses '/' as the vendor/model separator,
    # while routing wrappers use ':'. So we drop everything up to and including
    # the LAST ':' whose remainder still contains a '/'. That boundary is exactly
    # where the routing wrapper ends and 'vendor/model' begins.
    #   '@custom:kilocode:nvidia/nemotron-3-ultra:free' -> 'nvidia/nemotron-3-ultra:free'
    #   '@openrouter:nvidia/nemotron-3-super:free'       -> 'nvidia/nemotron-3-super:free'
    #   'nvidia/nemotron-3-ultra:free' (already bare)     -> unchanged (its only ':'
    #      precedes 'free', whose remainder has no '/', so nothing is stripped)
    _stripped = model_id.strip()
    _last_colon = -1
    for _i, _ch in enumerate(_stripped):
        if _ch == ":":
            # Keep the rightmost ':' whose remainder still has a '/', i.e. the
            # boundary between the routing wrapper and the real 'vendor/model'.
            if "/" in _stripped[_i + 1:]:
                _last_colon = _i
    if _last_colon != -1:
        _cand = _stripped[_last_colon + 1:].strip()
        if _cand:
            _stripped = _cand

    # Tokens that denote size/quant/variant and may differ between our id and
    # AA's base entry WITHOUT changing the underlying model. Version/generation
    # digits (e.g. '4' in gemma-4, '2-5' in lfm-2-5) are deliberately NOT here:
    # a generation change means a DIFFERENT model (gemma-3 vs gemma-4), so it
    # must stay part of the family key to avoid misattributing a score.
    _SIZE_RE = re.compile(r"[0-9]+([.][0-9]+)?[a-z]*b")  # 2.6b, 120b, a12b, 4b
    _VARIANT = {
        "it", "preview", "thinking", "instruct", "flash", "reasoning", "non",
        "vl", "code", "mini", "nano", "micro", "light", "base", "chat",
        "sft", "rl", "a", "v", "x", "s", "xs", "pro", "max", "ultra",
        "super", "note", "high", "low", "medium", "free",
    }

    def _toks(norm: str) -> list[str]:
        return [t for t in norm.split("-") if t]

    def _is_size_or_variant(t: str) -> bool:
        return bool(_SIZE_RE.fullmatch(t) or t in _VARIANT)

    # Collect candidate base ids (suffixes, and provider-qualified) to try.
    # Use the routing-prefix-stripped id so '@custom:kilocode:vendor/model'
    # resolves to 'vendor/model' (matches the AA catalog key).
    parts = [p for p in _stripped.strip().lower().split("/") if p]
    base_ids: list[str] = []
    for i in range(len(parts)):
        base_ids.append(_normalize_key("/".join(parts[i:])))
    if provider:
        base_ids.append(_normalize_key(f"{provider}/{'/'.join(parts[-1:])}"))

    for base in base_ids:
        if not base:
            continue
        # 1) exact match
        if base in m:
            return m[base]
        # 2) family-prefix fallback: find AA keys sharing the leading family,
        #    where the tail beyond the family is exclusively size/variant.
        base_toks = _toks(base)
        # walk from the longest possible family down to a 1-token family
        for fam_len in range(len(base_toks), 0, -1):
            fam = "-".join(base_toks[:fam_len])
            if not fam:
                continue
            our_tail = base_toks[fam_len:]
            if not all(_is_size_or_variant(t) for t in our_tail):
                # a non-size word sits in our tail -> this fam_len is wrong; the
                # real family is longer, so skip (try the next, longer fam)
                continue
            # require at least one more token in AA key beyond family to avoid
            # matching a bare family that AA also lists (e.g. 'hy3' itself).
            best = None
            for aa_key, score in m.items():
                aa_toks = _toks(aa_key)
                if len(aa_toks) <= fam_len:
                    continue
                if aa_toks[:fam_len] != base_toks[:fam_len]:
                    continue
                aa_tail = aa_toks[fam_len:]
                if all(_is_size_or_variant(t) for t in aa_tail):
                    best = score  # same base model, different size/mode
                    break
            if best is not None:
                return best
    return None
