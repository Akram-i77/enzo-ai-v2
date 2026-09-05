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
import time

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

# ─────────────────────────────────────────────────────────────────────────────
# The reported live failure: "the bot tried to buy $1.00 of PVP but it was
# rejected as BELOW_MINIMUM_TRADE". That code is ENZO's own - classify_error()
# can never produce it from a MoonPay message - and the guard that raised it
# RE-DERIVED the dollar size from the SOL amount: dollars -> SOL (/px) -> dollars
# (*px). In binary floating point (1.0/px)*px is 0.9999999999999999 for many
# prices, and `usd_equiv < min_trade` had no tolerance, so a $1.00 order sized
# exactly at the $1.00 floor was rejected with the detail
# "$1.0000 < execution.min_trade_usd $1.00". Section 5 above did not catch it
# because it drives the USDC path, where no conversion happens - and the
# operator's base_token is SOL.
section("5a. base_token=SOL: the floor survives the USD->SOL->USD round trip")
reset()
from enzo.providers import gmgn as _gmgn

SOL_CFG = json.loads(json.dumps(BASE_CFG))
SOL_CFG["execution"]["base_token"] = "SOL"
_real_sol_price = _gmgn.sol_price_usd
_real_sol_source = getattr(_gmgn, "sol_price_source", None)

# Prices around today's SOL at which the naive round trip loses a bit.
LOSSY = [c / 100.0 for c in range(19000, 21501)
         if (1.0 / (c / 100.0)) * (c / 100.0) < 1.0]
ok(len(LOSSY) > 100, "the round trip really is lossy at many live SOL prices",
   f"{len(LOSSY)} of 2501 prices in $190-$215 (e.g. ${LOSSY[0]:.2f})")

rejected = []
for px in LOSSY[:45]:
    executor._SOL_PRICE_CACHE.update({"price": 0.0, "ts": 0.0, "source": "unknown"})
    _gmgn.sol_price_usd = lambda _p=px: _p
    _gmgn.sol_price_source = lambda: "dexscreener"
    r = executor.buy_token(mint=GOOD_MINT, amount_usd=1.0, cfg=SOL_CFG)
    if r.get("reason_code") == "BELOW_MINIMUM_TRADE":
        rejected.append((px, str(r.get("detail"))[:80]))
ok(not rejected, "a $1.00 floor order is NEVER rejected at those 45 prices",
   str(rejected[:2])[:160])

executor._SOL_PRICE_CACHE.update({"price": 0.0, "ts": 0.0, "source": "unknown"})
_gmgn.sol_price_usd = lambda: LOSSY[0]
_gmgn.sol_price_source = lambda: "dexscreener"
rb = executor.buy_token(mint=GOOD_MINT, amount_usd=1.0, cfg=SOL_CFG)
ok(rb.get("ok") is True and bool(rb.get("tx_hash")),
   "and it executes end-to-end through the mock CLI at a lossy price",
   str(rb.get("reason") or rb.get("tx_hash") or "")[:60])
ok(abs(float(rb.get("sol_price") or 0) - LOSSY[0]) < 0.01
   and rb.get("sol_price_source") == "dexscreener",
   "the result records which SOL price and which source produced the amount",
   f"sol_price={rb.get('sol_price')} source={rb.get('sol_price_source')}")

section("5a-2. the gate still fires for a REAL shortfall, and names itself")
executor._SOL_PRICE_CACHE.update({"price": 0.0, "ts": 0.0, "source": "unknown"})
_gmgn.sol_price_usd = lambda: 200.0
_gmgn.sol_price_source = lambda: "dexscreener"
r = executor.execute_swap(from_token=executor.MOONPAY_NATIVE_SOL, to_token=GOOD_MINT,
                          from_amount=0.0025, wallet="enzo-trading", cfg=SOL_CFG,
                          direction="buy")                    # = $0.50, genuinely short
d = str(r.get("detail") or "")
ok(r.get("reason_code") == "BELOW_MINIMUM_TRADE", "$0.50 against a $1.00 floor is still rejected")
ok("NOT a MoonPay rejection" in d and "never called" in d,
   "and the detail says the exchange was never called (this is our gate)", d[:100])
ok("$1.0000 <" not in d and "SOL=$200.00" in d,
   "no self-contradictory '$1.0000 < $1.00'; the price used is shown", d[:120])
ok(abs(float(r.get("usd_equiv") or 0) - 0.5) < 1e-9
   and float(r.get("min_trade_usd") or 0) == 1.0,
   "the rejection carries the numbers (usd_equiv, min_trade_usd) for diagnosis",
   str({k: r.get(k) for k in ("usd_equiv", "min_trade_usd", "sol_price")}))

r2 = executor.execute_swap(from_token=executor.MOONPAY_NATIVE_SOL, to_token=GOOD_MINT,
                           from_amount=0.005, wallet="enzo-trading", cfg=SOL_CFG,
                           direction="buy", usd_notional=1.0)
ok(r2.get("reason_code") != "BELOW_MINIMUM_TRADE",
   "when the caller states the notional, THAT is what the guard compares",
   str(r2.get("reason_code")))

r3 = executor.execute_swap(from_token=executor.USDC_MAINNET, to_token=GOOD_MINT,
                           from_amount=0.999, wallet="enzo-trading", cfg=BASE_CFG,
                           direction="buy")
ok(r3.get("reason_code") == "BELOW_MINIMUM_TRADE",
   "$0.999 is still rejected: the tolerance is a millionth of a dollar, not a percent")

section("5a-3. a GUESSED SOL price is announced, and not cached as if it were real")
executor._SOL_PRICE_CACHE.update({"price": 0.0, "ts": 0.0, "source": "unknown"})
_gmgn.sol_price_usd = lambda: 0.0            # provider dead -> executor falls back
_gmgn.sol_price_source = lambda: "fallback"
px_guess = executor._sol_price(SOL_CFG)
ok(abs(px_guess - 180.0) < 1e-9 and executor.sol_price_source() == "fallback",
   "the fallback is 180.0 and sol_price_source() says 'fallback'",
   f"{px_guess} / {executor.sol_price_source()}")

try:
    db.cache_delete("sol_price:usd")
except Exception:
    pass
import urllib.request as _url


class _DeadNet:
    def __init__(self, *a, **k): pass
    def __enter__(self): raise OSError("no route to host (test)")
    def __exit__(self, *a): return False


_real_urlopen = _url.urlopen
_url.urlopen = _DeadNet
try:
    _p = _gmgn.__dict__.pop("sol_price_usd", None)      # restore the real function
    _gmgn.sol_price_usd = _real_sol_price
    got = _gmgn.sol_price_usd()
    ok(abs(got - 180.0) < 1e-9 and _gmgn.sol_price_source() == "fallback",
       "gmgn.sol_price_usd() reports a guess as a guess", f"{got} / {_gmgn.sol_price_source()}")
    ent = _gmgn._CACHE.get("sol_price:usd")
    ttl_left = (ent[1] - time.time()) if ent else -1
    ok(0 < ttl_left <= 5.5,
       "and caches it for ~5s, NOT the 60s of a live price (a guess must not "
       "size real orders for a minute)", f"{ttl_left:.1f}s left")
finally:
    _url.urlopen = _real_urlopen
    _gmgn.sol_price_usd = _real_sol_price
    if _real_sol_source is not None:
        _gmgn.sol_price_source = _real_sol_source

section("5a-4. base_token=SOL: the wallet must fund the ORDER *and* the fee reserve")
# The reserve check used to look at the fee reserve alone. Since the order is
# paid in SOL too, a wallet just above the reserve could open a position and be
# left with less than the reserve - i.e. unable to pay the fee of its own EXIT.
reset()
executor._SOL_PRICE_CACHE.update({"price": 0.0, "ts": 0.0, "source": "unknown"})
_gmgn.sol_price_usd = lambda: 200.0
_gmgn.sol_price_source = lambda: "dexscreener"
_reserve = float(SOL_CFG["execution"].get("sol_fee_reserve", 0.02))
os.environ["MOCK_STATE"] = json.dumps({"sol": round(_reserve + 0.001, 6), "usdc": 0.0})
r = executor.execute_swap(from_token=executor.MOONPAY_NATIVE_SOL, to_token=GOOD_MINT,
                          from_amount=0.005, wallet="enzo-trading", cfg=SOL_CFG,
                          direction="buy", usd_notional=1.0)
ok(r.get("reason_code") == "INSUFFICIENT_SOL_FOR_FEES",
   "a wallet holding only reserve+$0.20 cannot open a $1 position (it could not close it)",
   str(r.get("reason_code")))
d = str(r.get("detail") or "")
ok("this order" in d and "fee reserve" in d and "short by" in d,
   "and the message itemises order + reserve + shortfall", d[:150])
os.environ["MOCK_STATE"] = json.dumps({"sol": round(_reserve + 0.001, 6), "usdc": 0.0})
rs = executor.execute_swap(from_token=GOOD_MINT, to_token=executor.MOONPAY_NATIVE_SOL,
                           from_amount=1000.0, wallet="enzo-trading", cfg=SOL_CFG,
                           direction="sell")
ok(rs.get("reason_code") != "INSUFFICIENT_SOL_FOR_FEES",
   "the same wallet is still allowed to SELL - an exit is never blocked by the order side",
   str(rs.get("reason_code")))
os.environ["MOCK_STATE"] = json.dumps({"sol": round(_reserve + 0.006, 6), "usdc": 0.0})
r2 = executor.execute_swap(from_token=executor.MOONPAY_NATIVE_SOL, to_token=GOOD_MINT,
                           from_amount=0.005, wallet="enzo-trading", cfg=SOL_CFG,
                           direction="buy", usd_notional=1.0)
ok(r2.get("reason_code") != "INSUFFICIENT_SOL_FOR_FEES",
   "with order+reserve actually covered, the buy proceeds", str(r2.get("reason_code")))
os.environ["MOCK_STATE"] = "{}"

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
