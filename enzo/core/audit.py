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
            # Layer-0 evidence, persisted so any rejection can be audited later
            # WITHOUT re-querying GMGN: which launchpad, which phase and why,
            # the fees reading, the early-sniper window, and the measured holder
            # concentration behind the max_holder_percentage cap.
            "universe": decision.get("universe"),
            "top_holder_pct": decision.get("top_holder_pct"),
        }
        os.makedirs(os.path.dirname(AUDIT_JSONL_PATH), exist_ok=True)
        with open(AUDIT_JSONL_PATH, "a", encoding="utf-8") as f:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")
    except Exception:
        pass


_TAIL_BLOCK = 64 * 1024  # read the audit log backwards in 64 KB blocks


def _tail_lines(path: str, max_lines: int) -> list:
    """Return the last `max_lines` lines of a file WITHOUT reading all of it.

    `load_audit()` used to parse every line of enzo-audit.jsonl and only then
    slice `rows[-n:]`. The file had already grown to 4.7 MB / 14,528 rows (83 %
    of them duplicate stale-price warnings), and /api/activity calls this on
    every 10-second dashboard poll — so the dashboard got slower until it
    stalled. Seeking from the end keeps this O(rows requested).
    """
    out: list = []
    try:
        size = os.path.getsize(path)
    except OSError:
        return out
    if size == 0:
        return out

    with open(path, "rb") as f:
        pos = size
        buf = b""
        while pos > 0 and len(out) <= max_lines:
            step = min(_TAIL_BLOCK, pos)
            pos -= step
            f.seek(pos)
            buf = f.read(step) + buf
            lines = buf.split(b"\n")
            # the first element is a partial line unless we are at offset 0
            buf = lines[0] if pos > 0 else b""
            chunk = lines[1:] if pos > 0 else lines
            for raw in reversed(chunk):
                raw = raw.strip()
                if raw:
                    out.append(raw)
                    if len(out) >= max_lines:
                        break
        if pos == 0 and buf.strip() and len(out) < max_lines:
            out.append(buf.strip())

    out.reverse()
    rows = []
    for raw in out:
        try:
            rows.append(json.loads(raw.decode("utf-8", errors="replace")))
        except Exception:
            continue
    return rows


def load_audit(n: int = None) -> list:
    """Load audit rows. With `n`, only the last n rows are read from disk."""
    if not os.path.exists(AUDIT_JSONL_PATH):
        return []
    if n:
        # over-read slightly: some lines may be corrupt or partially written
        return _tail_lines(AUDIT_JSONL_PATH, int(n) + 16)[-int(n):]
    rows = []
    with open(AUDIT_JSONL_PATH, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                try:
                    rows.append(json.loads(line))
                except Exception:
                    pass
    return rows


def rotate_audit(max_bytes: int = 5 * 1024 * 1024, keep: int = 3) -> Optional[str]:
    """Rotate enzo-audit.jsonl once it exceeds `max_bytes`.

    Returns the archive path, or None when no rotation was needed. Called from
    the supervisor loop so the activity feed can never grow without bound.
    """
    try:
        if not os.path.exists(AUDIT_JSONL_PATH):
            return None
        if os.path.getsize(AUDIT_JSONL_PATH) < max_bytes:
            return None
        stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
        archive = f"{AUDIT_JSONL_PATH}.{stamp}"
        os.replace(AUDIT_JSONL_PATH, archive)
        # prune old archives beyond `keep`
        archives = sorted(
            (f for f in os.listdir(os.path.dirname(AUDIT_JSONL_PATH))
             if f.startswith(os.path.basename(AUDIT_JSONL_PATH) + ".")
             and ".bak." not in f),
            reverse=True,
        )
        for old in archives[keep:]:
            try:
                os.remove(os.path.join(os.path.dirname(AUDIT_JSONL_PATH), old))
            except OSError:
                pass
        return archive
    except Exception:
        return None


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
                    # The audit row HAS the reason and the veto codes, but they
                    # were dropped here - so the Activity feed showed
                    # "SYMBOL -> IGNORE (conf=0)" with no explanation at all.
                    # For a bot trading real money, "why not?" is the question
                    # the owner asks most often.
                    "reason": r.get("reason"),
                    "rejected_signals": (r.get("rejected_signals") or [])[:6],
                    "universe": r.get("universe"),
                    "top_holder_pct": r.get("top_holder_pct"),
                    "mint": r.get("mint"),
                }
            })
        if len(activities) >= limit:
            break

    return list(reversed(activities))
