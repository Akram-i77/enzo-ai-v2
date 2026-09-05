#!/usr/bin/env python3
"""The GMGN rate-limit budget: proof that ENZO now sends FEWER requests.

Why this file exists
--------------------
The operator's key kept getting banned and the dashboard said
``SNIPER_DATA_UNAVAILABLE``. The ban is GMGN's ``RATE_LIMIT_BANNED`` - a
rate-limit ban, not a content problem - and it was self-inflicted:

  * one scan cycle issued **63 gmgn-cli calls** (measured with the mock CLI's
    argv log: 2 discovery + 5 per deep analysis x up to 12 analyses);
  * at the configured 0.8 req/s a cycle needs ~79s, but the loop interval is 60s,
    so ``sleep_time = max(1.0, interval - elapsed)`` collapsed to 1s and the
    engine **never idled** - a continuous ~48 requests/min, 24/7;
  * three knobs that looked like protection were dead: they were in the shipped
    config and read by NO code (test_config_wiring listed them as frozen dead
    keys) - ``data_sources.gmgn.max_candidates_per_scan``,
    ``pump_monitor.max_analyses_per_min``, ``pump_monitor.min_analysis_interval_sec``;
  * on a ban ENZO waited and retried inside the call, although gmgn-cli had
    already retried once itself, and parsed a "resets at <datetime>" string that
    GMGN never sends (its payload carries ``reset_at`` as a UNIX timestamp), so it
    fell back to a 30s guess and retried straight into a ~5 minute ban. GMGN's own
    docs: "repeated requests during the cooldown can extend the ban by 5 seconds
    each time, up to 5 minutes".

Every check below counts REAL gmgn-cli invocations through the bundled mock, so
the numbers are the load GMGN would see, not a stub's opinion.

Run:  python3 tests/test_rate_limit_budget.py
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
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
    return cond


def section(t):
    print(f"\n=== {t} ===")


# ─────────────────────────────────────────────────────────────────────────────
# Isolated runtime. enzo-secrets.json is NOT copied, so nothing can reach the
# operator's real Telegram bot.
# ─────────────────────────────────────────────────────────────────────────────
SANDBOX = tempfile.mkdtemp(prefix="enzo-ratelimit-")
os.makedirs(os.path.join(SANDBOX, "config"), exist_ok=True)
os.makedirs(os.path.join(SANDBOX, "data"), exist_ok=True)
shutil.copy(os.path.join(ROOT, "config", "enzo-config.yaml"),
            os.path.join(SANDBOX, "config", "enzo-config.yaml"))
for f in ("enzo-control.json",):
    src = os.path.join(ROOT, "config", f)
    if os.path.exists(src):
        shutil.copy(src, os.path.join(SANDBOX, "config", f))

os.environ["ENZO_HOME"] = SANDBOX
os.environ["ENZO_CONFIG_PATH"] = os.path.join(SANDBOX, "config", "enzo-config.yaml")
os.environ["GMGN_API_KEY"] = "test-key-not-real"
os.environ["MOCK_STATE"] = "{}"
ARGV_LOG = os.path.join(SANDBOX, "data", "gmgn-argv.jsonl")
os.environ["GMGN_ARGV_LOG"] = ARGV_LOG

from conftest_paths import install_mock_on_path  # noqa: E402
install_mock_on_path()

import yaml  # noqa: E402
from enzo.core import config as C  # noqa: E402
from enzo.core import db  # noqa: E402
from enzo.core import engine  # noqa: E402
from enzo.providers import gmgn  # noqa: E402
from enzo.ui import botctl  # noqa: E402

print(f"sandbox: {SANDBOX}")
print(f"mock gmgn-cli: {shutil.which('gmgn-cli')}")

# This suite runs the REAL pipeline several times, and the engine logs a whole
# position dict per BUY. The checks read engine.cycle_stats() / the argv log, so
# INFO noise is dropped here to keep the output readable.
import logging  # noqa: E402
for _name in ("enzo.engine", "enzo.pump", "enzo.executor", "enzo.gmgn",
              "enzo.analyze", "enzo.portfolio"):
    logging.getLogger(_name).setLevel(logging.WARNING)


def set_cfg(gmgn_kw=None, pump_kw=None, engine_kw=None, paper=True):
    """Patch the sandbox YAML and drop the cached config (config.py caches it)."""
    path = os.environ["ENZO_CONFIG_PATH"]
    with open(path, encoding="utf-8") as fh:
        doc = yaml.safe_load(fh) or {}
    doc["paper_mode"] = paper
    # Fast local pacing: this suite measures VOLUME, and waiting 1.25s per call
    # would make it unusable. The caps under test are the volume knobs.
    g = doc.setdefault("data_sources", {}).setdefault("gmgn", {})
    g["requests_per_sec"] = 500.0
    g["request_gap_ms"] = 0
    g.update(gmgn_kw or {})
    p = doc.setdefault("pump_monitor", {})
    p.update(pump_kw or {})
    e = doc.setdefault("engine", {})
    e.update(engine_kw or {})
    with open(path, "w", encoding="utf-8") as fh:
        yaml.safe_dump(doc, fh, sort_keys=False, allow_unicode=True)
    C._CFG_CACHE.update({"mtime": None, "size": None, "path": None, "cfg": None})


def calls():
    """Real gmgn-cli invocations since the last reset."""
    if not os.path.exists(ARGV_LOG):
        return []
    out = []
    with open(ARGV_LOG, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if line:
                try:
                    out.append(json.loads(line).get("argv") or [])
                except Exception:
                    pass
    return out


def reset_calls():
    if os.path.exists(ARGV_LOG):
        os.remove(ARGV_LOG)
    gmgn._CACHE.clear()
    gmgn._DISCOVERY_STATUS.update({"last_ok_ts": 0.0, "last_error": None,
                                   "categories_ok": {}, "consecutive_empty": 0,
                                   "last_count": None})
    gmgn._GMGN_BIN_CACHE.update({"resolved": False, "bin": None})
    engine._ANALYSIS_TIMES.clear()


def deep_calls(argv):
    """Token-level calls (the ~5 per analysis), excluding discovery."""
    return [a for a in argv if a and a[0] == "token"]


def seed_candidates(n):
    """Make discovery return n DISTINCT qualifying tokens.

    The bundled mock ships one trenches token, so without this the budget and
    candidate caps could never be exercised (1 candidate < any cap). The token is
    duplicated from the mock's own payload - so the fields are exactly the ones
    the pre-screen accepts - with a unique address and symbol each.
    """
    reset_calls()
    proc = subprocess.run(["gmgn-cli", "market", "trenches", "--raw"],
                          capture_output=True, text=True, timeout=60,
                          env={**os.environ, "GMGN_MOCK_STATE": "{}"})
    try:
        base = json.loads(proc.stdout)["data"]["new_creation"][0]
    except Exception:
        base = None
    if not base:
        return 0
    toks = []
    for i in range(n):
        t = dict(base)
        t["address"] = f"MCK{i:03d}" + str(base.get("address"))[6:]
        t["symbol"] = f"MCK{i}"
        t["name"] = f"Mock Budget Token {i}"
        toks.append(t)
    os.environ["GMGN_MOCK_STATE"] = json.dumps({"trenches": {"new_creation": toks}})
    return len(toks)


def clear_cooldowns():
    """Forget every per-mint analysis stamp (the cooldown lives in the DB)."""
    try:
        with db.db_cursor(commit=True) as cur:
            cur.execute("DELETE FROM cache_store WHERE key LIKE 'enzoscan:%'")
    except Exception:
        pass
    engine._ANALYSIS_TIMES.clear()


db.init_db()
botctl.set_paused(False)

# ─────────────────────────────────────────────────────────────────────────────
section("1. the caps in the shipped config are read by code, not decoration")
cfg = C.load_config()
g = ((cfg.get("data_sources") or {}).get("gmgn") or {})
pm = cfg.get("pump_monitor") or {}
for key, val in (("data_sources.gmgn.max_candidates_per_scan", g.get("max_candidates_per_scan")),
                 ("data_sources.gmgn.max_depth_analyses", g.get("max_depth_analyses")),
                 ("data_sources.gmgn.reanalysis_cooldown_sec", g.get("reanalysis_cooldown_sec")),
                 ("pump_monitor.max_analyses_per_min", pm.get("max_analyses_per_min")),
                 ("pump_monitor.min_analysis_interval_sec", pm.get("min_analysis_interval_sec"))):
    ok(val not in (None, 0), f"{key} is configured and non-zero", str(val))
src = open(os.path.join(ROOT, "enzo", "core", "engine.py"), encoding="utf-8").read()
for key in ("max_candidates_per_scan", "max_analyses_per_min",
            "min_analysis_interval_sec", "reanalysis_cooldown_sec"):
    ok(f'"{key}"' in src, f"engine.py reads {key} (it used to be a dead knob)", "")
ok(int(pm.get("max_analyses_per_min", 99)) * 5 + 2 <= 35,
   "the shipped budget keeps the ceiling under ~35 requests/min "
   "(6 analyses x ~5 + discovery), where it was ~63 per cycle back-to-back",
   f"max_analyses_per_min={pm.get('max_analyses_per_min')}")

# ─────────────────────────────────────────────────────────────────────────────
section("2. baseline: what one cycle costs when the budget is wide open")
set_cfg(gmgn_kw={"max_depth_analyses": 4, "max_candidates_per_scan": 40,
                 "reanalysis_cooldown_sec": 0},
        pump_kw={"max_analyses_per_min": 100, "min_analysis_interval_sec": 0},
        engine_kw={"scan_interval_sec": 60})
ok(seed_candidates(8) == 8, "discovery can be seeded with 8 distinct candidates", "")
clear_cooldowns()
reset_calls()
base = engine.scan_once()
base_argv = calls()
base_deep = deep_calls(base_argv)
base_stats = engine.cycle_stats()
ok(base_stats["analysed"] > 0, "a wide-open cycle really does analyse candidates",
   f"{base_stats['analysed']} analysed")
ok(base_stats["analysed"] <= 4, "and stops at max_depth_analyses", str(base_stats["analysed"]))
ok(len(base_deep) >= base_stats["analysed"] * 3,
   "each deep analysis costs several token-level requests (info, security, holders, "
   "traders, created-tokens)", f"{len(base_deep)} token calls / {base_stats['analysed']} analyses")
ok(base_stats["gmgn_calls"] > 0, "cycle_stats reports the GMGN cost of the cycle",
   json.dumps(base_stats))
BASE_DEEP_PER_ANALYSIS = max(1.0, len(base_deep) / max(1, base_stats["analysed"]))

# ─────────────────────────────────────────────────────────────────────────────
section("3. max_analyses_per_min cuts the cycle short (it was never enforced)")
set_cfg(gmgn_kw={"max_depth_analyses": 4, "reanalysis_cooldown_sec": 0},
        pump_kw={"max_analyses_per_min": 2, "min_analysis_interval_sec": 0})
clear_cooldowns()
reset_calls()
engine.scan_once()
b_argv = calls()
b_stats = engine.cycle_stats()
ok(b_stats["analysed"] <= 2, "only the budgeted number of analyses ran",
   f"{b_stats['analysed']} of a possible 4")
ok(b_stats["skipped_budget"] >= 1, "and the skipped ones are COUNTED, not silent",
   f"{b_stats['skipped_budget']} skipped by budget")
ok(len(deep_calls(b_argv)) < len(base_deep),
   "so the cycle sent fewer token requests than the wide-open baseline",
   f"{len(deep_calls(b_argv))} vs {len(base_deep)}")

# ─────────────────────────────────────────────────────────────────────────────
section("4. the re-analysis cooldown: the same trending coins are not re-examined")
# This is the biggest cut of all. Discovery keeps returning the same tokens cycle
# after cycle; before, every one of them was re-analysed forever (5 requests each,
# every ~60s). GMGN's own docs call per-candidate re-fetching an anti-pattern.
set_cfg(gmgn_kw={"max_depth_analyses": 4, "reanalysis_cooldown_sec": 3600},
        pump_kw={"max_analyses_per_min": 100, "min_analysis_interval_sec": 3600})
clear_cooldowns()
reset_calls()
first = engine.scan_once()
first_deep = len(deep_calls(calls()))
reset_calls()                      # keep the DB cache, forget the argv log
second = engine.scan_once()
second_argv = calls()
second_deep = len(deep_calls(second_argv))
s2 = engine.cycle_stats()
ok(first_deep > 0, "the first cycle analysed (and paid for) its candidates",
   f"{first_deep} token requests")
ok(second_deep == 0, "the second cycle sent ZERO token requests - every candidate was "
   "still inside min_analysis_interval_sec / reanalysis_cooldown_sec",
   f"{second_deep} token requests (first cycle: {first_deep})")
ok(s2["skipped_cooldown"] >= 1, "and it reports how many it skipped and why",
   json.dumps(s2))
ok(len(second_argv) <= len(calls()) and second_deep == 0,
   "discovery may still refresh (it is cheap and cached), but the expensive per-coin "
   "work is gone", f"{len(second_argv)} total request(s) in cycle 2")

# ── persisted, so a RESTART does not re-burn the budget ──
probe = subprocess.run(
    [sys.executable, "-c", """
import json, os, sys
ROOT = sys.argv[1]
sys.path.insert(0, ROOT); sys.path.insert(0, os.path.join(ROOT, "tests"))
from conftest_paths import install_mock_on_path
install_mock_on_path()
from enzo.core import db, engine
from enzo.providers import gmgn
db.init_db()
gmgn._CACHE.clear()
engine._ANALYSIS_TIMES.clear()
if os.path.exists(os.environ["GMGN_ARGV_LOG"]):
    os.remove(os.environ["GMGN_ARGV_LOG"])
engine.scan_once()
st = engine.cycle_stats()
n = 0
p = os.environ["GMGN_ARGV_LOG"]
if os.path.exists(p):
    for line in open(p, encoding="utf-8"):
        line = line.strip()
        if line and (json.loads(line).get("argv") or [""])[0] == "token":
            n += 1
print(json.dumps({"analysed": st["analysed"], "skipped_cooldown": st["skipped_cooldown"],
                  "token_calls": n}))
""", ROOT],
    capture_output=True, text=True, timeout=300,
    env={**os.environ, "ENZO_HOME": SANDBOX,
         "ENZO_CONFIG_PATH": os.environ["ENZO_CONFIG_PATH"]})
try:
    fresh = json.loads(probe.stdout.strip().splitlines()[-1])
except Exception:
    fresh = {"analysed": -1, "skipped_cooldown": -1, "token_calls": -1,
             "err": probe.stdout[-400:] + probe.stderr[-400:]}
ok(fresh.get("token_calls") == 0 and fresh.get("analysed") == 0,
   "a FRESH PROCESS still respects the cooldown (it is stored in the DB, not in memory)",
   json.dumps(fresh))

# ── force bypasses it, because it is an explicit human request ──
reset_calls()
engine.scan_once(force=True)   # budget/interval ignored on purpose
f_stats = engine.cycle_stats()
ok(f_stats["analysed"] > 0 and len(deep_calls(calls())) > 0,
   "scan_once(force=True) - i.e. ./enzoctl scan --force - bypasses the cooldown",
   f"{f_stats['analysed']} analysed, {len(deep_calls(calls()))} token requests")

# ─────────────────────────────────────────────────────────────────────────────
section("5. max_candidates_per_scan caps what is even considered")
set_cfg(gmgn_kw={"max_depth_analyses": 8, "max_candidates_per_scan": 3,
                 "reanalysis_cooldown_sec": 0},
        pump_kw={"max_analyses_per_min": 100, "min_analysis_interval_sec": 0})
clear_cooldowns()
reset_calls()
engine.scan_once()
c_stats = engine.cycle_stats()
ok(c_stats["candidates"] >= c_stats["candidates_after_cap"],
   "discovered candidates are capped before ranking",
   f"{c_stats['candidates']} discovered -> {c_stats['candidates_after_cap']} kept")
ok(c_stats["candidates_after_cap"] <= 3, "and the cap is the configured one",
   str(c_stats["candidates_after_cap"]))
ok(c_stats["candidates"] > c_stats["candidates_after_cap"],
   "which really cut candidates this time (8 were seeded)",
   f"{c_stats['candidates']} -> {c_stats['candidates_after_cap']}")

# ─────────────────────────────────────────────────────────────────────────────
section("5b. the depth cap that ACTUALLY applies is the tightest of the two knobs")
# engine.py used `discovery.max_depth_tokens_per_cycle or gmgn.max_depth_analyses`,
# so discovery's 12 always won: lowering max_depth_analyses changed nothing while
# the log cheerfully printed 12. That is why the config looked tightened and the
# request volume did not move.
set_cfg(gmgn_kw={"max_depth_analyses": 6, "max_candidates_per_scan": 40,
                 "reanalysis_cooldown_sec": 0})
_cfg = C.load_config()
_doc = yaml.safe_load(open(os.environ["ENZO_CONFIG_PATH"], encoding="utf-8"))
_doc.setdefault("discovery", {})["max_depth_tokens_per_cycle"] = 2
yaml.safe_dump(_doc, open(os.environ["ENZO_CONFIG_PATH"], "w", encoding="utf-8"),
               sort_keys=False, allow_unicode=True)
C._CFG_CACHE.update({"mtime": None, "size": None, "path": None, "cfg": None})
_cand_cap, _depth_cap = engine.volume_caps(C.load_config())
ok(_depth_cap == 2, "discovery.max_depth_tokens_per_cycle=2 beats max_depth_analyses=6 "
   "(tightest wins, both are honoured)", f"effective depth cap {_depth_cap}")
_cand_cap2, _depth_cap2 = engine.volume_caps(
    {"discovery": {"max_depth_tokens_per_cycle": 9},
     "data_sources": {"gmgn": {"max_depth_analyses": 3}}})
ok(_depth_cap2 == 3, "and the other way round: max_depth_analyses=3 wins over 9",
   f"effective depth cap {_depth_cap2}")
ok(engine.volume_caps({}) == (40, 12), "with nothing configured the old defaults apply",
   str(engine.volume_caps({})))
clear_cooldowns()
reset_calls()
engine.scan_once()
ok(engine.cycle_stats()["analysed"] <= 2, "and the loop really stopped at the effective cap",
   f"{engine.cycle_stats()['analysed']} analysed")

# ─────────────────────────────────────────────────────────────────────────────
section("5c. the cooldown only freezes TERMINAL outcomes, never an opportunity")
# Real money: a 15-minute freeze on the wrong coin costs entries. WAIT is exactly
# the coin that may become a BUY two minutes later, and DATA_ERROR is a broken data
# source (during a ban every coin would otherwise be frozen). Only IGNORE and
# NOT_TRADABLE - a rug fingerprint or a failed hard gate - are worth freezing.
_M1 = "CoolDownTest111111111111111111111111111111111"
_M2 = "CoolDownTest222222222222222222222222222222222"
_M3 = "CoolDownTest333333333333333333333333333333333"
for _m in (_M1, _M2, _M3):
    db.cache_delete(f"enzoscan:{_m}")
engine._remember_analysis(_M1, {"decision": "IGNORE"})          # terminal
engine._remember_analysis(_M2, {"decision": "WAIT"})            # opportunity
engine._remember_analysis(_M3, {"decision": "DATA_ERROR"})      # broken data
engine._remember_analysis("NoResult1111111111111111111111111111111111", None)
ok(engine._cooldown_left(_M1, 45.0, 900.0)[0] > 0
   and "terminal IGNORE" in engine._cooldown_left(_M1, 45.0, 900.0)[1],
   "IGNORE (rug/dangerous) is frozen for the whole reanalysis_cooldown_sec",
   engine._cooldown_left(_M1, 45.0, 900.0)[1])
_left2, _why2 = engine._cooldown_left(_M2, 45.0, 900.0)
ok(0 < _left2 <= 45.0 and "min_analysis_interval_sec" in _why2,
   "WAIT is only held by the short interval floor, NOT the 15-minute cooldown "
   "(it may turn into a BUY next cycle)", f"{_left2:.0f}s left / {_why2}")
_left3, _why3 = engine._cooldown_left(_M3, 45.0, 900.0)
ok(0 < _left3 <= 45.0, "a DATA_ERROR is not frozen either - a broken data source must "
   "not blacklist a coin", f"{_left3:.0f}s left / {_why3}")
ok(engine._cooldown_left("NoResult1111111111111111111111111111111111", 45.0, 900.0)[0] == 0.0,
   "and a scan that produced no result stamps nothing (it may be retried)", "")
ok(engine._cooldown_left(_M1, 45.0, 900.0)[0] > 800,
   "the freeze really is the configured 900s, not the 45s floor",
   f"{engine._cooldown_left(_M1, 45.0, 900.0)[0]:.0f}s")
for _m in (_M1, _M2, _M3):
    db.cache_delete(f"enzoscan:{_m}")

# ─────────────────────────────────────────────────────────────────────────────
section("6. while a ban stands, a whole cycle sends ZERO requests")
# GMGN: every request during the cooldown extends the ban by 5s. So the sweep must
# not probe coin by coin - the old code sent one probe per candidate.
set_cfg(gmgn_kw={"max_depth_analyses": 4, "reanalysis_cooldown_sec": 0},
        pump_kw={"max_analyses_per_min": 100, "min_analysis_interval_sec": 0})
db.rl_report_ban("gmgn", ban_duration_sec=300)
clear_cooldowns()
reset_calls()
banned_run = engine.scan_once()
banned_argv = calls()
ok(len(banned_argv) == 0,
   "no gmgn-cli call at all during the ban (not even discovery)",
   f"{len(banned_argv)} call(s)")
ok(all(str(r.get("decision")) != "BUY" for r in (banned_run or [])),
   "and no coin is bought on missing data", str([r.get("decision") for r in (banned_run or [])][:5]))
left = gmgn.ban_status()
ok(left > 250, "the ban is still registered for GMGN's own window", f"{left:.0f}s left")
db.rl_clear_ban("gmgn")
ok(gmgn.ban_status() <= 0.5, "and ./enzoctl unban --confirm still clears it", "")

# ─────────────────────────────────────────────────────────────────────────────
section("7. the duty cycle is reported instead of hiding a continuous stream")
adv = engine.duty_cycle_advice(79.0, 60.0)
ok(bool(adv) and "never idles" in adv, "a 79s cycle against a 60s interval warns",
   adv[:110])
ok("requests/min" in adv or "/min" in adv, "and it states the resulting request rate",
   adv[adv.find("("):adv.find(")") + 1])
ok(engine.duty_cycle_advice(30.0, 60.0) == "", "a cycle that fits the interval says nothing", "")
ok(engine.duty_cycle_advice(0.0, 0.0) == "", "and a zero interval is not a division by zero", "")

# ─────────────────────────────────────────────────────────────────────────────
section("8. the volume is visible: counters, dashboard row, API and doctor")
st = gmgn.call_stats()
for k in ("total", "per_min_avg", "bans", "refused_by_limiter", "window_calls"):
    ok(k in st, f"gmgn.call_stats() exposes '{k}'", str(st.get(k)))
reset_calls()
gmgn.token_info("PumpV1xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx")
ok(gmgn.call_stats()["total"] >= 1 and len(calls()) == 1,
   "the counter tracks real CLI calls one-for-one", json.dumps(gmgn.call_stats()))

html = ""
try:
    from enzo.ui import dashboard
    _path = dashboard.generate()          # generate() writes the file and returns its path
    with open(_path, encoding="utf-8") as fh:
        html = fh.read()
except Exception as e:                                    # noqa: BLE001
    html = f"ERROR {e}"
ok('id="gmgnVolume"' in html, "the dashboard shows a Requests row", "")
ok("request(s) since start" in html, "with the lifetime count and per-minute average", "")
ok("last cycle" in html, "and what the last cycle cost", "")

# ── provenance: which source handed over each analysed coin ──
# seed_candidates() replaced the trenches list only, so the mock's default
# trending token (a distinct address) survives and is attributed to trending.
set_cfg(gmgn_kw={"max_depth_analyses": 3, "reanalysis_cooldown_sec": 0,
                 "trending_interval": "1m"},
        pump_kw={"max_analyses_per_min": 100, "min_analysis_interval_sec": 0})
clear_cooldowns()
reset_calls()
_prov = engine.scan_once()
_prov_stats = engine.cycle_stats()
ok(all("discovery_source" in r for r in (_prov or [])),
   "every decision carries the discovery source that produced it "
   "(gmgn_trenches / gmgn_trending / pumpdev / watchlist)",
   str([r.get("discovery_source") for r in (_prov or [])][:4]))
ok(isinstance(_prov_stats.get("sources"), dict) and sum(_prov_stats["sources"].values()) >= 1,
   "and cycle_stats tallies the analysed coins per source", json.dumps(_prov_stats.get("sources")))
_srcs = sorted({str(r.get("discovery_source")) for r in (_prov or [])})
ok("gmgn_trending" in _srcs and "gmgn_trenches" in _srcs,
   "a coin from `market trending` is attributed to trending and one from `market "
   "trenches` to trenches - so 'the coins are bad' can be traced to a source",
   str(_srcs))

api = ""
try:
    from enzo.ui import serve
    api = json.dumps(serve.health_snapshot())
except Exception as e:                                    # noqa: BLE001
    api = json.dumps({"error": str(e)})
ok('"call_stats"' in api and '"cycle_stats"' in api,
   "the /api/status JSON exposes both, so the UI can chart the load", api[:90])

doc_src = open(os.path.join(ROOT, "enzoctl"), encoding="utf-8").read()
ok("gmgn_request_budget" in doc_src, "enzoctl doctor has a gmgn_request_budget row", "")
ok("max_analyses_per_min" in doc_src and "reanalysis_cooldown_sec" in doc_src,
   "and it names the knobs that actually fix a rate-limit ban", "")

# ─────────────────────────────────────────────────────────────────────────────
print("\n" + "─" * 78)
print(f"~{BASE_DEEP_PER_ANALYSIS:.0f} token requests per deep analysis "
      f"(measured in section 2)")
print(f"Rate-limit budget: {PASS} passed, {FAIL} failed")
shutil.rmtree(SANDBOX, ignore_errors=True)
sys.exit(1 if FAIL else 0)
