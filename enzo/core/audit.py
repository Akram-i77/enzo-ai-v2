#!/usr/bin/env python3
"""
ENZO - Decision Audit Logger & Telemetry
Records decision audit lines to data/enzo-audit.jsonl and generates audit reports.
"""
import json
import os
import sys
from datetime import datetime, timezone
from collections import Counter
from typing import Dict, Any, Optional

from enzo.core.config import AUDIT_JSONL_PATH

HARD_GATES = [
    ("security", "SECURITY:"),
    ("liquidity", "Liquidity $"),
    ("holders", "Holders "),
    ("volume", "Volume24h"),
    ("market_cap", "Market cap $"),
    ("top_holder", "Top holder"),
]


def _classify(decision: dict):
    rej = decision.get("rejected_signals") or []
    hits = []
    for key, kw in HARD_GATES:
        if any(kw in r for r in rej):
            hits.append(key)
    first = hits[0] if hits else None
    reached_confidence = not any(h in ("security", "liquidity") for h in hits)
    return hits, first, reached_confidence


def _ax(d, k):
    v = d.get(k)
    return (v or {}).get("score") if isinstance(v, dict) else v


def record(decision: dict, extra: dict = None):
    try:
        if not isinstance(decision, dict):
            return
        extra = extra or {}
        hits, first, reached = _classify(decision)
        ax = decision.get("axis_scores") or {}
        q = decision.get("quality") or {}
        dev = decision.get("dev_events") or []

        row = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "symbol": decision.get("token_symbol"),
            "mint": decision.get("mint_address"),
            "token_type": decision.get("token_type"),
            "security_status": decision.get("security_status"),
            "decision": decision.get("decision"),
            "confidence": decision.get("confidence_score") or decision.get("weighted_confidence"),
            "reached_confidence": reached,
            "first_hard_gate": first,
            "hard_gates_hit": hits,
            "reason": decision.get("decision_reason"),
            "market_cap_usd": decision.get("market_cap_usd"),
            "axes": {
                "security": _ax(ax, "security"),
                "wallet_behavior": _ax(ax, "wallet_behavior"),
                "dev_behavior": _ax(ax, "dev_behavior"),
                "momentum": _ax(ax, "momentum"),
                "market_structure": _ax(ax, "market_structure"),
                "liquidity": _ax(ax, "liquidity"),
            },
            "rejected_signals": decision.get("rejected_signals"),
            "supporting_signals": decision.get("supporting_signals"),
        }
        os.makedirs(os.path.dirname(AUDIT_JSONL_PATH), exist_ok=True)
        with open(AUDIT_JSONL_PATH, "a", encoding="utf-8") as f:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")
    except Exception:
        pass


def load_audit(n: int = None) -> list:
    rows = []
    if not os.path.exists(AUDIT_JSONL_PATH):
        return rows
    with open(AUDIT_JSONL_PATH, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                try:
                    rows.append(json.loads(line))
                except Exception:
                    pass
    return rows[-n:] if n else rows


# ================================================================ Live Activity Stream API
def log_event(category: str, level: str, message: str, data: dict = None):
    """Append a system/UI-driven event to the audit log so the Live Activity tab can render it."""
    try:
        row = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "category": category,
            "level": level,
            "message": message,
            "data": data or {},
        }
        os.makedirs(os.path.dirname(AUDIT_JSONL_PATH), exist_ok=True)
        with open(AUDIT_JSONL_PATH, "a", encoding="utf-8") as f:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")
    except Exception:
        pass


def get_recent_activities(limit: int = 100) -> list:
    """Return recent activity items formatted for the dashboard activity stream."""
    rows = load_audit(n=limit * 3)  # over-fetch, then filter
    activities = []
    for r in rows:
        # Already in activity shape (logged via log_event)
        if "category" in r and "level" in r and "message" in r:
            activities.append({
                "ts": r.get("ts", ""),
                "time_str": (r.get("ts", "") or "")[11:19],
                "category": r.get("category", "SYSTEM"),
                "level": r.get("level", "INFO"),
                "message": r.get("message", ""),
                "data": r.get("data", {}),
            })
        else:
            # Decision audit row — convert into activity shape
            dec = (r.get("decision") or "INFO").upper()
            cat = "SYSTEM"
            if dec == "BUY":
                cat = "TRADE"
            elif dec in ("WAIT", "IGNORE"):
                cat = "ANALYSIS"
            elif dec == "CLOSED":
                cat = "EXIT"

            sym = r.get("symbol") or "?"
            conf = r.get("confidence") or 0
            msg = f"{sym} → {dec} (conf={float(conf):.0f})" if sym else (r.get("reason", "") or "")[:80]
            activities.append({
                "ts": r.get("ts", ""),
                "time_str": (r.get("ts", "") or "")[11:19],
                "category": cat,
                "level": "SUCCESS" if dec == "BUY" else ("ERROR" if dec == "IGNORE" else "WARNING" if dec == "WAIT" else "INFO"),
                "message": msg,
                "data": {
                    "axes": r.get("axes") or {},
                    "market_cap_usd": r.get("market_cap_usd"),
                }
            })
        if len(activities) >= limit:
            break

    return list(reversed(activities))
