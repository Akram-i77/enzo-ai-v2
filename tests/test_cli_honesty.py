#!/usr/bin/env python3
"""The operator's own commands must work — and must not lie when they cannot.

Two of them shipped broken because nothing exercised them end to end:

* `./enzoctl wallet` called `executor.get_balance_snapshot()`, a function that
  has NEVER existed, so the command raised AttributeError on every single run.
  Nothing caught it because no test ever ran the command, and because ruff/pyflakes
  catch undefined NAMES (F821) but not undefined ATTRIBUTES. Section 1 below pins
  the whole class: every `module.attribute` access in the codebase is resolved
  against the module that was actually imported.
* `./enzoctl logs` read its lines through `enzo.core.audit._tail_lines()`, whose
  contract is "parsed audit ROWS": it json-decodes each line and silently drops
  everything that is not JSON. So `logs audit` crashed
  (`'dict' object has no attribute 'rstrip'`) on any non-empty audit log, and
  `logs enzo|supervisor` printed nothing at all — including the traceback the
  owner had come to read.

Also pinned: one malformed audit row must not blank the whole Activity feed
(it used to answer HTTP 500), and a broken config.yaml must be reported as
"config unreadable, here is the file and the fix" (503 + reason) rather than as
an anonymous server crash (500) or, worse, as an error with no reason at all.

Run:  python3 tests/test_cli_honesty.py
"""
import ast
import importlib
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

PASS = FAIL = 0


def ok(cond, label, extra=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  \033[32mPASS\033[0m  {label}" + (f"   {extra}" if extra != "" else ""))
    else:
        FAIL += 1
        print(f"  \033[31mFAIL\033[0m  {label}" + (f"   {extra}" if extra != "" else ""))
    return bool(cond)


def section(t):
    print(f"\n=== {t} ===")


from conftest_paths import install_mock_on_path  # noqa: E402

MOCKBIN = install_mock_on_path()
PY = sys.executable
CTL = os.path.join(ROOT, "enzoctl")
_HOMES = []


def _env(home=None, extra=None):
    env = dict(os.environ)
    env.update({
        "PATH": (MOCKBIN or "") + os.pathsep + env.get("PATH", ""),
        "GMGN_API_KEY": "test-key",
        "GMGN_MOCK_STATE": "{}",
        "MOCK_STATE": "{}",
        "NO_COLOR": "1",
        "PYTHONDONTWRITEBYTECODE": "1",
        "PYTHONUNBUFFERED": "1",
    })
    if home:
        env["ENZO_HOME"] = home
    if extra:
        env.update(extra)
    return env


def make_home(tag, config_text=None):
    home = tempfile.mkdtemp(prefix="enzo-cli-%s-" % tag)
    _HOMES.append(home)
    os.makedirs(os.path.join(home, "config"), exist_ok=True)
    os.makedirs(os.path.join(home, "data", "logs"), exist_ok=True)
    dst = os.path.join(home, "config", "enzo-config.yaml")
    if config_text is None:
        shutil.copy(os.path.join(ROOT, "config", "enzo-config.yaml"), dst)
    else:
        with open(dst, "w", encoding="utf-8") as f:
            f.write(config_text)
    return home


def ctl(home, *args, timeout=120):
    p = subprocess.run([PY, CTL, *args], capture_output=True, text=True,
                       timeout=timeout, env=_env(home), cwd=ROOT)
    return p.returncode, (p.stdout or "") + (p.stderr or ""), (p.stdout or "")


def pyin(home, code, timeout=120):
    p = subprocess.run([PY, "-c", code], capture_output=True, text=True,
                       timeout=timeout, env=_env(home), cwd=ROOT)
    return p.returncode, (p.stdout or ""), (p.stderr or "")


def json_of(text):
    try:
        return json.loads(text)
    except Exception:
        dec = json.JSONDecoder()
        for idx, ch in enumerate(text or ""):
            if ch != "{":
                continue
            try:
                obj, _ = dec.raw_decode(text[idx:])
                return obj
            except Exception:
                continue
    return None


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = int(s.getsockname()[1])
    s.close()
    return p


# ═══════════════════════════════════════════════════════════════════════════
print("═" * 78)
print("ENZO CLI honesty — the owner's commands must work, and say so truthfully")
print("═" * 78)

try:
    # ── 1. the whole bug class: module.attribute that does not exist ─────────
    section("1. no `module.attribute` landmines anywhere in the codebase")
    guard_home = make_home("attrguard")
    guard_code = r'''
import ast, importlib, json, os, sys
ROOT = sys.argv[1]
sys.path.insert(0, ROOT)
files = []
for base, dirs, names in os.walk(os.path.join(ROOT, "enzo")):
    dirs[:] = [d for d in dirs if d != "__pycache__"]
    files += [os.path.join(base, n) for n in names if n.endswith(".py")]
files += [os.path.join(ROOT, "enzo.py"), os.path.join(ROOT, "enzoctl")]
cache = {}
def mod(name):
    if name not in cache:
        try:
            cache[name] = importlib.import_module(name)
        except Exception:
            cache[name] = None
    return cache[name]
missing, checked = [], 0
for path in files:
    try:
        src = open(path, encoding="utf-8").read()
        tree = ast.parse(src)
    except Exception as e:
        missing.append("%s: cannot parse (%s)" % (path, e))
        continue
    aliases = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for a in node.names:
                if a.name.startswith("enzo"):
                    aliases[a.asname or a.name.split(".")[0]] = a.name
        elif isinstance(node, ast.ImportFrom):
            if node.module and node.module.startswith("enzo"):
                for a in node.names:
                    if a.name != "*":
                        aliases[a.asname or a.name] = node.module + "." + a.name
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name)):
            continue
        target = aliases.get(node.value.id)
        if not target:
            continue
        m = mod(target)
        if m is None:
            continue                       # a symbol, not a module (or import failed)
        checked += 1
        if not hasattr(m, node.attr):
            missing.append("%s:%s %s.%s -> %s has no such attribute"
                           % (os.path.relpath(path, ROOT), node.lineno,
                              node.value.id, node.attr, target))
print("GUARD" + json.dumps({"files": len(files), "checked": checked, "missing": missing}))
'''
    # the guard needs ROOT as argv[1], so it runs from a file, not from -c
    guard_file = os.path.join(guard_home, "_guard.py")
    with open(guard_file, "w", encoding="utf-8") as f:
        f.write(guard_code)
    p = subprocess.run([PY, guard_file, ROOT], capture_output=True, text=True,
                       timeout=600, env=_env(guard_home), cwd=ROOT)
    m = re.search(r"GUARD(\{.*\})", p.stdout or "", re.S)
    guard = json.loads(m.group(1)) if m else {}
    ok(bool(guard), "the static guard ran over the source tree",
       str({k: guard.get(k) for k in ("files", "checked")}) or (p.stderr or "")[-300:])
    ok(int(guard.get("checked") or 0) > 200,
       "it resolved and checked every enzo module-attribute access",
       "%s accesses in %s files" % (guard.get("checked"), guard.get("files")))
    ok(not guard.get("missing"),
       "NO missing attribute anywhere — this is the bug that killed `wallet`",
       json.dumps(guard.get("missing"))[:400])
    with open(CTL, encoding="utf-8") as f:
        ctl_src = f.read()
    ok("executor.get_balance_snapshot(" not in ctl_src,
       "no call to the ghost function `executor.get_balance_snapshot` remains "
       "(the name may survive only in the comment explaining the fix)")
    ok("get_wallet_snapshot" in ctl_src,
       "...and `wallet` calls executor.get_wallet_snapshot(), which really exists")

    # ── 2. ./enzoctl wallet ─────────────────────────────────────────────────
    section("2. ./enzoctl wallet runs and reports the real wallet")
    hW = make_home("wallet")
    rc, text, out = ctl(hW, "wallet")
    ok(rc == 0, "wallet exits 0 (it used to raise AttributeError every time)", "rc=%s" % rc)
    ok("Traceback" not in text, "no traceback in the output", text[-160:] if "Traceback" in text else "")
    ok("USDC" in text, "the USDC balance is shown")
    ok("SOL" in text, "the SOL balance is shown")
    ok(re.search(r"\$\s?[\d,]+\.\d\d", text) is not None,
       "a dollar figure is printed", [l.strip() for l in text.splitlines() if "$" in l][:2])
    rc2, text2, out2 = ctl(hW, "wallet", "--json")
    pl = json_of(out2) or {}
    ok(rc2 == 0 and pl.get("ok") is True, "wallet --json exits 0 with ok=true", "rc=%s" % rc2)
    ok(isinstance(pl.get("wallet"), str) and bool(pl.get("wallet")),
       "the JSON names the wallet it read", str(pl.get("wallet")))
    rows = [r for r in (pl.get("balances") or []) if isinstance(r, dict)]
    ok(len(rows) >= 2,
       "...and carries one row per token balance", "%d rows" % len(rows))
    syms = [str(r.get("symbol") or "").upper() for r in rows]
    ok("USDC" in syms and "SOL" in syms,
       "...including USDC and SOL", json.dumps(syms))
    ok(isinstance(pl.get("usdc"), (int, float)) and float(pl.get("usdc") or 0) > 0,
       "the USDC amount is a number a supervisor can compare", str(pl.get("usdc")))
    ok(isinstance(pl.get("sol"), (int, float)),
       "and so is the SOL amount", str(pl.get("sol")))
    cap = pl.get("capital") or {}
    ok(isinstance(cap, dict) and cap.get("ok") is True and float(cap.get("usd") or 0) > 0,
       "deployable capital is reported with its source",
       json.dumps({k: cap.get(k) for k in ("ok", "usd", "source")}))
    # an unreadable wallet must be a message, not a traceback
    hW2 = make_home("wallet-nocli")
    p = subprocess.run([PY, CTL, "wallet"], capture_output=True, text=True, timeout=120,
                       env=_env(hW2, extra={"PATH": os.path.dirname(PY), "MOCK_STATE": "{}"}),
                       cwd=ROOT)
    text3 = (p.stdout or "") + (p.stderr or "")
    ok("Traceback" not in text3,
       "with no MoonPay CLI on PATH, wallet still does not dump a traceback",
       text3[-200:] if "Traceback" in text3 else text3.strip().splitlines()[-1:] )

    # ── 3. ./enzoctl logs ───────────────────────────────────────────────────
    section("3. ./enzoctl logs shows what the process actually wrote")
    hL = make_home("logs")
    log_dir = os.path.join(hL, "data", "logs")
    text_log = os.path.join(log_dir, "enzo.log")
    with open(text_log, "w", encoding="utf-8") as f:
        f.write("\n".join([
            "2026-09-06 10:00:01 INFO enzo.engine scan cycle start",
            "2026-09-06 10:00:02 WARNING enzo.providers.gmgn 429 from gmgn, backing off",
            "2026-09-06 10:00:03 ERROR enzo.execution.executor buy failed",
            "Traceback (most recent call last):",
            '  File "enzo/execution/executor.py", line 210, in _buy',
            "    out = cli.run(args)",
            "AttributeError: module has no attribute 'run'",
            "2026-09-06 10:00:04 INFO enzo.engine cycle done, 0 candidates",
        ]) + "\n")
    rc, text, out = ctl(hL, "logs")
    ok(rc == 0, "logs exits 0 on a plain text log", "rc=%s" % rc)
    ok("scan cycle start" in text and "cycle done" in text,
       "every line of the text log is shown (they are not JSON, and used to be dropped)")
    ok("Traceback (most recent call last):" in text,
       "the traceback header survives — this is the line the owner came for")
    ok("AttributeError: module has no attribute 'run'" in text,
       "...and so does the exception line itself")
    ok('  File "enzo/execution/executor.py", line 210, in _buy' in text,
       "...and the indented stack frame, whitespace intact")
    rc, text, out = ctl(hL, "logs", "-n", "3")
    lines = [l for l in out.splitlines() if l.strip()]
    ok(len(lines) == 3 and lines[-1].endswith("0 candidates"),
       "-n 3 tails the LAST three lines", str(len(lines)) + " " + (lines[-1:][0][:40] if lines else ""))
    rc, text, out = ctl(hL, "logs", "--json")
    pl = json_of(out) or {}
    ok(pl.get("path") == text_log and isinstance(pl.get("lines"), list)
       and len(pl.get("lines") or []) == 8,
       "--json returns the file path and the raw lines", str(len(pl.get("lines") or [])))

    # audit log: pretty rows, and a corrupt row shown instead of dropped
    audit_path = os.path.join(hL, "data", "enzo-audit.jsonl")
    with open(audit_path, "w", encoding="utf-8") as f:
        f.write(json.dumps({"ts": "2026-09-06T10:00:05.100000+00:00", "category": "SYSTEM",
                            "level": "INFO", "message": "Trading RESUMED via Web Dashboard"}) + "\n")
        f.write(json.dumps({"ts": "2026-09-06T10:00:06.100000+00:00", "category": "ENGINE",
                            "level": "ERROR", "message": "Manual scan failed: EnzoConfigError"}) + "\n")
        f.write('{"ts": "2026-09-06T10:00:07.100000+00:00", "category": "TRUNC\n')
    rc, text, out = ctl(hL, "logs", "audit")
    ok(rc == 0, "logs audit exits 0 (it used to raise AttributeError on any non-empty log)",
       "rc=%s" % rc)
    ok("Traceback" not in text, "no traceback", text[-200:] if "Traceback" in text else "")
    ok("Trading RESUMED" in text and "Manual scan failed" in text,
       "both real audit rows are shown, formatted")
    ok("ERROR" in text, "the level column is rendered")
    ok('"category": "TRUNC' in text,
       "the half-written row is shown RAW rather than silently dropped")
    rc, text, out = ctl(hL, "logs", "audit", "--json")
    pl = json_of(out) or {}
    ok(rc == 0 and len(pl.get("lines") or []) == 3,
       "logs audit --json returns all three raw lines", str(len(pl.get("lines") or [])))

    # empty and missing logs must speak
    hE = make_home("emptylog")
    open(os.path.join(hE, "data", "logs", "enzo.log"), "w").close()
    rc, text, out = ctl(hE, "logs")
    ok(rc == 0 and "exists but has no lines yet" in text,
       "an EMPTY log says so — printing nothing at all reads as 'the command never ran'",
       text.strip()[:80])
    rc, text, out = ctl(hE, "logs", "supervisor")
    ok(rc == 1 and "no log at" in text,
       "a MISSING log says which file is missing and exits non-zero", text.strip()[:80])

    # ── 4. one bad audit row must not blank the Activity feed ────────────────
    section("4. audit.get_recent_activities survives damaged rows")
    hA = make_home("auditfeed")
    with open(os.path.join(hA, "data", "enzo-audit.jsonl"), "w", encoding="utf-8") as f:
        f.write(json.dumps({"ts": time.time(), "symbol": "FLOATTS", "decision": "WAIT",
                            "confidence": 44, "axes": {"momentum": 44}}) + "\n")
        f.write(json.dumps({"ts": "2026-09-06T10:00:09+00:00", "symbol": "BADCONF",
                            "decision": "BUY", "confidence": "not-a-number"}) + "\n")
        f.write(json.dumps([1, 2, 3]) + "\n")
        f.write(json.dumps({"ts": "2026-09-06T10:00:10+00:00", "symbol": "ODD",
                            "decision": "IGNORE", "confidence": 12,
                            "rejected_signals": "MCAP_UNKNOWN"}) + "\n")
        f.write(json.dumps({"ts": "2026-09-06T10:00:11+00:00", "symbol": "GOOD",
                            "decision": "BUY", "confidence": 81,
                            "momentum_windows": {"1m": 2.5, "5m": 6.0, "scored": ["1m", "5m"],
                                                 "1h_context": None, "24h_context": None}}) + "\n")
    feed_code = (
        "import json;from enzo.core import audit;"
        "acts = audit.get_recent_activities(limit=50);"
        "print('FEED' + json.dumps(acts))"
    )
    rc, out, err = pyin(hA, feed_code)
    m = re.search(r"FEED(\[.*\])", out or "", re.S)
    feed = json.loads(m.group(1)) if m else None
    ok(rc == 0 and feed is not None,
       "the feed builds without raising (it used to answer HTTP 500 on a float ts)",
       (err or "")[-200:] if feed is None else "%d items" % len(feed))
    ok(bool(feed) and len(feed) == 5, "all five rows are represented", str(len(feed or [])))
    msgs = " ".join(str(a.get("message") or "") for a in (feed or []))
    ok("unreadable audit row skipped" in msgs,
       "the JSON-array row is reported as skipped, not hidden", msgs[:120])
    good = [a for a in (feed or []) if "GOOD" in str(a.get("message"))]
    ok(bool(good) and good[0].get("time_str") == "10:00:11",
       "a normal ISO timestamp still renders as HH:MM:SS", str(good[0].get("time_str")) if good else "-")
    ok(bool(good) and (good[0].get("data") or {}).get("momentum_windows", {}).get("scored") == ["1m", "5m"],
       "...and the momentum windows survive into the feed item")
    badconf = [a for a in (feed or []) if "BADCONF" in str(a.get("message"))]
    ok(bool(badconf) and "conf=0" in str(badconf[0].get("message")),
       "a non-numeric confidence degrades to 0 instead of raising",
       str(badconf[0].get("message")) if badconf else "-")

    # ── 5. the HTTP failure contract ────────────────────────────────────────
    section("5. a broken config is reported as a broken config (503 + reason)")
    hB = make_home("brokencfg", config_text="dashboard: [unclosed\n\n")
    fp_code = ("import json;from enzo.ui import serve;"
               "c, b = serve._failure_payload(RuntimeError('boom'));"
               "print('FP' + json.dumps([c, b]))")
    rc, out, err = pyin(hB, fp_code)
    m = re.search(r"FP(\[.*\])", out or "", re.S)
    fp = json.loads(m.group(1)) if m else None
    ok(bool(fp) and fp[0] == 503, "the API answers 503, not 500, when the CONFIG is the cause",
       str(fp[0]) if fp else (err or "")[-200:])
    ok(bool(fp) and fp[1].get("reason") == "CONFIG_UNREADABLE",
       "...with reason=CONFIG_UNREADABLE", str(fp[1].get("reason")) if fp else "-")
    ok(bool(fp) and "enzo-config.yaml" in str(fp[1].get("message")),
       "...naming the file that is broken", str(fp[1].get("message"))[:90] if fp else "-")
    ok(bool(fp) and "doctor" in str(fp[1].get("fix")),
       "...and the command that diagnoses it", str(fp[1].get("fix")) if fp else "-")
    hG = make_home("goodcfg")
    rc, out, err = pyin(hG, fp_code)
    m = re.search(r"FP(\[.*\])", out or "", re.S)
    fp2 = json.loads(m.group(1)) if m else None
    ok(bool(fp2) and fp2[0] == 500 and fp2[1].get("message") == "boom",
       "an unrelated failure is still a plain 500 with the original message",
       json.dumps(fp2)[:110] if fp2 else (err or "")[-200:])

    hs_code = ("import json;from enzo.ui import serve;"
               "print('HS' + json.dumps(serve.health_snapshot()))")
    rc, out, err = pyin(hB, hs_code)
    m = re.search(r"HS(\{.*\})", out or "", re.S)
    hs = json.loads(m.group(1)) if m else {}
    ok(hs.get("status") == "error", "/health on a broken config reports status=error")
    ok(any(str(x).startswith("CONFIG_UNREADABLE") for x in (hs.get("problems") or [])),
       "...and problems[] NAMES the reason — an empty list here is what made the "
       "failure undiagnosable", json.dumps(hs.get("problems"))[:120])
    # corrupt the database -> the state branch must be distinguishable
    hS = make_home("badstate")
    with open(os.path.join(hS, "data", "enzo.db"), "wb") as f:
        f.write(b"this is not a sqlite database at all" * 40)
    rc, out, err = pyin(hS, hs_code)
    m = re.search(r"HS(\{.*\})", out or "", re.S)
    hs2 = json.loads(m.group(1)) if m else {}
    ok(hs2.get("status") == "error" and
       any(str(x).startswith("STATE_UNREADABLE") for x in (hs2.get("problems") or [])),
       "a corrupt database is reported as STATE_UNREADABLE — a different fault, "
       "named differently", json.dumps(hs2.get("problems"))[:120])

    section("6. /api/activity keeps serving when the audit log is damaged")
    hP = make_home("apiact")
    port = free_port()
    with open(os.path.join(hP, "data", "enzo-audit.jsonl"), "w", encoding="utf-8") as f:
        f.write(json.dumps({"ts": time.time(), "symbol": "FLOATTS", "decision": "WAIT",
                            "confidence": 44}) + "\n")
        f.write(json.dumps([9, 9]) + "\n")
        f.write('{"ts": "half a row\n')
        f.write(json.dumps({"ts": "2026-09-06T10:00:12+00:00", "category": "SYSTEM",
                            "level": "INFO", "message": "boot"}) + "\n")
    srv = subprocess.Popen([PY, "-c",
                            "from enzo.ui import serve; serve.run_server('127.0.0.1', %d)" % port],
                           env=_env(hP), cwd=ROOT,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        up = False
        for _ in range(80):
            if srv.poll() is not None:
                break
            try:
                with socket.create_connection(("127.0.0.1", port), 0.4):
                    up = True
                    break
            except OSError:
                time.sleep(0.25)
        ok(up, "the dashboard server comes up on a sandbox home with a damaged audit log")
        code, body = None, ""
        try:
            with urllib.request.urlopen("http://127.0.0.1:%d/api/activity?limit=20" % port,
                                        timeout=20) as r:
                code, body = int(r.status), r.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as e:
            code, body = int(e.code), e.read().decode("utf-8", "replace")
        except Exception as e:
            body = str(e)
        ok(code == 200, "/api/activity answers 200 (it used to answer 500 for one bad row)",
           "HTTP %s %s" % (code, body[:120]))
        act = json_of(body) or {}
        ok(isinstance(act.get("activities"), list) and len(act.get("activities")) >= 2,
           "...and the feed still carries the readable rows",
           str(len(act.get("activities") or [])))
        ok("Traceback" not in body, "no traceback is served to the browser")
        # and the same home with a broken config answers 503 + reason, not 500
        with open(os.path.join(hP, "config", "enzo-config.yaml"), "w", encoding="utf-8") as f:
            f.write("dashboard: [unclosed\n")
        time.sleep(0.5)
        try:
            with urllib.request.urlopen("http://127.0.0.1:%d/api/state" % port, timeout=20) as r:
                code, body = int(r.status), r.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as e:
            code, body = int(e.code), e.read().decode("utf-8", "replace")
        except Exception as e:
            body = str(e)
        ok(code == 503, "/api/state answers 503 when the config is broken", "HTTP %s" % code)
        ok('"CONFIG_UNREADABLE"' in body, "...with reason=CONFIG_UNREADABLE in the body",
           body[:110])
    finally:
        try:
            srv.terminate()
            srv.wait(timeout=10)
        except Exception:
            try:
                srv.kill()
            except Exception:
                pass

finally:
    for h in list(_HOMES):
        try:
            subprocess.run([PY, CTL, "stop"], capture_output=True, text=True,
                           timeout=45, env=_env(h), cwd=ROOT)
        except Exception:
            pass
        shutil.rmtree(h, ignore_errors=True)

# ─────────────────────────────────────────────────────────────────────────────
print("\n" + "─" * 78)
print(f"CLI honesty: {PASS} passed, {FAIL} failed")
sys.exit(1 if FAIL else 0)
