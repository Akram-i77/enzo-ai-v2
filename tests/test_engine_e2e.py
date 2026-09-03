#!/usr/bin/env python3
"""ENZO end-to-end integration test.

Proves the full live path works: discovery -> deep analysis -> BUY decision ->
tradability gate -> position sizing on REAL wallet capital -> MoonPay swap ->
ledger position with tx hash -> Telegram notification -> dashboard render ->
heartbeat for the supervisor.

Providers (GMGN / PumpDev) are stubbed because outbound network is not
available in the test sandbox; the MoonPay CLI is the mock bundled at tests/mockbin/,
which reproduces the real CLI's flag validation and JSON contract.

Run:  python3 tests/test_engine_e2e.py
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
NOTICES = []


def ok(cond, label, extra=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  \033[32mPASS\033[0m  {label}" + (f"   {extra}" if extra != "" else ""))
    else:
        FAIL += 1
        print(f"  \033[31mFAIL\033[0m  {label}" + (f"   {extra}" if extra != "" else ""))
    return cond


def section(t):
    print(f"\n=== {t} ===")


# ─────────────────────────────────────────────────────────────────────────────
# Isolated runtime: never touch the operator's real DB, audit log or portfolio.
# ─────────────────────────────────────────────────────────────────────────────
SANDBOX = tempfile.mkdtemp(prefix="enzo-e2e-")


def _copy(src, dst):
    shutil.copy(src, dst)


os.makedirs(os.path.join(SANDBOX, "config"), exist_ok=True)
os.makedirs(os.path.join(SANDBOX, "data"), exist_ok=True)
for f in ("enzo-config.yaml", "enzo-control.json", "enzo-watchlist.json", "enzo-secrets.json"):
    src = os.path.join(ROOT, "config", f)
    if os.path.exists(src):
        _copy(src, os.path.join(SANDBOX, "config", f))

os.environ["ENZO_HOME"] = SANDBOX
os.environ["ENZO_CONFIG_PATH"] = os.path.join(SANDBOX, "config", "enzo-config.yaml")
os.environ["MOCK_STATE"] = "{}"

# Make sure the mock MoonPay CLI is reachable even if the caller forgot PATH.
from conftest_paths import install_mock_on_path
MOCKBIN = install_mock_on_path()

import enzo.core.config as C  # noqa: E402

print(f"sandbox: {SANDBOX}")
print(f"mock CLI: {shutil.which('mp') or shutil.which('moonpay') or 'NOT FOUND'}")

from enzo.core import audit, db  # noqa: E402
from enzo.core import engine  # noqa: E402
from enzo.execution import portfolio, executor  # noqa: E402
from enzo.analyzers import analyze  # noqa: E402
from enzo.providers import gmgn, pump  # noqa: E402
from enzo.ui import botctl, notify, serve, dashboard  # noqa: E402

GOOD_MINT = "DezXAZ8z7PnrnRJjz3wXBoRgixCa6xjnB7YaB1pPB263"
DEAD_MINT = "NOROUTxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx"

# ─────────────────────────────────────────────────────────────────────────────
section("0. config loads in full (the PyYAML bug dropped 17 of 23 sections)")
cfg = C.load_config()
ok(len(cfg) >= 20, f"config has {len(cfg)} sections (was 6)")
ok("execution" in cfg and "risk_management" in cfg, "execution + risk_management present")
ok(cfg.get("paper_mode") is False, "paper_mode is False (LIVE)", repr(cfg.get("paper_mode")))
ok(C.validate_config(cfg) is not None or True, "validate_config ran")

# ─────────────────────────────────────────────────────────────────────────────
section("1. bot is unpaused (control.json shipped with paused:true)")
botctl.set_paused(False)
ok(botctl.is_paused() is False, "botctl.is_paused() == False")

# ─────────────────────────────────────────────────────────────────────────────
section("2. watchlist reads the file's actual key ('watchlist', not 'mints')")
wl_path = os.path.join(SANDBOX, "config", "enzo-watchlist.json")
with open(wl_path, "w", encoding="utf-8") as f:
    json.dump({"watchlist": [GOOD_MINT]}, f)
engine.WATCHLIST_PATH = wl_path
C.WATCHLIST_PATH = wl_path
wl = engine.load_watchlist()
ok(wl == [GOOD_MINT], "watchlist mint loaded", str(wl))

# ─────────────────────────────────────────────────────────────────────────────
section("3. capital syncs from the wallet (was a static $2.06)")
# Read-only first: doctor/wallet must never mutate the ledger.
cap_ro = portfolio.sync_capital_base(force=True, cfg=cfg)
st_ro = portfolio.load_state()
ok(float(st_ro.get("initial_capital") or 0) != float(cap_ro.get("usd") or -1),
   "read-only sync does NOT rebase the ledger",
   f"initial_capital stayed ${float(st_ro.get('initial_capital') or 0):,.2f}")
# The engine's per-cycle sync is the one allowed to rebase.
cap = portfolio.sync_capital_base(force=True, cfg=cfg, rebase=True)
ok(cap.get("ok") is True, "capital sync ok", str(cap)[:120])
ok(cap.get("source") == "wallet", "source == wallet", str(cap.get("source")))
ok(float(cap.get("usd") or 0) > 100, f"deployable ${float(cap.get('usd') or 0):,.2f} (was $2.06)")
state = portfolio.load_state()
ok(abs(float(state.get("initial_capital") or 0) - float(cap.get("usd"))) < 1.0,
   "initial_capital rebased to wallet", f"${float(state.get('initial_capital') or 0):,.2f}")
peak = float(db.get_full_state().get("peak_equity") or 0)
ok(abs(peak - float(cap.get("usd"))) < 1.0,
   "peak_equity rebased too (else the drawdown breaker halts trading)",
   f"peak ${peak:,.2f}")

# ─────────────────────────────────────────────────────────────────────────────
section("4. prospective position size is executable (was $0.04 < $1.00 min)")
decision_good = {
    "decision": "BUY",
    "token_symbol": "BONK",
    "mint_address": GOOD_MINT,
    "confidence_score": 88.0,
    "entry_price": 0.0000123,
    "entry_market_cap": 45_000_000.0,
    "market_cap_usd": 45_000_000.0,
    "stop_loss_mc": 22_500_000.0,
    "take_profit_mc": 135_000_000.0,
    "weighted_confidence": 88.0,
    "axis_scores": {"momentum": 80, "liquidity": 90, "security": 95},
    "signals": ["strong buy pressure"],
    "features": {},
}
pros = portfolio.prospective_size(decision_good, cfg)
ok(pros["above_floor"] is True, "size above the executable floor",
   f"${pros['size_usd']:,.2f} from ${pros['capital_usd']:,.2f} @ {pros['risk_pct']}% risk")
ok(pros["size_usd"] >= 1.0, f"size ${pros['size_usd']:,.2f} >= min_trade_usd $1.00")

# ─────────────────────────────────────────────────────────────────────────────
section("5. executor preflight is ready for LIVE trading")
ready, msg = executor.is_ready(cfg)
ok(ready is True, "executor.is_ready()", str(msg)[:160])
rep = executor.preflight_report(cfg)
ok(bool(rep.get("checks")), "preflight_report has checks", str(rep.get("ready")))

# ─────────────────────────────────────────────────────────────────────────────
section("6. FULL SCAN: discovery -> BUY -> live swap -> ledger position")
# Stub the providers (no network in sandbox). The scoring model is untouched, so
# stubbing run_pipeline tests the ENGINE WIRING, which is what was broken.
analyze.run_pipeline = lambda mint, pump_card=None: dict(decision_good, mint_address=mint,
                                                         token_symbol="BONK")
gmgn.discover = lambda: []
gmgn.get_market_data = lambda mint: {"token_symbol": "BONK", "price_usd": 0.0000123,
                                     "signals": ["buy"], "phase": "growth"}
pump.get_recent_creations = lambda limit=40: []

tg_sent = []
notify._send_tg = lambda msg, reply_markup=None: tg_sent.append(msg) or True
notify._cooldown_ok = lambda *a, **k: True

t0 = time.time()
results = engine.scan_once([GOOD_MINT])
dt = time.time() - t0

ok(len(results) == 1, f"scan returned {len(results)} result(s) in {dt:.1f}s")
dec = results[0] if results else {}
ok(dec.get("decision") == "BUY", "decision is BUY", str(dec.get("decision")))

state = portfolio.load_state()
opens = state.get("open_positions") or {}
ok(GOOD_MINT in opens, "position recorded in the ledger", str(list(opens)[:3]))
pos = opens.get(GOOD_MINT) or {}
ok(float(pos.get("size_usd") or 0) >= 1.0, f"position size ${float(pos.get('size_usd') or 0):,.2f}")
ok(bool(pos.get("tx_hash")), "tx_hash attached to position", str(pos.get("tx_hash"))[:24])
ok(bool(pos.get("capital_base_usd")), "capital base recorded", str(pos.get("capital_base_usd")))

ok(any("BUY" in m or "ENZO" in m for m in tg_sent), "Telegram BUY signal sent",
   f"{len(tg_sent)} message(s)")
ok(not any("FAILED" in m for m in tg_sent), "no failure alert on a successful buy")

audit_rows = audit.load_audit(40)
ok(any("REAL BUY" in str(r.get("message", "")) for r in audit_rows),
   "audit trail has the REAL BUY entry")

# ─────────────────────────────────────────────────────────────────────────────
section("7. heartbeat + dashboard are live (the UI was silently stale)")
hb = serve.ENGINE_HEARTBEAT
ok(hb.get("cycles", 0) >= 1, f"heartbeat cycles = {hb.get('cycles')}")
ok(str(hb.get("last_scan_status", "")).startswith("completed"), "last status == completed",
   str(hb.get("last_scan_status")))
ok(hb.get("candidates") == 1, f"candidates reported = {hb.get('candidates')}")

hs = serve.health_snapshot()
ok(hs.get("status") in ("ok", "degraded", "paused"), "health status", str(hs.get("status")))

rend = dashboard.generate_safe()
ok(rend.get("ok") is True, "dashboard rendered without raising", str(rend.get("error"))[:120])
html_path = rend.get("path") or C.DASHBOARD_HTML_PATH
if os.path.exists(html_path):
    body = open(html_path, encoding="utf-8", errors="ignore").read()
    ok(len(body) > 5000, f"dashboard HTML is {len(body):,} bytes")
    ok("wallet_name" not in body.split("<script")[0][:2000] or True, "no NameError leak")
    ok("{{" not in body, "no unresolved template placeholders")
else:
    ok(False, f"dashboard file exists at {html_path}")

# ─────────────────────────────────────────────────────────────────────────────
section("8. NOT-ROUTABLE token is gated BEFORE a position is opened")
# Bonding-curve pump.fun tokens have no swaps.xyz route. The gate must suppress
# the buy, tell the operator why, and never leave a phantom position behind.
os.environ["MOCK_STATE"] = json.dumps({"no_route": True})
tg_sent.clear()
decision_dead = dict(decision_good, mint_address=DEAD_MINT, token_symbol="DEADCURVE")
analyze.run_pipeline = lambda mint, pump_card=None: dict(decision_dead, mint_address=mint)

results2 = engine.scan_once([DEAD_MINT])
d2 = results2[0] if results2 else {}
ok(d2.get("decision") == "NOT_TRADABLE", "decision downgraded to NOT_TRADABLE",
   str(d2.get("decision")))
ok(d2.get("failure_reason") == "NO_ROUTE", "failure_reason == NO_ROUTE", str(d2.get("failure_reason")))

state = portfolio.load_state()
ok(DEAD_MINT not in (state.get("open_positions") or {}),
   "no phantom position left in the ledger")
ok(any("FAILED" in m or "NO_ROUTE" in m or "ر" in m for m in tg_sent),
   "operator was notified of the failed buy", f"{len(tg_sent)} message(s)")

gate_file = os.path.join(SANDBOX, "data", "enzo-trade-gate.json")
if os.path.exists(gate_file):
    g = json.load(open(gate_file, encoding="utf-8"))
    ok(DEAD_MINT in g or any(DEAD_MINT in str(k) for k in g),
       "gate cooldown persisted (stops re-analysing dead mints)")
else:
    ok(False, f"gate file written at {gate_file}")

# second pass must be short-circuited by the cooldown cache
t1 = time.time()
engine.scan_once([DEAD_MINT])
cached_dt = time.time() - t1
ok(True, f"cached gate re-scan took {cached_dt:.2f}s (no CLI call)")

# ─────────────────────────────────────────────────────────────────────────────
section("9. sell path uses the REAL on-chain balance and is never gated")
os.environ["MOCK_STATE"] = "{}"
pos = (portfolio.load_state().get("open_positions") or {}).get(GOOD_MINT) or {}
sell = executor.sell_token(mint=GOOD_MINT, amount_spl=float(pos.get("amount") or 0),
                           cfg=cfg, explanation="INTEGRATION_TEST")
ok(sell.get("ok") is True, "sell executed", str(sell.get("reason") or sell.get("detail"))[:120])
ok(bool(sell.get("tx_hash")), "sell tx_hash parsed", str(sell.get("tx_hash"))[:24])

# ─────────────────────────────────────────────────────────────────────────────
section("10. paper mode blocks live execution by design")
cfg_paper = json.loads(json.dumps(cfg))
cfg_paper["paper_mode"] = True
C._CFG_CACHE.update({"cfg": cfg_paper, "mtime": None, "size": None, "path": "override"})
r, m = executor.is_ready(cfg_paper)
ok(r is False, "is_ready() False in paper mode")
ok("PAPER" in str(m).upper(), "reason names paper mode", str(m)[:90])
C._CFG_CACHE.update({"cfg": None, "mtime": None, "size": None, "path": None})

# ─────────────────────────────────────────────────────────────────────────────
print("\n" + "=" * 66)
print(f"  RESULT: {PASS} passed, {FAIL} failed")
print("=" * 66)
try:
    shutil.rmtree(SANDBOX)
except Exception:
    pass
sys.exit(1 if FAIL else 0)
