# ENZO → Pump.fun Advanced-API Hybrid (2026-08-11) — DONE

## What was built
Dual-source architecture: **Pump.fun advanced-api-v2 = wide cheap net**, **GMGN = deep probes only for survivors**. Goal: cut GMGN request pressure ~85%, kill the chronic ban loop.

### New file: `enzo_pump_adv.py` (complete layer)
- `graduated(limit, offset)` — GET /coins/graduated (migrated tokens, rich cards)
- `recent(...)` — GET /coins/list (recent creations incl pre-migration)
- `kolscan(...)` — GET /coins/kolscan (KOL-traded)
- `batch_metadata(mints)` — POST /coins/metadatas (~30/call, 0.4s, cached 30s)
- `metadata(mint)` / `deep_metadata(mint)` — GET /coins/metadata/{mint} deep risk fields (bundler%, is_banned, reuse counts, fees) cached 120s
- `screen_candidate(cand, config, deep)` — pure risk filter (ZERO GMGN calls): twitter/telegram/website reuse (serial dev), dev_hold%, bundler%, sniper_count, sniper_owned%, top10%, holders, mcap, volume; `is_pre` (progress<15%) exempt from dev-hold/liq/vol gates; returns `{verdict, reasons, hard}`
- `discovery_screen(cands)` — dedupe + ONE batch POST + screen all; returns (passed, skipped)
- `enrich_survivor(card)` — 1 call per final survivor → `card["pump_deep"]` (bundler_pct, is_banned, tg/ws/tw reuse, snipers, tx fees, priority fee, ATH, dev hold amt)
- `discover()` — wide sweep (3-4 calls, cached 30s), deduped by mint
- TTL cache, polite pacing (request_gap_ms), `enabled` toggle, never raises
- CLI: `--graduated N --recent N --kol N --batch N --metadata <mint>`

### Wired into the pipeline
- **`enzo_engine.py` scan_once**: Phase-A1 (pump) runs BEFORE GMGN discover. pump survivors (source=pump_*) merge into the candidate pool and skip GMGN list_screen (their fields differ). `state["pump_skips"]` counter. Whole block in try/except → GMGN-only fallback on pump failure.
- **`enzo_gmgn.py` discover()**: if long ban (>30s) active → return [] (pump pool carries the cycle) instead of stalling.
- **`enzo_run.py` run(mint, pump_card)**: optional pump_adv enrichment → `merged["pump_deep"]`, `data_sources_used` includes "PUMP_ADV".
- **`enzo_analyze.py`**: pump_deep axis penalties (soft, never hard): bundler ≥30% → −25 dev_behavior; twitter_reuse ≥3 → −20; telegram_reuse ≥4 → −10; is_banned → −30 security. Added to `supporting` list as "pump:..." reasons.
- **`enzo_engine.py` deep loop**: if `gmgn.ban_status() > 20` → defer candidate (skip, no stall).
- **`enzo-config.yaml`**: `data_sources.pump_advanced` {enabled, request_gap_ms, batch_size, use_kolscan, use_recent, thresholds{...}, penalties{...}}.

## Live test evidence (2026-08-11)
- Discovery: 83-88 pump candidates → 21-24 PASS / 6-9 SKIP with ~4 HTTP calls, ZERO GMGN.
- Batch POST: 30 cards in 0.37-0.76s. Enrich 5 survivors = 5 calls in 1.3s.
- Penalties verified: synthetic bundler 45% + tw_reuse 5 → conf 54 → 45 (−9 pts).
- Full scan_once with GMGN banned: completed in **8s** (defer all deep, pump pool processed) vs 280s+ stall before.
- GMGN discover during ban: returns 176 candidates when healthy; skips to pump pool when banned >30s.
- Hybrid live decisions observed: "pump:bundler 34% (-25)" merged with GMGN security on BBL (WAIT 37); HAZE IGNORE (DEV_FACTORY 1926); AU IGNORE (BUNDLE_DISTRIBUTION 100%).
- Fallback verified: `enabled: false` → `discover()` returns [] → engine proceeds GMGN-only (old behavior).

## GMGN ban reality (observed again today)
Even pure discovery (market trending/trenches/kol) triggers bans on this host — 5+ consecutive bans during one scan_once. The hybrid is not just optimization, it's resilience: pump layer works while GMGN is banned.

## Files changed
- NEW `enzo_pump_adv.py`
- `enzo_engine.py` (Phase-A1 before GMGN, pump pool merge, ban deferral in deep loop)
- `enzo_gmgn.py` (long-ban short-circuit in discover())
- `enzo_run.py` (pump_card param + pump_deep merge)
- `enzo_analyze.py` (pump_deep penalties block)
- `enzo-config.yaml` (pump_advanced config + thresholds + penalties)

## Notes / next steps
- `max_depth_analyses: 12` — when GMGN is healthy, 12 survivors get full GMGN deep (security+identity+dev_history) ≈ 60 calls — still the same budget as before, but only 12 tokens instead of 30+.
- If pump API breaks: set `data_sources.pump_advanced.enabled: false` → GMGN-only (verified).
- Consider lowering GMGN deep calls per token (e.g. skip dev_history when pump deep already flags serial dev) — future tuning.
