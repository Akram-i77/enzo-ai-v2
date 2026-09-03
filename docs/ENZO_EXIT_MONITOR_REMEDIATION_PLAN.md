# ENZO Exit Monitor Remediation Plan

**PLAN ONLY** — No code modification
**Date:** 2026-08-12
**Based on:** ENZO_EXIT_MONITOR_AUDIT.md

---

## 1. CURRENT ARCHITECTURE

### 1.1 Pump-Discovered Position Flow

```
┌─────────────────────────────────────────────────────────────────┐
│                    PUMP DISCOVERY PATH                          │
└─────────────────────────────────────────────────────────────────┘

[GMGN trenches] → poll_new_pairs()
                        ↓
              enqueue(mint) → worker thread
                        ↓
                 handle(mint)
                        ↓
              enzo_run.run(mint) → BUY decision
                        ↓
         enzo_portfolio.open_position(decision)
                        ↓
              pricefeed.subscribe(mint)
                        ↓
┌─────────────────────────────────────────────────────────────────┐
│  enzo_pump.py run() starts 3 threads:                           │
│                                                                 │
│  Thread 1: _pump_loop()                                         │
│    └─ Poll GMGN trenches every 30s                              │
│    └─ Enqueue fresh mints to worker                             │
│                                                                 │
│  Thread 2: worker()                                             │
│    └─ Process mint queue                                        │
│    └─ Call handle(mint) → pipeline → BUY/WAIT/IGNORE           │
│    └─ Rate-limited to 6 mints per 60s cycle                     │
│                                                                 │
│  Thread 3: _exit_monitor_loop() ← CONTINUOUS EXIT MONITOR      │
│    └─ Interval: 1.0s (configurable)                            │
│    └─ Load ALL open positions from portfolio                    │
│    └─ Call pump_advanced.position_prices(opens) → ONE batch    │
│    └─ Fallback GMGN/curve for missing mints                     │
│    └─ Call portfolio.check_exits(live_prices)                   │
│    └─ Notify + record outcomes                                  │
└─────────────────────────────────────────────────────────────────┘

EXIT LATENCY: ~1-3 seconds (pump batch + check_exits)
```

### 1.2 Watchlist/Engine Position Flow

```
┌─────────────────────────────────────────────────────────────────┐
│                   WATCHLIST DISCOVERY PATH                      │
└─────────────────────────────────────────────────────────────────┘

[enzo_watchlist.json] → scan_once()
                              ↓
                   For each mint in watchlist:
                              ↓
                   enzo_run.run(mint) → BUY decision
                              ↓
              enzo_portfolio.open_position(decision)
                              ↓
              [NO pricefeed.subscribe() call!]
                              ↓
┌─────────────────────────────────────────────────────────────────┐
│  enzo_engine.py scan_once() runs every 60s:                     │
│                                                                 │
│  - Load watchlist mints                                         │
│  - For each mint:                                               │
│      - Run pipeline → decision                                  │
│      - If BUY → open_position()                                 │
│      - Check if mint in open_positions                          │
│      - Call check_exits({mint: mc}) ← ONLY DURING SCAN!        │
│                                                                 │
│  NO CONTINUOUS EXIT MONITOR THREAD                              │
└─────────────────────────────────────────────────────────────────┘

EXIT LATENCY: Up to 60 seconds (only checked during scan cycle)
```

### 1.3 Critical Gap Summary

| Position Source | Exit Monitor | Latency |
|-----------------|--------------|---------|
| Pump discovery | `_exit_monitor_loop()` (Thread 3) | 1-3s |
| Watchlist scan | `scan_once()` inline check | Up to 60s |
| Manual position | None | Undefined |

**PROBLEM:** Watchlist positions have no continuous monitoring. They rely on the 60s scan cycle, which means exit conditions (SL/TP/trailing) may not trigger for up to a minute after the price crosses the threshold.

---

## 2. TARGET ARCHITECTURE

### 2.1 Unified Exit Monitor Design

```
┌─────────────────────────────────────────────────────────────────┐
│                    TARGET ARCHITECTURE                          │
└─────────────────────────────────────────────────────────────────┘

                    portfolio.open_positions
                              │
                              ▼
              ┌───────────────────────────────┐
              │  enzo_exit_monitor.py         │  ← NEW MODULE
              │                               │
              │  - Single background thread   │
              │  - Polls every 2s             │
              │  - Monitors ALL positions     │
              │  - Independent of discovery   │
              └───────────┬───────────────────┘
                          │
                          ▼
              ┌───────────────────────────────┐
              │  price_provider.batch_prices  │
              │                               │
              │  Priority:                    │
              │  1. Pump Advanced (batch)     │
              │  2. GMGN (fallback)           │
              │  3. Bonding curve (last resort)│
              └───────────┬───────────────────┘
                          │
                          ▼
              ┌───────────────────────────────┐
              │  portfolio.check_exits()      │  ← SINGLE SOURCE
              │                               │
              │  - Returns (closed, partials) │
              │  - Updates portfolio file     │
              │  - NO DUPLICATION OF LOGIC    │
              └───────────┬───────────────────┘
                          │
                          ▼
              ┌───────────────────────────────┐
              │  notification + learning      │
              │                               │
              │  - Telegram alerts            │
              │  - Record outcomes            │
              └───────────────────────────────┘
```

### 2.2 Key Design Principles

1. **Single Source of Truth:** `check_exits()` remains the ONLY function that decides exits
2. **Discovery Agnostic:** Monitor doesn't care HOW position was opened
3. **Batch Optimization:** One pump advanced call for ALL positions
4. **Graceful Degradation:** If pump fails, GMGN fallback; if both fail, hold and alert

---

## 3. EXIT MONITOR DESIGN

### 3.1 New Module: `enzo_exit_monitor.py`

```python
"""
ENZO - Unified Exit Monitor

Monitors ALL open positions regardless of discovery source.
Runs as a single background thread, polling every 2s.

Lifecycle:
  - Started by enzo_engine.run() or enzo_botctl.watchdog()
  - Reads portfolio.open_positions
  - Calls price_provider.batch_prices()
  - Calls portfolio.check_exits()
  - Notifies + records outcomes
"""
```

### 3.2 Configuration Parameters

| Parameter | Default | Config Key | Rationale |
|-----------|---------|------------|-----------|
| `interval_seconds` | 2.0 | `exit_monitor.interval_seconds` | Balance between latency and API load |
| `max_stale_age_seconds` | 10.0 | `exit_monitor.max_stale_age_seconds` | Alert if price older than this |
| `failure_threshold` | 3 | `exit_monitor.failure_threshold` | Consecutive failures before halt |
| `enable_monitoring` | true | `exit_monitor.enabled` | Feature flag |

### 3.3 Thread Safety

```python
class ExitMonitor:
    _instance = None
    _lock = threading.Lock()
    
    def __init__(self):
        self._stop_event = threading.Event()
        self._thread = None
        self._monitoring_mints = set()  # Prevent duplicate cycles
        self._consecutive_failures = 0
    
    @classmethod
    def get_instance(cls):
        # Singleton pattern
        with cls._lock:
            if cls._instance is None:
                cls._instance = cls()
            return cls._instance
```

### 3.4 Main Loop Logic

```python
def _monitor_cycle(self):
    """One cycle of exit monitoring. Called every interval_seconds."""
    try:
        # 1. Load ALL open positions (single source of truth)
        state = portfolio.load_state()
        open_positions = state.get("open_positions", {})
        mints = list(open_positions.keys())
        
        if not mints:
            return  # No positions to monitor
        
        # 2. Prevent duplicate monitoring
        with self._lock:
            if self._monitoring_mints.intersection(mints):
                # Already monitoring some of these in another cycle
                pass
            self._monitoring_mints.update(mints)
        
        try:
            # 3. Get prices for ALL mints (batch optimization)
            prices = price_provider.batch_prices(mints)
            
            # 4. Validate prices
            valid_prices = {}
            for mint, mc in prices.items():
                if mc and mc > 0:
                    valid_prices[mint] = float(mc)
                else:
                    self._log_stale_price(mint)
            
            if not valid_prices:
                self._handle_all_prices_stale(mints)
                return
            
            # 5. Check exits (SINGLE SOURCE OF DECISION)
            closed, partials = portfolio.check_exits(valid_prices)
            
            # 6. Notify + record
            for rec in partials:
                notify.send_message(
                    f"🪙 PARTIAL {rec['symbol']} {rec['reason']} | "
                    f"sold {rec['fraction']*100:.0f}% @ MC ${rec['exit_market_cap']:,.0f} "
                    f"PnL {rec['pnl']:+.4f} ({rec['pnl_pct']:+.2f}%)"
                )
            
            for rec in closed:
                learn.record_outcome(rec)
                notify.send_message(
                    f"💰 CLOSED {rec['symbol']} via {rec['reason']} | "
                    f"PnL {rec['pnl']:+.4f} ({rec['pnl_pct']:+.2f}%)"
                )
            
            # 7. Reset failure counter on success
            self._consecutive_failures = 0
            
        finally:
            # 8. Clear monitoring set
            with self._lock:
                self._monitoring_mints.difference_update(mints)
    
    except Exception as e:
        self._handle_cycle_error(e)
```

### 3.5 Handling Existing `enzo_pump._exit_monitor_loop()`

**Strategy:** Graceful deprecation, not removal.

```python
# In enzo_pump.py

def _exit_monitor_loop(interval=None):
    """DEPRECATED: Use enzo_exit_monitor.py instead.
    Kept for backward compatibility during transition."""
    
    # Check if unified monitor is running
    try:
        from enzo_exit_monitor import ExitMonitor
        if ExitMonitor.get_instance().is_running():
            log("exit-monitor: UNIFIED MONITOR ACTIVE — skipping legacy loop")
            return  # Unified monitor is handling it
    except Exception:
        pass
    
    # Fall back to legacy behavior
    log("exit-monitor: LEGACY MODE (unified monitor not available)")
    # ... existing logic ...
```

**Migration path:**
1. Phase A: Deploy unified monitor alongside legacy
2. Phase A+1: Unified monitor takes priority
3. Phase B: Remove legacy code after 1 week stable

---

## 4. PRICE PROVIDER DESIGN

### 4.1 Provider Module: `enzo_price_provider.py`

```python
"""
ENZO - Price Provider

Provides batch price fetching with fallback chain:
  1. Pump Advanced-v2 (batch, free, no rate limit)
  2. GMGN market_data (rate-limited, TTL cached)
  3. Bonding curve (pre-migration only)

Never raises exceptions — returns partial results on failure.
"""
```

### 4.2 Batch Price Fetching

```python
def batch_prices(mints: list[str], max_stale_age: float = 10.0) -> dict[str, float]:
    """
    Fetch live market caps for multiple mints.
    
    Returns:
        {mint: live_market_cap_usd} for mints with valid prices
        
    Note:
        Mints with missing/invalid prices are NOT included in result.
        Callers must check if expected mints are missing.
    """
    result = {}
    pump_failed = False
    gmgn_failed = False
    
    # === TIER 1: Pump Advanced (batch, free, no rate limit) ===
    try:
        import enzo_pump_adv as pa
        # ONE batch POST for ALL mints (up to ~30)
        live = pa.live_prices(mints)  # {mint: {price_usd, market_cap_usd}}
        
        for mint, info in live.items():
            mc = info.get("market_cap_usd")
            if mc and mc > 0:
                result[mint] = float(mc)
    
    except Exception as e:
        log(f"price_provider: pump_advanced failed: {e}")
        pump_failed = True
    
    # === TIER 2: GMGN fallback (rate-limited) ===
    missing = [m for m in mints if m not in result]
    
    if missing and (pump_failed or len(result) < len(mints)):
        for mint in missing:
            try:
                import enzo_gmgn
                mc = enzo_gmgn.get_live_market_cap(mint)  # Uses TTL cache
                if mc and mc > 0:
                    result[mint] = float(mc)
            except Exception as e:
                log(f"price_provider: GMGN failed for {mint[:10]}: {e}")
        
        # Check if GMGN is effectively unavailable
        if len(result) == 0 and len(missing) > 0:
            gmgn_failed = True
    
    # === TIER 3: Bonding curve (pre-migration only) ===
    still_missing = [m for m in mints if m not in result]
    
    if still_missing and (pump_failed or gmgn_failed):
        for mint in still_missing:
            try:
                import enzo_curve
                c = enzo_curve.read_bonding_curve(mint)
                if c.get("exists") and c.get("market_cap_usd"):
                    result[mint] = float(c["market_cap_usd"])
            except Exception:
                pass
    
    # === LOG STALE PRICES ===
    if len(result) < len(mints):
        missing_final = [m for m in mints if m not in result]
        log(f"price_provider: {len(missing_final)} mints with no price")
    
    return result
```

### 4.3 Source Selection Logic

```
For each mint:
    ├─ Try pump_advanced.live_prices([mint])
    │   └─ If success and mc > 0 → USE IT
    │   └─ If not in response → mint not tracked by pump
    │
    ├─ If pump failed OR mint missing:
    │   └─ Try gmgn.get_live_market_cap(mint)
    │       └─ If success and mc > 0 → USE IT
    │       └─ If banned/failed → skip
    │
    └─ If both failed:
        └─ Try curve.read_bonding_curve(mint)
            └─ If exists → USE IT
            └─ If not exists → mint is POST-MIGRATION or unknown
                └─ NO PRICE AVAILABLE
```

### 4.4 Price Validation

```python
def _validate_price(mint: str, mc: float, source: str) -> bool:
    """Validate a price before using it for exit decisions."""
    if mc is None:
        return False
    if mc <= 0:
        log(f"price_provider: INVALID {mint[:10]} mc={mc} from {source}")
        return False
    if mc < 100:  # Unrealistically low (e.g., $0.50)
        log(f"price_provider: SUSPICIOUS {mint[:10]} mc={mc} from {source}")
        return False
    return True
```

---

## 5. RAYDIUM MIGRATION DESIGN

### 5.1 State Machine

```
┌─────────────────────────────────────────────────────────────────┐
│                    MIGRATION STATE MACHINE                      │
└─────────────────────────────────────────────────────────────────┘

  PRE_MIGRATION                    MIGRATING                    POST_MIGRATION
  ─────────────                    ──────────                   ───────────────
  bonding_curve.exists = True      progress ≥ 99%               bonding_curve.exists = False
  price source: pump_advanced      price source: pump_advanced  price source: GMGN/DexScreener
  pool: None                       pool: None                   pool: DETECTED or None
                                                                
       │                                │                              │
       │  progress → 100%               │  curve closed               │
       └───────────────────────────────►└────────────────────────────►│
                                                                       │
                                                                       │
                                    ◄───────────────────────────────────┘
                                          (token migrated back? RARE)
```

### 5.2 Position Record Schema Update

```json
{
  "mint": "xxx",
  "symbol": "TOKEN",
  "entry_market_cap": 50000,
  "migration_phase": "PRE_MIGRATION",  // NEW FIELD
  "raydium_pool": null,                 // NEW FIELD (pool address)
  "migration_detected_at": null,        // NEW FIELD (timestamp)
  ...
}
```

### 5.3 Migration Detection Logic

```python
def detect_migration_phase(mint: str, current_phase: str = None) -> str:
    """
    Detect current migration phase for a mint.
    
    Returns:
        "PRE_MIGRATION" | "MIGRATING" | "POST_MIGRATION"
    """
    # 1. Check bonding curve
    c = read_bonding_curve(mint)
    
    if not c.get("exists"):
        # Curve closed → token migrated
        return "POST_MIGRATION"
    
    progress = c.get("progress_pct", 0)
    
    if progress >= 99.0:
        # Curve nearly complete → migrating
        return "MIGRATING"
    
    return "PRE_MIGRATION"
```

### 5.4 Pool Detection (Post-Migration)

```python
def detect_raydium_pool(mint: str) -> str | None:
    """
    Find Raydium pool address for a migrated token.
    
    Uses GMGN market_data which includes pool info.
    Returns pool address or None.
    """
    try:
        import enzo_gmgn
        md = enzo_gmgn.get_market_data(mint)
        
        # GMGN may return pool address in market data
        pool = md.get("pool_address") or md.get("raydium_pool")
        
        if pool:
            log(f"migration: detected Raydium pool for {mint[:10]}: {pool}")
            return pool
        
    except Exception as e:
        log(f"migration: pool detection failed for {mint[:10]}: {e}")
    
    return None
```

### 5.5 Price Source Switching

```python
def get_price_for_phase(mint: str, phase: str) -> float | None:
    """Get price using appropriate source for migration phase."""
    
    if phase == "PRE_MIGRATION" or phase == "MIGRATING":
        # Use pump_advanced (primary) or curve (fallback)
        return pump_adv_fallback(mint)
    
    elif phase == "POST_MIGRATION":
        # Use GMGN (Raydium pool price)
        return gmgn_market_data(mint)
    
    return None
```

### 5.6 Handling Pool Detection Failure

```
If pool detection fails:
    1. Log warning
    2. Continue using GMGN get_market_data() (which may still work)
    3. Mark position with "raydium_pool": null
    4. DO NOT BLOCK EXIT MONITORING
    
Position is still monitored; just no explicit pool address stored.
```

---

## 6. SUBSCRIPTION RECONCILIATION DESIGN

### 6.1 Desired Invariant

```
pricefeed._want == set(portfolio.open_positions.keys())
```

### 6.2 Reconciliation Function

```python
def reconcile_subscriptions():
    """
    Ensure pricefeed subscriptions match open positions.
    Called at:
      - Position open
      - Position close
      - Process startup
      - Periodic health check (every 30s)
    """
    pf_state = portfolio.load_state()
    portfolio_mints = set(pf_state.get("open_positions", {}).keys())
    
    feed = pricefeed.get_feed()
    subscribed_mints = feed.subscribed()
    
    # Add missing subscriptions
    for mint in portfolio_mints - subscribed_mints:
        feed.subscribe(mint)
        log(f"reconcile: subscribed {mint[:10]}")
    
    # Remove orphan subscriptions
    for mint in subscribed_mints - portfolio_mints:
        feed.unsubscribe(mint)
        log(f"reconcile: unsubscribed orphan {mint[:10]}")
    
    return len(portfolio_mints - subscribed_mints), len(subscribed_mints - portfolio_mints)
```

### 6.3 Event-Driven Updates

| Event | Action | Location |
|-------|--------|----------|
| Position opened | `feed.subscribe(mint)` | `portfolio.open_position()` |
| Position closed | `feed.unsubscribe(mint)` | `portfolio.close_position()` |
| Process startup | `reconcile_subscriptions()` | `exit_monitor.start()` |
| Periodic check | `reconcile_subscriptions()` | `exit_monitor._run()` (every 30s) |

### 6.4 Handling Edge Cases

| Case | Detection | Resolution |
|------|-----------|------------|
| Orphan subscription | Mint in `pricefeed._want` but not in portfolio | Remove via reconciliation |
| Missing subscription | Mint in portfolio but not in `pricefeed._want` | Add via reconciliation |
| Stale subscription | Position exists, subscription exists, but no price updates | Re-subscribe (force refresh) |
| Duplicate subscribe | `subscribe(mint)` called twice | PriceFeed dedupes internally |
| Race condition | Position opens/closes during reconciliation | Reconciliation is idempotent |

---

## 7. SOURCE FAILURE / FAILSAFE DESIGN

### 7.1 Failure Classification

| Failure Type | Detection | Severity |
|--------------|-----------|----------|
| Pump Advanced 4xx | HTTP status 400-499 | LOW (fallback available) |
| Pump Advanced 5xx | HTTP status 500-599 | MEDIUM (temporary outage) |
| Pump Advanced timeout | No response in 5s | MEDIUM |
| GMGN 429 (rate limit) | Shared ban file exists | MEDIUM (wait for reset) |
| GMGN 5xx | HTTP status 500-599 | MEDIUM |
| Both sources fail | All prices = None | **HIGH** |
| Stale price | Price age > max_stale_age | MEDIUM |
| Invalid price | mc ≤ 0 or mc < 100 | LOW (skip) |

### 7.2 Failure Handling Matrix

```
┌─────────────────────────────────────────────────────────────────┐
│                    FAILURE HANDLING MATRIX                      │
└─────────────────────────────────────────────────────────────────┘

SCENARIO                          ACTION                    AUTO-SELL?
────────────────────────────────────────────────────────────────────
Pump Advanced unavailable         Use GMGN fallback         NO
GMGN unavailable                  Use pump + curve          NO
Both unavailable                  HOLD positions            NO
                                   Alert via Telegram        
                                   Stop new entries          
                                   Continue retrying          
Stale price (>10s old)            Log warning               NO
                                   Use cached price           
Invalid price (≤0)                Skip mint                 NO
                                   Log error                  
Price source timeout              Continue with partial     NO
                                   Log at debug level        

CRITICAL RULE: PRICE FAILURE → NEVER AUTO-SELL
```

### 7.3 Failure State Tracking

```python
class PriceHealth:
    pump_available: bool = True
    gmgn_available: bool = True
    last_pump_success: float = 0
    last_gmgn_success: float = 0
    consecutive_failures: int = 0
    halt_new_entries: bool = False
```

### 7.4 Alert Logic

```python
def _check_price_health(health: PriceHealth) -> list[str]:
    """Return list of alerts."""
    alerts = []
    
    now = time.time()
    
    # Pump stale check
    if now - health.last_pump_success > 30:
        alerts.append("⚠️ Pump Advanced prices stale >30s")
    
    # GMGN stale check
    if now - health.last_gmgn_success > 60:
        alerts.append("⚠️ GMGN prices stale >60s")
    
    # Both failed
    if not health.pump_available and not health.gmgn_available:
        alerts.append("🚨 CRITICAL: Both price sources unavailable")
        health.halt_new_entries = True
    
    # Consecutive failures
    if health.consecutive_failures >= 3:
        alerts.append(f"🚨 {health.consecutive_failures} consecutive price failures")
    
    return alerts
```

### 7.5 Halt New Entries Behavior

```python
# In exit_monitor._monitor_cycle()

if health.halt_new_entries:
    # Set global flag that enzo_pump / enzo_engine check before opening
    state["price_feed_unhealthy"] = True
    portfolio.save_state(state)
    
    # Notify once
    if not health.halt_alerted:
        notify.send_message(
            "🚨 PRICE FEED UNHEALTHY\n"
            "Halting new entries until recovery.\n"
            "Existing positions are HELD (no auto-sell)."
        )
        health.halt_alerted = True
```

---

## 8. RATE LIMIT BUDGET

### 8.1 Pump Advanced Calls

| Positions | Batches Required | Calls per Cycle | Latency per Cycle |
|-----------|------------------|-----------------|-------------------|
| 1 | 1 | 1 | 0.3-0.6s |
| 5 | 1 | 1 | 0.3-0.6s |
| 10 | 1 | 1 | 0.3-0.6s |
| 20 | 1 | 1 | 0.3-0.6s |
| 30 | 1 | 1 | 0.3-0.6s |
| 50 | 2 | 2 | 0.6-1.2s |
| 100 | 4 | 4 | 1.2-2.4s |

**Pump Advanced:** No rate limit, batch up to ~30 mints per call.

### 8.2 GMGN Fallback Calls

| Positions | Pump Coverage | GMGN Calls | Total GMGN/sec | Risk |
|-----------|---------------|------------|----------------|------|
| 1 | 0-1 | 0-1 | 0-0.5 | Low |
| 5 | 0-5 | 0-5 | 0-2.5 | Low |
| 10 | 0-10 | 0-10 | 0-5.0 | Medium |
| 20 | 0-20 | 0-20 | 0-10.0 | **HIGH (ban risk)** |
| 50 | 0-50 | 0-50 | 0-25.0 | **UNUSABLE** |

**Strategy:** Minimize GMGN calls by ensuring pump_advanced covers 95%+ of positions.

### 8.3 Call Budget per Monitor Cycle

```
For N positions:
    1. pump_advanced.batch(N mints) → 1 call
    2. For mints not in pump response (post-migration):
        - gmgn.get_market_data(mint) → 1 call per mint
    
    Total calls = 1 + (N - pump_coverage)
    
Example:
    20 positions, 18 pre-migration, 2 post-migration
    → 1 pump call + 2 GMGN calls = 3 total
    → GMGN rate = 1 call / 2s = 0.5 req/sec (SAFE)
```

### 8.4 Rate Limit Mitigation

1. **Prefer pump_advanced:** Always try batch first
2. **Cache GMGN results:** 10s TTL means max 1 call per mint per 10s
3. **Stagger GMGN calls:** 0.5s gap between fallback calls
4. **Ban awareness:** Check `enzo-gmgn-ban.json` before calling

---

## 9. PERFORMANCE TARGETS

### 9.1 Target Metrics

| Metric | Target | Rationale |
|--------|--------|-----------|
| **Price freshness** | ≤ 2 seconds | Monitor cycle interval |
| **Exit detection latency** | ≤ 5 seconds | Cycle (2s) + price fetch (0.5s) + check_exits (0.1s) + notify (1-2s) |
| **Maximum stale-price age** | 10 seconds | Configurable, alert threshold |
| **Monitor cycle duration** | ≤ 500ms (excluding HTTP) | In-memory operations only |
| **Price fetch latency** | ≤ 1 second | Pump batch + minimal fallbacks |

### 9.2 Monitoring Dashboard Metrics

```python
# Exported via enzo_serve /api/health

{
  "exit_monitor": {
    "last_cycle_ts": "2026-08-12T20:00:00Z",
    "cycle_count": 12345,
    "positions_monitored": 3,
    "avg_cycle_ms": 320,
    "price_latency_ms": 450,
    "consecutive_failures": 0,
    "pump_available": true,
    "gmgn_available": true
  }
}
```

---

## 10. BACKWARD COMPATIBILITY

### 10.1 Components That Remain Unchanged

| Component | Interface | Status |
|-----------|-----------|--------|
| `portfolio.check_exits()` | `(dict) → (closed, partials)` | **PRESERVED** |
| `portfolio.open_position()` | `(decision) → {ok, reason, position}` | **PRESERVED** |
| `portfolio.close_position()` | `(state, mint, mc, reason) → {ok, record}` | **PRESERVED** |
| `enzo-portfolio.json` schema | All existing fields | **PRESERVED** (new fields optional) |
| Paper trading | No changes | **PRESERVED** |
| `enzo_learn.record_outcome()` | `(record) → None` | **PRESERVED** |
| `enzo_notify.send_message()` | `(text) → None` | **PRESERVED** |
| Dashboard HTML | Reads same JSON | **PRESERVED** |
| `enzo_botctl.watchdog()` | Existing logic | **PRESERVED** (add start_exit_monitor) |

### 10.2 New Optional Fields (Non-Breaking)

```json
// enzo-portfolio.json - position record

{
  "symbol": "TOKEN",
  "entry_market_cap": 50000,
  
  // NEW OPTIONAL FIELDS (backward compatible)
  "migration_phase": "PRE_MIGRATION",  // null → assume PRE_MIGRATION
  "raydium_pool": null,                // null → not detected yet
  "migration_detected_at": null        // null → migration not happened
}
```

### 10.3 Deprecation Path

| Component | Current Status | Future Status | Migration |
|-----------|----------------|---------------|-----------|
| `enzo_pump._exit_monitor_loop()` | Active | Deprecated | Log warning, redirect to unified |
| `pricefeed.subscribe()` | Called from pump only | Called from unified monitor | Same interface |
| `engine.check_exits()` inline | Inside `scan_once()` | Removed (unified handles) | Delete inline call |

---

## 11. ROLLBACK

### 11.1 Per-Phase Rollback Paths

#### Phase A: Unified Exit Monitor

| Change | Rollback Command |
|--------|------------------|
| Created `enzo_exit_monitor.py` | `rm enzo_exit_monitor.py` |
| Added `start_exit_monitor()` to engine | Remove function call |
| Modified `enzo_pump._exit_monitor_loop()` | Revert to original logic |

**Rollback Test:** After rollback, verify pump's exit_monitor still runs.

#### Phase B: Raydium Migration

| Change | Rollback Command |
|--------|------------------|
| Added `migration_phase` field | Delete field from positions (manual JSON edit) |
| Added `raydium_pool` field | Delete field from positions |
| Modified `current_market_cap()` | Revert to original function |

**Rollback Test:** Positions with `migration_phase` field still work (null-safe).

#### Phase C: Source Failure

| Change | Rollback Command |
|--------|------------------|
| Added `price_health` tracking | Remove health check code |
| Added `halt_new_entries` flag | Remove flag check from pump/engine |

**Rollback Test:** New entries still work after rollback.

#### Phase D: Dashboard API

| Change | Rollback Command |
|--------|------------------|
| Added `/api/prices` endpoint | Remove endpoint function |
| Added `/api/health` endpoint | Remove endpoint function |

**Rollback Test:** Dashboard still loads (API 404s handled gracefully).

### 11.2 Backup Strategy

```bash
# Before each phase
cp enzo-portfolio.json enzo-portfolio.json.bak.phase_A
cp enzo-config.yaml enzo-config.yaml.bak.phase_A

# After rollback
cp enzo-portfolio.json.bak.phase_A enzo-portfolio.json
```

---

## 12. IMPLEMENTATION PHASES

### Phase A: Independent Unified Exit Monitor

**Goal:** Continuous exit monitoring for ALL positions.

**Changes:**
1. Create `enzo_exit_monitor.py` (new file)
2. Add `ExitMonitor` class with background thread
3. Add `start_exit_monitor()` call to `enzo_engine.main()`
4. Add `start_exit_monitor()` call to `enzo_botctl.watchdog()`
5. Deprecate `enzo_pump._exit_monitor_loop()` (keep code, add warning)

**Test Criteria:**
- [ ] Open position via pump → exit within 3s of threshold
- [ ] Open position via watchlist → exit within 3s of threshold
- [ ] Multiple positions → all monitored
- [ ] Restart process → positions still monitored
- [ ] Legacy pump monitor logs deprecation warning

**Duration:** 2-3 days

---

### Phase B: Raydium Migration Continuity

**Goal:** Seamless price monitoring across pump→Raydium migration.

**Changes:**
1. Add `migration_phase`, `raydium_pool`, `migration_detected_at` to position schema
2. Implement `detect_migration_phase()` in `enzo_exit_monitor.py`
3. Update `price_provider.batch_prices()` to use GMGN for POST_MIGRATION mints
4. Update `enzo_portfolio.current_market_cap()` to check migration phase
5. Add migration event logging

**Test Criteria:**
- [ ] PRE_MIGRATION position uses pump price
- [ ] POST_MIGRATION position uses GMGN price
- [ ] Pool detected and stored when available
- [ ] Pool detection failure → position still monitored
- [ ] Migration phase persists across restarts

**Duration:** 3-4 days

---

### Phase C: Source Failure / Reconciliation

**Goal:** Robust handling of price source failures.

**Changes:**
1. Implement `PriceHealth` tracking in `enzo_exit_monitor.py`
2. Add `reconcile_subscriptions()` function
3. Add `halt_new_entries` flag check in pump/engine
4. Add failure alerts to Telegram
5. Add health check endpoint `/api/health`

**Test Criteria:**
- [ ] Pump failure → GMGN fallback works
- [ ] GMGN failure → pump still works
- [ ] Both fail → positions held, alert sent
- [ ] New entries halted on dual failure
- [ ] Recovery clears halt flag
- [ ] Orphan subscriptions removed
- [ ] Missing subscriptions added

**Duration:** 2-3 days

---

### Phase D: Dashboard Health / API Cleanup

**Goal:** Dashboard shows live prices and health status.

**Changes:**
1. Add `/api/prices` endpoint to `enzo_serve.py`
2. Add `/api/health` endpoint to `enzo_serve.py`
3. Update dashboard HTML to use new endpoints
4. Add health indicator to dashboard
5. Add stale price warning to dashboard

**Test Criteria:**
- [ ] Dashboard fetches `/api/prices` successfully
- [ ] Dashboard shows live prices (not stale)
- [ ] Dashboard shows health status
- [ ] Stale prices highlighted in UI

**Duration:** 1-2 days

---

## 13. TEST PLAN

### 13.1 Unit Tests (Per-Module)

| Test | Module | Description | Pass Criteria |
|------|--------|-------------|---------------|
| `test_batch_prices_pump` | price_provider | 5 mints, all pump | All 5 prices returned |
| `test_batch_prices_mixed` | price_provider | 5 mints, 2 post-migration | 5 prices, 2 from GMGN |
| `test_batch_prices_pump_fail` | price_provider | Pump 500 error | All prices from GMGN fallback |
| `test_batch_prices_all_fail` | price_provider | Both sources fail | Empty dict, alert logged |
| `test_check_exits_sl` | portfolio | Price drops -5% | STOP_LOSS triggered |
| `test_check_exits_tp` | portfolio | Price rises +10% | TAKE_PROFIT triggered |
| `test_check_exits_trailing` | portfolio | Peak +6%, then -5% | TRAILING_STOP triggered |
| `test_check_exits_stages` | portfolio | Price +25% | Partial sells at stages |
| `test_check_exits_time` | portfolio | Held > max_hours | TIME_EXIT triggered |
| `test_reconcile_add` | exit_monitor | Position opened | Subscription added |
| `test_reconcile_remove` | exit_monitor | Position closed | Subscription removed |
| `test_reconcile_orphan` | exit_monitor | Orphan subscription exists | Subscription removed |
| `test_migration_detect_pre` | exit_monitor | Bonding curve exists | Phase=PRE_MIGRATION |
| `test_migration_detect_post` | exit_monitor | Curve closed | Phase=POST_MIGRATION |
| `test_migration_price_switch` | price_provider | Post-migration mint | Uses GMGN price |

### 13.2 Integration Tests

| Test | Description | Pass Criteria |
|------|-------------|---------------|
| `test_pump_position_lifecycle` | Open via pump, price +15%, check exit | Position closes, PnL recorded |
| `test_watchlist_position_lifecycle` | Open via engine, price -10%, check exit | Position closes within 3s |
| `test_multiple_positions` | Open 5 positions, drop all | All 5 close correctly |
| `test_rapid_price_swing` | Price +20% then -20% in 2s | Only one exit (SL or TP) |
| `test_restart_recovery` | Kill process with 3 positions, restart | All 3 monitored, subscriptions OK |
| `test_migration_during_hold` | Position migrates while open | Price source switches seamlessly |
| `test_pump_api_outage` | Block pump API | GMGN fallback, positions held |
| `test_gmgn_ban` | Trigger GMGN rate limit | Pump continues, alert sent |
| `test_both_sources_fail` | Block both APIs | Positions HELD, alert sent, no auto-sell |
| `test_stale_price_alert` | Price age > 10s | Alert logged, position held |
| `test_health_endpoint` | Call /api/health | Returns status JSON |
| `test_dashboard_prices` | Open dashboard | Prices update every 2s |

### 13.3 Stress Tests

| Test | Description | Pass Criteria |
|------|-------------|---------------|
| `test_50_positions` | Open 50 positions | All monitored, batch calls used |
| `test_100_positions` | Open 100 positions | No rate limit, batches split |
| `test_rapid_opens` | Open 10 positions in 10s | All subscriptions added |
| `test_rapid_closes` | Close 10 positions in 5s | All subscriptions removed |
| `test_price_feed_restart` | Kill price provider, restart | Recovery < 5s |
| `test_monitor_restart` | Kill exit monitor, restart | Recovery < 5s |

### 13.4 Regression Tests

| Test | Description | Pass Criteria |
|------|-------------|---------------|
| `test_existing_positions` | Load portfolio with 3 positions | All monitored correctly |
| `test_legacy_portfolio_schema` | Load old JSON (no migration fields) | Works, fields default to null |
| `test_pump_monitor_deprecation` | Run legacy pump monitor | Warning logged, unified monitor runs |
| `test_backward_compat_check_exits` | Call check_exits() directly | Works as before |

---

## 14. RISK ANALYSIS

### 14.1 Phase A Risks

| Risk | Likelihood | Impact | Mitigation | Rollback |
|------|------------|--------|------------|----------|
| Unified monitor crashes | Low | High - no exit monitoring | Run as daemon thread with auto-restart | Delete enzo_exit_monitor.py |
| Duplicate exit checks | Medium | Medium - double-close | Use `_monitoring_mints` set to dedupe | Remove unified monitor, revert to pump-only |
| Thread safety issues | Medium | High - race conditions | Use `threading.Lock()` for portfolio writes | Revert to single-threaded pump monitor |
| Performance degradation | Low | Low - slower exits | Benchmark cycle time, optimize if needed | Increase interval_seconds |
| Feature flag misconfig | Low | Medium - monitor disabled | Default `enabled: true`, log warning if false | Set flag to true |

### 14.2 Phase B Risks

| Risk | Likelihood | Impact | Mitigation | Rollback |
|------|------------|--------|------------|----------|
| Migration detection false positive | Medium | High - wrong price source | Require multiple checks before switching | Remove migration fields from JSON |
| Pool detection fails | Medium | Medium - no pool address | Fall back to GMGN without pool | Delete raydium_pool field |
| Schema migration breaks existing positions | Low | High - positions unloadable | Fields are optional, null-safe | Restore portfolio from backup |
| GMGN doesn't have migrated token | Medium | High - no price | Log warning, hold position | Revert to bonding curve if available |
| Migration phase out of sync | Low | Medium - wrong source | Re-detect on every cycle | Force re-detection via manual trigger |

### 14.3 Phase C Risks

| Risk | Likelihood | Impact | Mitigation | Rollback |
|------|------------|--------|------------|----------|
| False positive dual failure | Low | High - halt new entries unnecessarily | Require 3 consecutive failures | Set halt_new_entries=false in config |
| Alert spam | Medium | Low - Telegram noise | Rate-limit alerts to 1 per minute | Disable failure alerts |
| Reconciliation race condition | Low | Medium - subscription thrash | Use lock, run every 30s max | Disable reconciliation |
| Health check overhead | Low | Low - extra CPU | Keep checks lightweight | Remove health endpoint |
| Halt not cleared on recovery | Medium | Medium - stuck in halt state | Auto-clear after 5min success | Manual clear via botctl |

### 14.4 Phase D Risks

| Risk | Likelihood | Impact | Mitigation | Rollback |
|------|------------|--------|------------|----------|
| Dashboard JS errors | Medium | Low - prices don't update | Add error handling, fallback to file | Revert dashboard HTML |
| API endpoint exposes data | Low | Medium - security risk | Bind to localhost only, no CORS | Remove endpoints |
| Health endpoint fails | Low | Low - no metrics | Return minimal JSON, never raise | Remove endpoint |
| Dashboard performance | Low | Low - slow render | Keep JSON small, paginate if needed | Simplify dashboard |

### 14.5 Overall System Risks

| Risk | Likelihood | Impact | Mitigation | Rollback |
|------|------------|--------|------------|----------|
| All price sources fail simultaneously | Low | Critical | Hold positions, alert, no auto-sell | Manual intervention |
| Portfolio file corruption | Low | Critical | Backup before every write, atomic writes | Restore from .bak file |
| Thread deadlock | Low | Critical - system freeze | Use timeouts, deadlock detection | Restart process |
| Memory leak in monitor thread | Low | Medium - OOM over days | Profile memory, restart daily | Reduce polling frequency |
| GMGN permanent ban | Medium | High - no fallback | Pump is primary, GMGN optional | Disable GMGN fallback |

---

## 15. FINAL RECOMMENDATION

### 15.1 Recommended Architecture

**Implement Phase A first** (Independent Unified Exit Monitor) as the critical foundation.

**Architecture choice:**
- Single `enzo_exit_monitor.py` module
- Runs as daemon thread in `enzo_engine.main()`
- Monitors ALL positions from `portfolio.open_positions`
- Uses `price_provider.batch_prices()` with pump-first fallback chain
- Calls `portfolio.check_exits()` as SINGLE SOURCE of exit decisions
- Gracefully deprecates `enzo_pump._exit_monitor_loop()`

**Why this architecture:**
1. **Single source of truth:** No duplicated exit logic
2. **Discovery agnostic:** Works for pump, watchlist, or manual positions
3. **Batch optimized:** One pump call for all positions, minimal GMGN
4. **Graceful degradation:** Fallback chain, hold on failure
5. **Backward compatible:** Existing `check_exits()` unchanged
6. **Clean rollback:** Delete one file, revert one function call

### 15.2 Implementation Order

| Priority | Phase | Duration | Dependencies |
|----------|-------|----------|--------------|
| **P0** | Phase A: Unified Exit Monitor | 2-3 days | None |
| P1 | Phase B: Raydium Migration | 3-4 days | Phase A |
| P1 | Phase C: Source Failure | 2-3 days | Phase A |
| P2 | Phase D: Dashboard API | 1-2 days | Phase C |

**Total estimated duration:** 8-12 days

### 15.3 Success Criteria

After all phases complete:
- [ ] ALL positions monitored continuously (≤3s latency)
- [ ] Pump→Raydium migration seamless
- [ ] Price source failures handled gracefully
- [ ] No auto-sell on price failure
- [ ] Dashboard shows live prices
- [ ] Health status visible
- [ ] Zero code duplication
- [ ] All existing tests pass
- [ ] Paper trading unaffected
- [ ] Learning outcomes recorded

### 15.4 Safe Deployment Strategy

1. **Development:** Implement in workspace, test locally
2. **Staging:** Run with `enable_unified_monitor: false`, verify legacy still works
3. **Canary:** Enable for 1-2 positions, monitor for 24h
4. **Full rollout:** Enable for all positions, deprecate legacy after 1 week
5. **Cleanup:** Remove legacy code after 2 weeks stable

---

## AUDIT SUMMARY

| Metric | Value |
|--------|-------|
| **Files inspected** | `enzo_pump.py`, `enzo_engine.py`, `enzo_portfolio.py`, `enzo_pricefeed.py`, `enzo_gmgn.py`, `enzo_pump_adv.py`, `enzo_run.py`, `enzo_serve.py`, `enzo_botctl.py`, `enzo_config.py`, `enzo_log.py`, `enzo_dashboard.py` |
| **Files modified** | **0** |
| **Files deleted** | **0** |
| **Files moved** | **0** |
| **Lines of plan** | ~1000 |
| **Implementation phases** | 4 |
| **Estimated duration** | 8-12 days |

---

**PLAN COMPLETE — NO FILES MODIFIED**

**Next step:** User approval to proceed with Phase A implementation.