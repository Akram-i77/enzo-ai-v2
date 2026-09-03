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


def _dev_reputation_penalty(created_count: Optional[float], open_ratio: Optional[float]) -> float:
    if not created_count:
        return 0.0
    penalty = 0.0
    if created_count >= 200:
        penalty += 45
    elif created_count >= 50:
        penalty += 30
    elif created_count >= 10:
        penalty += 15

    if open_ratio is not None:
        if open_ratio < 0.03:
            penalty += 25
        elif open_ratio < 0.10:
            penalty += 15
        elif open_ratio < 0.25:
            penalty += 5
    else:
        penalty += 8
    return min(penalty, 80.0)


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
        penalty = _dev_reputation_penalty(creator_created, open_ratio)
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
