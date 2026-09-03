#!/usr/bin/env python3
"""
ENZO - Wallet Behavior & Quality Analysis (Axis Module)
Evaluates top holders, concentration, wallet tags (smart/whale/bundler/sniper/rat),
and dumping/accumulation behavior.
"""
from typing import Dict, Any, Optional

from enzo.core.config import load_config, clamp
from enzo.providers import gmgn
from enzo.analyzers import security


def _buyer_growth(sec: dict) -> Optional[float]:
    uw = sec.get("unique_wallet_5m")
    uwh = sec.get("unique_wallet_history_5m")
    if isinstance(uw, (int, float)) and isinstance(uwh, (int, float)) and uwh:
        return round((uw / uwh - 1) * 100, 1)
    return None


def _classify(score: float, diversity: Optional[float], smart: Optional[int], bundler: Optional[int], dumping: bool) -> str:
    if score >= 80 and (diversity is None or diversity >= 70) and not bundler:
        return "خبيرة (Expert-like)"
    if score >= 65:
        return "طبيعية (Normal)"
    if bundler or (smart is not None and smart == 0 and diversity is not None and diversity < 40):
        return "باندرر/مشبوهة (Bundler/Suspicious)"
    if dumping:
        return "ضغط بيع (Dumping)"
    if diversity is not None and diversity < 40:
        return "مشبوهة (Concentrated/Suspicious)"
    if score < 40:
        return "مشبوهة (Suspicious)"
    return "غير معروفة (Unknown)"


def wallet_behavior_analysis(mint: str, merged: dict = None, config: dict = None) -> dict:
    cfg = config or load_config()
    wb = cfg.get("wallet_behavior", {}) or {}
    neutral = float(wb.get("neutral_score", 50))
    raw_weights = wb.get("weights", {"diversity": 0.35, "concentration": 0.25, "growth": 0.15, "identity": 0.25})
    weight_sum = sum(raw_weights.values()) or 1.0
    w = {k: v / weight_sum for k, v in raw_weights.items()}

    sec = (merged or {}).get("security", {}) or {}
    sig = (merged or {}).get("signals", {}) or {}
    flags = []
    detail = {}

    dist = security.cached_holder_distribution(mint, cfg)
    top1 = None
    top10 = None
    if dist.get("ok"):
        # dist values are fractions (0-1); convert to percent for scoring/flags
        top1 = dist.get("top1_pct")
        top10 = dist.get("top10_pct")
        top1_pct_v = (top1 * 100.0) if top1 is not None else None
        top10_pct_v = (top10 * 100.0) if top10 is not None else None
        detail["top1_pct"] = top1_pct_v
        detail["top10_pct"] = top10_pct_v
        flags.append(f"top10={top10_pct_v}% top1={top1_pct_v}%")
        diversity = clamp(100 - top10_pct_v) if top10_pct_v is not None else None
        concentration = clamp(100 - top1_pct_v) if top1_pct_v is not None else None
    else:
        diversity = None
        concentration = None
        flags.append("holder_distribution=UNAVAILABLE")

    growth = _buyer_growth(sec)
    if growth is not None:
        detail["buyer_growth_5m_pct"] = growth
        flags.append(f"buyer_growth_5m={growth}%")

    # Deep holder analysis from GMGN
    deep = gmgn.deep_holder_analysis(mint, limit=20) if hasattr(gmgn, "deep_holder_analysis") else {}
    deep_ok = deep.get("ok")
    detail["deep_ok"] = deep_ok
    smart = whale = kol = bundler = sniper = rat = None
    dumping = False
    accumulating = 0
    avg_profit = None
    avg_age = None
    top10_cur_sells = 0

    if deep_ok:
        st = deep.get("stats") or {}
        smart = st.get("smart_count")
        whale = st.get("whale_count")
        kol = st.get("kol_count")
        bundler = st.get("bundler_count")
        sniper = st.get("sniper_count")
        rat = st.get("rat_count")
        dumping = bool(st.get("top10_dumping") and st.get("top10_dumping") >= (st.get("top10_accumulating") or 0))
        accumulating = st.get("top10_accumulating") or 0
        avg_profit = st.get("top10_avg_profit_ratio")
        avg_age = st.get("avg_wallet_age_days")
        top10_cur_sells = st.get("top10_cur_sells") or 0
        detail["smart_count"] = smart
        detail["whale_count"] = whale
        detail["kol_count"] = kol
        detail["bundler_count"] = bundler
        detail["sniper_count"] = sniper
        detail["rat_count"] = rat
        detail["top10_dumping"] = st.get("top10_dumping")
        detail["top10_accumulating"] = accumulating
        detail["top10_avg_profit_ratio"] = avg_profit
        detail["avg_wallet_age_days"] = avg_age

        id_flags = []
        if smart:
            id_flags.append(f"smart={smart}")
        if whale:
            id_flags.append(f"whale={whale}")
        if kol:
            id_flags.append(f"kol={kol}")
        if bundler:
            id_flags.append(f"bundler={bundler}")
        if sniper:
            id_flags.append(f"sniper={sniper}")
        if rat:
            id_flags.append(f"rat={rat}")
        if id_flags:
            flags.append("top20_tags: " + ", ".join(id_flags))
        if dumping:
            flags.append(f"top10_dumping={detail['top10_dumping']}>=acc={accumulating} (sell pressure)")
        elif accumulating:
            flags.append(f"top10_accumulating={accumulating}")
        if top10_cur_sells >= 20:
            flags.append(f"top10_cur_sells={top10_cur_sells} (dumping)")
        if avg_profit is not None:
            flags.append(f"top10_avg_profit={avg_profit:.2f}x")
        if avg_age is not None:
            flags.append(f"avg_wallet_age={avg_age:.0f}d")
    else:
        flags.append("deep_holders=UNAVAILABLE")

    # Sub-scores
    sub = {}
    if diversity is not None:
        sub["diversity"] = diversity
    if concentration is not None:
        sub["concentration"] = concentration
    if growth is not None:
        sub["growth"] = clamp(50 + growth * 2)

    identity = None
    if smart is not None or whale is not None or bundler is not None or rat is not None:
        identity = 50.0
        smart_v = smart or 0
        whale_v = whale or 0
        kol_v = kol or 0
        bundler_v = bundler or 0
        sniper_v = sniper or 0
        rat_v = rat or 0
        identity += min(smart_v * 8, 25) + min(whale_v * 6, 18) + min(kol_v * 6, 12)
        identity -= min(bundler_v * 14, 45)
        identity -= min(sniper_v * 10, 30)
        identity -= min(rat_v * 12, 30)
        if dumping:
            identity -= 20
        if top10_cur_sells >= 20:
            identity -= 15
        if accumulating >= 5 and not dumping:
            identity += min(accumulating, 10)
        if avg_age is not None and avg_age < 2 and bundler_v == 0:
            identity -= 8

        identity = clamp(identity)
        sub["identity"] = identity
        detail["identity_score"] = identity
        flags.append(f"identity={identity:.0f}")

    if sub:
        num = sum(sub[k] * w.get(k, 1) for k in sub)
        den = sum(w.get(k, 1) for k in sub)
        score = clamp(round(num / den))
    else:
        score = neutral
        flags.append("NO_WALLET_DATA -> neutral")

    classification = _classify(score, diversity, smart, bundler, dumping)
    flags.append(f"classification={classification}")

    top1_pct_v = (top1 * 100.0) if top1 is not None else None
    available = bool(dist.get("ok")) or bool(deep_ok) or growth is not None

    return {
        "score": score,
        "available": available,
        "wallet_diversity_score": diversity,
        "concentration_score": concentration,
        "experienced_wallet_ratio": None,
        "fresh_wallet_ratio": clamp(growth) if growth is not None else None,
        "sniper_wallet_ratio": clamp(top1_pct_v) if top1_pct_v is not None else None,
        "buyer_growth_5m_pct": growth,
        "smart_count": smart,
        "whale_count": whale,
        "kol_count": kol,
        "bundler_count": bundler,
        "sniper_count": sniper,
        "rat_count": rat,
        "top10_dumping": detail.get("top10_dumping"),
        "classification": classification,
        "flags": flags,
        "detail": detail,
    }
