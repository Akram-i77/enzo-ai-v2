# ENZO → GMGN Migration — Final Technical Report
**Date:** 2026-08-05 (session 2, after ENZO_GMGN trial)
**Scope:** Original ENZO (`enzo_*.py`) now depends **100% on GMGN API**. Zero Helius / DexScreener / Jupiter / Birdeye / PumpPortal.
**Mode:** Paper trading only. No swap/order ever invoked. BUY reserved for future real mode.

---

## 1. What replaced what

| Old data source | Old module(s) | New GMGN path |
|---|---|---|
| DexScreener (REST newpairs/pairs) | `enzo_fetch.py` (DELETED) | `enzo_gmgn.discover()` → market trending + market trenches |
| Jupiter (price/mcap fallback) | `enzo_fetch_jupiter.py` | Rewritten as 871B adapter → `enzo_gmgn.get_market_data()` |
| Birdeye (security/holders/overview) | `enzo_security.py` (birdeye_overview etc.) | `enzo_gmgn.token_security()` + `holder_distribution()` + `security_scan()` |
| Helius RPC (bonding curve, WS price feed, signatures) | `enzo_curve.py`, `enzo_pricefeed.py`, `enzo_pump.py` | `enzo_gmgn.read_bonding_curve()` / GMGN polling pricefeed / trenches polling |
| PumpPortal WS + Helius getSignaturesForAddress | `enzo_pump.py` | `enzo_gmgn.discover()` filtered to trenches (newest-first) |
| Helius WS accountSubscribe (real-time price) | `enzo_pricefeed.py` | GMGN polling thread (fresh_secs=5, TTL cache 10s) |
| — | `enzo_ws.py` (stdlib WS client) | DELETED (nothing imported it) |

## 2. New unified data layer — `enzo_gmgn.py` (~14KB)
- Subprocess runner for `gmgn-cli` (~0.6s/req) with latency logging, **ban-aware retry** (parses "Rate limit resets at …", sleeps ≤120s, single retry) — same discipline proven in ENZO_GMGN.
- Smart cache with per-category TTLs: market_data 10s / info 30s / security 300s / holders 300s / curve 10s / sol_price 60s / discovery 25s.
- Public API: `discover()`, `token_info()`, `token_security()`, `holder_distribution()`, `holder_count()`, `security_scan()` (same output contract as before), `read_bonding_curve()`, `sol_price_usd()`, `kline()`, `get_market_data()`, `get_live_price()`, `get_live_market_cap()`, `stats()`, CLI `__main__`.

## 3. Logic changes (kept philosophy, GMGN realities)
1. **GMGN JSON shapes** handled in `_price_of()` / `_nested_field()` / `_market_cap_usd()`: prices nested in `price` object; **market cap NOT provided → derived = price × circulating_supply**; pump progress = `launchpad_progress` (not `progress`).
2. **Pre-migration trenches tokens** have empty/zero info → `_candidate_from_discovery()` + `_merge_info_candidate()` fill gaps from cached discovery lists (ZERO extra API calls).
3. **Early-stage concentration exemption**: pre-migration tokens with progress < 15% are EXEMPT from top-holder bundle gates (dev/curve holding 86–100% is normal pre-migration). progress ≥ 15% pre-migration: BUNDLE_DISTRIBUTION hard-reject at top10 > 90% / top1 > 80%; soft CONCENTRATED flag otherwise. Migrated: info-only flag.
4. **`security_axis()` returns the original scan + `score`/`flags`** — enzo_analyze reads `security_status`/`hard_reject` from the axis result (historical contract preserved).
5. **`weighted_confidence.security` tolerated as int** (config has int, not dict).
6. Smartmoney/KOL list endpoints return **empty lists** from this host → discovery relies on trenches + trending (both live-verified, 86 candidates).
7. **No push stream anymore**: pump monitor polls trenches every 30s (config `pump_monitor.interval_sec`); pricefeed polls GMGN (5s per mint, cache-shared).

## 4. Files changed / created / deleted
- **NEW:** `enzo_gmgn.py`
- **REWRITTEN (thin GMGN adapters, interfaces preserved):** `enzo_fetch_jupiter.py`, `enzo_curve.py`, `enzo_security.py`, `enzo_pricefeed.py`, `enzo_pump.py`, `enzo_run.py`
- **TOUCHED:** `enzo_engine.py` (log tag "dexscreener" → "gmgn"), `enzo-portfolio.py` → `enzo_portfolio.py` `current_market_cap()` (GMGN-only), `enzo-config.yaml` (data_sources → gmgn, +chain: sol, pump_monitor polling params), `enzo-secrets.json` (stripped to Telegram only — Birdeye/Helius keys removed)
- **DELETED (moved to `.trash-gmgn-migration/`):** `enzo_fetch.py`, `enzo_ws.py` (nothing imported them)
- **KEPT:** `enzo_audit.py` (pure logic, no network, used by enzo_analyze), all axis modules (wallet_behavior / dev_analysis / market_structure — they consume `merged["security"]` + `merged["signals"]`, no changes needed)

## 5. Live verification (all GMGN-only)
| Test | Result |
|---|---|
| Full pipeline `enzo_run.py <mint>` | **BUY, conf 60.0, ~2.1s** |
| All 6 axes | security 50 / wallet 47 / dev 60 / momentum 67 / structure 88 / liquidity 100 — real GMGN data |
| Security on real pre-migration token | BUNDLE_DISTRIBUTION top10=100%/top1=90.36% → DANGEROUS |
| Test round (12 deep analyses) | 7 WAIT / 5 IGNORE, ~25s round, 0 rate limits |
| `pump.poll_new_pairs()` | 10 trenches candidates in 4.7s |
| `pricefeed` | subscribe → price in ≤5s → unsubscribe OK |
| `portfolio.current_market_cap()` | migrated token → $106,448 |
| All modules | `py_compile` OK |
| `botctl watchdog` | serve + botctl ALIVE |

## 6. Requests per round (rate-limit discipline)
- Discovery: 4 feeds → 1 batch call (bulk), ~3s, 86 candidates.
- Deep analysis: 2 calls per candidate (market data + security scan; holders reuse the security call's data via cache) → 12 candidates ≈ 24 calls.
- **Total ≈ 25 calls / round** paced at ≥350ms gap (≈1 req / 2–3s effective with subprocess latency) → well under the ~1 req/s free-tier limit. Ban-aware retry protects penalty windows.

## 7. Known limitations (GMGN-only, by design — logged as gaps, never substituted)
- No push/streaming: pump discovery is 30s polling, price feed is 5s polling (was sub-second WS). Exit monitor still works, just less real-time.
- `market signal` endpoint returns `[]` → compensated via buys/sells + momentum + smart_degen_count (same as ENZO_GMGN).
- Smartmoney/KOL watch lists empty from this host (data source-side; trenches + trending still give 86 candidates/scan).
- Kline works for migrated tokens; 0 entries pre-migration (bonding curve math fills the gap via `read_bonding_curve`).
- Holder distribution = top-1/top-10 percentages from GMGN holders (paid-tier full distribution not available free).

## 8. Next steps (await user confirmation)
1. Re-enable trading flows (bot currently PAUSED via Telegram since 2026-07-16).
2. Re-enable watchlist-scan cron (`c8b31bd6`) — was disabled due to provider outage, not ENZO.
3. Optional: real-mode execution (wallet + keys — HIGH risk, explicit approval required).
4. First git commit of the whole migrated tree.

**Verdict:** Migration complete and live-verified. ENZO is now a single-source (GMGN) system with the same features and philosophy, lower infra complexity (no Helius/Birdeye/DexScreener keys at all), and disciplined ~1 req/s pacing.
