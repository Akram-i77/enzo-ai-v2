#!/usr/bin/env python3
"""
ENZO - SQLite High-Performance Database Layer (WAL Mode & Atomic Concurrency Safe)
"""
import sqlite3
import json
import os
import time
import threading
from datetime import datetime, timezone
from contextlib import contextmanager
from typing import Dict, Any, List, Optional
from enzo.core.config import PORTFOLIO_DB_PATH, PORTFOLIO_JSON_PATH

_init_lock = threading.Lock()
_DB_INITIALIZED = False


def _now_iso():
    return datetime.now(timezone.utc).isoformat()


def get_connection(timeout: float = 30.0) -> sqlite3.Connection:
    """Create a connection configured with high busy-timeout for concurrency."""
    conn = sqlite3.connect(PORTFOLIO_DB_PATH, timeout=timeout, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA busy_timeout = 30000;")
    except Exception:
        pass
    return conn


class db_cursor:
    """Context manager for safe database transactions with busy-retry on acquire.

    NOTE: Retry happens only while *acquiring* the connection/cursor. Once the
    caller's body runs, lock errors surface to the caller (busy_timeout=30s
    already absorbs most contention). This fixes the old generator-based
    implementation whose retry loop attempted a second `yield`, which is
    illegal with contextlib.contextmanager (RuntimeError: generator didn't
    stop after throw).
    """
    def __init__(self, commit: bool = True, max_retries: int = 5):
        self.commit = commit
        self.max_retries = max_retries
        self.conn = None
        self.cursor = None

    def __enter__(self):
        last_err = None
        for attempt in range(self.max_retries):
            try:
                self.conn = get_connection(timeout=30.0)
                self.cursor = self.conn.cursor()
                return self.cursor
            except sqlite3.OperationalError as e:
                last_err = e
                if "locked" in str(e).lower() or "busy" in str(e).lower():
                    time.sleep(0.1 * (attempt + 1))
                    continue
                raise
        if last_err:
            raise last_err
        raise sqlite3.OperationalError("db_cursor: could not acquire connection after retries")

    def __exit__(self, exc_type, exc, tb):
        if self.conn is None:
            return False
        try:
            if exc_type is None:
                if self.commit:
                    self.conn.commit()
            else:
                self.conn.rollback()
        finally:
            self.conn.close()
            self.conn = None
            self.cursor = None
        return False


def init_db():
    """Create tables and indexes if they do not exist (idempotent & thread-safe)."""
    global _DB_INITIALIZED
    with _init_lock:
        if _DB_INITIALIZED:
            return

        try:
            conn = get_connection(timeout=30.0)
            conn.execute("PRAGMA journal_mode = WAL;")
            conn.execute("PRAGMA synchronous = NORMAL;")
            conn.close()
        except Exception:
            pass

        try:
            with db_cursor(commit=True) as cur:
                # 1. Portfolio State Singleton
                cur.execute("""
                    CREATE TABLE IF NOT EXISTS portfolio_state (
                        id INTEGER PRIMARY KEY CHECK (id = 1),
                        initial_capital REAL NOT NULL DEFAULT 10000.0,
                        realized_pnl REAL NOT NULL DEFAULT 0.0,
                        daily_loss REAL NOT NULL DEFAULT 0.0,
                        consecutive_losses INTEGER NOT NULL DEFAULT 0,
                        peak_equity REAL NOT NULL DEFAULT 10000.0,
                        last_day TEXT NOT NULL,
                        last_updated TEXT NOT NULL,
                        halted TEXT
                    )
                """)

                # 2. Open Positions Table
                cur.execute("""
                    CREATE TABLE IF NOT EXISTS open_positions (
                        mint TEXT PRIMARY KEY,
                        symbol TEXT NOT NULL,
                        entry_price REAL NOT NULL,
                        entry_market_cap REAL NOT NULL,
                        current_market_cap REAL,
                        size_usd REAL NOT NULL,
                        initial_size_usd REAL NOT NULL,
                        amount REAL NOT NULL,
                        initial_amount REAL NOT NULL,
                        stop_loss_mc REAL,
                        take_profit_mc REAL,
                        trailing_active INTEGER DEFAULT 0,
                        trailing_stop_mc REAL,
                        peak_price REAL,
                        peak_market_cap REAL,
                        realized_pnl_total REAL DEFAULT 0.0,
                        unrealized_pnl REAL DEFAULT 0.0,
                        opened_at TEXT NOT NULL,
                        max_holding_hours REAL DEFAULT 48.0,
                        stages_hit_json TEXT,
                        signals_json TEXT,
                        axis_scores_json TEXT,
                        features_json TEXT,
                        extra_json TEXT
                    )
                """)

                # 3. Closed Trades History
                cur.execute("""
                    CREATE TABLE IF NOT EXISTS closed_trades (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        mint TEXT NOT NULL,
                        symbol TEXT NOT NULL,
                        entry_price REAL NOT NULL,
                        exit_price REAL NOT NULL,
                        entry_market_cap REAL NOT NULL,
                        exit_market_cap REAL NOT NULL,
                        size_usd REAL NOT NULL,
                        pnl REAL NOT NULL,
                        pnl_pct REAL NOT NULL,
                        reason TEXT NOT NULL,
                        opened_at TEXT NOT NULL,
                        closed_at TEXT NOT NULL,
                        signals_json TEXT,
                        axis_scores_json TEXT,
                        features_json TEXT
                    )
                """)
                cur.execute("CREATE INDEX IF NOT EXISTS idx_closed_mint ON closed_trades(mint)")
                cur.execute("CREATE INDEX IF NOT EXISTS idx_closed_ts ON closed_trades(closed_at)")

                # 4. Token Bucket Rate Limiter & Ban Coordinator
                cur.execute("""
                    CREATE TABLE IF NOT EXISTS rate_limiter (
                        key TEXT PRIMARY KEY,
                        tokens REAL NOT NULL,
                        last_updated REAL NOT NULL,
                        banned_until REAL NOT NULL DEFAULT 0.0,
                        rate_per_sec REAL NOT NULL DEFAULT 1.0,
                        capacity REAL NOT NULL DEFAULT 3.0
                    )
                """)

                # 5. Persistent L2 Key-Value Cache
                cur.execute("""
                    CREATE TABLE IF NOT EXISTS cache_store (
                        key TEXT PRIMARY KEY,
                        value_json TEXT NOT NULL,
                        ts REAL NOT NULL,
                        ttl REAL NOT NULL
                    )
                """)
                cur.execute("CREATE INDEX IF NOT EXISTS idx_cache_ts ON cache_store(ts)")

            _migrate_from_json_if_needed()
            _DB_INITIALIZED = True
        except Exception as e:
            if "locked" in str(e).lower():
                _DB_INITIALIZED = True
            else:
                raise


def _migrate_from_json_if_needed():
    """Import existing enzo-portfolio.json data into SQLite if state table is empty."""
    try:
        with db_cursor(commit=True) as cur:
            row = cur.execute("SELECT id FROM portfolio_state WHERE id = 1").fetchone()
            if row is not None:
                return

            data = {}
            if os.path.exists(PORTFOLIO_JSON_PATH):
                try:
                    with open(PORTFOLIO_JSON_PATH, "r", encoding="utf-8") as f:
                        data = json.load(f)
                except Exception:
                    data = {}

            init_cap = float(data.get("initial_capital", 10000.0))
            realized = float(data.get("realized_pnl", 0.0))
            daily_loss = float(data.get("daily_loss", 0.0))
            cons = int(data.get("consecutive_losses", 0))
            peak = float(data.get("peak_equity", init_cap))
            last_day = data.get("last_day") or _now_iso()[:10]
            last_upd = data.get("last_updated") or _now_iso()

            cur.execute("""
                INSERT INTO portfolio_state (
                    id, initial_capital, realized_pnl, daily_loss, consecutive_losses,
                    peak_equity, last_day, last_updated
                ) VALUES (1, ?, ?, ?, ?, ?, ?, ?)
            """, (init_cap, realized, daily_loss, cons, peak, last_day, last_upd))

            for mint, pos in (data.get("open_positions") or {}).items():
                cur.execute("""
                    INSERT OR REPLACE INTO open_positions (
                        mint, symbol, entry_price, entry_market_cap, current_market_cap,
                        size_usd, initial_size_usd, amount, initial_amount, stop_loss_mc,
                        take_profit_mc, trailing_active, trailing_stop_mc, peak_price,
                        peak_market_cap, realized_pnl_total, unrealized_pnl, opened_at,
                        max_holding_hours, stages_hit_json, signals_json, axis_scores_json,
                        features_json, extra_json
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, (
                    mint,
                    pos.get("symbol", "UNKNOWN"),
                    float(pos.get("entry_price", 0.0)),
                    float(pos.get("entry_market_cap", 0.0)),
                    float(pos.get("current_market_cap") or pos.get("entry_market_cap") or 0.0),
                    float(pos.get("size_usd", 0.0)),
                    float(pos.get("initial_size_usd", pos.get("size_usd", 0.0))),
                    float(pos.get("amount", 0.0)),
                    float(pos.get("initial_amount", pos.get("amount", 0.0))),
                    float(pos.get("stop_loss_mc") or 0.0),
                    float(pos.get("take_profit_mc") or 0.0),
                    1 if pos.get("trailing_active") else 0,
                    float(pos.get("trailing_stop_mc") or 0.0) if pos.get("trailing_stop_mc") else None,
                    float(pos.get("peak_price") or pos.get("entry_price") or 0.0),
                    float(pos.get("peak_market_cap") or pos.get("entry_market_cap") or 0.0),
                    float(pos.get("realized_pnl_total", 0.0)),
                    float(pos.get("unrealized_pnl", 0.0)),
                    pos.get("opened_at") or _now_iso(),
                    float(pos.get("max_holding_hours", 48.0)),
                    json.dumps(pos.get("stages_hit", [])),
                    json.dumps(pos.get("signals", [])),
                    json.dumps(pos.get("axis_scores", {})),
                    json.dumps(pos.get("features", {})),
                    json.dumps({k: v for k, v in pos.items() if k not in [
                        "symbol", "entry_price", "entry_market_cap", "current_market_cap",
                        "size_usd", "initial_size_usd", "amount", "initial_amount",
                        "stop_loss_mc", "take_profit_mc", "trailing_active", "trailing_stop_mc",
                        "peak_price", "peak_market_cap", "realized_pnl_total", "unrealized_pnl",
                        "opened_at", "max_holding_hours", "stages_hit", "signals",
                        "axis_scores", "features"
                    ]})
                ))

            # Full-state saves are resets: clear stale closed trades first so a
            # clean slate does not keep ghost entries from previous sessions.
            cur.execute("DELETE FROM closed_trades")

            for rec in (data.get("closed_positions") or []):
                cur.execute("""
                    INSERT INTO closed_trades (
                        mint, symbol, entry_price, exit_price, entry_market_cap,
                        exit_market_cap, size_usd, pnl, pnl_pct, reason, opened_at,
                        closed_at, signals_json, axis_scores_json, features_json
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, (
                    rec.get("mint", "UNKNOWN"),
                    rec.get("symbol", "UNKNOWN"),
                    float(rec.get("entry_price", 0.0)),
                    float(rec.get("exit_price", 0.0)),
                    float(rec.get("entry_market_cap", 0.0)),
                    float(rec.get("exit_market_cap", 0.0)),
                    float(rec.get("size_usd", 0.0)),
                    float(rec.get("pnl", 0.0)),
                    float(rec.get("pnl_pct", 0.0)),
                    rec.get("reason", "CLOSED"),
                    rec.get("opened_at") or _now_iso(),
                    rec.get("closed_at") or _now_iso(),
                    json.dumps(rec.get("signals", [])),
                    json.dumps(rec.get("axis_scores", {})),
                    json.dumps(rec.get("features", {}))
                ))
    except Exception:
        pass


def _pos_row_to_dict(row: sqlite3.Row) -> dict:
    d = dict(row)
    try:
        d["stages_hit"] = json.loads(d.get("stages_hit_json") or "[]")
    except Exception:
        d["stages_hit"] = []
    try:
        d["signals"] = json.loads(d.get("signals_json") or "[]")
    except Exception:
        d["signals"] = []
    try:
        d["axis_scores"] = json.loads(d.get("axis_scores_json") or "{}")
    except Exception:
        d["axis_scores"] = {}
    try:
        d["features"] = json.loads(d.get("features_json") or "{}")
    except Exception:
        d["features"] = {}
    if d.get("extra_json"):
        try:
            extra = json.loads(d["extra_json"])
            d.update(extra)
        except Exception:
            pass
    d["trailing_active"] = bool(d.get("trailing_active"))
    return d


def _closed_row_to_dict(row: sqlite3.Row) -> dict:
    d = dict(row)
    try:
        d["signals"] = json.loads(d.get("signals_json") or "[]")
    except Exception:
        d["signals"] = []
    try:
        d["axis_scores"] = json.loads(d.get("axis_scores_json") or "{}")
    except Exception:
        d["axis_scores"] = {}
    try:
        d["features"] = json.loads(d.get("features_json") or "{}")
    except Exception:
        d["features"] = {}
    return d


def get_full_state() -> dict:
    """Fetch complete portfolio state in standard format."""
    init_db()
    try:
        with db_cursor(commit=False) as cur:
            st_row = cur.execute("SELECT * FROM portfolio_state WHERE id = 1").fetchone()
            if not st_row:
                return _fallback_state()

            open_rows = cur.execute("SELECT * FROM open_positions ORDER BY opened_at ASC").fetchall()
            open_positions = {r["mint"]: _pos_row_to_dict(r) for r in open_rows}

            closed_rows = cur.execute("SELECT * FROM closed_trades ORDER BY closed_at DESC").fetchall()
            closed_positions = [_closed_row_to_dict(r) for r in closed_rows]

            return {
                "initial_capital": float(st_row["initial_capital"]),
                "realized_pnl": float(st_row["realized_pnl"]),
                "open_positions": open_positions,
                "closed_positions": closed_positions,
                "daily_loss": float(st_row["daily_loss"]),
                "consecutive_losses": int(st_row["consecutive_losses"]),
                "peak_equity": float(st_row["peak_equity"]),
                "last_day": st_row["last_day"],
                "last_updated": st_row["last_updated"],
                "halted": st_row["halted"]
            }
    except Exception:
        return _fallback_state()


def _fallback_state() -> dict:
    if os.path.exists(PORTFOLIO_JSON_PATH):
        try:
            with open(PORTFOLIO_JSON_PATH, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return {
        "initial_capital": 10000.0,
        "realized_pnl": 0.0,
        "open_positions": {},
        "closed_positions": [],
        "daily_loss": 0.0,
        "consecutive_losses": 0,
        "peak_equity": 10000.0,
        "last_day": _now_iso()[:10],
        "last_updated": _now_iso(),
    }


def _sync_json_cache():
    """Helper to keep data/enzo-portfolio.json updated without full table scans (atomic write)."""
    try:
        state = get_full_state()
        os.makedirs(os.path.dirname(PORTFOLIO_JSON_PATH), exist_ok=True)
        tmp = PORTFOLIO_JSON_PATH + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(state, f, indent=2, ensure_ascii=False)
        os.replace(tmp, PORTFOLIO_JSON_PATH)
    except Exception:
        pass


# ================================================================ Atomic DB Transactions
def atomic_open_position(pos: dict):
    """Atomically insert an open position into SQLite."""
    init_db()
    now_iso = _now_iso()
    mint = pos["mint"]

    # Any position field with no dedicated column is preserved in extra_json
    # (read back by _pos_row_to_dict). Previously such fields — capital_base_usd,
    # risk_pct_used, security_status, scam_score, weighted_confidence — were
    # silently dropped on insert, so the dashboard and the learning engine could
    # never see how a position had been sized.
    _KNOWN_POS_COLS = {
        "mint", "symbol", "entry_price", "entry_market_cap", "current_market_cap",
        "size_usd", "initial_size_usd", "amount", "initial_amount", "stop_loss_mc",
        "take_profit_mc", "trailing_active", "trailing_stop_mc", "peak_price",
        "peak_market_cap", "realized_pnl_total", "unrealized_pnl", "opened_at",
        "max_holding_hours", "stages_hit", "signals", "axis_scores", "features",
    }
    extra_json = json.dumps(
        {k: v for k, v in pos.items() if k not in _KNOWN_POS_COLS and not k.endswith("_json")},
        default=str,
    )

    with db_cursor(commit=True) as cur:
        cur.execute("""
            INSERT OR REPLACE INTO open_positions (
                mint, symbol, entry_price, entry_market_cap, current_market_cap,
                size_usd, initial_size_usd, amount, initial_amount, stop_loss_mc,
                take_profit_mc, trailing_active, trailing_stop_mc, peak_price,
                peak_market_cap, realized_pnl_total, unrealized_pnl, opened_at,
                max_holding_hours, stages_hit_json, signals_json, axis_scores_json,
                features_json, extra_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            mint,
            pos.get("symbol", "UNKNOWN"),
            float(pos.get("entry_price", 0.0)),
            float(pos.get("entry_market_cap", 0.0)),
            float(pos.get("current_market_cap") or pos.get("entry_market_cap") or 0.0),
            float(pos.get("size_usd", 0.0)),
            float(pos.get("initial_size_usd", pos.get("size_usd", 0.0))),
            float(pos.get("amount", 0.0)),
            float(pos.get("initial_amount", pos.get("amount", 0.0))),
            float(pos.get("stop_loss_mc") or 0.0),
            float(pos.get("take_profit_mc") or 0.0),
            1 if pos.get("trailing_active") else 0,
            float(pos.get("trailing_stop_mc") or 0.0) if pos.get("trailing_stop_mc") else None,
            float(pos.get("peak_price") or pos.get("entry_price") or 0.0),
            float(pos.get("peak_market_cap") or pos.get("entry_market_cap") or 0.0),
            float(pos.get("realized_pnl_total", 0.0)),
            float(pos.get("unrealized_pnl", 0.0)),
            pos.get("opened_at") or now_iso,
            float(pos.get("max_holding_hours", 48.0)),
            json.dumps(pos.get("stages_hit", [])),
            json.dumps(pos.get("signals", [])),
            json.dumps(pos.get("axis_scores", {})),
            json.dumps(pos.get("features", {})),
            extra_json
        ))

        cur.execute("UPDATE portfolio_state SET last_updated = ? WHERE id = 1", (now_iso,))

    _sync_json_cache()


def atomic_close_position(mint: str, record: dict, pnl_delta: float):
    """Atomically delete open position, insert closed trade record, and update portfolio balance."""
    init_db()
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    now_iso = _now_iso()

    with db_cursor(commit=True) as cur:
        # 1. Delete from open_positions
        cur.execute("DELETE FROM open_positions WHERE mint = ?", (mint,))

        # 2. Insert into closed_trades
        cur.execute("""
            INSERT INTO closed_trades (
                mint, symbol, entry_price, exit_price, entry_market_cap,
                exit_market_cap, size_usd, pnl, pnl_pct, reason, opened_at,
                closed_at, signals_json, axis_scores_json, features_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            mint,
            record.get("symbol", "UNKNOWN"),
            float(record.get("entry_price", 0.0)),
            float(record.get("exit_price", 0.0)),
            float(record.get("entry_market_cap", 0.0)),
            float(record.get("exit_market_cap", 0.0)),
            float(record.get("size_usd", 0.0)),
            float(record.get("pnl", 0.0)),
            float(record.get("pnl_pct", 0.0)),
            record.get("reason", "CLOSED"),
            record.get("opened_at") or now_iso,
            record.get("closed_at") or now_iso,
            json.dumps(record.get("signals", [])),
            json.dumps(record.get("axis_scores", {})),
            json.dumps(record.get("features", {}))
        ))

        # 3. Atomically update portfolio state
        st = cur.execute("SELECT * FROM portfolio_state WHERE id = 1").fetchone()
        if st:
            cur_realized = float(st["realized_pnl"]) + pnl_delta
            same_day = st["last_day"] == today
            cur_daily = (float(st["daily_loss"]) if same_day else 0.0) + pnl_delta
            cur_cons = 0 if pnl_delta >= 0 else ((int(st["consecutive_losses"]) if same_day else 0) + 1)
            init_cap = float(st["initial_capital"])
            cur_eq = init_cap + cur_realized
            new_peak = max(float(st["peak_equity"]), cur_eq)

            cur.execute("""
                UPDATE portfolio_state SET
                    realized_pnl = ?,
                    daily_loss = ?,
                    consecutive_losses = ?,
                    peak_equity = ?,
                    last_day = ?,
                    last_updated = ?
                WHERE id = 1
            """, (cur_realized, cur_daily, cur_cons, new_peak, today, now_iso))

    _sync_json_cache()


def atomic_update_open_positions(pos_updates: List[dict]):
    """Atomically update live market metrics for open positions (mcap, upnl, trailing stops).

    Also persists the *remaining position size* (size_usd / amount) and the
    accumulated realized PnL (realized_pnl_total) so that partial-exit state
    survives reloads. Previously those columns were omitted from the UPDATE,
    which made every TP stage sell against the FULL original size after a
    restart (over-selling + inflated PnL).
    """
    if not pos_updates:
        return
    init_db()

    with db_cursor(commit=True) as cur:
        for p in pos_updates:
            mint = p["mint"]
            cur.execute("""
                UPDATE open_positions SET
                    current_market_cap = ?,
                    unrealized_pnl = ?,
                    peak_market_cap = max(peak_market_cap, ?),
                    size_usd = ?,
                    amount = ?,
                    realized_pnl_total = ?,
                    trailing_active = ?,
                    trailing_stop_mc = ?,
                    stages_hit_json = ?
                WHERE mint = ?
            """, (
                float(p.get("current_market_cap", 0.0)),
                float(p.get("unrealized_pnl", 0.0)),
                float(p.get("peak_market_cap", 0.0)),
                float(p.get("size_usd", 0.0)),
                float(p.get("amount", 0.0)),
                float(p.get("realized_pnl_total", 0.0)),
                1 if p.get("trailing_active") else 0,
                float(p["trailing_stop_mc"]) if p.get("trailing_stop_mc") else None,
                json.dumps(p.get("stages_hit", [])),
                mint
            ))

    _sync_json_cache()


def atomic_update_position_extra(mint: str, updates: dict) -> bool:
    """Merge keys into an open position's extra_json without touching columns.

    Used to attach post-execution data (tx_hash, capital base, gate verdicts)
    that has no dedicated column. Returns False if the position does not exist.
    """
    if not mint or not updates:
        return False
    init_db()
    try:
        with db_cursor(commit=True) as cur:
            row = cur.execute(
                "SELECT extra_json FROM open_positions WHERE mint = ?", (mint,)
            ).fetchone()
            if not row:
                return False
            try:
                extra = json.loads(row["extra_json"] or "{}")
                if not isinstance(extra, dict):
                    extra = {}
            except Exception:
                extra = {}
            extra.update(updates)
            cur.execute(
                "UPDATE open_positions SET extra_json = ? WHERE mint = ?",
                (json.dumps(extra, default=str), mint),
            )
        _sync_json_cache()
        return True
    except Exception:
        return False


def atomic_add_realized(pnl_delta: float):
    """Add realized PnL (e.g. from a partial take-profit) to the portfolio balance.

    Called on EVERY partial exit so portfolio_state.realized_pnl tracks the
    trade ledger moment-by-moment; the final close then adds only the last
    portion's PnL.
    """
    init_db()
    now_iso = _now_iso()
    with db_cursor(commit=True) as cur:
        st = cur.execute("SELECT * FROM portfolio_state WHERE id = 1").fetchone()
        if st:
            cur_realized = float(st["realized_pnl"]) + float(pnl_delta or 0.0)
            init_cap = float(st["initial_capital"])
            cur_eq = init_cap + cur_realized
            new_peak = max(float(st["peak_equity"]), cur_eq)
            cur.execute("""
                UPDATE portfolio_state SET
                    realized_pnl = ?,
                    peak_equity = ?,
                    last_updated = ?
                WHERE id = 1
            """, (cur_realized, new_peak, now_iso))

    _sync_json_cache()


def atomic_update_peak_equity(equity_value: float):
    """Persist a higher live peak equity (called on each exit-monitor cycle)."""
    init_db()
    try:
        with db_cursor(commit=True) as cur:
            cur.execute("""
                UPDATE portfolio_state SET
                    peak_equity = max(peak_equity, ?),
                    last_updated = ?
                WHERE id = 1
            """, (float(equity_value), _now_iso()))
    except Exception:
        pass


def atomic_update_initial_capital(value: float) -> bool:
    """Rebase portfolio_state.initial_capital onto real deployable capital.

    Only ever called when there is no closed-trade history to distort (see
    portfolio.sync_capital_base) — otherwise ROI/win-rate maths would silently
    change meaning under an existing ledger.

    This exists because initial_capital sat at $2.06 while the trading wallet
    held real funds: every position sized to $0.04, below min_trade_usd, so no
    trade could ever execute even on a perfect BUY signal.
    """
    init_db()
    value = float(value)
    if value <= 0:
        return False
    try:
        with db_cursor(commit=True) as cur:
            row = cur.execute(
                "SELECT initial_capital FROM portfolio_state WHERE id = 1"
            ).fetchone()
            if not row:
                return False
            old = float(row["initial_capital"])
            if abs(old - value) < 1e-9:
                return False
            # The peak is RESET to the new baseline rather than max()'d. This is
            # only ever called with no closed-trade history, so there is no real
            # peak to preserve — and keeping a stale one (e.g. the $10,000
            # fallback default) would make the drawdown circuit breaker read a
            # 94% drawdown and halt trading permanently on the very first sync.
            cur.execute("""
                UPDATE portfolio_state SET
                    initial_capital = ?,
                    peak_equity = ?,
                    daily_loss = 0.0,
                    halted = NULL,
                    last_updated = ?
                WHERE id = 1
            """, (value, value, _now_iso()))
        _sync_json_cache()
        return True
    except Exception:
        return False


def save_full_state(state: dict):
    """Full fallback state save for initial setups or resets."""
    init_db()
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    now_iso = _now_iso()

    try:
        with db_cursor(commit=True) as cur:
            cur.execute("""
                UPDATE portfolio_state SET
                    initial_capital = ?,
                    realized_pnl = ?,
                    daily_loss = ?,
                    consecutive_losses = ?,
                    peak_equity = ?,
                    last_day = ?,
                    last_updated = ?,
                    halted = ?
                WHERE id = 1
            """, (
                float(state.get("initial_capital", 10000.0)),
                float(state.get("realized_pnl", 0.0)),
                float(state.get("daily_loss", 0.0)),
                int(state.get("consecutive_losses", 0)),
                float(state.get("peak_equity", 10000.0)),
                state.get("last_day") or today,
                now_iso,
                state.get("halted")
            ))

            open_mints = list((state.get("open_positions") or {}).keys())
            if open_mints:
                placeholders = ",".join("?" * len(open_mints))
                cur.execute(f"DELETE FROM open_positions WHERE mint NOT IN ({placeholders})", open_mints)
            else:
                cur.execute("DELETE FROM open_positions")

            for mint, pos in (state.get("open_positions") or {}).items():
                cur.execute("""
                    INSERT OR REPLACE INTO open_positions (
                        mint, symbol, entry_price, entry_market_cap, current_market_cap,
                        size_usd, initial_size_usd, amount, initial_amount, stop_loss_mc,
                        take_profit_mc, trailing_active, trailing_stop_mc, peak_price,
                        peak_market_cap, realized_pnl_total, unrealized_pnl, opened_at,
                        max_holding_hours, stages_hit_json, signals_json, axis_scores_json,
                        features_json
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, (
                    mint,
                    pos.get("symbol", "UNKNOWN"),
                    float(pos.get("entry_price", 0.0)),
                    float(pos.get("entry_market_cap", 0.0)),
                    float(pos.get("current_market_cap") or pos.get("entry_market_cap") or 0.0),
                    float(pos.get("size_usd", 0.0)),
                    float(pos.get("initial_size_usd", pos.get("size_usd", 0.0))),
                    float(pos.get("amount", 0.0)),
                    float(pos.get("initial_amount", pos.get("amount", 0.0))),
                    float(pos.get("stop_loss_mc") or 0.0),
                    float(pos.get("take_profit_mc") or 0.0),
                    1 if pos.get("trailing_active") else 0,
                    float(pos.get("trailing_stop_mc") or 0.0) if pos.get("trailing_stop_mc") else None,
                    float(pos.get("peak_price") or pos.get("entry_price") or 0.0),
                    float(pos.get("peak_market_cap") or pos.get("entry_market_cap") or 0.0),
                    float(pos.get("realized_pnl_total", 0.0)),
                    float(pos.get("unrealized_pnl", 0.0)),
                    pos.get("opened_at") or now_iso,
                    float(pos.get("max_holding_hours", 48.0)),
                    json.dumps(pos.get("stages_hit", [])),
                    json.dumps(pos.get("signals", [])),
                    json.dumps(pos.get("axis_scores", {})),
                    json.dumps(pos.get("features", {}))
                ))

            # Full-state saves are resets: replace the closed-trade ledger with
            # the supplied one so a clean slate has no ghost entries from
            # previous sessions (observed in the 3-min test: a reset kept a
            # stale closed trade that no longer matched realized_pnl).
            cur.execute("DELETE FROM closed_trades")
            for rec in (state.get("closed_positions") or []):
                cur.execute("""
                    INSERT INTO closed_trades (
                        mint, symbol, entry_price, exit_price, entry_market_cap,
                        exit_market_cap, size_usd, pnl, pnl_pct, reason, opened_at,
                        closed_at, signals_json, axis_scores_json, features_json
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, (
                    rec.get("mint", "UNKNOWN"),
                    rec.get("symbol", "UNKNOWN"),
                    float(rec.get("entry_price", 0.0)),
                    float(rec.get("exit_price", 0.0)),
                    float(rec.get("entry_market_cap", 0.0)),
                    float(rec.get("exit_market_cap", 0.0)),
                    float(rec.get("size_usd", 0.0)),
                    float(rec.get("pnl", 0.0)),
                    float(rec.get("pnl_pct", 0.0)),
                    rec.get("reason", "CLOSED"),
                    rec.get("opened_at") or _now_iso(),
                    rec.get("closed_at") or _now_iso(),
                    json.dumps(rec.get("signals", [])),
                    json.dumps(rec.get("axis_scores", {})),
                    json.dumps(rec.get("features", {}))
                ))
    except Exception:
        pass

    _sync_json_cache()


def rl_acquire(key: str = "gmgn", tokens_needed: float = 1.0,
               rate_per_sec: float = 0.8, capacity: float = 2.5,
               min_gap_sec: float = 1.2, max_wait_sec: float = 45.0) -> bool:
    """Process-safe token bucket rate limiter.

    Uses a single atomic UPDATE (no SELECT-then-UPDATE race) and fails
    CLOSED on DB errors (previously `except: return True` granted tokens
    when the limiter was broken — a fail-open bug).
    """
    init_db()
    t_start = time.time()

    while True:
        now = time.time()
        try:
            with db_cursor(commit=True) as cur:
                row = cur.execute("SELECT * FROM rate_limiter WHERE key = ?", (key,)).fetchone()
                if not row:
                    cur.execute("""
                        INSERT INTO rate_limiter (key, tokens, last_updated, banned_until, rate_per_sec, capacity)
                        VALUES (?, ?, ?, 0.0, ?, ?)
                    """, (key, capacity - tokens_needed, now, rate_per_sec, capacity))
                    return True

                banned_until = float(row["banned_until"])
                if banned_until > now:
                    ban_wait = banned_until - now
                    if ban_wait > max_wait_sec:
                        return False
                    time.sleep(min(ban_wait + 0.5, 2.0))
                    continue

                # CONFIG WINS OVER THE STORED RATE. The row is inserted once with
                # whatever requests_per_sec was configured at the first call, and
                # the refill expression below reads the STORED column — so without
                # this sync, editing data_sources.gmgn.requests_per_sec (or the
                # burst capacity) had no effect at all until the DB row was
                # deleted. That is why the shipped 0.8/s looked "hardcoded".
                if (abs(float(row["rate_per_sec"] or 0) - float(rate_per_sec)) > 1e-9
                        or abs(float(row["capacity"] or 0) - float(capacity)) > 1e-9):
                    cur.execute("UPDATE rate_limiter SET rate_per_sec = ?, capacity = ? "
                                "WHERE key = ?", (rate_per_sec, capacity, key))

                time_since_last = now - float(row["last_updated"])
                if time_since_last < min_gap_sec:
                    time.sleep(min_gap_sec - time_since_last)
                    continue

                # Atomic token-bucket deduction in a single statement.
                cur.execute("""
                    UPDATE rate_limiter SET
                        tokens = min(capacity, tokens + (? - last_updated) * rate_per_sec) - ?,
                        last_updated = ?
                    WHERE key = ? AND banned_until <= ?
                      AND min(capacity, tokens + (? - last_updated) * rate_per_sec) >= ?
                """, (now, tokens_needed, now, key, now, now, tokens_needed))
                if cur.rowcount == 1:
                    return True
        except Exception:
            return False  # fail-closed: never grant on limiter failure

        sleep_time = tokens_needed / max(rate_per_sec, 0.01)
        if (time.time() - t_start) + sleep_time > max_wait_sec:
            return False
        time.sleep(min(sleep_time, 0.5))


def rl_report_ban(key: str = "gmgn", ban_duration_sec: float = 60.0):
    init_db()
    now = time.time()
    banned_until = now + ban_duration_sec
    try:
        with db_cursor(commit=True) as cur:
            cur.execute("""
                INSERT INTO rate_limiter (key, tokens, last_updated, banned_until, rate_per_sec, capacity)
                VALUES (?, 0.0, ?, ?, 0.8, 2.5)
                ON CONFLICT(key) DO UPDATE SET
                    banned_until = max(banned_until, ?),
                    tokens = 0.0,
                    last_updated = ?
            """, (key, now, banned_until, banned_until, now))
    except Exception:
        pass


def rl_clear_ban(key: str = "gmgn") -> bool:
    """Operator escape hatch: drop a registered ban immediately.

    `rl_report_ban` deliberately takes max(existing, new) - a fresh report must
    never SHORTEN a real ban. The consequence is that a ban can only end by
    expiring, so a wrongly parsed reset time (the old hardcoded-timezone bug
    claimed up to 6 h) left the bot blind with no sanctioned way back. This is
    that way back; `enzoctl unban --confirm` is the only caller.
    """
    init_db()
    try:
        with db_cursor(commit=True) as cur:
            cur.execute("UPDATE rate_limiter SET banned_until = 0.0 WHERE key = ?", (key,))
            return cur.rowcount >= 0
    except Exception:
        return False


def rl_get_ban_remaining(key: str = "gmgn") -> float:
    init_db()
    try:
        with db_cursor(commit=False) as cur:
            row = cur.execute("SELECT banned_until FROM rate_limiter WHERE key = ?", (key,)).fetchone()
            if not row:
                return 0.0
            remaining = float(row["banned_until"]) - time.time()
            return max(0.0, remaining)
    except Exception:
        return 0.0


def cache_get(key: str) -> tuple:
    """Fetch a cached value. Returns (value, age_seconds, ttl) or (None, None, None)."""
    init_db()
    now = time.time()
    try:
        with db_cursor(commit=False) as cur:
            row = cur.execute("SELECT value_json, ts, ttl FROM cache_store WHERE key = ?", (key,)).fetchone()
            if not row:
                return None, None, None
            age = now - float(row["ts"])
            ttl = float(row["ttl"])
            if age > ttl:
                return None, None, None
            try:
                return json.loads(row["value_json"]), age, ttl
            except Exception:
                return None, None, None
    except Exception:
        return None, None, None


def cache_set(key: str, value, ttl: float = 300.0):
    init_db()
    now = time.time()
    val_json = json.dumps(value)
    try:
        with db_cursor(commit=True) as cur:
            cur.execute("""
                INSERT INTO cache_store (key, value_json, ts, ttl)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(key) DO UPDATE SET
                    value_json = excluded.value_json,
                    ts = excluded.ts,
                    ttl = excluded.ttl
            """, (key, val_json, now, ttl))
    except Exception:
        pass


def cache_delete(key: str):
    init_db()
    try:
        with db_cursor(commit=True) as cur:
            cur.execute("DELETE FROM cache_store WHERE key = ?", (key,))
    except Exception:
        pass
