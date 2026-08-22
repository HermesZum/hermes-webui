# Settings → Providers & Model Picker: Free-First, Capability- and Intelligence-Aware Improvements

**Status:** Proposed — research-backed plan (no code yet)
**Author:** Luna (Hermes assistant)
**Date:** 2026-08-17

## 1. Research findings (verified against live catalogs)

Pulled and inspected the real provider payloads (`/tmp/or_models.json`, `/tmp/kilo_models.json`).

### What metadata each model actually carries
- **OpenRouter** (`/v1/models`): `id`, `name`, `context_length`, `architecture.input_modalities`
  (text / image / pdf), `architecture.output_modalities`, `supported_parameters`
  (e.g. `tools`, `reasoning`, `response_format`, `structured_outputs`, `tool_choice`, …),
  `pricing.prompt` / `pricing.completion` (`"0"` ⇒ free), `created` (epoch).
  **No benchmark / intelligence score is exposed.**
- **Kilocode gateway** (`/v1/models`): same shape, **plus** `isFree` (bool) and
  `preferredIndex` (int — the gateway's own curated recommendation order;
  lower = more recommended). `preferredIndex` is the closest native "quality/intelligence"
  signal any provider gives us.

### Confirmed facts that drove the design
- 20 OpenRouter free models and 15 kilocode free models are present.
- Tool-**incapable** free models exist and currently leak into the picker:
  `z-ai/glm-5.2:free`, `nvidia/nemotron-3.5-content-safety:free`, `google/lyria-3-*`.
  Selecting one as the **main** model 404s (`No endpoints found that support tool use`)
  because the agent requires tools. This is the bug we hit on 2026-08-17.
- `providers.<pid>.models` allowlist is honored by the composer picker
  (`config.py:7784`) but **ignored** by the Settings/Providers live endpoint
  (`routes.py:20119 _handle_live_models`), which calls `provider_model_ids()` and
  returns the full unfiltered catalog.
- The OpenRouter free augmentation (`config.py:7485`) **deliberately bypasses** the
  tool-support filter (code comment), so `:free` variants stay visible regardless.

### Honesty constraint
No provider API returns an intelligence/benchmark number. Therefore "sort by intelligence"
is a **transparent proxy**, not a measured IQ. It must be:
(a) derived from real fields, (b) operator-adjustable, (c) labelled as a heuristic in the UI.

## 2. Design principles
- **Respect the allowlist.** `providers.<pid>.models` should gate the live endpoint too.
- **Don't hide by default.** Filters/toggles are opt-in and reversible.
- **No fake benchmarks.** "Intelligence" = curated tier map + provider `preferredIndex` + capability signals; always labelled a heuristic.
- **Use real metadata for capability** (tools / reasoning / vision / context) — that part is not a guess.
- **Use a real third-party intelligence score when available** (see §3b). The *providers*
  (OpenRouter/kilocode) expose **no** intelligence/benchmark field — confirmed against
  live catalogs. But Artificial Analysis publishes a measured Intelligence Index via a
  Data API (free tier), so the picker can use a real score instead of only a proxy.

## 3. Phased plan (highest value first)

### Phase 0 — Correctness (fixes the live 404; prerequisite)
- `routes.py:20119 _handle_live_models`: when `providers.<pid>.models` allowlist exists,
  intersect the live id list with it. For tool-requiring routers, drop ids whose catalog
  entry lacks `tools` in `supported_parameters` **from the selectable-main list** (keep them
  discoverable only as explicitly-flagged "no-tools" entries).
- `config.py:7485` OpenRouter free augmentation: skip `:free` variants lacking `tools`
  (or mark them `(no tools)` and exclude from main-model selection).
- **Result:** `glm-5.2:free` no longer selectable as main → 404 eliminated.

### Phase 1 — Capability enrichment (real metadata)
- New helper in `config.py` near `_configured_model_ids` (1379):
  `model_capability_flags(model) -> {tools, reasoning, vision, context, is_free}`.
- Expose `flags` on every entry in `/api/models` and `/api/models/live`.
- Render compact badges in the picker: `Free` · `Tools` · `Vision` · `Reason` · `{ctx}`.
  These are data-driven and honest.

### Phase 2 — Free-first prioritization (the "prioritize intelligent free models" ask)
- New WebUI settings (in `_SETTINGS_DEFAULTS`, `config.py:9260`):
  - `model_picker.free_first` (default **off** — preserves current behavior)
  - `model_picker.filter_free_only` (toggle / "Free only" chip)
- Float `is_free` / `pricing.prompt == "0"` entries to the top, preserving sub-sort.

### Phase 3 — Intelligence-aware sort
Composite sort key, descending:
1. **Real intelligence score (optional, §3b)** if `intelligence_source` is configured
   and reachable — takes priority over the proxy.
2. `intelligence_tier` — curated model-family→tier map (see §4); kilocode `preferredIndex`
   blends in when present (lower index ⇒ higher tier). Used when no real score is available.
3. `capability_score = tools*3 + reasoning*2 + vision*1`.
4. `context_length` (log-normalized).
5. `recency` (`created`, desc) as tiebreak.
- UI tooltip: *"Ranked by capability & intelligence tier — no public benchmark score is
  available from the provider; a third-party index is used when configured."

#### §3b — Real intelligence score source (Artificial Analysis Data API)
- **Why:** artificialanalysis.ai's *Intelligence Index* (v4.1.1) is a measured composite of
  9 independent evaluations (GDPval-AA v2, τ³-Banking, Terminal-Bench v2.1, SciCode,
  Humanity's Last Exam, GPQA Diamond, CritPt, AA-Omniscience, AA-LCR). It is exposed via a
  **Data API** (`artificialanalysis.ai/data-api/docs`) — free tier (with attribution) +
  paid. This is the only real, programmatically-available intelligence signal found.
- **Config:** new `model_picker.intelligence_source` = `none` (default) | `artificialanalysis`.
  When `artificialanalysis`, set `model_picker.aa_api_key` (or env `AA_DATA_API_KEY`).
- **Behaviour:** on startup / hourly, fetch the model list + Intelligence Index, cache by
  model id, and use the index as the primary sort key (step 1 above). On any failure
  (no key, offline, rate-limit) fall back to the curated proxy (step 2) — never block the
  picker. Attribution string shown per AA's terms.
- **Caveat:** an AA intelligence score measures raw LLM capability, **not** agent tool-fit.
  It does NOT replace the Phase 0 tool gate — `glm-5.2:free` (no `tools`) stays excluded
  from main-model selection even if its AA score is high.*

### Phase 4 — Picker UX
- When `free_first` is on: a top **"Recommended free"** group (top N by composite score),
  with `(no tools)` entries greyed at the bottom.
- `★` marker on the top capability+intelligence tier.

## 4. Curated intelligence tier map (operator-maintained, in `config.yaml`)
Lives under e.g. `model_picker.intelligence_tiers` so it is adjustable without a redeploy.
Suggested ordering from observed free families (high → low):
`nemotron-3-ultra-550b` › `claude/gpt-5.x class` › `nemotron-3-super-120b` ›
`gemini-3` › `nemotron-nano-30b` › `gemma-4` › `stepfun/step-3.7` › `dots-3-note` ›
`lfm-2.5-2.6b` / `nemotron-nano-9b` (lowest). Exact weights documented in the map.

## 5. Code targets (grounded)
- `routes.py:20119` `_handle_live_models` — allowlist + tool gate.
- `config.py:7485` OpenRouter free augmentation — tool gate.
- `config.py:2106` `_OPENROUTER_FREE_TIER_AUGMENT_CAP` — cap constant.
- `config.py:9260` `_SETTINGS_DEFAULTS` — new keys.
- `config.py` ~`1379` — `model_capability_flags` + `model_intelligence_score` helpers.
- `static/` picker render (`panels.js` / `ui.js`) — badges + free-first grouping.
- `static/index.html` + `boot.js` — Settings field + round-trip.

## 6. Verification
- Unit-test the scorer on the 20 OR + 15 kilocode free models cached in `/tmp`.
- Assert `glm-5.2:free` is excluded from the selectable-main list when tool-required.
- Assert sort order: `nemotron-ultra-550b` / Claude-class above `lfm-2.6b` / `nemotron-nano-9b`;
  kilocode `preferredIndex` respected.
- Manual: open Settings/Providers, toggle `free_first`, confirm ordering + badges;
  select a model, confirm no 404.

## 7. Decisions to confirm with operator
1. **Tier map location:** recommend `config.yaml` (adjustable) vs hardcoded constant.
2. **Tool-incapable handling:** recommend *show-greyed + filter*, not hard-remove
   (keeps them usable for auxiliary/non-tool tasks, avoids surprise).
3. **`free_first` default:** recommend **off** (opt-in) to avoid surprising users who pay for premium models.
4. **Intelligence source:** recommend `artificialanalysis` (real measured score) once a
   free API key is provided, with `none`/proxy fallback for air-gapped or keyless installs.
