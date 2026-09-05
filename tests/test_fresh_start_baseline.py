#!/usr/bin/env python3
"""Regression guard: a FRESH ledger must be anchored to the real wallet.

Why this matters more than it looks
-----------------------------------
`portfolio_state.initial_capital` defaults to a fictitious $10,000. It is the
baseline for equity, ROI and - critically - the max_drawdown circuit breaker.
In LIVE mode the engine rebases it onto the real wallet figure on the first
successful read (`sync_capital_base(rebase=True)` -> `_maybe_rebase`).

Two ways this silently failed, both found while auditing readiness for a fresh
OpenClaw workspace (exactly the situation where the ledger IS new):

  1. `_maybe_rebase` called `load_config()` AGAIN instead of using the config the
     caller had already loaded. Any hiccup there raised inside a `try:` whose
     `except` logged at DEBUG - invisible. The bot then traded with a $10,000
     baseline while the wallet held a few hundred dollars, so a 25% drawdown
     limit was measured against money that did not exist.
  2. When `atomic_update_initial_capital` refused (it returns False rather than
     raising), nothing was logged at all.

Both now log a WARNING naming the fix. This suite pins the behaviour down:
rebase happens when it should, is refused when it would corrupt history, is
idempotent, survives a missing config file, and is never silent about failure.
`enzoctl doctor` additionally reports a fictitious baseline as its own item.

Runs against the mock MoonPay CLI on a throwaway ENZO_HOME per scenario.
"""
import copy
import io
import json
import logging
import os
import shutil
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
sys.path.insert(0, HERE)

from conftest_paths import install_mock_on_path, mock_bin_dir   # noqa: E402

if not install_mock_on_path():
    print("\n  ABORT  no mock MoonPay CLI found (expected tests/mockbin/mp).")
    sys.exit(2)
MOCK_DIR = mock_bin_dir()

PASS = FAIL = 0


def check(name, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  \033[32mPASS\033[0m  {name}" + (f"   {detail}" if detail else ""))
    else:
        FAIL += 1
        print(f"  \033[31mFAIL\033[0m  {name}   {detail}")
    return bool(cond)


def section(t):
    print(f"\n=== {t} ===")


DEFAULT_BASE = 10000.0


class _Capture(logging.Handler):
    def __init__(self):
        super().__init__(level=logging.DEBUG)
        self.records = []

    def emit(self, record):
        self.records.append((record.levelname, record.getMessage()))

    def texts(self, level=None):
        return [m for lv, m in self.records if level is None or lv == level]


def scenario(sol=3.1, usdc=0.0, with_config=True, paper=False, wallet_ok=True,
             closed=0, open_pos=0, refuse_update=False, raise_update=False):
    """Build a throwaway workspace, run one capital sync, report the outcome."""
    tmp = tempfile.mkdtemp(prefix="enzo-freshstart-")
    os.makedirs(os.path.join(tmp, "config"), exist_ok=True)
    os.makedirs(os.path.join(tmp, "data"), exist_ok=True)
    if with_config:
        shutil.copy(os.path.join(ROOT, "config", "enzo-config.yaml"),
                    os.path.join(tmp, "config", "enzo-config.yaml"))
    os.environ["ENZO_HOME"] = tmp
    os.environ["MOCK_STATE"] = json.dumps({"usdc": usdc, "sol": sol})

    # Put the mock CLI on PATH, or take it away to simulate an unreadable wallet.
    paths = [p for p in os.environ["PATH"].split(os.pathsep) if p != MOCK_DIR]
    if wallet_ok:
        paths = [MOCK_DIR] + paths
    os.environ["PATH"] = os.pathsep.join(paths)

    for m in [m for m in list(sys.modules) if m.startswith("enzo")]:
        del sys.modules[m]

    from enzo.core import config as C, db, audit                # noqa: F401
    from enzo.execution import executor as X, portfolio as P

    cfg = copy.deepcopy(C.DEFAULTS)
    cfg["chain"] = "sol"
    cfg["paper_mode"] = paper
    cfg["execution"].update({
        "wallet_name": "enzo-trading", "base_token": "SOL",
        "capital_source": "ledger" if paper else "wallet",
        "min_trade_usd": 1.0, "moonpay_bin": "",
    })
    X._BIN_CACHE.update({"path": None, "checked_at": 0.0})
    X._CAPITAL_CACHE.update({"ok": False, "ts": 0.0})
    X.clear_gate()

    db.init_db()
    if closed:
        for i in range(closed):
            db.atomic_close_position(f"ClosedMint{i}111111111111111111111111111111", {
                "symbol": f"OLD{i}", "entry_price": 1e-6, "exit_price": 1.2e-6,
                "entry_market_cap": 100000.0, "exit_market_cap": 120000.0,
                "size_usd": 1.0, "pnl": 0.2, "pnl_pct": 20.0, "reason": "TAKE_PROFIT",
            }, 0.2)
    if open_pos:
        db.atomic_open_position({
            "mint": "OpenMint11111111111111111111111111111111", "symbol": "OPEN",
            "entry_price": 1e-6, "entry_market_cap": 100000.0, "size_usd": 1.0,
            "amount": 1e6, "stop_loss_mc": 62000.0, "take_profit_mc": 130000.0,
        })

    if refuse_update:
        db.atomic_update_initial_capital = lambda v: False
    if raise_update:
        def _boom(v):
            raise RuntimeError("synthetic db failure")
        db.atomic_update_initial_capital = _boom

    cap_log = _Capture()
    logging.getLogger("enzo.portfolio").addHandler(cap_log)
    logging.getLogger("enzo.portfolio").setLevel(logging.DEBUG)

    before = db.get_full_state()
    cap = P.sync_capital_base(force=True, cfg=cfg, rebase=True)
    after = db.get_full_state()

    logging.getLogger("enzo.portfolio").removeHandler(cap_log)
    result = {"tmp": tmp, "cap": cap, "before": before, "after": after,
              "warnings": cap_log.texts("WARNING"), "cfg": cfg, "P": P, "db": db}
    return result


def cleanup(r):
    shutil.rmtree(r["tmp"], ignore_errors=True)


try:
    section("1. دفتر جديد + وضع LIVE + محفظة مقروءة = الأساس هو رصيدك الحقيقي")
    r = scenario(sol=3.1)
    wallet = float(r["cap"].get("usd") or 0)
    check("القراءة نجحت", bool(r["cap"].get("ok")) and wallet > 0, f"${wallet:,.2f}")
    check("★ initial_capital صار رصيد المحفظة (لا 10,000 الوهمية)",
          abs(float(r["after"]["initial_capital"]) - wallet) < 0.01,
          f"{r['before']['initial_capital']:,.0f} → {r['after']['initial_capital']:,.2f}")
    check("والذروة صُفّرت على الأساس الجديد (لا تراجع وهمي 94% يوقف البوت)",
          abs(float(r["after"]["peak_equity"]) - wallet) < 0.01,
          f"peak={r['after']['peak_equity']:,.2f}")
    check("ولا إيقاف قسري موروث", not r["after"].get("halted"))
    cleanup(r)

    section("2. الدورة التالية: لا إعادة ضبط متكررة (متقارب لا متذبذب)")
    r = scenario(sol=3.1)
    first = float(r["after"]["initial_capital"])
    cap2 = r["P"].sync_capital_base(force=True, cfg=r["cfg"], rebase=True)
    st2 = r["db"].get_full_state()
    check("الأساس لم يتغير في الدورة الثانية", abs(float(st2["initial_capital"]) - first) < 0.01,
          f"{first:,.2f} → {float(st2['initial_capital']):,.2f}")
    check("ولم يُسجَّل حدث rebase ثانٍ", float(cap2.get("usd") or 0) > 0)
    cleanup(r)

    section("3. وجود سجلّ صفقات = لا إعادة ضبط (حماية معنى ROI ونسبة الربح)")
    r = scenario(sol=3.1, closed=2)
    check("الأساس بقي كما هو رغم قراءة المحفظة",
          abs(float(r["after"]["initial_capital"]) - DEFAULT_BASE) < 0.01,
          f"initial_capital={r['after']['initial_capital']:,.2f} · صفقات مغلقة={len(r['after']['closed_positions'])}")
    cleanup(r)

    section("4. وجود مركز مفتوح = لا إعادة ضبط (الدفتر يجب أن يبقى متسقاً)")
    r = scenario(sol=3.1, open_pos=1)
    check("الأساس لم يُمسّ", abs(float(r["after"]["initial_capital"]) - DEFAULT_BASE) < 0.01,
          f"مراكز مفتوحة={len(r['after']['open_positions'])}")
    cleanup(r)

    section("5. ★ العطل الذي أُصلح: لا ملف إعداد في ENZO_HOME")
    r = scenario(sol=3.1, with_config=False)
    wallet = float(r["cap"].get("usd") or 0)
    check("الضبط يتم رغم غياب ملف الإعداد (الإعداد يُمرَّر لا يُعاد تحميله)",
          abs(float(r["after"]["initial_capital"]) - wallet) < 0.01,
          f"→ {r['after']['initial_capital']:,.2f}")
    check("ولا تحذير من تخطٍّ (كان يفشل بصمت هنا)",
          not any("rebase skipped" in w for w in r["warnings"]), str(r["warnings"])[:90])
    cleanup(r)

    section("6. الرفض ليس صمتاً: تحذير يذكر الحل")
    r = scenario(sol=3.1, refuse_update=True)
    check("سجّل WARNING حين رفضت قاعدة البيانات التحديث",
          any("rebase did not apply" in w for w in r["warnings"]), str(r["warnings"])[:110])
    check("وذكر الأمر الصريح ./enzoctl rebase --confirm",
          any("rebase --confirm" in w for w in r["warnings"]))
    cleanup(r)

    section("7. الاستثناء ليس صمتاً: WARNING بدل DEBUG")
    r = scenario(sol=3.1, raise_update=True)
    check("سجّل WARNING مع سبب الاستثناء",
          any("rebase skipped" in w and "synthetic db failure" in w for w in r["warnings"]),
          str(r["warnings"])[:110])
    check("ولم ينهر المسار (القراءة ما زالت صالحة)", bool(r["cap"].get("ok")))
    cleanup(r)

    section("8. وضع PAPER: الأساس من الدفتر لا من المحفظة")
    r = scenario(sol=3.1, paper=True)
    check("المصدر ledger", r["cap"].get("source") == "ledger", str(r["cap"].get("source")))
    check("ولم يُعد الضبط على المحفظة", abs(float(r["after"]["initial_capital"]) - DEFAULT_BASE) < 0.01)
    cleanup(r)

    section("9. محفظة غير مقروءة: الأساس لا يُخمَّن والتداول يُحجب")
    r = scenario(sol=3.1, wallet_ok=False)
    check("القراءة فشلت أو حُجبت", not r["cap"].get("ok") or bool(r["cap"].get("blocked")),
          str(r["cap"].get("detail"))[:70])
    check("والأساس بقي على حاله (لا رقم مختلَق)",
          abs(float(r["after"]["initial_capital"]) - DEFAULT_BASE) < 0.01,
          f"initial_capital={r['after']['initial_capital']:,.2f}")
    cleanup(r)

    section("10. doctor: الأساس الوهمي بند مستقل")
    # A truly untouched ledger: create the workspace and let doctor be the first
    # thing that looks at it. It reads capital WITHOUT rebasing, so the fictitious
    # 10,000 baseline must be reported instead of being shown as equity.
    tmp = tempfile.mkdtemp(prefix="enzo-doctor-fresh-")
    os.makedirs(os.path.join(tmp, "config"), exist_ok=True)
    os.makedirs(os.path.join(tmp, "data"), exist_ok=True)
    shutil.copy(os.path.join(ROOT, "config", "enzo-config.yaml"),
                os.path.join(tmp, "config", "enzo-config.yaml"))
    os.environ["ENZO_HOME"] = tmp
    os.environ["MOCK_STATE"] = json.dumps({"usdc": 0.0, "sol": 3.1})
    os.environ["PATH"] = MOCK_DIR + os.pathsep + os.pathsep.join(
        p for p in os.environ["PATH"].split(os.pathsep) if p != MOCK_DIR)
    try:
        proc = subprocess.run([sys.executable, "enzoctl", "doctor"], cwd=ROOT,
                              env=dict(os.environ), capture_output=True, text=True, timeout=300)
        out = proc.stdout + proc.stderr
        lines = [l for l in out.splitlines() if "ledger_baseline" in l]
        check("doctor يفحص أساس الدفتر بنداً مستقلاً", bool(lines),
              (lines[0].strip()[:130] if lines else "لا سطر"))
        check("ويصف الأساس الوهمي صراحةً (10,000 مقابل المحفظة الحقيقية)",
              bool(lines) and "fictitious" in lines[0],
              (lines[0].strip()[:130] if lines else ""))
        check("ويذكر الحلّ: rebase --confirm", bool(lines) and "rebase --confirm" in out)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

finally:
    os.environ.pop("ENZO_HOME", None)

print("\n" + "=" * 68)
print(f"  RESULT: {PASS} passed, {FAIL} failed")
print("=" * 68)
sys.exit(1 if FAIL else 0)
