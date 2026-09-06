#!/usr/bin/env python3
"""Every button on the dashboard must DO something — proved in a real DOM.

tests/test_dashboard_js.py proves the emitted JavaScript PARSES, and
tests/dashboard_browser_test.js goes further: it loads the REAL generated HTML
into jsdom, executes the REAL scripts against a REAL running server, and CLICKS
EVERY BUTTON — tabs, filters, refresh, pause/resume, manual scan — asserting that
each handler exists, that clicking it issues the expected HTTP call, that the
visible pane/filter really changes, and that no uncaught error is raised.

That harness existed but nothing ran it: it needs `node tests/... <baseUrl>
<htmlPath>` plus a seeded workspace, so it was only ever invoked by hand and had
started to fail silently (11 of its 45 checks were red against an unseeded home).
This wrapper builds that workspace through the real APIs, starts a server, runs
the harness, and folds its RESULT line into the suite total — so a dead button is
a failed test from now on.

When node or jsdom is unavailable the wrapper says so loudly and contributes
0/0 rather than pretending the buttons were checked.

How this differs from tests/test_dashboard_e2e.py, which also runs the harness:
e2e seeds its workspace through the TRADING path (db.atomic_open_position), this
one seeds through db.save_full_state — the OTHER writer. save_full_state used to
rewrite position rows without extra_json, wiping rug_flags, so the two paths did
not agree on what a position carries. Running the same 45 button checks over both
is what pins that down: a flag written by either writer has to reach the 🚩 badge.

Run:  python3 tests/test_dashboard_buttons.py
"""
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
TESTS = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, ROOT)
sys.path.insert(0, TESTS)

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
JS = os.path.join(TESTS, "dashboard_browser_test.js")


def _env(home):
    env = dict(os.environ)
    env.update({
        "PATH": (MOCKBIN or "") + os.pathsep + env.get("PATH", ""),
        "ENZO_HOME": home,
        # the seed script lives in the sandbox, so ROOT has to be importable from
        # anywhere (-c picks up cwd, a script file does not)
        "PYTHONPATH": ROOT + os.pathsep + env.get("PYTHONPATH", ""),
        "GMGN_API_KEY": "test-key",
        "GMGN_MOCK_STATE": "{}",
        "MOCK_STATE": "{}",
        "NO_COLOR": "1",
        "PYTHONDONTWRITEBYTECODE": "1",
        "PYTHONUNBUFFERED": "1",
    })
    return env


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = int(s.getsockname()[1])
    s.close()
    return p


def find_jsdom():
    """First directory that can resolve `require('jsdom')`."""
    cands = [os.environ.get("ENZO_JSDOM_PATH"), os.environ.get("NODE_PATH"),
             os.path.join(ROOT, "node_modules"), "/tmp/jsdom-env/node_modules"]
    for c in cands:
        if c and os.path.isdir(os.path.join(c, "jsdom")):
            return c
    return None


# The workspace the harness was written against is built by a helper script
# (tests/_seed_dashboard_home.py) run as a SUBPROCESS with ENZO_HOME pointed at a
# throwaway sandbox. This wrapper itself never imports enzo in-process, so it can
# never resolve a path against the repository's own data directory - which is
# what tests/test_suite_isolation.py guards against.
SEED_SCRIPT = os.path.join(TESTS, "_seed_dashboard_home.py")


print("═" * 78)
print("ENZO dashboard buttons — clicked in a real DOM against a real server")
print("═" * 78)

node = shutil.which("node")
jsdom = find_jsdom()
home = None
srv = None

try:
    section("0. the harness and its dependencies")
    ok(os.path.exists(JS), "tests/dashboard_browser_test.js is present")
    ok(bool(node), "node is available", str(node or "NOT FOUND"))
    ok(bool(jsdom), "jsdom is resolvable", str(jsdom or "NOT FOUND"))
    if not (node and jsdom and os.path.exists(JS)):
        print("\n  \033[33m⚠ SKIPPED\033[0m  the button harness needs node + jsdom "
              "(set ENZO_JSDOM_PATH to a directory containing jsdom).")
        print("           No button was checked by this run — this is NOT a pass.")
    else:
        # ── build the workspace ───────────────────────────────────────────────
        section("1. a workspace seeded through the real APIs")
        home = tempfile.mkdtemp(prefix="enzo-buttons-")
        os.makedirs(os.path.join(home, "config"), exist_ok=True)
        os.makedirs(os.path.join(home, "data", "logs"), exist_ok=True)
        shutil.copy(os.path.join(ROOT, "config", "enzo-config.yaml"),
                    os.path.join(home, "config", "enzo-config.yaml"))
        ok(os.path.exists(SEED_SCRIPT), "the seeding helper is present", SEED_SCRIPT)
        p = subprocess.run([PY, SEED_SCRIPT], capture_output=True, text=True,
                           timeout=300, env=_env(home), cwd=ROOT)
        ok(p.returncode == 0 and "SEEDED" in (p.stdout or ""),
           "positions, closed trades, capital and audit rows seeded",
           (p.stdout or "").strip().splitlines()[-1:] or (p.stderr or "")[-300:])
        html = os.path.join(home, "data", "enzo-dashboard.html")
        ok(os.path.exists(html) and os.path.getsize(html) > 20000,
           "the dashboard HTML was generated from that state",
           "%d bytes" % (os.path.getsize(html) if os.path.exists(html) else 0))

        # the seeded rug flag must have survived the database round-trip, or the
        # badge the harness looks for could never be drawn
        chk = subprocess.run([PY, "-c",
                              "import json;from enzo.core import db;"
                              "st=db.get_full_state();"
                              "print('FLAGS' + json.dumps([p.get('rug_flags') for p in st['open_positions'].values()]))"],
                             capture_output=True, text=True, timeout=120, env=_env(home), cwd=ROOT)
        m = re.search(r"FLAGS(\[.*\])", chk.stdout or "", re.S)
        flags = json.loads(m.group(1)) if m else []
        ok(any(bool(f) for f in (flags or [])),
           "rug_flags survive the DB round-trip (save_full_state used to null extra_json, "
           "which silently disarmed the Layer-1 early stop)", json.dumps(flags))

        # ── serve it ─────────────────────────────────────────────────────────
        section("2. a live server on a free port")
        port = free_port()
        srv = subprocess.Popen([PY, "-c",
                                "from enzo.ui import serve; serve.run_server('127.0.0.1', %d)" % port],
                               env=_env(home), cwd=ROOT,
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        up = False
        for _ in range(80):
            if srv.poll() is not None:
                break
            try:
                with urllib.request.urlopen("http://127.0.0.1:%d/health" % port, timeout=2) as r:
                    up = r.status in (200, 503)
                    break
            except urllib.error.HTTPError as e:
                up = e.code in (200, 503)
                break
            except Exception:
                time.sleep(0.25)
        ok(up, "the dashboard server answers /health on port %d" % port)

        # ── click everything ─────────────────────────────────────────────────
        section("3. the jsdom harness clicks every button")
        env = _env(home)
        env["NODE_PATH"] = jsdom
        proc = subprocess.run([node, JS, "http://127.0.0.1:%d" % port, html],
                              capture_output=True, text=True, timeout=600, env=env, cwd=ROOT)
        out = (proc.stdout or "") + (proc.stderr or "")
        for line in out.splitlines():
            if line.strip():
                print("   │ " + line.rstrip()[:160])
        m = re.search(r"RESULT:\s*(\d+)\s*passed,\s*(\d+)\s*failed", out)
        if not m:
            ok(False, "the harness printed a RESULT line", out[-400:])
        else:
            jpass, jfail = int(m.group(1)), int(m.group(2))
            ok(jfail == 0,
               "every button-level check passed in the real DOM",
               "%d passed, %d failed" % (jpass, jfail))
            PASS += jpass
            FAIL += jfail
            if jfail:
                print("\n   failing checks:")
                for line in out.splitlines():
                    if "FAIL" in line:
                        print("     " + line.strip()[:160])
        ok(proc.returncode == 0 or bool(m),
           "the harness itself ran to completion", "rc=%s" % proc.returncode)

finally:
    if srv is not None:
        try:
            srv.terminate()
            srv.wait(timeout=10)
        except Exception:
            try:
                srv.kill()
            except Exception:
                pass
    if home:
        try:
            subprocess.run([PY, CTL, "stop"], capture_output=True, text=True,
                           timeout=45, env=_env(home), cwd=ROOT)
        except Exception:
            pass
        shutil.rmtree(home, ignore_errors=True)

# ─────────────────────────────────────────────────────────────────────────────
print("\n" + "─" * 78)
print(f"dashboard buttons: {PASS} passed, {FAIL} failed")
sys.exit(1 if FAIL else 0)
