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

from enzo.core.config import load_config, PORTFOLIO_JSON_PATH, RUN_DIR
import enzo.core.db as db
import enzo.core.learn as learn
import enzo.core.audit as audit
import enzo.core.log as log
from enzo.ui import notify
from enzo.providers import gmgn, pump
from enzo.execution import executor

_LOGGER = log.get_logger("enzo.portfolio")

# Deployable-capital snapshot, shared across processes (engine, dashboard,
# enzoctl) without adding a DB migration.
CAPITAL_PATH = os.path.join(RUN_DIR, "enzo-capital.json")


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# ─────────────────────────────────────────────────────────────────────────────
# Deployable capital — the number position sizing is actually based on.
#
# Before this existed, sizing used portfolio_state.initial_capital, a static
# value that sat at $2.06. At 1 % risk with a 50 % stop that produced a $0.04
# position, below execution.min_trade_usd ($1.00), so every BUY was rejected at
# the executor and rolled back. The bot could never enter a trade regardless of
# signal quality.
# ─────────────────────────────────────────────────────────────────────────────
def _read_capital_file() -> dict:
    try:
        if os.path.exists(CAPITAL_PATH):
            with open(CAPITAL_PATH, "r", encoding="utf-8") as f:
                d = json.load(f)
                return d if isinstance(d, dict) else {}
    except Exception:
        pass
    return {}


def _write_capital_file(d: dict) -> None:
    try:
        os.makedirs(os.path.dirname(CAPITAL_PATH), exist_ok=True)
        tmp = CAPITAL_PATH + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(d, f, indent=2)
        os.replace(tmp, CAPITAL_PATH)
    except Exception:
        pass


def sync_capital_base(force: bool = False, cfg: dict = None, rebase: bool = False) -> dict:
    """Refresh deployable capital from the real wallet (live + capital_source=wallet).

    Read-only by default. Pass rebase=True (the engine does, once per cycle) to
    also rebase portfolio_state.initial_capital onto the wallet figure — and even
    then only when the ledger number is genuinely implausible and there is no
    closed-trade history, so ROI/win-rate never silently change meaning and the
    drawdown baseline is not reset every cycle.

    Read-only matters: `enzoctl doctor` and `enzoctl wallet` must be able to
    report capital without mutating the ledger.

    Returns {"source": "wallet"|"ledger", "usd": float, "ok": bool, "detail": str}
    """
    cfg = cfg or load_config()
    ex = cfg.get("execution") or {}
    paper = bool(cfg.get("paper_mode", True))
    source = str(ex.get("capital_source") or "wallet")
    ttl = float(ex.get("capital_sync_ttl_sec", 60))

    state = load_state()
    ledger_cash = float(state.get("initial_capital", 0.0) or 0.0) + float(state.get("realized_pnl", 0.0) or 0.0)

    if paper or source != "wallet":
        out = {"source": "ledger", "usd": round(max(ledger_cash, 0.0), 2), "ok": True,
               "detail": "paper mode" if paper else f"capital_source={source}"}
        _write_capital_file({**out, "ts": time.time(), "iso": _now_iso()})
        return out

    snap = _read_capital_file()
    age = time.time() - float(snap.get("ts") or 0.0)
    if not force and snap.get("ok") and age < ttl:
        return {**snap, "age_sec": round(age, 1)}

    cap = executor.sync_wallet_capital(force=force, cfg=cfg)
    if not cap.get("ok"):
        detail = cap.get("detail") or "wallet balance read failed"
        # MONEY SAFETY: in LIVE mode we refuse to size positions on a number we
        # could not verify against the real wallet. db._fallback_state() returns
        # a fictitious $10,000 when the ledger cannot be read, and sizing 1% of
        # that would attempt a $200 trade against a wallet holding far less.
        # Refusing is fail-safe and loud; guessing is silent and expensive.
        # A recent successful reading may still be used briefly (grace window).
        grace = float(ex.get("capital_sync_grace_sec", 300))
        snap_age = time.time() - float(snap.get("ts") or 0.0)
        if snap.get("ok") and snap.get("source") == "wallet" and snap_age < grace:
            _LOGGER.warning("Capital sync failed (%s) — using last good wallet reading "
                            "$%.2f from %.0fs ago (grace %.0fs)",
                            detail, float(snap.get("usd") or 0.0), snap_age, grace)
            out = {"source": "wallet", "usd": float(snap.get("usd") or 0.0), "ok": True,
                   "detail": f"stale-but-in-grace: {detail}", "stale": True,
                   "age_sec": round(snap_age, 1)}
        else:
            _LOGGER.error("Capital sync failed (%s) and no fresh wallet reading is "
                          "available — LIVE position sizing is BLOCKED until the "
                          "wallet can be read. Fix the CLI/auth and it resumes "
                          "automatically.", detail)
            out = {"source": "wallet", "usd": 0.0, "ok": False, "detail": detail,
                   "blocked": True}
            try:
                audit.log_event(category="RISK", level="ERROR",
                                message=f"LIVE capital unreadable — trading blocked: {detail}",
                                data={"detail": str(detail)[:300]})
            except Exception:
                pass
        _write_capital_file({**out, "ts": time.time(), "iso": _now_iso()})
        return out

    usd = float(cap.get("total_usd", 0.0) or 0.0)
    out = {"source": "wallet", "usd": round(usd, 2), "ok": True, "detail": "",
           "wallet": cap.get("wallet"), "usdc": cap.get("usdc"), "sol": cap.get("sol"),
           "sol_price": cap.get("sol_price"), "deployable_sol": cap.get("deployable_sol"),
           "sol_reserve": cap.get("sol_reserve")}
    _write_capital_file({**out, "ts": time.time(), "iso": _now_iso()})

    if rebase:
        _maybe_rebase(usd, cap, state)

    return out


def _maybe_rebase(usd: float, cap: dict, state: dict) -> None:
    """Rebase initial_capital onto the real wallet figure when it is implausible.

    Guarded so it is convergent rather than something that fires every cycle:
      * never with closed-trade history (ROI/win-rate would change meaning),
      * never while a position is open (the ledger would no longer add up),
      * only when the ledger figure is below the minimum tradable size, or is
        out by more than 50% of the wallet reading.
    Without the last guard the drawdown baseline would be reset on every cycle
    and the max_drawdown circuit breaker would become meaningless.
    """
    try:
        if usd <= 0:
            return
        if state.get("closed_positions") or (state.get("open_positions") or {}):
            return
        ledger = float(state.get("initial_capital") or 0.0)
        cfg = load_config()
        min_trade = float((cfg.get("execution") or {}).get("min_trade_usd", 1.0))
        implausible = ledger < min_trade or abs(ledger - usd) > 0.5 * max(usd, 1.0)
        if not implausible:
            return
        if db.atomic_update_initial_capital(usd):
            _LOGGER.info("initial_capital rebased to live wallet capital: $%.2f (was $%.2f)",
                         usd, ledger)
            audit.log_event(
                category="SYSTEM", level="INFO",
                message=f"Capital base rebased to live wallet balance: ${usd:,.2f} "
                        f"(was ${ledger:,.2f}; USDC ${float(cap.get('usdc') or 0):,.2f} + "
                        f"{float(cap.get('deployable_sol') or 0):.4f} SOL)",
                data={"usd": round(usd, 2), "previous": round(ledger, 2), "source": "wallet"},
            )
    except Exception as e:
        _LOGGER.debug("initial_capital rebase skipped: %s", e)


def deployable_capital(cfg: dict = None, state: dict = None, force: bool = False) -> float:
    """USD available to deploy into new positions right now.

    In LIVE+wallet mode this is the verified wallet balance (0.0 if it could not
    be read, which blocks sizing). Only paper mode / capital_source=ledger falls
    back to ledger cash.
    """
    res = sync_capital_base(force=force, cfg=cfg)
    try:
        return max(0.0, float(res.get("usd") or 0.0))
    except Exception:
        cfg = cfg or load_config()
        if not bool(cfg.get("paper_mode", True)):
            return 0.0
        state = state or load_state()
        return max(0.0, float(state.get("initial_capital", 0.0) or 0.0)
                   + float(state.get("realized_pnl", 0.0) or 0.0))


def capital_info() -> dict:
    """Last known capital snapshot for the dashboard / status output."""
    d = _read_capital_file()
    if d.get("ts"):
        d["age_sec"] = round(time.time() - float(d["ts"]), 1)
    return d


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


def _can_open(state: dict, cfg: dict, size_usd: float, capital_usd: float = None,
              floor_applied: bool = False) -> Tuple[bool, str]:
    """Exposure/slot gate. `capital_usd` is the DEPLOYABLE base (live wallet
    balance in live mode) rather than ledger equity, so the max_exposure cap
    refers to money the bot can actually spend.

    When `floor_applied` is set, max_exposure is deliberately allowed to be
    exceeded: on a small wallet a 30% cap is under $1, so enforcing it would
    make the minimum order unreachable and the bot unable to trade at all. The
    slot limit and the "never spend money you do not have" rule still apply —
    _compute_size already capped the floor at unexposed capital.
    """
    rm = cfg.get("risk_management", {})
    max_open = int(rm.get("max_open_positions", 5))
    max_exp_pct = float(rm.get("max_exposure", 30.0))
    open_count = len(state.get("open_positions") or {})
    if open_count >= max_open:
        return False, f"max_open_positions reached ({max_open})"
    base = float(capital_usd) if capital_usd is not None else equity(state)
    exposure = sum(float(p.get("size_usd", 0.0)) for p in (state.get("open_positions") or {}).values())
    cap = base * (max_exp_pct / 100.0)
    if exposure + size_usd > cap:
        if floor_applied:
            return True, "ok (max_exposure waived for the minimum-trade floor)"
        return False, (f"max_exposure limit (${exposure + size_usd:,.2f} would exceed "
                       f"{max_exp_pct:.0f}% of ${base:,.2f} = ${cap:,.2f})")
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


def _compute_size(decision: dict, cfg: dict, state: dict) -> dict:
    """The ONE place position size is decided.

    open_position() and prospective_size() both call this, so the tradability
    gate can never disagree with the size that is actually sent — they used to
    duplicate the arithmetic and could drift apart.

    Sizing order:
      1. risk-based notional = capital x risk_pct / stop_pct
      2. capped by max_exposure and max_trade_usd
      3. RAISED to min_trade_usd if it fell below  (the floor)

    Step 3 is deliberate: `min_trade_usd` is the smallest order the exchange
    will accept, so it is a floor to clamp up to, not a threshold to reject at.
    With $2.06 of capital a 4% risk band computes a $0.04 position; cancelling
    that trade means the bot can never operate at all on a small wallet. Taking
    the $1 floor instead lets it trade, which is what the operator asked for.

    The floor is bounded by ONE hard rule: it may never exceed capital that is
    actually in the wallet and not already exposed. Trading $1 you do not have
    would just fail at the swap (or worse, be rejected mid-route), so that case
    is still refused — with a reason that says so plainly.
    """
    rm = cfg.get("risk_management", {}) or {}
    xs = cfg.get("exit_strategy", {}) or {}
    ex_cfg = cfg.get("execution", {}) or {}
    ps_cfg = cfg.get("position_sizing", {}) or {}

    risk_pct = _risk_pct_for(float(decision.get("confidence_score") or 0), cfg)
    max_exp_pct = float(rm.get("max_exposure", 30.0))
    stop_pct = float(xs.get("stop_loss_percentage", 50.0)) / 100.0

    capital_usd = deployable_capital(cfg, state)
    eq = capital_usd if capital_usd > 0 else equity(state)

    risk_usd = eq * (risk_pct / 100.0)
    raw_size = risk_usd / stop_pct if stop_pct > 0 else risk_usd

    ceil_usd = float(ex_cfg.get("max_trade_usd", 0.0) or 0.0)
    max_exp_usd = eq * (max_exp_pct / 100.0)
    size_usd = min(raw_size, max_exp_usd)
    if ceil_usd:
        size_usd = min(size_usd, ceil_usd)

    floor_usd = float(ps_cfg.get("min_position_usd", ex_cfg.get("min_trade_usd", 1.0)) or 0.0)
    floor_enabled = bool(ps_cfg.get("min_trade_is_floor", True))

    exposure = sum(float(p.get("size_usd", 0.0) or 0.0)
                   for p in (state.get("open_positions") or {}).values())
    available = max(0.0, eq - exposure)

    floor_applied = False
    blocked = None
    if floor_usd > 0 and size_usd < floor_usd - 1e-9:
        if floor_enabled:
            # Clamp up to the floor, but never above what the wallet can fund.
            target = min(floor_usd, available)
            if target < floor_usd - 1e-9:
                blocked = (
                    f"INSUFFICIENT_CAPITAL_FOR_MINIMUM_TRADE: the minimum order is "
                    f"${floor_usd:,.2f} but only ${available:,.2f} is available "
                    f"(${eq:,.2f} deployable - ${exposure:,.2f} already in "
                    f"{len(state.get('open_positions') or {})} open position(s)). "
                    f"Fund the wallet, close a position, or lower "
                    f"execution.min_trade_usd."
                )
            else:
                size_usd = floor_usd
                floor_applied = True
        else:
            blocked = (
                f"SIZE_BELOW_FLOOR: computed ${size_usd:,.4f} from ${eq:,.2f} "
                f"deployable capital at {risk_pct:.1f}% risk is below the "
                f"${floor_usd:,.2f} minimum (position_sizing.min_trade_is_floor "
                f"is false, so it is rejected instead of raised)."
            )

    # The risk actually being taken, after any floor override. Honest number for
    # the dashboard/audit: a $1 position on $2.06 with a 50% stop risks 24% of
    # capital, not the 4% the confidence band asked for.
    effective_risk_pct = 0.0
    if eq > 0:
        effective_risk_pct = (size_usd * (stop_pct if stop_pct > 0 else 1.0)) / eq * 100.0

    return {
        "size_usd": round(size_usd, 6),
        "capital_usd": round(float(eq), 2),
        "risk_pct": round(float(risk_pct), 3),
        "raw_size_usd": round(raw_size, 6),
        "floor_usd": floor_usd,
        "ceil_usd": ceil_usd,
        "floor_applied": floor_applied,
        "blocked": blocked,
        "above_floor": size_usd >= floor_usd - 1e-9,
        "exposure_usd": round(exposure, 2),
        "available_usd": round(available, 2),
        "max_exposure_usd": round(max_exp_usd, 2),
        "exposure_overridden": bool(floor_applied and size_usd > max_exp_usd + 1e-9),
        "effective_risk_pct": round(effective_risk_pct, 3),
        "stop_pct": round(stop_pct, 6),
    }


def prospective_size(decision: dict, cfg: dict = None) -> dict:
    """Compute the size open_position WOULD use, without opening anything.

    The live tradability gate needs the real notional (a $1 probe would pass a
    route check that the actual order might fail on a thin market), but it must
    run before the ledger position exists. Delegates to _compute_size so the two
    can never disagree.
    """
    cfg = cfg or load_config()
    return _compute_size(decision, cfg, load_state())


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

    xs = cfg.get("exit_strategy", {})
    stages = xs.get("take_profit_stages") or []
    max_hours = float(xs.get("max_holding_time_hours", 48))

    # Sizing base: real deployable capital (live wallet) instead of a static
    # ledger number. This is the fix for "$2.06 capital -> $0.04 positions".
    # risk_pct / max_exposure / stop_loss all live in _compute_size now — do not
    # recompute them here or the two will disagree.
    sizing = _compute_size(decision, cfg, state)
    risk_pct = float(sizing["risk_pct"])
    if sizing.get("blocked"):
        return {"ok": False, "reason": sizing["blocked"]}

    eq = float(sizing["capital_usd"])
    size_usd = float(sizing["size_usd"])

    if sizing.get("floor_applied"):
        # A real risk-model override with real money: say so loudly, once.
        _LOGGER.warning(
            "Minimum-trade floor applied: %s sized at $%.2f (risk model asked for "
            "$%.4f on $%.2f capital). Effective risk is %.1f%% of capital, not the "
            "configured %.1f%%.",
            mint, size_usd, float(sizing["raw_size_usd"]), eq,
            float(sizing["effective_risk_pct"]), float(sizing["risk_pct"]),
        )
        try:
            audit.log_event(
                category="RISK", level="WARNING",
                message=(f"min_trade_usd floor applied: ${size_usd:,.2f} position on "
                         f"${eq:,.2f} capital (risk model asked ${float(sizing['raw_size_usd']):,.4f}). "
                         f"Effective risk {float(sizing['effective_risk_pct']):.1f}% of capital."),
                data={"mint": mint, "size_usd": size_usd, "capital_usd": eq,
                      "raw_size_usd": sizing["raw_size_usd"],
                      "effective_risk_pct": sizing["effective_risk_pct"],
                      "exposure_overridden": sizing["exposure_overridden"]},
            )
        except Exception:
            pass

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
    ok, reason = _can_open(state, cfg, size_usd, capital_usd=eq,
                           floor_applied=bool(sizing.get("floor_applied")))
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
        "capital_base_usd": round(float(eq), 2),
        "risk_pct_used": round(float(risk_pct), 3),
        "min_floor_applied": bool(sizing.get("floor_applied")),
        "effective_risk_pct": float(sizing.get("effective_risk_pct") or 0.0),
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
