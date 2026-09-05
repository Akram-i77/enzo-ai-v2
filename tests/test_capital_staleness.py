#!/usr/bin/env python3
"""Regression guard: a stale wallet reading must NOT become immortal.

The defect this pins down
-------------------------
In LIVE mode position size is derived from the real MoonPay wallet balance.
When that balance cannot be read (CLI missing, auth expired, network down),
`portfolio.sync_capital_base` tolerates the LAST SUCCESSFUL reading for
`execution.capital_sync_grace_sec` (300s by default) and blocks sizing after
that. That is the right design.

The implementation broke it: on every failed read it wrote the snapshot back
with `ts = time.time()`. Because the grace window is measured from that `ts`,
each failed attempt RESET the clock, so the window never expired. One good
reading - possibly days old - stayed "deployable" forever:

  * `enzoctl doctor` printed  ✔ capital  $559.40 deployable (wallet)
    while the wallet could not be read at all,
  * the TTL cache then re-served that snapshot as fresh without even trying,
  * and LIVE sizing would have kept using a balance that no longer exists.

Found during the pre-transfer readiness audit, where doctor flipped from
"✖ capital $0.00" to "✔ capital $559.40" between two runs with no wallet
present. Both fixes are asserted here:

  1. portfolio keeps the ORIGINAL timestamp of the last successful read when it
     writes a stale snapshot, and never re-serves a stale snapshot from the TTL
     cache, so grace expires on schedule and sizing is blocked.
  2. `enzoctl doctor` reports a stale reading as STALE (a critical problem),
     never with a green tick.

Runs against the mock MoonPay CLI in tests/mockbin on a throwaway ENZO_HOME.
"""
import copy
import io
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
sys.path.insert(0, HERE)

from conftest_paths import install_mock_on_path, mock_bin_dir   # noqa: E402

MOCK_DIR = install_mock_on_path()
if not MOCK_DIR:
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


# ── sandbox with a SHORT grace window so expiry is observable in seconds ─────
SANDBOX = tempfile.mkdtemp(prefix="enzo-capital-stale-")
os.makedirs(os.path.join(SANDBOX, "config"), exist_ok=True)
os.makedirs(os.path.join(SANDBOX, "data"), exist_ok=True)
# `enzoctl doctor` runs as a subprocess and therefore reads the config FROM
# DISK, not the in-memory CFG dict used by the checks above. Copy the real file
# and shrink the two timing knobs to the same values, so doctor exercises the
# same short grace window instead of the production 300s.
_cfg_text = io.open(os.path.join(ROOT, "config", "enzo-config.yaml"), encoding="utf-8").read()
_cfg_text = re.sub(r"capital_sync_grace_sec:\s*[\d.]+", "capital_sync_grace_sec: 3", _cfg_text)
_cfg_text = re.sub(r"capital_sync_ttl_sec:\s*[\d.]+", "capital_sync_ttl_sec: 1", _cfg_text)
io.open(os.path.join(SANDBOX, "config", "enzo-config.yaml"), "w", encoding="utf-8").write(_cfg_text)
for _f in ("enzo-secrets.json", "enzo-control.json", "enzo-watchlist.json"):
    _src = os.path.join(ROOT, "config", _f)
    if os.path.exists(_src):
        shutil.copy(_src, os.path.join(SANDBOX, "config", _f))
os.environ["ENZO_HOME"] = SANDBOX
os.environ["MOCK_STATE"] = json.dumps({"usdc": 500.0, "sol": 6.0})

from enzo.core import config as C                            # noqa: E402
from enzo.execution import executor as X                     # noqa: E402
from enzo.execution import portfolio as P                    # noqa: E402

GRACE = 3.0
TTL = 1.0

CFG = copy.deepcopy(C.DEFAULTS)
CFG["chain"] = "sol"
CFG["paper_mode"] = False                    # LIVE: the only mode where this matters
CFG["execution"].update({
    "wallet_name": "enzo-trading", "base_token": "SOL", "capital_source": "wallet",
    "min_trade_usd": 1.0, "moonpay_bin": "", "moonpay_chain": "",
    "capital_sync_grace_sec": GRACE, "capital_sync_ttl_sec": TTL,
})


def break_wallet():
    """Make the CLI unreachable: exactly the real-world failure."""
    os.environ["PATH"] = os.pathsep.join(
        p for p in os.environ["PATH"].split(os.pathsep) if p != MOCK_DIR)
    X._BIN_CACHE.update({"path": None, "checked_at": 0.0})
    X._CAPITAL_CACHE.update({"ok": False, "ts": 0.0})
    X.clear_gate()


def fix_wallet():
    os.environ["PATH"] = MOCK_DIR + os.pathsep + os.environ["PATH"]
    X._BIN_CACHE.update({"path": None, "checked_at": 0.0})
    X._CAPITAL_CACHE.update({"ok": False, "ts": 0.0})
    X.clear_gate()


def snapshot():
    try:
        with open(P.CAPITAL_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


try:
    section("1. قراءة ناجحة = رأس مال حقيقي بطابع زمني حقيقي")
    fix_wallet()
    r = P.sync_capital_base(force=True, cfg=CFG)
    snap1 = snapshot()
    good_ts = float(snap1.get("ts") or 0)
    good_usd = float(r.get("usd") or 0)
    check("القراءة نجحت", bool(r.get("ok")) and good_usd > 0, f"${good_usd:,.2f}")
    check("ليست معلَّمة كقديمة", not r.get("stale") and not snap1.get("stale"))
    check("الطابع الزمني = الآن", abs(time.time() - good_ts) < 5, f"ts={good_ts:.0f}")

    section("2. تعطلت المحفظة: داخل نافذة السماح يُستخدم آخر رقم — بلا تجديد لختمه")
    break_wallet()
    r2 = P.sync_capital_base(force=True, cfg=CFG)
    snap2 = snapshot()
    check("ما زال مقبولاً داخل نافذة السماح", bool(r2.get("ok")) and bool(r2.get("stale")),
          str(r2.get("detail"))[:70])
    check("القيمة هي آخر قراءة حقيقية", abs(float(r2.get("usd") or 0) - good_usd) < 0.01,
          f"${float(r2.get('usd') or 0):,.2f}")
    check("★ الطابع الزمني لم يُجدَّد (هذا كان العطل)",
          abs(float(snap2.get("ts") or 0) - good_ts) < 0.001,
          f"ts محفوظ: {snap2.get('ts')} == {good_ts}")

    section("3. محاولات متكررة: العمر يكبر ولا يتصفّر أبداً")
    ages = []
    stamps = []
    for i in range(4):
        time.sleep(GRACE / 5.0)
        break_wallet()
        rr = P.sync_capital_base(force=True, cfg=CFG)
        ages.append(float(rr.get("age_sec") or -1))
        stamps.append(float(snapshot().get("ts") or 0))
    check("العمر يزداد باطراد مع كل محاولة فاشلة",
          all(b > a for a, b in zip(ages, ages[1:])), f"{[round(a,1) for a in ages]}")
    check("الطابع الزمني ثابت عبر كل المحاولات",
          all(abs(t - good_ts) < 0.001 for t in stamps))

    section(f"4. انتهت نافذة السماح ({GRACE:.0f} ثوانٍ) = حجب صريح لا استمرار")
    time.sleep(max(0.0, GRACE - (time.time() - good_ts) + 0.4))
    break_wallet()
    r3 = P.sync_capital_base(force=True, cfg=CFG)
    check("صار محجوباً", bool(r3.get("ok")) is False and bool(r3.get("blocked")),
          str(r3.get("detail"))[:70])
    # NOTE: `float(x or -1)` would turn a legitimate 0.0 into -1 - zeros are
    # falsy in Python, and zero is exactly the value under test here.
    check("رأس المال المُعلن = صفر (لا رقم قديم)", r3.get("usd") is not None and float(r3["usd"]) == 0.0,
          f"usd={r3.get('usd')}")
    check("رأس المال القابل للإنفاق = صفر",
          r3.get("spendable_usd") is not None and float(r3["spendable_usd"]) == 0.0,
          f"spendable={r3.get('spendable_usd')}")

    section("5. ذاكرة التخزين المؤقت لا تُحيي قراءة قديمة")
    time.sleep(TTL + 0.2)
    break_wallet()
    r4 = P.sync_capital_base(force=False, cfg=CFG)
    check("بلا force ما زال يرفض (لا يقدّم القديم كأنه طازج)", bool(r4.get("ok")) is False,
          str(r4.get("detail"))[:70])

    # A stale snapshot must also be refused by the TTL fast-path, not re-served
    # as fresh, and its timestamp must survive untouched (seeded 1.5s in the past
    # so "unchanged" is measurable rather than a race against the clock).
    seeded_ts = time.time() - 1.5
    with open(P.CAPITAL_PATH, "w", encoding="utf-8") as f:
        json.dump({"source": "wallet", "usd": good_usd, "ok": True, "stale": True,
                   "spendable_usd": 1.0, "base_token": "SOL", "ts": seeded_ts}, f)
    break_wallet()
    r5 = P.sync_capital_base(force=False, cfg=CFG)
    check("لقطة معلَّمة stale لا تُقدَّم أبداً كقراءة طازجة",
          bool(r5.get("stale")) or bool(r5.get("blocked")),
          f"ok={r5.get('ok')} stale={r5.get('stale')} blocked={r5.get('blocked')}")
    check("وختمها الزمني يبقى كما زُرع (لا يُجدَّد)",
          abs(float(snapshot().get("ts") or 0) - seeded_ts) < 0.001,
          f"{snapshot().get('ts'):.3f} == {seeded_ts:.3f}")

    section("6. التعافي: قراءة حقيقية جديدة تمحو الحالة القديمة")
    fix_wallet()
    r6 = P.sync_capital_base(force=True, cfg=CFG)
    snap6 = snapshot()
    check("عاد مقبولاً", bool(r6.get("ok")) and not r6.get("stale"), f"${float(r6.get('usd') or 0):,.2f}")
    check("طابع زمني جديد", abs(float(snap6.get("ts") or 0) - time.time()) < 5)
    check("لا أثر لـ stale على القرص", not snap6.get("stale"))

    section("7. doctor: القراءة القديمة لم تعد ✔ (فحص من طرف لطرف)")
    fix_wallet()
    P.sync_capital_base(force=True, cfg=CFG)          # seed one good reading
    break_wallet()                                    # then lose the wallet
    env = dict(os.environ, ENZO_HOME=SANDBOX, MOCK_STATE=os.environ["MOCK_STATE"])
    proc = subprocess.run([sys.executable, "enzoctl", "doctor"], cwd=ROOT, env=env,
                          capture_output=True, text=True, timeout=240)
    out = proc.stdout + proc.stderr
    # Match ONLY the doctor's capital line (the sandbox path also contains the
    # word "capital", which made a looser filter match the wrong line).
    import re as _re2
    cap_lines = [l for l in out.splitlines() if _re2.search(r"(✔|✖|⚠)\s+capital\b", l)]
    check("doctor يسمّيها STALE", any("STALE" in l for l in cap_lines),
          (cap_lines[0].strip()[:120] if cap_lines else "لا سطر capital"))
    check("ولا يمنحها علامة ✔", not any(l.strip().startswith("✔ capital") for l in cap_lines))
    check("بل علامة ✖ صريحة", any(l.strip().startswith("✖ capital") for l in cap_lines))
    check("وتُحسب مشكلة حرجة", "critical problem" in out)

finally:
    import shutil
    shutil.rmtree(SANDBOX, ignore_errors=True)

print("\n" + "=" * 68)
print(f"  RESULT: {PASS} passed, {FAIL} failed")
print("=" * 68)
sys.exit(1 if FAIL else 0)
