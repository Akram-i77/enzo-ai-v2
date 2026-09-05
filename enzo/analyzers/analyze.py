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


def universe_and_phase_gates(merged: dict, sig: dict, cfg: dict, mint,
                             fetch_deep: bool = False) -> dict:
    """Owner-set entry universe: which coins ENZO is allowed to trade at all.

    Three independent vetoes, all hard (a veto is never one vote among six):

    1. PUMP V1 ONLY - `token_universe.pump_v1_only`. GMGN reports the launchpad
       two ways (`launchpad` = "pump", `launchpad_platform` = "Pump.fun");
       anything else (letsbonk, moonshot, fourmeme, bags, a raw Raydium pair) is
       refused. When the payload says nothing at all the coin is refused too
       (`reject_unknown_launchpad`), because "we could not tell" is not "it is a
       pump coin".

    2. PHASE MINIMUMS - a bonding-curve coin and a graduated coin are different
       animals, so they get different floors: pre-migration needs
       market cap >= 5000 and at least 10 sell transactions (proof the token can
       actually be sold by someone other than the dev); migrated needs
       market cap >= 10000 and total fees paid >= 2.5 SOL. The phase comes from
       `launchpad_status` (0 closed / 1 live / 2 migrated) with migrated_pool,
       complete_timestamp, progress and exchange as fallbacks. When no evidence
       exists the STRICTER of the two floors applies (`unknown_phase: strict`) -
       guessing "pre-migration" would let a graduated coin in at half the bar.

    3. EARLY-SNIPER FLOOD - the rug signature the owner described: right after
       the dev's create transaction the first wallets in are snipers with huge
       size, and the coin migrates one or two candles later. gmgn-cli has no
       trade tape, so this is reconstructed from `token traders`: every row
       carries start_holding_at (first-buy timestamp) and buy_volume_cur (USD),
       and GMGN tags launch buyers `sniper`. Sorting by entry time and taking the
       first N wallets gives the same window; if enough of them are snipers and
       their combined (or any single) buy exceeds the threshold, the coin is
       refused permanently.

    Cost control: gates 2 (fees) and 3 each need one extra gmgn-cli call, so they
    are only fetched when `fetch_deep` is True - the deep-scan path - and only
    after the free gates passed. A required check that was NOT fetched is a veto
    with an explicit *_NOT_CHECKED reason, never a silent pass: an unfetched
    safety check must not read as a satisfied one.
    """
    tu = cfg.get("token_universe") or {}
    pg = cfg.get("phase_gates") or {}
    sf = cfg.get("sniper_flood") or {}
    vetoes, supporting = [], []

    profile = merged.get("launchpad")
    if not isinstance(profile, dict):
        try:
            profile = gmgn.launchpad_profile({**(merged or {}), **(sig or {})})
        except Exception:
            profile = {"is_pump_v1": False, "launchpad_known": False, "phase": "unknown",
                       "platform": None, "launchpad": None, "reasons": [], "migrated": None}
    phase = profile.get("phase") or "unknown"

    detail = {
        "pump_v1": bool(profile.get("is_pump_v1")),
        "launchpad_known": bool(profile.get("launchpad_known")),
        "launchpad": profile.get("launchpad"),
        "platform": profile.get("platform"),
        "phase": phase,
        "phase_evidence": profile.get("reasons") or [],
        "progress_pct": profile.get("progress_pct"),
        "fees": None, "snipers": None,
    }

    # ── 1) launchpad universe ────────────────────────────────────────────────
    if bool(tu.get("pump_v1_only", True)):
        if not profile.get("launchpad_known"):
            if bool(tu.get("reject_unknown_launchpad", True)):
                vetoes.append("LAUNCHPAD_UNKNOWN: the payload carries no launchpad / "
                              "launchpad_platform, so this cannot be confirmed as a "
                              "standard pump.fun coin")
            else:
                supporting.append("launchpad unknown (allowed by config)")
        elif not profile.get("is_pump_v1"):
            vetoes.append(f"NOT_PUMP_V1: launchpad={profile.get('launchpad') or '?'} "
                          f"platform={profile.get('platform') or '?'} — only standard "
                          f"pump.fun (Pump V1) coins are traded")
        else:
            supporting.append(f"Pump V1 ({profile.get('platform') or profile.get('launchpad')})")

    # ── 2) phase-aware minimums ──────────────────────────────────────────────
    pre = pg.get("pre_migration") or {}
    mig = pg.get("migrated") or {}
    policy = str(pg.get("unknown_phase", "strict")).lower()
    if phase == "pre_migration":
        need_mcap, need_sells, need_fees, fees_unit = (
            fnum_local(pre.get("min_market_cap")), fnum_local(pre.get("min_sells")),
            None, None)
    elif phase == "migrated":
        need_mcap, need_sells, need_fees, fees_unit = (
            fnum_local(mig.get("min_market_cap")), fnum_local(mig.get("min_sells")),
            fnum_local(mig.get("min_total_fees")), str(mig.get("fees_unit", "sol")).lower())
    else:
        if policy == "reject":
            vetoes.append("PHASE_UNKNOWN: migration state cannot be determined and "
                          "phase_gates.unknown_phase=reject")
            need_mcap = need_sells = need_fees = None
            fees_unit = None
        elif policy == "pre":
            need_mcap, need_sells = fnum_local(pre.get("min_market_cap")), fnum_local(pre.get("min_sells"))
            need_fees, fees_unit = None, None
        else:                                   # strict: the higher of both bars
            a, b = fnum_local(pre.get("min_market_cap")), fnum_local(mig.get("min_market_cap"))
            need_mcap = max([x for x in (a, b) if x is not None] or [0]) or None
            need_sells = fnum_local(pre.get("min_sells"))
            need_fees, fees_unit = None, None
            detail["phase_note"] = "unknown phase — the stricter market-cap floor applied"

    mcap = sig.get("market_cap_usd")
    if need_mcap is not None:
        if mcap is None:
            vetoes.append(f"MCAP_UNKNOWN: {phase} requires market cap >= ${need_mcap:,.0f} "
                          f"but the payload has none")
        elif float(mcap) < float(need_mcap):
            vetoes.append(f"MCAP_BELOW_{phase.upper()}_MIN: ${float(mcap):,.0f} < "
                          f"${float(need_mcap):,.0f}")
        else:
            supporting.append(f"market cap ${float(mcap):,.0f} >= ${float(need_mcap):,.0f} ({phase})")

    sells = sig.get("sells")
    if sells is None:
        sells = sig.get("sells_24h")
    if need_sells is not None:
        if sells is None:
            vetoes.append(f"SELLS_UNKNOWN: {phase} requires >= {need_sells:,.0f} sell "
                          f"transactions but the payload has none")
        elif float(sells) < float(need_sells):
            vetoes.append(f"SELLS_BELOW_MIN: {float(sells):,.0f} sell transactions < "
                          f"{float(need_sells):,.0f} ({phase})")
        else:
            supporting.append(f"{float(sells):,.0f} sells >= {float(need_sells):,.0f} minimum")

    if need_fees is not None:
        fees = merged.get("fees_paid")
        if not isinstance(fees, dict):
            if fetch_deep and not vetoes and mint:
                try:
                    fees = gmgn.fees_paid(mint, creator=sig.get("creator_address"), cfg=cfg)
                except Exception as e:
                    fees = {"ok": False, "reason": f"{type(e).__name__}: {e}"[:160]}
            else:
                fees = None
        detail["fees"] = fees
        if not isinstance(fees, dict):
            vetoes.append(f"FEES_NOT_CHECKED: migrated coins require total fees >= "
                          f"{need_fees} {fees_unit.upper()} — the check was not run")
        elif not fees.get("ok") or fnum_local(fees.get("value")) is None:
            if bool(mig.get("require_known_fees", True)):
                vetoes.append(f"FEES_UNKNOWN: migrated coins require total fees >= "
                              f"{need_fees} {fees_unit.upper()} but GMGN did not report "
                              f"them ({str(fees.get('reason'))[:90]})")
            else:
                supporting.append("fees unavailable (allowed by require_known_fees=false)")
        elif float(fees.get("value")) < float(need_fees):
            vetoes.append(f"FEES_BELOW_MIN: {float(fees.get('value')):.3f} "
                          f"{str(fees.get('unit') or fees_unit).upper()} < {need_fees} "
                          f"{fees_unit.upper()}")
        else:
            supporting.append(f"fees paid {float(fees.get('value')):.3f} "
                              f"{str(fees.get('unit') or fees_unit).upper()} >= {need_fees}")

    # ── 3) early-sniper flood ────────────────────────────────────────────────
    if bool(sf.get("enabled", True)):
        report = merged.get("early_snipers")
        if not isinstance(report, dict):
            if fetch_deep and not vetoes and mint:
                try:
                    report = gmgn.early_sniper_report(mint, cfg=cfg)
                except Exception as e:
                    report = {"ok": False, "verdict": "unknown",
                              "reason": f"{type(e).__name__}: {e}"[:160]}
            else:
                report = None
        detail["snipers"] = report
        if not isinstance(report, dict):
            vetoes.append("SNIPER_FLOOD_NOT_CHECKED: the early-sniper window was never "
                          "examined for this coin")
        else:
            verdict = str(report.get("verdict") or "unknown")
            if verdict == "veto":
                vetoes.append(f"SNIPER_FLOOD_EARLY: {str(report.get('reason'))[:180]}")
            elif verdict == "unknown":
                if str(sf.get("on_unknown", "reject")).lower() == "reject":
                    vetoes.append(f"SNIPER_DATA_UNAVAILABLE: {str(report.get('reason'))[:160]}")
                else:
                    supporting.append("early-sniper window unavailable (allowed by config)")
            else:
                supporting.append(f"first {len(report.get('window') or [])} wallets: "
                                  f"{report.get('sniper_count')} sniper-tagged "
                                  f"(${report.get('sniper_total_usd') or 0:,.0f} combined)")

    return {"vetoes": vetoes, "supporting": supporting, "detail": detail,
            "phase": phase, "pump_v1": bool(profile.get("is_pump_v1"))}


def fnum_local(v):
    """float() that keeps None as None - a missing threshold is not zero."""
    if v is None or v == "":
        return None
    try:
        return float(v)
    except Exception:
        return None


def analyze(merged: dict, config: dict = None, fetch_deep: bool = False) -> dict:
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

    # ── Holder-concentration cap (market_analysis.max_holder_percentage) ──────
    # BUG: `max_holder_pct` was read above and NEVER used, and its intended
    # source `sec["top_holder_pct"]` has no producer anywhere in the codebase,
    # so the 10% cap the operator set silently never fired on any coin.
    # The real number is the top-1 WALLET share from the holder distribution —
    # already fetched and cached for the wallet/dev axes, so this costs no extra
    # gmgn-cli call. Pools/lockers/the bonding curve are excluded upstream
    # (gmgn.holder_distribution(exclude_curve_ata=True)) so a healthy migrated
    # token whose AMM vault holds 40% is not vetoed.
    conc_pct, conc_src = None, None
    if top_holder_pct is not None:
        _th = float(top_holder_pct)
        conc_pct, conc_src = (_th * 100.0 if _th <= 1.0 else _th), "security.top_holder_pct"
    else:
        try:
            _dist = security.cached_holder_distribution(mint, cfg) or {}
        except Exception as _e:                            # noqa: BLE001
            _dist = {}
            _LOGGER.warning("holder distribution unavailable for %s — %s", mint, _e)
        _t1 = _dist.get("top1_pct") if isinstance(_dist, dict) else None
        if _t1 is not None:
            conc_pct, conc_src = float(_t1) * 100.0, "holders.top1_wallet"
    if conc_pct is not None and max_holder_pct > 0:
        if conc_pct > max_holder_pct:
            rejected.append(f"HOLDER_CONCENTRATION: top wallet {conc_pct:.1f}% > max "
                            f"{max_holder_pct:.1f}% ({conc_src})")
        else:
            supporting.append(f"Top wallet {conc_pct:.1f}% <= max {max_holder_pct:.1f}%")
    elif conc_pct is None and max_holder_pct > 0:
        # Never pretend the cap was enforced: say it could not be measured.
        supporting.append(f"holder concentration UNKNOWN — max {max_holder_pct:.1f}% cap NOT enforced")

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

    # ── Layer 0: the entry universe (Pump V1 only, phase minimums, early
    # snipers). Runs AFTER the free gates so the two extra gmgn-cli calls it may
    # need are only spent on coins that already survived everything cheaper.
    _uni = universe_and_phase_gates(merged, sig, cfg, mint, fetch_deep=fetch_deep)
    rejected.extend(_uni["vetoes"])
    supporting.extend(_uni["supporting"])

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
        "universe": _uni["detail"],
        # the measured holder concentration behind the max_holder_percentage cap
        # (None = could not be measured, in which case the cap was NOT enforced)
        "top_holder_pct": round(float(conc_pct), 2) if conc_pct is not None else None,
        "top_holder_source": conc_src,
        "axis_scores": axis_scores,
        "weights_used": weights,
        "features": features,
        "decision_breakdown": decision_breakdown,
        "market_cap_usd": market_cap,
    }



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
            # launchpad_profile was already resolved by get_market_data against the
            # FULL payload (launchpad_status, migrated_pool, complete_timestamp,
            # exchange). Without carrying it here the gates would re-derive the
            # phase from `signals` alone, which does not have those fields, and
            # every coin would land in "unknown phase" - the stricter bar, applied
            # to coins whose phase GMGN actually reported.
            "launchpad": md.get("launchpad"),
            "security": sec,
            "pumpdev_deep": pumpdev_deep,
            "data_sources_used": ["GMGN"] + (["PUMPDEV"] if pumpdev_deep else []),
        }
        # fetch_deep=True: this is the deep-scan path, so the two gates that need
        # an extra gmgn-cli call (fees paid, early-sniper window) may spend it.
        return analyze(merged, fetch_deep=True)
    except Exception as e:
        return {
            "decision": "ANALYSIS_ERROR",
            "token_symbol": "UNKNOWN",
            "mint_address": mint,
            "confidence_score": 0,
            "decision_reason": f"Pipeline analysis error: {e}",
            "hard_reject": ["EXCEPTION"]
        }
