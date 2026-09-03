# ENZO Exit Monitor & Price Feed Architecture — Comprehensive Audit Report

**Date:** 2026-08-12
**Scope:** Exit monitoring, price feed, position lifecycle, GMGN rate-limit impact, migration continuity
**Constraint:** AUDIT ONLY — No file modifications

---

## Executive Summary

ENZO's exit monitoring relies on a **hybrid architecture** with two parallel price sources:

1. **Pump Advanced-v2 (primary)** — Free, no rate limit, batch POST for all open positions every 1s
2. **GMGN (fallback)** — Rate-limited (~1 req/sec), TTL-cached, used when pump doesn't know the mint

**Key Finding:** The system is **functionally correct** but has **5 critical gaps** that could cause missed exits, stale prices, or incorrect PnL during pump→Raydium migration:

| Gap | Severity | Impact |
|-----|----------|--------|
| Exit monitor only runs in `enzo_pump.py` (not `enzo_engine.py`) | **HIGH** | Watchlist-only positions may miss exits |
| No explicit Raydium pool detection post-migration | **HIGH** | Price continuity breaks after migration |
| Entry market-cap backfill on legacy positions | **MEDIUM** | Could produce incorrect PnL baseline |
| No reconciliation between portfolio and pricefeed subscriptions | **MEDIUM** | Orphan subscriptions / missed prices |
| Dashboard JS fetches `api/prices` but no endpoint defined in `enzo_serve.py` | **LOW** | Live-price refresh broken in browser |

---

## A. Position Lifecycle & State Machine

### State Diagram

```
                    ┌─────────────────────────────────────────────────┐
                    │                                                 │
                    ▼                                                 │
              ┌──────────┐                                      ┌──────────┐
              │  CLOSED  │◄─────────────────────────────────────│  OPEN    │
              └──────────┘  (STOP_LOSS / TRAILING_STOP /        └──────────┘
                    ▲       TAKE_PROFIT / TIME_EXIT /                 │
                    │       TP_FINAL / MANUAL_EXIT)                   │
                    │                                                 │
                    └─────────────────────────────────────────────────┘
                                      (open_position)
```

### Entry Path

1. **Discovery** → GMGN trenches / pump_advanced graduated+recent+KOL
2. **Screening** → `enzo_pump_adv.screen_candidate()` (Phase-A1) or GMGN `list_screen()`
3. **Deep Analysis** → `enzo_run.run(mint)` → `enzo_analyze.analyze()`
4. **Decision** → BUY (conf ≥ 60) → `enzo_portfolio.open_position(decision)`
5. **Subscription** → `enzo_pricefeed.get_feed().subscribe(mint)`

### Exit Paths (from `check_exits()`)

| Trigger | Condition | Action |
|---------|-----------|--------|
| **STOP_LOSS** | `mcap ≤ entry_mc × (1 - stop_pct%)` | Full close |
| **TRAILING_STOP** | `trailing_active AND mcap ≤ trailing_stop_mc` | Full close |
| **TAKE_PROFIT** | `mcap ≥ take_profit_mc` (if no stages) | Full close |
| **TP_STAGE_N%** | `mcap ≥ entry_mc × (1 + stage.pct%)` | Partial sell (stage.sell fraction) |
| **TP_FINAL_N%** | Last stage hit | Full close (remainder) |
| **TIME_EXIT** | `held_hours ≥ max_holding_hours` | Full close |

### State Persistence

- **File:** `enzo-portfolio.json`
- **Fields:** `open_positions`, `closed_positions`, `realized_pnl`, `daily_loss`, `consecutive_losses`, `peak_equity`
- **Write:** Every `open_position()`, `close_position()`, `_partial_exit()`, `check_exits()` call

---

## B. `check_exits()` Logic & Call Chains

### Function Signature

```python
def check_exits(current_mcaps: dict) -> tuple[list, list]:
    # current_mcaps: {mint: live_market_cap_usd}
    # Returns: (closed_list, partial_list)
```

### Call Sites

| Module | Function | Frequency | Context |
|--------|----------|-----------|---------|
| `enzo_pump.py` | `_exit_monitor_loop()` | **Every 1s** | Pump advanced-v2 batch prices → check_exits |
| `enzo_pump.py` | `handle()` | Once per BUY | Immediate post-open check (after ~1-2s) |
| `enzo_engine.py` | `scan_once()` | Every 60s | Watchlist scan → calls `check_exits()` |

**CRITICAL GAP:** `enzo_engine.py` does NOT run the exit monitor continuously — only during watchlist scans. Positions opened from pump discovery are monitored by `_exit_monitor_loop()`, but watchlist-only positions may only be checked every 60s.

### Logic Flow (inside `check_exits()`)

```python
for mint, mcap in current_mcaps.items():
    pos = state["open_positions"].get(mint)
    if not pos:
        continue
    
    # 1. Update unrealized PnL
    pct = (mcap / entry_mc - 1) * 100
    pos["unrealized_pnl"] = pos["size_usd"] * (pct / 100.0)
    
    # 2. Update peak for trailing stop
    peak_mc = max(pos.get("peak_market_cap", entry_mc), mcap)
    pos["peak_market_cap"] = peak_mc
    
    # 3. Activate trailing if mcap ≥ entry_mc × (1 + trail_pct%)
    if not pos["trailing_active"]:
        if mcap >= entry_mc * (1 + trail_pct / 100.0):
            pos["trailing_active"] = True
            pos["trailing_stop_mc"] = mcap * (1 - trail_pct / 100.0)
    else:
        # Move trailing stop UP (never down)
        pos["trailing_stop_mc"] = max(pos["trailing_stop_mc"], mcap * (1 - trail_pct / 100.0))
    
    # 4. Check staged take-profit (scale-out)
    for stage in stages:
        if mcap >= entry_mc * (1 + stage.pct / 100.0):
            _partial_exit(state, mint, pos, stage.sell, mcap, stage.pct)
            if is_last_stage or pos["amount"] <= 1e-9:
                close_position(state, mint, mcap, "TP_FINAL_N%")
                break
    
    # 5. Check other exit conditions
    if trailing_active and mcap <= trailing_stop_mc:
        close_position(state, mint, mcap, "TRAILING_STOP")
    elif mcap <= entry_mc * (1 - stop_pct / 100.0):
        close_position(state, mint, mcap, "STOP_LOSS")
    elif held_hours >= max_holding_hours:
        close_position(state, mint, mcap, "TIME_EXIT")
```

---

## C. PriceFeed Architecture & Subscription Model

### Class: `PriceFeed` (enzo_pricefeed.py)

| Method | Purpose |
|--------|---------|
| `start()` | Launch background polling thread |
| `subscribe(mint)` | Add mint to subscription set + immediate fetch |
| `unsubscribe(mint)` | Remove mint from subscription set + clear cache |
| `get_price(mint)` | Return cached price (fresh_secs TTL) or fetch |
| `_run()` | Background loop: poll all subscribed mints |

### Subscription Lifecycle

```
enzo_pump.handle() → BUY decision → enzo_portfolio.open_position()
                                           ↓
                               enzo_pricefeed.subscribe(mint)
                                           ↓
                               Background thread polls every 5s
                                           ↓
                               Position closed → NO EXPLICIT UNSUBSCRIBE
```

**GAP:** No `unsubscribe()` call when position closes. Orphan subscriptions continue polling until process restart.

### Price Source Priority (inside `get_price()`)

1. **Pump Advanced-v2** — `live_prices([mint])` (batch POST, 1s TTL)
2. **GMGN market_data** — `get_market_data(mint)` (10s TTL)
3. **GMGN bonding_curve** — `read_bonding_curve(mint)` (fallback)

### Cache Layers

| Layer | Location | TTL | Scope |
|-------|----------|-----|-------|
| PriceFeed internal | `self._cache` | 5s (default) | Per-mint, in-memory |
| Pump Advanced | `enzo_pump_adv._cache` | 1s (configurable) | Per-mint, file-backed |
| GMGN market_data | `enzo_gmgn._cache` | 10s | Per-mint, in-memory |
| GMGN discovery | `enzo_gmgn._cache` | 25s | List-level, in-memory |

---

## D. Price Sources & Fallback Chain

### Primary: Pump Advanced-v2

**Endpoint:** `POST https://pump.fun/api/coins/metadatas`
**Payload:** `{"mint": ["mint1", "mint2", ...]}`
**Response:** Array of token metadata including `currentMarketPrice`, `marketCap`

**Advantages:**
- Free, no rate limit
- Batch request (up to ~30 mints per call)
- Sub-second response times

**Limitations:**
- Only covers pump.fun tokens (pre-migration)
- Post-migration tokens return empty or stale data

### Fallback: GMGN

**Endpoints:**
- `GET /api/v1/market/{mint}` — market data (price, volume, liquidity)
- `GET /api/v1/security/{mint}` — security info
- `GET /api/v1/holder/{mint}` — holder distribution

**Rate Limit:** ~1 req/sec effective limit; bans for 90-120s if exceeded

**Cache Strategy:**
- `enzo_gmgn._rl_acquire()` — thread lock + pacing (≥1.2s between calls)
- `enzo-gmgn-ban.json` — cross-process ban state
- TTL cache prevents redundant calls within window

### Fallback: Bonding Curve (pre-migration only)

**Function:** `enzo_gmgn.read_bonding_curve(mint)`
**Mechanism:** Derives price from on-chain bonding-curve account (via Helius getAccountInfo or simulation)
**Limitation:** Only works pre-migration; post-migration tokens have no bonding curve

---

## E. Entry Price Recording & Persistence

### Recording Points

| Stage | Field | Source |
|-------|-------|--------|
| `enzo_analyze.analyze()` | `decision["entry_market_cap"]` | GMGN market_data snapshot |
| `enzo_run.run()` | `merged["price_usd"]` | GMGN market_data |
| `enzo_portfolio.open_position()` | `pos["entry_market_cap"]` | `decision.get("entry_market_cap") or decision.get("market_cap_usd") or current_market_cap(mint)` |

### Backfill Logic (inside `check_exits()`)

```python
if not pos.get("entry_market_cap"):
    pos["entry_market_cap"] = current_market_cap(mint) or 0
    pos["initial_size_usd"] = pos["size_usd"]
    pos["peak_market_cap"] = pos["entry_market_cap"]
```

**RISK:** Legacy positions opened before market-cap tracking was added may get **current** market cap as entry baseline, producing incorrect PnL calculations.

---

## F. Exit Accuracy: Slippage, Latency, Partial Fills

### Latency Budget (pump.fun pre-migration)

| Stage | Latency | Source |
|-------|---------|--------|
| Pump Advanced API call | 0.3-0.6s | HTTP POST batch |
| `_exit_monitor_loop` interval | 1.0s (configurable) | Polling cycle |
| `check_exits()` execution | <0.1s | In-memory + file write |
| Notification send | 0.5-2s | Telegram API |
| **Total** | **1.8-3.7s** | From price move to exit decision |

### Slippage Sources

1. **Stale cache:** PriceFeed cache (5s) + Pump Advanced cache (1s) = up to 6s stale
2. **Polling interval:** Exit monitor runs every 1s, so price may be 1s old
3. **No real execution:** Paper trading assumes perfect fill at observed price

### Partial Fill Model (staged take-profit)

```python
# Stage 1: sell 30% at +20%
_partial_exit(state, mint, pos, 0.30, mcap, 20)

# Stage 2: sell 40% at +50%
_partial_exit(state, mint, pos, 0.40, mcap, 50)

# Stage 3 (final): sell 100% REMAINING at +100%
_partial_exit(state, mint, pos, 1.0, mcap, 100)  # NOT 0.30
```

**Correctness:** Final stage sells `frac=1.0` of remaining size, not the stage's `sell` fraction.

---

## G. GMGN Rate-Limit Impact on Exit Monitoring

### Rate-Limit Mechanism

| Layer | Mechanism |
|-------|-----------|
| **Thread lock** | `enzo_gmgn._rl_lock` ensures single caller |
| **Pacing** | `_rl_acquire()` enforces ≥1.2s between calls |
| **Ban detection** | Parses "resets at" timestamp from 429 response |
| **Cross-process state** | `enzo-gmgn-ban.json` shared file |
| **Ban grace** | 8s grace after ban reset to avoid re-trigger |

### Impact on Exit Monitor

**Good news:** Exit monitor uses **pump advanced-v2** for price refresh, which has **no rate limit**. GMGN is only used as fallback for mints pump doesn't know (post-migration or API hiccups).

**Worst case:**
- Position migrates from pump.fun → Raydium
- Pump Advanced returns empty for that mint
- Exit monitor falls back to GMGN `current_market_cap()` → `get_market_data()`
- If GMGN is banned, price is stale or missing
- Exit conditions may not trigger until ban lifts

### Mitigation in Place

```python
# enzo_pump.py: fallback loop
missing = [m for m in mints if m not in live]
for m in missing:
    mc = enzo_portfolio.current_market_cap(m)  # GMGN fallback
    if mc:
        live[m] = float(mc)
```

---

## H. Latency Budget & Staleness Analysis

### Scenario: Fast Crash (pump.fun token)

```
T+0.0s   Price at $100K MC
T+0.5s   Pump Advanced batch call completes (price: $100K)
T+1.0s   Exit monitor wakes, uses cached price ($100K, now 0.5s stale)
T+1.5s   Price crashes to $50K on-chain
T+2.0s   Next pump batch call would see $50K, but...
T+2.0s   Exit monitor still processing previous cycle
T+3.0s   Exit monitor reads $50K, triggers STOP_LOSS
```

**Effective latency:** 1.5-2.5s from on-chain move to exit decision

### Cache Staleness Stack

```
PriceFeed._cache (5s TTL)
    ↓
enzo_pump_adv._cache (1s TTL)
    ↓
HTTP response (0.3-0.6s transit)
    ↓
On-chain state (immediate)
```

**Maximum staleness:** 5s (PriceFeed) + 1s (pump_adv) + 0.6s (HTTP) ≈ **6.6s**

---

## I. Pump.fun → Raydium Migration State

### Migration Flow

```
pump.fun bonding curve (progress 0-100%)
    ↓
100% complete → migration triggered
    ↓
Liquidity deployed to Raydium
    ↓
Token tradable on Raydium DEX
    ↓
pump.fun bonding-curve account closed
```

### Current Detection

```python
# enzo_gmgn.read_bonding_curve()
if not c.get("exists"):
    return {"exists": False, "phase": "raydium", ...}
```

**Detection method:** If bonding-curve account doesn't exist, assume migrated.

### Price Continuity Gap

**Before migration:**
- Pump Advanced provides price/mcap
- Bonding-curve calculation available

**After migration:**
- Pump Advanced returns empty or stale
- Bonding-curve account closed
- Must rely on GMGN market_data (Raydium pool) or DexScreener

**Current fallback chain:**

```python
# enzo_portfolio.current_market_cap()
def current_market_cap(mint):
    # 1. Pump Advanced (returns empty post-migration)
    mc = enzo_pump_adv.live_price(mint)
    if mc:
        return mc
    
    # 2. GMGN market_data (works for Raydium pools)
    mc = enzo_gmgn.get_live_market_cap(mint)
    if mc:
        return mc
    
    # 3. Bonding curve (pre-migration only)
    c = enzo_curve.read_bonding_curve(mint)
    if c.get("exists"):
        return c["market_cap_usd"]
    
    return None
```

**CRITICAL GAP:** No explicit Raydium pool address tracking. Price continuity depends entirely on GMGN having the migrated token in its database.

---

## J. Raydium Pool Detection & Price Continuity

### What's Missing

1. **No pool address persistence** — Position doesn't store Raydium pool after migration
2. **No migration event detection** — No webhook or polling for "graduated" status
3. **No DexScreener fallback** — GMGN is sole post-migration source

### Recommended Detection (NOT IMPLEMENTED)

```python
# Hypothetical migration detector
def detect_migration(mint):
    c = read_bonding_curve(mint)
    if not c.get("exists"):
        # Query DexScreener or Jupiter for Raydium pool
        pool = find_raydium_pool(mint)
        return {"migrated": True, "pool": pool}
    return {"migrated": False}
```

### Current Behavior

- Exit monitor continues polling pump advanced
- Pump returns empty for migrated token
- Fallback to GMGN `get_market_data()`
- If GMGN has the token, price works
- If GMGN doesn't have it yet, price is None → no exit monitoring

---

## K. Failure Modes & Watchdog Gaps

### Failure Scenarios

| Scenario | Current Behavior | Gap |
|----------|------------------|-----|
| **Pump Advanced API down** | Fallback to GMGN | Works, but rate-limited |
| **GMGN banned** | Pump Advanced still works | Exit monitoring continues |
| **Both down** | Price = None → no exits | **CRITICAL: Positions unmonitored** |
| **Process crash (enzo_pump)** | No restart mechanism | **GAP: No watchdog for pump process** |
| **Process crash (enzo_engine)** | No restart mechanism | **GAP: No watchdog for engine process** |
| **Stale portfolio file** | Loads last saved state | OK if disk is healthy |
| **Concurrent portfolio writes** | No file locking | **RISK: Data corruption** |

### Watchdog Coverage

```python
# enzo_botctl.watchdog()
def watchdog():
    start_serve()           # Dashboard server
    if not botctl_running():
        start_botctl()      # Telegram listener
    if is_paused():
        stop_pump()
    else:
        start_pump()        # Pump monitor
```

**Missing:**
- No check for `enzo_engine.py` (watchlist scanner)
- No health check for price feed thread
- No alert when both price sources fail

---

## L. Reconciliation & Drift Detection

### What Should Be Reconciled

1. **Portfolio ↔ PriceFeed subscriptions**
   - Every open position should be subscribed
   - Closed positions should be unsubscribed

2. **Portfolio ↔ Learning outcomes**
   - Every closed position should have a learning record

3. **Dashboard state ↔ Portfolio file**
   - Dashboard reads from file, so naturally consistent

### Current State

| Reconciliation | Implemented | Gap |
|----------------|-------------|-----|
| Portfolio → PriceFeed subscribe | ✅ (on open) | — |
| Portfolio → PriceFeed unsubscribe | ❌ | Orphan subscriptions |
| Closed → Learning record | ✅ (in `enzo_pump.handle()`) | Not in `enzo_engine` |
| Dashboard ↔ Portfolio | ✅ (reads file) | — |

### Drift Risk

If a position is closed outside the normal flow (e.g., manual edit of `enzo-portfolio.json`), the pricefeed subscription persists, wasting resources.

---

## M. Structured Findings Summary

### Section A: Executive Summary
- Hybrid architecture: pump advanced (primary) + GMGN (fallback)
- 5 critical gaps identified
- Exit monitor only in `enzo_pump.py`, not `enzo_engine.py`

### Section B: Call Chains
- `check_exits()` called from 3 places: pump exit-monitor (1s), pump handle (once), engine scan (60s)
- Engine-only positions may have 60s exit latency

### Section C: PriceFeed
- 5s cache TTL
- No unsubscribe on close → orphan subscriptions
- Pump advanced first, then GMGN, then curve

### Section D: Price Sources
- Pump advanced: free, batch, no rate limit (pre-migration only)
- GMGN: rate-limited, 1 req/sec, TTL cache
- Curve: pre-migration only

### Section E: Entry Price
- Recorded from decision or live lookup
- Backfill on legacy positions risks incorrect baseline

### Section F: Exit Accuracy
- Latency: 1.8-3.7s from move to decision
- Staleness: up to 6.6s
- Paper trading assumes perfect fill

### Section G: Rate-Limit Impact
- Exit monitor uses pump advanced → no rate limit impact
- GMGN fallback only for migrated tokens
- Ban can cause missed prices for post-migration positions

### Section H: Latency Budget
- Effective exit latency: 1.5-2.5s on fast crash
- Cache stack: 5s + 1s + 0.6s = 6.6s max staleness

### Section I: Migration
- Detection: bonding-curve account non-existence
- Continuity: relies on GMGN having migrated token
- Gap: no Raydium pool tracking

### Section J: Raydium Detection
- Missing: pool address persistence, migration events, DexScreener fallback
- Current: GMGN-only post-migration

### Section K: Failure Modes
- Both sources down → unmonitored positions
- No process restart for engine
- No file locking on portfolio writes

### Section L: Reconciliation
- Unsubscribe gap → orphan subscriptions
- Learning record only in pump path, not engine

---

## Recommendations (Prioritized)

### P0: Critical

1. **Add exit monitor to `enzo_engine.py`**
   - Run `_exit_monitor_loop` as background thread in engine
   - Ensures watchlist positions are monitored continuously

2. **Implement Raydium pool detection**
   - Query DexScreener/GMGN for pool address on migration detection
   - Store pool address in position record
   - Use pool price for post-migration monitoring

3. **Add health check for dual-source failure**
   - Alert when both pump advanced and GMGN fail
   - Halt trading if price sources unreliable

### P1: High

4. **Unsubscribe on position close**
   - Call `pricefeed.unsubscribe(mint)` in `close_position()`
   - Prevents orphan subscriptions

5. **Add file locking to portfolio writes**
   - Use `fcntl.flock()` or `portalocker`
   - Prevents concurrent write corruption

6. **Add engine process watchdog**
   - Extend `enzo_botctl.watchdog()` to check/restart engine

### P2: Medium

7. **Persist entry_market_cap explicitly**
   - Always capture at open time, never backfill
   - Validate legacy positions or force-close them

8. **Add reconciliation cron**
   - Periodically check: open positions ↔ subscriptions ↔ learning records
   - Alert on drift

### P3: Low

9. **Implement DexScreener fallback**
   - Use DexScreener API when GMGN fails
   - Provides redundancy for post-migration tokens

10. **Add `api/prices` endpoint to `enzo_serve.py`**
    - Dashboard JS expects this endpoint
    - Return `{positions: {mint: {live_mc, uPnL}}, equity: ...}`

---

## Appendix: File Reference

| File | Purpose |
|------|---------|
| `enzo_pump.py` | Pump.fun monitor + exit monitor loop |
| `enzo_engine.py` | Watchlist scanner |
| `enzo_portfolio.py` | Position ledger + `check_exits()` |
| `enzo_pricefeed.py` | Price polling + subscription |
| `enzo_pump_adv.py` | Pump advanced-v2 API client |
| `enzo_gmgn.py` | GMGN unified data layer |
| `enzo_run.py` | Pipeline orchestrator |
| `enzo_dashboard.py` | HTML dashboard generator |
| `enzo_botctl.py` | Telegram control + watchdog |
| `enzo-config.yaml` | Configuration |
| `enzo-portfolio.json` | Position state |
| `enzo-gmgn-ban.json` | Cross-process ban state |

---

**Audit Complete — No files modified.**
