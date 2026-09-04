#!/usr/bin/env python3
"""Regression guard: a developer who dumped is a VETO, not one vote among six.

Why this file exists
--------------------
On 2026-09-04 the bot bought CAT while dev.py had already flagged
DEV_SOLD_ALL with a dev-behaviour score of 0. Nothing stopped the trade: the dev
signal only entered the weighted confidence as one of six axes, five healthy
axes outvoted it (56.15 vs the 55 threshold), and the security score - which the
BUY gate also consults - measures mint/freeze authority and liquidity only,
never whether the dev still holds anything. The position then rode the dump
down until the daily-loss breaker halted the bot at -26.55%.

analyze.rug_rejection() now turns that signal into a rejection reason, and the
decision gate appends it to `rejected` BEFORE hard_fail is evaluated, so the
token becomes IGNORE with a reason that names the rug.

Second guard: pump.dev rate-limits per IP, so only the engine may own the
WebSocket. serve.py once called get_pumpdev_client() (which CREATES and starts
the client) while building /health, opening a second connection from the same
IP; the IP got limited and the engine's feed - and with it every live price -
starved. serve.py must peek, never create.
"""
import os
import re
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO not in sys.path:
    sys.path.insert(0, REPO)

from enzo.analyzers import analyze  # noqa: E402

PASS = FAIL = 0


def check(name, ok, detail=""):
    global PASS, FAIL
    if ok:
        PASS += 1
        print(f"  ✔ {name}")
    else:
        FAIL += 1
        print(f"  ✖ {name}" + (f" — {detail}" if detail else ""))


print("\n=== 1) المطور باع كل شيء = رفض قاطع ===")
r = analyze.rug_rejection({"available": True, "score": 0,
                           "events": ["DEV_SOLD_ALL", "DEV_FACTORY(40_created)"]})
check("DEV_SOLD_ALL يُنتج سبب رفض", isinstance(r, str) and r.startswith("RUG"), str(r))
check("السبب يسمّي الحدث", r is not None and "DEV_SOLD_ALL" in r, str(r))

print("\n=== 2) درجة صفر مع أحداث سلبية = رفض ===")
r = analyze.rug_rejection({"available": True, "score": 0,
                           "events": ["DEV_SELLING", "DEV_DISTRIBUTING"]})
check("score=0 مع أحداث يُرفض", isinstance(r, str) and r.startswith("RUG"), str(r))

print("\n=== 3) مطور محتفظ = لا رفض ===")
for label, ax in (("DEV_HOLDING بدرجة جيدة", {"available": True, "score": 60, "events": ["DEV_HOLDING"]}),
                  ("يشتري المزيد", {"available": True, "score": 75, "events": ["DEV_BUYING_MORE"]}),
                  ("درجة موجبة مع مصنع", {"available": True, "score": 20, "events": ["DEV_FACTORY(12_created)"]}),
                  ("بلا أحداث إطلاقاً", {"available": True, "score": 0, "events": []})):
    r = analyze.rug_rejection(ax)
    check(f"{label} -> لا رفض", r is None, str(r))

print("\n=== 4) محور غير متاح = لا حكم (لا نختلق رفضاً) ===")
r = analyze.rug_rejection({"available": False, "score": 0, "events": ["DEV_SOLD_ALL"]})
check("غير متاح -> None", r is None, str(r))
r = analyze.rug_rejection({})
check("قاموس فارغ -> None", r is None, str(r))

print("\n=== 5) البوابة موصولة فعلاً بمسار القرار ===")
src = open(os.path.join(REPO, "enzo", "analyzers", "analyze.py"), encoding="utf-8").read()
gate = src[src.index("rug_rejection(dev_axis)"):]
check("النتيجة تُضاف إلى rejected قبل hard_fail",
      gate.index("rejected.append(_rug)") < gate.index("hard_fail = bool(rejected)"))

print("\n=== 6) اللوحة لا تفتح تغذية PumpDev أبداً ===")
serve = open(os.path.join(REPO, "enzo", "ui", "serve.py"), encoding="utf-8").read()
# Ignore comments: the fix is documented by naming the forbidden call, and a
# raw substring test would flag its own explanation.
serve_code = "\n".join(
    ln.split("#", 1)[0] if not ln.lstrip().startswith("#") else ""
    for ln in serve.splitlines())
check("serve.py لا يستدعي get_pumpdev_client()",
      "get_pumpdev_client()" not in serve_code)
check("serve.py يستخدم peek_pumpdev_client()", "peek_pumpdev_client()" in serve_code)
pump_src = open(os.path.join(REPO, "enzo", "providers", "pump.py"), encoding="utf-8").read()
check("peek لا يُنشئ العميل",
      re.search(r"def peek_pumpdev_client.*?return _PUMPDEV_CLIENT", pump_src, re.S) is not None
      and "PumpDevStreamClient()" not in
      re.search(r"def peek_pumpdev_client.*?(?=\ndef |\Z)", pump_src, re.S).group(0))

print("\n=== 7) مراقب الخروج يسجّل مصدر السعر ===")
em = open(os.path.join(REPO, "enzo", "execution", "exit_monitor.py"), encoding="utf-8").read()
check("يسجّل price_source", '"price_source"' in em)
check("يسجّل price_ts", '"price_ts"' in em)
serve = open(os.path.join(REPO, "enzo", "ui", "serve.py"), encoding="utf-8").read()
check("اللوحة تكشف price_is_live", '"price_is_live"' in serve)

print("\n=== 8) أمر rebase لا يغيّر شيئاً بلا --confirm ===")
import json as _json
import subprocess as _sp
from enzo.core import db as _db
_before = _db.get_full_state().get("initial_capital")
_r = _sp.run([sys.executable, os.path.join(REPO, "enzoctl"), "rebase"],
             capture_output=True, text=True, timeout=120, cwd=REPO)
_out = _r.stdout + _r.stderr
check("بدون --confirm يخرج برمز 1 (لم يُطبَّق)", _r.returncode == 1, str(_r.returncode))
check("يطلب --confirm صراحة", "--confirm" in _out, _out[-120:])
_after = _db.get_full_state().get("initial_capital")
check("قاعدة رأس المال لم تتغير", abs(float(_after or 0) - float(_before or 0)) < 1e-9,
      f"{_before} -> {_after}")

print("\n" + "=" * 68)
print(f"  RESULT: {PASS} passed, {FAIL} failed")
print("=" * 68)
sys.exit(1 if FAIL else 0)
