#!/usr/bin/env python3
"""Regression guard: the min_trade floor must hold at the LAST gate too.

Why this file exists
--------------------
`min_trade_usd` is a FLOOR (the owner's explicit rule): a computed $0.08 position
must be RAISED to $1.00 and traded, not cancelled. Sizing enforces that, but
executor.buy_token() carried its own older hard-reject gate. Whenever any path
handed it a sub-floor amount, the two gates contradicted each other and the
trade died with "below minimum" - exactly what a real wallet showed on
2026-09-04 (conf=70 band computed $0.08 on a $2.06 ledger figure).

buy_token() now applies the same rule at the last gate: clamp up to the floor
when the wallet can fund it, otherwise refuse with a detail that says plainly
how much spendable base token exists and what to change.
"""
import os
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO not in sys.path:
    sys.path.insert(0, REPO)
sys.path.insert(0, os.path.join(REPO, "tests"))

from conftest_paths import install_mock_on_path, isolate_home  # noqa: E402
install_mock_on_path()

# Isolate BEFORE importing enzo (config resolves state paths at import time).
isolate_home(prefix="enzo-floor-")

from enzo.execution import executor, portfolio  # noqa: E402

PASS = FAIL = 0


def check(name, ok, detail=""):
    global PASS, FAIL
    if ok:
        PASS += 1
        print(f"  ✔ {name}")
    else:
        FAIL += 1
        print(f"  ✖ {name}" + (f" — {detail}" if detail else ""))


MINT = "So11111111111111111111111111111111111111112"
_real_deployable = portfolio.deployable_capital


def fake_capital(value):
    def _f(cfg=None, state=None, force=False):
        return value
    return _f


try:
    print("\n=== 1) محفظة تملك الأرضية -> يُرفع الحجم وتُنفَّذ الصفقة ===")
    portfolio.deployable_capital = fake_capital(5.0)
    r = executor.buy_token(mint=MINT, amount_usd=0.08)
    check("الصفقة لم تُرفض", r.get("ok") is True, str(r.get("reason")))
    check("الحجم رُفع إلى الأرضية $1.00", float(r.get("amount_usd", 0)) == 1.0,
          str(r.get("amount_usd")))

    print("\n=== 2) محفظة لا تملك الأرضية -> رفض صريح بالمبلغ المتاح ===")
    portfolio.deployable_capital = fake_capital(0.5)
    r = executor.buy_token(mint=MINT, amount_usd=0.08)
    check("رُفضت", r.get("ok") is False)
    check("رمز السبب BELOW_MINIMUM", "BELOW_MIN" in str(r.get("reason_code", "")),
          str(r.get("reason_code")))
    det = str(r.get("detail", ""))
    check("الرسالة تذكر المتاح فعلاً ($0.50)", "$0.50" in det, det[:120])
    check("الرسالة تقترح إصلاح عملة الأساس", "base_token" in det, det[:160])

    print("\n=== 3) حجم فوق الأرضية لا يتأثر ===")
    portfolio.deployable_capital = fake_capital(50.0)
    r = executor.buy_token(mint=MINT, amount_usd=7.5)
    check("نجحت", r.get("ok") is True, str(r.get("reason")))
    check("الحجم كما هو $7.50", float(r.get("amount_usd", 0)) == 7.5,
          str(r.get("amount_usd")))
finally:
    portfolio.deployable_capital = _real_deployable

print("\n" + "=" * 68)
print(f"  RESULT: {PASS} passed, {FAIL} failed")
print("=" * 68)
sys.exit(1 if FAIL else 0)
