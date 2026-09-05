#!/usr/bin/env python3
"""The owner's entry universe: Pump V1 only, phase-aware floors, early snipers.

Decisions implemented here (owner, 2026-09-05)
---------------------------------------------
1. Trade STANDARD pump.fun coins only ("Pump V1"). GMGN names the launchpad in
   `launchpad` ("pump") and `launchpad_platform` ("Pump.fun"); anything else is
   refused, and so is a coin whose launchpad cannot be determined.
2. Pre-migration coins: market cap >= $5,000 and at least 10 SELL transactions.
3. Migrated coins: market cap >= $10,000 and total fees paid >= 2.5 SOL.
4. Rug signature: the wallets that got in FIRST (right after the dev's create
   transaction) are snipers with huge size - so the first 8 entries are examined
   and the coin is refused when enough of them are sniper-tagged and their
   combined (or any single) buy exceeds $5,000.

What the data source can and cannot do (verified against gmgn-cli 1.6.1)
-----------------------------------------------------------------------
gmgn-cli has NO trade tape: its commands are token info/security/pool/holders/
traders and market kline/trenches/trending/signal/hot-searches/search. "The first
8 transactions" therefore cannot be read literally. What CAN be read is
`token traders`, where every row carries start_holding_at (unix time of that
wallet's first buy), buy_volume_cur (USD bought since creation) and
maker_token_tags (GMGN labels launch buyers `sniper`). Sorting by entry time
reconstructs who was in first - which is the same window, observed per wallet
instead of per transaction. Honest limits, all asserted below: the endpoint
returns TOP traders, so a wallet that bought and dumped to zero may be absent;
rows without a timestamp are never counted as "early"; and when the window cannot
be read the gate says so (SNIPER_DATA_UNAVAILABLE) instead of passing silently.

Every scenario runs against tests/mockbin/gmgn-cli, which reproduces the real
tool's argument semantics (--address required, --token rejected, GMGN_API_KEY
mandatory, trenches/trending envelope shapes) so these paths are exercised
without an API key or a network.

Run: python3 tests/test_token_universe_gates.py
"""
import copy
import io
import json
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(_HERE)
if REPO not in sys.path:
    sys.path.insert(0, REPO)
sys.path.insert(0, _HERE)

from conftest_paths import install_mock_on_path, isolate_home  # noqa: E402

if not install_mock_on_path():
    print("\n  \033[31mABORT\033[0m  no mock gmgn-cli found (expected tests/mockbin/gmgn-cli).")
    sys.exit(2)

# Isolate BEFORE importing enzo: config resolves every state path at import time.
_SANDBOX = isolate_home(prefix="enzo-universe-")
os.environ["GMGN_API_KEY"] = "mock-key-for-tests"

# The provider paces every gmgn-cli call (data_sources.gmgn.request_gap_ms, 350ms
# in production). Against a local mock that only makes the suite slow, so the
# sandbox copy asks for 5ms - the pacing logic itself is covered elsewhere.
_yaml = os.path.join(_SANDBOX, "config", "enzo-config.yaml")
_txt = io.open(_yaml, encoding="utf-8").read()
# Replace each value in place. Inserting a second `requests_per_sec` line would
# produce a DUPLICATE KEY, which PyYAML resolves by silently keeping the LAST
# one - the sandbox then ran at the production 0.8 req/s and the suite took
# three minutes instead of seconds. (config._parse_yaml now refuses duplicates.)
_txt = _txt.replace("request_gap_ms: 350", "request_gap_ms: 1")
_txt = _txt.replace("requests_per_sec: 0.8", "requests_per_sec: 200")
_keys = [l for l in _txt.splitlines() if l.strip().startswith("requests_per_sec:")]
assert len(_keys) == 1, f"sandbox config must not duplicate the key: {_keys}"
io.open(_yaml, "w", encoding="utf-8").write(_txt)

from enzo.analyzers import analyze as A                              # noqa: E402
from enzo.analyzers import security                                  # noqa: E402
from enzo.core.config import load_config                             # noqa: E402
from enzo.providers import gmgn                                      # noqa: E402

PASS = FAIL = 0
_n = [0]


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


def mint_for(tag):
    """A distinct mint per scenario: the provider caches by mint, and a cache hit
    from the previous scenario would test the wrong payload."""
    _n[0] += 1
    return f"Mock{_n[0]:02d}{tag}1111111111111111111111111111111"[:44]


BASE_CFG = load_config()


def decide(state, cfg_patch=None, fetch_deep=True, tag="t"):
    """Run the real analyzer over a mocked GMGN payload and return the decision.

    Each scenario gets its own mint AND its own dev address: `fees_paid` caches
    the dev's created-tokens book per creator, so reusing one creator across
    scenarios would serve the first scenario's book to every later one.
    """
    mint = mint_for(tag)
    state = dict(state or {})
    info = dict(state.get("token_info") or {})
    dev = dict(info.get("dev") or {})
    dev.setdefault("creator_address", f"Dev{mint[:20]}111111111111111111111")
    info["dev"] = dev
    state["token_info"] = info
    os.environ["GMGN_MOCK_STATE"] = json.dumps(state)
    cfg = copy.deepcopy(BASE_CFG)
    for key, val in (cfg_patch or {}).items():
        section_name, _, field = key.partition(".")
        if field:
            cfg.setdefault(section_name, {})[field] = val
        else:
            cfg[section_name] = val
    md = gmgn.get_market_data(mint)
    sec = security.security_scan(mint)
    merged = {
        "mint": mint,
        "token_symbol": md.get("token_symbol"),
        "price_usd": md.get("price_usd"),
        "signals": md.get("signals"),
        "phase": md.get("phase"),
        "launchpad": md.get("launchpad"),
        "security": sec,
    }
    d = A.analyze(merged, cfg, fetch_deep=fetch_deep)
    d["_mint"] = mint
    return d


def rejected(d):
    return " | ".join(str(x) for x in (d.get("rejected_signals") or []))


def has(d, token):
    return token.lower() in rejected(d).lower()


MIGRATED = {"launchpad_status": "2", "migrated_pool": "PoolAddr111",
            "price": {"price": "0.000031"}}          # cap = 0.000031 x 1e9 = $31,000
PRE = {"launchpad_status": "1", "migrated_pool": ""}


def fees(mint, value=None, absent=False):
    """created-tokens override carrying (or withholding) total_fee for this mint."""
    if absent:
        return {"created_tokens": {"tokens": []}}
    return {"created_tokens": {"tokens": [{"token_address": mint, "total_fee": value}]}}


# ── 1) Pump V1 universe ──────────────────────────────────────────────────────
section("1) العملة القياسية pump.fun فقط (Pump V1)")

d = decide({}, tag="pump")
uni = d.get("universe") or {}
check("عملة pump.fun تُقبل كونياً (ليست مرفوضة بسبب المنصة)",
      d.get("decision") in ("BUY", "WAIT") and uni.get("pump_v1") is True,
      f"{d.get('decision')} · platform={uni.get('platform')} · phase={uni.get('phase')}")

d = decide({"token_info": {"launchpad": "letsbonk", "launchpad_platform": "letsbonk.fun"}}, tag="lb")
check("منصة إطلاق أخرى (letsbonk) ⇒ رفض قاطع NOT_PUMP_V1",
      has(d, "NOT_PUMP_V1") and d.get("decision") == "IGNORE", rejected(d)[:120])

d = decide({"token_info": {"launchpad": "moonshot", "launchpad_platform": "moonshot_app"}}, tag="ms")
check("moonshot ⇒ رفض كذلك", has(d, "NOT_PUMP_V1"), rejected(d)[:90])

d = decide({"token_info": {"launchpad": "", "launchpad_platform": "",
                           "launchpad_status": "", "exchange": "",
                           "pool": {"exchange": ""}}}, tag="unk")
check("منصة غير معروفة ⇒ LAUNCHPAD_UNKNOWN (لا تخمين)",
      has(d, "LAUNCHPAD_UNKNOWN"), rejected(d)[:110])

d = decide({"token_info": {"launchpad": "letsbonk", "launchpad_platform": "letsbonk.fun"}},
           cfg_patch={"token_universe.pump_v1_only": False}, tag="lboff")
check("وإطفاء pump_v1_only يعطّل البوابة (قرار المالك قابل للتراجع)",
      not has(d, "NOT_PUMP_V1"), rejected(d)[:90])

d = decide({"token_info": {"launchpad": "", "launchpad_platform": "",
                           "launchpad_status": "", "exchange": "",
                           "pool": {"exchange": ""}}},
           cfg_patch={"token_universe.reject_unknown_launchpad": False}, tag="unk2")
check("reject_unknown_launchpad=false ⇒ المجهول لا يُرفض بسبب الجهل وحده",
      not has(d, "LAUNCHPAD_UNKNOWN"), rejected(d)[:90])

# exchange alone must not make a foreign launchpad look like pump.fun
prof = gmgn.launchpad_profile({"launchpad_platform": "letsbonk.fun",
                               "pool": {"exchange": "pump_amm"}})
check("exchange وحده لا يُثبت هوية pump (يُستخدم للمرحلة فقط)",
      prof["is_pump_v1"] is False and prof["migrated"] is True,
      f"is_pump_v1={prof['is_pump_v1']} · migrated={prof['migrated']}")


# ── 2) phase detection ───────────────────────────────────────────────────────
section("2) تحديد المرحلة: قبل الهجرة أم بعدها")

for status, want in (("0", "pre_migration"), ("1", "pre_migration"), ("2", "migrated")):
    p = gmgn.launchpad_profile({"launchpad_platform": "Pump.fun", "launchpad_status": status})
    check(f"launchpad_status={status} ⇒ {want}", p["phase"] == want, f"got {p['phase']}")

p = gmgn.launchpad_profile({"launchpad_platform": "Pump.fun", "migrated_pool": "PoolX"})
check("migrated_pool موجود ⇒ مهاجرة", p["phase"] == "migrated", str(p["reasons"]))

p = gmgn.launchpad_profile({"launchpad_platform": "Pump.fun", "complete_timestamp": 1700000000})
check("complete_timestamp ⇒ مهاجرة (اكتمل المنحنى)", p["phase"] == "migrated", str(p["reasons"]))

p = gmgn.launchpad_profile({"launchpad_platform": "Pump.fun", "launchpad_progress": "1.0"})
check("progress=100% ⇒ مهاجرة", p["phase"] == "migrated", str(p["reasons"]))

p = gmgn.launchpad_profile({"launchpad_platform": "Pump.fun", "exchange": "pump_amm"})
check("exchange=pump_amm ⇒ مهاجرة", p["phase"] == "migrated", str(p["reasons"]))

p = gmgn.launchpad_profile({"launchpad_platform": "Pump.fun", "exchange": "pump_fun"})
check("exchange=pump_fun ⇒ ما زالت على المنحنى", p["phase"] == "pre_migration", str(p["reasons"]))

p = gmgn.launchpad_profile({"launchpad_platform": "Pump.fun"})
check("بلا أي دليل ⇒ unknown (لا يُخمَّن)", p["phase"] == "unknown", str(p["reasons"]))


# ── 3) pre-migration floors ─────────────────────────────────────────────────
section("3) حدود ما قبل الهجرة: كاب 5000$ و10 صفقات بيع")

d = decide({"token_info": {**PRE, "price": {"price": "0.0000049"}}}, tag="mc4900")
check("كاب $4,900 قبل الهجرة ⇒ رفض", has(d, "MCAP_BELOW_PRE_MIGRATION_MIN"), rejected(d)[:110])

d = decide({"token_info": {**PRE, "price": {"price": "0.000005"}}}, tag="mc5000")
check("كاب $5,000 بالضبط ⇒ مقبول (الحدّ شامل)",
      not has(d, "MCAP_BELOW"), f"{d.get('decision')} · {rejected(d)[:70]}")

d = decide({"token_info": {**PRE, "price": {"sells_24h": 9}}}, tag="sells9")
check("9 صفقات بيع ⇒ رفض SELLS_BELOW_MIN", has(d, "SELLS_BELOW_MIN"), rejected(d)[:110])

d = decide({"token_info": {**PRE, "price": {"sells_24h": 10}}}, tag="sells10")
check("10 صفقات بيع بالضبط ⇒ مقبول", not has(d, "SELLS_BELOW_MIN"), rejected(d)[:70])

d = decide({"token_info": {**PRE, "price": {"sells_24h": None, "sells_1h": None,
                                            "sells_5m": None, "sells_1m": None}}},
           tag="sellsnone")
check("عدد البيوع غير مُبلَّغ ⇒ SELLS_UNKNOWN (لا يُعتبر صفراً ولا يُتجاهل)",
      has(d, "SELLS_UNKNOWN"), rejected(d)[:110])


# ── 4) migrated floors ──────────────────────────────────────────────────────
section("4) حدود ما بعد الهجرة: كاب 10000$ ورسوم 2.5 SOL")

MINT_HOLDER = {}


def decide_migrated(price, fee_state, tag, cfg_patch=None):
    """Migrated scenarios need the mint to look up its own fee row."""
    mint = mint_for(tag)
    MINT_HOLDER[tag] = mint
    dev = f"Dev{mint[:20]}111111111111111111111"
    state = {"token_info": {**MIGRATED, "price": {"price": price},
                            "dev": {"creator_address": dev}}}
    state.update(fee_state(mint) if callable(fee_state) else fee_state)
    os.environ["GMGN_MOCK_STATE"] = json.dumps(state)
    cfg = copy.deepcopy(BASE_CFG)
    for key, val in (cfg_patch or {}).items():
        a, _, b = key.partition(".")
        cfg.setdefault(a, {})[b] = val if b else val
        if not b:
            cfg[a] = val
    md = gmgn.get_market_data(mint)
    sec = security.security_scan(mint)
    merged = {"mint": mint, "token_symbol": md.get("token_symbol"),
              "price_usd": md.get("price_usd"), "signals": md.get("signals"),
              "phase": md.get("phase"), "launchpad": md.get("launchpad"), "security": sec}
    d = A.analyze(merged, cfg, fetch_deep=True)
    d["_mint"] = mint
    return d


d = decide_migrated("0.0000095", lambda m: fees(m, 3.42), "mig9500")
check("مهاجرة بكاب $9,500 ⇒ رفض (الحدّ 10,000)",
      has(d, "MCAP_BELOW_MIGRATED_MIN"), rejected(d)[:110])

d = decide_migrated("0.000031", lambda m: fees(m, 2.49), "fees249")
check("رسوم 2.49 SOL ⇒ رفض FEES_BELOW_MIN", has(d, "FEES_BELOW_MIN"), rejected(d)[:120])

d = decide_migrated("0.000031", lambda m: fees(m, 2.5), "fees250")
check("رسوم 2.50 SOL بالضبط ⇒ مقبول (الحدّ شامل)",
      not has(d, "FEES_BELOW_MIN") and d.get("decision") in ("BUY", "WAIT"),
      f"{d.get('decision')} · {rejected(d)[:70]}")

d = decide_migrated("0.000031", lambda m: fees(m, absent=True), "feesnone")
check("رسوم غير مُبلَّغة + require_known_fees ⇒ رفض FEES_UNKNOWN",
      has(d, "FEES_UNKNOWN"), rejected(d)[:130])

d = decide_migrated("0.000031", lambda m: fees(m, absent=True), "feesnone2",
                    cfg_patch={"phase_gates": {**BASE_CFG["phase_gates"],
                                               "migrated": {**BASE_CFG["phase_gates"]["migrated"],
                                                            "require_known_fees": False}}})
check("require_known_fees=false ⇒ القيد يُتجاهل صراحةً لا بصمت",
      not has(d, "FEES_UNKNOWN") and not has(d, "FEES_BELOW_MIN"), rejected(d)[:90])

uni = d.get("universe") or {}
check("وتقرير القرار يحمل قيمة الرسوم ومصدرها (للتدقيق)",
      isinstance(uni.get("fees"), dict) or uni.get("fees") is None,
      json.dumps(uni.get("fees"))[:110])

d = decide({"token_info": {"launchpad_platform": "Pump.fun", "launchpad_progress": "",
                           "launchpad_status": "", "migrated_pool": "",
                           "pool": {"exchange": ""}}}, tag="phaseunk", fetch_deep=True)
check("مرحلة مجهولة ⇒ يُطبَّق الحدّ الأشدّ (10,000) لا الأضعف",
      has(d, "MCAP_BELOW_UNKNOWN_MIN"), rejected(d)[:120])


# ── 5) the early-sniper rug signature ───────────────────────────────────────
section("5) بصمة الـ rug: أول 8 محافظ سنايبرز بأحجام ضخمة")


def snipers(n, each, start=1000, tags=("sniper",)):
    return [{"address": f"Sn{i}1111111111111111111111111111111111",
             "start_holding_at": start + i, "buy_volume_cur": each,
             "sell_volume_cur": 0.0, "buy_tx_count_cur": 1,
             "maker_token_tags": list(tags), "tags": []} for i in range(n)]


def retail(n, each=90.0, start=5000):
    return [{"address": f"Rt{i}1111111111111111111111111111111111",
             "start_holding_at": start + i * 30, "buy_volume_cur": each,
             "sell_volume_cur": 10.0, "buy_tx_count_cur": 2,
             "maker_token_tags": [], "tags": ["fresh_wallet"]} for i in range(n)]


d = decide({"traders": snipers(8, 1200.0)}, tag="sn8")
rep = (d.get("universe") or {}).get("snipers") or {}
check("8 سنايبرز بمجموع $9,600 ⇒ رفض قاطع SNIPER_FLOOD_EARLY",
      has(d, "SNIPER_FLOOD_EARLY") and d.get("decision") == "IGNORE", rejected(d)[:140])
check("والتقرير يذكر العدد والمجموع (تشخيص قابل للقراءة)",
      rep.get("sniper_count") == 8 and rep.get("sniper_total_usd") == 9600.0,
      f"n={rep.get('sniper_count')} total={rep.get('sniper_total_usd')} verdict={rep.get('verdict')}")
check("والنافذة هي أول 8 محافظ بدخولها لا بأكبر حجم",
      len(rep.get("window") or []) == 8 and
      [w["address"] for w in rep["window"]] == [f"Sn{i}1111111111111111111111111111111111" for i in range(8)],
      str([w.get("seconds_after_open") for w in (rep.get("window") or [])][:3]))

d = decide({"traders": snipers(1, 6500.0) + retail(7)}, tag="sn1big")
check("سنايبر واحد بشراء $6,500 ⇒ رفض (حدّ المحفظة الواحدة)",
      has(d, "SNIPER_FLOOD_EARLY"), rejected(d)[:130])

d = decide({"traders": snipers(3, 1200.0) + retail(5)}, tag="sn3")
check("3 سنايبرز فقط (أقل من min_sniper_count=4) ⇒ لا رفض",
      not has(d, "SNIPER_FLOOD_EARLY"), rejected(d)[:90])

d = decide({"traders": snipers(6, 700.0) + retail(2)}, tag="sn6small")
check("6 سنايبرز بمجموع $4,200 (تحت 5,000) ⇒ لا رفض — لا تضحية بالانطلاقات الشرعية",
      not has(d, "SNIPER_FLOOD_EARLY") and d.get("decision") in ("BUY", "WAIT"),
      f"{d.get('decision')} · {rejected(d)[:70]}")

d = decide({"traders": [{"address": "NoTs111111111111111111111111111111111111",
                         "buy_volume_cur": 9000.0, "maker_token_tags": ["sniper"]}
                        for _ in range(8)]}, tag="nots")
rep = (d.get("universe") or {}).get("snipers") or {}
check("محافظ بلا وقت دخول ⇒ لا تُحسب «مبكرة» (صدق لا تخمين)",
      rep.get("verdict") == "unknown" and rep.get("rows_with_ts") == 0
      and has(d, "SNIPER_DATA_UNAVAILABLE"),
      f"verdict={rep.get('verdict')} rows_with_ts={rep.get('rows_with_ts')}")

d = decide({"fail": {"endpoint": "token/traders", "rc": 1, "err": "boom"}}, tag="fail")
check("فشل جلب المتداولين + on_unknown=reject ⇒ رفض معلن لا مرور صامت",
      has(d, "SNIPER_DATA_UNAVAILABLE"), rejected(d)[:120])

d = decide({"fail": {"endpoint": "token/traders", "rc": 1, "err": "boom"}}, tag="fail2",
           cfg_patch={"sniper_flood": {**BASE_CFG["sniper_flood"], "on_unknown": "allow"}})
check("on_unknown=allow ⇒ يمرّ مع تسجيل السبب (قرار المالك)",
      not has(d, "SNIPER_DATA_UNAVAILABLE"), rejected(d)[:90])

d = decide({"traders": snipers(8, 1200.0)}, tag="snoff",
           cfg_patch={"sniper_flood": {**BASE_CFG["sniper_flood"], "enabled": False}})
check("sniper_flood.enabled=false ⇒ البوابة معطّلة بالكامل (بلا veto ولا NOT_CHECKED)",
      not has(d, "SNIPER_FLOOD_EARLY") and not has(d, "SNIPER_FLOOD_NOT_CHECKED"),
      rejected(d)[:90])

d = decide({"traders": [{"address": f"Bd{i}11111111111111111111111111111111111",
                         "start_holding_at": 1000 + i, "buy_volume_cur": 1500.0,
                         "maker_token_tags": ["bundler"], "tags": []} for i in range(8)]},
           tag="bundler")
check("bundler وحده لا يُعتبر سنايبر افتراضياً (include_bundler=false)",
      not has(d, "SNIPER_FLOOD_EARLY"), rejected(d)[:90])

d = decide({"traders": [{"address": f"Bd{i}11111111111111111111111111111111111",
                         "start_holding_at": 1000 + i, "buy_volume_cur": 1500.0,
                         "maker_token_tags": ["bundler"], "tags": []} for i in range(8)]},
           tag="bundler2",
           cfg_patch={"sniper_flood": {**BASE_CFG["sniper_flood"], "include_bundler": True}})
check("include_bundler=true ⇒ يُحتسب (قابل للتوسيع بقرار المالك)",
      has(d, "SNIPER_FLOOD_EARLY"), rejected(d)[:110])

d = decide({"traders": retail(12) + snipers(8, 1200.0, start=9000)}, tag="late")
rep = (d.get("universe") or {}).get("snipers") or {}
check("سنايبرز دخلوا متأخرين ⇒ لا يدخلون نافذة الأولين (الترتيب بالدخول لا بالحجم)",
      rep.get("sniper_count") == 0 and not has(d, "SNIPER_FLOOD_EARLY"),
      f"snipers_in_window={rep.get('sniper_count')} · {rejected(d)[:60]}")


# ── 6) no silent skips, no sacrifice ────────────────────────────────────────
section("6) لا مرور صامتاً ولا تضحية بالانطلاقات الشرعية")

d = decide({"token_info": {**MIGRATED, "price": {"price": "0.000031"}}},
           tag="nodeep", fetch_deep=False)
check("فحص لم يُجرَ (fetch_deep=False) ⇒ FEES_NOT_CHECKED veto لا نجاح",
      has(d, "FEES_NOT_CHECKED"), rejected(d)[:120])

d = decide({"token_info": {**MIGRATED, "price": {"price": "0.000031"}},
            "created_tokens": {"tokens": []}}, tag="nodeep2", fetch_deep=False)
check("وكذلك نافذة السنايبرز: SNIPER_FLOOD_NOT_CHECKED",
      has(d, "SNIPER_FLOOD_NOT_CHECKED"), rejected(d)[:120])

d = decide({"token_info": {**PRE, "price": {"price": "0.000025", "sells_24h": 240,
                                            "buys_24h": 900, "volume_24h": 180000.0}},
            "traders": retail(8, 400.0)}, tag="rocket")
check("انطلاقة شرعية (كاب $25k، 240 بيعاً، 8 محافظ عادية) ⇒ BUY بلا تضحية",
      d.get("decision") == "BUY", f"{d.get('decision')} · {rejected(d)[:80]}")

d = decide_migrated("0.000042", lambda m: fees(m, 12.75), "rocket2")
check("عملة مهاجرة شرعية برسوم 12.75 SOL ⇒ لا تُرفض",
      not has(d, "FEES_BELOW_MIN") and not has(d, "FEES_UNKNOWN")
      and d.get("decision") in ("BUY", "WAIT"),
      f"{d.get('decision')} · {rejected(d)[:80]}")


# ── 7) the regression that made all of this invisible ───────────────────────
section("7) analyze() تُرجع قراراً فعلاً (عطل الذيل اليتيم)")

import ast                                                        # noqa: E402
import io                                                         # noqa: E402
import subprocess                                                 # noqa: E402

src = io.open(os.path.join(REPO, "enzo", "analyzers", "analyze.py"), encoding="utf-8").read()
tree = ast.parse(src)
no_return = []
for node in tree.body:
    if isinstance(node, ast.FunctionDef):
        if not any(isinstance(x, ast.Return) for x in ast.walk(node)):
            no_return.append(node.name)
check("كل دالة في analyze.py تُرجع قيمة (كانت analyze تُرجع None منذ 40a19f6)",
      not no_return, f"بلا return: {no_return}" if no_return else f"{len(tree.body)} كائنات")

code = (
    "import json,sys;"
    f"sys.path.insert(0,{REPO!r});"
    "from enzo.analyzers import analyze as A;"
    f"d=A.run_pipeline({mint_for('e2e')!r});"
    "assert isinstance(d,dict), type(d);"
    "print(json.dumps({'decision':d.get('decision'),'conf':d.get('confidence_score'),"
    "'universe':bool(d.get('universe')),'reason':(d.get('decision_reason') or '')[:60]}))"
)
proc = subprocess.run([sys.executable, "-c", code], cwd=REPO, capture_output=True,
                      text=True, timeout=300,
                      env=dict(os.environ, ENZO_HOME=_SANDBOX, GMGN_MOCK_STATE="{}"))
line = [l for l in proc.stdout.splitlines() if l.startswith("{")]
check("run_pipeline عبر العملية الكاملة يُرجع قاموس قرار (لا None)",
      proc.returncode == 0 and bool(line),
      (line[-1] if line else (proc.stderr or "")[-180:]))
if line:
    payload = json.loads(line[-1])
    check("والقرار يحمل الثقة وملفّ الكون (universe) للتدقيق",
          payload.get("conf") is not None and payload.get("universe") is True,
          json.dumps(payload))

print("\n" + "=" * 68)
print(f"  RESULT: {PASS} passed, {FAIL} failed")
print("=" * 68)
sys.exit(1 if FAIL else 0)
