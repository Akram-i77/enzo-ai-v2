#!/usr/bin/env python3
"""
ENZO - Unified Data Layer (GMGN Provider)
Handles GMGN endpoints, rate pacing, ban recovery, and standard normalization shapes.
"""
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
from datetime import datetime, timezone
from typing import Optional, Dict, Any, List

from enzo.core.config import load_config, GMGN_BAN_FILE_PATH
import enzo.core.db as db
import enzo.core.log as log

_LOGGER = log.get_logger("enzo.gmgn")

ROOT_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# ================================================================ cache
_CACHE = {}
_CACHE_TTL = {
    "market_data": 10,   # live price/mcap/liq/vol
    "info": 30,
    "security": 300,     # 5 min — security rarely changes
    "holders": 300,      # 5 min — holder distribution
    "curve": 10,         # bonding-curve phase/progress
    "sol_price": 60,     # SOL price
    "discovery": 25,     # list endpoints
    "kline": 120,        # 2 min — OHLCV candles
}
_CACHE_STATS = {"hit": 0, "miss": 0}
_CACHE_LOCK = threading.Lock()


# ================================================================ error surface
# Every read helper below returns an empty payload on failure so a single dead
# endpoint cannot stop the scan loop. That is fine for control flow, but it made
# "GMGN_API_KEY is not set" and "this gmgn-cli has no --address flag" look
# EXACTLY like "GMGN answered and there was nothing to buy". Each swallowed
# exception is therefore recorded here and logged at WARNING (throttled), and
# `enzoctl doctor` / `enzoctl probe` read it back.
_PROVIDER_STATUS = {"last_error": None, "last_error_endpoint": None, "last_error_ts": 0.0,
                    "error_count": 0, "api_key_missing": False}
_ERR_LOG_THROTTLE = {}


def provider_status() -> dict:
    """Last GMGN failure seen by the provider layer (None = all clear)."""
    out = dict(_PROVIDER_STATUS)
    out["age_sec"] = round(time.time() - out["last_error_ts"], 1) if out["last_error_ts"] else None
    out["api_key_present"] = bool(os.environ.get("GMGN_API_KEY") or _api_key_file())
    # Honest in BOTH directions and before any call has been made. Previously
    # this flag only flipped after a CLI call died on the missing key, so a
    # freshly started bot reported api_key_present=False AND
    # api_key_missing=False - a contradiction every consumer (dashboard banner,
    # /api/state, enzoctl doctor) would then have to re-derive on its own.
    out["api_key_missing"] = bool(out.get("api_key_missing")) or not out["api_key_present"]
    out["addr_dialect"] = dict(_ADDR_DIALECT)
    return out


def reset_provider_status() -> None:
    _PROVIDER_STATUS.update({"last_error": None, "last_error_endpoint": None,
                             "last_error_ts": 0.0, "error_count": 0,
                             "api_key_missing": False})
    _ERR_LOG_THROTTLE.clear()


def _note_error(endpoint, exc):
    msg = f"{type(exc).__name__}: {exc}"
    _PROVIDER_STATUS.update({
        "last_error": msg[:400], "last_error_endpoint": endpoint,
        "last_error_ts": time.time(),
        "error_count": int(_PROVIDER_STATUS.get("error_count") or 0) + 1,
    })
    if "GMGN_API_KEY" in msg:
        _PROVIDER_STATUS["api_key_missing"] = True
    tkey = (endpoint, msg[:120])
    if time.time() - (_ERR_LOG_THROTTLE.get(tkey) or 0) >= 30.0:
        _ERR_LOG_THROTTLE[tkey] = time.time()
        _LOGGER.warning("GMGN %s failed — %s", endpoint, msg[:300])


def get_cache_stats() -> dict:
    with _CACHE_LOCK:
        h, m = _CACHE_STATS["hit"], _CACHE_STATS["miss"]
        tot = h + m
        pct = round(h / tot * 100, 1) if tot else 0.0
        return {"hit": h, "miss": m, "total": tot, "hit_pct": pct}


def _cache_get(key):
    with _CACHE_LOCK:
        e = _CACHE.get(key)
        if not e:
            _CACHE_STATS["miss"] += 1
            return None
        val, exp = e
        if time.time() < exp:
            _CACHE_STATS["hit"] += 1
            return val
        _CACHE.pop(key, None)
        _CACHE_STATS["miss"] += 1
        return None


def _cache_set(key, val, ttl=None):
    if ttl is None:
        cat = key.split(":", 1)[0]
        ttl = _CACHE_TTL.get(cat, 30)
    with _CACHE_LOCK:
        _CACHE[key] = (val, time.time() + ttl)


# ================================================================ rate limiter
class GMGNError(Exception):
    pass


class RateLimited(GMGNError):
    pass


_RL_LOCK = threading.Lock()
_RL_LAST_CALL = 0.0
_RL_MIN_GAP = 1.2  # fallback only; config data_sources.gmgn.request_gap_ms overrides
_RL_GRACE = 2.0
_BAN_RE = re.compile(r"resets at\s+([0-9:\-T ]+)", re.IGNORECASE)


def _rl_min_gap() -> float:
    """Read the configured request gap (ms) and expose it as seconds.

    The floor is 20ms (50 calls/s) rather than 100ms: the gap is an operator-set
    value, and a 100ms floor silently overrode any smaller setting - which is how
    a test sandbox asking for 1ms ended up pacing every call at 100ms and taking
    three minutes. The floor still protects against `request_gap_ms: 0`.
    """
    try:
        cfg = load_config()
        ms = float(((cfg.get("data_sources", {}) or {}).get("gmgn", {}) or {}).get("request_gap_ms", 350))
        return max(0.02, ms / 1000.0)
    except Exception:
        return _RL_MIN_GAP


def ban_status() -> float:
    """Public helper: remaining ban seconds (0 = not banned)."""
    return db.rl_get_ban_remaining("gmgn")


def _ban_wait_seconds(msg):
    m = _BAN_RE.search(msg)
    if not m:
        return 30.0
    try:
        fmt = "%Y-%m-%d %H:%M:%S" if " " in m.group(1) else "%Y-%m-%dT%H:%M:%S"
        reset = datetime.strptime(m.group(1), fmt)
        import zoneinfo
        reset = reset.replace(tzinfo=zoneinfo.ZoneInfo("Africa/Lagos"))
        wait = (reset - datetime.now(zoneinfo.ZoneInfo("Africa/Lagos"))).total_seconds()
        return max(1.0, wait)
    except Exception:
        return 30.0


_GMGN_BIN_CACHE = {"bin": None, "resolved": False}


def resolve_gmgn_bin(cfg: dict = None) -> Optional[str]:
    """Locate the gmgn-cli binary.

    Resolution order:
      1. data_sources.gmgn.cli  (absolute path OR a bare name)
      2. ENZO_GMGN_BIN environment variable
      3. a bare `gmgn-cli` lookup on PATH
      4. common install locations (npm global, ~/.local/bin, nvm)

    The command used to be hardcoded as ["gmgn-cli", ...], which ignored the
    `cli:` key the config file already documented — so an operator who installed
    it somewhere off PATH had no way to point the bot at it, and every discovery
    call failed with FileNotFoundError.
    """
    if _GMGN_BIN_CACHE["resolved"]:
        return _GMGN_BIN_CACHE["bin"]

    try:
        cfg = cfg or load_config()
    except Exception:
        cfg = {}
    gmgn_cfg = (cfg.get("data_sources") or {}).get("gmgn") or {}
    configured = str(gmgn_cfg.get("cli") or "").strip()

    candidates = []
    for cand in (configured, str(os.environ.get("ENZO_GMGN_BIN") or "").strip(),
                 "gmgn-cli", "gmgn"):
        if cand and cand not in candidates:
            candidates.append(cand)

    found = None
    for cand in candidates:
        if os.path.isabs(cand) or os.path.sep in cand:
            if os.path.exists(cand) and os.access(cand, os.X_OK):
                found = cand
                break
            continue
        which = shutil.which(cand)
        if which:
            found = which
            break

    if not found:
        extra_dirs = [
            os.path.expanduser("~/.npm-global/bin"),
            os.path.expanduser("~/.local/bin"),
            "/usr/local/bin",
        ]
        home = os.path.expanduser("~")
        nvm_root = os.path.join(home, ".nvm", "versions", "node")
        if os.path.isdir(nvm_root):
            try:
                for ver in sorted(os.listdir(nvm_root), reverse=True):
                    extra_dirs.append(os.path.join(nvm_root, ver, "bin"))
            except Exception:
                pass
        for d in extra_dirs:
            for name in ("gmgn-cli", "gmgn"):
                pth = os.path.join(d, name)
                if os.path.exists(pth) and os.access(pth, os.X_OK):
                    found = pth
                    break
            if found:
                break

    _GMGN_BIN_CACHE.update({"bin": found, "resolved": True})
    if found:
        _LOGGER.info("GMGN CLI resolved: %s", found)
    else:
        _LOGGER.error(
            "gmgn-cli not found (looked for %s). GMGN discovery and market data "
            "will fail for EVERY token. Install it, or set data_sources.gmgn.cli "
            "in config/enzo-config.yaml to its full path.",
            ", ".join(candidates[:4]),
        )
    return found


def _api_key_file():
    """The CLI reads ~/.config/gmgn/.env first, then a project .env."""
    for path in (os.path.join(os.path.expanduser("~"), ".config", "gmgn", ".env"),
                 os.path.join(ROOT_DIR, ".env"), os.path.join(os.getcwd(), ".env")):
        try:
            if os.path.exists(path) and "GMGN_API_KEY=" in open(path, encoding="utf-8",
                                                                 errors="replace").read():
                return path
        except Exception:
            continue
    return None


# Which address flag this gmgn-cli build accepts, learned per endpoint at runtime:
# v1.6.x requires --address and errors on --token, while the builds this provider
# was written against accepted --token. Guessing wrong means every holders/kline
# call fails, so the first error teaches the process and the answer is reused.
_ADDR_DIALECT = {}


def _run_addr(base_args, endpoint, mint, extra=()):
    """Run a token-scoped command, discovering the accepted address flag."""
    remembered = _ADDR_DIALECT.get(endpoint)
    tried = []
    for flag in [f for f in (remembered, "--address", "--token") if f]:
        if flag in tried:
            continue
        tried.append(flag)
        try:
            res = _run(list(base_args) + [flag, str(mint)] + list(extra), endpoint)
            _ADDR_DIALECT[endpoint] = flag
            return res
        except GMGNError as e:
            msg = str(e)
            if "unknown option" in msg or "required option" in msg:
                _LOGGER.warning("%s: gmgn-cli rejected %s (%s) — trying the other dialect",
                                endpoint, flag, msg.split("err=")[-1][:110])
                continue
            raise
    raise GMGNError(f"{endpoint}: gmgn-cli accepted neither --address nor --token "
                    f"(tried {', '.join(tried)}) — check the installed version")


def _burst_capacity() -> float:
    """Token-bucket burst size (data_sources.gmgn.burst_capacity, default 2.5).

    A bucket of 2.5 lets the bot fire discovery + a couple of deep calls back to
    back and then settle onto the steady rate. It used to be a hardcoded default
    inside db.rl_acquire, i.e. not configurable at all.
    """
    try:
        cfg = load_config()
        g = (cfg.get("data_sources", {}) or {}).get("gmgn", {}) or {}
        return max(1.0, float(g.get("burst_capacity", 2.5)))
    except Exception:
        return 2.5


def _rate_per_sec() -> float:
    """Sustained request rate for the GMGN token bucket (config-driven).

    It used to be a hardcoded 0.8/s inside _run, which made the pacing
    untestable and unfixable from config: `request_gap_ms` only set the minimum
    gap between two calls, so the effective ceiling stayed 48 req/min whatever
    the owner wrote.
    """
    try:
        cfg = load_config()
        return float(((cfg.get("data_sources", {}) or {}).get("gmgn", {}) or {})
                     .get("requests_per_sec", 0.8))
    except Exception:
        return 0.8


def _run(args, endpoint, timeout=40):
    """Run one gmgn-cli command; measure latency; handle bans with sleep+retry."""
    acquired = db.rl_acquire("gmgn", tokens_needed=1.0, rate_per_sec=_rate_per_sec(),
                             capacity=_burst_capacity(), min_gap_sec=_rl_min_gap(),
                             max_wait_sec=45.0)
    if not acquired:
        raise RateLimited(f"{endpoint}: rate limited or banned")

    bin_path = resolve_gmgn_bin()
    if not bin_path:
        raise GMGNError(
            f"{endpoint}: gmgn-cli not found — set data_sources.gmgn.cli in "
            f"config/enzo-config.yaml or install it on PATH")
    # --raw asks for compact JSON. Both modes are JSON in v1.6 (pretty without
    # the flag), so this is only cheaper to parse - but a missing GMGN_API_KEY
    # kills every call before any request, and that message must reach the logs
    # instead of being mistaken for an empty market.
    if "--raw" not in args and "--help" not in args and "-h" not in args:
        args = list(args) + ["--raw"]
    if not os.environ.get("GMGN_API_KEY") and not _api_key_file():
        raise GMGNError(
            f"{endpoint}: GMGN_API_KEY is not set — gmgn-cli refuses every "
            f"request without it (export GMGN_API_KEY, or write "
            f"~/.config/gmgn/.env). Discovery and market data return nothing.")
    cmd = [bin_path] + args
    t0 = time.time()
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, env={**os.environ})
        ms = (time.time() - t0) * 1000
        out = (proc.stdout or "").strip()
        err = (proc.stderr or "").strip()
        if proc.returncode != 0:
            if "429" in out or "RATE_LIMIT" in out.upper() or "RATE_LIMIT" in err.upper():
                if "BANNED" in out.upper() or "BANNED" in err.upper():
                    wait = _ban_wait_seconds(out + err)
                    db.rl_report_ban("gmgn", ban_duration_sec=wait)
                    _LOGGER.warning(f"GMGN ban active until reset ({wait:.0f}s) — sleeping")
                    if wait > 0:
                        if db.rl_acquire("gmgn", tokens_needed=1.0, max_wait_sec=wait + 5.0):
                            proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, env={**os.environ})
                            out = (proc.stdout or "").strip()
                            err = (proc.stderr or "").strip()
                            if proc.returncode != 0:
                                raise RateLimited(f"{endpoint}: still banned")
                else:
                    raise RateLimited(f"{endpoint}: rate limited")
            else:
                raise GMGNError(f"{endpoint}: rc={proc.returncode} err={err[:200]}")

        raw = out or "{}"
        try:
            return json.loads(raw)
        except Exception:
            raise GMGNError(f"{endpoint}: non-json output: {raw[:200]}")
    except subprocess.TimeoutExpired:
        raise GMGNError(f"{endpoint}: timeout ({timeout}s)")


# ================================================================ normalization helpers
def fnum(v, default=None):
    if v is None or v == "":
        return default
    try:
        return float(v)
    except Exception:
        return default


def _norm_pct(v):
    if v is None or v == "":
        return None
    try:
        f = float(v)
        return f / 100.0 if f > 1.0 else f
    except Exception:
        return None


def _get(d, *keys, default=None):
    if not isinstance(d, dict):
        return default
    for k in keys:
        if k in d and d[k] is not None:
            return d[k]
    return default


def _price_of(d):
    """USD price, tolerant of both payload generations.

    gmgn-cli v1.6 returns `price` as an OBJECT ({price, buys_1m, sells_24h,
    volume_24h, ...}) and the docs are explicit that market cap is NOT a field -
    it is `price.price x circulating_supply`. Older builds returned a flat
    number. `fnum(dict)` is None, so the nested shape used to fall through to a
    None price for every token.
    """
    v = _get(d, "price", "price_usd", "current_price", "usd_price",
             "token_price", "last_price")
    if isinstance(v, dict):
        v = _get(v, "price", "price_usd", "current_price", "usd_price", "last")
    return fnum(v)


def _nested_field(d, *paths):
    if not isinstance(d, dict):
        return None
    for p in paths:
        parts = p.split(".")
        cur = d
        ok = True
        for part in parts:
            if isinstance(cur, dict) and part in cur:
                cur = cur[part]
            else:
                ok = False
                break
        if ok and cur is not None:
            return cur
    return None


def _market_cap_usd(info):
    return fnum(_nested_field(info, "market_cap", "market_cap_usd", "mcap", "fdv", "usd_market_cap",
                              "token.market_cap", "token.fdv", "token.mcap", "base.market_cap"))


def _volume_24h(info):
    return fnum(_nested_field(info, "volume_24h", "price.volume_24h", "v24h_usd",
                              "volume_usd", "volume", "price.volume",
                              "token.volume_24h", "token.v24h_usd", "base.volume_24h"))


def _price_change_pct(info, key):
    v = _nested_field(info, f"price_change_{key}", f"change_{key}", f"price_change_percent_{key}",
                      f"token.price_change_{key}", f"token.change_{key}", f"base.price_change_{key}")
    if v is not None:
        return fnum(v)
    return None


def _progress_pct(v):
    if v is None or v == "":
        return None
    try:
        f = float(v)
        return f * 100.0 if f <= 1.0 else f
    except Exception:
        return None


def _info_progress(info):
    # progress_pct is the name this provider publishes in `signals`, so a signals
    # dict can be re-profiled without the original payload.
    v = _nested_field(info, "progress", "launchpad_progress", "progress_pct",
                      "bonding_curve_progress", "token.progress",
                      "token.bonding_curve_progress", "base.progress")
    return _progress_pct(v)


def _candidate_from_discovery(mint):
    for cat in ["trending", "trenches", "smartmoney", "kol"]:
        cached = _cache_get(f"discovery:{cat}")
        if cached:
            for item in cached:
                if (item.get("address") or item.get("mint")) == mint:
                    return item
    return None


def _merge_info_candidate(info, cand):
    if not cand or not isinstance(cand, dict):
        return info
    merged = dict(info)
    for k, v in cand.items():
        if k not in merged or merged[k] is None:
            merged[k] = v
    return merged


def _phase_from_progress(progress):
    if progress is None:
        return "trading"
    if progress >= 100.0:
        return "completed"
    if progress >= 80.0:
        return "near_completion"
    return "bonding"


def _swap_count(info, side, window="24h"):
    """Buy/sell transaction count for a window, from either payload generation.

    v1.6 nests them as price.buys_24h / price.sells_1m; the discovery lists carry
    flat buys_24h / sells_24h (trenches) or buys / sells (trending). None is
    preserved - a missing counter must not read as zero sells.
    """
    # Callers pass "buy"/"sell"; accept "buys"/"sells" too rather than building
    # "sellss_24h" and silently returning None (None means UNKNOWN to the gates).
    side = str(side or "").rstrip("s") or "buy"
    v = _nested_field(info,
                      f"price.{side}s_{window}", f"{side}s_{window}", f"{side}s",
                      f"price.{side}s", f"token.{side}s_{window}", f"base.{side}s")
    return fnum(v)


def _buy_pressure(info):
    b = _swap_count(info, "buy")
    s = _swap_count(info, "sell")
    if b is not None and s is not None and (b + s) > 0:
        return round(b / (b + s) * 100.0, 1)
    b_usd = fnum(_nested_field(info, "buy_volume_24h", "buy_volume", "token.buy_volume"))
    s_usd = fnum(_nested_field(info, "sell_volume_24h", "sell_volume", "token.sell_volume"))
    if b_usd is not None and s_usd is not None and (b_usd + s_usd) > 0:
        return round(b_usd / (b_usd + s_usd) * 100.0, 1)
    return None


def list_screen(candidate: dict, config: dict = None) -> dict:
    """Pre-screen discovery candidate using inline list fields."""
    cfg = config or load_config()
    ma = cfg.get("market_analysis", {}) or {}
    rej = []
    flags = []

    mcap = fnum(candidate.get("market_cap") or candidate.get("fdv") or candidate.get("usd_market_cap"))
    min_mc = fnum(ma.get("min_market_cap", 0))
    if min_mc and mcap is not None and mcap < min_mc:
        rej.append(f"list_screen: mcap ${mcap:,.0f} < ${min_mc:,.0f}")

    top10 = _norm_pct(candidate.get("top10_holder_rate") or candidate.get("top_10_holder_rate"))
    if top10 is not None and top10 > 0.85:
        rej.append(f"list_screen: top10 {top10*100:.1f}% > 85%")

    bundler = _norm_pct(candidate.get("bundler_hold_rate") or candidate.get("bundle_hold_rate"))
    if bundler is not None and bundler > 0.40:
        rej.append(f"list_screen: bundler {bundler*100:.1f}% > 40%")

    snipers = _norm_pct(candidate.get("sniper_hold_rate") or candidate.get("snipers_hold_rate"))
    if snipers is not None and snipers > 0.50:
        rej.append(f"list_screen: snipers {snipers*100:.1f}% > 50%")

    creator_close = candidate.get("creator_close")
    creator_hold = _norm_pct(candidate.get("creator_hold_rate") or candidate.get("dev_team_hold_rate"))
    if creator_close is True or (creator_hold is not None and creator_hold == 0.0):
        flags.append("list: dev sold all")
    elif creator_hold is not None and creator_hold > 0.50:
        rej.append(f"list_screen: dev hold {creator_hold*100:.1f}% > 50%")

    return {
        "pass": len(rej) == 0,
        "reasons": rej,
        "flags": flags,
        "mcap": mcap,
        "top10_pct": round(top10 * 100, 1) if top10 is not None else None,
        "bundler_pct": round(bundler * 100, 1) if bundler is not None else None,
    }


_DISCOVERY_STATUS = {"last_ok_ts": 0.0, "last_error": None, "categories_ok": {},
                     "consecutive_empty": 0, "last_count": None}


def discovery_status() -> dict:
    """Observed discovery health, for /health, /api/activity and enzoctl.

    Previously a failing GMGN sweep was indistinguishable from "GMGN answered
    and there was genuinely nothing to buy" — both looked like an empty list.
    """
    out = dict(_DISCOVERY_STATUS)
    out["age_sec"] = round(time.time() - out["last_ok_ts"], 1) if out["last_ok_ts"] else None
    return out


def discover(chain=None) -> list:
    """Discovery sweep from GMGN list endpoints across trenches, trending, and smartmoney.

    Each category is ALSO cached under its own `discovery:{cat}` key so that
    `_candidate_from_discovery` (which reads `discovery:trending|trenches|...`)
    can merge smart-money fields (smart_degen_count, hot_level, buys/sells,
    creator_created_count) into the deep analysis. Previously only
    `discovery:sweep:{ch}` was written, so the category lookup always missed.
    """
    cfg = load_config()
    ch = chain or cfg.get("chain", "sol")
    ckey = f"discovery:sweep:{ch}"
    cached = _cache_get(ckey)
    if cached is not None:
        return cached

    items = []
    seen_mints = set()
    gmgn_cfg = cfg.get("data_sources", {}).get("gmgn", {})
    categories = gmgn_cfg.get("discovery", ["trenches", "trending"])
    cat_items_map = {cat: [] for cat in categories}

    # `market smartmoney` / `market kol` do not exist in gmgn-cli v1.6 (smart
    # money and KOL live under `track`, which returns TRADE RECORDS for a wallet,
    # not a token list). Calling them burned a rate-limit slot and logged a
    # failure every single cycle - the "returns [] from this host" mystery in the
    # old audit was an unknown-command error, not a region block.
    VALID_CATEGORIES = {"trenches", "trending", "hot-searches", "signal"}
    platform_filter = str(gmgn_cfg.get("launchpad_platform_filter") or "").strip()
    try:
        limit = max(1, min(int(gmgn_cfg.get("discovery_limit", 50)), 80))
    except Exception:
        limit = 50
    min_mcap = fnum((cfg.get("token_universe") or {}).get("discovery_min_market_cap"))

    for cat in categories:
        if cat not in VALID_CATEGORIES:
            msg = (f"'market {cat}' is not a gmgn-cli command (v1.6 has "
                   f"trenches/trending/hot-searches/signal; smart money and KOL "
                   f"live under `track` and return trades, not token lists)")
            _LOGGER.warning("GMGN discovery category skipped — %s", msg)
            _DISCOVERY_STATUS["categories_ok"][cat] = {
                "ok": False, "count": 0, "error": msg, "skipped": True}
            continue
        try:
            args = ["market", cat, "--chain", ch, "--limit", str(limit)]
            if platform_filter:
                # trenches calls it --launchpad-platform, trending --platform
                args += ["--launchpad-platform" if cat == "trenches" else "--platform",
                         platform_filter]
            if min_mcap is not None:
                args += ["--min-marketcap", str(min_mcap)]
            res = _run(args, f"market/{cat}")
            raw_items = _extract_items(res, cat)
            for it in raw_items:
                normed = _norm_list_item(it, cat)
                mint = normed.get("mint") or normed.get("address")
                if mint:
                    cat_items_map[cat].append(normed)
                    if mint not in seen_mints:
                        seen_mints.add(mint)
                        items.append(normed)
            _DISCOVERY_STATUS["categories_ok"][cat] = {
                "ok": True, "count": len(cat_items_map.get(cat) or []), "error": None}
        except Exception as e:
            # Was _LOGGER.debug(): a GMGN sweep that failed for every category
            # returned [] and was then CACHED as a legitimate empty result for
            # 25s, so "Discovered 0 candidates" looked like a quiet market
            # rather than a dead data source. That is the exact ambiguity the
            # diagnosis called out as root cause A6.
            msg = f"{type(e).__name__}: {e}"
            _LOGGER.warning("GMGN category '%s' discovery failed — %s", cat, msg[:200])
            _DISCOVERY_STATUS["categories_ok"][cat] = {"ok": False, "count": 0, "error": msg[:300]}
            _DISCOVERY_STATUS["last_error"] = f"{cat}: {msg[:200]}"

    for cat, cat_items in cat_items_map.items():
        if cat_items:
            _cache_set(f"discovery:{cat}", cat_items, ttl=25)

    cats = _DISCOVERY_STATUS["categories_ok"]
    attempted = {k: v for k, v in cats.items() if not v.get("skipped")}
    all_failed = bool(attempted) and not any(c.get("ok") for c in attempted.values())
    if items:
        _DISCOVERY_STATUS["last_ok_ts"] = time.time()
        _DISCOVERY_STATUS["consecutive_empty"] = 0
        _DISCOVERY_STATUS["last_count"] = len(items)
        _cache_set(ckey, items, ttl=25)
    elif all_failed:
        # Do NOT cache a failure as if it were an answer: the next cycle should
        # retry immediately rather than wait out a 25s TTL on a dead result.
        _DISCOVERY_STATUS["consecutive_empty"] += 1
        _DISCOVERY_STATUS["last_count"] = 0
        _LOGGER.error("GMGN discovery failed for every category (%s) — result NOT cached, "
                      "will retry next cycle", ", ".join(sorted(cats)))
    else:
        _DISCOVERY_STATUS["last_ok_ts"] = time.time()
        _DISCOVERY_STATUS["consecutive_empty"] += 1
        _DISCOVERY_STATUS["last_count"] = 0
        _cache_set(ckey, items, ttl=25)
    return items


# `market <category>` shapes differ per command and per CLI generation:
#   trenches -> {new_creation: [...], near_completion: [...], completed: [...]}
#   trending -> {data: {rank: [...]}}
# The previous parser only knew the trenches keys and, because its conditional
# expression bound to `isinstance(res, dict)`, it applied them to EVERY dict -
# so `market trending` silently yielded zero tokens on every cycle.
_LIST_SHAPES = (
    ("new_creation", "near_completion", "completed"),
    ("rank",),
    ("list",),
    ("data",),
)


def _extract_items(res, category):
    """Pull the token rows out of any known discovery envelope."""
    if isinstance(res, list):
        return res
    if not isinstance(res, dict):
        return []
    bodies = [res]
    inner = res.get("data")
    if isinstance(inner, dict):
        bodies.append(inner)
    elif isinstance(inner, list):
        return inner
    for body in bodies:
        for shape in _LIST_SHAPES:
            rows = []
            for key in shape:
                v = body.get(key)
                if isinstance(v, list):
                    rows.extend(v)
            if rows:
                return rows
    return []


def _norm_list_item(it, source):
    mint = it.get("address") or it.get("mint") or it.get("token_address")
    return {
        "address": mint,
        "mint": mint,
        "symbol": it.get("symbol") or "UNKNOWN",
        "name": it.get("name") or "",
        "source": source,
        "price": _price_of(it),
        "market_cap": fnum(_get(it, "market_cap", "mcap", "usd_market_cap")),
        "liquidity": fnum(_get(it, "liquidity", "usd_liquidity")),
        "volume_24h": fnum(_get(it, "volume_24h", "v24h_usd", "volume")),
        "progress": _progress_pct(it.get("progress") or it.get("bonding_curve_progress")),
        "created_at": it.get("created_at") or it.get("open_timestamp"),
        "top10_holder_rate": _norm_pct(it.get("top10_holder_rate")),
        "bundler_hold_rate": _norm_pct(it.get("bundler_hold_rate")),
        "sniper_hold_rate": _norm_pct(it.get("sniper_hold_rate")),
        "creator_close": it.get("creator_close"),
        "creator_hold_rate": _norm_pct(it.get("creator_hold_rate")),
        "smart_degen_count": fnum(it.get("smart_degen_count")),
        "hot_level": fnum(it.get("hot_level")),
        "buys": fnum(it.get("buys") or it.get("buys_24h")),
        "sells": fnum(it.get("sells") or it.get("sells_24h")),
        "creator_created_count": fnum(it.get("creator_created_count") or it.get("creator_token_total")
                                     or it.get("dev_created_count") or it.get("creator_total_created")),
        # ── fields the v1.6 lists actually carry, and the new gates need ──────
        # launchpad_platform ("Pump.fun" / "letsbonk" / "pool_meteora" / ...) is
        # how a standard pump.fun coin is told apart from any other launchpad;
        # complete_timestamp/exchange say whether the curve already graduated.
        "launchpad": it.get("launchpad"),
        "launchpad_platform": it.get("launchpad_platform") or it.get("platform"),
        "launchpad_status": it.get("launchpad_status"),
        "exchange": it.get("exchange"),
        "sniper_count": fnum(it.get("sniper_count")),
        "top70_sniper_hold_rate": _norm_pct(it.get("top70_sniper_hold_rate")),
        "bundler_rate": _norm_pct(it.get("bundler_rate") or it.get("bundler_trader_amount_rate")),
        "sells_24h": fnum(it.get("sells_24h") or it.get("sells")),
        "buys_24h": fnum(it.get("buys_24h") or it.get("buys")),
        "swaps_24h": fnum(it.get("swaps_24h") or it.get("swaps")),
        "created_timestamp": it.get("created_timestamp") or it.get("creation_timestamp"),
        "complete_timestamp": it.get("complete_timestamp"),
        "creator_address": it.get("creator") or it.get("creator_address"),
        "raw": it
    }


def token_info(mint: str) -> dict:
    ckey = f"info:{mint}"
    cached = _cache_get(ckey)
    if cached is not None:
        return cached

    try:
        cfg = load_config()
        res = _run_addr(["token", "info", "--chain", cfg.get("chain", "sol")], "token/info", mint)
        data = res.get("data", {}) if isinstance(res, dict) and "data" in res else (res if isinstance(res, dict) else {})
        _cache_set(ckey, data, ttl=_CACHE_TTL["info"])
        return data
    except Exception as e:                                 # noqa: BLE001
        _note_error("token/info", e)
        return {}


def token_security(mint: str) -> dict:
    ckey = f"security:{mint}"
    cached = _cache_get(ckey)
    if cached is not None:
        return cached

    try:
        cfg = load_config()
        res = _run_addr(["token", "security", "--chain", cfg.get("chain", "sol")], "token/security", mint)
        data = res.get("data", {}) if isinstance(res, dict) and "data" in res else (res if isinstance(res, dict) else {})
        _cache_set(ckey, data, ttl=_CACHE_TTL["security"])
        return data
    except Exception as e:                                 # noqa: BLE001
        _note_error("token/security", e)
        return {}


def _normalize_holders(data, limit: int = 20) -> list:
    """Normalize GMGN holder payloads (list or dict with 'holders'/'list'/'top_holders')
    into a canonical list.

    Field names follow gmgn-cli v1.6.1 (`skills/gmgn-holder-analysis/SKILL.md`):

      amount_percentage       fraction of TOTAL supply (0-1)
      addr_type               0 = normal wallet, 1 = burn/dead, 2 = DEX/pool
      sell_amount_percentage  fraction of that wallet's buys already sold
      unrealized_pnl          ratio (0.5 = +50%)
      start_holding_at        unix ts of the first buy

    BUG FIXED: the old normalizer looked for percent/percentage/share/pct only,
    so on the real payload EVERY row came back with pct=None. That silently
    zeroed top1/top10 concentration, the top-10 dumping/accumulating counts,
    the average profit and the average wallet age — and since `dumping >=
    accumulating` was 0 >= 0 the wallet axis could never flag sell pressure.
    """
    holders = []
    if isinstance(data, list):
        holders = data
    elif isinstance(data, dict):
        holders = (data.get("holders") or data.get("list") or data.get("top_holders")
                   or data.get("data") or [])
        if isinstance(holders, dict):
            holders = holders.get("list") or holders.get("holders") or []

    norm = []
    for h in holders:
        if not isinstance(h, dict):
            continue
        pct = fnum(_get(h, "amount_percentage", "percent", "percentage", "share", "pct",
                        "holder_percent"))
        if pct is not None and pct > 1.0:
            pct = pct / 100.0                       # tolerate percent-style payloads
        try:
            addr_type = int(h.get("addr_type")) if h.get("addr_type") is not None else None
        except Exception:
            addr_type = None
        sold = fnum(_get(h, "sell_amount_percentage", "sell_percent", "sell_pct"))
        if sold is not None:
            sold = max(0.0, min(1.0, sold))
        held = (1.0 - sold) if sold is not None else None
        start = h.get("start_holding_at")
        holding_days = None
        try:
            if start:
                holding_days = round(max(0.0, (time.time() - float(start)) / 86400.0), 2)
        except Exception:
            holding_days = None
        norm.append({
            "address": h.get("address") or h.get("owner") or h.get("wallet"),
            "pct": pct,
            "tags": h.get("tags") or [],
            "maker_token_tags": h.get("maker_token_tags") or [],
            "addr_type": addr_type,
            # addr_type 1 = burn/dead, 2 = DEX/pool (bonding curve, AMM vault)
            "is_pool_or_burn": addr_type in (1, 2),
            # fraction of THIS wallet's buys still held / already sold — the
            # accumulating-vs-distributing verdict the wallet axis needs
            "buy_pct": held,
            "sell_pct": sold,
            "profit_ratio": fnum(_get(h, "unrealized_pnl", "profit_ratio", "avg_profit_ratio",
                                      "profit_multiple")),
            "usd_value": fnum(h.get("usd_value")),
            "avg_cost": fnum(h.get("avg_cost")),
            "profit_usd": fnum(h.get("profit")),
            "start_holding_at": start,
            "holding_days": holding_days,
            # not present in the v1.6 holder payload; kept for older shapes
            "wallet_age_days": fnum(h.get("wallet_age_days") or h.get("wallet_age") or h.get("age_days")),
            "is_contract": bool(h.get("is_contract") or h.get("contract")) or addr_type == 2,
            "raw": h,
        })
    return norm[:limit]


# Holder rows GMGN returns include the bonding-curve ATA and, after graduation,
# the AMM vault (pump_amm / Raydium / Meteora) plus lockers and burn addresses.
# Those routinely hold 20-80% of supply, so treating one as "the top holder"
# would veto every healthy migrated token.
_POOL_TAG_KEYWORDS = ("pool", "amm", "curve", "vault", " lp", "lp ", "raydium",
                      "meteora", "orca", "locker", "lock", "burn", "treasury",
                      "contract", "exchange")


def _is_pool_holder(row) -> bool:
    """True when a holder row is a pool/locker/contract instead of a trader."""
    if not isinstance(row, dict):
        return False
    # addr_type is authoritative in v1.6: 1 = burn/dead, 2 = DEX/pool.
    if row.get("addr_type") in (1, 2) or row.get("is_pool_or_burn"):
        return True
    if row.get("is_contract") or row.get("is_pool") or row.get("is_locker"):
        return True
    tags = []
    for key in ("tags", "tag", "maker_token_tags", "holder_tag", "wallet_tag"):
        v = (row.get("raw") or {}).get(key) if isinstance(row.get("raw"), dict) else None
        v = v if v is not None else row.get(key)
        if isinstance(v, str):
            tags.append(v)
        elif isinstance(v, (list, tuple)):
            tags.extend(str(x) for x in v)
    blob = " " + " ".join(tags).lower() + " "
    return any(k in blob for k in _POOL_TAG_KEYWORDS)


def holder_distribution(mint: str, exclude_curve_ata: bool = True, limit: int = 20) -> dict:
    """Holder distribution in a canonical shape consumed by the wallet/dev axes.

    Returns:
        {"ok": bool, "top1_pct": float|None (fraction), "top10_pct": float|None (fraction),
         "holder_count": int, "holders": [...], "raw": ...}
    """
    ckey = f"holders:{mint}"
    cached = _cache_get(ckey)
    if cached is not None:
        return cached

    try:
        cfg = load_config()
        # Ask for more rows than we return: the top rows are usually the curve /
        # AMM vault, and truncating BEFORE filtering them out would leave us
        # measuring concentration over a handful of leftover wallets.
        fetch_limit = max(int(limit), 50)
        res = _run_addr(["token", "holders", "--chain", cfg.get("chain", "sol")],
                        "token/holders", mint, ["--limit", str(fetch_limit)])
        data = res.get("data", {}) if isinstance(res, dict) and "data" in res else (res if isinstance(res, dict) else {})
        all_rows = _normalize_holders(data, limit=fetch_limit)
        if exclude_curve_ata:
            excluded = [h for h in all_rows if _is_pool_holder(h)]
            holders = [h for h in all_rows if not _is_pool_holder(h)]
        else:
            excluded, holders = [], all_rows
        holders = holders[:limit]
        top10 = sum(h["pct"] for h in holders[:10] if h["pct"] is not None)
        top1 = holders[0]["pct"] if holders and holders[0]["pct"] is not None else None
        norm = {
            "ok": True,
            "top1_pct": top1,
            "top10_pct": top10 if top10 else None,
            "holder_count": len(holders),
            "holders": holders,
            # transparency for enzoctl probe / the dashboard: which rows were
            # dropped as pools, and what the unfiltered top-1 share was
            "rows_total": len(all_rows),
            "excluded_pools": [{"address": h.get("address"), "pct": h.get("pct"),
                                "tags": h.get("tags"), "is_contract": h.get("is_contract")}
                               for h in excluded],
            "top1_pct_all": (all_rows[0]["pct"] if all_rows and all_rows[0]["pct"] is not None
                             else None),
            # tradeable float = 1 - burn - DEX, over the rows we fetched. Below
            # ~2% (typical before migration) float-based percentages are
            # meaningless, so callers must not re-base onto it.
            "burn_pct": round(sum(h["pct"] for h in all_rows
                                  if h.get("addr_type") == 1 and h.get("pct") is not None), 6),
            "dex_pct": round(sum(h["pct"] for h in all_rows
                                 if h.get("addr_type") == 2 and h.get("pct") is not None), 6),
            "raw": data,
        }
        norm["float_share"] = round(max(0.0, 1.0 - norm["burn_pct"] - norm["dex_pct"]), 6)
        norm["float_degenerate"] = norm["float_share"] < 0.02
        _cache_set(ckey, norm, ttl=_CACHE_TTL["holders"])
        return norm
    except Exception as e:                                 # noqa: BLE001
        _note_error("token/holders", e)
        return {"ok": False, "top1_pct": None, "top10_pct": None, "holder_count": 0,
                "holders": [], "error": f"{type(e).__name__}: {e}"[:200]}


def deep_holder_analysis(mint: str, limit: int = 20) -> dict:
    """Deep holder analysis: holder distribution + top-trader identity tags.

    Powers the wallet_behavior axis (smart/whale/KOL/bundler/sniper/rat counts,
    top-10 dumping/accumulation, avg profit & wallet age).
    """
    try:
        dist = holder_distribution(mint, limit=limit)
        holders = dist.get("holders") or []
        identity = top_trader_identity(mint, limit=limit)
        smart = int(identity.get("smart", 0))
        whale = int(identity.get("whale", 0))
        kol = int(identity.get("kol", 0))

        def _tag_count(tag_kw):
            """Count holders carrying a keyword in EITHER tag list.

            v1.6 splits them: `maker_token_tags` holds bundler/rat_trader/
            sniper/whale/top_holder/transfer_in/dev_team/creator, while `tags`
            holds smart_degen/pump_smart/renowned/fresh_wallet/wash_trader/kol.
            Reading only `tags` made the sniper/bundler/rat counts permanently 0.
            """
            c = 0
            for h in holders:
                both = list(h.get("tags") or []) + list(h.get("maker_token_tags") or [])
                tag_str = " ".join(str(t).lower() for t in both)
                if tag_kw in tag_str:
                    c += 1
            return c

        bundler = _tag_count("bundler")
        sniper = _tag_count("sniper")
        rat = _tag_count("rat")

        top10 = holders[:10]
        top10_dumping = sum(1 for h in top10 if (h.get("sell_pct") or 0) > (h.get("buy_pct") or 0))
        top10_accumulating = sum(1 for h in top10 if (h.get("buy_pct") or 0) > (h.get("sell_pct") or 0))
        top10_cur_sells = sum(1 for h in top10 if (h.get("sell_pct") or 0) >= 0.5)

        profits = [h["profit_ratio"] for h in top10 if h.get("profit_ratio") is not None]
        avg_profit = (sum(profits) / len(profits)) if profits else None
        ages = [h["wallet_age_days"] for h in holders if h.get("wallet_age_days") is not None]
        avg_age = (sum(ages) / len(ages)) if ages else None

        stats = {
            "smart_count": smart,
            "whale_count": whale,
            "kol_count": kol,
            "bundler_count": bundler,
            "sniper_count": sniper,
            "rat_count": rat,
            "top10_dumping": top10_dumping,
            "top10_accumulating": top10_accumulating,
            "top10_cur_sells": top10_cur_sells,
            "top10_avg_profit_ratio": avg_profit,
            "avg_wallet_age_days": avg_age,
            "holder_count": dist.get("holder_count", len(holders)),
        }
        return {"ok": True, "stats": stats, "holders": holders,
                "top1_pct": dist.get("top1_pct"), "top10_pct": dist.get("top10_pct")}
    except Exception as e:                                 # noqa: BLE001
        _note_error("token/deep-holders", e)
        return {"ok": False, "stats": {}, "error": f"{type(e).__name__}: {e}"[:200]}


def security_scan(mint: str) -> dict:
    """Tier-1 security scan (GMGN on-chain security + holder concentration)."""
    sec = token_security(mint)
    info = token_info(mint)
    cand = _candidate_from_discovery(mint)
    merged_info = _merge_info_candidate(info, cand)

    # Authorities
    ren_mint = _get(sec, "renounced_mint", "is_mint_renounced", default=None)
    if ren_mint is None:
        ren_mint = _get(merged_info, "renounced_mint", default=None)

    ren_freeze = _get(sec, "renounced_freeze_account", "renounced_freeze", default=None)
    if ren_freeze is None:
        ren_freeze = _get(merged_info, "renounced_freeze_account", "renounced_freeze", default=None)

    is_honeypot = _get(sec, "is_honeypot", "honeypot", default=False)
    is_blacklisted = _get(sec, "is_blacklisted", "blacklisted", default=False)

    hard_rejects = []
    flags = []

    if is_honeypot:
        hard_rejects.append("HONEYPOT")
    if is_blacklisted:
        hard_rejects.append("BLACKLISTED")
    if ren_mint in (False, 0, "0", "false"):
        hard_rejects.append("MINT_AUTHORITY_ACTIVE")
    if ren_freeze in (False, 0, "0", "false"):
        hard_rejects.append("FREEZE_AUTHORITY_ACTIVE")

    # Holder concentration
    top10 = _norm_pct(_get(sec, "top10_holder_rate", "top_10_holder_rate") or _get(merged_info, "top10_holder_rate"))
    if top10 is not None and top10 > 0.85:
        hard_rejects.append(f"TOP10_CONCENTRATION_{top10*100:.0f}PCT")

    status = "DANGEROUS" if hard_rejects else ("WARNING" if flags else "SAFE")
    safety_score = 0 if status == "DANGEROUS" else (70 if status == "WARNING" else 95)

    return {
        "mint_address": mint,
        "token_type": "PUMP" if _get(merged_info, "is_pumpfun") or "pump" in mint.lower() else "STANDARD",
        "security_status": status,
        "safety_score": safety_score,
        "hard_reject": hard_rejects,
        "security_flags": flags,
        "mint_authority": None if ren_mint in (True, 1, "1") else "ACTIVE",
        "freeze_authority": None if ren_freeze in (True, 1, "1") else "ACTIVE",
        "top10_holder_pct": round(top10 * 100, 1) if top10 is not None else None,
        "holder_count": fnum(_get(merged_info, "holder_count", "holders")),
        "liquidity": fnum(_get(merged_info, "liquidity", "usd_liquidity")),
        "price": _price_of(merged_info),
        "quality": {
            "top1_pct": _norm_pct(_get(sec, "top1_holder_rate") or _get(merged_info, "top1_holder_rate")),
            "creator_hold_pct": _norm_pct(_get(merged_info, "creator_hold_rate")),
            "creator_created_count": fnum(_get(merged_info, "creator_created_count", "creator_token_total",
                                               "dev_created_count", "creator_total_created")),
            "dev_events": ["DEV_SOLD_ALL"] if merged_info.get("creator_close") else []
        }
    }


def cached_holder_distribution(mint: str, cfg: dict = None, ttl: float = 600) -> dict:
    return holder_distribution(mint)


def read_bonding_curve(mint: str) -> dict:
    info = token_info(mint)
    cand = _candidate_from_discovery(mint)
    merged = _merge_info_candidate(info, cand)
    prog = _info_progress(merged)
    return {
        "mint": mint,
        "progress_pct": prog,
        "phase": _phase_from_progress(prog),
        "is_pump": True,
        "market_cap": _market_cap_usd(merged),
        "price": _price_of(merged)
    }


def sol_price_usd() -> float:
    """Live SOL/USD price via DexScreener, cached 60s, conservative fallback 180.0."""
    ckey = "sol_price:usd"
    cached = _cache_get(ckey)
    if cached is not None:
        return cached

    price = None
    try:
        import urllib.request
        req = urllib.request.Request(
            "https://api.dexscreener.com/latest/dex/tokens/So11111111111111111111111111111111111111112",
            headers={"User-Agent": "Mozilla/5.0", "Accept": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=6) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        for pair in (data.get("pairs") or []):
            pu = fnum(pair.get("priceUsd"))
            if pu and pu > 0:
                price = pu
                break
    except Exception:
        pass

    if not price:
        price = 180.0  # conservative fallback only
    _cache_set(ckey, price, ttl=_CACHE_TTL["sol_price"])
    return price


def kline(mint: str, resolution="1m", from_ts=None, to_ts=None) -> list:
    ckey = f"kline:{mint}:{resolution}"
    cached = _cache_get(ckey)
    if cached is not None:
        return cached

    try:
        cfg = load_config()
        base = ["market", "kline", "--chain", cfg.get("chain", "sol"),
                "--resolution", str(resolution)]
        if from_ts:
            base += ["--from", str(int(from_ts))]
        if to_ts:
            base += ["--to", str(int(to_ts))]
        res = _run_addr(base, "market/kline", mint)
        data = res.get("data", []) if isinstance(res, dict) else (res if isinstance(res, list) else [])
        _cache_set(ckey, data, ttl=_CACHE_TTL["kline"])
        return data
    except Exception:
        return []


def get_market_data(mint: str) -> dict:
    """Fetch live market snapshot for a token."""
    ckey = f"market_data:{mint}"
    cached = _cache_get(ckey)
    if cached is not None:
        return cached

    info = token_info(mint)
    cand = _candidate_from_discovery(mint)
    merged = _merge_info_candidate(info, cand)

    sym = merged.get("symbol") or None
    # Handle GMGN token info response: price is nested at info['price']['price']
    price_info = merged.get("price", {})
    price = fnum(_get(price_info, "price")) or _price_of(merged) or 0.0
    # Never fabricate market cap if the quote does not resolve.
    # Try: (1) explicit market_cap field, (2) price × circulating_supply.
    mcap = _market_cap_usd(merged)
    if (mcap is None or mcap == 0) and price > 0:
        supply_str = merged.get("circulating_supply") or merged.get("total_supply")
        supply = fnum(supply_str)
        if supply:
            mcap = price * supply
    vol = _volume_24h(merged)
    liq = fnum(merged.get("liquidity"))
    if liq is None:
        liq = fnum(_get(merged, "usd_liquidity"))
    prog = _info_progress(merged)

    # ── Data quality: MISSING must stay distinguishable from ZERO ────────────
    # These three used to be coerced with `or 0.0`. The analyzer then saw a
    # token with "$0 market cap / $0 liquidity / $0 volume" and rejected it for
    # being worthless, when the truth was that GMGN had returned nothing at all
    # (rate-limited, blocked, or an unknown mint). That single coercion is why
    # the audit log holds 1,649 "Market cap $0 < min" rejects and why the bot
    # appeared to "never analyse any coin". None is now preserved so the caller
    # can report NO_MARKET_DATA instead of a misleading quality rejection.
    missing = [name for name, val in (("market_cap_usd", mcap), ("liquidity_usd", liq),
                                      ("volume_24h_usd", vol), ("price_usd", price or None),
                                      ("symbol", sym)) if val is None]
    banned = ban_status()
    data_quality = {
        "complete": not missing,
        "missing": missing,
        "provider": "gmgn",
        "rate_limited": bool(banned > 0),
        "ban_remaining_sec": round(float(banned), 1),
        "fetched_at": datetime.now(timezone.utc).isoformat(),
    }

    # Launchpad identity + migration phase drive the owner's gates (standard
    # pump.fun coins only; different minimums before and after migration), so
    # they are resolved here once and carried in the snapshot.
    profile = launchpad_profile(merged)

    data = {
        "token_symbol": sym or "UNKNOWN",
        "price_usd": price,
        "phase": _phase_from_progress(prog),
        "data_quality": data_quality,
        "launchpad": profile,
        "signals": {
            "market_cap_usd": mcap,
            "liquidity_usd": liq,
            "volume_24h_usd": vol,
            "price_change_1h": _price_change_pct(merged, "1h") or 0.0,
            "price_change_24h": _price_change_pct(merged, "24h") or 0.0,
            "price_change_5m": _price_change_pct(merged, "5m"),
            "buy_pressure_pct": _buy_pressure(merged),
            "progress_pct": prog,
            "smart_degen_count": fnum(_get(merged, "smart_degen_count")),
            "hot_level": fnum(_get(merged, "hot_level")),
            # buys/sells now come through _swap_count, which understands the v1.6
            # nested price object (price.sells_24h) as well as the flat list fields
            "buys": _swap_count(merged, "buy"),
            "sells": _swap_count(merged, "sell"),
            "sells_1m": _swap_count(merged, "sell", "1m"),
            "sells_5m": _swap_count(merged, "sell", "5m"),
            "buys_24h": _swap_count(merged, "buy"),
            "sells_24h": _swap_count(merged, "sell"),
            "sniper_count": fnum(_nested_field(merged, "sniper_count",
                                               "wallet_tags_stat.sniper_wallets")),
            "top70_sniper_hold_rate": _norm_pct(_nested_field(
                merged, "top70_sniper_hold_rate", "stat.top70_sniper_hold_rate")),
            "bundler_rate": _norm_pct(_nested_field(
                merged, "bundler_rate", "bundler_trader_amount_rate", "stat.bundler_rate")),
            "is_pump_v1": profile.get("is_pump_v1"),
            "launchpad_known": profile.get("launchpad_known"),
            "launchpad": profile.get("launchpad"),
            "launchpad_platform": profile.get("platform"),
            "launchpad_status": profile.get("launchpad_status"),
            "exchange": profile.get("exchange"),
            "migrated": profile.get("migrated"),
            "migration_phase": profile.get("phase"),
            "phase_evidence": profile.get("reasons"),
            "created_timestamp": fnum(_nested_field(merged, "creation_timestamp",
                                                    "created_timestamp", "open_timestamp")),
            "creator_address": _nested_field(merged, "dev.creator_address",
                                             "creator_address", "creator"),
        }
    }
    _cache_set(ckey, data, ttl=_CACHE_TTL["market_data"])
    return data


# ================================================================ launchpad / phase
# "Standard pump coin" == launched on pump.fun's own bonding curve (Pump V1).
# GMGN names it two ways: `launchpad` ("pump") and `launchpad_platform`
# ("Pump.fun"); other launchpads arrive as letsbonk / moonshot / fourmeme / bags.
PUMP_LAUNCHPAD_IDS = {"pump", "pump.fun", "pumpfun", "pump_v1"}
PUMP_PLATFORM_HINTS = ("pump.fun", "pump_fun", "pumpfun")
# exchange values that mean "the curve graduated" vs "still on the curve"
MIGRATED_EXCHANGES = {"pump_amm", "pumpswap", "raydium", "meteora_dlmm",
                      "meteora_damm_v2", "meteora", "orca", "fluxbeam"}
CURVE_EXCHANGES = {"pump_fun", "pump.fun", "pumpfun", ""}


def launchpad_profile(d) -> dict:
    """Pump-V1 identity + migration phase, with the evidence that decided it.

    `launchpad_status` is the authoritative field (0 = not opened, 1 = live on
    the curve, 2 = migrated); the rest are fallbacks for payloads that omit it.
    `migrated` stays None when nothing in the payload can say - the gates treat
    "unknown" differently from "pre-migration" rather than guessing.
    """
    if not isinstance(d, dict):
        d = {}
    lp = str(_nested_field(d, "launchpad", "token.launchpad", "base.launchpad") or "").strip().lower()
    platform = str(_nested_field(d, "launchpad_platform", "platform",
                                 "token.launchpad_platform") or "").strip().lower()
    status = _nested_field(d, "launchpad_status", "token.launchpad_status")
    migrated_pool = _nested_field(d, "migrated_pool", "token.migrated_pool")
    if migrated_pool is None and _nested_field(d, "migrated") is not None:
        # a flattened signals dict carries the verdict, not the pool address
        migrated_pool = "reported-migrated" if _nested_field(d, "migrated") else None
    complete_ts = fnum(_nested_field(d, "complete_timestamp", "completed_timestamp"))
    exchange = str(_nested_field(d, "exchange", "pool.exchange") or "").strip().lower()
    progress = _info_progress(d)

    # Identity comes from the launchpad fields ONLY. `exchange` says where the
    # token trades now (pump_fun on the curve, pump_amm/raydium after graduating)
    # and is used for the phase fallback below - it must not decide whether a coin
    # is a standard pump.fun launch, or a token from another launchpad whose pool
    # happens to be reported as pump_amm would slip through the universe gate.
    is_pump = (lp in PUMP_LAUNCHPAD_IDS
               or any(h in platform for h in PUMP_PLATFORM_HINTS))
    # An empty string is GMGN saying "I have nothing", not a launchpad name.
    known = bool(lp or platform or exchange or (status is not None and str(status) != ""))

    migrated, reasons = None, []
    st = None
    if status is not None and str(status) != "":
        try:
            st = int(float(status))
            migrated = (st == 2)
            reasons.append(f"launchpad_status={st} (0=closed,1=live,2=migrated)")
        except Exception:
            reasons.append(f"launchpad_status unparseable ({status!r})")
    if migrated is None and migrated_pool:
        migrated = True
        reasons.append("migrated_pool present")
    if migrated is None and complete_ts:
        migrated = True
        reasons.append("complete_timestamp set (curve finished)")
    if migrated is None and progress is not None and progress >= 100.0:
        migrated = True
        reasons.append("progress=100%")
    if migrated is None and exchange:
        if exchange in MIGRATED_EXCHANGES:
            migrated = True
            reasons.append(f"exchange={exchange}")
        elif exchange in CURVE_EXCHANGES:
            migrated = False
            reasons.append(f"exchange={exchange} (still on the bonding curve)")

    phase = "migrated" if migrated else ("pre_migration" if migrated is False else "unknown")
    return {
        "is_pump_v1": bool(is_pump),
        "launchpad_known": bool(known),
        "launchpad": lp or None,
        "platform": platform or None,
        "launchpad_status": st,
        "migrated": migrated,
        "phase": phase,
        "progress_pct": progress,
        "exchange": exchange or None,
        "complete_timestamp": complete_ts,
        "reasons": reasons,
    }


# ================================================================ early snipers
def token_traders_raw(mint: str, limit: int = 100, order_by: str = "buy_volume_cur",
                      tag: str = None, ttl: float = 90.0) -> list:
    """Raw `token traders` rows (v1.6: --address, --order-by, --tag, --limit<=100)."""
    ckey = f"traders_raw:{mint}:{limit}:{order_by}:{tag or '-'}"
    cached = _cache_get(ckey)
    if cached is not None:
        return cached
    cfg = load_config()
    extra = ["--limit", str(int(limit)), "--order-by", str(order_by), "--direction", "desc"]
    if tag:
        extra += ["--tag", str(tag)]
    res = _run_addr(["token", "traders", "--chain", cfg.get("chain", "sol")],
                    "token/traders", mint, extra)
    rows = res.get("data") if isinstance(res, dict) else res
    if not isinstance(rows, list):
        rows = _extract_items(res, "traders")
    out = [r for r in rows if isinstance(r, dict)]
    _cache_set(ckey, out, ttl=ttl)
    return out


def _wallet_tags(row) -> set:
    """GMGN splits wallet labels across two lists.

    `maker_token_tags` carries bundler / rat_trader / sniper / whale / top_holder
    / transfer_in / dev_team / creator; `tags` carries smart_degen / pump_smart /
    renowned / fresh_wallet / wash_trader / kol. Reading only one of them (the old
    top_trader_identity looked for "whale"/"kol" inside `tags`, where they never
    appear) silently reports zero of everything.
    """
    out = set()
    for key in ("maker_token_tags", "tags", "token_tags", "wallet_tags"):
        v = row.get(key)
        if isinstance(v, list):
            out.update(str(x).strip().lower() for x in v if x)
        elif isinstance(v, str) and v:
            out.update(x.strip().lower() for x in v.split(","))
    return {t for t in out if t}


def early_sniper_report(mint: str, cfg: dict = None) -> dict:
    """The owner's rug signature: whoever got in first, right after the dev's
    create transaction, is a wall of snipers with huge size.

    gmgn-cli exposes NO trade tape - its token commands are info / security /
    pool / holders / traders - so "the first 8 transactions" cannot be read
    literally. What CAN be read is each top trader's `start_holding_at` (unix
    timestamp of their first buy), `buy_volume_cur` (USD bought since creation)
    and the tags GMGN assigns, where `sniper` means "bought at token open".
    Sorting by start_holding_at reconstructs entry order, so the first N rows ARE
    the wallets that were in first.

    Honest limits, also reported in the payload:
      * the endpoint returns TOP traders (we ask for 100 by buy volume), so a
        wallet that bought and dumped to zero may be missing;
      * rows without a timestamp cannot be placed in the window and are counted
        separately, never silently treated as "early";
      * GMGN's own docs warn a sniper tag cannot tell the dev's alts from a
        third-party bot - for a VETO that ambiguity is acceptable.
    """
    cfg = cfg or load_config()
    sf = (cfg.get("sniper_flood") or {})
    first_n = int(sf.get("first_n", 8) or 8)
    limit = int(sf.get("traders_limit", 100) or 100)
    order_by = str(sf.get("order_by", "buy_volume_cur") or "buy_volume_cur")
    sniper_tags = {str(t).strip().lower() for t in
                   (sf.get("sniper_tags") or ["sniper"]) if str(t).strip()}
    if sf.get("include_bundler", False):
        sniper_tags.add("bundler")

    out = {
        "ok": False, "mint": mint, "first_n": first_n, "sniper_tags": sorted(sniper_tags),
        "window": [], "sniper_count": 0, "sniper_total_usd": None,
        "max_single_usd": None, "verdict": "unknown", "reason": "",
        "rows_seen": 0, "rows_with_ts": 0, "creation_ts": None,
        "thresholds": {"min_sniper_count": int(sf.get("min_sniper_count", 4) or 0),
                       "max_total_sniper_buy_usd": fnum(sf.get("max_total_sniper_buy_usd"), 5000.0),
                       "max_single_sniper_buy_usd": fnum(sf.get("max_single_sniper_buy_usd"), 5000.0)},
    }
    try:
        rows = token_traders_raw(mint, limit=limit, order_by=order_by)
    except Exception as e:
        out["reason"] = f"token traders failed: {type(e).__name__}: {e}"[:220]
        return out

    out["rows_seen"] = len(rows)
    creation = fnum(_nested_field(token_info(mint) if rows else {},
                                  "creation_timestamp", "open_timestamp"))
    out["creation_ts"] = creation

    timed = []
    for r in rows:
        ts = fnum(r.get("start_holding_at") or r.get("first_buy_timestamp")
                  or r.get("created_timestamp"))
        if ts is None:
            continue
        timed.append((ts, r))
    out["rows_with_ts"] = len(timed)
    if not timed:
        out["reason"] = ("no trader row carries start_holding_at, so entry order "
                         "cannot be reconstructed")
        return out

    timed.sort(key=lambda x: x[0])
    window = timed[:first_n]
    for ts, r in window:
        buy_usd = fnum(r.get("buy_volume_cur") or r.get("buy_amount_usd")
                       or r.get("history_bought_cost"))
        tags = _wallet_tags(r)
        out["window"].append({
            "address": r.get("address"),
            "start_holding_at": ts,
            "seconds_after_open": round(ts - creation, 1) if creation else None,
            "buy_usd": buy_usd,
            "tags": sorted(tags),
            "is_sniper": bool(tags & sniper_tags),
        })

    snipers = [w for w in out["window"] if w["is_sniper"]]
    sizes = [w["buy_usd"] for w in snipers if w["buy_usd"] is not None]
    out["sniper_count"] = len(snipers)
    out["sniper_total_usd"] = round(sum(sizes), 2) if sizes else None
    out["max_single_usd"] = round(max(sizes), 2) if sizes else None
    out["ok"] = True

    th = out["thresholds"]
    total = out["sniper_total_usd"]
    single = out["max_single_usd"]
    n_win = len(out["window"])
    if not snipers:
        out["verdict"] = "pass"
        out["reason"] = f"none of the first {n_win} wallets is sniper-tagged"
    elif total is None and single is None:
        out["verdict"] = "unknown"
        out["reason"] = "snipers found but no buy_volume_cur on any of them"
    elif single is not None and single > th["max_single_sniper_buy_usd"]:
        # One wallet alone over the single-size bar is a veto whatever the count:
        # the owner's rule is "its size OR their sum exceeds the threshold", and
        # gating that behind min_sniper_count would let a $6,500 launch snipe
        # through just because it arrived without friends.
        out["verdict"] = "veto"
        out["reason"] = (f"a single sniper-tagged wallet in the first {n_win} bought "
                         f"${single:,.0f} > ${th['max_single_sniper_buy_usd']:,.0f}")
    elif len(snipers) < th["min_sniper_count"]:
        out["verdict"] = "pass"
        out["reason"] = (f"{len(snipers)} of the first {n_win} wallets are sniper-tagged "
                         f"(< min {th['min_sniper_count']}) and the largest single buy is "
                         f"${single if single is not None else 0:,.0f}")
    elif total is not None and total > th["max_total_sniper_buy_usd"]:
        out["verdict"] = "veto"
        out["reason"] = (f"{len(snipers)} of the first {n_win} wallets are sniper-tagged and "
                         f"bought ${total:,.0f} combined (largest single "
                         f"${single if single is not None else 0:,.0f}) > "
                         f"${th['max_total_sniper_buy_usd']:,.0f} threshold")
    else:
        out["verdict"] = "pass"
        out["reason"] = (f"{len(snipers)} sniper-tagged in the first {n_win} but only "
                         f"${total if total is not None else 0:,.0f} combined "
                         f"(<= ${th['max_total_sniper_buy_usd']:,.0f})")
    return out


# ================================================================ fees paid
def fees_paid(mint: str, creator: str = None, cfg: dict = None) -> dict:
    """Total fees a coin has paid, for the migrated-coin gate.

    Not available on `token info` / trenches / trending in v1.6. It IS on
    `portfolio created-tokens` (the dev's launch book), where each row carries
    `total_fee` and `coin_creator_fee` - the same numbers GMGN's UI shows as
    fees paid. The unit is not stated by the API; the owner's threshold is in
    SOL, so `phase_gates.migrated.fees_unit` declares what the number means and
    the raw value is always reported alongside it.
    """
    cfg = cfg or load_config()
    unit = str(((cfg.get("phase_gates") or {}).get("migrated") or {}).get("fees_unit", "sol")).lower()
    out = {"ok": False, "mint": mint, "value": None, "unit": unit, "source": None,
           "reason": ""}
    # 1) in case a future payload carries it inline, take it there first
    try:
        info = token_info(mint)
        inline = _nested_field(info, "total_fee", "fees_paid", "total_fees",
                               "fee_sol", "data.total_fee")
        if inline is not None and fnum(inline) is not None:
            out.update(ok=True, value=fnum(inline), source="token_info")
            return out
        creator = creator or _nested_field(info, "dev.creator_address", "creator_address",
                                           "creator")
    except Exception as e:
        out["reason"] = f"token_info failed: {type(e).__name__}"[:160]
    if not creator:
        out["reason"] = out["reason"] or "no creator address to query the launch book"
        return out
    # 2) the dev's created-tokens book (capped ~101 newest rows)
    ckey = f"created_tokens:{creator}"
    rows = _cache_get(ckey)
    try:
        if rows is None:
            res = _run_addr(["portfolio", "created-tokens", "--chain",
                             cfg.get("chain", "sol")], "portfolio/created-tokens", creator)
            data = res.get("data", res) if isinstance(res, dict) else {}
            rows = data.get("tokens") if isinstance(data, dict) else None
            if not isinstance(rows, list):
                rows = _extract_items(res, "created-tokens")
            _cache_set(ckey, rows, ttl=900)
    except Exception as e:
        out["reason"] = f"created-tokens failed: {type(e).__name__}: {e}"[:200]
        return out
    for row in rows or []:
        if not isinstance(row, dict):
            continue
        if str(row.get("token_address") or row.get("address") or "") == str(mint):
            v = fnum(row.get("total_fee", row.get("coin_creator_fee")))
            if v is not None:
                out.update(ok=True, value=v, source="portfolio/created-tokens")
                return out
            out["reason"] = "the launch-book row for this mint carries no total_fee"
            return out
    out["reason"] = out["reason"] or "mint not in the dev's created-tokens book"
    return out


def top_trader_identity(mint: str, limit: int = 20) -> dict:
    try:
        cfg = load_config()
        res = _run_addr(["token", "traders", "--chain", cfg.get("chain", "sol")],
                        "token/traders", mint, ["--limit", str(limit)])
        traders = res.get("data", []) if isinstance(res, dict) else (res if isinstance(res, list) else [])
        smart = whale = kol = sniper = bundler = 0
        for t in traders:
            # Read BOTH tag lists: whale lives in maker_token_tags, KOL is
            # spelled `renowned` in tags. The old code looked for "whale"/"kol"
            # inside `tags` only, so it reported 0/0/0 for every token.
            tags = _wallet_tags(t)
            if any("smart" in x for x in tags):
                smart += 1
            if any("whale" in x for x in tags):
                whale += 1
            if any(x in ("kol", "renowned") for x in tags):
                kol += 1
            if "sniper" in tags:
                sniper += 1
            if "bundler" in tags:
                bundler += 1
        return {"n": len(traders), "smart": smart, "whale": whale, "kol": kol,
                "sniper": sniper, "bundler": bundler}
    except Exception:
        return {"n": 0, "smart": 0, "whale": 0, "kol": 0, "sniper": 0, "bundler": 0}


def get_live_price(mint: str) -> Optional[float]:
    md = get_market_data(mint)
    return md.get("price_usd")


def get_live_market_cap(mint: str) -> Optional[float]:
    md = get_market_data(mint)
    mcap = md.get("signals", {}).get("market_cap_usd")
    if mcap:
        return float(mcap)
    # Fallback: price × circulating_supply
    price = md.get("price_usd", 0.0)
    if price and price > 0:
        # get_market_data already computes mcap from price×supply in data dict
        # Try to get it from there, otherwise compute
        signals = md.get("signals", {})
        mcap2 = signals.get("market_cap_usd")
        if mcap2:
            return float(mcap2)
    return None
