#!/usr/bin/env python3
"""
ENZO - Analysis Engine (Weighted-Confidence, Multi-Axis Decision Matrix)
Evaluates 6 independent axes:
  - security (30%)
  - wallet_behavior (20%)
  - dev_behavior (20%)
  - momentum (15%)
  - market_structure (10%)
  - liquidity (5%)
"""
import json
import sys
from typing import Dict, Any, Optional

from enzo.core.config import load_config, clamp
from enzo.analyzers import security, dev, wallet, market_structure
from enzo.providers import gmgn
from enzo.core import learn


def _liquidity_axis(sig: dict, sec: dict, ma: dict) -> dict:
    liq = float(sig.get("liquidity_usd", sec.get("liquidity", 0)) or 0)
    min_liq = float(ma.get("min_liquidity", 150))
    if liq >= min_liq:
        score = clamp((liq / max(min_liq, 1.0)) * 20)
    else:
        score = 0
    return {
        "score": int(score),
        "flags": [f"liquidity=${liq:,.0f} (min ${min_liq:,.0f})"],
        "detail": {"liquidity": liq, "min_liquidity": min_liq}
    }


def _momentum_axis(sig: dict, ma: dict) -> dict:
    pc1h = float(sig.get("price_change_1h") or 0)
    pc24 = float(sig.get("price_change_24h") or 0)
    pc5m = sig.get("price_change_5m")
    pc5m = float(pc5m) if pc5m is not None else None
    base = clamp(50 + pc1h * 5 + pc24 * 1)

    if pc5m is not None:
        base = clamp(base + pc5m * 2)

    bp_raw = sig.get("buy_pressure_pct")
    if bp_raw is not None:
        buy_pressure = float(bp_raw)
        pressure_score = clamp((buy_pressure - 40) * 2)
        score = clamp(0.6 * base + 0.4 * pressure_score)
        flags = [f"1h={pc1h:+.2f}% 24h={pc24:+.2f}% buy_pressure={buy_pressure:.1f}%"]
    else:
        score = base
        flags = [f"1h={pc1h:+.2f}% 24h={pc24:+.2f}% (buy_pressure n/a)"]

    if pc5m is not None:
        flags.append(f"5m={pc5m:+.2f}%")

    smart = sig.get("smart_degen_count")
    if smart is not None:
        smart = float(smart)
        if smart >= 10:
            score = clamp(score + 10)
            flags.append(f"smart_degen={int(smart)}")
        elif smart >= 3:
            score = clamp(score + 5)
            flags.append(f"smart_degen={int(smart)}")

    hot = sig.get("hot_level")
    if hot is not None:
        hot = float(hot)
        if hot >= 3:
            score = clamp(score + 5)
            flags.append(f"hot_level={int(hot)}")

    b = sig.get("buys")
    s = sig.get("sells")
    if b is not None and s is not None and (b + s) > 0:
        ratio = float(b) / (float(b) + float(s))
        if ratio >= 0.6:
            score = clamp(score + 5)
            flags.append(f"buy_ratio={ratio:.0%}")
        elif ratio <= 0.4:
            score = clamp(score - 8)
            flags.append(f"buy_ratio={ratio:.0%} (sell-heavy)")

    return {
        "score": int(score),
        "flags": flags,
        "detail": {
            "pc1h": pc1h,
            "pc24": pc24,
            "pc5m": pc5m,
            "buy_pressure": bp_raw,
            "smart_degen_count": smart,
            "hot_level": hot
        }
    }


def analyze(merged: dict, config: dict = None) -> dict:
    cfg = config or load_config()
    ma = cfg.get("market_analysis", {}) or {}
    xs = cfg.get("exit_strategy", {}) or {}
    wc = cfg.get("weighted_confidence", {}) or {}

    weights = {
        "security": float(wc.get("security", 30)),
        "wallet_behavior": float(wc.get("wallet_behavior", 20)),
        "dev_behavior": float(wc.get("dev_behavior", 20)),
        "momentum": float(wc.get("momentum", 15)),
        "market_structure": float(wc.get("market_structure", 10)),
        "liquidity": float(wc.get("liquidity", 5)),
    }

    sec = merged.get("security", {}) or {}
    sig = merged.get("signals", {}) or {}
    mint = sec.get("mint_address") or merged.get("mint")
    scam_points = len(sec.get("hard_reject") or [])
    min_liq = float(ma.get("min_liquidity", 150))
    min_vol = float(ma.get("min_volume", 50))
    min_holders = float(ma.get("min_holders", 0) or 0)
    min_mcap = float(ma.get("min_market_cap", 0) or 0)
    min_bp = float(ma.get("min_buy_pressure", 45))
    min_conf = float(ma.get("min_confidence_score", 55))
    max_holder_pct = float(ma.get("max_holder_percentage", 100) or 100)

    liq_raw = sig.get("liquidity_usd")
    if liq_raw is None:
        liq_raw = sec.get("liquidity")
    liq = float(liq_raw or 0)
    vol_raw = sig.get("volume_24h_usd")
    has_volume = vol_raw is not None
    vol = float(vol_raw) if has_volume else 0.0
    bp_raw = sig.get("buy_pressure_pct")
    has_pressure = bp_raw is not None
    buy_pressure = float(bp_raw) if has_pressure else None
    pc1h = float(sig.get("price_change_1h") or 0)
    market_cap = sig.get("market_cap_usd")
    holder_count = sec.get("holder_count")
    top_holder_pct = sec.get("top_holder_pct")

    rejected = []
    supporting = []

    # Independent Axis Scoring
    sec_axis = security.security_axis(sec, cfg)
    liq_axis = _liquidity_axis(sig, sec, ma)
    mom_axis = _momentum_axis(sig, ma)
    wal_axis = wallet.wallet_behavior_analysis(mint, merged, cfg)
    dev_axis = dev.dev_analysis(mint, merged, cfg)
    ms_axis = market_structure.analyze(mint, merged, cfg)

    axes = {
        "security": sec_axis["score"],
        "wallet_behavior": wal_axis["score"],
        "dev_behavior": dev_axis["score"],
        "momentum": mom_axis["score"],
        "market_structure": ms_axis["score"],
        "liquidity": liq_axis["score"],
    }

    # Axis availability: axes without real data are EXCLUDED from the weighted
    # average instead of contributing a free neutral 50 (which systematically
    # inflated confidence toward BUY when providers/analyzers were silent).
    axis_available = {
        "security": bool(sec_axis.get("available", sec_axis.get("security_status") is not None)),
        "wallet_behavior": bool(wal_axis.get("available", False)),
        "dev_behavior": bool(dev_axis.get("available", False)),
        "momentum": True,  # derived from signals (always present after DATA_OK)
        "market_structure": bool(ms_axis.get("available", False)),
        "liquidity": True,  # real gate — 0 liquidity rejects
    }

    # PumpDev deep penalties
    pd = merged.get("pumpdev_deep") or merged.get("pump_deep") or {}
    if pd:
        ds = cfg.get("data_sources", {}) or {}
        p_cfg = ds.get("pumpdev", {}) or ds.get("pump_advanced", {}) or {}
        pen = p_cfg.get("penalties", {}) or {}
        p_reasons = []
        bp = pd.get("bundler_pct")
        if bp is not None and bp >= float(pen.get("bundler_penalty_threshold", 0.30)):
            cut = float(pen.get("bundler_penalty", 25))
            axes["dev_behavior"] = max(0, axes["dev_behavior"] - cut)
            p_reasons.append(f"pumpdev:bundler {bp:.0%} (-{cut:.0f})")

        tr = pd.get("twitter_reuse")
        if tr is not None and tr >= float(pen.get("twitter_reuse_penalty_threshold", 3)):
            cut = float(pen.get("twitter_reuse_penalty", 20))
            axes["dev_behavior"] = max(0, axes["dev_behavior"] - cut)
            p_reasons.append(f"pumpdev:twitter_reuse={int(tr)} (-{cut:.0f})")

        if pd.get("is_banned"):
            cut = float(pen.get("banned_penalty", 30))
            axes["security"] = max(0, axes["security"] - cut)
            p_reasons.append(f"pumpdev:banned (-{cut:.0f})")

        if p_reasons:
            supporting.append("; ".join(p_reasons))

    # Hard Gates
    security_status = sec_axis["security_status"]
    hard_reject = sec_axis.get("hard_reject") or []
    for r in hard_reject:
        rejected.append(f"SECURITY: {r}")

    if security_status == "DANGEROUS" and not hard_reject:
        rejected.append("SECURITY: DANGEROUS (deep rug signals)")
    if liq_raw is None:
        rejected.append("Liquidity UNKNOWN (provider returned no liquidity field)")
    elif liq < min_liq:
        rejected.append(f"Liquidity ${liq:,.0f} < min ${min_liq:,.0f}")
    else:
        supporting.append(f"Liquidity ${liq:,.0f} >= min ${min_liq:,.0f}")

    # ── Data availability gate (runs BEFORE the quality gates) ──────────────
    # If the provider returned nothing, say so. Reporting "Market cap $0 < min"
    # for a token whose data never arrived is what made 1,649 rejections look
    # like bad tokens instead of a broken/rate-limited data source.
    dq = merged.get("data_quality") or {}
    missing = [n for n, v in (("market_cap_usd", market_cap), ("liquidity_usd", liq_raw),
                              ("volume_24h_usd", vol_raw), ("price_usd", sig.get("price_usd")),
                              ("buy_pressure_pct", bp_raw)) if v is None]
    for extra in (dq.get("missing") or []):
        if extra not in missing:
            missing.append(extra)
    no_data = len(missing) >= 3 or (dq.get("rate_limited") and market_cap is None)
    if no_data:
        why = "GMGN rate limit active" if dq.get("rate_limited") else "provider returned no usable fields"
        rejected.append(f"NO_MARKET_DATA: {why} (missing: {', '.join(missing[:5])})")
        # Copy, do not mutate: hard_reject is the list object inside sec_axis.
        hard_reject = list(hard_reject) + ["NO_MARKET_DATA"]

    # Real Quality Gates (reject when the data is present and below the yaml floor)
    if min_mcap and market_cap is not None and float(market_cap) < min_mcap:
        rejected.append(f"Market cap ${float(market_cap):,.0f} < min ${min_mcap:,.0f}")
    elif market_cap is not None:
        supporting.append(f"Market cap ${float(market_cap):,.0f} >= min ${min_mcap:,.0f}")

    if has_volume and vol < min_vol:
        rejected.append(f"Volume24h ${vol:,.0f} < min ${min_vol:,.0f}")
    elif has_volume:
        supporting.append(f"Volume24h ${vol:,.0f} >= min ${min_vol:,.0f}")

    if min_holders and holder_count is not None and holder_count < min_holders:
        rejected.append(f"Holders {holder_count} < min {min_holders}")
    elif holder_count is not None:
        supporting.append(f"Holders {holder_count} >= min {min_holders}")

    if has_pressure and buy_pressure is not None and buy_pressure < min_bp:
        rejected.append(f"Buy pressure {buy_pressure:.1f}% < {min_bp:.0f}%")
    elif has_pressure and buy_pressure is not None:
        supporting.append(f"Buy pressure {buy_pressure:.1f}% >= {min_bp:.0f}%")

    if pc1h >= 0:
        supporting.append(f"1h momentum {pc1h:+.2f}%")

    # Weighted Confidence Computation — only over axes that actually have data
    active_axes = [k for k in axes if axis_available[k]]
    total_w = sum(weights[k] for k in active_axes) or 1.0
    conf = clamp(round(sum(axes[k] * weights[k] for k in active_axes) / total_w)) if active_axes else 0

    # Apply self-calibrating confidence bias from the learning engine
    # (respects apply_weight_adjustments=false — weights are never adjusted).
    lcfg = cfg.get("learning", {}) or {}
    if lcfg.get("enabled", True):
        try:
            conf = learn.calibrate_confidence(conf)
        except Exception:
            pass

def fingerprint_signals(wal_axis: dict, dev_axis: dict, cfg: dict) -> dict:
    """Absolute rug fingerprints observable at FIRST look - layer 1.

    Delta-based dev analysis is blind at first observation (there is nothing to
    compare against yet), which is exactly when memecoin buys happen. These
    signals need no history: bundled buys, freshly created sybil wallets,
    insiders selling right now, and serial-rugger factories are all readable
    from the very first snapshot. An organic community launch does not carry
    them, so vetoing on them costs no legitimate entry.

    Returns {"vetoes": [...], "flags": [...], "stats": {...}} where `flags` are
    the same signals at `soft_flag_ratio` of their veto threshold - not enough
    to refuse the trade, but enough to put it on the tighter early stop (layer 3).
    """
    rp = cfg.get("rug_protection") or {}
    out = {"vetoes": [], "flags": [], "stats": {}}
    if not bool(rp.get("fingerprints_enabled", True)):
        return out
    w = wal_axis or {}
    detail = w.get("detail") or {}
    stats = {
        "bundlers_top20": w.get("bundler_count"),
        "snipers_top20": w.get("sniper_count"),
        "rats_top20": w.get("rat_count"),
        "avg_wallet_age_days": detail.get("avg_wallet_age_days"),
        "top10_cur_sells": detail.get("top10_cur_sells"),
    }
    out["stats"] = {k: v for k, v in stats.items() if v is not None}
    ratio = float(rp.get("soft_flag_ratio", 0.5))

    for name, val, veto_at in (
        ("bundlers_top20", stats["bundlers_top20"], float(rp.get("veto_bundlers_top20", 6))),
        ("snipers_top20", stats["snipers_top20"], float(rp.get("veto_snipers_top20", 8))),
        ("rats_top20", stats["rats_top20"], float(rp.get("veto_rats_top20", 5))),
        ("top10_cur_sells", stats["top10_cur_sells"], float(rp.get("veto_top10_cur_sells", 25))),
    ):
        if val is None or veto_at <= 0:
            continue
        if float(val) >= veto_at:
            out["vetoes"].append(
                f"RUG-FINGERPRINT: {name}={int(val)} >= {int(veto_at)} "
                f"(coordinated/bundled buying fingerprint)")
        elif float(val) >= veto_at * ratio:
            out["flags"].append(f"{name}={int(val)}")

    age, age_veto = stats["avg_wallet_age_days"], float(rp.get("veto_avg_wallet_age_days", 3.0))
    if age is not None and age_veto > 0:
        if float(age) < age_veto:
            out["vetoes"].append(
                f"RUG-FINGERPRINT: avg top-holder wallet age {float(age):.1f}d < "
                f"{age_veto:.0f}d (freshly created sybil wallets)")
        elif float(age) < age_veto * 2:
            out["flags"].append(f"avg_wallet_age={float(age):.1f}d")

    dd = (dev_axis or {}).get("detail") or {}
    created, open_ratio = dd.get("creator_created_count"), dd.get("open_ratio")
    c_veto = float(rp.get("veto_factory_created", 50))
    o_veto = float(rp.get("veto_factory_open_ratio", 0.03))
    if created is not None and open_ratio is not None and c_veto > 0:
        if float(created) >= c_veto and float(open_ratio) < o_veto:
            out["vetoes"].append(
                f"RUG-FINGERPRINT: serial factory - created {int(created)} tokens, "
                f"only {float(open_ratio) * 100:.1f}% still open")
    return out


def rug_rejection(dev_axis: dict) -> str | None:
    """Return a rejection reason when the developer's behaviour IS the rug.

    A dev wallet that closed its entire position is not a trade, it is a request
    for exit liquidity. The dev axis score alone cannot carry this signal: it is
    one of six weighted axes, so five healthy axes outvote a dead dev. The CAT
    trade of 2026-09-04 bought with dev_behavior=0 (DEV_SOLD_ALL) because the
    weighted confidence still cleared the threshold at 56.15.
    """
    if not dev_axis.get("available"):
        return None
    events = [str(e) for e in (dev_axis.get("events") or [])]
    if "DEV_SOLD_ALL" in events:
        return ("RUG: developer wallet CLOSED its entire position (DEV_SOLD_ALL) - "
                "no holder is left to support the price")
    try:
        score = float(dev_axis.get("score") or 0.0)
    except (TypeError, ValueError):
        score = 0.0
    if score <= 0 and events:
        return (f"RUG: dev-behaviour score {score:.0f} with events: "
                + ", ".join(events[:3]))
    return None


    # Machine Learning Features
    features = {
        "security_ok": security_status == "SAFE",
        "bundle_risk": any("BUNDLE" in f or "CONCENTRATED" in f for f in sec.get("security_flags", [])),
        "dev_holding": "DEV_HOLDING" in dev_axis.get("events", []),
        "dev_selling": "DEV_SELLING" in dev_axis.get("events", []) or "DEV_SOLD_ALL" in dev_axis.get("events", []),
        "dev_buying": "DEV_BUYING_MORE" in dev_axis.get("events", []),
        "dev_factory": any("DEV_FACTORY" in e for e in dev_axis.get("events", [])),
        "wallet_diversity_high": (wal_axis.get("wallet_diversity_score") or 0) >= 70,
        "smart_money_in": (wal_axis.get("smart_count") or 0) > 0,
        "whale_in": (wal_axis.get("whale_count") or 0) > 0,
        "market_structure_growing": ms_axis["score"] >= 60,
        "momentum_positive": pc1h >= 0,
        "liquidity_ok": liq >= min_liq,
    }

    # ── Rug gate: dev dumping is a veto, not one vote among six ──────────
    _rug = rug_rejection(dev_axis)
    if _rug:
        rejected.append(_rug)

    # ── Layer 1: absolute fingerprints, readable at first look ───────────
    _fp = fingerprint_signals(wal_axis, dev_axis, cfg)
    rejected.extend(_fp["vetoes"])

    # Decision Logic
    hard_fail = bool(rejected)
    if hard_fail:
        decision = "IGNORE"
        # Name the ACTUAL reason. "Failed security/liquidity gate" was shown for
        # every rejection — including a provider that simply returned no data —
        # which is why the audit log could not tell a risky token from a broken
        # data source.
        if "NO_MARKET_DATA" in hard_reject:
            nd = next((r for r in rejected if str(r).startswith("NO_MARKET_DATA")), "NO_MARKET_DATA")
            decision = "DATA_ERROR"
            reason = f"Market data unavailable — {nd}"
        else:
            first = str(rejected[0])[:160]
            extra = f" (+{len(rejected) - 1} more)" if len(rejected) > 1 else ""
            reason = f"Rejected by quality gate: {first}{extra}"
    elif conf >= min_conf and security_status in ("SAFE", "WARNING"):
        decision = "BUY"
        reason = "No security risk; weighted confidence above threshold."
    elif security_status in ("SAFE", "WARNING"):
        decision = "WAIT"
        reason = "No security risk but weighted confidence below threshold."
    else:
        decision = "IGNORE"
        reason = "DANGEROUS security status (clear rug risk)."

    price = float(sec.get("price") or merged.get("price_usd", 0) or 0)
    sl_pct = float(xs.get("stop_loss_percentage", 50.0))
    tp_pct = float(xs.get("take_profit_percentage", 150.0))
    entry = price if decision == "BUY" else None
    entry_mc = market_cap if (decision == "BUY" and market_cap) else None
    stop_loss_mc = round(market_cap * (1 - sl_pct / 100), 2) if entry_mc else None
    take_profit_mc = round(market_cap * (1 + tp_pct / 100), 2) if entry_mc else None

    axis_scores = {
        "security": sec_axis,
        "wallet_behavior": wal_axis,
        "dev_behavior": dev_axis,
        "momentum": mom_axis,
        "market_structure": ms_axis,
        "liquidity": liq_axis,
    }

    _dev_ev = ", ".join(dev_axis.get("events", [])) or "n/a"
    _wallet_cls = wal_axis.get("classification", "n/a")
    decision_breakdown = (
        f"الأمان={axes['security']} | المحافظ={axes['wallet_behavior']} | "
        f"المطور={axes['dev_behavior']} | الزخم={axes['momentum']} | "
        f"هيكل_السوق={axes['market_structure']} | السيولة={axes['liquidity']} "
        f"|| المطور: {_dev_ev} | تصنيف_المحافظ: {_wallet_cls} | ثقة_نهائية: {conf}"
    )

    return {
        "token_symbol": merged.get("token_symbol") or "UNKNOWN",
        "mint_address": mint,
        "decision": decision,
        "confidence_score": conf,
        "weighted_confidence": conf,
        "opportunity_score": conf,
        "scam_score": scam_points,
        "risk_score": 50,
        "expected_roi": f"{tp_pct:.1f}%" if decision == "BUY" else None,
        "expected_loss": f"{sl_pct:.1f}%" if decision == "BUY" else None,
        "risk_reward_ratio": f"{tp_pct/sl_pct:.1f}:1" if decision == "BUY" else None,
        "entry_price": entry,
        "entry_market_cap": entry_mc,
        "current_price": price if price else None,
        "current_market_cap": market_cap,
        "stop_loss_mc": stop_loss_mc,
        "take_profit_mc": take_profit_mc,
        "estimated_holding_time": f"{xs.get('max_holding_time_hours', 48)}h",
        "decision_reason": reason,
        "supporting_signals": supporting,
        "rug_flags": _fp["flags"],
        "rug_fingerprints": _fp["stats"],
        "rejected_signals": rejected,
        "security_status": security_status,
        "token_type": sec_axis.get("token_type"),
        "hard_reject": hard_reject,
        "axis_scores": axis_scores,
        "weights_used": weights,
        "features": features,
        "decision_breakdown": decision_breakdown,
        "market_cap_usd": market_cap,
    }


def run_pipeline(mint: str, pump_card: dict = None) -> dict:
    """Convenience full pipeline runner for a single mint."""
    try:
        md = gmgn.get_market_data(mint)
        if not md or not md.get("signals"):
            return {
                "decision": "DATA_ERROR",
                "token_symbol": "UNKNOWN",
                "mint_address": mint,
                "confidence_score": 0,
                "decision_reason": "No market data received from provider / rate limited",
                "hard_reject": ["DATA_UNAVAILABLE"]
            }

        sec = security.security_scan(mint)

        pumpdev_deep = None
        if pump_card:
            try:
                from enzo.providers import pump as pump_provider
                if "pumpdev_deep" not in pump_card and "pump_deep" not in pump_card:
                    pump_card = pump_provider.enrich_survivor(pump_card)
                pumpdev_deep = pump_card.get("pumpdev_deep") or pump_card.get("pump_deep")
            except Exception:
                pass

        merged = {
            "mint": mint,
            "token_symbol": md.get("token_symbol"),
            "price_usd": md.get("price_usd"),
            "signals": md.get("signals"),
            "phase": md.get("phase"),
            "security": sec,
            "pumpdev_deep": pumpdev_deep,
            "data_sources_used": ["GMGN"] + (["PUMPDEV"] if pumpdev_deep else []),
        }
        return analyze(merged)
    except Exception as e:
        return {
            "decision": "ANALYSIS_ERROR",
            "token_symbol": "UNKNOWN",
            "mint_address": mint,
            "confidence_score": 0,
            "decision_reason": f"Pipeline analysis error: {e}",
            "hard_reject": ["EXCEPTION"]
        }
