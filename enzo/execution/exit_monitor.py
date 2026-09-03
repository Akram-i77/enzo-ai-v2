#!/usr/bin/env python3
"""
ENZO - Unified Exit Monitor (Background Thread Service)
Continuously monitors open positions for Stop-Loss, Take-Profit, Trailing Stops, and Time Exits
with multi-tier price source priority (PumpDev -> GMGN -> Bonding Curve) and Stale Price Protection.
"""
import json
import os
import sys
import threading
import time
from datetime import datetime, timezone
from typing import Dict, Optional, Set, Tuple

from enzo.core.config import load_config
from enzo.execution import portfolio, executor
from enzo.providers import gmgn, pump
from enzo.ui import notify
import enzo.core.log as log
import enzo.core.audit as audit

_LOGGER = log.get_logger("enzo.exit_monitor")


class ExitMonitor:
    """Unified background exit monitor with stale price safety guards."""
    _instance = None
    _lock = threading.Lock()

    def __new__(cls, *args, **kwargs):
        with cls._lock:
            if cls._instance is None:
                cls._instance = super(ExitMonitor, cls).__new__(cls)
                cls._instance._initialized = False
            return cls._instance

    def __init__(self, poll_interval: float = 2.0):
        if self._initialized:
            return
        self.poll_interval = poll_interval
        self._stop_event = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._consecutive_failures: Dict[str, int] = {}
        self._warned_mints: Set[str] = set()
        self._initialized = True

    def start(self):
        with self._lock:
            if self._thread and self._thread.is_alive():
                return
            self._stop_event.clear()
            self._thread = threading.Thread(target=self._run_loop, daemon=True, name="enzo-exit-monitor")
            self._thread.start()
            _LOGGER.info("Exit monitor background service started.")

    def stop(self):
        self._stop_event.set()
        if self._thread:
            self._thread.join(timeout=4.0)
            _LOGGER.info("Exit monitor background service stopped.")

    def is_running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def fetch_price_with_metadata(self, mint: str, max_stale_age: float = 10.0) -> Optional[dict]:
        """Fetch market cap with source and timestamp verification (PumpDev -> GMGN -> Curve)."""
        now = time.time()

        # 1. Tier 1: PumpDev Real-Time WebSocket Stream
        try:
            client = pump.get_pumpdev_client()
            info = client.get_live_mcap_info(mint)
            if info:
                mc = float(info.get("market_cap") or 0.0)
                ts = float(info.get("timestamp") or now)
                age = now - ts
                if mc > 0 and age <= max_stale_age:
                    return {"market_cap": mc, "source": "pumpdev", "timestamp": ts, "age": age}
        except Exception:
            pass

        # 2. Tier 2: GMGN Live Market Data
        try:
            mc = gmgn.get_live_market_cap(mint)
            if mc and float(mc) > 0:
                return {"market_cap": float(mc), "source": "gmgn", "timestamp": now, "age": 0.0}
        except Exception:
            pass

        # 3. Tier 3: Bonding Curve Fallback
        try:
            c = gmgn.read_bonding_curve(mint)
            if c and c.get("market_cap") and float(c["market_cap"]) > 0:
                return {"market_cap": float(c["market_cap"]), "source": "bonding_curve", "timestamp": now, "age": 0.0}
        except Exception:
            pass

        return None

    def _run_loop(self):
        while not self._stop_event.is_set():
            try:
                cfg = load_config()
                em_cfg = cfg.get("exit_monitor", {}) or {}
                max_stale_age = float(em_cfg.get("max_stale_age_seconds", 10.0))
                failure_threshold = int(em_cfg.get("failure_threshold", 3))
                never_auto_sell = bool(em_cfg.get("never_auto_sell_on_failure", True))
                interval = float(em_cfg.get("interval_seconds", self.poll_interval))

                state = portfolio.load_state()
                open_pos = state.get("open_positions") or {}
                if open_pos:
                    valid_mcaps = {}
                    for mint in list(open_pos.keys()):
                        price_meta = self.fetch_price_with_metadata(mint, max_stale_age=max_stale_age)
                        if price_meta:
                            self._consecutive_failures[mint] = 0
                            self._warned_mints.discard(mint)
                            valid_mcaps[mint] = price_meta["market_cap"]
                        else:
                            self._consecutive_failures[mint] = self._consecutive_failures.get(mint, 0) + 1
                            fail_count = self._consecutive_failures[mint]
                            sym = open_pos[mint].get('symbol', mint[:8])
                            if fail_count >= failure_threshold:
                                _LOGGER.warning(
                                    f"Price unavailable/stale for {sym} ({mint[:8]}...) "
                                    f"({fail_count} consecutive failures). Holding position safely."
                                )
                                audit.log_event(
                                    category="PRICE", level="WARNING",
                                    message=f"Stale/unavailable price for {sym} ({fail_count}x) — holding, no auto-sell",
                                    data={"mint": mint, "failures": fail_count}
                                )
                                if mint not in self._warned_mints:
                                    self._warned_mints.add(mint)
                                    try:
                                        notify.notify_risk(
                                            "Stale Price Guard",
                                            f"{sym}: no fresh price for {fail_count} cycles. Position HELD "
                                            f"(never_auto_sell_on_failure={'true' if never_auto_sell else 'false'})."
                                        )
                                    except Exception:
                                        pass

                    if valid_mcaps:
                        # ── Capture position data BEFORE check_exits removes it from state ──
                        # check_exits calls atomic_close_position which removes pos from open_positions
                        pos_amounts = {mint: float(open_pos[mint].get("amount", 0)) for mint in list(open_pos.keys())}

                        closed, partials = portfolio.check_exits(valid_mcaps)

                        # ── LIVE TRADING: execute real sells on-chain via MoonPay CLI ──
                        paper = bool(cfg.get("paper_mode", True))
                        if not paper:
                            for c in closed:
                                mint = c.get("mint")
                                amount_spl = pos_amounts.get(mint, 0)
                                if amount_spl > 1e-9:
                                    sell_res = executor.sell_token(
                                        mint=mint,
                                        amount_spl=amount_spl,
                                        to_token="USDC",
                                        explanation=f"Enzo EXIT {c.get('reason')} {c.get('symbol', mint[:8])} PnL ${c.get('pnl', 0):+.2f}",
                                    )
                                    if sell_res.get("ok"):
                                        _LOGGER.info(f"✓ REAL SELL: {c.get('symbol')} tx={sell_res.get('tx_hash', 'pending')}")
                                        audit.log_event(
                                            category="TRADE", level="SELL",
                                            message=f"REAL SELL: {c.get('symbol')} tx={sell_res.get('tx_hash', 'pending')} PnL ${c.get('pnl', 0):+,.2f}",
                                            data={"mint": mint, "tx": sell_res.get("tx_hash"), "reason": c.get("reason")},
                                        )
                                    else:
                                        _LOGGER.error(f"✗ REAL SELL FAILED: {c.get('symbol')} — {sell_res.get('reason')}")
                                        audit.log_event(
                                            category="TRADE", level="ERROR",
                                            message=f"REAL SELL FAILED: {c.get('symbol')} — {sell_res.get('reason')}",
                                            data={"mint": mint, "sell_result": sell_res, "pnl": c.get("pnl")},
                                        )

                        for c in closed:
                            _LOGGER.info(f"Position closed: {c.get('symbol')} ({c.get('mint')[:8]}) | PnL: ${c.get('pnl', 0):+,.2f} ({c.get('pnl_pct', 0):+,.1f}%) | Reason: {c.get('reason')}")
                        for p in partials:
                            _LOGGER.info(f"Partial exit: {p.get('symbol')} | PnL: ${p.get('pnl', 0):+,.2f} | Reason: {p.get('reason')}")
            except Exception as e:
                _LOGGER.error(f"Error in exit monitor cycle: {e}")

            time.sleep(max(0.5, interval))


_MONITOR = None


def get_exit_monitor() -> ExitMonitor:
    global _MONITOR
    if _MONITOR is None:
        _MONITOR = ExitMonitor()
    return _MONITOR
