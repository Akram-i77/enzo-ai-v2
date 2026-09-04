#!/usr/bin/env python3
"""Regression guard: rug-protection layers 1, 3 and 4 (owner-approved 2026-09-05).

Layer 1 — absolute fingerprint VETOES at entry (analyze.fingerprint_signals):
    bundled buys, fresh sybil wallets, insiders selling now, serial factories.
    Readable at FIRST look, so they close the delta-blind spot without delaying
    any legitimate entry (organic launches do not carry these fingerprints).

Layer 3 — risk-conditioned EARLY STOP: a tighter stop for the first minutes,
    applied ONLY to positions whose entry carried soft flags. Clean entries keep
    the owner's -38% stop and -40% trailing completely untouched.

Layer 4 — RUG TRIPWIRE after entry: liquidity pulled / holders collapsing /
    top10 flipping to selling, measured against the entry snapshot. Two votes
    close the position at whatever the price is - a rug in motion has no
    technical level left to respect.

Runs in a throwaway ENZO_HOME; the real database and config are never touched.
"""
import os
import shutil
import sys
import tempfile
import time
from datetime import datetime, timezone

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

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


SANDBOX = tempfile.mkdtemp(prefix="enzo-ruglayers-")
os.makedirs(os.path.join(SANDBOX, "config"), exist_ok=True)
os.makedirs(os.path.join(SANDBOX, "data"), exist_ok=True)
for f in ("enzo-config.yaml", "enzo-control.json", "enzo-watchlist.json", "enzo-secrets.json"):
    src = os.path.join(ROOT, "config", f)
    if os.path.exists(src):
        shutil.copy(src, os.path.join(SANDBOX, "config", f))
os.environ["ENZO_HOME"] = SANDBOX

from enzo.analyzers import analyze                      # noqa: E402
from enzo.execution import portfolio                    # noqa: E402
from enzo.core.config import load_config                # noqa: E402

CFG = load_config()
MINT = "RuG1111111111111111111111111111111111111111"
ENTRY_MC = 100_000.0


def wal(bundlers=None, snipers=None, rats=None, age=None, sells=None):
    return {"available": True, "score": 40,
            "bundler_count": bundlers, "sniper_count": snipers, "rat_count": rats,
            "detail": {"avg_wallet_age_days": age, "top10_cur_sells": sells}}


def dev(created=None, open_ratio=None):
    return {"available": True, "score": 50, "events": [],
            "detail": {"creator_created_count": created, "open_ratio": open_ratio}}


# ─────────────────────────────────────────────────────────────────────────────
section("1) الطبقة 1: بصمات مطلقة تُرفض عند أول نظرة")
fp = analyze.fingerprint_signals(wal(bundlers=9), dev(), CFG)
ok(any("bundlers_top20" in v for v in fp["vetoes"]), "9 bundlers ضمن أعلى 20 = نقض", str(fp["vetoes"]))
fp = analyze.fingerprint_signals(wal(bundlers=4), dev(), CFG)
ok(not fp["vetoes"] and fp["flags"], "4 bundlers = علم (نصف العتبة) لا نقض", str(fp["flags"]))
fp = analyze.fingerprint_signals(wal(age=1.5), dev(), CFG)
ok(any("wallet age" in v for v in fp["vetoes"]), "عمر محافظ 1.5 يوم = نقض", str(fp["vetoes"]))
fp = analyze.fingerprint_signals(wal(age=5), dev(), CFG)
ok(not fp["vetoes"] and fp["flags"], "عمر 5 أيام = علم فقط", str(fp["flags"]))
fp = analyze.fingerprint_signals(wal(sells=30), dev(), CFG)
ok(any("top10_cur_sells" in v for v in fp["vetoes"]), "العشرة الكبار يبيعون الآن (30) = نقض")
fp = analyze.fingerprint_signals(wal(), dev(created=120, open_ratio=0.01), CFG)
ok(any("factory" in v for v in fp["vetoes"]), "مصنع серийный (120 عملة، 1% مفتوح) = نقض", str(fp["vetoes"]))
fp = analyze.fingerprint_signals(wal(), dev(created=120, open_ratio=0.40), CFG)
ok(not fp["vetoes"], "منشئ كثير لكن 40% من عملاته حيّة = لا نقض")
fp = analyze.fingerprint_signals(wal(bundlers=2, age=40, sells=1), dev(), CFG)
ok(not fp["vetoes"] and not fp["flags"], "عملة عضوية نظيفة = لا نقض ولا علم", str(fp))
fp = analyze.fingerprint_signals({}, {}, CFG)
ok(not fp["vetoes"], "لا بيانات محفظة = لا اختلاق نقض")
cfg_off = dict(CFG); cfg_off["rug_protection"] = dict(CFG["rug_protection"], fingerprints_enabled=False)
fp = analyze.fingerprint_signals(wal(bundlers=99), dev(), cfg_off)
ok(not fp["vetoes"], "fingerprints_enabled=false يعطّل الطبقة 1")

section("1ب) الطبقة 1 موصولة ببوابة القرار")
src = open(os.path.join(ROOT, "enzo", "analyzers", "analyze.py"), encoding="utf-8").read()
gate = src[src.index("def analyze_token"):] if "def analyze_token" in src else src
ok("rejected.extend(_fp[\"vetoes\"])" in src, "النقوض تُضاف إلى rejected")
ok(src.index("rejected.extend(_fp") < src.index("hard_fail = bool(rejected)"),
   "…وقبل تقييم hard_fail")
ok('"rug_flags": _fp["flags"]' in src, "الأعلام تُمرَّر للنتيجة (لتخزينها مع المركز)")

# ─────────────────────────────────────────────────────────────────────────────
def make_pos(flags=None, opened_sec_ago=30.0, snap=True):
    now = time.time()
    p = {
        "symbol": "RUG", "mint": MINT, "entry_price": 0.001,
        "entry_market_cap": ENTRY_MC, "current_market_cap": ENTRY_MC,
        "peak_market_cap": ENTRY_MC, "peak_market_cap_at": now,
        "size_usd": 1.0, "initial_size_usd": 1.0, "amount": 1000.0,
        "initial_amount": 1000.0, "realized_pnl_total": 0.0,
        "stages_hit": [False, False, False], "trailing_active": False,
        "opened_at": datetime.fromtimestamp(now - opened_sec_ago, timezone.utc).isoformat(),
        "max_holding_hours": 48,
        "rug_flags": list(flags or []),
        "entry_liq": 100_000.0 if snap else None,
        "entry_holders": 500.0 if snap else None,
        "entry_top10_sells": 2.0 if snap else None,
        "entry_top10_dumping": False if snap else None,
    }
    return p


def run(mcap, pos, trip=None):
    state = {"initial_capital": 1000.0, "realized_pnl": 0.0, "peak_equity": 1000.0,
             "open_positions": {MINT: pos}, "closed_positions": []}
    portfolio.load_state = lambda *a, **k: state
    closed, _partials = portfolio.check_exits({MINT: mcap}, tripwire_stats=trip)
    return [str(c.get("reason", "")) for c in (closed or [])], state["open_positions"].get(MINT)


section("3) الطبقة 3: وقف أضيق للمدخل المشبوه فقط")
r, _ = run(ENTRY_MC * 0.87, make_pos(flags=["bundlers_top20=4"]))
ok(any(x.startswith("EARLY_STOP") for x in r), "دخل بعلم وهبط -13% داخل النافذة = EARLY_STOP", str(r))
r, _ = run(ENTRY_MC * 0.87, make_pos(flags=[]))
ok(not r, "دخل نظيف وهبط -13% = لا خروج (وقفه -38% لم يُمس)", str(r))
r, _ = run(ENTRY_MC * 0.87, make_pos(flags=["bundlers_top20=4"], opened_sec_ago=11 * 60))
ok(not any(x.startswith("EARLY_STOP") for x in r), "بعد انتهاء النافذة (11 د) = لا وقف مبكر", str(r))
r, _ = run(ENTRY_MC * 0.60, make_pos(flags=["bundlers_top20=4"], opened_sec_ago=11 * 60))
ok(any(x == "STOP_LOSS" for x in r), "خارج النافذة عند -40% = STOP_LOSS العادي", str(r))
r, _ = run(ENTRY_MC * 0.60, make_pos(flags=[]))
ok(any(x == "STOP_LOSS" for x in r), "دخل نظيف عند -40% = STOP_LOSS العادي", str(r))
cfg2 = dict(CFG); cfg2["rug_protection"] = dict(CFG["rug_protection"], early_stop_enabled=False)
_orig = portfolio.load_config
portfolio.load_config = lambda *a, **k: cfg2
try:
    r, _ = run(ENTRY_MC * 0.87, make_pos(flags=["bundlers_top20=4"]))
finally:
    portfolio.load_config = _orig
ok(not r, "early_stop_enabled=false يعطّل الطبقة 3", str(r))

section("4) الطبقة 4: كاشف الرغّ أثناء حدوثه")
trip_bad = {"liq": 50_000.0, "holders": 400.0, "top10_sells": 3.0, "top10_dumping": False}
r, _ = run(ENTRY_MC * 1.05, make_pos(), trip={MINT: trip_bad})
ok(any(x.startswith("RUG_TRIPWIRE") for x in r),
   "سيولة -50% وحاملون -20% والعملة +5% = خروج فوري", str(r))
r, _ = run(ENTRY_MC * 1.05, make_pos(), trip={MINT: {"liq": 50_000.0, "holders": 490.0,
                                                     "top10_sells": 2.0, "top10_dumping": False}})
ok(not r, "صوت واحد فقط = لا خروج (الحد صوتان)", str(r))
r, _ = run(ENTRY_MC * 1.05, make_pos(), trip={MINT: {"liq": 99_000.0, "holders": 499.0,
                                                     "top10_sells": 20.0, "top10_dumping": True}})
ok(any(x.startswith("RUG_TRIPWIRE") for x in r), "قفزة بيع العشرة + انقلابهم للتفريغ = خروج", str(r))
r, _ = run(ENTRY_MC * 1.05, make_pos(snap=False), trip={MINT: trip_bad})
ok(not any(x.startswith("RUG_TRIPWIRE") for x in r),
   "بلا لقطة دخول = لا اختلاق إنذار (تدهور لطيف)", str(r))
cfg3 = dict(CFG); cfg3["rug_protection"] = dict(CFG["rug_protection"], tripwire_enabled=False)
portfolio.load_config = lambda *a, **k: cfg3
try:
    r, _ = run(ENTRY_MC * 1.05, make_pos(), trip={MINT: trip_bad})
finally:
    portfolio.load_config = _orig
ok(not r, "tripwire_enabled=false يعطّل الطبقة 4", str(r))
r, _ = run(ENTRY_MC * 0.55, make_pos(), trip={MINT: {"liq": 10_000.0, "holders": 100.0,
                                                     "top10_sells": 40.0, "top10_dumping": True}})
ok(any(x.startswith("RUG_TRIPWIRE") for x in r),
   "الإنذار يسبق وقف الخسارة (يخرج بأعلى سعر متاح)", str(r))

shutil.rmtree(SANDBOX, ignore_errors=True)
print("\n" + "=" * 68)
print(f"  RESULT: {PASS} passed, {FAIL} failed")
print("=" * 68)
sys.exit(1 if FAIL else 0)
