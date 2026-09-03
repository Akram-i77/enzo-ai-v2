#!/usr/bin/env python3
"""min_trade_usd must behave as a FLOOR, not a rejection threshold.

Operator requirement: when capital is small enough that the risk-based position
size falls below min_trade_usd, the size is RAISED to the floor and the trade
still executes. It is only refused when the wallet genuinely cannot fund the
floor.

Run:  python3 tests/test_min_trade_floor.py
"""
import json
import os
import shutil
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

PASS = FAIL = 0


def ok(cond, label, extra=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  \033[32mPASS\033[0m  {label}" + (f"   {extra}" if extra != "" else ""))
    else:
        FAIL += 1
        print(f"  \033[31mFAIL\033[0m  {label}" + (f"   {extra}" if extra != "" else ""))
    return bool(cond)


def section(t):
    print(f"\n=== {t} ===")


SANDBOX = tempfile.mkdtemp(prefix="enzo-floor-")
os.makedirs(os.path.join(SANDBOX, "config"), exist_ok=True)
os.makedirs(os.path.join(SANDBOX, "data"), exist_ok=True)
for f in ("enzo-config.yaml", "enzo-control.json", "enzo-watchlist.json", "enzo-secrets.json"):
    src = os.path.join(ROOT, "config", f)
    if os.path.exists(src):
        shutil.copy(src, os.path.join(SANDBOX, "config", f))
os.environ["ENZO_HOME"] = SANDBOX
os.environ["MOCK_STATE"] = "{}"
from conftest_paths import install_mock_on_path
MOCKBIN = install_mock_on_path()

import enzo.core.config as C          # noqa: E402
from enzo.execution import portfolio, executor   # noqa: E402
from enzo.core import db               # noqa: E402

BASE_CFG = C.load_config()
GOOD_MINT = "DezXAZ8z7PnrnRJjz3wXBoRgixCa6xjnB7YaB1pPB263"

# A BUY decision whose confidence band asks for 4% risk.
DECISION = {
    "decision": "BUY", "token_symbol": "BONK", "mint_address": GOOD_MINT,
    "confidence_score": 88.0, "entry_price": 0.0000123,
    "entry_market_cap": 45_000_000.0, "market_cap_usd": 45_000_000.0,
    "stop_loss_mc": 22_500_000.0, "take_profit_mc": 135_000_000.0,
    "axis_scores": {}, "signals": [], "features": {},
}


def with_capital(usd, cfg=None, open_positions=None):
    """Pin deployable capital so sizing is deterministic without a wallet."""
    cfg = json.loads(json.dumps(cfg or BASE_CFG))
    portfolio.deployable_capital = lambda *a, **k: float(usd)
    state = portfolio.load_state()
    state["open_positions"] = open_positions or {}
    portfolio.load_state = lambda: state
    return cfg, state


def reset():
    portfolio.deployable_capital = lambda *a, **k: 0.0
    portfolio.load_state = db.get_full_state


print(f"sandbox: {SANDBOX}")
print(f"min_trade_usd = {BASE_CFG['execution']['min_trade_usd']} · "
      f"max_trade_usd = {BASE_CFG['execution']['max_trade_usd']} · "
      f"max_exposure = {BASE_CFG['risk_management']['max_exposure']}% · "
      f"stop_loss = {BASE_CFG['exit_strategy'].get('stop_loss_percentage')}%")

# ─────────────────────────────────────────────────────────────────────────────
section("1. the exact reported case: $2.06 capital -> $0.04 computed")
cfg, _ = with_capital(2.06)
sz = portfolio.prospective_size(DECISION, cfg)
# 2.06 capital x 4.0% risk (confidence 88 band) / 50% stop = $0.1648.
# Assert the relationship, not a magic number, so a config tweak cannot
# silently invalidate the expectation.
exp_raw = 2.06 * (sz["risk_pct"] / 100.0) / sz["stop_pct"]
ok(abs(sz["raw_size_usd"] - exp_raw) < 1e-4,
   f"risk model computes ${sz['raw_size_usd']:,.4f} = 2.06 x {sz['risk_pct']}% / {sz['stop_pct']:.2f}")
ok(sz["raw_size_usd"] < sz["floor_usd"],
   f"computed size is below the ${sz['floor_usd']:,.2f} floor (the case under test)")
ok(sz["size_usd"] == 1.0, f"size RAISED to the floor", f"${sz['size_usd']:,.2f}")
ok(sz["floor_applied"] is True, "floor_applied flag set")
ok(sz["blocked"] is None, "not blocked")
ok(sz["exposure_overridden"] is True,
   f"max_exposure (${sz['max_exposure_usd']:,.2f}) deliberately waived")
ok(sz["effective_risk_pct"] > sz["risk_pct"],
   f"effective risk {sz['effective_risk_pct']}% reported honestly vs configured {sz['risk_pct']}%")

res = portfolio.open_position(dict(DECISION), cfg)
ok(res.get("ok") is True, "position actually OPENS", str(res.get("reason"))[:90])
pos = res.get("position") or {}
ok(float(pos.get("size_usd") or 0) == 1.0, "ledger size is $1.00", str(pos.get("size_usd")))
ok(pos.get("min_floor_applied") is True, "min_floor_applied persisted on the position")
ok(float(pos.get("effective_risk_pct") or 0) > 1.0, "effective_risk_pct persisted",
   str(pos.get("effective_risk_pct")))

# ─────────────────────────────────────────────────────────────────────────────
section("2. SAFETY: floor may never exceed money the wallet actually has")
reset()
cfg, _ = with_capital(0.50)
sz = portfolio.prospective_size(DECISION, cfg)
ok(sz["blocked"] is not None, "blocked with $0.50 capital")
ok(str(sz["blocked"]).startswith("INSUFFICIENT_CAPITAL_FOR_MINIMUM_TRADE"),
   "reason names the real problem", str(sz["blocked"])[:60])
res = portfolio.open_position(dict(DECISION), cfg)
ok(res.get("ok") is False, "no position opened")
ok("INSUFFICIENT_CAPITAL" in str(res.get("reason", "")), "open_position agrees with the sizer")

section("2b. capital already exposed counts against the floor")
reset()
cfg, _ = with_capital(2.06, open_positions={
    "OTHERMINT111111111111111111111111111111111": {"size_usd": 1.50, "amount": 1.0}})
sz = portfolio.prospective_size(DECISION, cfg)
ok(sz["available_usd"] == 0.56, f"available = 2.06 - 1.50", f"${sz['available_usd']:,.2f}")
ok(sz["blocked"] is not None, "blocked — $1 floor exceeds the $0.56 available")
res = portfolio.open_position(dict(DECISION), cfg)
ok(res.get("ok") is False, "no position opened")

section("2c. exactly enough for the floor is allowed")
reset()
cfg, _ = with_capital(1.00)
sz = portfolio.prospective_size(DECISION, cfg)
ok(sz["blocked"] is None, "not blocked at exactly $1.00")
ok(sz["size_usd"] == 1.0, "sized at the floor", f"${sz['size_usd']:,.2f}")

# ─────────────────────────────────────────────────────────────────────────────
section("3. healthy capital is untouched by the floor")
reset()
cfg, _ = with_capital(559.40)
sz = portfolio.prospective_size(DECISION, cfg)
ok(sz["floor_applied"] is False, "floor not applied when the risk size clears it")
ok(sz["size_usd"] > 1.0, f"normal risk sizing ${sz['size_usd']:,.2f}")
ok(abs(sz["size_usd"] - sz["raw_size_usd"]) < 1e-6, "size == risk-model size, unmodified")
ok(sz["exposure_overridden"] is False, "max_exposure respected")

section("3b. max_trade_usd ceiling still wins")
reset()
cfg, _ = with_capital(1_000_000.0)
sz = portfolio.prospective_size(DECISION, cfg)
ceil = float(cfg["execution"]["max_trade_usd"])
ok(sz["size_usd"] <= ceil + 1e-6, f"capped at max_trade_usd ${ceil:,.0f}",
   f"${sz['size_usd']:,.2f}")

section("3c. max_open_positions is still enforced even with the floor")
reset()
cfg, _ = with_capital(2.06, open_positions={
    f"MINT{i}": {"size_usd": 0.0, "amount": 0.0}
    for i in range(int(cfg["risk_management"]["max_open_positions"]))})
res = portfolio.open_position(dict(DECISION), cfg)
ok(res.get("ok") is False, "slot limit blocks the trade")
ok("max_open_positions" in str(res.get("reason", "")), "reason is the slot limit",
   str(res.get("reason"))[:60])

# ─────────────────────────────────────────────────────────────────────────────
section("4. min_trade_is_floor: false restores the old reject behaviour")
reset()
cfg_off = json.loads(json.dumps(BASE_CFG))
cfg_off.setdefault("position_sizing", {})["min_trade_is_floor"] = False
cfg, _ = with_capital(2.06, cfg_off)
sz = portfolio.prospective_size(DECISION, cfg)
ok(sz["floor_applied"] is False, "floor not applied")
ok(str(sz["blocked"] or "").startswith("SIZE_BELOW_FLOOR"), "rejected as SIZE_BELOW_FLOOR",
   str(sz["blocked"])[:60])

# ─────────────────────────────────────────────────────────────────────────────
section("5. the executor accepts exactly the floor (boundary, no float trap)")
reset()
r = executor.execute_swap(
    from_token=executor.USDC_MAINNET, to_token=GOOD_MINT, from_amount=1.0,
    wallet="enzo-trading", cfg=BASE_CFG, direction="buy")
ok(r.get("ok") is True, "$1.00 exactly passes the executor's own min guard",
   str(r.get("reason") or "")[:70])
r2 = executor.execute_swap(
    from_token=executor.USDC_MAINNET, to_token=GOOD_MINT, from_amount=0.99,
    wallet="enzo-trading", cfg=BASE_CFG, direction="buy")
ok(r2.get("ok") is False and r2.get("reason_code") == "BELOW_MINIMUM_TRADE",
   "$0.99 is still rejected", str(r2.get("reason_code")))

section("5b. a $1 buy executes end-to-end through the mock CLI")
reset()
b = executor.buy_token(mint=GOOD_MINT, amount_usd=1.0, entry_price=0.0000123,
                       explanation="floor test", cfg=BASE_CFG)
ok(b.get("ok") is True, "buy_token succeeded at the floor", str(b.get("reason") or "")[:70])
ok(bool(b.get("tx_hash")), "tx_hash returned", str(b.get("tx_hash"))[:24])

# ─────────────────────────────────────────────────────────────────────────────
section("6. sizer and gate can never disagree (shared code path)")
reset()
for cap in (0.5, 1.0, 2.06, 50.0, 559.4, 100000.0):
    cfg, _ = with_capital(cap)
    a = portfolio.prospective_size(DECISION, cfg)
    b = portfolio._compute_size(DECISION, cfg, portfolio.load_state())
    if a["size_usd"] != b["size_usd"] or a["blocked"] != b["blocked"]:
        ok(False, f"prospective_size == _compute_size at ${cap:,.2f}")
        break
    reset()
else:
    ok(True, "prospective_size == _compute_size across 6 capital levels")

print("\n" + "=" * 66)
print(f"  RESULT: {PASS} passed, {FAIL} failed")
print("=" * 66)
try:
    shutil.rmtree(SANDBOX)
except Exception:
    pass
sys.exit(1 if FAIL else 0)
