#!/usr/bin/env python3
"""
ENZO - Unified Data Layer (GMGN Provider)
Handles GMGN endpoints, rate pacing, ban recovery, and standard normalization shapes.
"""
import json
import os
import re
import subprocess
import sys
import threading
import time
from datetime import datetime
from typing import Optional, Dict, Any, List

from enzo.core.config import load_config, GMGN_BAN_FILE_PATH
import enzo.core.db as db
import enzo.core.log as log

_LOGGER = log.get_logger("enzo.gmgn")

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
    """Read the configured request gap (ms) and expose it as seconds."""
    try:
        cfg = load_config()
        ms = float(((cfg.get("data_sources", {}) or {}).get("gmgn", {}) or {}).get("request_gap_ms", 350))
        return max(0.1, ms / 1000.0)
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


def _run(args, endpoint, timeout=40):
    """Run one gmgn-cli command; measure latency; handle bans with sleep+retry."""
    acquired = db.rl_acquire("gmgn", tokens_needed=1.0, rate_per_sec=0.8, min_gap_sec=_rl_min_gap(), max_wait_sec=45.0)
    if not acquired:
        raise RateLimited(f"{endpoint}: rate limited or banned")

    cmd = ["gmgn-cli"] + args
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
    return fnum(_get(d, "price", "price_usd", "current_price", "usd_price", "token_price", "last_price"))


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
    return fnum(_nested_field(info, "volume_24h", "v24h_usd", "volume_usd", "volume",
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
    v = _nested_field(info, "progress", "bonding_curve_progress", "token.progress",
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


def _buy_pressure(info):
    b = fnum(_nested_field(info, "buys_24h", "buys", "buy_count", "token.buys", "base.buys"))
    s = fnum(_nested_field(info, "sells_24h", "sells", "sell_count", "token.sells", "base.sells"))
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
    categories = gmgn_cfg.get("discovery", ["trenches", "trending", "smartmoney"])
    cat_items_map = {cat: [] for cat in categories}

    for cat in categories:
        try:
            res = _run(["market", cat, "--chain", ch, "--limit", "30"], f"market/{cat}")
            raw_items = res.get("completed", []) + res.get("near_completion", []) + res.get("new_creation", []) \
                if isinstance(res, dict) else res.get("data", {}).get("list", []) if isinstance(res, dict) else []
            for it in raw_items:
                normed = _norm_list_item(it, cat)
                mint = normed.get("mint") or normed.get("address")
                if mint:
                    cat_items_map[cat].append(normed)
                    if mint not in seen_mints:
                        seen_mints.add(mint)
                        items.append(normed)
        except Exception as e:
            _LOGGER.debug(f"GMGN category {cat} discovery error: {e}")

    for cat, cat_items in cat_items_map.items():
        if cat_items:
            _cache_set(f"discovery:{cat}", cat_items, ttl=25)
    _cache_set(ckey, items, ttl=25)
    return items


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
        "raw": it
    }


def token_info(mint: str) -> dict:
    ckey = f"info:{mint}"
    cached = _cache_get(ckey)
    if cached is not None:
        return cached

    try:
        cfg = load_config()
        res = _run(["token", "info", "--chain", cfg.get("chain", "sol"), "--address", mint], "token/info")
        data = res.get("data", {}) if isinstance(res, dict) and "data" in res else (res if isinstance(res, dict) else {})
        _cache_set(ckey, data, ttl=_CACHE_TTL["info"])
        return data
    except Exception:
        return {}


def token_security(mint: str) -> dict:
    ckey = f"security:{mint}"
    cached = _cache_get(ckey)
    if cached is not None:
        return cached

    try:
        cfg = load_config()
        res = _run(["token", "security", "--chain", cfg.get("chain", "sol"), "--address", mint], "token/security")
        data = res.get("data", {}) if isinstance(res, dict) and "data" in res else (res if isinstance(res, dict) else {})
        _cache_set(ckey, data, ttl=_CACHE_TTL["security"])
        return data
    except Exception:
        return {}


def _normalize_holders(data, limit: int = 20) -> list:
    """Normalize GMGN holder payloads (list or dict with 'holders'/'list'/'top_holders') into a canonical list."""
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
        pct = fnum(h.get("percent") or h.get("percentage") or h.get("share") or h.get("pct") or h.get("holder_percent"))
        if pct is not None:
            pct = pct / 100.0 if pct > 1.0 else pct  # normalize to fraction 0-1
        norm.append({
            "address": h.get("address") or h.get("owner") or h.get("wallet"),
            "pct": pct,
            "tags": h.get("tags") or [],
            "buy_pct": fnum(h.get("buy_percent") or h.get("buy_pct")),
            "sell_pct": fnum(h.get("sell_percent") or h.get("sell_pct")),
            "profit_ratio": fnum(h.get("profit_ratio") or h.get("avg_profit_ratio") or h.get("profit_multiple")),
            "wallet_age_days": fnum(h.get("wallet_age_days") or h.get("wallet_age") or h.get("age_days")),
            "is_contract": bool(h.get("is_contract") or h.get("contract")),
            "raw": h,
        })
    return norm[:limit]


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
        res = _run(["token", "holders", "--chain", cfg.get("chain", "sol"), "--token", mint, "--limit", str(limit)], "token/holders")
        data = res.get("data", {}) if isinstance(res, dict) and "data" in res else (res if isinstance(res, dict) else {})
        holders = _normalize_holders(data, limit=limit)
        top10 = sum(h["pct"] for h in holders[:10] if h["pct"] is not None)
        top1 = holders[0]["pct"] if holders and holders[0]["pct"] is not None else None
        norm = {
            "ok": True,
            "top1_pct": top1,
            "top10_pct": top10 if top10 else None,
            "holder_count": len(holders),
            "holders": holders,
            "raw": data,
        }
        _cache_set(ckey, norm, ttl=_CACHE_TTL["holders"])
        return norm
    except Exception:
        return {"ok": False, "top1_pct": None, "top10_pct": None, "holder_count": 0, "holders": []}


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
            c = 0
            for h in holders:
                tag_str = " ".join(str(t).lower() for t in (h.get("tags") or []))
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
    except Exception:
        return {"ok": False, "stats": {}}


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
        args = ["market", "kline", "--chain", cfg.get("chain", "sol"), "--token", mint, "--resolution", resolution]
        if from_ts:
            args += ["--from-ts", str(int(from_ts))]
        if to_ts:
            args += ["--to-ts", str(int(to_ts))]
        res = _run(args, "market/kline")
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

    sym = merged.get("symbol") or "UNKNOWN"
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
    mcap = mcap or 0.0
    vol = _volume_24h(merged) or 0.0
    liq = fnum(merged.get("liquidity")) or fnum(_get(merged, "usd_liquidity")) or 0.0
    prog = _info_progress(merged)

    data = {
        "token_symbol": sym,
        "price_usd": price,
        "phase": _phase_from_progress(prog),
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
            "buys": fnum(_get(merged, "buys", "buys_24h")),
            "sells": fnum(_get(merged, "sells", "sells_24h")),
        }
    }
    _cache_set(ckey, data, ttl=_CACHE_TTL["market_data"])
    return data


def top_trader_identity(mint: str, limit: int = 20) -> dict:
    try:
        cfg = load_config()
        res = _run(["token", "traders", "--chain", cfg.get("chain", "sol"), "--address", mint, "--limit", str(limit)], "token/traders")
        traders = res.get("data", []) if isinstance(res, dict) else (res if isinstance(res, list) else [])
        smart, whale, kol = 0, 0, 0
        for t in traders:
            tags = [x.lower() for x in t.get("tags", [])]
            if any("smart" in x for x in tags):
                smart += 1
            if any("whale" in x for x in tags):
                whale += 1
            if any("kol" in x for x in tags):
                kol += 1
        return {"n": len(traders), "smart": smart, "whale": whale, "kol": kol}
    except Exception:
        return {"n": 0, "smart": 0, "whale": 0, "kol": 0}


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
