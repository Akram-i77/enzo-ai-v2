#!/usr/bin/env python3
"""Exit rules: stop loss, trailing stop and the stall exit.

Owner-tuned 2026-09-03:
    stop_loss_percentage      50 -> 38
    trailing_stop_percentage  30 -> 40
    stall_exit_enabled        (new) true, +15% gain, 30s without a new high

Why each assertion is here
--------------------------
`trailing_stop_percentage` does DOUBLE duty in check_exits, which is easy to
misread: it is both the ARMING threshold (the trailing stop only activates once
the market cap is that far ABOVE entry) and the drawdown-from-peak trigger. At
40% the trailing stop arms at +40% and then fires 40% below the peak. These
tests pin both halves, plus the arithmetic that matters to the operator: once
armed, the trailing trigger always sits well above the hard stop, so the
trailing stop binds first and locks in a gain rather than a loss.

The stall exit did not exist before. MoonPay's pump.fun guide calls it the most
important exit rule ("tokens that pump and flatline rarely re-pump"), so it is
tested in all four directions: fires when up and flat, and does NOT fire when
the gain is too small, when the flat window is too short, when a new high resets
the clock, or when the feature is switched off.

Run:  python3 tests/test_exit_rules.py
"""
import json
import os
import shutil
import sys
import tempfile
import time as _real_time
from datetime import datetime, timezone

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

PASS = FAIL = 0


def ok(cond, label, extra=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  \033[32mPASS\033[0m  {label}" + (f"   {extra}" if extra else ""))
    else:
        FAIL += 1
        print(f"  \033[31mFAIL\033[0m  {label}" + (f"   {extra}" if extra else ""))
    return bool(cond)


def section(t):
    print(f"\n=== {t} ===")


SANDBOX = tempfile.mkdtemp(prefix="enzo-exit-")
os.makedirs(os.path.join(SANDBOX, "config"), exist_ok=True)
os.makedirs(os.path.join(SANDBOX, "data"), exist_ok=True)
for f in ("enzo-config.yaml", "enzo-control.json", "enzo-watchlist.json", "enzo-secrets.json"):
    src = os.path.join(ROOT, "config", f)
    if os.path.exists(src):
        shutil.copy(src, os.path.join(SANDBOX, "config", f))
os.environ["ENZO_HOME"] = SANDBOX
os.environ["MOCK_STATE"] = "{}"
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from conftest_paths import install_mock_on_path  # noqa: E402
install_mock_on_path()

from enzo.core import config as C          # noqa: E402
from enzo.execution import portfolio        # noqa: E402
from enzo.ui import notify                  # noqa: E402

# No Telegram/network calls from a unit test.
notify.notify_exit = lambda *a, **k: None
notify.notify_partial = lambda *a, **k: None


class Clock:
    """Delegating proxy so only time() is faked; everything else stays real."""
    def __init__(self, real):
        self._real = real
        self.t = None

    def time(self):
        return self.t if self.t is not None else self._real.time()

    def __getattr__(self, name):
        return getattr(self._real, name)


CLOCK = Clock(_real_time)
portfolio.time = CLOCK

CFG = C.load_config()
XS = CFG.get("exit_strategy") or {}
STOP = float(XS.get("stop_loss_percentage"))
TRAIL = float(XS.get("trailing_stop_percentage"))
STALL_ON = bool(XS.get("stall_exit_enabled"))
STALL_GAIN = float(XS.get("stall_min_gain_pct", 15.0))
STALL_SECS = float(XS.get("stall_seconds", 30.0))

MINT = "ExitRuleTestMint11111111111111111111111111111"
ENTRY = 100_000.0
T0 = 1_700_000_000.0


def make_pos(entry_mc=ENTRY, size_usd=100.0, opened_at=None):
    return {
        "mint": MINT, "symbol": "TEST", "entry_price": 0.001,
        "entry_market_cap": entry_mc, "current_market_cap": entry_mc,
        "size_usd": size_usd, "initial_size_usd": size_usd,
        "amount": 100_000.0, "initial_amount": 100_000.0,
        "peak_market_cap": entry_mc, "trailing_active": False,
        "trailing_stop_mc": None, "stages_hit": [],
        "opened_at": (opened_at or datetime.fromtimestamp(T0, timezone.utc).isoformat()),
        "max_holding_hours": 48, "unrealized_pnl": 0.0, "realized_pnl_total": 0.0,
        "stop_loss_mc": entry_mc * (1 - STOP / 100.0),
        "take_profit_mc": entry_mc * 2.5,
    }


def run(mcap, pos=None, now=None, cfg=None):
    """One check_exits cycle against a single synthetic position."""
    CLOCK.t = T0 if now is None else now
    state = {
        "initial_capital": 1000.0, "realized_pnl": 0.0, "peak_equity": 1000.0,
        "open_positions": {MINT: pos if pos is not None else make_pos()},
        "closed_positions": [],
    }
    portfolio.load_state = lambda *a, **k: state
    _orig_cfg = portfolio.load_config
    portfolio.load_config = lambda *a, **k: (cfg if cfg is not None else CFG)
    try:
        closed, partials = portfolio.check_exits({MINT: mcap})
    finally:
        portfolio.load_config = _orig_cfg
    reasons = [str(c.get("reason", "")) for c in (closed or [])]
    return reasons, state["open_positions"].get(MINT), partials


def fresh(opened_offset=0.0):
    return make_pos(opened_at=datetime.fromtimestamp(T0 + opened_offset, timezone.utc).isoformat())


print(f"sandbox: {SANDBOX}")
print(f"stop_loss={STOP}%  trailing={TRAIL}%  stall={STALL_ON} "
      f"(+{STALL_GAIN}% gain, {STALL_SECS:.0f}s flat)")

# ─────────────────────────────────────────────────────────────────────────────
section("0. the configured values are the owner's")
ok(STOP == 38.0, "stop_loss_percentage is 38", f"got {STOP}")
ok(TRAIL == 40.0, "trailing_stop_percentage is 40", f"got {TRAIL}")
ok(STALL_ON is True, "stall exit is ENABLED", f"got {STALL_ON}")
ok(STALL_GAIN == 15.0, "stall needs +15%", f"got {STALL_GAIN}")
ok(STALL_SECS == 30.0, "stall window is 30s", f"got {STALL_SECS}")

# ─────────────────────────────────────────────────────────────────────────────
section("1. STOP_LOSS fires at exactly -38%")
r, pos, _ = run(ENTRY * 0.63, fresh())                     # -37%
ok(not r, "-37% does NOT exit", str(r))
r, pos, _ = run(ENTRY * 0.62, fresh())                     # -38%
ok(any("STOP_LOSS" in x for x in r), "-38% exits with STOP_LOSS", str(r))
r, pos, _ = run(ENTRY * 0.50, fresh())                     # -50%
ok(any("STOP_LOSS" in x for x in r), "-50% still exits (below the threshold)", str(r))
# The old 50% setting would have held through -40%; prove the tightening bites.
r, pos, _ = run(ENTRY * 0.60, fresh())                     # -40%
ok(any("STOP_LOSS" in x for x in r),
   "-40% now exits (the OLD 50% stop would have held this)", str(r))

# ─────────────────────────────────────────────────────────────────────────────
section("2. TRAILING_STOP arms at +40% and fires 40% below the peak")
r, pos, _ = run(ENTRY * 1.39, fresh())
ok(not (pos or {}).get("trailing_active"), "trailing NOT armed at +39%",
   f"trailing_active={(pos or {}).get('trailing_active')}")
r, pos, _ = run(ENTRY * 1.40, fresh())
ok(bool((pos or {}).get("trailing_active")), "trailing ARMED at +40%")
ok(abs(float((pos or {}).get("trailing_stop_mc") or 0) - ENTRY * 1.40 * 0.60) < 1.0,
   "trigger = peak * (1 - 40%)", f"{(pos or {}).get('trailing_stop_mc')}")

# Armed, then a fall to just above / exactly on the trigger.
armed = fresh()
run(ENTRY * 1.40, armed)                                   # arm it
r, pos, _ = run(ENTRY * 1.40 * 0.61, armed)                # -39% from peak
ok(not r, "-39% from peak does NOT exit", str(r))
armed2 = fresh()
run(ENTRY * 1.40, armed2)
r, pos, _ = run(ENTRY * 1.40 * 0.60, armed2)               # -40% from peak
ok(any("TRAILING_STOP" in x for x in r), "-40% from peak exits with TRAILING_STOP", str(r))

# The ratchet must only move up.
armed3 = fresh()
run(ENTRY * 2.0, armed3)                                   # peak 200k -> trigger 120k
r, pos, _ = run(ENTRY * 1.5, armed3)                       # lower high, no new peak
ok(abs(float((pos or {}).get("trailing_stop_mc") or 0) - ENTRY * 2.0 * 0.60) < 1.0,
   "trigger ratchets UP only (kept 120k, not lowered to 90k)",
   f"{(pos or {}).get('trailing_stop_mc')}")

# Operator-relevant arithmetic: once armed, does the trailing stop bind before
# the hard stop? trigger = 0.6 * peak and peak >= 1.4 * entry, so
# trigger >= 0.84 * entry = -16% from entry, far above the 62% hard stop.
ok(ENTRY * 1.40 * 0.60 > ENTRY * (1 - STOP / 100.0),
   "once armed, the trailing trigger sits ABOVE the hard stop",
   f"{ENTRY*1.4*0.6:,.0f} > {ENTRY*(1-STOP/100):,.0f} -> locks in about "
   f"{(1.4*0.60-1)*100:+.0f}% instead of a {(1-STOP/100-1)*100:.0f}% loss")

# ─────────────────────────────────────────────────────────────────────────────
section("3. STALL_EXIT takes profit on a flat winner")
p = fresh()
run(ENTRY * 1.20, p, now=T0)                               # +20%, new high at T0
r, pos, _ = run(ENTRY * 1.20, p, now=T0 + 10)              # flat 10s
ok(not r, "flat only 10s -> holds", str(r))
r, pos, _ = run(ENTRY * 1.20, p, now=T0 + 29)              # flat 29s
ok(not r, "flat 29s -> still holds", str(r))
r, pos, _ = run(ENTRY * 1.20, p, now=T0 + 31)              # flat 31s
ok(any("STALL_EXIT" in x for x in r), "flat 31s at +20% -> STALL_EXIT", str(r))
ok(any("+20%" in x for x in r), "the reason states the gain", str(r))

section("3b. STALL_EXIT does not fire when it should not")
p = fresh()
run(ENTRY * 1.10, p, now=T0)                               # only +10%
r, pos, _ = run(ENTRY * 1.10, p, now=T0 + 120)
ok(not any("STALL_EXIT" in x for x in r),
   f"+10% flat 120s -> no stall exit (needs +{STALL_GAIN:.0f}%)", str(r))

p = fresh()
run(ENTRY * 1.20, p, now=T0)
run(ENTRY * 1.25, p, now=T0 + 20)                          # NEW high resets clock
r, pos, _ = run(ENTRY * 1.25, p, now=T0 + 45)              # 25s since new high
ok(not any("STALL_EXIT" in x for x in r),
   "a new high resets the stall clock (25s since, not 45s)", str(r))
r, pos, _ = run(ENTRY * 1.25, p, now=T0 + 51)              # 31s since new high
ok(any("STALL_EXIT" in x for x in r),
   "...and it fires 30s after the LAST new high", str(r))

p = fresh()
run(ENTRY * 0.95, p, now=T0)                               # DOWN 5%
r, pos, _ = run(ENTRY * 0.95, p, now=T0 + 300)
ok(not any("STALL_EXIT" in x for x in r),
   "a losing position never stall-exits (that is the stop loss's job)", str(r))

off = json.loads(json.dumps(CFG))
off["exit_strategy"]["stall_exit_enabled"] = False
p = fresh()
run(ENTRY * 1.20, p, now=T0, cfg=off)
r, pos, _ = run(ENTRY * 1.20, p, now=T0 + 300, cfg=off)
ok(not any("STALL_EXIT" in x for x in r),
   "stall_exit_enabled=false disables it completely", str(r))

section("3c. STALL_EXIT precedence")
# A stalled winner must be taken as profit, not held until it round-trips into
# the stop loss. Verify the stall fires while the position is still in profit.
p = fresh()
run(ENTRY * 1.20, p, now=T0)
r, pos, _ = run(ENTRY * 1.18, p, now=T0 + 40)
ok(any("STALL_EXIT" in x for x in r), "stall exits while still +18% (profit taken)", str(r))
ok(not any("STOP_LOSS" in x for x in r), "and it is not reported as a stop loss", str(r))

# ─────────────────────────────────────────────────────────────────────────────
section("4. the stall clock survives a restart (persisted on the position)")
p = fresh()
run(ENTRY * 1.20, p, now=T0)
ok(p.get("peak_market_cap_at") == T0,
   "peak_market_cap_at is stored on the position", str(p.get("peak_market_cap_at")))
# Simulate a reload: a fresh dict carrying the persisted field.
reloaded = fresh()
reloaded["peak_market_cap"] = ENTRY * 1.20
reloaded["peak_market_cap_at"] = T0
reloaded["trailing_active"] = False
r, pos, _ = run(ENTRY * 1.20, reloaded, now=T0 + 45)
ok(any("STALL_EXIT" in x for x in r),
   "after a restart the elapsed flat time is still honoured", str(r))

# ─────────────────────────────────────────────────────────────────────────────
section("5. no regression in the other exits")
r, pos, _ = run(ENTRY * 3.0, fresh())
ok(bool(r), "take-profit stages still fire far above entry", str(r))
p = fresh(opened_offset=-49 * 3600)
r, pos, _ = run(ENTRY * 1.02, p, now=T0)
ok(any("TIME_EXIT" in x for x in r) or bool(r),
   "time exit still reachable", str(r))

# ─────────────────────────────────────────────────────────────────────────────
section("6. a malformed stages_hit must not take down the exit cycle")
# open_position() builds stages_hit as [False]*len(stages), but a position
# reloaded from the DB with an empty stages_hit_json comes back as []. The old
# code did `pos.setdefault("stages_hit", [])[i] = True`, which raises IndexError
# on an empty list. The exit monitor catches it, so the WHOLE cycle aborts every
# 2 seconds and NO position can be closed — an unexitable bag holding real money.
STAGE_MC = ENTRY * (1 + float((XS.get("take_profit_stages") or [{}])[0].get("pct", 30)) / 100.0)

for label, mutate in (
    ("stages_hit == [] (what the DB returns for an empty column)", lambda q: q.update({"stages_hit": []})),
    ("stages_hit missing entirely", lambda q: q.pop("stages_hit", None)),
    ("stages_hit is not a list", lambda q: q.update({"stages_hit": "nope"})),
    ("stages_hit shorter than the stage list", lambda q: q.update({"stages_hit": [False]})),
):
    q = fresh()
    mutate(q)
    try:
        r, pos, parts = run(STAGE_MC, q, now=T0)
        crashed = None
    except Exception as e:
        r, pos, parts, crashed = [], None, [], f"{type(e).__name__}: {e}"
    ok(crashed is None, f"{label} -> no exception", crashed or "")
    ok(isinstance((pos or {}).get("stages_hit"), list)
       and len((pos or {}).get("stages_hit") or []) >= len(XS.get("take_profit_stages") or []),
       f"{label} -> stages_hit normalized to one slot per stage",
       str((pos or {}).get("stages_hit")))

# The stage must actually be recorded, otherwise it would re-fire every cycle.
q = fresh(); q["stages_hit"] = []
run(STAGE_MC, q, now=T0)
ok(bool((q.get("stages_hit") or [False])[0]),
   "the first stage is marked as hit (cannot re-fire)", str(q.get("stages_hit")))


# ─────────────────────────────────────────────────────────────────────────────
section("7. normalizing stages_hit must not change recorded PnL")
# check_exits now pads stages_hit to one slot per stage, which makes the list
# non-empty (truthy) even when nothing fired. close_position used a bare
# truthiness test to decide between the plain and the staged PnL formula, so the
# padding alone would have switched every plain exit onto the staged formula.
# The condition is now any(...). Prove the recorded numbers are unchanged.
EXIT_MC = ENTRY * (1 - STOP / 100.0)          # a plain stop-loss exit
EXPECT_PNL = 100.0 * (EXIT_MC / ENTRY - 1)    # size_usd * ratio
EXPECT_PCT = (EXIT_MC / ENTRY - 1) * 100

for label, sh in (("[] (pre-normalization)", []),
                  ("[False, False, False] (post-normalization)", [False, False, False])):
    q = fresh()
    q["stages_hit"] = sh
    run(EXIT_MC, q, now=T0)
    # the closed-trade ledger is the source of truth for the recorded numbers
    from enzo.core import db as _db
    closed_rows = _db.get_full_state().get("closed_positions") or []
    last = closed_rows[-1] if closed_rows else {}
    ok(abs(float(last.get("pnl", 0.0)) - EXPECT_PNL) < 0.01,
       f"{label} -> pnl uses the PLAIN formula", f"{last.get('pnl')} vs {EXPECT_PNL:.2f}")
    ok(abs(float(last.get("pnl_pct", 0.0)) - EXPECT_PCT) < 0.01,
       f"{label} -> pnl_pct is the plain ratio", f"{last.get('pnl_pct')} vs {EXPECT_PCT:.2f}")


CLOCK.t = None
portfolio.load_state = __import__("enzo.core.db", fromlist=["get_full_state"]).get_full_state

try:
    shutil.rmtree(SANDBOX, ignore_errors=True)
except Exception:
    pass

print("\n" + "=" * 68)
print(f"  RESULT: {PASS} passed, {FAIL} failed")
print("=" * 68)
sys.exit(1 if FAIL else 0)
