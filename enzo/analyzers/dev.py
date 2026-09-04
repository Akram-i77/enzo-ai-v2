#!/usr/bin/env python3
"""
ENZO - Developer Behavior Analysis (Axis Module)
Evaluates developer holdings, distribution, serial-launcher reputation, and factory dev risk.
"""
import time
from typing import Dict, Any, Optional

from enzo.core.config import load_config, clamp
import enzo.core.cache as cache
from enzo.providers import gmgn
from enzo.analyzers import security


def _dev_reputation_penalty(created_count: Optional[float], open_ratio: Optional[float],
                            da: Optional[dict] = None) -> float:
    """Serial-launcher penalty, driven by `dev_behavior.factory_dev_*` config.

    Every number here used to be hardcoded while the YAML advertised knobs with
    the same names and the same values - so editing the YAML did nothing and the
    owner could not tell. The defaults below are byte-for-byte the old hardcoded
    numbers, so wiring this is behaviour-neutral until an owner changes a value;
    from then on the value is honoured. `da` is the dev_behavior config dict.
    """
    if not created_count:
        return 0.0
    da = da or {}

    def _n(key, fallback):
        try:
            return float(da.get(key, fallback))
        except Exception:
            return float(fallback)

    heavy_at = _n("factory_dev_heavy_created", 200)
    mid_at = _n("factory_dev_mid_created", 50)
    min_at = _n("factory_dev_min_created", 10)

    penalty = 0.0
    if created_count >= heavy_at:
        penalty += _n("factory_dev_heavy_penalty", 45)
    elif created_count >= mid_at:
        penalty += _n("factory_dev_mid_penalty", 30)
    elif created_count >= min_at:
        penalty += _n("factory_dev_penalty", 15)

    if open_ratio is not None:
        dead_at = _n("factory_dev_dead_open_ratio", 0.03)
        low_at = _n("factory_dev_low_open_ratio", 0.10)
        watch_at = _n("factory_dev_watching_open_ratio", 0.25)
        if open_ratio < dead_at:
            penalty += _n("factory_dev_dead_open_penalty", 25)
        elif open_ratio < low_at:
            penalty += _n("factory_dev_low_open_penalty", 15)
        elif open_ratio < watch_at:
            penalty += _n("factory_dev_watching_open_penalty", 5)
    else:
        penalty += _n("factory_dev_no_open_ratio_penalty", 8)
    return min(penalty, _n("factory_dev_penalty_cap", 80.0))


def dev_analysis(mint: str, merged: dict = None, config: dict = None) -> dict:
    cfg = config or load_config()
    da = cfg.get("dev_behavior", {}) or {}
    neutral = float(da.get("neutral_score", 50))
    sec = (merged or {}).get("security", {}) or {}
    sig = (merged or {}).get("signals", {}) or {}
    q = sec.get("quality", {}) or {}

    is_pump = sec.get("is_pumpfun") or sec.get("token_type") == "PUMP"
    dev_addr = sec.get("mint_authority")
    info = {}
    try:
        info = gmgn.token_info(mint)
    except Exception:
        pass

    creator = (info.get("dev") or {}).get("creator_address") or info.get("creator")
    if is_pump and creator:
        dev_addr = creator

    dev_share = q.get("dev_team_hold_rate")
    if dev_share is None:
        dev_share = sig.get("dev_team_hold_rate") or sig.get("creator_hold_rate")
    if dev_share is not None:
        dev_share = float(dev_share)
    else:
        dist = security.cached_holder_distribution(mint, cfg)
        # dist.top1_pct is already a fraction (0-1)
        dev_share = dist.get("top1_pct") if dist.get("ok") else None

    holder_count = sec.get("holder_count")
    liq = float(sig.get("liquidity_usd", sec.get("liquidity", 0)) or 0)

    creator_status = str((info.get("dev") or {}).get("creator_token_status") or info.get("creator_token_status") or "").lower()
    creator_close = bool(info.get("creator_close")) or "close" in creator_status
    creator_created = q.get("creator_created_count") or sig.get("creator_created_count")
    open_ratio = q.get("creator_created_open_ratio") or sig.get("creator_created_open_ratio")

    prev, _ = cache.get(f"dev:{mint}")
    events = []
    score = neutral

    if creator_close:
        events.append("DEV_SOLD_ALL")
        score += float(da.get("impact_dev_selling", -30)) * 2
    elif dev_share is not None:
        if prev and isinstance(prev, dict):
            pshare = prev.get("dev_share")
            pholders = prev.get("holder_count")
            if pshare is not None:
                if dev_share < pshare - float(da.get("sell_threshold_pct", 2)) / 100.0:
                    events.append("DEV_SELLING")
                    score += float(da.get("impact_dev_selling", -30))
                elif dev_share > pshare + float(da.get("buy_threshold_pct", 2)) / 100.0:
                    events.append("DEV_BUYING_MORE")
                    score += float(da.get("impact_dev_buying", 25))
                else:
                    events.append("DEV_HOLDING")
                    score += float(da.get("impact_dev_holding", 10))
            if pholders is not None and holder_count is not None and holder_count > pholders and dev_share < (pshare or 100):
                events.append("DEV_DISTRIBUTING")
                score += float(da.get("impact_dev_distributing", 8))
        else:
            # First observation only: record the hold but grant NO free +10
            # bias — reward only after positive follow-up data exists.
            events.append("DEV_HOLDING")
            score += 0.0
    else:
        events.append("DEV_UNKNOWN")

    flags_factory = None
    if creator_created is not None:
        penalty = _dev_reputation_penalty(creator_created, open_ratio, da)
        if penalty > 0:
            events.append(f"DEV_FACTORY({int(creator_created)}_created)")
            score -= penalty
            _or_s = f", open_ratio={open_ratio*100:.1f}%" if open_ratio is not None else ""
            flags_factory = f"dev_factory_penalty={penalty:.0f} (created={int(creator_created)}{_or_s})"
        else:
            flags_factory = f"dev_created={int(creator_created)}"

    cache.set(f"dev:{mint}", {
        "dev_share": dev_share,
        "holder_count": holder_count,
        "liq": liq,
        "ts": time.time(),
    }, ttl=float(da.get("track_ttl_sec", 1800)))

    score = clamp(round(score))
    dev_share_pct = (dev_share * 100) if dev_share is not None else None
    flags = [f"dev={dev_addr or 'unknown'}", f"dev_share={dev_share_pct}%"] + events
    if flags_factory:
        flags.append(flags_factory)

    return {
        "score": score,
        "available": dev_share is not None or creator_created is not None or creator_close,
        "dev_address": dev_addr,
        "dev_share": dev_share_pct,
        "events": events,
        "flags": flags,
        "detail": {
            "dev_share_pct": dev_share_pct,
            "holder_count": holder_count,
            "liquidity": liq,
            "creator_status": creator_status,
            "creator_created_count": creator_created,
            "open_ratio": open_ratio,
        },
    }
