#!/usr/bin/env python3
"""Guard: the test suite must never write into the live workspace.

What this pins down
-------------------
`enzo.core.config` resolves every state path at import time — WORKSPACE_ROOT,
DATA_DIR, PORTFOLIO_DB_PATH, the capital cache in data/run, the log file. So a
suite that imports `enzo` first and points ENZO_HOME somewhere else afterwards
still writes into whatever workspace it was launched from.

Eight of the fifteen suites did exactly that. Running the tests therefore created
or modified, in the real data directory:

    enzo.db                     the ledger
    logs/enzo.log               the bot log
    enzo-trade-gate.json        the gate's persisted state
    run/enzo-capital.json       the capital snapshot  <-- the dangerous one
    enzo-dashboard.html         the generated page

`run/enzo-capital.json` is not litter, it is a money-path hazard: when the real
wallet read fails, LIVE sizing trusts the cached snapshot for
`execution.capital_sync_grace_sec` (300s), and `enzoctl doctor` reports it as a
recent reading. A test run in a fresh deployment — the natural thing to do after
transferring the bot — could leave the mock wallet's $559.40 there for the engine
to size real trades against.

Two kinds of check, because a static one alone would not have caught this:

  1. STATIC — every suite establishes an isolated ENZO_HOME (or calls
     conftest_paths.isolate_home) BEFORE its first `enzo` import, and
     isolate_home purges already-imported enzo modules and does not copy the
     secrets file into /tmp.
  2. LIVE — the two suites that used to write the ledger and the capital cache
     are run for real, and the sensitive files in the repository's data
     directory must come out untouched (existence + mtime). The generated
     dashboard HTML is deliberately excluded from the comparison: the UI server
     rewrites it, so including it would make this guard flaky.

Run: python3 tests/test_suite_isolation.py   (no ENZO_HOME needed)
"""
import os
import re
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
if REPO not in sys.path:
    sys.path.insert(0, REPO)
sys.path.insert(0, HERE)

PASS = FAIL = 0


def check(label, cond, extra=""):
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


IMPORT_RE = re.compile(r"^\s*(?:from enzo[\.\s]|import enzo[\.\s]|import enzo$)")
SANDBOX_RE = re.compile(r"isolate_home\(|os\.environ\[.ENZO_HOME.\]\s*=")


def first_line(lines, rx, skip_docstring=True):
    """Line number (1-based) of the first match, ignoring the module docstring."""
    in_doc = False
    for i, ln in enumerate(lines, 1):
        st = ln.strip()
        if skip_docstring:
            if not in_doc and (st.startswith('"""') or st.startswith("'''")):
                q = st[:3]
                if st.count(q) >= 2 and len(st) > 3:
                    continue                     # one-line docstring
                in_doc = True
                continue
            if in_doc:
                if st.endswith('"""') or st.endswith("'''"):
                    in_doc = False
                continue
        if st.startswith("#"):
            continue
        if rx.search(ln):
            return i
    return None


# ── 1. STATIC: sandbox before import, in every suite ─────────────────────────
section("1. كل حزمة تعزل ENZO_HOME قبل استيراد enzo")

suites = sorted(f for f in os.listdir(HERE)
                if f.startswith("test_") and f.endswith(".py")
                and f != os.path.basename(__file__))
check("وُجدت حزم الاختبارات", len(suites) >= 15, f"{len(suites)} حزمة")

offenders = []
for name in suites:
    src = open(os.path.join(HERE, name), encoding="utf-8").read().splitlines()
    imp = first_line(src, IMPORT_RE)
    box = first_line(src, SANDBOX_RE)
    if imp is None:
        continue                                  # never imports enzo: nothing to leak
    if box is None or box > imp:
        offenders.append(f"{name} (import سطر {imp}, عزل سطر {box})")
check("لا حزمة تستورد enzo قبل عزل مساحة العمل", not offenders,
      "; ".join(offenders) if offenders else f"{len(suites)} حزمة سليمة")

helper = open(os.path.join(HERE, "conftest_paths.py"), encoding="utf-8").read()
check("المساعد المشترك يوفّر isolate_home", "def isolate_home(" in helper)
body = helper.split("def isolate_home(", 1)[-1]
check("ويعزل قبل الاستيراد بحذف وحدات enzo المحمّلة",
      'del sys.modules[mod]' in body and 'm.startswith("enzo.")' in body)
check("ويضبط ENZO_HOME نفسه", 'os.environ["ENZO_HOME"] = home' in body)
check("ولا ينسخ ملف الأسرار إلى /tmp", "enzo-secrets.json" not in body)
check("وينشئ مجلدات data/logs و data/run",
      'os.path.join("data", "logs")' in body and 'os.path.join("data", "run")' in body)

# ── 2. LIVE: running the old offenders must not touch the real data dir ──────
section("2. تشغيل الحزم المسرِّبة سابقاً لا يلمس workspace الحقيقي")

# Files the leak used to create/modify. The dashboard HTML is excluded on
# purpose: enzo.ui.serve rewrites it continuously, so comparing it would make
# this guard flaky without adding signal.
WATCHED = ["enzo.db", "enzo-trade-gate.json",
           os.path.join("logs", "enzo.log"),
           os.path.join("run", "enzo-capital.json")]


def snapshot(data_dir):
    out = {}
    for rel in WATCHED:
        p = os.path.join(data_dir, rel)
        out[rel] = (os.path.exists(p),
                    os.path.getmtime(p) if os.path.exists(p) else None,
                    os.path.getsize(p) if os.path.exists(p) else None)
    return out


def engine_running():
    """True if the bot itself is live - then data/ changes for legitimate reasons
    and comparing it would make this guard flaky instead of useful."""
    pid_path = os.path.join(REPO, "data", "run", "enzo.pid")
    if not os.path.exists(pid_path):
        return False
    try:
        pid = int(open(pid_path, encoding="utf-8").read().strip() or 0)
        os.kill(pid, 0)
        return True
    except Exception:
        return False


# Run against the repository's own data directory - the worst case, and exactly
# what happened before: no ENZO_HOME, launched from the checkout.
real_data = os.path.join(REPO, "data")
live = engine_running()
if live:
    print("  \033[33mSKIP\033[0m  المحرك يعمل الآن: data/ يتغيّر لأسباب مشروعة، "
          "فالمقارنة الحيّة تُتخطّى (الفحص الساكن أعلاه يبقى سارياً)")
before = snapshot(real_data)

for name in ("test_rug_gate.py", "test_base_token_capital.py"):
    env = dict(os.environ)
    env.pop("ENZO_HOME", None)                    # deliberately unset
    proc = subprocess.run([sys.executable, os.path.join(HERE, name)],
                          cwd=REPO, env=env, capture_output=True, text=True,
                          timeout=600)
    tail = [l for l in (proc.stdout or "").splitlines() if "RESULT:" in l]
    check(f"{name} تعمل وتنجح بلا ENZO_HOME", proc.returncode == 0 and "0 failed" in (tail[-1] if tail else ""),
          (tail[-1].strip() if tail else (proc.stderr or "")[-160:]))

after = snapshot(real_data)
changed = [k for k in WATCHED if before[k] != after[k]]
if live:
    print("  \033[33mSKIP\033[0m  مقارنة data/ الحقيقي (المحرك يعمل)")
else:
    check("لم يُكتب شيء في data/ الحقيقي (الدفتر، الذاكرة، السجل، البوابة)",
          not changed,
          ("تغيّر: " + ", ".join(f"{k} {before[k]}→{after[k]}" for k in changed)) if changed
          else "بلا تغيير")

# Prove the redirection mechanism itself works: inside a fresh interpreter,
# isolate_home() must move DATA_DIR off the repository and a state file written
# through it must land in the sandbox. (A suite cannot be used for this proof -
# it isolates itself and therefore ignores an inherited ENZO_HOME, which is the
# intended behaviour.)
probe = (
    "import os, sys;"
    "sys.path.insert(0, %r);"
    "from conftest_paths import isolate_home;"
    "home = isolate_home(prefix='enzo-isoproof-');"
    "from enzo.core import config as C;"
    "p = os.path.join(C.DATA_DIR, 'enzo-trade-gate.json');"
    "open(p, 'w', encoding='utf-8').write('{}');"
    "print('HOME=' + home);"
    "print('DATA=' + C.DATA_DIR);"
    "print('EXISTS=' + str(os.path.exists(p)))"
) % HERE
proc = subprocess.run([sys.executable, "-c", probe], cwd=REPO,
                      capture_output=True, text=True, timeout=300)
vals = dict(l.split("=", 1) for l in proc.stdout.splitlines() if "=" in l)
home, data_dir = vals.get("HOME", ""), vals.get("DATA", "")
check("وآلية العزل تحوّل المسارات فعلاً: الحالة تُكتب داخل الصندوق لا في المستودع",
      proc.returncode == 0 and bool(home) and data_dir.startswith(home)
      and data_dir != real_data and vals.get("EXISTS") == "True",
      f"DATA_DIR={data_dir or (proc.stderr or '')[-140:]}")

print("\n" + "=" * 68)
print(f"  RESULT: {PASS} passed, {FAIL} failed")
print("=" * 68)
sys.exit(1 if FAIL else 0)
