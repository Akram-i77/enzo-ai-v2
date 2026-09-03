#!/usr/bin/env python3
"""
ENZO - Self-Calibrating Learning Engine
Learns from closed trades to continuously refine confidence scores and axis weighting.
"""
import json
import os
import sys
import threading
from datetime import datetime, timezone
from enzo.core.config import LEARNING_PATH

TARGET_WIN_RATE = 50.0  # % baseline the bias calibrates around
_STATE_LOCK = threading.Lock()  # serializes read-modify-write of learning.json


def _now_iso():
    return datetime.now(timezone.utc).isoformat()


def load_state() -> dict:
    try:
        if os.path.exists(LEARNING_PATH):
            with open(LEARNING_PATH, "r", encoding="utf-8") as f:
                return json.load(f)
    except Exception:
        pass
    return {
        "outcomes": [],
        "signal_effectiveness": {},
        "feature_outcomes": {},
        "axis_outcomes": {},
        "confidence_bias": 0.0,
        "updated_at": _now_iso(),
    }


def save_state(state: dict):
    state["updated_at"] = _now_iso()
    try:
        os.makedirs(os.path.dirname(LEARNING_PATH), exist_ok=True)
        tmp = LEARNING_PATH + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(state, f, indent=2, ensure_ascii=False)
        os.replace(tmp, LEARNING_PATH)  # atomic on POSIX
    except Exception as e:
        sys.stderr.write(f"[ENZO][LEARN-ERR] {e}\n")


def record_outcome(record: dict, features: dict = None, axis_scores: dict = None) -> dict:
    """Record a completed trade outcome and adjust confidence bias and axis stats."""
    with _STATE_LOCK:
        return _record_outcome_locked(record, features, axis_scores)


def _record_outcome_locked(record: dict, features: dict = None, axis_scores: dict = None) -> dict:
    state = load_state()
    win = float(record.get("pnl", 0)) > 0
    outcome = {
        "symbol": record.get("symbol"),
        "mint": record.get("mint"),
        "pnl": round(float(record.get("pnl", 0)), 4),
        "pnl_pct": round(float(record.get("pnl_pct", 0)), 2),
        "reason": record.get("reason"),
        "win": win,
        "signals": record.get("signals", []),
        "at": _now_iso(),
    }
    state.setdefault("outcomes", []).append(outcome)

    # Per-signal effectiveness
    sig_eff = state.setdefault("signal_effectiveness", {})
    for sig in outcome["signals"]:
        eff = sig_eff.setdefault(sig, {"wins": 0, "losses": 0, "n": 0})
        eff["n"] += 1
        if win:
            eff["wins"] += 1
        else:
            eff["losses"] += 1

    # Per-feature effectiveness
    feats = features if features is not None else record.get("features") or {}
    fo = state.setdefault("feature_outcomes", {})
    for k, v in feats.items():
        if v:
            e = fo.setdefault(k, {"wins": 0, "losses": 0, "n": 0})
            e["n"] += 1
            if win:
                e["wins"] += 1
            else:
                e["losses"] += 1

    # Per-axis win/loss and avg score
    axs = axis_scores if axis_scores is not None else record.get("axis_scores") or {}
    ao = state.setdefault("axis_outcomes", {})
    for k, sc in axs.items():
        score = sc.get("score") if isinstance(sc, dict) else sc
        try:
            score = float(score)
        except Exception:
            continue
        e = ao.setdefault(k, {"wins": 0, "losses": 0, "sum_score_win": 0.0, "sum_score_loss": 0.0, "n": 0})
        e["n"] += 1
        if win:
            e["wins"] += 1
            e["sum_score_win"] += score
        else:
            e["losses"] += 1
            e["sum_score_loss"] += score

    # Recalibrate confidence bias from rolling win rate
    outcomes = state["outcomes"]
    recent = outcomes[-50:]
    wr = (sum(1 for o in recent if o["win"]) / len(recent) * 100) if recent else TARGET_WIN_RATE
    state["confidence_bias"] = max(-5.0, min(5.0, round((wr - TARGET_WIN_RATE) / 10.0, 2)))

    save_state(state)
    return {"ok": True, "win_rate": round(wr, 1), "confidence_bias": state["confidence_bias"]}


def calibrate_confidence(base_conf: float) -> float:
    """Apply learned bias to a confidence score."""
    state = load_state()
    bias = float(state.get("confidence_bias", 0.0))
    return max(0.0, min(100.0, round(base_conf + bias, 1)))


def feature_win_rates() -> list:
    state = load_state()
    fo = state.get("feature_outcomes", {})
    out = []
    for k, e in fo.items():
        if e["n"]:
            out.append({"feature": k, "win_rate": round(e["wins"] / e["n"] * 100, 1), "n": e["n"]})
    return sorted(out, key=lambda x: x["win_rate"], reverse=True)


def axis_win_rates() -> list:
    state = load_state()
    ao = state.get("axis_outcomes", {})
    out = []
    for k, e in ao.items():
        if e["n"]:
            avg = (e["sum_score_win"] / e["wins"]) if e["wins"] else 0.0
            avg = avg if e["wins"] else (e["sum_score_loss"] / e["losses"] if e["losses"] else 0.0)
            out.append({"axis": k, "win_rate": round(e["wins"] / e["n"] * 100, 1),
                        "avg_score": round(avg, 1), "n": e["n"]})
    return sorted(out, key=lambda x: x["win_rate"], reverse=True)


def suggested_weights(base_weights: dict, min_n: int = 10) -> dict:
    state = load_state()
    ao = state.get("axis_outcomes", {})
    new = {}
    for k, w in base_weights.items():
        e = ao.get(k)
        if e and e["n"] >= min_n:
            r = e["wins"] / e["n"]
            new[k] = round(w * (0.5 + r), 2)
        else:
            new[k] = w
    return new


def get_state() -> dict:
    state = load_state()
    outcomes = state.get("outcomes", [])
    wins = [o for o in outcomes if o["win"]]
    losses = [o for o in outcomes if not o["win"]]
    total = len(outcomes)
    avg_pnl = (sum(o["pnl"] for o in outcomes) / total) if total else 0.0
    avg_pnl_pct = (sum(o["pnl_pct"] for o in outcomes) / total) if total else 0.0

    ranked = sorted(
        state.get("signal_effectiveness", {}).items(),
        key=lambda kv: (kv[1]["wins"] / kv[1]["n"]) if kv[1]["n"] else 0,
        reverse=True,
    )
    return {
        "total_trades": total,
        "wins": len(wins),
        "losses": len(losses),
        "win_rate": round(len(wins) / total * 100, 1) if total else 0.0,
        "avg_pnl": round(avg_pnl, 4),
        "avg_pnl_pct": round(avg_pnl_pct, 2),
        "confidence_bias": state.get("confidence_bias", 0.0),
        "top_signals": [
            {"signal": s, "win_rate": round(e["wins"] / e["n"] * 100, 1), "n": e["n"]}
            for s, e in ranked[:5]
        ],
        "worst_signals": [
            {"signal": s, "win_rate": round(e["wins"] / e["n"] * 100, 1), "n": e["n"]}
            for s, e in ranked[-3:]
        ],
        "feature_win_rates": feature_win_rates(),
        "axis_win_rates": axis_win_rates(),
    }


def insights() -> str:
    s = get_state()
    if s["total_trades"] == 0:
        return "🧠 ENZO Learning: no closed trades yet — awaiting paper outcomes."
    top = ", ".join(f"{t['signal']} ({t['win_rate']}%)" for t in s["top_signals"]) or "n/a"
    return (
        f"🧠 ENZO Learning — trades={s['total_trades']} win_rate={s['win_rate']}% "
        f"avg_pnl%={s['avg_pnl_pct']} bias={s['confidence_bias']:+} | "
        f"best signals: {top}"
    )


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "reset":
        try:
            if os.path.exists(LEARNING_PATH):
                os.remove(LEARNING_PATH)
        except Exception:
            pass
        print("Learning state reset.")
    else:
        import pprint
        pprint.pprint(get_state())
        print("\n" + insights())
