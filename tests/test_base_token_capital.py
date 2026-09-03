#!/usr/bin/env python3
"""Base-token-aware capital, and precise "why can't I trade this" reasons.

Two things are asserted here.

1. SPENDABLE != TOTAL
   A wallet holding $500 USDC and $60 of SOL has $560 of wealth, but a swap can
   only spend the asset configured as execution.base_token. MoonPay's own
   pump.fun flow funds buys from SOL. Before this was fixed, deployable_capital()
   returned the combined total regardless of base token, so switching
   base_token to SOL on a USDC-heavy wallet would size every trade against money
   that could not be sent — producing INSUFFICIENT_BALANCE on every buy while the
   dashboard reported healthy capital. `usd`/`total_usd` stays total wealth (the
   equity and drawdown baseline); `spendable_usd` is what sizing uses.

2. A failed quote must say WHY
   NO_ROUTE, TOKEN_NOT_SUPPORTED and CHAIN_NOT_SUPPORTED are different problems
   with different fixes. Collapsing them is what led to the conclusion that
   "MoonPay only supports 50 whitelisted tokens" and the proposal to replace the
   executor entirely.
"""
import copy
import json
import os
import sys
import tempfile

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(_HERE))
sys.path.insert(0, _HERE)

from conftest_paths import install_mock_on_path

if not install_mock_on_path():
    print("\n  ABORT  no mock MoonPay CLI found (expected tests/mockbin/mp).")
    sys.exit(2)

from enzo.core import config as C
from enzo.execution import executor as X
from enzo.execution import portfolio as P

PASS = FAIL = 0


def check(name, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  \033[32mPASS\033[0m  {name}" + (f"   {detail}" if detail else ""))
    else:
        FAIL += 1
        print(f"  \033[31mFAIL\033[0m  {name}   {detail}")


KNOWN = "DezXAZ8z7PnrnRJjz3wXBoRgixCa6xjnB7YaB1pPB263"
UNKNOWN = "UNKNOWNmintThatMoonPayDoesNotList11111111111111"

USDC_BAL, SOL_BAL = 500.0, 0.35
RESERVE = 0.02


def cfg_with(base: str) -> dict:
    c = copy.deepcopy(C.DEFAULTS)
    c["chain"] = "sol"                    # GMGN's spelling — the real production value
    c["paper_mode"] = False
    c["execution"].update({
        "wallet_name": "enzo-trading", "base_token": base, "capital_source": "wallet",
        "min_trade_usd": 1.0, "max_trade_usd": 500.0, "sol_fee_reserve": RESERVE,
        "retry_attempts": 1, "moonpay_bin": "", "moonpay_chain": "",
    })
    return c


tmp = tempfile.mkdtemp(prefix="enzo-base-")
os.environ["ENZO_HOME"] = tmp
os.environ["MOCK_STATE"] = json.dumps({"usdc": USDC_BAL, "sol": SOL_BAL})
os.environ.pop("MOCK_ARGV_LOG", None)

try:
    # ── 1. spendable vs total, per base token ────────────────────────────────
    print("\n=== 1. USDC base ===")
    cfg_u = cfg_with("USDC")
    X._BIN_CACHE.update({"path": None, "checked_at": 0.0})
    X._CAPITAL_CACHE.update({"ok": False, "ts": 0.0})
    X.clear_gate()
    r = P.sync_capital_base(force=True, cfg=cfg_u)

    total = float(r.get("total_usd") or r.get("usd") or 0)
    spend = float(r.get("spendable_usd") or 0)
    sol_px = float(r.get("sol_price") or 0)
    check("capital read succeeded", bool(r.get("ok")), str(r.get("detail"))[:60])
    check("base_token reported", r.get("base_token") == "USDC", str(r.get("base_token")))
    check("total wealth includes BOTH assets", total > USDC_BAL,
          f"total=${total:,.2f} (USDC ${USDC_BAL} + SOL)")
    check("spendable == the USDC balance only", abs(spend - USDC_BAL) < 0.01,
          f"spendable=${spend:,.2f}")
    check("spendable < total (SOL is not spendable as USDC)", spend < total,
          f"{spend:,.2f} < {total:,.2f}")
    check("deployable_capital returns the SPENDABLE figure",
          abs(P.deployable_capital(cfg=cfg_u, force=True) - USDC_BAL) < 0.01,
          f"${P.deployable_capital(cfg=cfg_u):,.2f}")

    print("\n=== 2. SOL base (what MoonPay's pump.fun flow uses) ===")
    cfg_s = cfg_with("SOL")
    X._CAPITAL_CACHE.update({"ok": False, "ts": 0.0})
    X.clear_gate()
    r2 = P.sync_capital_base(force=True, cfg=cfg_s)
    spend2 = float(r2.get("spendable_usd") or 0)
    total2 = float(r2.get("total_usd") or r2.get("usd") or 0)
    expect_sol = max(0.0, SOL_BAL - RESERVE) * sol_px
    check("base_token reported", r2.get("base_token") == "SOL", str(r2.get("base_token")))
    check("spendable == (SOL - fee reserve) * price",
          abs(spend2 - expect_sol) < 0.05, f"spendable=${spend2:,.2f} expected=${expect_sol:,.2f}")
    check("fee reserve is excluded from spendable", spend2 < SOL_BAL * sol_px,
          f"${spend2:,.2f} < ${SOL_BAL * sol_px:,.2f}")
    check("total wealth is unchanged by the base token",
          abs(total2 - total) < 0.01, f"${total2:,.2f} vs ${total:,.2f}")
    check("USDC-heavy wallet does NOT inflate SOL spendable",
          spend2 < USDC_BAL, f"spendable=${spend2:,.2f} while USDC=${USDC_BAL}")
    check("deployable_capital returns the SOL figure",
          abs(P.deployable_capital(cfg=cfg_s, force=True) - expect_sol) < 0.05,
          f"${P.deployable_capital(cfg=cfg_s):,.2f}")

    # The bug this guards: sizing against total while only SOL can be sent.
    print("\n=== 3. sizing cannot exceed what the base token can fund ===")
    dep = P.deployable_capital(cfg=cfg_s, force=True)
    check("with SOL base, deployable is far below total wealth",
          dep < total2 * 0.5, f"deployable=${dep:,.2f} vs wealth=${total2:,.2f}")

    # ── 4. precise failure reasons ───────────────────────────────────────────
    print("\n=== 4. why a trade cannot be filled ===")
    X._CAPITAL_CACHE.update({"ok": False, "ts": 0.0})
    X.clear_gate()
    g_known = X.check_tradable(KNOWN, 5.0, cfg=cfg_u)
    check("a normal mint is routable", bool(g_known.get("tradable")),
          f"reason={g_known.get('reason')}")

    X.clear_gate()
    os.environ["MOCK_STATE"] = json.dumps({"usdc": USDC_BAL, "sol": SOL_BAL, "no_route": True})
    g_nr = X.check_tradable(KNOWN, 5.0, cfg=cfg_u)
    check("no-liquidity mint is NOT tradable", not g_nr.get("tradable"),
          f"reason={g_nr.get('reason')}")
    check("...and it explains that MoonPay knows the mint but cannot route it",
          g_nr.get("detail") is not None, str(g_nr.get("detail"))[:80])

    X.clear_gate()
    os.environ["MOCK_STATE"] = json.dumps({"usdc": USDC_BAL, "sol": SOL_BAL})
    g_unk = X.check_tradable(UNKNOWN, 5.0, cfg=cfg_u)
    check("an unrecognised mint is NOT tradable", not g_unk.get("tradable"),
          f"reason={g_unk.get('reason')}")
    check("...and it is reported as TOKEN_NOT_SUPPORTED, not a bare NO_ROUTE",
          g_unk.get("reason") == X.E_TOKEN_UNSUPPORTED, f"reason={g_unk.get('reason')}")

    chk = X.token_check(UNKNOWN, "solana", cfg_u)
    check("token_check rejects an unknown mint", not chk.get("ok"), f"reason={chk.get('reason')}")
    chk2 = X.token_check(KNOWN, "solana", cfg_u)
    check("token_check accepts a known mint", bool(chk2.get("ok")), f"reason={chk2.get('reason')}")

    print("\n=== 5. the three reasons stay distinct ===")
    check("TOKEN_NOT_SUPPORTED != NO_ROUTE", X.E_TOKEN_UNSUPPORTED != X.E_NO_ROUTE)
    check("NO_ROUTE != CHAIN_NOT_SUPPORTED", X.E_NO_ROUTE != X.E_BAD_CHAIN)
    check("all three are stable strings", all(isinstance(v, str) and v for v in
          (X.E_TOKEN_UNSUPPORTED, X.E_NO_ROUTE, X.E_BAD_CHAIN)))
finally:
    for k in ("ENZO_HOME", "MOCK_STATE"):
        os.environ.pop(k, None)
    try:
        for root, _dirs, files in os.walk(tmp, topdown=False):
            for f in files:
                try:
                    os.unlink(os.path.join(root, f))
                except Exception:
                    pass
            try:
                os.rmdir(root)
            except Exception:
                pass
    except Exception:
        pass

print("\n" + "=" * 68)
print(f"  RESULT: {PASS} passed, {FAIL} failed")
print("=" * 68)
sys.exit(1 if FAIL else 0)
