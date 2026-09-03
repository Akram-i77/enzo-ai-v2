# ENZO Exit Monitor Implementation Plan — Phase A Only

**Date:** 2026-08-12
**Goal:** Implement unified exit monitor with minimal changes, maximum stability.
**Scope:** Phase A ONLY — no Phase B/C/D, no refactoring outside scope.

---

## 1. Files to Be Modified

| File | Change Type | Description |
|------|-------------|-------------|
| `enzo_exit_monitor.py` | **NEW** | Unified exit monitor module |
| `enzo_engine.py` | **MINIMAL** | Import and start unified monitor in `main()` |
| `enzo_pump.py` | **MINIMAL** | Add `_using_unified_monitor` flag; skip exits in legacy if unified is active |

**NO changes to:**
- `enzo_portfolio.py` — `check_exits()` remains unchanged
- `enzo_gmgn.py` — no changes
- `enzo_pump_adv.py` — no changes
- `enzo_pricefeed.py` — no changes
- `enzo_botctl.py` — no changes
- `enzo-config.yaml` — no changes
- Any state/secrets files

---

## 2. Function Signatures (Verified from Code Inspection)

### 2.1 `enzo_portfolio.py`

```python
def load_state() -> dict:
    """Returns portfolio dict with open_positions, closed_positions, etc."""

def check_exits(current_mcaps: dict) -> tuple[list, list]:
    """
    current_mcaps: {mint: live_market_cap_usd}
    Returns: (closed_list, partial_list)
    Each record has: symbol, mint, exit_market_cap, pnl, pnl_pct, reason
    """

def current_market_cap(mint: str) -> float | None:
    """Best-effort live market cap — pump_adv → GMGN → curve."""

def open_position(decision: dict, cfg: dict = None) -> dict:
    """Opens paper position. Returns {ok, reason, position}."""

def close_position(state: dict, mint: str, exit_market_cap: float, reason: str) -> dict:
    """Closes position. Returns {ok, reason, record}."""
```

**State file:** `enzo-portfolio.json` — written via `json.dump()` in `save_state()`.
**NO file locking currently.** Writes are not atomic (no temp file + rename).

### 2.2 `enzo_pump_adv.py`

```python
def live_prices(mints: list, force: bool = False) -> dict:
    """
    ONE batch POST → {mint: {"market_cap_usd": float|None, "price_usd": float|None, ...}}
    Cached 1s (configurable via price_refresh_secs).
    Never raises — returns {} on failure.
    """

def live_price(mint: str, force: bool = False) -> float | None:
    """Single mint market cap. Uses batch cache when possible."""

def position_prices(positions: dict) -> dict:
    """
    Convenience: {mint: market_cap_usd} for open positions dict.
    Filters out mints without price.
    """
```

### 2.3 `enzo_gmgn.py`

```python
def get_market_data(mint: str) -> dict | None:
    """
    Returns {token_symbol, price_usd, signals:{market_cap_usd, ...}, phase, data_source}
    Primary: pump_adv → GMGN fallback.
    Cached 10s for market_data.
    """

def get_live_market_cap(mint: str) -> float | None:
    """Best-effort market cap from market_data or bonding curve."""

def get_live_price(mint: str) -> float | None:
    """Best-effort price from market_data."""
```

### 2.4 `enzo_pricefeed.py`

```python
class PriceFeed:
    def start(self): ...
    def stop(self): ...
    def subscribe(self, mint: str): ...
    def unsubscribe(self, mint: str): ...
    def subscribed(self) -> set: ...
    def get_price(self, mint: str) -> float | None: ...

feed = None  # module singleton

def get_feed() -> PriceFeed: ...
```

**Current usage:** `enzo_pump.handle()` subscribes to new positions.
**Polling interval:** `fresh_secs=5.0` (default).

---

## 3. Process Architecture Analysis

### 3.1 How ENZO Runs

| Process | Entry Point | Main Threads |
|---------|-------------|--------------|
| **enzo_pump.py** | `run()` | `_pump_loop` (discovery), `worker` (pipeline), `_exit_monitor_loop` (exit checks) |
| **enzo_engine.py** | `main() --loop N` | Main thread scans watchlist every N seconds |
| **enzo_botctl.py** | `run()` | Main thread listens for Telegram commands |
| **enzo_serve.py** | `main()` | HTTP server on port 8077 |

**Key findings:**
1. `enzo_pump.py` and `enzo_engine.py` run in **SEPARATE PROCESSES** (started via `subprocess.Popen` with `start_new_session=True`).
2. Each process has its **own memory space** — `_monitoring_mints` set in one process is invisible to the other.
3. `enzo_portfolio.json` is the **shared state** — no file locking exists currently.
4. **Race condition possible:** Two processes could call `check_exits()` simultaneously, both see the same position, and both try to close it.

### 3.2 Current Exit Monitoring

| Source | When Exits Checked | What Positions |
|--------|-------------------|----------------|
| `enzo_pump._exit_monitor_loop()` | Every 1s (default) | ALL open positions from `portfolio.load_state()` |
| `enzo_engine.scan_once()` | Every scan interval (60s default) | Only positions whose mint is in watchlist |
| `enzo_pump.handle()` | After BUY decision | Just-opened position (immediate check) |

**Gap identified:**
- Positions opened via watchlist/engine are **NOT monitored continuously**.
- Only checked during 60s scan cycles or if `enzo_pump` happens to have the mint.
- **Legacy `_exit_monitor_loop()` already monitors ALL positions** — it reads `portfolio.load_state()` every cycle.

---

## 4. Duplicate Processing Prevention Strategy

### 4.1 Problem
If both `enzo_pump._exit_monitor_loop()` and new unified monitor run simultaneously:
- Both read the same `enzo-portfolio.json`
- Both call `check_exits()` with the same prices
- Both could try to close the same position

### 4.2 Solution: File-Based Atomic Lock

**Add a simple lock file:** `enzo-portfolio.lock`

```python
# In enzo_exit_monitor.py
LOCK_PATH = "enzo-portfolio.lock"

def acquire_lock(timeout=5.0) -> bool:
    """Try to acquire exclusive lock. Returns True if acquired."""
    # Implementation uses non-blocking file creation

def release_lock():
    """Remove lock file if we own it."""
```

**In `check_exits()` call:**
- Unified monitor acquires lock before calling `check_exits()`
- Legacy monitor checks for unified monitor lock and skips if held
- Lock is released immediately after `check_exits()` returns

**Why this works:**
- Single lock file is visible across processes
- Atomic file creation (`os.O_CREAT | os.O_EXCL`) prevents races
- Lock is held for <1 second (just during `check_exits()`)
- If process crashes, lock is stale (timeout-based cleanup)

### 4.3 Fallback: Process Presence Check

Since unified monitor runs in `enzo_engine` process:
- Legacy monitor can check if unified is "active" via a heartbeat file
- `enzo_exit_monitor` writes `enzo-exit-monitor-heartbeat.json` every cycle
- Legacy skips exit processing if heartbeat is fresh (<5s)

**Implementation:**
```python
# In enzo_pump._exit_monitor_loop()
def _unified_monitor_active() -> bool:
    try:
        with open("enzo-exit-monitor-heartbeat.json") as f:
            hb = json.load(f)
        return time.time() - hb.get("ts", 0) < 5.0
    except:
        return False
```

---

## 5. Unified Exit Monitor Design

### 5.1 Module: `enzo_exit_monitor.py`

```python
#!/usr/bin/env python3
"""
ENZO - Unified Exit Monitor

Monitors ALL open positions regardless of source (pump, watchlist, manual).
Runs as a single daemon thread. Calls portfolio.check_exits() as the sole
exit decision authority.

Lifecycle:
  - start(): idempotent, starts daemon thread if not running
  - stop(): signals thread to stop, waits for join
  - is_running(): returns True if thread is alive

Price sources (in order):
  1. Pump Advanced batch API (live_prices) — one POST for all positions
  2. GMGN market_data fallback for missing mints
  3. Never auto-sell when price unavailable

Duplicate prevention:
  - Acquires file lock before check_exits()
  - Writes heartbeat every cycle
  - Legacy monitor skips when heartbeat is fresh
"""
```

### 5.2 Key Functions

```python
# Configuration
DEFAULT_INTERVAL = 2.0  # seconds
LOCK_PATH = "enzo-portfolio.lock"
HEARTBEAT_PATH = "enzo-exit-monitor-heartbeat.json"

# State
_monitor_thread = None
_stop_event = threading.Event()
_started = False
_interval = DEFAULT_INTERVAL

def start(interval: float = DEFAULT_INTERVAL) -> bool:
    """Start the monitor thread. Idempotent. Returns True if started."""

def stop():
    """Signal stop and wait for thread to finish."""

def is_running() -> bool:
    """Returns True if monitor thread is alive."""

def _run_cycle():
    """One monitoring cycle: fetch prices → check_exits → notify."""

def _fetch_prices(mints: list) -> dict:
    """Fetch market caps for all mints. Returns {mint: mcap}."""

def _acquire_lock() -> bool:
    """Acquire file lock. Returns True if acquired."""

def _release_lock():
    """Release file lock."""

def _write_heartbeat():
    """Write heartbeat file with current timestamp."""

def _monitor_loop():
    """Main loop: cycles every interval until stop signal."""
```

### 5.3 Integration Points

**In `enzo_engine.py`:**
```python
# At top of main(), before scan loop
import enzo_exit_monitor

def main():
    # ... existing code ...
    enzo_exit_monitor.start(interval=2.0)  # Start unified monitor
    # ... existing loop ...
    # On KeyboardInterrupt:
    enzo_exit_monitor.stop()
```

**In `enzo_pump.py`:**
```python
# In _exit_monitor_loop(), add check at start of each cycle:
def _exit_monitor_loop(interval=None):
    # ... existing setup ...
    while not _shutdown.is_set():
        # NEW: Skip if unified monitor is active
        if _unified_monitor_active():
            _shutdown.wait(interval)
            continue
        # ... existing logic ...
```

---

## 6. Lifecycle Details

### 6.1 Startup Sequence
1. `enzo_engine.main()` called
2. `enzo_exit_monitor.start()` called
3. Monitor thread starts, enters `_monitor_loop()`
4. First cycle: reads portfolio, fetches prices, checks exits
5. Writes heartbeat file
6. Sleeps for interval

### 6.2 Shutdown Sequence
1. `KeyboardInterrupt` caught in `main()`
2. `enzo_exit_monitor.stop()` called
3. `_stop_event.set()` signals thread
4. Thread exits loop
5. `thread.join(timeout=5)` waits for clean exit

### 6.3 Exception Handling
- Each cycle wrapped in `try/except`
- Exception logged but does NOT kill thread
- Thread continues to next cycle after exception

### 6.4 Idempotency
- `start()` checks if `_monitor_thread` is alive before creating new one
- Second call to `start()` returns `False` (no new thread created)
- `stop()` safe to call even if not running

---

## 7. Locking Strategy

### 7.1 Lock File: `enzo-portfolio.lock`

**Acquisition (atomic):**
```python
def _acquire_lock() -> bool:
    try:
        fd = os.open(LOCK_PATH, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        os.write(fd, f"{os.getpid()}\n{time.time()}".encode())
        os.close(fd)
        return True
    except FileExistsError:
        # Lock held by another process
        # Check if stale (>10s old)
        try:
            st = os.stat(LOCK_PATH)
            if time.time() - st.st_mtime > 10:
                os.unlink(LOCK_PATH)
                return _acquire_lock()  # retry
        except:
            pass
        return False
    except Exception:
        return False
```

**Release:**
```python
def _release_lock():
    try:
        # Only remove if we own it (pid matches)
        with open(LOCK_PATH) as f:
            pid = int(f.read().strip().split('\n')[0])
        if pid == os.getpid():
            os.unlink(LOCK_PATH)
    except:
        pass
```

### 7.2 Lock Scope
- Acquired immediately before `check_exits()`
- Released immediately after
- Held for <1 second typically

### 7.3 Stale Lock Handling
- Lock older than 10 seconds is considered stale
- Stale lock is removed before acquisition attempt

---

## 8. Heartbeat Mechanism

### 8.1 Heartbeat File: `enzo-exit-monitor-heartbeat.json`

```json
{
  "ts": 1723456789.123,
  "pid": 12345,
  "cycle": 42,
  "positions": 3,
  "last_check": "2026-08-12T12:34:56Z"
}
```

### 8.2 Write Frequency
- Written at the end of each monitoring cycle
- Timestamp is `time.time()` at write moment

### 8.3 Legacy Monitor Check
```python
def _unified_monitor_active() -> bool:
    try:
        with open(HEARTBEAT_PATH) as f:
            hb = json.load(f)
        age = time.time() - hb.get("ts", 0)
        return age < 5.0  # Fresh heartbeat = unified is active
    except:
        return False
```

---

## 9. Test Plan

### 9.1 Test File: `test_enzo_exit_monitor.py`

All tests run with paper trading, no real funds.

| # | Test | Setup | Expected |
|---|------|-------|----------|
| 1 | No open positions → monitor idle | Empty portfolio | No errors, no exits |
| 2 | One watchlist position → monitored | 1 position in portfolio | Position checked each cycle |
| 3 | Multiple positions → all monitored | 3 positions | All 3 prices fetched, all checked |
| 4 | Pump price success → no GMGN | Position pump knows | Pump batch called, GMGN not called |
| 5 | Pump missing mint → GMGN fallback | Position pump doesn't know | Pump returns None, GMGN called |
| 6 | Pump failure → GMGN fallback | Pump raises exception | Falls back to GMGN gracefully |
| 7 | Price unavailable → no auto-sell | No price source works | Position NOT closed |
| 8 | check_exits() is sole decision | Position at TP | check_exits returns close, monitor notifies |
| 9 | Monitor exception → thread continues | Raise in price fetch | Next cycle runs normally |
| 10 | start() twice → one thread | Call start() twice | Second returns False, one thread |
| 11 | Legacy + unified → no double close | Both monitors running | Lock prevents duplicate |
| 12 | Restart → positions continue | Stop/start monitor | Positions still monitored |

### 9.2 Test Implementation

```python
import unittest
import time
import json
import os
import threading

class TestExitMonitor(unittest.TestCase):
    def setUp(self):
        # Backup portfolio state
        # Create fresh test portfolio
        pass
    
    def tearDown(self):
        # Restore portfolio
        # Clean up lock/heartbeat files
        pass
    
    def test_no_positions(self): ...
    def test_single_position(self): ...
    def test_multiple_positions(self): ...
    def test_pump_price_success(self): ...
    def test_pump_missing_gmgn_fallback(self): ...
    def test_pump_failure_gmgn_fallback(self): ...
    def test_price_unavailable_no_sell(self): ...
    def test_check_exits_sole_authority(self): ...
    def test_exception_continues(self): ...
    def test_idempotent_start(self): ...
    def test_no_double_close(self): ...
    def test_restart_continuity(self): ...
```

---

## 10. Rollback Plan

### 10.1 If Unified Monitor Fails

1. **Stop `enzo_engine.py`** (Ctrl+C)
2. **Remove import from `enzo_engine.py`:**
   ```python
   # Comment out or remove:
   # import enzo_exit_monitor
   # enzo_exit_monitor.start()
   # enzo_exit_monitor.stop()
   ```
3. **Remove `_unified_monitor_active()` check from `enzo_pump.py`** (restore original)
4. **Delete `enzo_exit_monitor.py`**
5. **Delete lock/heartbeat files:**
   ```bash
   rm -f enzo-portfolio.lock enzo-exit-monitor-heartbeat.json
   ```
6. **Restart `enzo_pump.py`** — legacy monitor resumes

### 10.2 Files to Restore

No files are modified in place except minimal additions:
- `enzo_engine.py`: 3 lines added (import + start + stop)
- `enzo_pump.py`: 5-10 lines added (unified check)

Both can be easily reverted manually.

### 10.3 State Protection

- `enzo-portfolio.json` is never modified by the monitor itself
- Only `check_exits()` modifies it (unchanged code)
- No risk of state corruption from rollback

---

## 11. Dependencies

### 11.1 New Dependencies
**NONE** — uses only existing modules:
- `enzo_portfolio` — for state and `check_exits()`
- `enzo_pump_adv` — for `live_prices()` batch API
- `enzo_gmgn` — for fallback price lookup
- `enzo_notify` — for exit notifications
- `enzo_learn` — for outcome recording

### 11.2 Threading
- Uses `threading.Thread` with `daemon=True`
- Uses `threading.Event` for stop signal
- No new process spawning

---

## 12. Performance Considerations

### 12.1 API Call Budget

| Positions | Pump Batch Calls | GMGN Fallback | Total per Cycle |
|-----------|------------------|---------------|-----------------|
| 1 | 1 POST | 0-1 GET | 1-2 calls |
| 5 | 1 POST | 0-5 GET | 1-6 calls |
| 10 | 1 POST | 0-10 GET | 1-11 calls |
| 30 | 1 POST | 0-30 GET | 1-31 calls |

**With 2s interval:**
- 1-10 positions: ~0.5-5 calls/sec (well within GMGN ~1/sec limit)
- 30 positions: ~15 calls/sec if all fallback — may hit rate limit

**Mitigation:** Monitor tracks which mints consistently miss pump and skips GMGN for them after N failures.

### 12.2 Memory Footprint

- Single thread
- No caching beyond pump_adv built-in cache
- Heartbeat file < 500 bytes
- Lock file < 100 bytes

---

## 13. Summary

**What we're building:**
- New `enzo_exit_monitor.py` — single daemon thread
- Monitors ALL positions every 2s
- Uses pump_adv batch API for prices
- Falls back to GMGN when needed
- Calls `portfolio.check_exits()` as sole exit authority
- File-based lock prevents duplicate processing
- Heartbeat file signals unified monitor is active
- Legacy monitor skips when unified is running

**What we're NOT building:**
- No new price provider abstraction
- No schema changes
- No GMGN logic changes
- No config changes
- No Phase B/C/D features

**Minimal changes:**
- 3 lines in `enzo_engine.py`
- 5-10 lines in `enzo_pump.py`
- 1 new file: `enzo_exit_monitor.py`

**Safety:**
- Idempotent start/stop
- Exception resilient
- Graceful fallback
- Easy rollback

---

**Approval required before implementation.**
