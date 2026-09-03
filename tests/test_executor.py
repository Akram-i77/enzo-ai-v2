#!/usr/bin/env python3
"""Executor test harness — runs the REAL executor against a mock MoonPay CLI
that reproduces commander's exact option semantics (verified against
@moonpay/cli@1.96.0's own command builder)."""
import json, os, sys, time, copy

os.environ["PATH"] = "/tmp/mockbin:" + os.environ["PATH"]
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from enzo.core import config as C
from enzo.execution import executor as X

LIVE = copy.deepcopy(C.DEFAULTS)
LIVE["paper_mode"] = False
LIVE["execution"].update({"wallet_name": "enzo-trading", "base_token": "USDC",
                          "min_trade_usd": 1.0, "max_trade_usd": 500.0,
                          "sol_fee_reserve": 0.02, "retry_attempts": 2})

PASS = FAIL = 0
def check(name, cond, detail=""):
    global PASS, FAIL
    if cond: PASS += 1; print(f"  \033[32mPASS\033[0m  {name}" + (f"   {detail}" if detail else ""))
    else:    FAIL += 1; print(f"  \033[31mFAIL\033[0m  {name}   {detail}")

# clear any stale gate + binary cache between sections
def reset():
    X._BIN_CACHE.update({"path": None, "checked_at": 0.0})
    X.clear_gate()
    X._CAPITAL_CACHE.update({"ok": False, "ts": 0.0})

MINT = "DezXAZ8z7PnrnRJjz3wXBoRgixCa6xjnB7YaB1pPB263"

print("\n=== 1. binary resolution (was hardcoded to ~/.npm-global/bin/moonpay) ===")
reset()
b = X.resolve_bin(LIVE, force=True)
check("resolve_bin finds 'mp' on PATH", b and b.endswith("/mp"), b or "None")
check("MOONPAY_BIN legacy constant still exported", isinstance(X.MOONPAY_BIN, str))

print("\n=== 2. quote flags (bot used --chain; real schema needs --from-chain/--to-chain) ===")
reset()
q = X.get_quote(X.USDC_MAINNET, MINT, 50.0, "solana", LIVE)
check("get_quote succeeds", isinstance(q, dict) and q.get("route") == "swaps.xyz", str(q)[:90])
check("quote echoes HUMAN amount (50, not 50000000)", q and float(q.get("fromAmount")) == 50.0, str(q and q.get("fromAmount")))

print("\n=== 3. amount units (bot multiplied by 10**decimals) ===")
check("_fmt_amount(50.0)=='50'", X._fmt_amount(50.0) == "50", X._fmt_amount(50.0))
check("_fmt_amount(0.000123) no exponent", "e" not in X._fmt_amount(0.000123).lower(), X._fmt_amount(0.000123))
check("_fmt_amount(2.06)=='2.06'", X._fmt_amount(2.06) == "2.06", X._fmt_amount(2.06))

print("\n=== 4. balances (bot assumed items[0] is SOL — mock puts USDC first) ===")
reset()
check("get_usdc_balance==500.0", X.get_usdc_balance("enzo-trading") == 500.0, str(X.get_usdc_balance()))
check("get_sol_balance==0.35 (matched by address, not index)", X.get_sol_balance("enzo-trading") == 0.35, str(X.get_sol_balance()))
snap = X.get_wallet_snapshot("enzo-trading", LIVE)
check("snapshot sees 3 rows", snap["rows"] == 3, str(snap["symbols"]))

print("\n=== 5. live buy end-to-end (old code died on --yes / bad quote) ===")
reset()
r = X.buy_token(MINT, 50.0, wallet="enzo-trading", entry_price=0.0001, cfg=LIVE)
check("buy ok", r.get("ok") is True, str(r.get("reason")) + " " + str(r.get("detail",""))[:80])
check("tx_hash parsed from 'signature'", isinstance(r.get("tx_hash"), str) and len(r["tx_hash"]) >= 60, str(r.get("tx_hash"))[:24])
check("amount_usd recorded", r.get("amount_usd") == 50.0, str(r.get("amount_usd")))
check("base_token USDC", r.get("base_token") == "USDC")

print("\n=== 6. tx status (bot used --chain/--id; real param is --transactionId) ===")
reset()
st = X.get_tx_status(r["tx_hash"], cfg=LIVE)
check("get_tx_status returns confirmed", isinstance(st, dict) and st.get("status") == "confirmed", str(st)[:80])

print("\n=== 7. sell uses the REAL on-chain balance ===")
reset()
s = X.sell_token(MINT, amount_spl=999999999.0, wallet="enzo-trading", to_token="USDC", cfg=LIVE)
check("sell ok", s.get("ok") is True, str(s.get("reason")))
check("sell clamped to wallet balance (1000000)", s.get("from_amount") == 1000000.0, str(s.get("from_amount")))
reset()
s2 = X.sell_token(MINT, amount_spl=None, wallet="enzo-trading", cfg=LIVE)
check("sell with no ledger amount still sells full balance", s2.get("ok") and s2.get("from_amount") == 1000000.0, str(s2.get("from_amount")))

print("\n=== 8. NO_ROUTE classification + gate cooldown ===")
reset()
os.environ["MOCK_STATE"] = json.dumps({"no_route": True})
X._BIN_CACHE.update({"path": None, "checked_at": 0.0})
r = X.buy_token("NOROUTEabc1111111111111111111111111111111", 50.0, cfg=LIVE)
check("buy fails", r.get("ok") is False)
check("reason == NO_ROUTE", r.get("reason") == X.E_NO_ROUTE, str(r.get("reason")))
check("detail explains bonding-curve reality", "bonding curve" in str(r.get("detail","")).lower())
gated = X.check_tradable("NOROUTEabc1111111111111111111111111111111", 50.0, LIVE)
check("gate now short-circuits from cache", gated.get("cached") is True, str(gated)[:70])
check("gate persisted to data/enzo-trade-gate.json", os.path.exists(C.TRADE_GATE_PATH))
os.environ["MOCK_STATE"] = "{}"

print("\n=== 9. min/max trade guards ===")
reset()
r = X.buy_token(MINT, 0.04, cfg=LIVE)
check("$0.04 rejected as BELOW_MINIMUM_TRADE", r.get("ok") is False and r.get("reason") == X.E_BELOW_MIN, str(r.get("detail",""))[:90])
r = X.buy_token(MINT, 9000.0, cfg=LIVE)
check("$9000 rejected as ABOVE_MAXIMUM_TRADE", r.get("ok") is False and r.get("reason") == X.E_ABOVE_MAX, str(r.get("reason")))

print("\n=== 10. fee reserve guard ===")
reset()
os.environ["MOCK_STATE"] = json.dumps({"sol": 0.0001, "usdc": 500.0})
X._BIN_CACHE.update({"path": None, "checked_at": 0.0})
r = X.buy_token(MINT, 50.0, cfg=LIVE)
check("insufficient SOL for fees blocked before swap", r.get("ok") is False and r.get("reason") == X.E_INSUFFICIENT_FEE, str(r.get("reason")))
os.environ["MOCK_STATE"] = "{}"

print("\n=== 11. live capital sync (replaces the static $2.06 initial_capital) ===")
reset()
os.environ["MOCK_STATE"] = json.dumps({"sol": 0.35, "usdc": 500.0})
X._BIN_CACHE.update({"path": None, "checked_at": 0.0})
cap = X.sync_wallet_capital(force=True, cfg=LIVE)
check("capital sync ok", cap.get("ok") is True, str(cap.get("detail")))
check("usdc read = 500.0", cap.get("usdc") == 500.0, str(cap.get("usdc")))
check("fee reserve 0.02 SOL excluded", abs(cap.get("deployable_sol",0) - 0.33) < 1e-9, str(cap.get("deployable_sol")))
check("total_usd > 500 (includes SOL)", cap.get("total_usd",0) > 500.0, f"${cap.get('total_usd')} @ SOL ${cap.get('sol_price')}")
os.environ["MOCK_STATE"] = "{}"

print("\n=== 12. is_ready() ===")
reset()
os.environ["MOCK_STATE"] = json.dumps({"sol": 0.35, "usdc": 500.0})
X._BIN_CACHE.update({"path": None, "checked_at": 0.0})
ok, why = X.is_ready(LIVE)
check("is_ready True in LIVE with funded wallet", ok is True, why[:140])
reset()
PAPER = copy.deepcopy(LIVE); PAPER["paper_mode"] = True
ok, why = X.is_ready(PAPER)
check("is_ready False in PAPER mode", ok is False and X.E_PAPER_BLOCKED in why, why[:110])
reset()
BROKE = copy.deepcopy(LIVE); BROKE["execution"]["wallet_name"] = "no-such-wallet"
ok, why = X.is_ready(BROKE)
check("is_ready False for a missing wallet", ok is False and X.E_WALLET_MISSING in why, why[:130])
os.environ["MOCK_STATE"] = "{}"

print("\n=== 13. error classification ===")
cases = [
    (1, "", "error: unknown option '--yes'", X.E_UNKNOWN_OPTION),
    (1, "", "Error: not authenticated - run `mp login`", X.E_NOT_AUTHED),
    (1, "", "Error: consent required, run mp consent accept", X.E_CONSENT),
    (1, "", "Error: no route found for this token pair", X.E_NO_ROUTE),
    (1, "", "HTTP 429 Too Many Requests", X.E_RATE_LIMITED),
    (1, "", "insufficient balance for trade", X.E_INSUFFICIENT),
    (-1, "", "timed out after 180s", X.E_TIMEOUT),
    (-2, "", "MoonPay CLI not found", X.E_CLI_NOT_FOUND),
]
for code, out, err, want in cases:
    got = X.classify_error(code, out, err)
    check(f"classify {want}", got == want, f"got {got} for {err[:44]!r}")

print("\n=== 14. preflight_report structure ===")
reset()
rep = X.preflight_report(LIVE)
check("report has checks list", isinstance(rep.get("checks"), list) and len(rep["checks"]) >= 3, str([c["name"] for c in rep.get("checks",[])]))
check("report ready flag present", isinstance(rep.get("ready"), bool))

print("\n=== 15. REGRESSION: the OLD executor against the same mock ===")
sys.path.insert(0, "/tmp")
import importlib.util
spec = importlib.util.spec_from_file_location("old_executor", "/tmp/old_executor_probe.py")
old = importlib.util.module_from_spec(spec)
try:
    spec.loader.exec_module(old)
    old.MOONPAY_BIN = "/tmp/mockbin/mp"
    oq = old.get_quote(old.USDC_MAINNET, MINT, 50.0, "solana")
    check("OLD get_quote returns None (proves the --chain bug)", oq is None, f"got {oq}")
    orr = old.execute_swap(old.USDC_MAINNET, MINT, 50.0, wallet="enzo-trading")
    check("OLD execute_swap fails", orr.get("ok") is False, str(orr.get("reason"))[:90])
    check("OLD _to_smallest_unit inflates $50 -> 50000000", old._to_smallest_unit(old.USDC_MAINNET, 50.0) == 50000000, str(old._to_smallest_unit(old.USDC_MAINNET, 50.0)))
    check("OLD _parse_tx_hash can never match", old._parse_tx_hash("signature: " + "5"*88) is None)
except Exception as e:
    print(f"  (old-executor probe unavailable: {e})")

reset()
print(f"\n{'='*66}\n  RESULT: {PASS} passed, {FAIL} failed\n{'='*66}")
sys.exit(1 if FAIL else 0)
