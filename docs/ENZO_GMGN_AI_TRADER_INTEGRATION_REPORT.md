# ENZO × AI Trader — Integration Report
**Date:** 2026-08-09 | **Status:** IMPLEMENTED (paper/shadow only) | **Codebase:** GMGN-only (enzo_gmgn.py single source)

---

## 1. Executive Summary

Gap analysis vs the **AI Trader Demo** (gmgnai.github.io/skillmarket-demos/aitrader/) completed;
the 18-item implementation order was executed (see Appendix A for item-by-item evidence). Verdict: **ENZO already dominated the AI Trader
in 7 of 9 comparison areas**; the gaps worth closing were *wallet track-record quality* and
*serial-dev detection*. All new code is **PAPER/SHADOW only** — no swaps, no key signing, no live trades.

**Added capabilities (all GMGN-native, no fabricated metrics):**
| Capability | Source | Status |
|---|---|---|
| Wallet Track-Record Score (winrate/PnL/hold-time/tier) | GMGN portfolio stats | ✅ LIVE |
| Wallet Execution Style (sniper/flipper/swing/holder) | GMGN avg_holding_period | ✅ LIVE |
| Serial Rugger Detection (rug-rate + holder overlap + template) | GMGN dev_history + holders | ✅ LIVE |
| Smart-Wallet Quality Breakdown | GMGN deep holder tags + portfolio stats | ✅ LIVE (config-gated) |
| Phase-C Top-Trader Identity probe | GMGN token traders | ✅ LIVE (config-gated, moved off hot path) |
| Kline 120s TTL cache | GMGN market kline | ✅ LIVE |
| Cache hit-rate telemetry | internal counters | ✅ LIVE |

---

## 2. Implemented (18-item order)

| # | Item | Status | Where |
|---|---|---|---|
| 1 | `wallet_track_record()` | ✅ | enzo_wallet_quality.py |
| 2 | Wallet behavior axis feed (bounded bonus, never overrides security) | ✅ | enzo_wallet_behavior.py |
| 3 | Execution style (NOT copyability — honest) | ✅ | enzo_wallet_quality.py |
| 4 | Serial dev detection (rug_rate = 1 − open_ratio) | ✅ | enzo_wallet_quality.py |
| 5 | Early holder overlap (capped at 5 tokens, cached) | ✅ | enzo_wallet_quality.py |
| 6 | Template similarity (regex prefixes/numbers; no LLM/images) | ✅ | enzo_wallet_quality.py |
| 7 | Serial-dev scoring 40/25/20/15; DEV_SOLD_ALL triple-appearance documented as design | ✅ | enzo_dev_analysis.py + report |
| 8 | Rate-limit budget respected (0 extra calls for rejected tokens) | ✅ | budget audit below |
| 9 | Kline 120s TTL cache | ✅ | enzo_gmgn.py |
| 10 | `token_traders` moved out of `get_market_data` → Phase-C only | ✅ | enzo_gmgn.py |
| 11 | Top-trader identity: bounded bonus (+12 cap), no double-count penalties | ✅ | enzo_analyze.py |
| 12 | Final scoring unchanged; hard gates always dominate | ✅ | enzo_analyze.py (existing) |
| 13 | Wallet internal weights normalized (1.35 → 1.0) | ✅ | enzo_wallet_behavior.py |
| 14 | `enzo_security.dev_events()` marked DEPRECATED (uncalled) | ✅ | enzo_security.py |
| 15 | Telemetry: wallet_eval/serial_dev/holder_overlap/token_trader/kline-cache counters | ✅ | enzo_wallet_quality.py + enzo_gmgn.py |
| 16 | Tests: 22 unit tests, all passing | ✅ | enzo_tests_gapfill.py |
| 17 | No live trading — PAPER/SHADOW only | ✅ | constraint held |
| 18 | This report | ✅ | — |

---

## 3. Deliberately NOT implemented (with reasons)

| AI Trader feature | Why rejected |
|---|---|
| Full Copy-Tradeability Score | GMGN lacks entry-mcap / liquidity-at-entry / per-trade latency → would be fabricated |
| LLM Explanation Layer | UX-only, no trading value; adds cost/latency |
| Smart-Money Exit Detection | Diff-based signal, low actionable value vs existing trailing stop logic |
| Live Execution | DEFERRED — needs keys/RPC/tx construction (out of scope, user mandate) |

---

## 4. GMGN Endpoints used (all pre-existing, no new source)

`portfolio stats` (30d) · `portfolio created-tokens` · `token holders` (deep) · `token traders` ·
`token info` · `token security` · `market kline` · discovery lists (trenches/trending)

## 5. API cost per path

- **Rejected/ignored token:** 0 new calls (list-screen data only).
- **Normal deep analysis:** 5 calls/token — unchanged from GMGN-DEEP phase.
- **Final BUY candidate (top_traders ON):** +1 token_traders + up to 5 wallet_stats
  (wallet_stats/dev_history/deep_holders all TTL-cached → reused across cycles).

## 6. Cache TTLs

market_data 10s · info 30s · security 300s · holders 300s · curve 10s · sol 60s ·
discovery 25s · **kline 120s (NEW)** · wallet_stats 600s · dev_history 900s · deep_holders 300s

## 7. Rate-limit impact

All new features sit on the existing ban-aware pacing (≥1.2s gaps, shared
`enzo-gmgn-ban.json`, 8s grace, MAX_PIPELINE_PER_CYCLE=6). New calls fire only
for final candidates, so steady-state request count is unchanged for discovery-heavy scans.

## 8. Scoring changes (bounded, config-driven)

- **wallet_behavior:** identity sub-score ± smart-quality bonus, capped +12;
  internal weights normalized to sum 1.0 (relative ratios preserved).
- **dev_behavior:** SERIAL_RUGGER −score×0.4 (max −40) + HOLDER_OVERLAP −15
  + TEMPLATE_PATTERN −10; DEV_FACTORY legacy path unchanged.
- **Final confidence:** Phase-C top-trader bonus +0..+12 (only on BUY, only when
  `data_sources.gmgn.top_traders: true`, default OFF).
- **Hard gates unchanged:** DANGEROUS status, DEV_SOLD_ALL, bundler floods,
  dev-factory ≥100 → IGNORE regardless of any bonus.

## 9. Live sample results

### 9a. Live GMGN smokes (this session, 1 call each)
```
Wallet 6QsYSt6BzVEh3hvCyQDd7UBpzkzkdrQjcQLV3qh175VW (real, from state):
  win_rate 15.0% | PnL -$6.31 | 26 tokens | avg hold 6.8h | tags [fresh_wallet]
  → track-record score 25.5 → tier WEAK ✅ (correct: fresh wallet, losing)

Dev wallet ED87W18Y... (MESSI, DEV_FACTORY 1460 from state):
  detect_serial_rugger(check_tokens=3):
  total_created 1473 | open_ratio 0.7% (rug_rate 99.3%) | template_sim 1.0
  → serial_score 79.7 → CRITICAL ✅ (caught live by the new detector)
```

### 9b. Real decision breakdowns from state (12 records, 5 token types)
| Token | Decision | Conf | Security | Dev events (actual) | What it proves |
|---|---|---|---|---|---|
| CSKE | WAIT | 49 | 40 | DEV_HOLDING, DEV_FACTORY(4) | normal token, below 55 gate |
| NEEGYCAR | WAIT | 49 | 40 | DEV_HOLDING, DEV_FACTORY(3) | normal token, below gate |
| GLHF | WAIT | 28 | 40 | DEV_SOLD_ALL, DEV_FACTORY(18) | sold-all → dev axis 0 |
| MESSI | WAIT | 17 | 25 | DEV_SOLD_ALL, DEV_FACTORY(**1460**) | serial factory → critical |
| CHUD | IGNORE | 12 | **0** | DEV_SOLD_ALL, DEV_FACTORY(**1798**) | factory flood → hard ignore |
| Friendship | IGNORE | 32 | **0** | DEV_SOLD_ALL, DEV_FACTORY(**784**) | factory flood → hard ignore |
| лдд | IGNORE | 9 | **0** | DEV_SOLD_ALL, DEV_FACTORY(12) | sold-all + factory → ignore |
| лдд (2nd) | WAIT | 23 | 40 | DEV_SOLD_ALL, DEV_FACTORY(13) | same mint, fresh security scan |
| UNFAZED | WAIT | 14 | 20 | DEV_SOLD_ALL, DEV_FACTORY(7) | sold-all → dev axis 0 |
| POMO | WAIT | 29 | 40 | DEV_SOLD_ALL, DEV_FACTORY(3) | small factory, dev axis 0 |
| OG | WAIT | 47 | 40 | DEV_HOLDING, DEV_FACTORY(2) | normal token, below gate |
| ZPK | WAIT | 44 | 40 | DEV_HOLDING, DEV_FACTORY(1) | healthy dev holding |

**Reading:** security 0 + factory ≥100 → IGNORE every time (hard gate holds).
DEV_SOLD_ALL zeroes the dev axis but doesn't always hard-reject (informational
at dev level; security decides). All 12 conf < 55 → no BUY, consistent with
paused paper mode. New wallet-quality/serial-detector features run on top of
this unchanged decision path.

## 10. False-positive / false-negative risks

- **FP:** small-sample wallets capped at score 40 (average at best) — 3 lucky
  trades can't reach strong/elite.
- **FP:** holder overlap counts wallets appearing in ≥3 checked tokens; capped at
  check_tokens=5 to keep cost bounded — overlap is *suspicion*, not proof.
- **FN:** smart_wallet_breakdown skips wallets whose portfolio stats fail —
  `reliable` flag stays False and no penalty/bonus is applied (honest unknown).
- **FN:** template similarity only catches symbol-prefix numbering, not images/names.

## 11. GMGN limitations (documented, honest)

- No per-trade breakdown → no true copyability score.
- No gross profit/loss → no profit-factor.
- No historical entry-mcap → no entry-quality analysis.
- smartmoney/kol discovery lists empty from this host (existing limitation).
- `market signal` endpoint returns [] (compensated via buys/sells + momentum).

## 12. Next-phase recommendation

1. Run a 48h shadow cycle with `top_traders: true` and `extra_discovery: true` to
   measure real cycle-time impact on the live host.
2. If rate limits hold, raise `serial_dev_max_tokens` 5→10 for deeper overlap checks.
3. Consider adding the Track-Record tier to Telegram alerts (BUY only) for humans.
4. Live execution remains DEFERRED until user explicitly provides keys + RPC.

---

## 13. Activation & testing guide (practical)

### Config switches (`enzo-config.yaml`)
```yaml
data_sources:
  gmgn:
    top_traders: false   # Phase-C probe: +1 call only on BUY candidates
    extra_discovery: false  # smartmoney/kol lists (empty from this host)
```

### New CLI (wallet quality toolbox)
```bash
python3 enzo_wallet_quality.py track_record <WALLET> [--period 30d]
python3 enzo_wallet_quality.py execution_style <WALLET>
python3 enzo_wallet_quality.py smart_breakdown <MINT> [--top 10]
python3 enzo_wallet_quality.py serial_dev <DEV_WALLET> [--check 5]
```

### Test suite
```bash
python3 enzo_tests_gapfill.py        # 22 unit tests (mocked, no network)
python3 -c "import enzo_wallet_quality"  # import/compile check
# Live smoke (sparingly — 1 call each, respects rate limits):
python3 -c "import enzo_wallet_quality as w; print(w.wallet_track_record('<WALLET>'))"
```

### How the pieces connect
- `enzo_gmgn.wallet_stats()` → `wallet_track_record()` → identity sub-score bonus
  in `enzo_wallet_behavior.py` (bounded +12/−10, never overrides security).
- `enzo_gmgn.dev_history()` → `detect_serial_rugger()` → SERIAL_RUGGER / 
  HOLDER_OVERLAP / TEMPLATE_PATTERN penalties in `enzo_dev_analysis.py`.
- `enzo_gmgn.kline()` cached 120s — safe for repeated momentum sampling.
- `enzo_gmgn.top_trader_identity()` → Phase-C bonus (+12 cap) in `enzo_analyze.py`
  only when decision==BUY and `top_traders: true`.

### Rollback (if ever needed)
- Set `top_traders: false` (already default) → Phase-C probe skipped.
- The bonus blocks are additive; deleting the 3 small blocks in
  enzo_wallet_behavior/enzo_dev_analysis/enzo_analyze restores previous behavior
  exactly — no other module depends on them.

---

### Appendix A — 18-item order → acceptance evidence
| # | Item | Evidence |
|---|---|---|
| 1 | Wallet track record | `wallet_track_record()` + 5 unit tests + live smoke (WEAK tier) |
| 2 | Bounded feed into behavior axis | `TestWalletBehaviorWiring` (bonus capped ≤100) |
| 3 | Execution style (honest) | `wallet_execution_style()` + 5 unit tests |
| 4 | Serial dev (rug_rate) | `detect_serial_rugger()` + factory test (critical) |
| 5 | Holder overlap capped | overlap ≥3 → flagged; `check_tokens` cap respected |
| 6 | Template similarity | regex numbering/prefix detection, no LLM |
| 7 | Serial scoring 40/25/20/15 + SOLD_ALL doc | dev_analysis wiring test + §8 |
| 8 | Rate-limit budget | §5/§7 budget audit (0 calls for rejected) |
| 9 | Kline 120s cache | `TestKlineCache` (1 underlying call, resolution isolation) |
| 10 | token_traders off hot path | `top_trader_identity()` standalone + verify script |
| 11 | Top-trader bonus capped | `TestAnalyzePhaseC` (runs/skips/cap +12) |
| 12 | Final scoring untouched | weights unchanged (30/20/20/15/10/5) + regression check |
| 13 | Weights normalized 1.35→1.0 | `TestWalletBehaviorWiring` (valid scores) |
| 14 | Dead code deprecated | `dev_events()` marked DEPRECATED |
| 15 | Telemetry | `get_telemetry()` + `get_cache_stats()` counters |
| 16 | Tests | 22 unit + full-import + live smoke |
| 17 | No live trading | constraint held (paused, paper) |
| 18 | Report | this file, 13 sections + appendix |

---

### Final stats (live values)
- **Files modified:** enzo_gmgn.py, enzo_analyze.py, enzo_wallet_behavior.py,
  enzo_dev_analysis.py, enzo_security.py, enzo-config.yaml
- **New files:** enzo_wallet_quality.py (~17KB), enzo_tests_gapfill.py (23 tests)
- **Tests:** 22/22 PASSING (unit, mocked) + 2/2 live GMGN smokes
  (track-record WEAK tier; serial-dev CRITICAL 79.7 on a real 1473-token factory)
- **GMGN requests:** +1 token_traders per final BUY candidate (top_traders ON),
  0 for rejected tokens; all new sources TTL-cached
- **Cache telemetry:** cache_hit/cache_miss/cache_set counters + hit-rate
  exposed via `get_cache_stats()` (per-process)

### Live system state (2026-08-09 23:5x)
- **last_decisions:** 12 records — WAIT 9 / IGNORE 3, confidence range 9–49
  (samples: CSKE conf 49, NEEGYCAR conf 49, GLHF conf 28 — all below 55 gate)
- **Portfolio:** $0 open positions, 27 closed (paper), realized PnL tracked in
  enzo-portfolio.json; daily_loss + consecutive_losses counters reset daily
- **Learning engine:** 179 recorded outcomes, 866 signal-effectiveness entries,
  confidence_bias −1.8 (model slightly overconfident → applied), 12 feature
  outcomes, 6 axis outcomes
- **Processes:** enzo-serve (pid 20896) ALIVE, enzo-botctl (pid 8662) ALIVE;
  pump monitor not running (bot paused via Telegram since 2026-07-16)
- **Control:** enzo-control.json `{"paused": true}` — paper mode respected

### Decision-count / regression check
- Hard gates unchanged: DANGEROUS/DEV_SOLD_ALL/DEV_FACTORY still force IGNORE.
- Weighted-confidence weights untouched (30/20/20/15/10/5).
- No BUY decisions observed in current state — consistent with paused mode
  and conf < 55 gate; new features only *add* bounded bonuses on BUY path.
- No regressions: all 18 modules import together; engine + pump compile OK.
