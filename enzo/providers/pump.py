#!/usr/bin/env python3
"""
ENZO - Pump.fun & PumpDev WebSocket Data Provider Layer
Handles real-time WebSocket token streaming, rapid card scanning, pre-filtering,
live trade price feeds, quote-aware market cap calculations, and batch subscriptions.
"""
import asyncio
import json
import os
import sys
import time
import threading
import collections
import urllib.request
import urllib.error
from typing import List, Dict, Any, Optional, Set

from enzo.core.config import load_config, load_secrets, RUN_DIR
from enzo.providers import gmgn
import enzo.core.log as log

_LOGGER = log.get_logger("enzo.pump")

PUMP_API_BASE = "https://frontend-api-v2.pump.fun"
PUMPDEV_DEFAULT_WS = "wss://pumpdev.io/ws?key=187M_-n4a8qkXK1wBcp5m5b5jfhf_Y1EFk81kiUPPQP1GcP21a7jWOSV3cJ7seaD"
PUMP_PROGRAM_ID = "6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P"
TOTAL_SUPPLY = 1_000_000_000

_PUMP_CACHE = {}
_PUMP_CACHE_TTL = 30.0


def _cache_get(key: str):
    e = _PUMP_CACHE.get(key)
    if e and time.time() < e[1]:
        return e[0]
    return None


def _cache_set(key: str, val: Any, ttl: float = _PUMP_CACHE_TTL):
    _PUMP_CACHE[key] = (val, time.time() + ttl)


def _http_get(url: str, timeout: float = 8.0) -> Optional[Any]:
    try:
        req = urllib.request.Request(url, headers={
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
            "Accept": "application/json",
        })
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            if resp.status == 200:
                return json.loads(resp.read().decode("utf-8"))
    except Exception as e:
        _LOGGER.debug(f"HTTP GET failed {url}: {e}")
    return None


def _to_fraction(v) -> float:
    """Normalize a percentage-or-fraction value into a 0-1 fraction.

    Values > 1.0 are treated as percentages (e.g. 30 -> 0.30); values <= 1.0
    are treated as fractions (e.g. 0.30 -> 0.30). Used to unify bundler_pct
    across the WS and HTTP metadata paths before comparing against the 0.30
    penalty threshold.
    """
    try:
        f = float(v)
        if f > 1.0:
            return f / 100.0
        return f
    except Exception:
        return 0.0


def _http_post(url: str, payload: dict, timeout: float = 8.0) -> Optional[Any]:
    try:
        body = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(url, data=body, headers={
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
            "Content-Type": "application/json",
            "Accept": "application/json",
        })
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            if resp.status == 200:
                return json.loads(resp.read().decode("utf-8"))
    except Exception as e:
        _LOGGER.debug(f"HTTP POST failed {url}: {e}")
    return None


# ================================================================ PumpDev Real-Time WebSocket Engine
class PumpDevStreamClient:
    """Persistent background WebSocket client streaming new Pump.fun launches and trades from pumpdev.io."""
    def __init__(self):
        self._stop_event = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._recent_tokens = collections.deque(maxlen=150)
        self._live_mcap: Dict[str, dict] = {}  # mint -> {"market_cap": float, "source": "pumpdev", "timestamp": float}
        self._subscribed_trades: Set[str] = set()
        self._pending_subscriptions: Set[str] = set()
        self._lock = threading.Lock()
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._ws = None
        # ── Observable connection state ─────────────────────────────────────
        # Without these the UI could only guess: /api/activity reported
        # "CONNECTING" forever, whether the socket was live, retrying, or dead
        # because the `websockets` package was never installed. That is how the
        # bot spent its whole life discovering 0 candidates with no visible cause.
        self._state = "IDLE"            # IDLE|CONNECTING|STREAMING|RETRYING|DOWN
        self._last_error: Optional[str] = None
        self._last_message_ts: float = 0.0
        self._connect_count = 0
        self._message_count = 0
        self._token_count = 0
        self._started_at: float = 0.0

    def status(self) -> dict:
        """True connection state for /api/activity, /health and enzoctl doctor."""
        now = time.time()
        alive = bool(self._thread and self._thread.is_alive())
        state = self._state if alive else ("DOWN" if self._state != "IDLE" else "NOT_STARTED")
        age = round(now - self._last_message_ts, 1) if self._last_message_ts else None
        stale = bool(age is not None and age > 90.0)
        return {
            "state": state,
            "thread_alive": alive,
            "ws_open": self._ws is not None,
            "buffered_tokens": len(self._recent_tokens),
            "tokens_seen": self._token_count,
            "messages": self._message_count,
            "connects": self._connect_count,
            "last_message_age_sec": age,
            "stale": stale,
            "last_error": self._last_error,
            "uptime_sec": round(now - self._started_at, 1) if self._started_at else None,
            "subscribed_trades": len(self._subscribed_trades),
        }

    def start(self):
        if self._thread and self._thread.is_alive():
            return
        self._stop_event.clear()
        self._started_at = time.time()
        self._state = "CONNECTING"
        self._last_error = None
        self._thread = threading.Thread(target=self._run_loop, daemon=True, name="enzo-pumpdev-ws")
        self._thread.start()
        _LOGGER.info("PumpDev WebSocket Streaming Client initialized.")

    def stop(self):
        self._stop_event.set()
        if self._thread:
            self._thread.join(timeout=2.0)

    def get_recent_tokens(self, limit: int = 40) -> List[dict]:
        with self._lock:
            return list(self._recent_tokens)[:limit]

    def get_live_mcap(self, mint: str) -> Optional[float]:
        with self._lock:
            item = self._live_mcap.get(mint)
            if item:
                return item.get("market_cap")
        return None

    def get_live_mcap_info(self, mint: str) -> Optional[dict]:
        with self._lock:
            return self._live_mcap.get(mint)

    def subscribe_trades(self, mints: List[str]):
        """Subscribe in batch to live token trades over WebSocket."""
        new_mints = [m for m in mints if m and m not in self._subscribed_trades]
        if not new_mints:
            return

        with self._lock:
            self._subscribed_trades.update(new_mints)
            self._pending_subscriptions.update(new_mints)

        if self._loop and self._loop.is_running() and self._ws:
            asyncio.run_coroutine_threadsafe(self._flush_subscriptions(), self._loop)

    async def _flush_subscriptions(self):
        with self._lock:
            to_send = list(self._pending_subscriptions)
            self._pending_subscriptions.clear()

        if to_send and self._ws:
            try:
                # Send in batch chunk of up to 50 keys per message
                for i in range(0, len(to_send), 50):
                    batch = to_send[i:i + 50]
                    await self._ws.send(json.dumps({"method": "subscribeTokenTrade", "keys": batch}))
                    _LOGGER.info(f"Subscribed to {len(batch)} token trades on PumpDev WebSocket.")
            except Exception as e:
                _LOGGER.debug(f"Failed sending subscribeTokenTrade batch: {e}")

    def _get_ws_url(self) -> str:
        sec = load_secrets()
        return sec.get("pumpdev_ws_url") or PUMPDEV_DEFAULT_WS

    def _run_loop(self):
        try:
            import websockets
        except ImportError:
            self._state = "DOWN"
            self._last_error = ("Python 'websockets' package not installed — the launch feed "
                                "cannot connect, so discovery yields 0 candidates")
            _LOGGER.error(self._last_error)
            _LOGGER.error("Fix: python3 -m pip install websockets   (or: bash bootstrap.sh)")
            return

        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)

        async def _connect_and_listen():
            backoff = 3.0
            while not self._stop_event.is_set():
                url = self._get_ws_url()
                try:
                    _LOGGER.info(f"Connecting to PumpDev WebSocket stream...")
                    self._state = "CONNECTING"
                    async with websockets.connect(url, ping_interval=20, ping_timeout=15) as ws:
                        self._ws = ws
                        self._state = "STREAMING"
                        self._connect_count += 1
                        self._last_error = None
                        backoff = 3.0
                        # 1. Subscribe to new token creations
                        await ws.send(json.dumps({"method": "subscribeNewToken"}))
                        _LOGGER.info("[✓] Subscribed to PumpDev 'subscribeNewToken' stream.")

                        # 2. Resubscribe to all active trades on connect
                        with self._lock:
                            subs = list(self._subscribed_trades)
                        if subs:
                            for i in range(0, len(subs), 50):
                                batch = subs[i:i + 50]
                                await ws.send(json.dumps({"method": "subscribeTokenTrade", "keys": batch}))
                            _LOGGER.info(f"[✓] Resubscribed {len(subs)} token trades on PumpDev.")

                        while not self._stop_event.is_set():
                            try:
                                msg = await asyncio.wait_for(ws.recv(), timeout=25.0)
                                self._handle_ws_message(msg)
                            except asyncio.TimeoutError:
                                await ws.ping()
                except Exception as e:
                    self._state = "RETRYING"
                    self._last_error = f"{type(e).__name__}: {e}"
                    # Exponential backoff, capped at 60s. A fixed 3s retry wrote
                    # ~28,800 identical log lines per day whenever the endpoint
                    # was unreachable, burying every other message in the log.
                    _LOGGER.warning("PumpDev WebSocket disconnected (%s%s). Retrying in %.0fs...",
                                    type(e).__name__,
                                    f": {e}" if str(e) else " (no detail)",
                                    backoff)
                    self._ws = None
                    waited = 0.0
                    while waited < backoff and not self._stop_event.is_set():
                        await asyncio.sleep(0.5)
                        waited += 0.5
                    backoff = min(60.0, backoff * 1.7)

        try:
            self._loop.run_until_complete(_connect_and_listen())
        except Exception as e:
            self._last_error = f"{type(e).__name__}: {e}"
            _LOGGER.warning(f"PumpDev loop terminated: {e}")
        finally:
            if not self._stop_event.is_set():
                self._state = "DOWN"
            self._loop.close()

    def _resolve_market_cap_usd(self, event: dict) -> Optional[float]:
        """Quote-aware and dynamic SOL/USD market cap resolver."""
        # 1. Direct USD/Quote Market Cap if provided by event
        if event.get("usd_market_cap") is not None:
            return float(event["usd_market_cap"])
        if event.get("marketCapQuote") is not None and str(event.get("quoteMint", "")).upper() in ("USD", "USDC", "USDT"):
            return float(event["marketCapQuote"])

        # 2. Dynamic SOL Market Cap conversion using live SOL price
        mcap_sol = event.get("marketCapSol") or event.get("vSolInBondingCurve")
        if mcap_sol is not None:
            sol_price = gmgn.sol_price_usd() or 180.0
            return float(mcap_sol) * sol_price

        # 3. Fallback to raw marketCap if numeric
        raw_mc = event.get("marketCap")
        if raw_mc is not None and float(raw_mc) > 0:
            return float(raw_mc)

        return None

    def _handle_ws_message(self, raw_msg: str):
        try:
            self._message_count += 1
            self._last_message_ts = time.time()
            if self._state != "STREAMING":
                self._state = "STREAMING"
            event = json.loads(raw_msg)
            if not isinstance(event, dict):
                return

            tx_type = event.get("txType")
            mint = event.get("mint")
            now = time.time()

            # 1. Handle New Token Creation
            if tx_type == "create" and mint:
                self._token_count += 1
                mcap_usd = self._resolve_market_cap_usd(event) or 0.0
                mcap_sol = float(event.get("marketCapSol") or event.get("vSolInBondingCurve") or 0.0)

                card = {
                    "coinMint": mint,
                    "mint": mint,
                    "ticker": event.get("symbol") or event.get("ticker") or "PUMP",
                    "name": event.get("name") or "",
                    "marketCap": mcap_usd,
                    "marketCapSol": mcap_sol,
                    "creator": event.get("traderPublicKey"),
                    "solAmount": float(event.get("solAmount") or 0.0),
                    "created_timestamp": int(now),
                    "source": "pumpdev_ws",
                    "devHoldingsPercentage": float(event.get("devHoldingsPercentage") or 5.0),
                    "sniperOwnedPercentage": float(event.get("sniperOwnedPercentage") or 0.0),
                    "topHoldersPercentage": float(event.get("topHoldersPercentage") or 15.0),
                    "pumpdev_deep": {
                        "bundler_pct": _to_fraction(event.get("bundlerHoldRate") or 0.0),
                        "twitter_reuse": int(event.get("twitterReuseCount") or 0),
                        "telegram_reuse": int(event.get("telegramReuseCount") or 0),
                        "website_reuse": int(event.get("websiteReuseCount") or 0),
                        "is_banned": bool(event.get("isBanned")),
                        "sniper_count": int(event.get("sniperCount") or 0),
                    },
                    "raw": event
                }

                with self._lock:
                    self._recent_tokens.appendleft(card)
                    if mcap_usd > 0:
                        self._live_mcap[mint] = {"market_cap": mcap_usd, "source": "pumpdev", "timestamp": now}

                _LOGGER.debug(f"PumpDev New Token: {card['ticker']} ({mint[:8]}...) MC: ${mcap_usd:,.0f}")

            # 2. Handle Live Trades
            elif tx_type in ("buy", "sell") and mint:
                mcap_usd = self._resolve_market_cap_usd(event)
                if mcap_usd and mcap_usd > 0:
                    with self._lock:
                        self._live_mcap[mint] = {"market_cap": mcap_usd, "source": "pumpdev", "timestamp": now}

        except Exception as e:
            _LOGGER.debug(f"Error handling PumpDev WS message: {e}")


_PUMPDEV_CLIENT = None


def get_pumpdev_client() -> PumpDevStreamClient:
    global _PUMPDEV_CLIENT
    if _PUMPDEV_CLIENT is None:
        _PUMPDEV_CLIENT = PumpDevStreamClient()
        _PUMPDEV_CLIENT.start()
    return _PUMPDEV_CLIENT


FEED_STATUS_PATH = os.path.join(RUN_DIR, "enzo-feed.json")


def publish_status() -> None:
    """Owner-side: snapshot this process's feed state for the status pages.

    Only the process that OWNS the WebSocket may call get_pumpdev_client(), so
    the dashboard cannot ask it directly. The engine publishes its view here
    each cycle and serve.py reads the file - one connection per IP, still a
    single source of truth.
    """
    client = _PUMPDEV_CLIENT
    if client is None:
        return
    try:
        st = dict(client.status() or {})
        st["ts"] = time.time()
        st["owner_pid"] = os.getpid()
        tmp = FEED_STATUS_PATH + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(st, f)
        os.replace(tmp, FEED_STATUS_PATH)
    except Exception:
        pass


def read_published_status(max_age_sec: float = 180.0) -> dict:
    """Consumer-side: the engine's last published feed state, or {} if absent."""
    try:
        with open(FEED_STATUS_PATH, "r", encoding="utf-8") as f:
            d = json.load(f) or {}
        if time.time() - float(d.get("ts") or 0.0) > max_age_sec:
            return {}
        return d
    except Exception:
        return {}


def peek_pumpdev_client() -> PumpDevStreamClient | None:
    """Return the already-running client WITHOUT creating one.

    pump.dev rate-limits per IP ("Too many connections"), so exactly ONE process
    may own the WebSocket - the engine. The dashboard server used to call
    get_pumpdev_client() while building /health, which opened a SECOND connection
    from the same IP, got the IP limited, and starved the engine's feed: prices
    went stale everywhere while two processes both believed they were helping.
    Status pages must peek, never create.
    """
    return _PUMPDEV_CLIENT


# ================================================================ endpoints
def get_recent_creations(limit: int = 40, offset: int = 0) -> List[dict]:
    """Retrieve freshly launched memecoins via PumpDev WebSocket stream with multi-source fallback."""
    ckey = f"creations:{limit}:{offset}"
    cached = _cache_get(ckey)
    if cached is not None:
        return cached

    # Tier 1: Real-Time PumpDev WebSocket Stream
    client = get_pumpdev_client()
    stream_tokens = client.get_recent_tokens(limit=limit)
    if stream_tokens and len(stream_tokens) >= 5:
        _cache_set(ckey, stream_tokens, 10.0)
        return stream_tokens

    # Tier 2: Frontend API v2
    url = f"{PUMP_API_BASE}/coins/list?limit={limit}&offset={offset}&sort=created_timestamp&order=DESC"
    res = _http_get(url)
    if isinstance(res, list) and len(res) > 0:
        _cache_set(ckey, res, 20.0)
        return res

    # Tier 3: PumpPortal Token List Fallback
    try:
        url_portal = "https://pumpportal.fun/api/data/token-list"
        portal_res = _http_get(url_portal, timeout=5.0)
        if isinstance(portal_res, list) and len(portal_res) > 0:
            formatted = []
            for item in portal_res[:limit]:
                formatted.append({
                    "coinMint": item.get("mint") or item.get("address"),
                    "mint": item.get("mint") or item.get("address"),
                    "ticker": item.get("symbol") or item.get("ticker") or "PUMP",
                    "name": item.get("name") or "",
                    "marketCap": float(item.get("marketCap") or item.get("usd_market_cap") or 0.0),
                    "devHoldingsPercentage": float(item.get("devHoldingsPercentage") or 0.0),
                    "sniperOwnedPercentage": float(item.get("sniperOwnedPercentage") or 0.0),
                    "topHoldersPercentage": float(item.get("topHoldersPercentage") or 0.0),
                    "source": "pumpportal",
                    "raw": item
                })
            _cache_set(ckey, formatted, 20.0)
            return formatted
    except Exception:
        pass

    # Tier 4: DexScreener Latest Solana Profiles Fallback
    # NOTE: never fabricate market cap/ticker for profile-only items — they are
    # returned with marketCap=0 so the pre-screen rejects them (min mcap gate).
    try:
        url_dex = "https://api.dexscreener.com/token-profiles/latest/v1"
        dex_res = _http_get(url_dex, timeout=5.0)
        if isinstance(dex_res, list) and len(dex_res) > 0:
            formatted = []
            for item in dex_res:
                if item.get("chainId") == "solana":
                    token_addr = item.get("tokenAddress")
                    formatted.append({
                        "coinMint": token_addr,
                        "mint": token_addr,
                        "ticker": item.get("tokenSymbol") or "UNKNOWN",
                        "name": item.get("description", "")[:20],
                        "marketCap": 0.0,  # unknown — do not invent a quote
                        "verified": False,
                        "devHoldingsPercentage": 0.0,
                        "topHoldersPercentage": 0.0,
                        "source": "dexscreener",
                        "raw": item
                    })
            if formatted:
                _cache_set(ckey, formatted[:limit], 20.0)
                return formatted[:limit]
    except Exception:
        pass

    # Return any stream tokens collected so far even if small
    if stream_tokens:
        return stream_tokens

    return []


def get_graduated_tokens(limit: int = 40, offset: int = 0) -> List[dict]:
    ckey = f"graduated:{limit}:{offset}"
    cached = _cache_get(ckey)
    if cached is not None:
        return cached

    url = f"{PUMP_API_BASE}/coins/graduated?limit={limit}&offset={offset}&sort=graduation_date&order=DESC"
    res = _http_get(url) or []
    if isinstance(res, list):
        _cache_set(ckey, res, 30.0)
        return res
    return []


def get_kol_tokens(limit: int = 40, offset: int = 0) -> List[dict]:
    ckey = f"kol:{limit}:{offset}"
    cached = _cache_get(ckey)
    if cached is not None:
        return cached

    url = f"{PUMP_API_BASE}/coins/kolscan?limit={limit}&offset={offset}"
    res = _http_get(url) or []
    if isinstance(res, list):
        _cache_set(ckey, res, 30.0)
        return res
    return []


def get_batch_metadata(mints: List[str]) -> List[dict]:
    if not mints:
        return []
    url = f"{PUMP_API_BASE}/coins/metadatas"
    res = _http_post(url, {"mints": mints[:50]}) or []
    return res if isinstance(res, list) else []


def get_deep_metadata(mint: str) -> dict:
    ckey = f"deep:{mint}"
    cached = _cache_get(ckey)
    if cached is not None:
        return cached

    url = f"{PUMP_API_BASE}/coins/metadata/{mint}"
    res = _http_get(url) or {}
    if isinstance(res, dict):
        _cache_set(ckey, res, 60.0)
        return res
    return {}


# ================================================================ screening & normalization
def screen_pump_card(card: dict, config: dict = None) -> dict:
    """Rapid pre-filtering of raw pump coin card before running deep analysis.

    Thresholds are read from config (data_sources.pumpdev.thresholds) instead
    of hard-coded 35/40/80 values.
    """
    cfg = config or load_config()
    ma = cfg.get("market_analysis", {}) or {}
    th = ((cfg.get("data_sources", {}) or {}).get("pumpdev", {}) or {}).get("thresholds", {}) or {}
    reasons = []

    mcap = float(card.get("marketCap") or card.get("usd_market_cap") or 0.0)
    min_mc = float(ma.get("min_market_cap", th.get("min_mcap", 1000)))
    if min_mc and mcap < min_mc:
        reasons.append(f"mcap ${mcap:,.0f} < ${min_mc:,.0f}")

    dev_pct = float(card.get("devHoldingsPercentage") or 0.0)
    dev_max = float(th.get("dev_hold_hard_pct", 35))
    if dev_pct > dev_max:
        reasons.append(f"dev holding {dev_pct:.1f}% > {dev_max:.0f}%")

    sniper_pct = float(card.get("sniperOwnedPercentage") or 0.0)
    sniper_max = float(th.get("sniper_owned_hard_pct", 40))
    if sniper_pct > sniper_max:
        reasons.append(f"sniper share {sniper_pct:.1f}% > {sniper_max:.0f}%")

    top10_pct = float(card.get("topHoldersPercentage") or 0.0)
    top10_max = float(th.get("top10_hard_pct", 90))
    if top10_pct > top10_max:
        reasons.append(f"top10 concentration {top10_pct:.1f}% > {top10_max:.0f}%")

    tw_reuse = int(card.get("twitterReuseCount") or (card.get("pumpdev_deep") or {}).get("twitter_reuse") or 0)
    tw_max = int(th.get("max_twitter_reuse", 5))
    if tw_reuse >= tw_max:
        reasons.append(f"serial dev twitter reuse count={tw_reuse}")

    return {
        "pass": len(reasons) == 0,
        "mint": card.get("coinMint") or card.get("mint"),
        "symbol": card.get("ticker") or card.get("symbol") or "UNKNOWN",
        "market_cap": mcap,
        "dev_pct": dev_pct,
        "top10_pct": top10_pct,
        "reasons": reasons
    }


def enrich_survivor(card: dict) -> dict:
    """Enrich card with deep risk fields from PumpDev or metadata endpoints."""
    mint = card.get("coinMint") or card.get("mint")
    if not mint:
        return card

    if "pumpdev_deep" in card and card["pumpdev_deep"]:
        return card

    deep = get_deep_metadata(mint)
    if not deep:
        return card

    card["pumpdev_deep"] = {
        "bundler_pct": _to_fraction(deep.get("bundler_owned_percentage_v2") or 0.0),
        "twitter_reuse": int(deep.get("twitter_reuse_count") or 0),
        "telegram_reuse": int(deep.get("telegram_reuse_count") or 0),
        "website_reuse": int(deep.get("website_reuse_count") or 0),
        "is_banned": bool(deep.get("is_banned")),
        "ath_market_cap": float(deep.get("ath_market_cap") or 0.0),
        "sniper_count": int(deep.get("sniper_count") or 0),
    }
    return card
