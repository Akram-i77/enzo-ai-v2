#!/usr/bin/env python3
"""
ENZO - Security Analysis Layer (Tier 1 On-Chain Security)
Analyzes mint authorities, freeze authorities, honeypot flags, and concentration.
"""
from typing import Dict, Any
from enzo.core.config import load_config
from enzo.providers import gmgn


def security_scan(mint: str) -> dict:
    """Tier-1 security scan delegating to GMGN provider."""
    return gmgn.security_scan(mint)


def cached_holder_distribution(mint: str, cfg: dict = None, ttl: float = 600) -> dict:
    return gmgn.cached_holder_distribution(mint, cfg, ttl)


def security_axis(security_data: dict, config: dict = None) -> dict:
    """Compute security axis score (0-100) and identify hard risk flags."""
    cfg = config or load_config()
    sa = cfg.get("weighted_confidence", {}).get("security", {}) or {}
    if isinstance(sa, (int, float)):
        sa = {"weight": sa, "neutral_score": 50}
    neutral = float(sa.get("neutral_score", 50))

    if not security_data:
        return {
            "score": neutral,
            "available": False,
            "flags": ["no_security_data -> neutral"],
            "security_status": None,
            "hard_reject": []
        }

    out = dict(security_data)
    status = security_data.get("security_status")
    hard_reject = security_data.get("hard_reject") or []
    safety = security_data.get("safety_score")

    if status == "DANGEROUS" or hard_reject:
        out["score"] = 0
        out["flags"] = hard_reject or ["DANGEROUS"]
    elif safety is not None:
        out["score"] = max(0, min(100, int(safety)))
        out["flags"] = security_data.get("security_flags") or []
    else:
        out["score"] = neutral
        out["flags"] = ["unknown -> neutral"]

    out["available"] = status is not None
    out["security_status"] = status
    out["hard_reject"] = hard_reject
    return out
