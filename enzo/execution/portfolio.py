#!/usr/bin/env python3
"""
ENZO - Portfolio Manager (SQLite WAL Backed Ledger)
Tracks positions, risk limits, PnL calculations, trailing stops, and multi-stage exits with atomic concurrency.
"""
import json
import os
import time
from datetime import datetime, timezone
from typing import Dict, Any, List, Optional, Tuple

from enzo.core.config import load_config, PORTFOLIO_JSON_PATH
import enzo.core.db as db
import enzo.core.learn as learn
import enzo.core.audit as audit
from enzo.ui import notify
from enzo.providers import gmgn, pump


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _risk_pct_for(confidence: float, cfg: dict) -> float:
    rm = cfg.get("risk_management", {}) or {}
    base = float(rm.get("risk_per_trade", 2.5))
    bands = (cfg.get("position_sizing", {}) or {}).get("confidence_bands") or []
    for b in bands:
        try:
            lo = float(b.get("min", 0))
            hi = float(b.get("max", 100))
            if lo <= float(confidence) <= hi:
                return float(b.get("risk_pct", base))
        except Exception:
            continue
    return base


def load_state() -> dict:
    return db.get_full_state()


def save_state(state: dict):
    db.save_full_state(state)


def equity(state: dict) -> float:
    eq = float(state.get("initial_capital", 10000.0)) + float(state.get("realized_pnl", 0.0))
    for pos in (state.get("open_positions") or {}).values():
        eq += float(pos.get("unrealized_pnl", 0.0))
    return eq


def _can_open(state: dict, cfg: dict, size_usd: float) -> Tuple[bool, str]:
    rm = cfg.get("risk_management", {})
    max_open = int(rm.get("max_open_positions", 5))
    max_exp_pct = float(rm.get("max_exposure", 30.0))
    open_count = len(state.get("open_positions") or {})
    if open_count >= max_open:
        return False, f"max_open_positions reached ({max_open})"
    exposure = sum(float(p.get("size_usd", 0.0)) for p in (state.get("open_positions") or {}).values())
    if exposure + size_usd > equity(state) * (max_exp_pct / 100.0):
        return False, "max_exposure limit"
    return True, "ok"


def is_halted(state: dict, cfg: dict) -> Optional[str]:
    rm = cfg.get("risk_management", {})
    if not rm.get("enable_risk_halts", True):
        return None
    max_daily = float(rm.get("max_daily_loss", 8.0))
    max_dd = float(rm.get("max_drawdown", 25.0))
    max_cons = int(rm.get("consecutive_losses_limit", 12))
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    same_day = state.get("last_day") == today
    dl = state.get("daily_loss", 0.0) if same_day else 0.0
    cons = state.get("consecutive_losses", 0) if same_day else 0
    eq = equity(state)
    if dl < 0 and eq > 0 and (-dl / eq * 100) >= max_daily:
        return f"daily loss {(-dl / eq * 100):.1f}% >= {max_daily}%"
    peak = state.get("peak_equity", eq)
    if peak > 0 and (peak - eq) / peak * 100 >= max_dd:
        return f"drawdown {(peak - eq) / peak * 100:.1f}% >= {max_dd}%"
    if cons >= max_cons:
        return f"consecutive losses {cons} >= {max_cons}"
    return None


def current_market_cap(mint: str, max_age: float = 10.0) -> Optional[float]:
    """Multi-source price fetcher: PumpDev (age-verified) -> GMGN -> Bonding Curve.

    Never returns a stale PumpDev quote: the WS timestamp is checked against
    max_age and only fresh quotes are accepted (falls through to the next tier).
    """
    # Priority 1: PumpDev real-time live market cap (with age verification)
    try:
        info = pump.get_pumpdev_client().get_live_mcap_info(mint)
        if info:
            mc = float(info.get("market_cap") or 0.0)
            ts = float(info.get("timestamp") or 0.0)
            age = (time.time() - ts) if ts else 0.0
            if mc > 0 and age <= max_age:
                return mc
    except Exception:
        pass

    # Priority 2: GMGN live market data
    try:
        mc = gmgn.get_live_market_cap(mint)
        if mc and float(mc) > 0:
            return float(mc)
    except Exception:
        pass

    # Priority 3: Bonding Curve calculation
    try:
        c = gmgn.read_bonding_curve(mint)
        if c.get("market_cap"):
            return float(c["market_cap"])
    except Exception:
        pass

    return None


def open_position(decision: dict, cfg: dict = None) -> dict:
    """Atomically open a new trading position."""
    cfg = cfg or load_config()
    state = load_state()
    mint = decision.get("mint_address") or decision.get("token_symbol")
    if not mint:
        return {"ok": False, "reason": "no mint"}
    if mint in state.get("open_positions", {}):
        return {"ok": False, "reason": "already open"}

    entry = float(decision.get("entry_price") or 0)
    sl_mc = float(decision.get("stop_loss_mc") or 0)
    tp_mc = float(decision.get("take_profit_mc") or 0)
    if entry <= 0:
        return {"ok": False, "reason": "invalid entry price"}

    rm = cfg.get("risk_management", {})
    xs = cfg.get("exit_strategy", {})
    stages = xs.get("take_profit_stages") or []
    risk_pct = _risk_pct_for(float(decision.get("confidence_score") or 0), cfg)
    max_exp_pct = float(rm.get("max_exposure", 30.0))
    stop_pct = float(xs.get("stop_loss_percentage", 50.0)) / 100.0
    max_hours = float(xs.get("max_holding_time_hours", 48))

    eq = equity(state)
    risk_usd = eq * (risk_pct / 100.0)
    size_usd = risk_usd / stop_pct if stop_pct > 0 else risk_usd
    max_exp_usd = eq * (max_exp_pct / 100.0)
    size_usd = min(size_usd, max_exp_usd)
    amount = size_usd / entry

    entry_mc = decision.get("entry_market_cap") or decision.get("market_cap_usd")
    if not entry_mc:
        try:
            entry_mc = current_market_cap(mint)
        except Exception:
            entry_mc = None
    entry_mc = float(entry_mc) if entry_mc else 0.0
    tp_pct = float(xs.get("take_profit_percentage", 150.0))

    halt = is_halted(state, cfg)
    if halt:
        return {"ok": False, "reason": f"HALTED: {halt}"}
    ok, reason = _can_open(state, cfg, size_usd)
    if not ok:
        return {"ok": False, "reason": reason}

    pos = {
        "symbol": decision.get("token_symbol"),
        "mint": mint,
        "entry_price": entry,
        "scam_score": decision.get("scam_score"),
        "security_status": decision.get("security_status"),
        "entry_market_cap": entry_mc,
        "current_market_cap": entry_mc,
        "stop_loss_mc": sl_mc,
        "take_profit_mc": (tp_mc or (entry_mc * (1 + tp_pct / 100.0))) if entry_mc else None,
        "size_usd": round(size_usd, 2),
        "initial_size_usd": round(size_usd, 2),
        "amount": amount,
        "initial_amount": amount,
        "realized_pnl_total": 0.0,
        "opened_at": _now_iso(),
        "max_holding_hours": max_hours,
        "unrealized_pnl": 0.0,
        "signals": decision.get("supporting_signals", []),
        "axis_scores": decision.get("axis_scores"),
        "features": decision.get("features"),
        "weighted_confidence": decision.get("confidence_score"),
        "peak_price": entry,
        "peak_market_cap": entry_mc,
        "trailing_active": False,
        "trailing_stop_mc": None,
        "stages_hit": [False] * len(stages) if stages else [],
    }

    # Subscribe to live PumpDev WebSocket trades
    try:
        pump.get_pumpdev_client().subscribe_trades([mint])
    except Exception:
        pass

    db.atomic_open_position(pos)
    return {"ok": True, "reason": "opened", "position": pos}


def close_position(state: dict, mint: str, exit_market_cap: float, reason: str) -> dict:
    """Atomically close a position and register realized PnL."""
    pos = state.get("open_positions", {}).get(mint)
    if not pos:
        return {"ok": False, "reason": "no open position"}

    entry_mc = float(pos.get("entry_market_cap") or 0.0)
    ratio = (exit_market_cap / entry_mc - 1) if entry_mc > 0 else 0.0
    this_pnl = float(pos.get("size_usd", 0.0)) * ratio
    if pos.get("stages_hit"):
        record_pnl = float(pos.get("realized_pnl_total", 0.0)) + this_pnl
        init_size = float(pos.get("initial_size_usd", pos.get("size_usd", 0.0)))
        record_pnl_pct = (record_pnl / init_size * 100) if init_size > 0 else 0.0
    else:
        record_pnl = this_pnl
        record_pnl_pct = ratio * 100

    record = {
        "symbol": pos.get("symbol"),
        "mint": mint,
        "entry_price": float(pos.get("entry_price", 0.0)),
        "exit_price": float(pos.get("entry_price", 0.0)) * (1 + ratio),
        "entry_market_cap": entry_mc,
        "exit_market_cap": exit_market_cap,
        "size_usd": float(pos.get("size_usd", 0.0)),
        "pnl": round(record_pnl, 4),
        "pnl_pct": round(record_pnl_pct, 2),
        "reason": reason,
        "signals": pos.get("signals", []),
        "axis_scores": pos.get("axis_scores"),
        "features": pos.get("features"),
        "opened_at": pos.get("opened_at"),
        "closed_at": _now_iso(),
    }

    db.atomic_close_position(mint, record, pnl_delta=this_pnl)

    # Feed learning engine
    try:
        learn.record_outcome(record, features=pos.get("features"), axis_scores=pos.get("axis_scores"))
    except Exception:
        pass

    return {"ok": True, "reason": reason, "record": record}


def _partial_exit(state: dict, mint: str, pos: dict, frac: float, market_cap: float, stage_pct: int) -> Optional[dict]:
    entry_mc = float(pos.get("entry_market_cap") or 0.0)
    ratio = (market_cap / entry_mc - 1) if entry_mc > 0 else 0.0
    sold_size = float(pos.get("size_usd", 0.0)) * frac
    if sold_size <= 1e-9:
        return None
    pnl = sold_size * ratio
    new_size = float(pos.get("size_usd", 0.0)) - sold_size
    new_amount = float(pos.get("amount", 0.0)) * (1 - frac)
    pos["size_usd"] = new_size
    pos["amount"] = new_amount
    pos["realized_pnl_total"] = float(pos.get("realized_pnl_total", 0.0)) + pnl

    return {
        "symbol": pos.get("symbol"),
        "mint": mint,
        "exit_price": float(pos.get("entry_price", 0.0)) * (1 + ratio),
        "exit_market_cap": market_cap,
        "fraction": frac,
        "pnl": round(pnl, 4),
        "pnl_pct": round(ratio * 100, 2),
        "reason": f"TP_STAGE_{stage_pct}%",
    }


def check_exits(current_mcaps: dict) -> Tuple[List[dict], List[dict]]:
    cfg = load_config()
    paper = bool(cfg.get("paper_mode", True))
    xs = cfg.get("exit_strategy", {})
    trail_pct = float(xs.get("trailing_stop_percentage", 30.0))
    stop_pct = float(xs.get("stop_loss_percentage", 50.0))
    stages = xs.get("take_profit_stages") or []
    state = load_state()
    closed = []
    partials = []
    pos_updates = []
    now = time.time()

    # Persist live peak equity so drawdown reflects unrealized moves promptly
    try:
        eq = equity(state)
        db.atomic_update_peak_equity(eq)
    except Exception:
        pass

    for mint, mcap in list(current_mcaps.items()):
        pos = state.get("open_positions", {}).get(mint)
        if not pos or mcap is None:
            continue
        if not pos.get("entry_market_cap"):
            pos["entry_market_cap"] = current_market_cap(mint) or 0.0
            pos["initial_size_usd"] = pos.get("size_usd", 0.0)
            pos["peak_market_cap"] = pos["entry_market_cap"]
        if not pos.get("entry_market_cap"):
            continue

        entry_mc = float(pos["entry_market_cap"])
        pct = (mcap / entry_mc - 1) * 100 if entry_mc > 0 else 0.0
        pos["current_market_cap"] = mcap
        pos["unrealized_pnl"] = float(pos.get("size_usd", 0.0)) * (pct / 100.0)

        peak_mc = max(float(pos.get("peak_market_cap", entry_mc)), mcap)
        pos["peak_market_cap"] = peak_mc
        if not pos.get("trailing_active"):
            if mcap >= entry_mc * (1 + trail_pct / 100.0):
                pos["trailing_active"] = True
                pos["trailing_stop_mc"] = mcap * (1 - trail_pct / 100.0)
        else:
            pos["trailing_stop_mc"] = max(float(pos.get("trailing_stop_mc") or mcap), mcap * (1 - trail_pct / 100.0))

        fully_closed = False
        if stages:
            n_stages = len(stages)
            for i, st in enumerate(stages):
                if i < len(pos.get("stages_hit", [])) and pos["stages_hit"][i]:
                    continue
                if mcap >= entry_mc * (1 + float(st["pct"]) / 100.0) * (1 - 1e-9):
                    is_last = (i == n_stages - 1)
                    frac = 1.0 if is_last else float(st["sell"])
                    rec = _partial_exit(state, mint, pos, frac, mcap, int(st["pct"]))
                    if rec:
                        partials.append(rec)
                        # Register this partial's realized PnL in the portfolio
                        # balance immediately so equity matches the ledger.
                        try:
                            db.atomic_add_realized(float(rec.get("pnl", 0.0)))
                            notify.notify_partial(rec, paper=paper)
                            audit.log_event(
                                category="TRADE", level="TP",
                                message=f"Partial TP: {rec.get('symbol')} {rec.get('reason')} +${float(rec.get('pnl', 0)):,.2f}",
                                data={"mint": mint, "pnl": rec.get("pnl")}
                            )
                        except Exception:
                            pass
                    pos.setdefault("stages_hit", [])[i] = True
                    if is_last or float(pos.get("amount", 0.0)) <= 1e-9:
                        r = close_position(state, mint, mcap, f"TP_FINAL_{int(st['pct'])}%")
                        if r.get("ok"):
                            closed.append(r["record"])
                        fully_closed = True
                        break

        if fully_closed:
            continue

        try:
            opened = datetime.fromisoformat(pos["opened_at"]).timestamp()
        except Exception:
            opened = now
        held_hours = (now - opened) / 3600.0

        if pos.get("trailing_active") and pos.get("trailing_stop_mc") and mcap <= float(pos["trailing_stop_mc"]):
            r = close_position(state, mint, mcap, "TRAILING_STOP")
        elif (not stages) and pos.get("take_profit_mc") and mcap >= float(pos["take_profit_mc"]):
            r = close_position(state, mint, mcap, "TAKE_PROFIT")
        elif mcap <= entry_mc * (1 - stop_pct / 100.0):
            r = close_position(state, mint, mcap, "STOP_LOSS")
        elif held_hours >= float(pos.get("max_holding_hours", 48)):
            r = close_position(state, mint, mcap, "TIME_EXIT")
        else:
            r = None

        if r and r.get("ok"):
            closed.append(r["record"])
            try:
                notify.notify_exit(r["record"], paper=paper)
                audit.log_event(
                    category="EXIT", level="INFO",
                    message=f"Position closed: {r['record'].get('symbol')} ({r['record'].get('reason')}) PnL ${float(r['record'].get('pnl', 0)):+,.2f}",
                    data={"mint": mint, "pnl": r["record"].get("pnl"), "reason": r["record"].get("reason")}
                )
            except Exception:
                pass
        else:
            pos_updates.append(pos)

    if pos_updates:
        db.atomic_update_open_positions(pos_updates)

    return closed, partials


def get_state() -> dict:
    state = load_state()
    state = dict(state)
    state["equity"] = round(equity(state), 2)
    state["halted"] = is_halted(state, load_config())
    closed_list = state.get("closed_positions") or []
    wins = [c for c in closed_list if float(c.get("pnl", 0.0)) > 0]
    losses = [c for c in closed_list if float(c.get("pnl", 0.0)) <= 0]
    total = len(closed_list)
    state["stats"] = {
        "total_trades": total,
        "wins": len(wins),
        "losses": len(losses),
        "win_rate": round(len(wins) / total * 100, 1) if total else 0.0,
        "realized_pnl": round(float(state.get("realized_pnl", 0.0)), 2),
    }
    return state
