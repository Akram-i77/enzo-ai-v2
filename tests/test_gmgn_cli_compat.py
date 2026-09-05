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
import re
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


def set_yaml(section, **kw):
    """Patch any top-level config section in the sandbox YAML."""
    path = os.environ["ENZO_CONFIG_PATH"]
    with open(path, encoding="utf-8") as fh:
        doc = yaml.safe_load(fh) or {}
    sec = doc.get(section) or {}
    sec.update(kw)
    doc[section] = sec
    with open(path, "w", encoding="utf-8") as fh:
        yaml.safe_dump(doc, fh, sort_keys=False, allow_unicode=True)
    C._CFG_CACHE.update({"mtime": None, "size": None, "path": None, "cfg": None})


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
    # Before any call: the flag must already say the key is missing. It used to
    # stay False until a CLI call died, so /api/state published
    # api_key_present=False together with api_key_missing=False.
    _pre = gmgn.provider_status()
    ok(_pre.get("api_key_present") is False and _pre.get("api_key_missing") is True,
       "provider_status is honest BEFORE any call (no present=False/missing=False pair)",
       f"present={_pre.get('api_key_present')} missing={_pre.get('api_key_missing')}")
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
    # near_completion, not new_creation: the shipped trenches_types asks for
    # near_completion + completed, and the mock (like the real API) filters by
    # --type and serves near_completion under the `pump` key.
    "trenches": {"near_completion": [{
        "address": "TrenchMint111111111111111111111111111111111", "symbol": "TRN",
        "launchpad_platform": "Pump.fun", "price": 0.000009, "usd_market_cap": 9000.0,
        "liquidity": 7000.0, "buys_24h": 120, "sells_24h": 40, "volume_24h": 21000.0,
        "progress": 0.31, "creator": "DevTrench111111111111111111111111111111111"}],
     "completed": []},   # isolate the injected stage from the mock's default rows
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
   "trenches parsed (near_completion, served under the API's `pump` key)", str(tr))
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
section("11. a GMGN ban is read from its REAL payload, and never probed or retried")
# The operator's live symptom, verbatim in the dashboard's Activity stream:
#   SNIPER_DATA_UNAVAILABLE: token traders failed: RateLimited:
#   token/traders: still banned
# The cause is a RATE-LIMIT ban: the scan loop sent ~63 GMGN requests per cycle
# (2 discovery + 5 per deep analysis x up to 12 analyses) and cycles ran
# back-to-back, i.e. ~48 req/min forever. Three defects then made the ban worse:
#   (a) ENZO looked for a "resets at <datetime>" string, but GMGN's documented
#       payload carries a UNIX timestamp - {"code":429,"error":"RATE_LIMIT_BANNED",
#       ...,"reset_at":1775184222} plus an X-RateLimit-Reset header - so the parse
#       fell back to a 30s guess and retried 30s into a ~5-minute ban;
#   (b) it waited and retried INSIDE the call, a third attempt after gmgn-cli had
#       already retried once by itself; GMGN's docs say every request during the
#       cooldown EXTENDS the ban by 5s ("Do not spam retries ... stop and tell the
#       user the exact retry time instead of sending more requests");
#   (c) when the retry was still banned it raised WITHOUT re-registering the ban,
#       so the next candidate probed GMGN again - one probe per coin per sweep.
reset_provider()
set_state({})

from datetime import datetime as _dt, timedelta as _td, timezone as _tz  # noqa: E402
import enzo.core.db as _db  # noqa: E402
import json as _json  # noqa: E402
import time as _time  # noqa: E402


def _txt(delta_sec, fmt):
    return fmt(_dt.now(_tz.utc) + _td(seconds=delta_sec))


# ── GMGN's documented ban payload: reset_at as a UNIX timestamp ──
for _label, _delta in [("300s (the typical ban)", 300), ("120s", 120), ("20s", 20)]:
    _payload = _json.dumps({"code": 429, "error": "RATE_LIMIT_BANNED",
                            "message": "too many requests",
                            "reset_at": int(_time.time() + _delta)})
    _w, _h = gmgn._ban_reset_wait(_payload)
    ok(abs(_w - _delta) <= 2 and _h == "reset_at",
       f"reset_at read from the real 429 body ({_label})", f"{_w:.0f}s / {_h}")

_w, _h = gmgn._ban_reset_wait(
    "X-RateLimit-Reset: %d" % int(_time.time() + 180))
ok(abs(_w - 180) <= 2 and _h == "reset_at", "the X-RateLimit-Reset header is read too",
   f"{_w:.0f}s / {_h}")
_w, _h = gmgn._ban_reset_wait(
    _json.dumps({"code": 429, "error": "RATE_LIMIT_BANNED",
                 "reset_at": int((_time.time() + 90) * 1000)}))
ok(abs(_w - 90) <= 2, "a millisecond reset_at is not mistaken for the year 55000",
   f"{_w:.0f}s")
_w, _h = gmgn._ban_reset_wait(_json.dumps({"code": 429, "error": "RATE_LIMIT_BANNED",
                                           "reset_at": 4102444800}))  # 2100
ok(abs(_w - gmgn.BAN_MAX_WAIT) < 1.0, "an absurd reset_at is clamped to 6h, not believed",
   f"{_w:.0f}s")

# ── legacy human-readable stamps still work (second choice) ──
_minute = _dt.now(_tz.utc).replace(second=0, microsecond=0) + _td(minutes=2)
_ban_cases = [
    ("…T18:30:00.000Z", _txt(120, lambda d: d.strftime("%Y-%m-%dT%H:%M:%S.000Z")), 120, 3),
    ("…+00:00", _txt(120, lambda d: d.strftime("%Y-%m-%dT%H:%M:%S+00:00")), 120, 3),
    ("… 18:30:00 UTC", _txt(120, lambda d: d.strftime("%Y-%m-%d %H:%M:%S UTC")), 120, 3),
    ("naive = machine local",
     (_dt.now().astimezone() + _td(seconds=120)).strftime("%Y-%m-%dT%H:%M:%S"), 120, 3),
    ("no seconds, Z", _minute.strftime("%Y-%m-%dT%H:%MZ"),
     (_minute - _dt.now(_tz.utc)).total_seconds(), 5),
]
for _label, _stamp, _want, _tol in _ban_cases:
    _w, _h = gmgn._ban_reset_wait(
        f"[gmgn-cli] Error: 429 RATE_LIMIT — You are BANNED, resets at {_stamp}")
    ok(abs(_w - _want) <= _tol and _h == "datetime",
       f"a printed datetime still works as fallback: '{_label}'", f"{_w:.0f}s / {_h}")

_w, _h = gmgn._ban_reset_wait("you are BANNED")
ok(abs(_w - gmgn.BAN_FALLBACK_WAIT) < 1.0 and _h.startswith("documented"),
   "no timestamp at all => GMGN's documented 5 minutes, NOT a 30s guess (which was "
   "retried into a live ban)", f"{_w:.0f}s / {_h}")

# ── the live path through the mock CLI: banned => fail fast, ONE call only ──
_ban_txt = _json.dumps({"code": 429, "error": "RATE_LIMIT_BANNED",
                        "message": "plan rate limit exceeded",
                        "reset_at": int(_time.time() + 120)})
_db.rl_report_ban("gmgn", ban_duration_sec=0)              # start from no ban
set_state({"fail": {"endpoint": "token/traders", "rc": 1, "err": _ban_txt}})
_before = len(argv_log())
_calls_before = gmgn.call_stats()["total"]
_raised = None
try:
    gmgn.token_traders_raw("BanTest11111111111111111111111111111111111")
except Exception as _e:                                   # noqa: BLE001
    _raised = _e
ok(isinstance(_raised, gmgn.RateLimited), "a banned call raises RateLimited",
   str(type(_raised).__name__))
_msg = str(_raised)
# The message must carry the ban name AND a clock time + a countdown, because
# "wait a while" is what the old code effectively said. The countdown is read
# back out of the message and allowed 115-120s: reset_at was computed a moment
# before formatting, so an exact 120 would be flaky.
_m_in = re.search(r"\(in (\d+)s", _msg)
ok("RATE_LIMIT_BANNED" in _msg and "retry at" in _msg and _m_in
   and 115 <= int(_m_in.group(1)) <= 120,
   "and it names the ban plus the EXACT retry time GMGN asked for",
   _msg[:120] + f"  [countdown={_m_in.group(1) if _m_in else '?'}s]")
ok(len(argv_log()) - _before == 1,
   "exactly ONE CLI call: ENZO does not wait-and-retry a third time (gmgn-cli already "
   "retried once, and each request during the cooldown adds 5s to the ban)",
   f"{len(argv_log()) - _before} call(s)")
_left = gmgn.ban_status()
ok(110 <= _left <= 125, "the ban is registered for GMGN's own reset_at, not a guess",
   f"{_left:.0f}s left")
ok(gmgn.call_stats()["bans"] >= 1, "the ban is counted so the volume is visible",
   _json.dumps(gmgn.call_stats()))
ok(gmgn.call_stats()["total"] == _calls_before + 1, "and the request counter tracks it",
   f"{gmgn.call_stats()['total']} total")

_before = len(argv_log())
_raised2 = None
try:
    gmgn.token_traders_raw("BanTest22222222222222222222222222222222222")
except Exception as _e:                                   # noqa: BLE001
    _raised2 = _e
ok(isinstance(_raised2, gmgn.RateLimited) and "rate limited or banned" in str(_raised2),
   "while the ban stands the NEXT candidate is refused without probing GMGN",
   str(_raised2)[:80])
ok(len(argv_log()) == _before, "zero CLI calls for that candidate (no ban-probing sweep)",
   f"{len(argv_log()) - _before} extra call(s)")

_rep = gmgn.early_sniper_report("BanTest11111111111111111111111111111111111")
ok(_rep.get("verdict") == "unknown" and "banned" in str(_rep.get("reason")).lower(),
   "so the sniper report is 'unknown' naming the ban - never a silent pass",
   str(_rep.get("reason"))[:100])

set_state({})
# rl_report_ban(..., 0) only ever EXTENDS a ban (max(banned_until, new)) - it does
# not clear it. Without rl_clear_ban the ban from this section leaks into every
# later one and all discovery calls come back RateLimited.
_db.rl_clear_ban("gmgn")
reset_provider()

# ─────────────────────────────────────────────────────────────────────────────
section("12. `market trending` gets the --interval it REQUIRES (it failed every cycle)")
# gmgn-cli v1.6.1 declares TWO required options for `market trending`:
#   .requiredOption("--chain <chain>")
#   .requiredOption("--interval <interval>", "1m / 5m / 1h / 6h / 24h")
# ENZO built ["market","trending","--chain",ch,"--limit",N,"--platform",...], so
# commander aborted with "required option '--interval <interval>' not specified"
# BEFORE any HTTP call. `trending` was listed in the config as a discovery source
# and returned zero tokens on every cycle of the bot's life, while the only trace
# was one warning line per cycle. 700+ checks stayed green because this mock did
# not reproduce the requirement - it does now.
reset_provider()
# A DISTINCT trending token: the mock's default payload reuses one address for both
# categories, and discovery de-duplicates by mint - so without this the trending
# token would be attributed to trenches and the provenance check would prove
# nothing.
set_state({"trending": {"rank": [{
    "address": "TrendFix333333333333333333333333333333333", "symbol": "TRD",
    "launchpad_platform": "Pump.fun", "exchange": "pump_amm", "price": 0.000031,
    "market_cap": 31000.0, "liquidity": 42000.0, "buys": 1500, "sells": 700,
    "volume": 180000.0, "creator": "DevTrend333333333333333333333333333333333"}]}})
_db.rl_clear_ban("gmgn")          # a leftover ban would refuse every call below
set_gmgn_cfg(discovery=["trenches", "trending"], trending_interval="1m",
             discovery_limit=30)
items = gmgn.discover("sol")
mk = [a for a in argv_log() if len(a) > 1 and a[0] == "market"]
td_args = next((a for a in mk if a[1] == "trending"), [])
ok("--interval" in td_args and td_args[td_args.index("--interval") + 1] == "1m",
   "trending is called with --interval 1m (data_sources.gmgn.trending_interval)",
   str(td_args))
ok(td_args and td_args[td_args.index("--limit") + 1] == "30",
   "and with discovery_limit=30 (was 50)", str(td_args))
_cats = gmgn.discovery_status().get("categories_ok") or {}
ok((_cats.get("trending") or {}).get("ok") is True,
   "so the trending category now SUCCEEDS instead of failing every cycle",
   str(_cats.get("trending")))
ok(any(str(it.get("source")) == "trending" for it in items),
   "and its tokens really reach the candidate list", str({it.get("source") for it in items}))

# The mock must refuse exactly like the real CLI, or this bug can come back.
_p = subprocess.run([shutil.which("gmgn-cli"), "market", "trending", "--chain", "sol",
                     "--limit", "30", "--raw"], capture_output=True, text=True,
                    env={**os.environ, "GMGN_MOCK_STATE": "{}"}, timeout=60)
ok(_p.returncode != 0 and "required option '--interval <interval>' not specified"
   in (_p.stderr or ""),
   "the mock CLI now mirrors the real one: trending without --interval is an error "
   "(this is the check that was missing)", (_p.stderr or "").strip()[:90])

# An invalid interval must be refused HERE, with a readable reason, not sent.
reset_provider()
set_gmgn_cfg(discovery=["trending"], trending_interval="2m")
_bad_items = gmgn.discover("sol")
_cats2 = gmgn.discovery_status().get("categories_ok") or {}
_td2 = _cats2.get("trending") or {}
ok(_td2.get("ok") is False and "1m/5m/1h/6h/24h" in str(_td2.get("error")),
   "an interval GMGN does not have is refused with the valid list named",
   str(_td2.get("error"))[:120])
ok(not [a for a in argv_log() if len(a) > 1 and a[1] == "trending"],
   "and no CLI call is wasted on it", str(argv_log()[:2]))

# Restore the shipped values for the sections that follow.
reset_provider()
set_state({})
_db.rl_clear_ban("gmgn")
set_gmgn_cfg(discovery=["trenches", "trending"], trending_interval="1m",
             discovery_limit=30)

# ─────────────────────────────────────────────────────────────────────────────
section("13. `market kline` shapes: the candle axis was blind, then fatal")
# gmgn-cli v1.6.1 documents kline as an OBJECT with a `list` array whose rows are
# objects with STRING numbers and `time` in MILLISECONDS. kline() returned whatever
# `data` happened to be, so:
#   * against the real CLI it returned a dict -> every consumer saw len()==1 and
#     gave up: the candle axis contributed NOTHING, silently;
#   * against array rows (older builds, the bundled mock) it returned a list of
#     LISTS -> market_structure's first `c.get("close")` raised AttributeError,
#     which escaped axis -> analyze -> run_pipeline and turned the WHOLE decision
#     into ANALYSIS_ERROR / hard_reject ['EXCEPTION']. It only fires from the
#     SECOND scan of a mint (the first returns early on "insufficient_samples"),
#     i.e. precisely the coins the engine looks at again - and the rejection looks
#     like a data-source fault, not a code bug.
from enzo.analyzers import market_structure as _ms  # noqa: E402
from enzo.analyzers import analyze as _az  # noqa: E402

reset_provider()
set_state({})
_db.rl_clear_ban("gmgn")
_KM = "KlineMint1111111111111111111111111111111111"

# ── the documented shape ──
_now = int(time.time())
_kl = gmgn.kline(_KM, "5m", from_ts=_now - 1860, to_ts=_now)
ok(isinstance(_kl, list) and len(_kl) == 5,
   "the documented envelope {data:{list:[...]}} is unwrapped into a LIST of candles "
   "(kline() used to return the dict itself)", f"{type(_kl).__name__} len={len(_kl)}")
ok(all(isinstance(r, dict) for r in _kl), "every row is a dict, never an array",
   str(_kl[:1]))
_r0 = _kl[0] if _kl else {}
ok(isinstance(_r0.get("close"), float) and isinstance(_r0.get("volume"), float),
   "the API's STRING numbers are parsed into floats", str(_r0))
ok(_r0.get("time") and _r0["time"] < 1e11,
   "and `time` is converted from milliseconds to Unix seconds", str(_r0.get("time")))
ok(not isinstance(_kl, dict), "kline() never returns a dict again", "")

# A kline entry CACHED by the previous revision (raw dict / array rows) must not be
# handed back as-is: the operator's live DB still holds such entries.
gmgn._cache_set(f"kline:{_KM}:5m", {"list": [
    {"time": (_now - 300) * 1000, "open": "0.0000080", "close": "0.0000088",
     "high": "0.0000090", "low": "0.0000075", "volume": "1200000",
     "amount": "5379110"},
    {"time": (_now - 240) * 1000, "open": "0.0000081", "close": "0.0000089",
     "high": "0.0000091", "low": "0.0000076", "volume": "1300000",
     "amount": "5380110"}]}, ttl=120)
_stale = gmgn.kline(_KM, "5m")
ok(isinstance(_stale, list) and len(_stale) == 2
   and all(isinstance(r, dict) and isinstance(r.get("close"), float) for r in _stale),
   "a stale CACHE entry from the previous revision (raw {list:[...]}) is normalized "
   "on the way out too", str(_stale[:1])[:120])
gmgn._CACHE.pop(f"kline:{_KM}:5m", None)

# ── the older array shape ──
reset_provider()
set_state({"kline_arrays": True})
_kl2 = gmgn.kline(_KM, "5m", from_ts=_now - 1860, to_ts=_now)
ok(isinstance(_kl2, list) and len(_kl2) == 5 and all(isinstance(r, dict) for r in _kl2),
   "bare array rows [[ts,o,h,l,c,v],...] normalize to the SAME dict shape",
   str(_kl2[:1]))
ok(abs(float(_kl2[0].get("close") or 0) - float(_kl[0].get("close") or 0)) < 1e-9,
   "and to the same close price as the documented shape",
   f"{_kl2[0].get('close')} vs {_kl[0].get('close')}")

# ── the axis is no longer blind ──
set_state({})
reset_provider()
_kt = _ms._kline_volume_trend(_KM)
ok(bool(_kt) and _kt.get("green_ratio") is not None and _kt.get("vol_trend") is not None,
   "the candle axis now yields green_ratio / vol_trend (it was {} against the real CLI)",
   str(_kt))

# ── the regression that mattered: the SECOND look must not explode ──
set_yaml("market_structure", min_sample_interval_sec=0)
_ms.clear(_KM)
reset_provider()
_d1 = _az.run_pipeline(_KM)
_d2 = _az.run_pipeline(_KM)      # series >= 2 -> reaches the candle axis
ok(_d1.get("decision") != "ANALYSIS_ERROR",
   "first look: a real decision", str(_d1.get("decision")))
ok(_d2.get("decision") != "ANALYSIS_ERROR"
   and "EXCEPTION" not in (_d2.get("hard_reject") or []),
   "SECOND look at the same mint: still a real decision - it used to come back "
   "ANALYSIS_ERROR / hard_reject ['EXCEPTION'] from an AttributeError in the candle "
   "axis, i.e. the coin was rejected by a code bug",
   f"{_d2.get('decision')} | {str(_d2.get('decision_reason'))[:70]}")
_ms2 = ((_d2.get("axis_scores") or {}).get("market_structure") or {})
ok(_ms2.get("available") is True and int((_ms2.get("detail") or {}).get("samples") or 0) >= 2,
   "and the market-structure axis really ran on >=2 samples instead of falling back "
   "to 'insufficient_samples'", str(_ms2.get("detail"))[:110])
_ms.clear(_KM)
set_yaml("market_structure", min_sample_interval_sec=60)
reset_provider()
set_state({})

# ─────────────────────────────────────────────────────────────────────────────
section("14. trenches stages: --type is sent, and `data.pump` is not dropped")
# Two defects, both silent, both verified against gmgn-cli's own schema docs:
#  (a) the API returns the near_completion category under the key **data.pump**
#      ("In the response, near_completion is always returned under the key data.pump
#      regardless of the input --type"), and ENZO's _LIST_SHAPES looked for
#      "near_completion" - so the whole stage was dropped on every cycle against the
#      real CLI. The bundled mock emitted "near_completion", which the real API never
#      sends, so no test could ever notice.
#  (b) `market trenches --type` selects the lifecycle stages (repeatable; default all
#      three). ENZO never sent it. The owner removed new_creation, so the filter has
#      to be real: without (a) fixed first, dropping new_creation would have left
#      ONLY `completed`, silently losing the stage the owner asked to keep.
reset_provider()
set_state({})
_db.rl_clear_ban("gmgn")
set_gmgn_cfg(discovery=["trenches"], trenches_types=["near_completion", "completed"])
_items = gmgn.discover("sol")
_mk = [a for a in argv_log() if len(a) > 1 and a[0] == "market" and a[1] == "trenches"]
_tr = _mk[0] if _mk else []
_types = [_tr[i + 1] for i, x in enumerate(_tr) if x == "--type"]
ok(_types == ["near_completion", "completed"],
   "trenches is called with the repeatable --type filter the owner configured",
   str(_tr))
ok("new_creation" not in _types,
   "new_creation is NOT requested (the owner's instruction)", str(_types))
_syms = {str(it.get("symbol")) for it in _items}
ok("NEAR" in _syms,
   "the near_completion stage arrives under the API's own key `data.pump` and IS "
   "parsed (it used to be dropped whole)", str(_syms))
ok("DONE" in _syms, "and the completed stage arrives too", str(_syms))
ok("MOCK" not in _syms,
   "the new_creation-only token is really gone from the candidate list", str(_syms))
_near = next((it for it in _items if str(it.get("symbol")) == "NEAR"), {})
ok(str(_near.get("source")) == "trenches", "provenance still says trenches", str(_near.get("source")))

# The legacy spelling must keep working (older builds / injected states).
reset_provider()
set_state({"trenches": {"near_completion": [{
    "address": "LegacyNear444444444444444444444444444444444", "symbol": "LGN",
    "launchpad_platform": "Pump.fun", "usd_market_cap": 52000.0, "liquidity": 9000.0,
    "volume_24h": 40000.0, "buys_24h": 300, "sells_24h": 120, "progress": 0.9}]}})
_leg = gmgn.discover("sol")
ok(any(str(it.get("symbol")) == "LGN" for it in _leg),
   "a payload that spells the stage `near_completion` is parsed as well",
   str({it.get("symbol") for it in _leg}))

# An empty list means "CLI default" - no --type flag at all.
reset_provider()
set_state({})
set_gmgn_cfg(discovery=["trenches"], trenches_types=[])
gmgn.discover("sol")
_tr2 = next((a for a in argv_log()
             if len(a) > 1 and a[0] == "market" and a[1] == "trenches"), [])
ok("--type" not in _tr2, "an empty trenches_types sends no --type (all three stages)",
   str(_tr2))
_syms2 = {str(it.get("symbol")) for it in gmgn.discover("sol")}
ok({"MOCK", "NEAR", "DONE"} <= _syms2, "and all three stages come back", str(_syms2))

# A stage GMGN does not have must be refused locally, with the valid list named.
reset_provider()
set_gmgn_cfg(discovery=["trenches"], trenches_types=["near_completion", "brand_new"])
_bad = gmgn.discover("sol")
_cat = (gmgn.discovery_status().get("categories_ok") or {}).get("trenches") or {}
ok(_cat.get("ok") is False and "new_creation/near_completion/completed"
   in str(_cat.get("error")),
   "an unknown stage is refused with the valid stages named", str(_cat.get("error"))[:130])
ok(not [a for a in argv_log() if len(a) > 1 and a[1] == "trenches"],
   "and no CLI call is wasted on it", str(argv_log()[:1]))

# Restore the shipped values.
reset_provider()
set_state({})
_db.rl_clear_ban("gmgn")
set_gmgn_cfg(discovery=["trenches", "trending"], trending_interval="1m",
             discovery_limit=30, trenches_types=["near_completion", "completed"])

# ─────────────────────────────────────────────────────────────────────────────
print("\n" + "─" * 78)
for n in NOTICES:
    print(f"  NOTE  {n}")
print(f"\nGMGN CLI compatibility + full path: {PASS} passed, {FAIL} failed")
shutil.rmtree(SANDBOX, ignore_errors=True)
sys.exit(1 if FAIL else 0)
