#!/usr/bin/env python3
"""Executor test harness — runs the REAL executor against a mock MoonPay CLI
that reproduces commander's exact option semantics (verified against
@moonpay/cli@1.96.0's own command builder)."""
import json, os, sys, time, copy

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(_HERE))
sys.path.insert(0, _HERE)

from conftest_paths import install_mock_on_path, isolate_home, mock_bin_dir

MOCK_DIR = install_mock_on_path()
if not MOCK_DIR:
    print("\n  \033[31mABORT\033[0m  no mock MoonPay CLI found. Expected tests/mockbin/mp, "
          "or set ENZO_MOCK_BIN_DIR.")
    sys.exit(2)

# Isolate BEFORE importing: this suite drives the real executor against the mock
# CLI, which writes the trade gate and log into whatever DATA_DIR resolved to.
isolate_home(prefix="enzo-exec-")

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
# The floor rule is the owner's: min_trade_usd clamps UP when the wallet can
# fund it, and only refuses when it cannot. Both halves are asserted, with the
# capital reading pinned so the test cannot depend on a stale snapshot file.
from enzo.execution import portfolio as _PF
_real_dep = _PF.deployable_capital
_PF.deployable_capital = lambda cfg=None, state=None, force=False: 0.0
r = X.buy_token(MINT, 0.04, cfg=LIVE)
check("$0.04 with an EMPTY wallet rejected as BELOW_MINIMUM_TRADE",
      r.get("ok") is False and r.get("reason") == X.E_BELOW_MIN,
      str(r.get("detail", ""))[:90])
_PF.deployable_capital = lambda cfg=None, state=None, force=False: 9.0
r = X.buy_token(MINT, 0.04, cfg=LIVE)
check("$0.04 with a FUNDED wallet clamped up to the $1 floor (owner rule)",
      r.get("ok") is True and float(r.get("amount_usd", 0)) == 1.0,
      str(r.get("amount_usd")))
_PF.deployable_capital = _real_dep
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
# The pre-fix executor is recovered from git history at run time, so this
# regression proof travels with the repo instead of depending on a scratch file
# in /tmp that only existed on the machine where the bug was found.
import importlib.util
import subprocess
import tempfile

ROOT = os.path.dirname(_HERE)
REL = "enzo/execution/executor.py"


def _oldest_commit_for(path):
    try:
        out = subprocess.run(["git", "-C", ROOT, "log", "--format=%H", "--", path],
                             capture_output=True, text=True, timeout=20)
        commits = [c for c in out.stdout.split() if c]
        return commits[-1] if commits else None
    except Exception:
        return None


_old_src = None
_commit = _oldest_commit_for(REL)
if _commit:
    try:
        r = subprocess.run(["git", "-C", ROOT, "show", f"{_commit}:{REL}"],
                           capture_output=True, text=True, timeout=20)
        if r.returncode == 0 and r.stdout.strip():
            _old_src = r.stdout
    except Exception:
        _old_src = None

old = None
if _old_src:
    _tmp = tempfile.NamedTemporaryFile("w", suffix="_old_executor.py", delete=False,
                                       encoding="utf-8")
    _tmp.write(_old_src)
    _tmp.close()
    spec = importlib.util.spec_from_file_location("old_executor", _tmp.name)
    old = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(old)
    except Exception as _e:
        print(f"  \033[33mSKIP\033[0m  could not import the historical executor: {_e}")
        old = None
    else:
        print(f"  \033[2mrecovered {REL} from commit {_commit[:9]}\033[0m")
        # point the historical module at the same bundled mock
        old.MOONPAY_BIN = os.path.join(mock_bin_dir() or "", "mp")
        for _attr in ("MOONPAY_BIN_PATH", "MOONPAY_CLI"):
            if hasattr(old, _attr):
                setattr(old, _attr, old.MOONPAY_BIN)
else:
    print("  \033[33mSKIP\033[0m  git history unavailable — cannot recover the pre-fix "
          "executor for comparison")

if old is not None:
    try:
        oq = old.get_quote(old.USDC_MAINNET, MINT, 50.0, "solana")
        check("OLD get_quote returns None (proves the --chain bug)", oq is None, f"got {oq}")
        orr = old.execute_swap(old.USDC_MAINNET, MINT, 50.0, wallet="enzo-trading")
        check("OLD execute_swap fails", orr.get("ok") is False, str(orr.get("reason"))[:90])
        check("OLD _to_smallest_unit inflates $50 -> 50000000",
              old._to_smallest_unit(old.USDC_MAINNET, 50.0) == 50000000,
              str(old._to_smallest_unit(old.USDC_MAINNET, 50.0)))
        check("OLD _parse_tx_hash can never match",
              old._parse_tx_hash("signature: " + "5" * 88) is None)
    except Exception as e:
        print(f"  \033[33mSKIP\033[0m  old-executor probe raised {type(e).__name__}: {e}")
    finally:
        try:
            os.remove(_tmp.name)
        except Exception:
            pass

reset()
print(f"\n{'='*66}\n  RESULT: {PASS} passed, {FAIL} failed\n{'='*66}")
sys.exit(1 if FAIL else 0)
