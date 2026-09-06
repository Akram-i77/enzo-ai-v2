#!/usr/bin/env python3
"""The dashboard must never be *reported* as running when it is not.

The owner's complaint was exact: "OpenClaw says it opened the dashboard and it
runs, but in reality it does not run." The cause was not a rendering bug - it was
an unverified claim. `start` printed the dashboard URL as soon as a PID file
appeared, and exited 0, while the serve thread had died binding the port (an
older ENZO or any other process holding it). The URL then opened SOMEBODY ELSE's
page, full of numbers that looked alive.

Pinned here, so the claim can never quietly become an assumption again:

* every ENZO response carries X-Enzo-Pid / X-Enzo-Data, and a 200 from a process
  that does NOT identify itself is reported as "not this bot" - never as "up";
* `enzoctl start` exits non-zero and names the stranger when the port is taken,
  exits 0 with a "verified" line when this bot really answers, and says
  "disabled" when started with --no-dashboard;
* `enzoctl status` shows the same verified/foreign/down verdict;
* the recorded bind failure (data/run/enzo-dashboard-error.json) appears when the
  server cannot bind and is cleared again by `stop`, so a stopped bot is not
  accused forever;
* `python3 enzo.py start` - the legacy supervisor - verifies too, and reports the
  Telegram listener as NOT ACTIVE when no bot token is configured instead of
  printing "Active" over a thread that exited immediately.

Run:  python3 tests/test_dashboard_liveness.py
"""
import http.server
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time

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
ENZO_PY = os.path.join(ROOT, "enzo.py")

_HOMES = []
_PROCS = []


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


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = int(s.getsockname()[1])
    s.close()
    return p


def _set_dash_port(txt, port):
    """Rewrite only dashboard.port (the file has other 'port:' keys)."""
    out, in_dash = [], False
    for ln in txt.splitlines():
        if re.match(r"^dashboard:\s*$", ln):
            in_dash = True
            out.append(ln)
            continue
        if in_dash and re.match(r"^\S", ln):
            in_dash = False
        if in_dash and re.match(r"^\s+port:\s*\d+", ln):
            out.append(re.sub(r"^(\s+port:\s*).*$", r"\g<1>%d" % port, ln))
            continue
        out.append(ln)
    return "\n".join(out) + "\n"


def make_home(tag, port=None, config_text=None):
    home = tempfile.mkdtemp(prefix="enzo-live-%s-" % tag)
    _HOMES.append(home)
    os.makedirs(os.path.join(home, "config"), exist_ok=True)
    os.makedirs(os.path.join(home, "data", "logs"), exist_ok=True)
    dst = os.path.join(home, "config", "enzo-config.yaml")
    if config_text is None:
        with open(os.path.join(ROOT, "config", "enzo-config.yaml"), encoding="utf-8") as f:
            config_text = f.read()
        if port:
            config_text = _set_dash_port(config_text, port)
    with open(dst, "w", encoding="utf-8") as f:
        f.write(config_text)
    return home


def marker_path(home):
    return os.path.join(home, "data", "run", "enzo-dashboard-error.json")


def ctl(home, *args, timeout=120):
    p = subprocess.run([PY, CTL, *args], capture_output=True, text=True,
                       timeout=timeout, env=_env(home), cwd=ROOT)
    return p.returncode, (p.stdout or "") + (p.stderr or ""), (p.stdout or "")


def json_of(stdout):
    try:
        return json.loads(stdout)
    except Exception:
        dec, idx = json.JSONDecoder(), 0
        for idx, ch in enumerate(stdout):
            if ch != "{":
                continue
            try:
                obj, _ = dec.raw_decode(stdout[idx:])
                return obj
            except Exception:
                continue
    return None


# ── a responder that is NOT ENZO: 200 on everything, no identity headers ──────
class _Stranger(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        body = b'{"status":"ok","equity":999999,"note":"somebody else"}'
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        try:
            self.wfile.write(body)
        except Exception:
            pass

    def log_message(self, *a):
        pass


def start_stranger():
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Stranger)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, int(srv.server_address[1])


def start_enzo_server(home, port):
    """A real ENZO dashboard server in its own process, bound to a sandbox home."""
    code = ("from enzo.ui import serve; "
            "serve.run_server('127.0.0.1', %d)" % port)
    p = subprocess.Popen([PY, "-c", code], env=_env(home), cwd=ROOT,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    _PROCS.append(p)
    for _ in range(80):
        if p.poll() is not None:
            return p
        try:
            with socket.create_connection(("127.0.0.1", port), 0.4):
                return p
        except OSError:
            time.sleep(0.25)
    return p


def probe(port, home=None):
    """probe_dashboard() from a clean process (its own ENZO_HOME, explicit port)."""
    home = home or make_home("probe-%d" % port)
    code = ("import json; from enzo.ui import serve; "
            "print('PROBE' + json.dumps(serve.probe_dashboard(port=%d, timeout=2.0)))" % port)
    p = subprocess.run([PY, "-c", code], capture_output=True, text=True,
                       timeout=90, env=_env(home), cwd=ROOT)
    out = p.stdout or ""
    m = re.search(r"PROBE(\{.*\})", out, re.S)
    if not m:
        return {"answers": False, "error": "probe produced no JSON: " + out[-300:] + (p.stderr or "")[-300:]}
    try:
        return json.loads(m.group(1))
    except Exception as e:
        return {"answers": False, "error": "probe JSON unreadable: %s" % e}


def wait_answer(port, seconds=20.0):
    end = time.time() + seconds
    while time.time() < end:
        try:
            with socket.create_connection(("127.0.0.1", port), 0.5):
                return True
        except OSError:
            time.sleep(0.3)
    return False


def run_enzo_py_start(home, port, seconds=18):
    """`enzo.py start` runs the engine in the foreground, so capture then kill."""
    cmd = [PY, ENZO_PY, "start", "--port", str(port), "--interval", "600"]
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=seconds,
                           env=_env(home), cwd=ROOT)
        return (p.stdout or "") + (p.stderr or "")
    except subprocess.TimeoutExpired as e:
        def dec(x):
            if isinstance(x, bytes):
                return x.decode("utf-8", "replace")
            return x or ""
        return dec(e.stdout) + dec(e.stderr)


def stop_all():
    for p in list(_PROCS):
        try:
            if p.poll() is None:
                p.terminate()
        except Exception:
            pass
    time.sleep(0.5)
    for p in list(_PROCS):
        try:
            if p.poll() is None:
                p.kill()
        except Exception:
            pass
    for h in list(_HOMES):
        try:
            subprocess.run([PY, CTL, "stop"], capture_output=True, text=True,
                           timeout=60, env=_env(h), cwd=ROOT)
        except Exception:
            pass
        shutil.rmtree(h, ignore_errors=True)


# ═══════════════════════════════════════════════════════════════════════════
print("═" * 78)
print("ENZO dashboard liveness — a claimed dashboard must be a VERIFIED dashboard")
print("═" * 78)

try:
    # ── 1. identity: who is answering the port? ───────────────────────────────
    section("1. probe_dashboard attributes the responder (X-Enzo-Pid / X-Enzo-Data)")
    port = free_port()
    home = make_home("srv", port=port)
    srv_proc = start_enzo_server(home, port)
    ok(wait_answer(port, 25), "the sandbox ENZO server answers on port %d" % port)
    pr = probe(port)
    ok(pr.get("answers") is True, "probe reports answers=True for a live ENZO server")
    ok(pr.get("pid") == srv_proc.pid,
       "the responder identifies itself as the ENZO process we started",
       "header pid=%s process pid=%s" % (pr.get("pid"), srv_proc.pid))
    ok(bool(pr.get("data_dir")) and
       os.path.realpath(home) in os.path.realpath(str(pr.get("data_dir"))),
       "X-Enzo-Data names THIS workspace's data dir - the page is built from our own state",
       str(pr.get("data_dir")))
    ok(pr.get("status") in (200, 503),
       "the health status is reported as observed (200 ok / 503 degraded)", str(pr.get("status")))
    ok(pr.get("health") in ("ok", "degraded", "error", None),
       "the health verdict comes from the body, not from a guess", str(pr.get("health")))
    try:
        srv_proc.terminate()
        srv_proc.wait(timeout=10)
    except Exception:
        srv_proc.kill()

    section("2. a 200 from a stranger is NOT reported as ours")
    stranger, sports = start_stranger()
    try:
        pr = probe(sports)
        ok(pr.get("answers") is True, "the foreign responder does answer HTTP")
        ok(pr.get("pid") is None,
           "…and it sends no X-Enzo-Pid, so it cannot be claimed as this bot", str(pr.get("pid")))
        ok(pr.get("data_dir") in (None, ""),
           "…and no X-Enzo-Data either: the page it serves is not built from our data")
        # the raw body looks perfectly healthy - that is the trap
        import urllib.request
        with urllib.request.urlopen("http://127.0.0.1:%d/health" % sports, timeout=3) as r:
            body = r.read().decode()
        ok('"status":"ok"' in body.replace(" ", ""),
           "the stranger's body claims status ok — proof that a 200 alone proves nothing")
    finally:
        stranger.shutdown()

    section("3. nothing on the port: answers=False, with the recorded reason")
    dead = free_port()
    pr = probe(dead)
    ok(pr.get("answers") is False, "an unbound port reports answers=False")
    ok(bool(pr.get("error")), "…and says why (connection refused / timeout)", str(pr.get("error"))[:70])
    # a bind failure recorded by run_server must surface through the probe
    home2 = make_home("mark", port=dead)
    code = ("from enzo.ui import serve;"
            "import json;"
            "srv=None;"
            "exec('''\n"
            "try:\n"
            "    serve.run_server('127.0.0.1', %d)\n"
            "except Exception as e:\n"
            "    print('RAISED', type(e).__name__)\n"
            "''');" % dead)
    # occupy the port first so the bind really fails
    blocker = socket.socket()
    blocker.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    blocker.bind(("127.0.0.1", dead))
    blocker.listen(1)
    try:
        p = subprocess.run([PY, "-c", code], capture_output=True, text=True,
                           timeout=60, env=_env(home2), cwd=ROOT)
        ok("RAISED" in (p.stdout or ""),
           "run_server raises when the port is held (it does not silently serve nothing)")
        ok(os.path.exists(marker_path(home2)),
           "…and it WRITES data/run/enzo-dashboard-error.json before raising, so the reason survives")
        pr = probe(dead, home=home2)
        ok(bool(pr.get("recorded_error")),
           "the next probe reads that marker and reports the recorded reason",
           str(pr.get("recorded_error"))[:70])
    finally:
        blocker.close()

    # ── 4. enzoctl start ─────────────────────────────────────────────────────
    section("4. ./enzoctl start when the port is already held by another process")
    stranger2, fport = start_stranger()
    try:
        hA = make_home("occupied", port=fport)
        rc, text, out = ctl(hA, "start", "--wait", "12")
        ok(rc == 1, "start EXITS NON-ZERO — a supervisor cannot read this as success", "rc=%s" % rc)
        ok("THE DASHBOARD IS NOT THIS BOT" in text,
           "start says plainly that the dashboard is not this bot's")
        ok("does not identify itself" in text,
           "…and names the stranger as a process that does not identify itself")
        ok("would show ANOTHER instance" in text or "ANOTHER instance" in text,
           "…and warns that opening the URL shows another instance's numbers")
        ok("engine IS running" in text,
           "…and states the engine was NOT stopped (open positions keep their exit monitor)")
        ok("identified itself as THIS bot" not in text,
           "no 'verified' claim anywhere in that output")
        rc2, text2, _ = ctl(hA, "status")
        ok(rc2 == 1,
           "status EXITS NON-ZERO too - a supervisor that only reads the exit code "
           "cannot mistake this for a healthy bot", "rc=%s" % rc2)
        pl2 = json_of(text2) or {}
        ok("THIS bot's page is not up" in text2,
           "status names the same verdict: port answered, but not by this bot")
        ok(os.path.exists(marker_path(hA)),
           "the bind failure stays recorded while the bot runs")
        rc3, text3, _ = ctl(hA, "stop")
        ok(rc3 == 0, "stop succeeds", "rc=%s" % rc3)
        time.sleep(1.0)
        ok(not os.path.exists(marker_path(hA)),
           "stop CLEARS the marker — a stopped bot is not accused of a dead dashboard")
        rc4, text4, _ = ctl(hA, "status")
        ok("not running — but port" in text4,
           "with the bot stopped, status still names who holds the port",
           "rc=%s" % rc4)
    finally:
        stranger2.shutdown()

    section("5. ./enzoctl start on a free port verifies against the live page")
    pB = free_port()
    hB = make_home("free", port=pB)
    rc, text, out = ctl(hB, "start", "--wait", "25")
    ok(rc == 0, "start exits 0 when this bot really serves the page", "rc=%s" % rc)
    ok("identified itself as THIS bot" in text,
       "start prints a VERIFIED line instead of a bare URL")
    ok("enzo-dashboard.html" in text, "…and still gives the owner the URL to open")
    ok(not os.path.exists(marker_path(hB)),
       "no failure marker is left behind on a healthy start")
    ok(wait_answer(pB, 20), "the page really answers on that port")
    prB = probe(pB, home=hB)
    pid_file = os.path.join(hB, "data", "run", "enzo.pid")
    sup_pid = None
    try:
        with open(pid_file, encoding="utf-8") as f:
            sup_pid = int(f.read().strip() or 0) or None
    except Exception:
        sup_pid = None
    ok(prB.get("answers") is True and prB.get("pid") == sup_pid,
       "the responder's X-Enzo-Pid equals the supervisor PID in data/run/enzo.pid",
       "header=%s pidfile=%s" % (prB.get("pid"), sup_pid))
    rc2, text2, _ = ctl(hB, "status")
    ok(rc2 == 0, "status exits 0 for a healthy bot", "rc=%s" % rc2)
    ok("serving" in text2 and "this bot" in text2,
       "status shows the dashboard row as serving · this bot")
    rc3, text3, out3 = ctl(hB, "status", "--json")
    pl = json_of(out3) or {}
    dash = (pl.get("dashboard") or {})
    ok(dash.get("serving") is True and dash.get("ours") is True,
       "status --json carries dashboard.serving/ours for a machine supervisor",
       json.dumps(dash)[:90])
    rc4, _, _ = ctl(hB, "stop")
    ok(rc4 == 0, "stop succeeds after a verified start", "rc=%s" % rc4)

    section("6. ./enzoctl start --no-dashboard says a page is NOT being served")
    hC = make_home("nodash", port=free_port())
    rc, text, out = ctl(hC, "start", "--no-dashboard", "--json", "--wait", "8")
    ok(rc == 0, "starting without a dashboard is a legitimate, successful start", "rc=%s" % rc)
    pl = json_of(out) or {}
    ok(pl.get("dashboard_disabled") is True and pl.get("dashboard_serving") is False,
       "--json reports dashboard_disabled=true / dashboard_serving=false",
       json.dumps({k: pl.get(k) for k in ("dashboard_disabled", "dashboard_serving")}))
    ok(not os.path.exists(marker_path(hC)),
       "a disabled dashboard writes no failure marker (nothing failed)")
    rc5, text5, out5 = ctl(hC, "status")
    pl5 = json_of(out5) or {}
    ok(rc5 == 0,
       "with --no-dashboard a missing page is a CHOICE, not a failure: status still exits 0",
       "rc=%s" % rc5)
    ok(pl5.get("dashboard_foreign") is None and pl5.get("dashboard_down") is None,
       "...and it sets neither dashboard_foreign nor dashboard_down",
       json.dumps({k: pl5.get(k) for k in ("dashboard_foreign", "dashboard_down")}))
    ctl(hC, "stop")

    hC2 = make_home("nodash2", port=free_port())
    rc6, text6, _ = ctl(hC2, "start", "--no-dashboard", "--wait", "8")
    ok(rc6 == 0 and "disabled (--no-dashboard)" in text6,
       "the human output says the dashboard is disabled rather than printing a URL",
       [l.strip() for l in text6.splitlines() if "dashboard" in l][:1])
    ok("no page is being served" in text6,
       "...and states plainly that no page is being served")
    ctl(hC2, "stop")

    # ── 7. the legacy supervisor ─────────────────────────────────────────────
    section("7. python3 enzo.py start — the legacy supervisor verifies too")
    pD = free_port()
    hD = make_home("legacy", port=pD)
    out = run_enzo_py_start(hD, pD)
    ok("[verified:" in out,
       "enzo.py start prints [verified: pid=… health=…] after probing the port")
    ok("✗ Live Dashboard" not in out,
       "…and no dashboard failure line on a healthy start")
    ok("NOT ACTIVE" in out and "telegram_bot_token" in out,
       "the Telegram listener is reported NOT ACTIVE (no token) instead of 'Active'",
       [l.strip() for l in out.splitlines() if "Telegram Bot" in l][:1])
    ctl(hD, "stop")

    stranger3, gport = start_stranger()
    try:
        hE = make_home("legacy-busy", port=gport)
        out = run_enzo_py_start(hE, gport)
        ok("NOT SERVED BY THIS PROCESS" in out,
           "with the port held, enzo.py start says the dashboard is NOT served by this process")
        ok("[verified:" not in out, "…and prints no verified claim")
        ok("belongs to that other process" in out,
           "…and warns that the page at that URL belongs to the other process")
        ok("Trading Engine" in out,
           "the engine line is still printed: trading is not killed by a dead page")
    finally:
        stranger3.shutdown()
    ctl(hE, "stop")

    section("8. the Telegram listener answers 'is it alive?' honestly")
    code = (
        "import time, json\n"
        "from enzo.ui import botctl\n"
        "l = botctl.TelegramBotListener()\n"
        "before = l.is_alive()\n"
        "def fake_loop():\n"
        "    while not l._stop_event.is_set():\n"
        "        time.sleep(0.05)\n"
        "l._poll_loop = fake_loop\n"
        "l.start()\n"
        "time.sleep(0.4)\n"
        "during = l.is_alive()\n"
        "l.stop()\n"
        "time.sleep(0.4)\n"
        "after = l.is_alive()\n"
        "print('TG' + json.dumps([before, during, after]))\n"
    )
    hF = make_home("tg")
    p = subprocess.run([PY, "-c", code], capture_output=True, text=True,
                       timeout=60, env=_env(hF), cwd=ROOT)
    m = re.search(r"TG(\[[^\]]*\])", p.stdout or "")
    trio = json.loads(m.group(1)) if m else None
    ok(trio == [False, True, False],
       "is_alive() is False before start, True while polling, False after stop",
       str(trio) + ("" if m else (p.stderr or "")[-200:]))
    # and the real listener (no token in this sandbox) must NOT stay alive
    code2 = (
        "import time, json;"
        "from enzo.ui import botctl;"
        "l = botctl.get_telegram_listener(); l.start(); time.sleep(1.2);"
        "print('TG2' + json.dumps(l.is_alive()))"
    )
    p = subprocess.run([PY, "-c", code2], capture_output=True, text=True,
                       timeout=60, env=_env(hF), cwd=ROOT)
    m = re.search(r"TG2(true|false)", p.stdout or "")
    ok(bool(m) and m.group(1) == "false",
       "with no bot token the listener thread exits at once, and is_alive() says so",
       (m.group(1) if m else (p.stdout or "")[-120:] + (p.stderr or "")[-200:]))

finally:
    stop_all()

# ─────────────────────────────────────────────────────────────────────────────
print("\n" + "─" * 78)
print(f"dashboard liveness: {PASS} passed, {FAIL} failed")
sys.exit(1 if FAIL else 0)
