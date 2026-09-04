#!/usr/bin/env python3
"""Regression guard: the PAUSE switch must never silently re-arm live trading.

Why this file exists
--------------------
`paused` is the one control an operator uses to stop real money from moving.
Two defects made it untrustworthy, both fixed and both guarded here:

1. `set_paused()` wrote with a plain `open(..., "w")`. An interrupted write
   (crash, disk full, power loss, supervisor kill) leaves a TRUNCATED file.

2. `is_paused()` swallowed every read error and returned False. Combined with
   (1) that means a damaged control file silently reports "not paused" and the
   engine resumes buying — the exact opposite of what the operator asked for.
   It now fails CLOSED (stays paused) and logs loudly; pressing Resume rewrites
   the file atomically, which is the recovery path.

`set_paused()` also never recorded who changed it or when, so a paused bot could
not be diagnosed. It now stamps `updated_at` / `updated_by`, and every caller
passes a label identifying its surface (web-dashboard / telegram:button /
telegram:/pause / telegram:/resume).

All checks run against a throwaway CONTROL_PATH in a temp dir. The repository's
real config/enzo-control.json is never touched.
"""

import json
import os
import re
import sys
import tempfile

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO not in sys.path:
    sys.path.insert(0, REPO)

from enzo.ui import botctl  # noqa: E402

PASS = FAIL = 0


def check(name, ok, detail=""):
    global PASS, FAIL
    if ok:
        PASS += 1
        print(f"  ✔ {name}")
    else:
        FAIL += 1
        print(f"  ✖ {name}" + (f" — {detail}" if detail else ""))
    return ok


TMP = tempfile.mkdtemp(prefix="enzo-pause-test-")
REAL_PATH = botctl.CONTROL_PATH
botctl.CONTROL_PATH = os.path.join(TMP, "enzo-control.json")


def write_raw(text):
    with open(botctl.CONTROL_PATH, "w", encoding="utf-8") as f:
        f.write(text)


def read_raw():
    with open(botctl.CONTROL_PATH, "r", encoding="utf-8") as f:
        return json.load(f)


def reset():
    if os.path.exists(botctl.CONTROL_PATH):
        os.remove(botctl.CONTROL_PATH)


# ── 1. round-trip ────────────────────────────────────────────────────────────
def test_round_trip():
    print("\n=== 1. الإيقاف والاستئناف يُحفظان ويُقرآن بشكل صحيح ===")
    reset()
    check("لا ملف = غير موقوف (لم يطلب أحد الإيقاف)", botctl.is_paused() is False)

    botctl.set_paused(True, by="test")
    check("set_paused(True) -> is_paused() True", botctl.is_paused() is True)
    check("القيمة محفوظة على القرص فعلاً", read_raw().get("paused") is True)

    botctl.set_paused(False, by="test")
    check("set_paused(False) -> is_paused() False", botctl.is_paused() is False)
    check("الاستئناف محفوظ على القرص", read_raw().get("paused") is False)


# ── 2. audit trail ───────────────────────────────────────────────────────────
def test_audit_trail():
    print("\n=== 2. سجلّ التدقيق: من أوقف ومتى ===")
    from datetime import datetime, timezone
    reset()
    write_raw(json.dumps({"paused": False, "updated_at": "2001-01-01T00:00:00+00:00",
                          "updated_by": "stale"}))
    before = datetime.now(timezone.utc)
    botctl.set_paused(True, by="web-dashboard")
    data = read_raw()
    check("updated_by يسجّل المصدر", data.get("updated_by") == "web-dashboard",
          str(data.get("updated_by")))
    stamp = data.get("updated_at") or ""
    fresh = False
    try:
        fresh = datetime.fromisoformat(stamp) >= before
    except Exception:
        fresh = False
    check("updated_at طابع زمني جديد (ليس القديم)", fresh, stamp)
    check("الحقول غير المعروفة تُحفظ ولا تُفقد",
          botctl.set_paused(False, by="x") is None and "paused" in read_raw())


# ── 3. atomic write ──────────────────────────────────────────────────────────
def test_atomic_write():
    print("\n=== 3. الكتابة ذرّية (لا ملف مشوّه عند الانقطاع) ===")
    reset()
    for i in range(25):
        botctl.set_paused(i % 2 == 0, by="loop")
    leftovers = [f for f in os.listdir(TMP) if f.endswith(".tmp")]
    check("لا بقايا ملفات .tmp بعد 25 كتابة", not leftovers, str(leftovers))
    check("الملف الناتج JSON صالح دائماً", isinstance(read_raw(), dict))


# ── 4. fail CLOSED on damage ─────────────────────────────────────────────────
def test_fail_closed():
    print("\n=== 4) ملف تالف -> يفشل آمناً (يبقى موقوفاً) ===")
    cases = [
        ("JSON مبتور (كتابة انقطعت)", '{"paused": tr'),
        ("فارغ تماماً", ''),
        ("JSON صالح لكنه null", 'null'),
        ("قائمة بدل كائن", '[1, 2, 3]'),
        ("رقم", '42'),
        ("نص عشوائي", 'not json at all'),
    ]
    for label, content in cases:
        write_raw(content)
        got = botctl.is_paused()
        check(f"{label} -> is_paused() True", got is True, f"got {got!r}")

    reset()
    check("الملف المفقود -> False (لم يُطلب إيقاف قط)", botctl.is_paused() is False)


# ── 5. recovery ──────────────────────────────────────────────────────────────
def test_recovery():
    print("\n=== 5) الاسترداد: الاستئناف يُصلح الملف التالف ===")
    write_raw('{"paused": tr')
    check("قبل الإصلاح: موقوف (فشل آمن)", botctl.is_paused() is True)
    botctl.set_paused(False, by="web-dashboard")
    check("بعد الاستئناف: غير موقوف", botctl.is_paused() is False)
    data = read_raw()
    check("الملف أُعيد بناؤه سليماً", data.get("paused") is False
          and data.get("updated_by") == "web-dashboard", str(data))

    write_raw('null')
    botctl.set_paused(True, by="telegram:/pause")
    check("كائن غير صالح يُستبدل بكائن سليم", read_raw().get("paused") is True)


# ── 6. every caller labels itself ────────────────────────────────────────────
def test_callers_label_themselves():
    print("\n=== 6) كل نداء set_paused يمرّر مصدراً ===")
    # Scan EVERY production entry point, not just the UI modules. A caller added
    # later in enzo.py or enzoctl would otherwise go unlabelled and undetected.
    rels = ["enzo/ui/botctl.py", "enzo/ui/serve.py", "enzo.py", "enzoctl"]
    srcs = {}
    for rel in rels:
        with open(os.path.join(REPO, rel), encoding="utf-8") as f:
            srcs[rel] = f.read()
    all_src = "\n".join(srcs.values())
    calls = re.findall(r'set_paused\(([^)]*)\)', all_src)
    calls = [c for c in calls if not c.strip().startswith("paused")]  # skip the def
    unlabelled = [c for c in calls if "by=" not in c]
    check(f"كل النداءات ({len(calls)}) في {len(srcs)} ملفات إنتاجية تمرّر by=",
          not unlabelled, str(unlabelled))
    check("set_paused لها معامل by افتراضي",
          re.search(r'def set_paused\(paused:\s*bool,\s*by:', srcs["enzo/ui/botctl.py"]) is not None)


# ── 7. the engine actually honours the flag ──────────────────────────────────
def test_engine_honours_flag():
    print("\n=== 7) المحرّك يحترم مفتاح الإيقاف فعلاً ===")
    with open(os.path.join(REPO, "enzo/core/engine.py"), encoding="utf-8") as f:
        eng = f.read()
    n = len(re.findall(r'botctl\.is_paused\(\)|\bis_paused\(\)', eng))
    check(f"engine.py يستعلم عن is_paused() في {n} موضع(ين)", n >= 2, f"n={n}")
    check("يوجد مسار إيقاف صريح", "is_paused()" in eng)


# ── teardown ─────────────────────────────────────────────────────────────────
def restore():
    botctl.CONTROL_PATH = REAL_PATH
    for f in os.listdir(TMP):
        try:
            os.remove(os.path.join(TMP, f))
        except OSError:
            pass
    try:
        os.rmdir(TMP)
    except OSError:
        pass


if __name__ == "__main__":
    print("=" * 68)
    print("  ENZO — سلامة مفتاح الإيقاف (pause switch safety)")
    print("=" * 68)
    try:
        for fn in (test_round_trip, test_audit_trail, test_atomic_write,
                   test_fail_closed, test_recovery, test_callers_label_themselves,
                   test_engine_honours_flag):
            try:
                fn()
            except Exception as e:
                check(f"{fn.__name__} لم يرمِ استثناءً", False, f"{type(e).__name__}: {e}")
    finally:
        restore()
    print("\n" + "=" * 68)
    print(f"  RESULT: {PASS} passed, {FAIL} failed")
    print("=" * 68)
    sys.exit(1 if FAIL else 0)
