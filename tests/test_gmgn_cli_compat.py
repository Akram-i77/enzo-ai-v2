#!/usr/bin/env python3
"""GMGN provider compatibility with the REAL gmgn-cli v1.6.1, plus the honest
full path (nothing stubbed).

Why this file exists
--------------------
1. The installed CLI is gmgn-cli v1.6.1. Compared with what the bot used to
   call, it renamed the data-address flag (--token -> --address), REMOVED
   `market smartmoney` / `market kol` (smart money and KOL now live under
   `track`, which returns trade records for a wallet, not a token list),
   REQUIRES GMGN_API_KEY for every data call, returns per-endpoint envelopes
   (trenches -> new_creation/near_completion/completed, trending -> data.rank),
   and reports holder shares as `amount_percentage` with `addr_type`
   (0 = wallet, 1 = burn, 2 = DEX/pool). Every one of those silently produced
   zeros or "0 candidates" before. `tests/mockbin/gmgn-cli` reproduces all of
   it — flag validation included — and its `legacy_dialect`/`legacy_only`
   switches emulate the OLD build so the dialect fallback is really exercised.

2. test_engine_e2e deliberately STUBS analyze.run_pipeline (it tests engine
   wiring). That is exactly why a refactor could leave analyze() returning None
   and the suite still print 462 green checks. Section 10 runs the REAL path
   end to end — engine.scan_mint -> analyze.run_pipeline -> gmgn provider ->
   mock CLI -> ledger — with nothing stubbed, in paper mode.

Run:  python3 tests/test_gmgn_cli_compat.py
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
NOTICES = []


def ok(cond, label, extra=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  \033[32mPASS\033[0m  {label}" + (f"   {extra}" if extra != "" else ""))
    else:
        FAIL += 1
        print(f"  \033[31mFAIL\033[0m  {label}" + (f"   {extra}" if extra != "" else ""))
    return cond


def notice(msg):
    NOTICES.append(msg)
    print(f"  \033[33mNOTE\033[0m  {msg}")


def section(t):
    print(f"\n=== {t} ===")


# ─────────────────────────────────────────────────────────────────────────────
# Isolated runtime. enzo-secrets.json is deliberately NOT copied, so no test can
# ever ping the operator's real Telegram bot.
# ─────────────────────────────────────────────────────────────────────────────
SANDBOX = tempfile.mkdtemp(prefix="enzo-gmgn-compat-")
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
os.environ.setdefault("MOCK_STATE", "{}")
ARGV_LOG = os.path.join(SANDBOX, "data", "gmgn-argv.jsonl")
os.environ["GMGN_ARGV_LOG"] = ARGV_LOG

from conftest_paths import install_mock_on_path  # noqa: E402
MOCKBIN = install_mock_on_path()

import yaml  # noqa: E402
from enzo.core import config as C  # noqa: E402
from enzo.providers import gmgn  # noqa: E402

print(f"sandbox: {SANDBOX}")
print(f"mock gmgn-cli: {shutil.which('gmgn-cli')}")

MINT = "PumpV1xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx"
OLD_MINT = "Legacyxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx"

# A holder list shaped exactly like the v1.6 payload: the bonding curve / AMM
# vault (addr_type 2) and a burn address (addr_type 1) dominate supply, and the
# dangerous wallet is the 12% one that is NOT a contract.
POOL_ROW = {"address": "CurveATA111111111111111111111111111111111111111",
            "amount_percentage": 0.62, "addr_type": 2, "tags": ["pool"],
            "maker_token_tags": [], "balance": 620_000_000.0, "usd_value": 9000.0}
BURN_ROW = {"address": "Burn1111111111111111111111111111111111111111111",
            "amount_percentage": 0.03, "addr_type": 1, "tags": [],
            "maker_token_tags": [], "balance": 30_000_000.0}


def wallet_row(addr, pct, tags=(), mtags=(), sold=0.0, pnl=0.0):
    return {"address": addr, "amount_percentage": pct, "addr_type": 0,
            "tags": list(tags), "maker_token_tags": list(mtags),
            "sell_amount_percentage": sold, "unrealized_pnl": pnl,
            "buy_tx_count_cur": 3, "sell_tx_count_cur": 1,
            "start_holding_at": int(time.time()) - 3600,
            "balance": pct * 1_000_000_000.0, "usd_value": pct * 14000.0}


def argv_log():
    out = []
    if not os.path.exists(ARGV_LOG):
        return out
    with open(ARGV_LOG, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if line:
                try:
                    out.append(json.loads(line).get("argv") or [])
                except Exception:
                    pass
    return out


def reset_provider():
    """Fresh caches/status/bin-resolution so each section sees a clean slate."""
    gmgn._CACHE.clear()
    gmgn._CACHE_STATS["hit"] = gmgn._CACHE_STATS["miss"] = 0
    gmgn._DISCOVERY_STATUS.update({"last_ok_ts": 0.0, "last_error": None,
                                   "categories_ok": {}, "consecutive_empty": 0,
                                   "last_count": None})
    gmgn._GMGN_BIN_CACHE.update({"resolved": False, "bin": None})
    if os.path.exists(ARGV_LOG):
        os.remove(ARGV_LOG)


def set_state(obj):
    os.environ["GMGN_MOCK_STATE"] = json.dumps(obj)


def set_gmgn_cfg(**kw):
    """Patch data_sources.gmgn in the sandbox YAML and drop the cached config."""
    path = os.environ["ENZO_CONFIG_PATH"]
    with open(path, encoding="utf-8") as fh:
        doc = yaml.safe_load(fh) or {}
    g = (doc.get("data_sources") or {}).get("gmgn") or {}
    g.update(kw)
    doc.setdefault("data_sources", {})["gmgn"] = g
    with open(path, "w", encoding="utf-8") as fh:
        yaml.safe_dump(doc, fh, sort_keys=False, allow_unicode=True)
    C._CFG_CACHE.update({"mtime": None, "size": None, "path": None, "cfg": None})


# ─────────────────────────────────────────────────────────────────────────────
section("1. the CLI binary is found and speaks the v1.6 dialect")
reset_provider()
set_state({})
b = gmgn.resolve_gmgn_bin()
ok(bool(b) and os.path.exists(str(b)), "resolve_gmgn_bin() found a binary", str(b))
info = gmgn.token_info(MINT)
ok(bool(info), "token info returned a payload", str(list(info)[:5]))
args = [a for a in argv_log() if "info" in a]
ok(any("--address" in a for a in args), "the v1.6 flag --address is used", str(args[:1]))
ok(not any("--token" in a for a in args), "the removed --token flag is NOT tried first")

# ─────────────────────────────────────────────────────────────────────────────
section("2. an OLD gmgn-cli (--token only) still works via the dialect fallback")
reset_provider()
set_state({"legacy_dialect": True, "legacy_only": True})
info2 = gmgn.token_info(OLD_MINT)
ok(bool(info2), "token info succeeded against an old build", str(list(info2)[:5]))
seq = ["--address" if "--address" in a else ("--token" if "--token" in a else None)
       for a in argv_log() if "info" in a]
ok(seq[:2] == ["--address", "--token"],
   "provider tried --address, was rejected, then fell back to --token", str(seq))

# ─────────────────────────────────────────────────────────────────────────────
section("3. a missing GMGN_API_KEY is a LOUD failure, not an empty market")
reset_provider()
set_state({})
saved = os.environ.pop("GMGN_API_KEY", None)
try:
    gmgn.reset_provider_status()
    raised = None
    try:
        empty = gmgn.token_info("NoKeyxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx")
    except Exception as e:                                  # noqa: BLE001
        raised = e
    else:
        ok(empty == {}, "token_info returned an empty payload (not a fake zero price)",
           str(empty)[:60])
    st = gmgn.provider_status()
    txt = str(st.get("last_error") or "")
    ok(bool(txt) and "GMGN_API_KEY" in txt,
       "the failure is recorded naming GMGN_API_KEY (it used to vanish into {})",
       txt[:130])
    ok(st.get("api_key_missing") is True, "provider_status flags api_key_missing")
    ok(st.get("api_key_present") is False, "provider_status reports no key on disk either")
    ok(st.get("last_error_endpoint") == "token/info",
       "and says which endpoint died", str(st.get("last_error_endpoint")))
    ok(int(st.get("error_count") or 0) >= 1, "errors are counted, not just last-wins")
    ok(raised is None, "the read helper still returns {} so one dead endpoint "
                       "cannot stop the scan loop", str(raised)[:80])
finally:
    if saved is not None:
        os.environ["GMGN_API_KEY"] = saved

# ─────────────────────────────────────────────────────────────────────────────
section("4. every discovery envelope is parsed (trending used to yield 0)")
reset_provider()
set_state({
    "trenches": {"new_creation": [{
        "address": "TrenchMint111111111111111111111111111111111", "symbol": "TRN",
        "launchpad_platform": "Pump.fun", "price": 0.000009, "usd_market_cap": 9000.0,
        "liquidity": 7000.0, "buys_24h": 120, "sells_24h": 40, "volume_24h": 21000.0,
        "progress": 0.31, "creator": "DevTrench111111111111111111111111111111111"}]},
    "trending": {"rank": [{
        "address": "TrendMint2222222222222222222222222222222222", "symbol": "TRD",
        "launchpad_platform": "Pump.fun", "exchange": "pump_amm", "price": 0.000031,
        "market_cap": 31000.0, "liquidity": 42000.0, "buys": 1500, "sells": 700,
        "volume": 180000.0, "creator": "DevTrend222222222222222222222222222222222"}]},
})
items = gmgn.discover("sol")
cats = gmgn.discovery_status().get("categories_ok") or {}
tr, td = cats.get("trenches") or {}, cats.get("trending") or {}
ok(tr.get("ok") is True and int(tr.get("count") or 0) == 1,
   "trenches parsed (new_creation/near_completion/completed)", str(tr))
ok(td.get("ok") is True and int(td.get("count") or 0) == 1,
   "trending parsed (data.rank) — the silent-zero bug", str(td))
srcs = {str(it.get("source")) for it in items}
ok(srcs == {"trenches", "trending"}, "candidates come from BOTH categories", str(srcs))
by_src = {it["source"]: it for it in items}
for field in ("mint", "price", "market_cap", "liquidity", "buys_24h", "sells_24h",
              "launchpad_platform"):
    ok(all(by_src[s].get(field) is not None for s in ("trenches", "trending")),
       f"both sources normalise '{field}'",
       str({s: by_src[s].get(field) for s in by_src}))
ok(abs(float(by_src["trenches"]["market_cap"]) - 9000.0) < 1e-6,
   "trenches usd_market_cap mapped onto market_cap", str(by_src["trenches"]["market_cap"]))
ok(abs(float(by_src["trending"]["sells_24h"]) - 700.0) < 1e-6,
   "trending flat 'sells' mapped onto sells_24h (the pre-migration gate needs it)",
   str(by_src["trending"]["sells_24h"]))
# duplicate mints across categories must collapse to one candidate
reset_provider()
set_state({})
dupes = gmgn.discover("sol")
mints = [it.get("mint") for it in dupes]
ok(len(mints) == len(set(mints)), "the same mint in two categories is de-duplicated",
   f"{len(mints)} items")

# ─────────────────────────────────────────────────────────────────────────────
section("5. dead discovery categories are skipped with a reason (no rate-limit burn)")
reset_provider()
set_state({})
set_gmgn_cfg(discovery=["trenches", "smartmoney", "kol", "trending"])
items2 = gmgn.discover("sol")
cats2 = gmgn.discovery_status().get("categories_ok") or {}
dead_calls = [a for a in argv_log() if len(a) > 1 and a[0] == "market"
              and a[1] in ("smartmoney", "kol")]
ok(not dead_calls, "`market smartmoney`/`market kol` were NEVER invoked", str(dead_calls))
ok((cats2.get("smartmoney") or {}).get("skipped") is True,
   "smartmoney marked skipped WITH an explanation",
   str((cats2.get("smartmoney") or {}).get("error"))[:150])
ok((cats2.get("kol") or {}).get("skipped") is True, "kol marked skipped too")
ok(bool(items2), f"the live categories still produced {len(items2)} candidates")

# ─────────────────────────────────────────────────────────────────────────────
section("6. discovery pushes the Pump.fun + min-market-cap filters down to the CLI")
reset_provider()
set_state({})
set_gmgn_cfg(launchpad_platform_filter="Pump.fun", discovery_limit=50)
gmgn.discover("sol")
mk = [a for a in argv_log() if len(a) > 1 and a[0] == "market"]
tr_args = next((a for a in mk if a[1] == "trenches"), [])
td_args = next((a for a in mk if a[1] == "trending"), [])
ok(tr_args and "--launchpad-platform" in tr_args and
   tr_args[tr_args.index("--launchpad-platform") + 1] == "Pump.fun",
   "trenches uses --launchpad-platform Pump.fun", str(tr_args))
ok(td_args and "--platform" in td_args and
   td_args[td_args.index("--platform") + 1] == "Pump.fun",
   "trending uses --platform Pump.fun (different flag name for the same filter)",
   str(td_args))
ok(tr_args and "--min-marketcap" in tr_args,
   "token_universe.discovery_min_market_cap is pushed to the CLI", str(tr_args))
ok(tr_args and "--limit" in tr_args and tr_args[tr_args.index("--limit") + 1] == "50",
   "discovery_limit is pushed to the CLI", str(tr_args))

# ─────────────────────────────────────────────────────────────────────────────
section("7. v1.6 field shapes: nested price object, fractions, trader tags")
set_state({"token_info": {
    "launchpad": "pump", "launchpad_platform": "Pump.fun", "launchpad_status": 2,
    "launchpad_progress": 1.0,
    "price": {"price": "0.004", "buys_24h": 120, "sells_24h": 34, "sells_5m": 3,
              "volume_24h": "98000"},
}})
i = gmgn.token_info("Nestedxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx")
ok(abs(float(gmgn._price_of(i) or 0) - 0.004) < 1e-12,
   "_price_of reads price.price (the nested object)", str(gmgn._price_of(i)))
ok(int(gmgn._swap_count(i, "sell") or -1) == 34, "_swap_count reads price.sells_24h",
   str(gmgn._swap_count(i, "sell")))
ok(int(gmgn._swap_count(i, "sell", "5m") or -1) == 3, "_swap_count honours the window arg")
ok(int(gmgn._swap_count(i, "sells") or -1) == 34,
   "_swap_count tolerates 'sells' too (a typo must not read as UNKNOWN)",
   str(gmgn._swap_count(i, "sells")))
ok(gmgn._swap_count({}, "sell") is None, "a missing counter stays None, never 0")
lp = gmgn.launchpad_profile(i)
ok(lp.get("is_pump_v1") is True, "launchpad_profile detects Pump V1",
   str({k: lp.get(k) for k in ('launchpad', 'platform')}))
ok(lp.get("migrated") is True and lp.get("phase") == "migrated",
   "launchpad_status 2 => migrated", str(lp.get("phase")))
ok(abs(float(gmgn._norm_pct(0.1783)) - 0.1783) < 1e-9,
   "_norm_pct keeps GMGN's decimal fraction as a fraction", str(gmgn._norm_pct(0.1783)))
ok(abs(float(gmgn._norm_pct(17.83)) - 0.1783) < 1e-9,
   "_norm_pct converts a percent-style payload to the same fraction",
   str(gmgn._norm_pct(17.83)))
ok(gmgn._norm_pct(None) is None and gmgn._norm_pct("") is None,
   "_norm_pct never turns 'missing' into 0")

set_state({"traders": [
    {"address": "W1", "maker_token_tags": ["whale", "sniper"], "tags": [],
     "buy_volume_cur": 9000, "sell_volume_cur": 0, "start_holding_at": 1},
    {"address": "W2", "maker_token_tags": [], "tags": ["renowned"],
     "buy_volume_cur": 100, "sell_volume_cur": 0, "start_holding_at": 2},
]})
ident = gmgn.top_trader_identity("Tagxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx")
ok(int(ident.get("whale") or 0) == 1 and int(ident.get("sniper") or 0) == 1,
   "whale/sniper read from maker_token_tags", str(ident))
ok(int(ident.get("kol") or 0) == 1, "'renowned' in tags counted as KOL", str(ident))

# ─────────────────────────────────────────────────────────────────────────────
section("8. holder rows: amount_percentage, addr_type, and curve/pool exclusion")
reset_provider()
set_state({"traders": [{"address": "T1", "maker_token_tags": ["smart_degen"],
                        "tags": ["smart_degen"], "buy_volume_cur": 500,
                        "start_holding_at": 1}],
           "holders": [POOL_ROW, BURN_ROW,
                       wallet_row("Dev999999999999999999999999999999999999999", 0.12,
                                  mtags=["sniper", "dev_team"], sold=0.8, pnl=1.4),
                       wallet_row("Retail11111111111111111111111111111111111111", 0.04,
                                  tags=["smart_degen"], sold=0.1, pnl=-0.2)]})
d = gmgn.holder_distribution("Holdersxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx")
ok(d.get("ok") is True, "holder_distribution ok", str(d.get("ok")))
ok(abs(float(d.get("top1_pct") or 0) - 0.12) < 1e-9,
   "top1_pct is the largest WALLET (12%), not the 62% AMM vault", str(d.get("top1_pct")))
ok(abs(float(d.get("top1_pct_all") or 0) - 0.62) < 1e-9,
   "the unfiltered top-1 (the vault) is still reported for transparency",
   str(d.get("top1_pct_all")))
exc = d.get("excluded_pools") or []
ok(len(exc) == 2, "the pool row and the burn row were excluded (addr_type 2 and 1)",
   str([(e.get("address", "")[:9], e.get("pct")) for e in exc]))
ok(abs(float(d.get("dex_pct") or 0) - 0.62) < 1e-9 and
   abs(float(d.get("burn_pct") or 0) - 0.03) < 1e-9,
   "burn/DEX shares reported", f"dex={d.get('dex_pct')} burn={d.get('burn_pct')}")
ok(abs(float(d.get("float_share") or 0) - 0.35) < 1e-9 and d.get("float_degenerate") is False,
   "tradeable float = 1 - burn - DEX = 35%, not degenerate", str(d.get("float_share")))
rows = {h["address"][:6]: h for h in (d.get("holders") or [])}
dev = rows.get("Dev999") or {}
ok(abs(float(dev.get("sell_pct") or -1) - 0.8) < 1e-9 and
   abs(float(dev.get("buy_pct") or -1) - 0.2) < 1e-9,
   "sell_amount_percentage mapped to sell_pct (80% of its buys already sold)",
   f"sell={dev.get('sell_pct')} held={dev.get('buy_pct')}")
ok(abs(float(dev.get("profit_ratio") or 0) - 1.4) < 1e-9,
   "unrealized_pnl mapped to profit_ratio", str(dev.get("profit_ratio")))
ok(dev.get("holding_days") is not None, "holding_days derived from start_holding_at",
   str(dev.get("holding_days")))
gmgn._CACHE.clear()
d_all = gmgn.holder_distribution("Holdersxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx",
                                 exclude_curve_ata=False)
ok(abs(float(d_all.get("top1_pct") or 0) - 0.62) < 1e-9,
   "exclude_curve_ata=False keeps the vault (the flag is real, not decorative)",
   str(d_all.get("top1_pct")))
deep = gmgn.deep_holder_analysis("Holdersxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx")
st = (deep or {}).get("stats") or {}
ok(int(st.get("sniper_count") or 0) == 1, "sniper counted from maker_token_tags",
   str({k: st.get(k) for k in ('sniper_count', 'bundler_count', 'smart_count')}))
ok(int(st.get("smart_count") or 0) == 1, "smart_degen counted from tags")
ok(int(st.get("top10_dumping") or 0) == 1 and int(st.get("top10_accumulating") or 0) == 1,
   "top-10 dumping/accumulating are no longer permanently 0",
   f"dump={st.get('top10_dumping')} acc={st.get('top10_accumulating')}")

# ─────────────────────────────────────────────────────────────────────────────
section("9. the rate limiter is config-driven (a hardcoded 0.8/s made tests take 3m)")
reset_provider()
set_state({})
set_gmgn_cfg(requests_per_sec=200, request_gap_ms=10)
ok(abs(float(gmgn._rate_per_sec() or 0) - 200.0) < 1e-6,
   "_rate_per_sec reads data_sources.gmgn.requests_per_sec", str(gmgn._rate_per_sec()))
ok(abs(float(gmgn._rl_min_gap() or 0) - 0.02) < 1e-6,
   "_rl_min_gap reads request_gap_ms and keeps a 20ms floor (never 0)",
   str(gmgn._rl_min_gap()))
ok(abs(float(gmgn._burst_capacity() or 0) - 2.5) < 1e-6,
   "_burst_capacity reads data_sources.gmgn.burst_capacity", str(gmgn._burst_capacity()))
t0 = time.time()
for n in range(4):
    gmgn.token_info(f"Rate{n}xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx")
fast_el = time.time() - t0
set_gmgn_cfg(requests_per_sec=0.8, request_gap_ms=350)
gmgn._CACHE.clear()
ok(abs(float(gmgn._rate_per_sec() or 0) - 0.8) < 1e-6 and
   abs(float(gmgn._rl_min_gap() or 0) - 0.35) < 1e-6,
   "and back to the shipped 0.8/s with a 350ms gap",
   f"{gmgn._rate_per_sec()}/s gap={gmgn._rl_min_gap()}s")
ok(fast_el < 2.0, f"4 calls at 200/s took {fast_el:.2f}s (was ~4.6s: the DB row kept "
                  f"the old 0.8/s and refilled from it)")
# The bug behind that number: db.rl_acquire inserted rate_per_sec ONCE and the
# refill expression read the STORED column, so changing the config did nothing.
from enzo.core import db as _db                                    # noqa: E402
with _db.db_cursor() as _cur:
    _row = _cur.execute("SELECT rate_per_sec, capacity FROM rate_limiter WHERE key = 'gmgn'"
                        ).fetchone()
ok(_row is not None and abs(float(_row["rate_per_sec"]) - 200.0) < 1e-6,
   "the stored limiter rate now FOLLOWS the config", str(dict(_row) if _row else None))
set_gmgn_cfg(burst_capacity=4.0)
gmgn.token_info("Burstxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx")
with _db.db_cursor() as _cur:
    _row2 = _cur.execute("SELECT capacity FROM rate_limiter WHERE key = 'gmgn'").fetchone()
ok(_row2 is not None and abs(float(_row2["capacity"]) - 4.0) < 1e-6,
   "and so does the burst capacity", str(dict(_row2) if _row2 else None))
set_gmgn_cfg(requests_per_sec=200, request_gap_ms=10, burst_capacity=2.5)  # keep it fast
gmgn._CACHE.clear()

# ─────────────────────────────────────────────────────────────────────────────
section("10. FULL PATH, NOTHING STUBBED: engine.scan_mint -> analyze -> gmgn -> decision")
# Regression guard for the bug where analyze() returned None (its decision tail
# was orphaned into rug_rejection) while 462 checks stayed green, because every
# engine test replaced run_pipeline with a lambda. Also proves the new gates and
# the holder-concentration cap fire inside the REAL pipeline.
reset_provider()
set_state({})
script = r'''
import json, os, sys, tempfile, shutil
ROOT = sys.argv[1]
sys.path.insert(0, ROOT); sys.path.insert(0, os.path.join(ROOT, "tests"))
SANDBOX = tempfile.mkdtemp(prefix="enzo-fullpath-")
os.makedirs(os.path.join(SANDBOX, "config"), exist_ok=True)
os.makedirs(os.path.join(SANDBOX, "data"), exist_ok=True)
shutil.copy(os.path.join(ROOT, "config", "enzo-config.yaml"),
            os.path.join(SANDBOX, "config", "enzo-config.yaml"))
for f in ("enzo-control.json",):
    p = os.path.join(ROOT, "config", f)
    if os.path.exists(p): shutil.copy(p, os.path.join(SANDBOX, "config", f))
os.environ["ENZO_HOME"] = SANDBOX
os.environ["ENZO_CONFIG_PATH"] = os.path.join(SANDBOX, "config", "enzo-config.yaml")
os.environ["GMGN_API_KEY"] = "test-key-not-real"
os.environ["MOCK_STATE"] = "{}"
from conftest_paths import install_mock_on_path
install_mock_on_path()
import yaml
from enzo.core import config as C
from enzo.core import engine, db
from enzo.analyzers import analyze
from enzo.providers import gmgn
from enzo.ui import botctl

# paper mode + a fast rate limit for the sandbox; nothing else is faked
p = os.environ["ENZO_CONFIG_PATH"]
doc = yaml.safe_load(open(p, encoding="utf-8"))
doc["paper_mode"] = True
g = doc.setdefault("data_sources", {}).setdefault("gmgn", {})
g["requests_per_sec"] = 200; g["request_gap_ms"] = 10
yaml.safe_dump(doc, open(p, "w", encoding="utf-8"), sort_keys=False, allow_unicode=True)
C._CFG_CACHE.update({"mtime": None, "size": None, "path": None, "cfg": None})
botctl.set_paused(False)

CLEAN_INFO = {"launchpad": "pump", "launchpad_platform": "Pump.fun",
              "launchpad_status": 1, "launchpad_progress": 0.35,
              "usd_market_cap": 62000, "market_cap": 62000, "liquidity": 18000,
              "holder_count": 900,
              "price": {"price": "0.0000123", "buys_24h": 400, "sells_24h": 180,
                        "volume_24h": "120000"}}
POOL = {"address": "CurveATA111111111111111111111111111111111111111",
        "amount_percentage": 0.62, "addr_type": 2, "tags": ["pool"]}

def w(addr, pct, mtags=(), sold=0.0):
    return {"address": addr, "amount_percentage": pct, "addr_type": 0,
            "tags": [], "maker_token_tags": list(mtags),
            "sell_amount_percentage": sold, "unrealized_pnl": 0.1,
            "buy_tx_count_cur": 2, "sell_tx_count_cur": 1, "start_holding_at": 1700000000}

FIXTURES = {
    # a healthy pre-migration Pump V1 coin, top wallet 6% (under the 10% cap)
    "clean": {"token_info": CLEAN_INFO, "holders": [POOL, w("W1" * 20, 0.06),
                                                    w("W2" * 20, 0.04)]},
    # one sniper bought $6.2k in the first seconds -> rug, never buy
    "flood": {"token_info": CLEAN_INFO,
              "holders": [POOL, w("W1" * 20, 0.06)],
              "traders": [{"address": "SN1", "maker_token_tags": ["sniper"],
                           "tags": ["sniper"], "buy_volume_cur": 6200,
                           "sell_volume_cur": 0, "start_holding_at": 1700000001,
                           "buy_tx_count_cur": 1}]},
    # top WALLET holds 12% of supply -> the owner's max_holder_percentage cap
    "conc_high": {"token_info": CLEAN_INFO,
                  "holders": [POOL, w("Dev" * 14, 0.12, ("dev_team",), 0.05),
                              w("W2" * 20, 0.04)]},
}
out = {}
for name, state in FIXTURES.items():
    mint = name.capitalize() + "x" * 40
    os.environ["GMGN_MOCK_STATE"] = json.dumps(state)
    gmgn._CACHE.clear()
    dec = analyze.run_pipeline(mint)
    d = dec if isinstance(dec, dict) else {}
    eng = {}
    try:
        e = engine.scan_mint(mint)
        eng = {k: (e or {}).get(k) for k in ("decision", "reason", "error", "size_usd")} \
            if isinstance(e, dict) else {"raw": str(e)}
    except Exception as ex:
        eng = {"error": f"{type(ex).__name__}: {ex}"}
    out[name] = {
        "type": type(dec).__name__,
        "decision": d.get("decision"),
        "reason": d.get("decision_reason"),
        "rejected": d.get("rejected_signals"),
        "supporting": d.get("supporting_signals"),
        "top_holder_pct": d.get("top_holder_pct"),
        "top_holder_source": d.get("top_holder_source"),
        "universe": d.get("universe"),
        "engine": eng,
    }
pos = db.get_full_state().get("positions")
pos = list(pos.values()) if isinstance(pos, dict) else (pos or [])
json.dump({"results": out,
           "positions": [str(p.get("token_symbol") or p.get("mint")) for p in pos]},
          sys.stdout)
'''
proc = subprocess.run([sys.executable, "-c", script, ROOT], capture_output=True, text=True,
                      timeout=900, cwd=ROOT)
payload = None
try:
    lines = [l for l in (proc.stdout or "").strip().splitlines() if l.strip().startswith("{")]
    payload = json.loads(lines[-1]) if lines else None
except Exception:
    payload = None
if payload is None:
    print((proc.stdout or "")[-2000:])
    print((proc.stderr or "")[-2000:])
ok(payload is not None, "the full-path subprocess produced a result")
res = (payload or {}).get("results") or {}
cl, fl, ch = res.get("clean") or {}, res.get("flood") or {}, res.get("conc_high") or {}

ok(cl.get("type") == "dict", "analyze.run_pipeline returns a DICT (not None)",
   str(cl.get("type")))
ok(str(cl.get("decision") or "") != "",
   f"a clean Pump V1 coin gets a real decision: {cl.get('decision')}", str(cl.get("reason"))[:80])
uni = cl.get("universe") or {}
ok(uni.get("pump_v1") is True, "the decision carries the universe verdict (pump_v1 True)",
   str({k: uni.get(k) for k in ('pump_v1', 'platform', 'phase')}))
ok(cl.get("top_holder_pct") is not None and cl.get("top_holder_source") == "holders.top1_wallet",
   f"holder concentration measured from the wallet list: {cl.get('top_holder_pct')}%",
   str(cl.get("top_holder_source")))
ok(not any("HOLDER_CONCENTRATION" in str(r) for r in (cl.get("rejected") or [])),
   "a 6% top wallet does NOT trip the 10% cap", str(cl.get("rejected"))[:120])

ok(str(fl.get("decision") or "") not in ("BUY", ""),
   f"a single $6.2k sniper in the first seconds is NOT a BUY ({fl.get('decision')})",
   str([r for r in (fl.get("rejected") or []) if "SNIPER" in str(r)])[:140])
ok(any("SNIPER_FLOOD" in str(r) for r in (fl.get("rejected") or [])),
   "the veto code SNIPER_FLOOD_* is in the rejection list", str(fl.get("rejected"))[:160])
ok(str((fl.get("engine") or {}).get("decision") or "") != "BUY",
   "engine.scan_mint refuses it too (same path the live loop takes)",
   str(fl.get("engine"))[:120])

ok(ch.get("top_holder_pct") is not None and float(ch.get("top_holder_pct") or 0) > 11.0,
   f"the 62% AMM vault was excluded, leaving the 12% wallet: {ch.get('top_holder_pct')}%",
   str(ch.get("top_holder_source")))
ok(any("HOLDER_CONCENTRATION" in str(r) for r in (ch.get("rejected") or [])),
   "max_holder_percentage=10 now VETOES a 12% top wallet (it was read and never used)",
   str([r for r in (ch.get("rejected") or []) if "HOLDER" in str(r)])[:160])
ok(str(ch.get("decision") or "") != "BUY",
   f"and the decision follows the veto: {ch.get('decision')}")
ok(not (payload or {}).get("positions"),
   "no ledger position was opened for any vetoed coin", str((payload or {}).get("positions")))

# ─────────────────────────────────────────────────────────────────────────────
print("\n" + "─" * 78)
for n in NOTICES:
    print(f"  NOTE  {n}")
print(f"\nGMGN CLI compatibility + full path: {PASS} passed, {FAIL} failed")
shutil.rmtree(SANDBOX, ignore_errors=True)
sys.exit(1 if FAIL else 0)
