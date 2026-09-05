#!/usr/bin/env python3
"""enzoctl doctor + probe: the operator's two diagnostic tools must stay honest.

`doctor` is what the owner (and OpenClaw) runs when something looks wrong, and
`probe <mint>` is the only place that shows what the new trading gates actually
see for one coin. Both are user-facing truth-tellers, so both are pinned here:

* a missing GMGN_API_KEY must be a ✖, not a silent empty market
* an unverifiable check must be ⚠ (never ✔)
* unknown discovery categories (market smartmoney / kol, removed in gmgn-cli
  v1.6) must be called out instead of burning a rate-limit slot every cycle
* the owner-set gate thresholds must be echoed back (Pump V1 only, phase floors,
  sniper flood, holder-concentration cap)
* `probe` must print the live numbers AND the real pipeline verdict, so the tool
  and the trading loop can never disagree

Run:  python3 tests/test_enzoctl_probe.py
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile

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


SANDBOX = tempfile.mkdtemp(prefix="enzo-enzoctl-")
os.makedirs(os.path.join(SANDBOX, "config"), exist_ok=True)
os.makedirs(os.path.join(SANDBOX, "data"), exist_ok=True)
shutil.copy(os.path.join(ROOT, "config", "enzo-config.yaml"),
            os.path.join(SANDBOX, "config", "enzo-config.yaml"))

from conftest_paths import install_mock_on_path  # noqa: E402
MOCKBIN = install_mock_on_path()
PY = sys.executable

# A migrated Pump V1 coin with 4 snipers in the first seconds ($5.8k combined),
# 4.12 SOL of fees paid, an AMM vault holding 41% and a 7% top wallet.
STATE = {
    "token_info": {
        "symbol": "BONKX", "name": "Probe Coin", "launchpad": "pump",
        "launchpad_platform": "Pump.fun", "launchpad_status": 2,
        "launchpad_progress": 1.0, "creation_timestamp": 1767580000,
        "usd_market_cap": 42000, "market_cap": 42000, "liquidity": 26000,
        "holder_count": 812,
        "dev": {"creator_address": "DevProbe11111111111111111111111111111111111"},
        "price": {"price": "0.000042", "buys_24h": 910, "sells_24h": 402,
                  "volume_24h": "230000"},
    },
    "holders": [
        {"address": "AmmVault1111111111111111111111111111111111111", "amount_percentage": 0.41,
         "addr_type": 2, "tags": ["pool"]},
        {"address": "Burn1111111111111111111111111111111111111111", "amount_percentage": 0.02,
         "addr_type": 1, "tags": []},
        {"address": "Whale111111111111111111111111111111111111111", "amount_percentage": 0.07,
         "addr_type": 0, "tags": ["smart_degen"], "maker_token_tags": ["whale"],
         "sell_amount_percentage": 0.15, "unrealized_pnl": 0.9,
         "start_holding_at": 1767580400},
        {"address": "Retail11111111111111111111111111111111111111", "amount_percentage": 0.03,
         "addr_type": 0, "tags": [], "maker_token_tags": [], "sell_amount_percentage": 0.05,
         "start_holding_at": 1767581000},
    ],
    "traders": [
        {"address": f"Sniper{i}" + "1" * 38, "maker_token_tags": ["sniper"], "tags": ["sniper"],
         "buy_volume_cur": amt, "sell_volume_cur": 0, "start_holding_at": 1767580000 + i * 2,
         "buy_tx_count_cur": 1}
        for i, amt in enumerate([2400, 1800, 900, 700], start=1)
    ] + [{"address": "Retail5" + "5" * 39, "maker_token_tags": [], "tags": [],
          "buy_volume_cur": 120, "sell_volume_cur": 0, "start_holding_at": 1767580600,
          "buy_tx_count_cur": 2}],
    "created_tokens": {"tokens": [{
        "token_address": "ProbeMint11111111111111111111111111111111111",
        "symbol": "BONKX", "total_fee": 4.12, "coin_creator_fee": 2.06,
        "is_open": True, "market_cap": 42000.0}]},
}
CLEAN_STATE = {
    "token_info": {
        "symbol": "CLEAN", "name": "Clean Coin", "launchpad": "pump",
        "launchpad_platform": "Pump.fun", "launchpad_status": 1,
        "launchpad_progress": 0.4, "creation_timestamp": 1767580000,
        "usd_market_cap": 62000, "market_cap": 62000, "liquidity": 18000,
        "holder_count": 900,
        "price": {"price": "0.0000123", "buys_24h": 400, "sells_24h": 180,
                  "volume_24h": "120000"},
    },
    "holders": [
        {"address": "CurveATA1111111111111111111111111111111111111", "amount_percentage": 0.60,
         "addr_type": 2, "tags": ["pool"]},
        {"address": "W1" + "1" * 42, "amount_percentage": 0.06, "addr_type": 0, "tags": [],
         "maker_token_tags": [], "sell_amount_percentage": 0.1, "start_holding_at": 1767580400},
    ],
    "traders": [{"address": f"R{i}" + "9" * 40, "maker_token_tags": [], "tags": [],
                 "buy_volume_cur": 200, "sell_volume_cur": 0,
                 "start_holding_at": 1767580000 + i * 60, "buy_tx_count_cur": 1}
                for i in range(1, 9)],
}
MINT = "ProbeMint11111111111111111111111111111111111"
CLEAN_MINT = "CleanMint1111111111111111111111111111111111"


def run_ctl(*args, state=None, api_key="mock-key", config_mutator=None, timeout=300):
    """Run ./enzoctl <args> against the sandbox, return (rc, parsed_json, text)."""
    cfg_path = os.path.join(SANDBOX, "config", "enzo-config.yaml")
    if config_mutator is not None:
        import yaml
        with open(cfg_path, encoding="utf-8") as fh:
            doc = yaml.safe_load(fh) or {}
        config_mutator(doc)
        with open(cfg_path, "w", encoding="utf-8") as fh:
            yaml.safe_dump(doc, fh, sort_keys=False, allow_unicode=True)
    env = dict(os.environ)
    env.update({
        "ENZO_HOME": SANDBOX, "ENZO_CONFIG_PATH": cfg_path,
        "PATH": MOCKBIN + os.pathsep + env.get("PATH", ""),
        "MOCK_STATE": "{}",
        "GMGN_MOCK_STATE": json.dumps(state if state is not None else {}),
        "NO_COLOR": "1",
    })
    if api_key is None:
        env.pop("GMGN_API_KEY", None)
    else:
        env["GMGN_API_KEY"] = api_key
    proc = subprocess.run([PY, os.path.join(ROOT, "enzoctl"), *args],
                          capture_output=True, text=True, timeout=timeout, env=env, cwd=ROOT)
    text = (proc.stdout or "") + (proc.stderr or "")
    out = proc.stdout or ""
    try:
        parsed = json.loads(out)
    except Exception:
        # Tolerant: decode the first complete JSON object anywhere in stdout.
        # enzoctl --json prints one indented object, so scanning line-by-line
        # for '{' picks up a nested brace instead.
        parsed, dec = None, json.JSONDecoder()
        for idx, ch in enumerate(out):
            if ch != "{":
                continue
            try:
                parsed, _end = dec.raw_decode(out[idx:])
                break
            except Exception:
                continue
    return proc.returncode, parsed, text


# ─────────────────────────────────────────────────────────────────────────────
section("1. enzoctl probe — the gates' own numbers, from the live provider")
rc, js, txt = run_ctl("probe", MINT, "--json", state=STATE)
ok(js is not None, "probe --json produced a payload", txt[-300:] if js is None else "")
ok(rc == 1, f"a vetoed coin exits non-zero (rc={rc}) so scripts can rely on it")
prov = (js or {}).get("provider") or {}
ok(bool(prov.get("bin")), "probe reports which gmgn-cli it used", str(prov.get("bin")))
ok((prov.get("dialect") or {}).get("flag") == "--address",
   "probe reports the CLI's address flag (v1.6 dialect)",
   str((prov.get("dialect") or {}).get("flag")))
ok((prov.get("status") or {}).get("api_key_present") is True, "probe confirms the API key")
nums = (js or {}).get("numbers") or {}
ok(abs(float(nums.get("market_cap_usd") or 0) - 42000) < 1, "market cap read: $42,000",
   str(nums.get("market_cap_usd")))
ok(abs(float(nums.get("sells_24h") or 0) - 402) < 1, "sells read: 402 (the pre-migration gate)",
   str(nums.get("sells_24h")))
uni = ((js or {}).get("decision") or {}).get("universe") or {}
ok(uni.get("pump_v1") is True and uni.get("phase") == "migrated",
   "Pump V1 + migrated phase detected", str({k: uni.get(k) for k in ('pump_v1', 'phase')}))
sn = (js or {}).get("snipers") or {}
ok(sn.get("ok") is True and int(sn.get("sniper_count") or 0) == 4,
   "the first-8 window found 4 sniper wallets", str(sn.get("sniper_count")))
ok(abs(float(sn.get("sniper_total_usd") or 0) - 5800) < 1,
   "their combined size is $5,800", str(sn.get("sniper_total_usd")))
ok(abs(float(sn.get("max_single_usd") or 0) - 2400) < 1,
   "and the largest single buy is $2,400", str(sn.get("max_single_usd")))
win = sn.get("window") or []
ok(win and win[0].get("seconds_after_open") is not None,
   "each wallet in the window carries 'seconds after open'",
   str(win[0].get("seconds_after_open")) if win else "no window")
fe = (js or {}).get("fees") or {}
ok(fe.get("ok") is True and abs(float(fe.get("value") or 0) - 4.12) < 1e-9,
   "fees paid read from the dev's launch book: 4.12", str(fe.get("value")))
ok(str(fe.get("unit") or "").lower() == "sol" and fe.get("source") == "portfolio/created-tokens",
   "with the declared unit and its source", f"{fe.get('unit')} / {fe.get('source')}")
ho = (js or {}).get("holders") or {}
ok(abs(float(ho.get("top1_pct") or 0) - 0.07) < 1e-9,
   "top-1 WALLET is 7% (the 41% AMM vault was excluded)", str(ho.get("top1_pct")))
ok(abs(float(ho.get("top1_pct_all") or 0) - 0.41) < 1e-9,
   "and the excluded vault is still shown for transparency", str(ho.get("top1_pct_all")))
ok(len(ho.get("excluded_pools") or []) == 2, "the vault and the burn address are listed",
   str([e.get("pct") for e in (ho.get("excluded_pools") or [])]))
ok(abs(float(ho.get("float_share") or 0) - 0.57) < 1e-9,
   "tradeable float reported (1 - burn - DEX)", str(ho.get("float_share")))
dec = (js or {}).get("decision") or {}
rej = [str(r) for r in (dec.get("rejected_signals") or [])]
ok(any("SNIPER_FLOOD" in r for r in rej), "probe shows the pipeline's actual veto",
   str(rej)[:150])
ok(str(dec.get("decision") or "") != "BUY", f"and its decision follows: {dec.get('decision')}")
th = (js or {}).get("thresholds") or {}
ok((th.get("phase_gates") or {}).get("migrated", {}).get("min_total_fees") == 2.5,
   "probe echoes the configured thresholds next to the numbers",
   str((th.get("phase_gates") or {}).get("migrated")))

# human-readable mode must carry the same evidence
rc2, _, txt2 = run_ctl("probe", MINT, state=STATE)
for needle in ("Early snipers", "Global fees paid", "Holder concentration",
               "Pipeline verdict", "SNIPER_FLOOD_EARLY", "4.12 SOL", "7.0%"):
    ok(needle in txt2, f"human output shows '{needle}'")
ok("tradeable float" in txt2, "human output explains the float basis")

# --no-deep must skip the two extra calls and still answer
rc3, js3, txt3 = run_ctl("probe", MINT, "--json", "--no-deep", state=STATE)
ok(js3 is not None and (js3.get("snipers") is None and js3.get("fees") is None),
   "--no-deep skips the sniper and fee lookups",
   str({k: js3.get(k) for k in ('snipers', 'fees')} if js3 else None))
ok(bool((js3 or {}).get("decision")), "--no-deep still returns the pipeline verdict")

# a clean coin must NOT exit non-zero
rc4, js4, _ = run_ctl("probe", CLEAN_MINT, "--json", state=CLEAN_STATE)
dec4 = (js4 or {}).get("decision") or {}
ok(not (dec4.get("rejected_signals") or []),
   f"a clean Pump V1 coin has no veto (decision={dec4.get('decision')})",
   str(dec4.get("rejected_signals"))[:150])
ok(rc4 == 0, f"and probe exits 0 for it (rc={rc4})")

# ─────────────────────────────────────────────────────────────────────────────
section("2. enzoctl doctor — the new GMGN + gate checks")
rc, js, txt = run_ctl("doctor", "--json", state=STATE)
checks = {c["name"]: c for c in ((js or {}).get("checks") or [])} if js else {}
if not checks:
    # doctor --json may nest the list; fall back to the text output
    print(txt[-400:])
for name in ("gmgn_cli", "gmgn_api_key", "gmgn_cli_dialect", "gmgn_discovery_categories",
             "gmgn_rate_config", "universe_gates", "holder_concentration_cap",
             "momentum_windows"):
    ok(name in checks, f"doctor reports '{name}'", str(list(checks)[:6]) if not checks else "")
ok((checks.get("gmgn_api_key") or {}).get("ok") is True, "API key present => ok")
ok((checks.get("gmgn_cli_dialect") or {}).get("ok") is True and
   "--address" in str((checks.get("gmgn_cli_dialect") or {}).get("detail")),
   "dialect check names the accepted flag",
   str((checks.get("gmgn_cli_dialect") or {}).get("detail"))[:90])
ok((checks.get("universe_gates") or {}).get("ok") is True and
   "Pump V1 only=True" in str((checks.get("universe_gates") or {}).get("detail")),
   "the owner's gate settings are echoed back",
   str((checks.get("universe_gates") or {}).get("detail"))[:150])
ok((checks.get("holder_concentration_cap") or {}).get("ok") is True and
   "10.0%" in str((checks.get("holder_concentration_cap") or {}).get("detail")),
   "the holder cap is reported as configured",
   str((checks.get("holder_concentration_cap") or {}).get("detail"))[:120])

# The momentum axis is what decides "is this coin moving up right now"; it scored
# 1h/24h while the provider read neither (a constant 50). The owner moved it to
# 1m/5m, so doctor has to echo the windows and weights that judge the money.
_c = checks.get("momentum_windows") or {}
ok(_c.get("ok") is True and "1m" in str(_c.get("detail")) and "5m" in str(_c.get("detail")),
   "doctor names the momentum windows that are really scored",
   str(_c.get("detail"))[:150])
ok("x8" in str(_c.get("detail")) and "x3" in str(_c.get("detail")),
   "with the owner's weights per +1% (8.0 / 3.0 from the shipped config)",
   str(_c.get("detail"))[:150])
ok("context only" in str(_c.get("detail")),
   "and it says out loud that 1h/24h are context, not score", str(_c.get("detail"))[:160])

rc, js, txt = run_ctl("doctor", "--json", state=STATE,
                      config_mutator=lambda d: d.setdefault("market_analysis", {})
                      .update(momentum={"weight_1m": "fast", "weight_5m": 3.0}))
checks = {c["name"]: c for c in ((js or {}).get("checks") or [])} if js else {}
_c = checks.get("momentum_windows") or {}
ok(_c.get("ok") is False and "not numbers" in str(_c.get("detail")),
   "a non-numeric weight is a failed check, not a silent fallback",
   str(_c.get("detail"))[:150])
ok(_c.get("critical") is False, "but it does not stop the bot (the axis has defaults)")

# no API key -> a hard ✖ with the fix, and doctor exits non-zero
rc, js, txt = run_ctl("doctor", "--json", api_key=None, state=STATE)
checks = {c["name"]: c for c in ((js or {}).get("checks") or [])} if js else {}
k = checks.get("gmgn_api_key") or {}
ok(k.get("ok") is False and "GMGN_API_KEY" in str(k.get("detail")),
   "a missing API key is a FAIL naming the variable", str(k.get("detail"))[:110])
ok(bool(k.get("fix")), "and it comes with a fix", str(k.get("fix"))[:100])
ok(rc != 0, f"doctor exits non-zero when a critical check fails (rc={rc})")

# unknown discovery categories -> warning, not a silent rate-limit burn
rc, js, txt = run_ctl("doctor", "--json", state=STATE,
                      config_mutator=lambda d: d["data_sources"]["gmgn"].update(
                          discovery=["trenches", "smartmoney", "kol"]))
checks = {c["name"]: c for c in ((js or {}).get("checks") or [])} if js else {}
c = checks.get("gmgn_discovery_categories") or {}
ok(c.get("ok") is False and "smartmoney" in str(c.get("detail")),
   "dead discovery categories are called out by name", str(c.get("detail"))[:130])
ok(c.get("critical") is False, "but they are a warning, not a stop-the-bot failure")

# pump_v1_only switched off must be visible as a failed check
rc, js, txt = run_ctl("doctor", "--json", state=STATE,
                      config_mutator=lambda d: d["token_universe"].update(pump_v1_only=False))
checks = {c["name"]: c for c in ((js or {}).get("checks") or [])} if js else {}
ok((checks.get("universe_gates") or {}).get("ok") is False,
   "turning off 'Pump V1 only' shows up as a failed check")

# an unverified discovery sweep must be ⚠, never ✔
rc, js, txt = run_ctl("doctor", "--json", state=STATE)
checks = {c["name"]: c for c in ((js or {}).get("checks") or [])} if js else {}
d = checks.get("gmgn_discovery") or {}
ok(d.get("ok") is False and "no discovery sweep" in str(d.get("detail")),
   "with no sweep yet, discovery is unverifiable (not a green tick)",
   str(d.get("detail"))[:110])

# ─────────────────────────────────────────────────────────────────────────────
section("unban: the operator's way out of a GMGN ban")
# During a ban every gate reads "unknown", so the Activity stream fills with
# SNIPER_DATA_UNAVAILABLE / FEES_UNKNOWN / MCAP_UNKNOWN - rejections that say
# nothing about the coins. rl_report_ban only ever EXTENDS a ban, so without a
# sanctioned clear the bot stays blind for the whole (possibly mis-parsed) window.


def _ban_sql(action, seconds=0.0):
    """Touch the SANDBOX db exactly like a real 429 would (separate process,
    because the db path is captured at import time)."""
    code = ("import os,sys;os.environ['ENZO_HOME']=sys.argv[1];"
            "sys.path.insert(0,sys.argv[2]);from enzo.core import db;"
            "a=sys.argv[3];"
            "(db.rl_report_ban('gmgn', float(sys.argv[4])) if a == 'set' else"
            " (db.rl_clear_ban('gmgn') if a == 'clear' else None));"
            "print(round(db.rl_get_ban_remaining('gmgn'), 1))")
    r = subprocess.run([PY, "-c", code, SANDBOX, ROOT, action, str(seconds)],
                       capture_output=True, text=True, timeout=180,
                       env=dict(os.environ, ENZO_HOME=SANDBOX, NO_COLOR="1"))
    try:
        return float((r.stdout.strip().splitlines() or ["0"])[-1])
    except Exception:
        return -1.0


def set_ban(seconds):
    return _ban_sql("set", seconds)


def ban_left():
    return _ban_sql("read")


_ban_sql("clear")
rc, js, txt = run_ctl("unban", "--json")
ok(rc == 0 and js.get("ok") is True and js.get("changed") is False,
   "with no ban, `unban` says so and changes nothing", f"rc={rc} {str(js)[:80]}")
rc, js, txt = run_ctl("unban")                      # human output, not JSON
ok("No GMGN ban" in txt, "and the human output says the same",
   (txt.strip().splitlines() or [""])[0][:70])

set_ban(90)
rc, js, txt = run_ctl("unban", "--json")
ok(rc == 1 and js.get("confirm_required") is True,
   "with a ban active, `unban` refuses to act without --confirm", f"rc={rc} {str(js)[:70]}")
ok(abs(float(js.get("ban_remaining_sec") or 0) - 90) < 20,
   "and reports how long is left", f"{js.get('ban_remaining_sec')}s")
left = ban_left()
ok(left > 60, "nothing was cleared by the dry run", f"{left:.0f}s still registered")
rc, js, txt = run_ctl("unban")                      # human output again
ok("requests_per_sec" in txt, "the dry run points at the pacing knobs that cause bans",
   "requests_per_sec mentioned" if "requests_per_sec" in txt else txt[:100])

rc, js, txt = run_ctl("doctor", "--json")
_d = {c.get("name"): c for c in (js.get("checks") or [])}
ok("BAN ACTIVE" in str((_d.get("gmgn_rate_config") or {}).get("detail")),
   "doctor names an active ban in gmgn_rate_config (with the way out)",
   str((_d.get("gmgn_rate_config") or {}).get("detail"))[:110])

rc, js, txt = run_ctl("unban", "--confirm", "--json")
ok(rc == 0 and js.get("ok") is True and js.get("changed") is True,
   "`unban --confirm` clears it", f"rc={rc} {str(js)[:80]}")
ok(ban_left() <= 0.5, "and the ban is really gone from the db", f"{ban_left():.0f}s left")

set_ban(45)
rc, js, txt = run_ctl("unban", "--json", "--confirm")
ok(rc == 0 and js.get("changed") is True,
   "`--json` after the subcommand still works (the old argparse trap)", f"rc={rc}")
_ban_sql("clear")

# ─────────────────────────────────────────────────────────────────────────────
print("\n" + "─" * 78)
print(f"enzoctl doctor + probe: {PASS} passed, {FAIL} failed")
shutil.rmtree(SANDBOX, ignore_errors=True)
sys.exit(1 if FAIL else 0)
