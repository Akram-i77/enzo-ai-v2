#!/usr/bin/env python3
"""Seed the workspace that tests/dashboard_browser_test.js was written against.

Run by tests/test_dashboard_buttons.py with ENZO_HOME pointed at a throwaway
sandbox (never at a real deployment). Everything is written through the real
APIs - db.save_full_state(), the audit JSONL, the capital snapshot,
dashboard.generate() - so the button harness is proved against the same shapes
the trading engine produces, not against hand-made fixtures.

The workspace holds:
  * one open position carrying rug_flags (the 🚩 badge + its tooltip),
  * one closed RUG_TRIPWIRE exit (its own colour pill in the trades table),
  * audit rows with the Layer-0 veto evidence (SNIPER_FLOOD_EARLY, universe
    platform/phase/fees/snipers, top-holder %) and with measured momentum
    windows (1m/5m scored, 1h/24h context only, buy pressure),
  * a capital snapshot, so the KPI cards show a real number.

Usage:  ENZO_HOME=<sandbox> python3 tests/_seed_dashboard_home.py
"""
import json, os, sys, time
from enzo.core import db
from enzo.ui import dashboard

now = time.time()
home = os.environ["ENZO_HOME"]


def w(rel, obj):
    p = os.path.join(home, rel)
    os.makedirs(os.path.dirname(p), exist_ok=True)
    with open(p, "w", encoding="utf-8") as f:
        json.dump(obj, f)


db.init_db()

M1 = "Mint1" + "1" * 39
M2 = "Mint2" + "2" * 39
POSITIONS = {
    M1: {"symbol": "ALPHA", "mint": M1, "entry_price": 1e-05, "size_usd": 12.5,
         "initial_size_usd": 12.5, "entry_market_cap": 40000.0,
         "current_market_cap": 52000.0, "unrealized_pnl": 3.75,
         "stop_loss_mc": 24800.0, "take_profit_mc": 100000.0,
         "peak_market_cap": 55000.0, "trailing_active": True,
         "trailing_stop_mc": 46000.0, "opened_at": now - 900,
         "rug_flags": ["BUNDLERS_TOP20=9"], "entry_liq": 12000.0,
         "entry_holders": 210, "entry_top10_sells": 4.0,
         "signals": ["Liquidity $12,000 >= min $5,000", "5m momentum +11.11%"],
         "axis_scores": {"momentum": {"score": 88}}, "discovery_source": "gmgn_trenches"},
    M2: {"symbol": "BETA", "mint": M2, "entry_price": 2e-06, "size_usd": 8.0,
         "initial_size_usd": 8.0, "entry_market_cap": 20000.0,
         "current_market_cap": 15000.0, "unrealized_pnl": -2.0,
         "stop_loss_mc": 12400.0, "opened_at": now - 3600, "rug_flags": [],
         "price_is_live": False, "signals": [], "trailing_active": False},
}
CLOSED = [
    {"symbol": "WIN", "mint": "W" * 44, "pnl": 21.4, "reason": "TAKE_PROFIT",
     "opened_at": now - 7200, "closed_at": now - 3600, "size_usd": 10.0},
    {"symbol": "RUGGED", "mint": "R" * 44, "pnl": -14.0,
     "reason": "RUG_TRIPWIRE: 2 of 3 votes (liquidity pulled 62%)",
     "opened_at": now - 5000, "closed_at": now - 4000, "size_usd": 11.0},
]
st = db.get_full_state()
st["open_positions"] = POSITIONS
st["closed_positions"] = CLOSED
st["initial_capital"] = 559.4
st["realized_pnl"] = -1.7
db.save_full_state(st)

w("data/run/enzo-capital.json", {"ok": True, "total_usd": 559.4, "spendable_usd": 59.4,
                                 "source": "wallet", "ts": now, "base_token": "SOL"})

AUDIT = [
    {"ts": now, "symbol": "FALL", "mint": "F" * 44, "decision": "IGNORE",
     "confidence": 28, "reason": "momentum negative",
     "axes": {"momentum": 28, "security": 95, "market_structure": 40},
     "momentum_windows": {"1m": -7.41, "5m": -20.0, "scored": ["1m", "5m"],
                          "1h_context": 68.0, "24h_context": 292.0,
                          "buy_pressure_pct": 69.0},
     "rejected_signals": ["SNIPER_FLOOD_EARLY: 4 of the first 8 wallets bought $5,800"],
     "universe": {"pump_v1": True, "platform": "pump.fun", "phase": "migrated",
                  "fees": {"ok": True, "value": 4.12, "unit": "sol"},
                  "snipers": {"sniper_count": 4, "sniper_total_usd": 5800}},
     "top_holder_pct": 12.0},
    {"ts": now, "symbol": "BLIND", "mint": "B" * 44, "decision": "WAIT",
     "confidence": 44, "reason": "no price window measurable",
     "axes": {"momentum": 64},
     "momentum_windows": {"1m": None, "5m": None, "scored": [],
                          "1h_context": None, "24h_context": None,
                          "buy_pressure_pct": None}},
    {"ts": now, "symbol": "OLD", "mint": "O" * 44, "decision": "WAIT",
     "confidence": 50, "reason": "legacy row without axis detail",
     "axes": {"momentum": 50}},
]
ap = os.path.join(home, "data", "enzo-audit.jsonl")
os.makedirs(os.path.dirname(ap), exist_ok=True)
with open(ap, "w", encoding="utf-8") as f:
    for row in AUDIT:
        f.write(json.dumps(row) + "\n")

out = dashboard.generate()
print("SEEDED", len(POSITIONS), "positions,", len(AUDIT), "audit rows,",
      os.path.getsize(out) if out and os.path.exists(str(out)) else 0, "bytes of HTML")
