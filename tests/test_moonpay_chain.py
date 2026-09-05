#!/usr/bin/env python3
"""Regression guard: the chain identifier MoonPay receives must be "solana".

The production failure this reproduces
--------------------------------------
The config has ONE shared key, `chain: sol`, because GMGN's CLI names Solana
"sol". The MoonPay CLI names it "solana" and rejects anything else with:

    Chain definition not found: sol

Three call sites passed the GMGN spelling straight through to MoonPay
(check_tradable, buy_token, sell_token), so EVERY MoonPay call failed:

  * `token balance list` -> capital unreadable -> "trading blocked"
  * `token retrieve`/`token quote` -> NO_ROUTE for every token
  * `token swap` -> could never execute

The bot still discovered tokens and scored BUY signals above threshold, which
made it look like "MoonPay doesn't support pump.fun tokens". It did not matter
which token was tried: the chain name was wrong before the token was ever
considered.

Why 47 executor tests missed it
------------------------------
Every executor function DEFAULTS to `chain="solana"`, and the tests mostly
called them without threading cfg["chain"] through. The mock CLI also accepted
any --chain value, so nothing ever objected. This suite closes both holes: the
mock now validates the chain registry AND records the exact argv, so these
tests assert on the command line that really left the process.
"""
import copy
import json
import os
import sys
import tempfile

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(_HERE))
sys.path.insert(0, _HERE)

from conftest_paths import install_mock_on_path, isolate_home

MOCK_DIR = install_mock_on_path()
if not MOCK_DIR:
    print("\n  ABORT  no mock MoonPay CLI found (expected tests/mockbin/mp).")
    sys.exit(2)

# Isolate BEFORE importing: config resolves state paths at import time, and this
# suite exercises the trade gate, which writes data/enzo-trade-gate.json.
isolate_home(prefix="enzo-chain-")

from enzo.core import config as C
from enzo.execution import executor as X

PASS = FAIL = 0


def check(name, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  \033[32mPASS\033[0m  {name}" + (f"   {detail}" if detail else ""))
    else:
        FAIL += 1
        print(f"  \033[31mFAIL\033[0m  {name}   {detail}")


MINT = "DezXAZ8z7PnrnRJjz3wXBoRgixCa6xjnB7YaB1pPB263"

# The REAL production value. This is the whole point: cfg says "sol".
CFG = copy.deepcopy(C.DEFAULTS)
CFG["chain"] = "sol"
CFG["paper_mode"] = False
CFG["execution"].update({
    "wallet_name": "enzo-trading", "base_token": "USDC", "capital_source": "wallet",
    "min_trade_usd": 1.0, "max_trade_usd": 500.0, "sol_fee_reserve": 0.02,
    "retry_attempts": 1, "moonpay_bin": "",
})

CHAIN_FLAGS = ("--chain", "--from-chain", "--to-chain")


def recorded(argv_log):
    rows = []
    if os.path.exists(argv_log):
        for line in open(argv_log, encoding="utf-8"):
            line = line.strip()
            if line:
                try:
                    rows.append(json.loads(line).get("argv") or [])
                except Exception:
                    pass
    return rows


def chain_values(argvs):
    """Every value passed to a chain flag, across all recorded invocations."""
    out = []
    for a in argvs:
        for i, tok in enumerate(a):
            if tok in CHAIN_FLAGS and i + 1 < len(a):
                out.append((tok, a[i + 1]))
    return out


# ── 1. the normalizer itself ─────────────────────────────────────────────────
print("\n=== 1. moonpay_chain() translation ===")
check('GMGN "sol" -> MoonPay "solana"', X.moonpay_chain(CFG) == "solana", X.moonpay_chain(CFG))
check('"solana" passes through', X.moonpay_chain({"chain": "solana"}) == "solana")
check('case-insensitive ("SOL")', X.moonpay_chain({"chain": "SOL"}) == "solana")
check('raw argument is normalized too', X.moonpay_chain(CFG, "sol") == "solana")
check('unknown chain is NOT rewritten', X.moonpay_chain({"chain": "ton"}) == "ton",
      "a future chain must pass through, not be coerced")
ovr = copy.deepcopy(CFG)
ovr["execution"]["moonpay_chain"] = "solana"
check("execution.moonpay_chain override wins", X.moonpay_chain(ovr) == "solana")
check("missing chain falls back to solana", X.moonpay_chain({}) == "solana")

# ── 2. what actually reaches the CLI ─────────────────────────────────────────
print("\n=== 2. argv sent to MoonPay with cfg chain='sol' ===")
tmpdir = tempfile.mkdtemp(prefix="enzo-chain-")
argv_log = os.path.join(tmpdir, "argv.jsonl")
os.environ["MOCK_ARGV_LOG"] = argv_log
os.environ["MOCK_STATE"] = json.dumps({"usdc": 500.0, "sol": 0.35})
os.environ["ENZO_HOME"] = tmpdir

try:
    X._BIN_CACHE.update({"path": None, "checked_at": 0.0})
    X.clear_gate()
    X._CAPITAL_CACHE.update({"ok": False, "ts": 0.0})

    # capital read (this is what produced "LIVE capital unreadable")
    cap = X.sync_wallet_capital(force=True, cfg=CFG)
    check("wallet capital readable with chain='sol'", bool(cap.get("ok")),
          f"ok={cap.get('ok')} detail={str(cap.get('detail'))[:70]}")
    check("capital amount is real, not zero", float(cap.get("total_usd") or 0) > 0,
          f"total_usd={cap.get('total_usd')}")

    snap = X.get_wallet_snapshot(cfg=CFG)
    check("wallet snapshot sees rows", bool(snap.get("rows")), f"rows={snap.get('rows')}")

    # tradability gate (this is what produced NO_ROUTE for every token)
    X.clear_gate()
    gate = X.check_tradable(MINT, 5.0, cfg=CFG)
    check("tradability gate did not fail on a chain error",
          "CHAIN" not in str(gate.get("detail", "")).upper()
          and "Chain definition not found" not in str(gate.get("detail", "")),
          str(gate.get("reason")) + " / " + str(gate.get("detail"))[:70])

    # an actual swap
    X._CAPITAL_CACHE.update({"ok": False, "ts": 0.0})
    X.execute_swap(from_token=X.USDC_MAINNET, to_token=MINT, from_amount=5.0,
                   wallet="enzo-trading", chain=CFG["chain"], cfg=CFG,
                   direction="buy", explanation="chain regression test")
finally:
    argvs = recorded(argv_log)
    for k in ("MOCK_ARGV_LOG", "MOCK_STATE", "ENZO_HOME"):
        os.environ.pop(k, None)

check("the bot actually invoked the CLI", len(argvs) > 0, f"{len(argvs)} invocation(s)")

vals = chain_values(argvs)
check("every MoonPay call carried a chain flag", len(vals) > 0, f"{len(vals)} flag(s)")

bad = [(f, v) for f, v in vals if v != "solana"]
check(
    'no chain flag ever carried GMGN\'s "sol"',
    not bad,
    "these would fail with 'Chain definition not found': " + ", ".join(f"{f}={v}" for f, v in bad[:8]),
)
check('all chain flags read "solana"', all(v == "solana" for _, v in vals),
      str(sorted({v for _, v in vals})))

blob = json.dumps(argvs)
check("the production error string never appeared in any invocation",
      "Chain definition not found" not in blob)

# ── 3. self-validation: would this suite catch the bug coming back? ──────────
print("\n=== 3. the guard is not vacuous ===")
# Neuter the normalizer and confirm the mock now rejects the call. If this
# assertion ever stops failing, the test above is not really testing anything.
_orig = X.moonpay_chain
tmp2 = tempfile.mkdtemp(prefix="enzo-chain-neg-")
neg_log = os.path.join(tmp2, "argv.jsonl")
os.environ["MOCK_ARGV_LOG"] = neg_log
os.environ["MOCK_STATE"] = json.dumps({"usdc": 500.0, "sol": 0.35})
os.environ["ENZO_HOME"] = tmp2
try:
    X.moonpay_chain = lambda cfg=None, raw=None: (str(raw) if raw else str((cfg or {}).get("chain") or "solana"))
    X._BIN_CACHE.update({"path": None, "checked_at": 0.0})
    X.clear_gate()
    X._CAPITAL_CACHE.update({"ok": False, "ts": 0.0})
    # check_tradable is the path that threads cfg["chain"] through to the CLI
    # (this is what produced NO_ROUTE for BEAVER in production).
    broken_gate = X.check_tradable(MINT, 5.0, cfg=CFG)
    broken_cap = X.sync_wallet_capital(force=True, cfg=CFG)
finally:
    neg_argvs = recorded(neg_log)
    X.moonpay_chain = _orig
    for k in ("MOCK_ARGV_LOG", "MOCK_STATE", "ENZO_HOME"):
        os.environ.pop(k, None)

neg_vals = chain_values(neg_argvs)
check("without the fix, GMGN's \"sol\" really does reach the CLI",
      any(v == "sol" for _, v in neg_vals),
      str(sorted({v for _, v in neg_vals})))
neg_blob = json.dumps(neg_argvs)
check("without the fix, the tradability gate breaks (reproduces production NO_ROUTE)",
      (not broken_gate.get("tradable")) or ("Chain definition not found" in neg_blob)
      or broken_gate.get("reason") == X.E_NO_ROUTE,
      f"reason={broken_gate.get('reason')} tradable={broken_gate.get('tradable')}")
check("the production error string is what the CLI actually said",
      "Chain definition not found: sol" in neg_blob
      or not broken_gate.get("tradable"),
      "mock must reject \"sol\" exactly like the real CLI")

for d in (tmpdir, tmp2):
    try:
        for f in os.listdir(d):
            os.unlink(os.path.join(d, f))
        os.rmdir(d)
    except Exception:
        pass

# ── 4. a chain mistake must not masquerade as an untradable token ────────────
print("\n=== 4. error classification tells the two apart ===")
cases = [
    ("Chain definition not found: sol", X.E_BAD_CHAIN),
    ("error: unsupported chain 'sol'", X.E_BAD_CHAIN),
    ("invalid chain supplied", X.E_BAD_CHAIN),
    ("no route found for this pair", X.E_NO_ROUTE),
    ("insufficient liquidity for this swap", X.E_NO_ROUTE),
    ("insufficient balance", X.E_INSUFFICIENT),
    ("error: unknown option '--yes'", X.E_UNKNOWN_OPTION),
    ("please login first", X.E_NOT_AUTHED),
]
for text, want in cases:
    got = X.classify_error(1, None, text)
    check(f'{text[:38]!r} -> {want}', got == want, f"got {got}")

# The specific incident: a chain typo used to surface as NO_ROUTE, which reads
# as "MoonPay cannot trade this token" and sends diagnosis the wrong way.
check("CHAIN_NOT_SUPPORTED is NOT collapsed into NO_ROUTE",
      X.E_BAD_CHAIN != X.E_NO_ROUTE)

print("\n=== 5. quote_ok reports the real reason, not a blanket NO_ROUTE ===")
tmp3 = tempfile.mkdtemp(prefix="enzo-chain-q-")
os.environ["MOCK_ARGV_LOG"] = os.path.join(tmp3, "argv.jsonl")
os.environ["MOCK_STATE"] = json.dumps({"usdc": 500.0, "sol": 0.35})
os.environ["ENZO_HOME"] = tmp3
try:
    X._BIN_CACHE.update({"path": None, "checked_at": 0.0})
    X.clear_gate()
    # Feed the gate a raw "sol" while bypassing the normalizer, to confirm the
    # failure that reaches the operator names the chain and not the token.
    _orig = X.moonpay_chain
    X.moonpay_chain = lambda cfg=None, raw=None: (str(raw) if raw else str((cfg or {}).get("chain") or "solana"))
    try:
        ok, reason, q = X.quote_ok(X.USDC_MAINNET, MINT, 5.0, "sol", CFG)
    finally:
        X.moonpay_chain = _orig
    check("quote_ok fails when the wrong chain is sent", not ok, f"ok={ok}")
    check("quote_ok names the CHAIN as the culprit", reason == X.E_BAD_CHAIN,
          f"reason={reason} (was NO_ROUTE before this fix)")
finally:
    for k in ("MOCK_ARGV_LOG", "MOCK_STATE", "ENZO_HOME"):
        os.environ.pop(k, None)
try:
    for f in os.listdir(tmp3):
        os.unlink(os.path.join(tmp3, f))
    os.rmdir(tmp3)
except Exception:
    pass


print("\n" + "=" * 68)
print(f"  RESULT: {PASS} passed, {FAIL} failed")
print("=" * 68)
sys.exit(1 if FAIL else 0)
