#!/usr/bin/env python3
"""
ENZO - Market Structure Analysis (Axis Module)
Tracks multi-sample growth in market cap, liquidity, buyer count, and candle momentum.
"""
import os
import json
import time
import threading
from typing import Dict, Any, Optional

from enzo.core.config import load_config, clamp, MARKET_STRUCTURE_PATH
from enzo.providers import gmgn

_MAX_MINTS = 200  # prune store to the most recently updated N mints
_STORE_LOCK = threading.Lock()


def _store_get() -> dict:
    try:
        if os.path.exists(MARKET_STRUCTURE_PATH):
            with open(MARKET_STRUCTURE_PATH, "r", encoding="utf-8") as f:
                return json.load(f)
    except Exception:
        pass
    return {}


def _store_set(d: dict):
    """Atomic write (temp file + os.replace) with pruning of stale mints."""
    try:
        os.makedirs(os.path.dirname(MARKET_STRUCTURE_PATH), exist_ok=True)
        if len(d) > _MAX_MINTS:
            d = dict(sorted(d.items(), key=lambda kv: (kv[1][-1]["ts"] if kv[1] else 0), reverse=True)[:_MAX_MINTS])
        tmp = MARKET_STRUCTURE_PATH + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(d, f)
        os.replace(tmp, MARKET_STRUCTURE_PATH)
    except Exception:
        pass


def _growth(a, b):
    try:
        a = float(a)
        b = float(b)
    except Exception:
        return None
    if not a:
        return None
    return (b - a) / a * 100.0


def _kline_volume_trend(mint: str, candles: int = 6) -> dict:
    """Candle-derived signals. MUST NOT raise: this axis is one of six, and an
    exception here used to escape all the way to run_pipeline and turn the whole
    decision into ANALYSIS_ERROR (hard_reject ['EXCEPTION']) - from the SECOND
    scan of a mint onward, because the first scan returns early on
    "insufficient_samples". A missing candle input is worth 0 information, never a
    rejected coin. gmgn.kline() now normalizes every payload generation into dict
    rows; the isinstance guards below keep a future shape change from being fatal.
    """
    try:
        now = int(time.time())
        kl = gmgn.kline(mint, "5m", from_ts=now - candles * 300 - 60, to_ts=now)
    except Exception:
        return {}
    if not isinstance(kl, (list, tuple)) or len(kl) < 2:
        return {}
    closes = []
    vols = []
    for c in list(kl)[-candles:]:
        if not isinstance(c, dict):
            continue
        try:
            cl = float(c.get("close") or 0)
            v = float(c.get("volume") or 0)
        except Exception:
            continue
        if cl > 0:
            closes.append(cl)
            vols.append(v)
    if len(closes) < 2:
        return {}
    green = sum(1 for i in range(1, len(closes)) if closes[i] > closes[i - 1])
    green_ratio = green / (len(closes) - 1)
    vol_trend = None
    if len(vols) >= 3:
        half = len(vols) // 2
        v1 = sum(vols[:half]) / max(1, half)
        v2 = sum(vols[half:]) / max(1, len(vols) - half)
        if v1:
            vol_trend = (v2 / v1 - 1) * 100.0
    last_up = closes[-1] > closes[-2]
    return {
        "green_ratio": round(green_ratio, 2),
        "vol_trend": vol_trend,
        "last_close_up": last_up
    }


def analyze(mint: str, merged: dict = None, config: dict = None) -> dict:
    cfg = config or load_config()
    ms = cfg.get("market_structure", {}) or {}
    neutral = float(ms.get("neutral_score", 50))
    interval = float(ms.get("min_sample_interval_sec", 60))
    max_samples = int(ms.get("max_samples", 30))

    sig = (merged or {}).get("signals", {}) or {}
    sec = (merged or {}).get("security", {}) or {}
    mc = sig.get("market_cap_usd")
    liq = float(sig.get("liquidity_usd", sec.get("liquidity", 0)) or 0)
    vol = sig.get("volume_24h_usd")
    buyers = sec.get("unique_wallet_5m")
    pc1h = float(sig.get("price_change_1h") or 0)
    pc5m = sig.get("price_change_5m")
    pc5m = float(pc5m) if pc5m is not None else None

    now = time.time()
    with _STORE_LOCK:
        store = _store_get()
        series = store.get(mint, [])

        if not series or (now - series[-1]["ts"]) >= interval:
            series.append({"ts": now, "mc": mc, "liq": liq, "vol": vol, "buyers": buyers})
            store[mint] = series[-max_samples:]
            _store_set(store)

    if len(series) < 2:
        # Insufficient samples: cap the score at 40 so a lone first sample
        # (especially after a violent 1h pump) can never score near 100.
        first_score = min(40, clamp(35 + pc1h * 1.5))
        return {
            "score": int(first_score),
            "available": False,
            "flags": ["insufficient_samples -> conservative"],
            "detail": {"samples": len(series), "imbalance_score": first_score}
        }

    a, b = series[0], series[-1]
    dt_min = max(1.0, (b["ts"] - a["ts"]) / 60.0)

    mc_g = _growth(a["mc"], b["mc"])
    liq_g = _growth(a["liq"], b["liq"])
    buyer_inc = _growth(a["buyers"], b["buyers"])
    vol_accel = None
    if len(series) >= 3:
        m1 = _growth(series[0]["vol"], series[len(series) // 2]["vol"])
        m2 = _growth(series[len(series) // 2]["vol"], series[-1]["vol"])
        if m1 is not None and m2 is not None:
            vol_accel = m2 - m1
    imbalance = clamp(50 + pc1h * 2)

    kt = _kline_volume_trend(mint)
    green_ratio = kt.get("green_ratio") if kt else None
    vol_trend = kt.get("vol_trend") if kt else None
    last_up = kt.get("last_close_up") if kt else None

    parts = []
    if mc_g is not None:
        parts.append(clamp(50 + mc_g))
    if liq_g is not None:
        parts.append(clamp(50 + liq_g))
    if buyer_inc is not None:
        parts.append(clamp(50 + buyer_inc))
    parts.append(imbalance)
    if green_ratio is not None:
        parts.append(clamp(40 + green_ratio * 60))

    score = clamp(round(sum(parts) / len(parts))) if parts else neutral

    flags = [
        f"mc_growth={round(mc_g, 1) if mc_g is not None else 'n/a'}%/window",
        f"liq_growth={round(liq_g, 1) if liq_g is not None else 'n/a'}%",
        f"buyers_increase={round(buyer_inc, 1) if buyer_inc is not None else 'n/a'}%",
        f"1h_momentum={pc1h:+.2f}%",
    ]
    if vol_accel is not None:
        flags.append(f"vol_accel={round(vol_accel, 1)}%/window")
    if green_ratio is not None:
        flags.append(f"green_candles={green_ratio * 100:.0f}%")
    if vol_trend is not None:
        flags.append(f"5m_vol_trend={vol_trend:+.1f}%")
    if last_up is not None:
        flags.append("last_5m_close=up" if last_up else "last_5m_close=down")
    if pc5m is not None:
        flags.append(f"5m_change={pc5m:+.2f}%")

    return {
        "score": score,
        "available": True,
        "flags": flags,
        "detail": {
            "mc_growth_pct": mc_g,
            "liq_growth_pct": liq_g,
            "buyer_increase_pct": buyer_inc,
            "volume_acceleration": vol_accel,
            "imbalance_score": imbalance,
            "green_candle_ratio": green_ratio,
            "kline_vol_trend_pct": vol_trend,
            "last_candle_up": last_up,
            "price_change_5m": pc5m,
            "samples": len(series),
            "window_min": round(dt_min, 1),
        },
    }


def clear(mint: str = None):
    if mint:
        s = _store_get()
        s.pop(mint, None)
        _store_set(s)
    else:
        _store_set({})
