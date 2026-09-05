#!/usr/bin/env python3
"""Config-wiring guard: every knob the owner can see must actually do something.

Why this file exists
--------------------
A config file that lies is worse than no config file. This repo shipped with
knobs that were never read by any code (`scam_detection.*`, `scoring_weights.*`,
the factory-dev penalties, the dashboard refresh interval...). The owner edits
the YAML, restarts, and NOTHING changes - while the file insists the value is in
force. On a bot that trades real money that is how a "risk limit" ends up being
pure decoration.

Three guards live here:

  1. PARITY - every key in config.DEFAULTS exists in config/enzo-config.yaml and
     vice versa, so no knob is hidden and no knob is orphaned.
  2. DEAD-KEY FREEZE - every YAML leaf is searched for in the executable code
     (config.py excluded, comments stripped). Keys that nothing reads are
     compared against a FROZEN baseline: the debt may shrink, it may never grow.
     Adding a new decorative knob fails this suite.
  3. WIRING PROOFS - for the knobs that were just connected (dev factory
     penalties, dashboard refresh/activity limit) the test proves BOTH directions:
     default values reproduce the old hardcoded behaviour exactly, and changing
     the value changes the outcome.

Nothing here touches the owner's database or config: the wiring proof runs in a
throwaway ENZO_HOME.
"""
import io
import os
import re
import shutil
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

PASS = FAIL = 0


def ok(cond, label, extra=""):
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


import yaml                                              # noqa: E402
from enzo.core.config import DEFAULTS                    # noqa: E402

YAML_PATH = os.path.join(ROOT, "config", "enzo-config.yaml")
Y = yaml.safe_load(io.open(YAML_PATH, encoding="utf-8")) or {}

# Keys present in the YAML but not in DEFAULTS. Only tolerated when documented
# here; anything new fails the parity check below.
# Deliberate owner decisions (2026-09-03): the bot trades LIVE with capital
# synced from the real MoonPay wallet, and every buy/sell is routed through
# native SOL exactly as MoonPay's own pump.fun guide does.
INTENTIONAL_OVERRIDES = {
    "paper_mode",            # DEFAULTS True (safe) -> owner runs LIVE
    "execution.base_token",  # DEFAULTS USDC -> owner funds swaps from SOL
}

KNOWN_YAML_ONLY = {
    # legacy name, read by nothing - kept visible so the owner can decide to
    # delete it rather than having it vanish silently from the file.
    "dev_behavior.impact_dev_remove_liq",
}


def _walk(a, b, path=""):
    out = []
    if isinstance(a, dict) and isinstance(b, dict):
        for k in a:
            if k not in b:
                out.append(path + str(k))
            else:
                out += _walk(a[k], b[k], path + str(k) + ".")
    return out


def _leaves(d, path=""):
    out = []
    if isinstance(d, dict):
        for k, v in d.items():
            out += _leaves(v, path + str(k) + ".")
    elif isinstance(d, list):
        for i, v in enumerate(d):
            out += _leaves(v, path + f"[{i}].")
    else:
        out.append((path.rstrip("."), re.sub(r"\[\d+\]$", "", path.rstrip(".")).split(".")[-1]))
    return out


def _executable_source():
    """All .py under enzo/ plus enzoctl, minus config.py, minus comments."""
    def strip(src):
        kept = []
        for line in src.splitlines():
            if re.match(r"^\s*#", line):
                continue
            kept.append(re.sub(r"(?<!['\"])#\s.*$", "", line))
        return "\n".join(kept)

    chunks = []
    for root, dirs, files in os.walk(os.path.join(ROOT, "enzo")):
        dirs[:] = [d for d in dirs if d != "__pycache__"]
        for f in files:
            if not f.endswith(".py"):
                continue
            p = os.path.join(root, f)
            if p.endswith(os.path.join("core", "config.py")):
                continue          # DEFAULTS defines keys; it does not read them
            chunks.append(strip(io.open(p, encoding="utf-8").read()))
    ctl = os.path.join(ROOT, "enzoctl")
    if os.path.exists(ctl):
        chunks.append(strip(io.open(ctl, encoding="utf-8").read()))
    return "\n".join(chunks)


# ── 1. parity ────────────────────────────────────────────────────────────────
def test_parity():
    section("1. التطابق: كل مفتاح في الافتراضيات موجود في YAML وبالعكس")
    missing_yaml = _walk(DEFAULTS, Y)
    ok(not missing_yaml, "لا مفتاح في DEFAULTS غائب عن YAML (لا knob مخفي عن المالك)",
       str(missing_yaml) if missing_yaml else f"{len(_leaves(Y))} ورقة في YAML")
    extra = [k for k in _walk(Y, DEFAULTS) if k not in KNOWN_YAML_ONLY]
    ok(not extra, "لا مفتاح في YAML غائب عن DEFAULTS (إلا الموثَّق)",
       str(extra) if extra else f"الموثَّق: {sorted(KNOWN_YAML_ONLY)}")
    vals = []
    for path, key in _leaves(DEFAULTS):
        pass
    # same leaf value in both files => the YAML is not silently overriding intent
    def flat(d, path=""):
        out = {}
        if isinstance(d, dict):
            for k, v in d.items():
                out.update(flat(v, path + str(k) + "."))
        elif isinstance(d, list):
            for i, v in enumerate(d):
                out.update(flat(v, path + f"[{i}]."))
        else:
            out[path.rstrip(".")] = d
        return out
    fd, fy = flat(DEFAULTS), flat(Y)
    diff = {k: (fd[k], fy.get(k)) for k in fd if k in fy and fd[k] != fy[k]}
    # The owner's file SHOULD differ from the safe defaults in exactly two
    # places - both explicit decisions. Anything else means somebody (or
    # something) moved a value in one file only, which is how a stop-loss ends
    # up at a number nobody chose. Closed allowlist => new drift fails.
    unexpected = {k: v for k, v in diff.items() if k not in INTENTIONAL_OVERRIDES}
    ok(not unexpected, "YAML لا ينحرف عن DEFAULTS إلا في القرارات الموثَّقة",
       str(list(unexpected.items())[:4]) if unexpected
       else f"{len(fd) - len(diff)} قيمة متطابقة، {len(diff)} انحراف مقصود")
    missing_override = sorted(set(INTENTIONAL_OVERRIDES) - set(diff))
    ok(not missing_override, "القرارات الموثَّقة ما زالت سارية في ملفك",
       str(missing_override) if missing_override
       else "; ".join(f"{k}={diff[k][1]!r}" for k in sorted(diff)))


# ── 2. dead-key freeze ───────────────────────────────────────────────────────
FROZEN_DEAD = {
    "cache.holder_dist_ttl",
    "data_sources.gmgn.extra_discovery",
    "data_sources.gmgn.max_candidates_per_scan",
    "data_sources.gmgn.top_traders",
    "data_sources.pumpdev.batch_size",
    "data_sources.pumpdev.penalties.telegram_reuse_penalty",
    "data_sources.pumpdev.penalties.telegram_reuse_penalty_threshold",
    "data_sources.pumpdev.price_refresh_secs",
    "data_sources.pumpdev.thresholds.bundler_hard_pct",
    "data_sources.pumpdev.thresholds.bundler_soft_pct",
    "data_sources.pumpdev.thresholds.dev_hold_soft_pct",
    "data_sources.pumpdev.thresholds.max_telegram_reuse",
    "data_sources.pumpdev.thresholds.max_website_reuse",
    "data_sources.pumpdev.thresholds.pre_migration_exempt",
    "data_sources.pumpdev.thresholds.sniper_hard",
    "data_sources.pumpdev.thresholds.sniper_soft",
    "data_sources.pumpdev.thresholds.top10_soft_pct",
    "data_sources.pumpdev.use_kolscan",
    "data_sources.pumpdev.use_recent",
    "dev_behavior.impact_dev_remove_liq",
    "dev_behavior.liq_remove_pct",
    "dev_behavior.no_big_hits_max_ath_mc",
    "dev_behavior.no_big_hits_penalty",
    "discovery.dedupe_window_minutes",
    "discovery.max_tokens_per_scan",
    "entry_strategy.min_risk_reward_ratio",
    "entry_strategy.trend_confirmation_periods",
    "execution.confirm_blocks",
    "execution.rollback_on_failure",
    "execution.slippage_bps",
    "learning.apply_weight_adjustments",
    "learning.min_samples_for_adjust",
    "logging.log_api_errors",
    "logging.log_network_latency",
    "market_analysis.max_scam_score",
    "notifications.send_scan_summary",
    "paper_trading.slippage_tolerance",
    "pump_monitor.max_analyses_per_min",
    "pump_monitor.max_candidates",
    "pump_monitor.min_analysis_interval_sec",
    "pump_monitor.min_initial_buy_sol",
    "risk_management.circuit_breaker_drop_pct",
    "risk_management.exit_monitor_interval_seconds",
    "scam_detection.bundle_single_threshold",
    "scam_detection.bundle_top10_threshold",
    "scam_detection.bundler_flood_penalty",
    "scam_detection.bundlers_in_top20_penalty",
    "scam_detection.concentrated_holders_threshold",
    "scam_detection.deep_dangerous_threshold",
    "scam_detection.dev_factory_penalty",
    "scam_detection.dev_team_hold_penalty",
    "scam_detection.fake_liquidity_threshold",
    "scam_detection.fake_social_engagement_threshold",
    "scam_detection.fake_volume_threshold",
    "scam_detection.honeypot_risk_threshold",
    "scam_detection.rat_flood_penalty",
    "scam_detection.rug_pull_risk_threshold",
    "scam_detection.sniper_flood_penalty",
    "scam_detection.suspicious_ownership_threshold",
    "scam_detection.top10_dumping_penalty",
    "scam_detection.top70_sniper_hold_penalty",
    "scoring_weights.on_chain_activity",
    "scoring_weights.price_action",
    "scoring_weights.scam_indicators",
    "scoring_weights.social_momentum",
}


def test_dead_keys():
    section("2. المفاتيح الميتة: الدين مُجمَّد — يُمنع أن يكبر")
    src = _executable_source()
    dead = set()
    for path, key in _leaves(Y):
        if not key or key.isdigit():
            continue
        if not re.search(r"['\"]" + re.escape(key) + r"['\"]", src):
            dead.add(path)
    new_dead = sorted(dead - FROZEN_DEAD)
    ok(not new_dead, "لا مفتاح ميت جديد أُضيف إلى الإعداد",
       str(new_dead) if new_dead else f"{len(dead)} ميتاً معروفاً من {len(_leaves(Y))}")
    revived = sorted(FROZEN_DEAD - dead)
    ok(True, "مفاتيح كانت ميتة وصارت مقروءة (يُفضَّل حذفها من القائمة المجمَّدة)",
       str(revived) if revived else "لا شيء")
    critical = ("rug_protection.", "exit_strategy.", "position_sizing.", "dashboard.",
                "dev_behavior.factory_", "exit_monitor.")
    bad = sorted(d for d in dead if d.startswith(critical))
    ok(not bad, "الأقسام الحرجة (حماية الرغّ/الخروج/الحجم/اللوحة) بلا مفاتيح ميتة",
       str(bad) if bad else "")
    print(f"  \033[33mINFO\033[0m  دين معروف: {len(dead)} مفتاحاً لا يقرؤه كود "
          f"(أكبرها scam_detection وdata_sources) — موثَّق في هذا الاختبار")


# ── 3. wiring proofs ─────────────────────────────────────────────────────────
def _original_factory_penalty(created_count, open_ratio):
    """Byte-for-byte copy of the hardcoded function that config replaced."""
    if not created_count:
        return 0.0
    penalty = 0.0
    if created_count >= 200:
        penalty += 45
    elif created_count >= 50:
        penalty += 30
    elif created_count >= 10:
        penalty += 15
    if open_ratio is not None:
        if open_ratio < 0.03:
            penalty += 25
        elif open_ratio < 0.10:
            penalty += 15
        elif open_ratio < 0.25:
            penalty += 5
    else:
        penalty += 8
    return min(penalty, 80.0)


def test_factory_penalty_wiring():
    section("3. عقوبة مصنع المطوِّر: مربوطة بالإعداد دون تغيير السلوك")
    from enzo.analyzers.dev import _dev_reputation_penalty as new
    created = [0, 1, 5, 9, 10, 11, 25, 49, 50, 51, 120, 199, 200, 201, 500, 1000]
    ratios = [None, 0.0, 0.005, 0.029, 0.03, 0.05, 0.099, 0.10, 0.15, 0.249, 0.25, 0.5, 0.9, 1.0]
    diffs = []
    n = 0
    for c in created:
        for r in ratios:
            n += 1
            exp = _original_factory_penalty(c, r)
            if new(c, r) != exp or new(c, r, {}) != exp:
                diffs.append((c, r, exp, new(c, r)))
    ok(not diffs, f"القيم الافتراضية تعيد الأرقام الصلبة القديمة حرفياً ({n} حالة)",
       str(diffs[:3]) if diffs else "")

    from enzo.core.config import load_config
    live = load_config().get("dev_behavior", {})
    live_diff = [(c, r) for c in created for r in ratios
                 if new(c, r, live) != _original_factory_penalty(c, r)]
    ok(not live_diff, "إعدادك الحيّ الحالي = السلوك الذي كان يعمل (لا مفاجأة بعد الربط)",
       str(live_diff[:3]) if live_diff else "")

    ok(new(10, None, {"factory_dev_penalty": 99}) != _original_factory_penalty(10, None),
       "تغيير factory_dev_penalty يغيّر النتيجة فعلاً (المفتاح يعمل)")
    ok(new(1000, 0.0, {"factory_dev_penalty_cap": 10}) == 10.0,
       "تغيير السقف factory_dev_penalty_cap يُحترم")
    ok(new(10, None, {"factory_dev_min_created": 50}) == 8.0,
       "تغيير عتبة factory_dev_min_created يُحترم (لا عقوبة تحتها)")


def test_dashboard_knobs_wired():
    section("4. مفاتيح اللوحة: refresh_seconds وactivity_limit")
    sbx = tempfile.mkdtemp(prefix="enzo-knob-")
    try:
        os.makedirs(os.path.join(sbx, "config"), exist_ok=True)
        os.makedirs(os.path.join(sbx, "data"), exist_ok=True)
        for f in ("enzo-config.yaml", "enzo-control.json", "enzo-watchlist.json",
                  "enzo-secrets.json"):
            src = os.path.join(ROOT, "config", f)
            if os.path.exists(src):
                shutil.copy(src, os.path.join(sbx, "config", f))
        yml = os.path.join(sbx, "config", "enzo-config.yaml")
        txt = io.open(yml, encoding="utf-8").read()
        txt = re.sub(r"refresh_seconds:\s*\d+", "refresh_seconds: 7", txt)
        txt = re.sub(r"activity_limit:\s*\d+", "activity_limit: 42", txt)
        io.open(yml, "w", encoding="utf-8").write(txt)

        os.environ["ENZO_HOME"] = sbx
        for m in [m for m in list(sys.modules) if m.startswith("enzo")]:
            del sys.modules[m]
        from enzo.ui import dashboard as dash2
        from enzo.core.config import load_config as lc2
        cfg2 = lc2()
        ok(int(cfg2["dashboard"]["refresh_seconds"]) == 7 and
           int(cfg2["dashboard"]["activity_limit"]) == 42,
           "الصندوق يقرأ القيم المعدَّلة (7 ثوانٍ / 42 حدثاً)")
        html = io.open(dash2.generate(), encoding="utf-8").read()
        m = re.search(r"refreshData\(false\); \}, (\d+)\)", html)
        ok(bool(m) and m.group(1) == "7000",
           "refresh_seconds يتحكّم فعلاً في فترة تحديث المتصفح",
           f"{m.group(1) if m else '?'}ms")
        serve_src = io.open(os.path.join(ROOT, "enzo", "ui", "serve.py"), encoding="utf-8").read()
        ok('get("activity_limit"' in serve_src,
           "serve.py يقرأ activity_limit من الإعداد (لا رقم ثابت)")
    finally:
        os.environ.pop("ENZO_HOME", None)
        for m in [m for m in list(sys.modules) if m.startswith("enzo")]:
            del sys.modules[m]
        shutil.rmtree(sbx, ignore_errors=True)


def test_python_floor_agreement():
    section("5. الحد الأدنى لبايثون: كلمة واحدة في كل الأدوات")
    readme = io.open(os.path.join(ROOT, "README.md"), encoding="utf-8").read()
    boot = io.open(os.path.join(ROOT, "bootstrap.sh"), encoding="utf-8").read()
    ctl = io.open(os.path.join(ROOT, "enzoctl"), encoding="utf-8").read()
    # README states it in prose, bootstrap.sh compares an integer, enzoctl
    # compares a tuple. They disagreed once (3.9 vs 3.10), so a host could pass
    # doctor and fail bootstrap - two tools, two answers.
    rm = re.search(r"Python\s+(\d+)\.(\d+)\+", readme)
    bs = re.search(r"-ge\s+(\d)(\d\d)\b", boot)
    ct = re.search(r"sys\.version_info\s*>=\s*\((\d+),\s*(\d+)\)", ctl)
    ok(bool(rm) and bool(bs) and bool(ct), "الأدوات الثلاث تذكر حداً أدنى قابلاً للقراءة",
       f"README={rm.groups() if rm else None} bootstrap={bs.groups() if bs else None} "
       f"enzoctl={ct.groups() if ct else None}")
    if rm and bs and ct:
        floors = {"README": f"{rm.group(1)}.{rm.group(2)}",
                  # bootstrap.sh compares major*100+minor, so 310 == 3.10 and
                  # 309 == 3.9: the last two digits ARE the minor version.
                  "bootstrap.sh": f"{bs.group(1)}.{int(bs.group(2))}",
                  "enzoctl doctor": f"{ct.group(1)}.{ct.group(2)}"}
        ok(len(set(floors.values())) == 1, "الحد الأدنى متطابق في README وbootstrap وdoctor",
           str(floors))
        ok(list(floors.values())[0] == "3.10", "الحد المعلن هو 3.10 (لم يُتحقق من 3.9)",
           list(floors.values())[0])


def main():
    test_parity()
    test_dead_keys()
    test_factory_penalty_wiring()
    test_dashboard_knobs_wired()
    test_python_floor_agreement()
    print("\n" + "=" * 68)
    print(f"  RESULT: {PASS} passed, {FAIL} failed")
    print("=" * 68)
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
