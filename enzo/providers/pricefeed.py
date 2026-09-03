#!/usr/bin/env python3
"""
ENZO - Price Feed Provider
Manages threaded background price polling for active positions via GMGN provider.
"""
import threading
import time
from typing import Dict, Optional, Set

from enzo.providers import gmgn
import enzo.core.log as log

_LOGGER = log.get_logger("enzo.pricefeed")


class PriceFeed:
    """Threaded background price poller for open positions."""
    def __init__(self, fresh_secs: float = 5.0):
        self.fresh_secs = fresh_secs
        self._cache: Dict[str, tuple] = {}  # mint -> (price_usd, mcap_usd, ts)
        self._want: Set[str] = set()
        self._lock = threading.Lock()
        self._thread: Optional[threading.Thread] = None
        self._stop = threading.Event()

    def start(self):
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, daemon=True, name="enzo-pricefeed")
        self._thread.start()

    def stop(self):
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=3.0)

    def subscribe(self, mint: str):
        with self._lock:
            self._want.add(mint)
        try:
            from enzo.providers import pump
            pump.get_pumpdev_client().subscribe_trades([mint])
        except Exception:
            pass

    def unsubscribe(self, mint: str):
        with self._lock:
            self._want.discard(mint)
            self._cache.pop(mint, None)

    def subscribed(self) -> Set[str]:
        with self._lock:
            return set(self._want)

    def get_price(self, mint: str) -> Optional[float]:
        with self._lock:
            e = self._cache.get(mint)
            if e:
                return e[0]
        # On-demand fallback
        md = gmgn.get_market_data(mint)
        p = md.get("price_usd")
        if p:
            mc = md.get("signals", {}).get("market_cap_usd")
            with self._lock:
                self._cache[mint] = (p, mc, time.time())
        return p

    def get_market_cap(self, mint: str) -> Optional[float]:
        # Fast path 1: PumpDev live WebSocket stream
        try:
            from enzo.providers import pump
            live_mc = pump.get_pumpdev_client().get_live_mcap(mint)
            if live_mc:
                return live_mc
        except Exception:
            pass

        # Fast path 2: Local cache
        with self._lock:
            e = self._cache.get(mint)
            if e and e[1]:
                return e[1]
        md = gmgn.get_market_data(mint)
        mc = md.get("signals", {}).get("market_cap_usd")
        if mc:
            p = md.get("price_usd")
            with self._lock:
                self._cache[mint] = (p, mc, time.time())
        return mc

    def _run(self):
        while not self._stop.is_set():
            with self._lock:
                mints = list(self._want)

            for mint in mints:
                if self._stop.is_set():
                    break
                try:
                    md = gmgn.get_market_data(mint)
                    p = md.get("price_usd")
                    mc = md.get("signals", {}).get("market_cap_usd")
                    if p or mc:
                        with self._lock:
                            self._cache[mint] = (p, mc, time.time())
                except Exception as e:
                    _LOGGER.debug(f"Price update error for {mint}: {e}")
                time.sleep(self.fresh_secs / max(len(mints), 1))

            time.sleep(1.0)
