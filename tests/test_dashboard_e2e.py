#!/usr/bin/env python3
"""End-to-end guard for the dashboard: every route, every button, live HTTP.

Why this file exists
--------------------
The dashboard is the owner's ONLY window into a bot that trades real money. Two
classes of failure are invisible to unit tests and lethal in production:

  A. A control (button/tab/filter) that renders but does nothing — the handler
     is missing, misnamed, or bound to an id that does not exist. The page looks
     perfect; the owner clicks "Pause" during a rug and nothing happens.
  B. An API route the page calls that the server does not serve (or vice versa),
     so a panel silently stays empty.

This harness covers both, plus the data path added by the rug-protection layers:
new position fields (rug_flags, entry snapshot) must survive SQLite, must reach
/api/state, and must be drawn on screen.

Everything runs in a throwaway ENZO_HOME against a REAL server subprocess on a
free port. The production database and config are fingerprinted before and after
and asserted unchanged.

Optional browser layer: when jsdom is resolvable (NODE_PATH or ENZO_JSDOM_PATH),
tests/dashboard_browser_test.js loads the generated HTML in a DOM, executes the
real scripts against the live server and CLICKS EVERY BUTTON. Without jsdom that
part reports SKIP instead of pretending to have passed.
"""
import hashlib
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

PASS = FAIL = SKIP = 0


def ok(cond, label, extra=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  \033[32mPASS\033[0m  {label}" + (f"   {extra}" if extra else ""))
    else:
        FAIL += 1
        print(f"  \033[31mFAIL\033[0m  {label}" + (f"   {extra}" if extra else ""))
    return bool(cond)


def skip(label, extra=""):
    global SKIP
    SKIP += 1
    print(f"  \033[33mSKIP\033[0m  {label}" + (f"   {extra}" if extra else ""))


def section(t):
    print(f"\n=== {t} ===")


# ── sandbox: never touch the owner's money or state ──────────────────────────
SANDBOX = tempfile.mkdtemp(prefix="enzo-dash-e2e-")
os.makedirs(os.path.join(SANDBOX, "config"), exist_ok=True)
os.makedirs(os.path.join(SANDBOX, "data"), exist_ok=True)
for f in ("enzo-config.yaml", "enzo-control.json", "enzo-watchlist.json", "enzo-secrets.json"):
    src = os.path.join(ROOT, "config", f)
    if os.path.exists(src):
        shutil.copy(src, os.path.join(SANDBOX, "config", f))
os.environ["ENZO_HOME"] = SANDBOX

REAL_DB = os.path.join(ROOT, "data", "enzo.db")
REAL_CFG = os.path.join(ROOT, "config", "enzo-config.yaml")
REAL_CTL = os.path.join(ROOT, "config", "enzo-control.json")


def _fp(path):
    if not os.path.exists(path):
        return None
    with open(path, "rb") as fh:
        return hashlib.sha256(fh.read()).hexdigest()[:16], os.path.getsize(path)


BEFORE = {p: _fp(p) for p in (REAL_DB, REAL_CFG, REAL_CTL)}

from enzo.core import audit, db                      # noqa: E402
from enzo.core.config import load_config             # noqa: E402
from enzo.ui import dashboard                        # noqa: E402

CFG = load_config()
RUG = CFG.get("rug_protection", {}) or {}


# ── 1. generated HTML: static cross-checks ───────────────────────────────────
def static_checks():
    section("1. الصفحة المولَّدة: كل زر له معالج، وكل مسار له خادم")
    html_path = dashboard.generate()
    html = open(html_path, encoding="utf-8").read()
    ok(len(html) > 20000, "الصفحة تولّدت كاملة", f"{len(html)} حرف")

    serve_src = open(os.path.join(ROOT, "enzo", "ui", "serve.py"), encoding="utf-8").read()

    buttons = re.findall(r"<button\b[^>]*>", html)
    ok(len(buttons) >= 13, "عدد الأزرار في الصفحة ≥ 13", f"الموجود {len(buttons)}")

    onclicks = [re.search(r'onclick="([^"]+)"', b) for b in buttons]
    dead = [b for b, m in zip(buttons, onclicks) if not m]
    ok(not dead, "كل زر يحمل onclick (لا زر ميت شكلاً)", f"الميتة: {len(dead)}")

    fns = set()
    for m in onclicks:
        if not m:
            continue
        fm = re.match(r"\s*([A-Za-z_$][\w$]*)\s*\(", m.group(1))
        if fm:
            fns.add(fm.group(1))
    fns = sorted(fns)
    defined = set(re.findall(r"function\s+([A-Za-z_$][\w$]*)\s*\(", html))
    defined |= set(re.findall(r"(?:var|let|const)\s+([A-Za-z_$][\w$]*)\s*=\s*function", html))
    missing = [f for f in fns if f not in defined]
    ok(not missing, "كل معالج مذكور في onclick معرَّف فعلاً في JS",
       f"{len(fns)} معالج" + (f" — ناقص: {missing}" if missing else ""))

    fetches = sorted({u for u in re.findall(r"fetch\(\s*['\"]([^'\"]+)['\"]", html)})
    unserved = [u for u in fetches if u.split("?")[0] not in serve_src]
    ok(fetches and not unserved, "كل مسار تطلبه الصفحة موجود في الخادم",
       f"{len(fetches)} مسارات: {', '.join(fetches)}" + (f" — غير مخدوم: {unserved}" if unserved else ""))

    ids_used = sorted(set(re.findall(r"getElementById\(\s*['\"]([^'\"]+)['\"]", html)))
    ids_present = set(re.findall(r'\bid="([^"]+)"', html))
    # Some elements are CONDITIONAL: the server renders them only in a fault
    # state and the JS deliberately probes for their absence. Those are not
    # ghosts. A reference counts as guarded when the JS null-checks it before
    # use; anything else missing from the HTML is a real dead control.
    guarded = set(re.findall(
        r"if\s*\(\s*!\s*document\.getElementById\(\s*['\"]([^'\"]+)['\"]", html))
    for _var, _iid in re.findall(
            r"(?:var|let|const)\s+(\w+)\s*=\s*document\.getElementById\(\s*['\"]([^'\"]+)['\"]", html):
        if re.search(r"if\s*\(\s*!\s*" + re.escape(_var) + r"\b", html) or \
           re.search(re.escape(_var) + r"\s*&&", html):
            guarded.add(_iid)
    ghost = [i for i in ids_used if i not in ids_present and i not in guarded]
    ok(ids_used and not ghost, "كل عنصر يطلبه JS موجود في الصفحة أو محروس بفحص وجود",
       f"{len(ids_used)} معرِّفاً، {len(guarded & set(ids_used))} مشروط"
       + (f" — شبح: {ghost}" if ghost else ""))

    tabs = sorted(set(re.findall(r"switchTab\(\s*['\"]([^'\"]+)['\"]", html)))
    ghost_tabs = [t for t in tabs if t not in ids_present]
    ok(tabs and not ghost_tabs, "كل تبويب يستدعيه زر موجود في الصفحة",
       f"{len(tabs)} تبويبات" + (f" — ناقص: {ghost_tabs}" if ghost_tabs else ""))

    # The conditional element must be conditional in BOTH directions: it appears
    # when a previous render really failed, and disappears after recovery, so a
    # fault banner can never become a permanent false alarm on the owner screen.
    err_path = getattr(dashboard, "LAST_ERROR_PATH", None)
    if err_path:
        try:
            with open(err_path, "w", encoding="utf-8") as _ef:
                json.dump({"error": "synthetic render failure (test)"}, _ef)
            h2 = open(dashboard.generate(), encoding="utf-8").read()
            ok('id="serverFault"' in h2 and "synthetic render failure" in h2,
               "لافتة عطل التوليد تظهر حين يوجد عطل سابق فعلاً")
            # A successful render clears the error record by itself - assert that
            # self-healing instead of deleting the file behind its back.
            cleared = not os.path.exists(err_path)
            if os.path.exists(err_path):
                os.remove(err_path)
            h3 = open(dashboard.generate(), encoding="utf-8").read()
            ok(cleared, "التوليد الناجح يمحو سجلّ العطل بنفسه (تعافٍ ذاتي)")
            ok('id="serverFault"' not in h3,
               "وتختفي اللافتة بعد التعافي (لا إنذار كاذب دائم على الشاشة)")
        finally:
            if os.path.exists(err_path):
                os.remove(err_path)
    else:
        skip("لافتة عطل التوليد", "LAST_ERROR_PATH غير معرَّف")

    for key, label in (("rugProtectionCard", "بطاقة طبقات حماية الرغّ"),
                       ("rugProtectionStatus", "حالة الطبقات (ARMED/OFF)"),
                       ("function rugBadge", "دالة شارة العلم 🚩"),
                       ("rugBadge(p) +", "استدعاء الشارة داخل صف المركز"),
                       (".stage-pill.rug", "لون خاص لمخارج الرغّ")):
        ok(key in html, f"اللوحة تعرض {label}")

    ok(re.search(r"L1 · FINGERPRINT VETO · (ARMED|OFF)", html) is not None,
       "حالة الطبقة 1 مطبوعة في التشخيص")
    ok(str(int(RUG.get("tripwire_min_votes", 2))) in html and
       str(int(RUG.get("early_stop_pct", 12))) in html,
       "عتباتك الحقيقية (لا قيم افتراضية) هي المعروضة")

    # Fresh LIVE ledger (nothing seeded yet): the page itself must warn that the
    # $10,000 equity on the KPI cards is a placeholder, not the owner's money.
    ok('id="baselineFault"' in html,
       "دفتر جديد: اللوحة تحذّر أن $10,000 رقم افتراضي لا رصيدك")

    if shutil.which("node"):
        scripts = re.findall(r"<script[^>]*>(.*?)</script>", html, re.S)
        tmp = tempfile.NamedTemporaryFile("w", suffix=".js", delete=False, encoding="utf-8")
        tmp.write("\n;\n".join(scripts)); tmp.close()
        r = subprocess.run(["node", "--check", tmp.name], capture_output=True, text=True)
        ok(r.returncode == 0, "node --check: JavaScript المولَّد صالح",
           (r.stderr.strip().splitlines() or [""])[0][:160])
        os.unlink(tmp.name)
    else:
        skip("node --check على JS المولَّد", "node غير متوفر")
    return html_path, html


# ── 2. new position fields must survive SQLite ───────────────────────────────
ACTIVITY_LIMIT = 3
MINT = "FlaggedToken1111111111111111111111111111111"
RUG_MINT = "RuggedToken1111111111111111111111111111111"
FLAGS = ["bundlers_top20=4 (soft)", "avg_wallet_age_days=2.1 (< 3.0)"]


def seed_db():
    section("2. الحقول الجديدة تنجو من قاعدة البيانات (لا تُفقد عند إعادة التشغيل)")
    db.init_db()
    now = datetime.now(timezone.utc).isoformat()
    db.atomic_open_position({
        "mint": MINT, "symbol": "FLAG", "entry_price": 1e-6,
        "entry_market_cap": 100_000.0, "current_market_cap": 96_000.0,
        "size_usd": 1.0, "amount": 1e6, "stop_loss_mc": 62_000.0,
        "take_profit_mc": 130_000.0, "trailing_active": False,
        "stages_hit": [False, False, False], "opened_at": now,
        "rug_flags": FLAGS,
        "entry_liq": 50_000.0, "entry_holders": 400,
        "entry_top10_sells": 3, "entry_top10_dumping": False,
        "price_source": "pump_ws", "price_ts": time.time(),
    })
    st = db.get_full_state()
    p = (st.get("open_positions") or {}).get(MINT) or {}
    ok(p.get("rug_flags") == FLAGS, "rug_flags تعود من SQLite كما دخلت", str(p.get("rug_flags")))
    ok(float(p.get("entry_liq") or 0) == 50_000.0 and int(p.get("entry_holders") or 0) == 400,
       "لقطة الدخول (سيولة/حاملون) محفوظة — بدونها لا يعمل كاشف الرغّ بعد إعادة تشغيل")
    ok(p.get("price_source") == "pump_ws", "مصدر السعر ما زال محفوظاً مع المركز")

    db.atomic_close_position(RUG_MINT, {
        "symbol": "RUG", "entry_price": 1e-6, "exit_price": 6e-7,
        "entry_market_cap": 100_000.0, "exit_market_cap": 40_000.0,
        "size_usd": 1.0, "pnl": -0.4, "pnl_pct": -40.0,
        "reason": "RUG_TRIPWIRE(liquidity pulled 50,000 -> 20,000; holders collapsed 400 -> 210)",
        "opened_at": now, "closed_at": now,
    }, -0.4)
    closed = db.get_full_state().get("closed_positions") or []
    rec = next((c for c in closed if c.get("mint") == RUG_MINT), {})
    ok(str(rec.get("reason", "")).startswith("RUG_TRIPWIRE"),
       "سبب الخروج RUG_TRIPWIRE يُحفظ حرفياً في سجل الصفقات", str(rec.get("reason"))[:60])
    audit.log_event("ANALYSIS", "WARNING",
                    "IGNORE FLAG: RUG-FINGERPRINT: bundlers_top20=9 >= 6", {"mint": MINT})
    ok(len(audit.get_recent_activities(limit=10)) > 0, "تدفق النشاط يسجّل رفض البصمة")

    # ── Layer 0 on the dashboard: the reason and the gate evidence must reach
    #    the feed. The audit row always had them; the activity converter dropped
    #    them, so an IGNORE showed up as "SYM -> IGNORE (conf=0)" with no why.
    _FLOOD_DECISION = {
        "token_symbol": "FLOOD", "mint_address": "FloodMint111111111111111111111111111111111",
        "decision": "IGNORE", "confidence_score": 0,
        "decision_reason": "Rejected by quality gate: SNIPER_FLOOD_EARLY",
        "market_cap_usd": 42000.0,
        "axis_scores": {"security": 70, "momentum": 60},
        "rejected_signals": ["SNIPER_FLOOD_EARLY: 4 of the first 8 wallets are sniper-tagged "
                             "and bought $5,800 combined > $5,000 threshold",
                             "HOLDER_CONCENTRATION: top wallet 12.0% > max 10.0%"],
        "universe": {"pump_v1": True, "platform": "pump.fun", "phase": "migrated",
                     "phase_evidence": ["launchpad_status=2"],
                     "fees": {"ok": True, "value": 4.12, "unit": "sol"},
                     "snipers": {"sniper_count": 4, "sniper_total_usd": 5800}},
        "top_holder_pct": 12.0,
    }
    audit.record(_FLOOD_DECISION)
    _acts = audit.get_recent_activities(limit=15)
    _va = next((a for a in _acts if (a.get("data") or {}).get("mint", "").startswith("FloodMint")), None)
    ok(_va is not None, "قرار الرفض وصل إلى تدفق النشاط", str(_va and _va.get("category")))
    if _va:
        _d = _va.get("data") or {}
        ok(bool(_d.get("reason")), "ومعه السبب (كان يسقط في التحويل)", str(_d.get("reason"))[:60])
        ok(any("SNIPER_FLOOD_EARLY" in str(r) for r in (_d.get("rejected_signals") or [])),
           "ومعه رموز الرفض حرفية", str(_d.get("rejected_signals"))[:90])
        ok((_d.get("universe") or {}).get("phase") == "migrated",
           "ومعه دليل الكون (الطور/المنصّة/الرسوم/القنّاصون)", str(_d.get("universe"))[:90])
        ok(float(_d.get("top_holder_pct") or 0) == 12.0,
           "ومعه تركّز المحافظ المقاس", str(_d.get("top_holder_pct")))
        ok(_va.get("category") == "ANALYSIS", "وتصنيفه ANALYSIS (يظهر تحت 6-Axis Scans)",
           str(_va.get("category")))

    # ── the page must carry the new cards, the new filter and the thresholds ──
    _h2 = open(dashboard.generate(), encoding="utf-8").read()
    for needle, why in (
            ('id="universeGateCard"', "بطاقة «الكون المسموح · الطبقة 0»"),
            ('id="universeGateStatus"', "وحالة تسليحها N/5"),
            ('filterActivity(\'UNIVERSE\')', "زر تصفية أعتاب الدخول"),
            ('id="gmgnSourceCard"', "بطاقة صحة مصدر البيانات"),
            ('id="gmgnKeyStatus"', "وحالة مفتاح GMGN_API_KEY فيها"),
            ('id="gmgnLastError"', "وآخر خطأ من المزوّد (لا يُبتلع)"),
            ('$5,000', "حدّ ما قبل الترحيل"),
            ('$10,000', "حدّ ما بعد الترحيل"),
            ('2.5 SOL', "حدّ الرسوم مع وحدته المعلنة"),
            ('first 8 wallets', "نافذة أول 8"),
            ('HOLDER_CONCENTRATION', "رمز رفض تركّز المحافظ"),
            ('no trade tape', "والاعتراف الصريح بحدّ بديل القنّاصين")):
        ok(needle in _h2, f"اللوحة تعرض: {why}")

    # ── honesty: no API key => a loud banner, and it disappears when present ──
    _saved_key = os.environ.get("GMGN_API_KEY")
    _saved_home = os.environ.get("HOME")
    try:
        # HOME is pointed at the sandbox so a real ~/.config/gmgn/.env on this
        # machine cannot mask the test, and the key is set UNSET then SET
        # explicitly: the suite never relied on the environment having one.
        os.environ["HOME"] = SANDBOX
        os.environ.pop("GMGN_API_KEY", None)
        _h3 = open(dashboard.generate(), encoding="utf-8").read()
        ok('id="gmgnKeyFault"' in _h3,
           "بلا GMGN_API_KEY تظهر لافتة حمراء صريحة (لا لوحة تبدو سليمة)")
        ok("GMGN_API_KEY" in _h3 and "تقرأ «مجهول»" in _h3,
           "واللافتة تشرح الأثر: كل بوابات الدخول تقرأ «مجهول»")
        os.environ["GMGN_API_KEY"] = "test-key-present"
        _h4 = open(dashboard.generate(), encoding="utf-8").read()
        ok('id="gmgnKeyFault"' not in _h4,
           "وبوجود المفتاح تختفي اللافتة (لا إنذار دائم على بوت سليم)")
    finally:
        if _saved_key is None:
            os.environ.pop("GMGN_API_KEY", None)
        else:
            os.environ["GMGN_API_KEY"] = _saved_key
        if _saved_home is not None:
            os.environ["HOME"] = _saved_home

    # ── a GMGN ban must be visible as a GLOBAL fault: during a ban every gate
    #    reads "unknown", so the per-coin vetoes in the feed are not verdicts.
    try:
        db.rl_report_ban("gmgn", ban_duration_sec=90)
        _h7 = open(dashboard.generate(), encoding="utf-8").read()
        ok('id="gmgnBanFault"' in _h7, "حظر GMGN سارٍ ⇒ لافتة عامة على اللوحة")
        ok('id="gmgnBanRemain"' in _h7 and "ACTIVE" in _h7,
           "وبطاقة مصدر البيانات تعرض الحظر ومتى ينتهي")
        ok("SNIPER_DATA_UNAVAILABLE" in _h7 and "ليست أحكاماً على العملات" in _h7,
           "واللافتة تشرح أن رموز «مجهول» سببها المصدر لا العملات")
        ok("BANNED" in _h7, "ورأس البطاقة يقول BANNED بدل NORMAL")
        # rl_report_ban only ever EXTENDS a ban (max(banned_until, new)) - a fresh
        # report must never shorten a real one - so clearing needs rl_clear_ban,
        # which is what `./enzoctl unban --confirm` calls.
        db.rl_clear_ban("gmgn")
        _h8 = open(dashboard.generate(), encoding="utf-8").read()
        ok('id="gmgnBanFault"' not in _h8, "وبمسح الحظر تختفي اللافتة (لا إنذار دائم)")
    finally:
        try:
            db.rl_clear_ban("gmgn")
        except Exception:
            pass

    # ── a GUESSED SOL price belongs next to the money, not only in the log:
    #    with base_token=SOL every order size is derived from it, so a fallback
    #    reading means the SOL actually sent is worth more or less than intended.
    from enzo.execution import portfolio as _pf
    _snap_path = _pf.CAPITAL_PATH
    _saved_snap = (open(_snap_path, encoding="utf-8").read()
                   if os.path.exists(_snap_path) else None)

    def _write_snap(src, price):
        os.makedirs(os.path.dirname(_snap_path), exist_ok=True)
        with open(_snap_path, "w", encoding="utf-8") as _sf:
            json.dump({"ok": True, "source": "wallet", "usd": 7.0, "total_usd": 7.0,
                       "spendable_usd": 6.6, "base_token": "SOL", "sol": 0.035,
                       "sol_price": price, "sol_price_source": src,
                       "ts": time.time()}, _sf)

    try:
        _write_snap("fallback", 180.0)
        _h5 = open(dashboard.generate(), encoding="utf-8").read()
        ok('id="solPriceFault"' in _h5,
           "لقطة بسعر SOL مخمَّن ⇒ لافتة صريحة على اللوحة (لا حجم خاطئ بصمت)")
        ok("DexScreener" in _h5 and "base_token=SOL" in _h5,
           "واللافتة تسمّي المصدر والسبب وأثره على حجم الأمر")
        _write_snap("dexscreener", 203.4)
        _h6 = open(dashboard.generate(), encoding="utf-8").read()
        ok('id="solPriceFault"' not in _h6,
           "وبعودة القراءة الحيّة تختفي اللافتة (لا إنذار دائم)")
    finally:
        try:
            if _saved_snap is not None:
                open(_snap_path, "w", encoding="utf-8").write(_saved_snap)
            elif os.path.exists(_snap_path):
                os.remove(_snap_path)
        except Exception:
            pass

    # ...and the warning must disappear once the ledger is real (there is now a
    # closed trade), otherwise it would cry forever on a working bot.
    _h = open(dashboard.generate(), encoding="utf-8").read()
    ok('id="baselineFault"' not in _h,
       "وبعدما صار للدفتر سجلّ حقيقي يختفي التحذير (لا إنذار دائم)")

    # dashboard.activity_limit must be a real knob, not decoration: cap it in the
    # sandbox config, seed more events than the cap, and let the LIVE server
    # prove the cap is applied on the wire.
    yml = os.path.join(SANDBOX, "config", "enzo-config.yaml")
    txt = open(yml, encoding="utf-8").read()
    txt = re.sub(r"activity_limit:\s*\d+", f"activity_limit: {ACTIVITY_LIMIT}", txt)
    open(yml, "w", encoding="utf-8").write(txt)
    for i in range(6):
        audit.log_event("SYSTEM", "INFO", f"filler event {i} for the activity cap test", {})
    ok(f"activity_limit: {ACTIVITY_LIMIT}" in open(yml, encoding="utf-8").read(),
       f"صندوق الاختبار يضبط activity_limit={ACTIVITY_LIMIT} مع 7 أحداث مزروعة")
    # The cap test just pushed older events out of the live feed, so re-seed the
    # Layer-0 veto AFTER the fillers: the DOM clicks that follow must be able to
    # see a real Gate-Veto event through the wire, not only in-process.
    audit.record(_FLOOD_DECISION)
    ok(any("SNIPER_FLOOD_EARLY" in str(r)
           for a in audit.get_recent_activities(limit=int(ACTIVITY_LIMIT))
           for r in ((a.get("data") or {}).get("rejected_signals") or [])),
       "حدث رفض الطبقة 0 ما زال داخل حدّ النشاط الحيّ (لا يدفعه الحشو خارجه)")


# ── 3. live server over real HTTP ────────────────────────────────────────────
def free_port():
    s = socket.socket(); s.bind(("127.0.0.1", 0)); p = s.getsockname()[1]; s.close(); return p


def http(method, url, timeout=40):
    req = urllib.request.Request(url, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            body = r.read().decode("utf-8", "replace")
            return r.status, body
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", "replace")
    except Exception as e:                                     # noqa: BLE001
        return 0, f"{type(e).__name__}: {e}"


def live_checks(html_path):
    section("3. خادم حقيقي على منفذ حر: كل مسار يُختبر عبر HTTP")
    port = free_port()
    env = dict(os.environ, ENZO_HOME=SANDBOX, PYTHONUNBUFFERED="1")
    # Server output goes to a FILE, never to a pipe we do not drain: a full
    # pipe blocks the child, and a blocked child looks exactly like a hung test.
    log_path = os.path.join(SANDBOX, "server.log")
    log_fh = open(log_path, "w", encoding="utf-8")
    proc = subprocess.Popen([sys.executable, "-m", "enzo.ui.serve", str(port)],
                            cwd=ROOT, env=env,
                            stdout=log_fh, stderr=subprocess.STDOUT, text=True)
    proc._enzo_log = log_path
    base = f"http://127.0.0.1:{port}"
    up = False
    health_body = ""
    try:
        # Readiness = the server ANSWERS. /health legitimately returns 503 when
        # the bot is degraded (engine never scanned, no wallet in this sandbox),
        # so waiting for 200 here would spin forever on a healthy HTTP server.
        for _ in range(40):
            if proc.poll() is not None:
                break
            code, health_body = http("GET", base + "/health", timeout=5)
            if code in (200, 503):
                up = True
                break
            time.sleep(0.5)
        ok(up, "الخادم انطلق وردّ على /health", f"HTTP {code}" if up else f"exit={proc.poll()}")
        if up:
            try:
                hj = json.loads(health_body)
            except Exception:
                hj = {}
            ok("status" in hj and isinstance(hj.get("problems"), list),
               "/health يعيد حالة وقائمة مشاكل بنيوية", str(hj.get("status")))
            ok(code == 503 and any("ENGINE_NEVER_SCANNED" in p for p in hj.get("problems", [])),
               "/health صادق: يعلن التدهور بدل أخضر كاذب (المحرك لم يفحص في الصندوق)",
               "; ".join(hj.get("problems", []))[:120])
        if not up:
            try:
                print("  --- سجل الخادم ---\n" + open(log_path, encoding="utf-8").read()[-1500:])
            except Exception:
                pass
            return base, proc, None

        code, body = http("GET", base + "/")
        ok(code == 200 and "rugProtectionCard" in body,
           "GET / يعيد اللوحة كاملة وفيها بطاقة الحماية", f"HTTP {code}, {len(body)} حرف")
        ok(code == 200 and body.count("<button") >= 14,
           "كل الأزرار وصلت عبر HTTP (13 + زر أعتاب الدخول الجديد)",
           f"{body.count('<button')} زر")
        ok(code == 200 and "universeGateCard" in body,
           "وبطاقة الطبقة 0 وصلت عبر HTTP لا في الملف المولّد فقط")

        code, body = http("GET", base + "/api/state")
        js = {}
        try:
            js = json.loads(body)
        except Exception:
            js = {}
        ok(code == 200 and js.get("status") == "success", "GET /api/state = 200 وحالة success", f"HTTP {code}")

        ug = (js.get("config_summary") or {}).get("universe_gates") or {}
        ok(bool(ug), "/api/state يعرّض universe_gates", str(list(ug)[:6]))
        ok(ug.get("pump_v1_only") is True and float(ug.get("pre_min_market_cap") or 0) == 5000.0
           and float(ug.get("pre_min_sells") or 0) == 10.0
           and float(ug.get("mig_min_market_cap") or 0) == 10000.0
           and float(ug.get("mig_min_total_fees") or 0) == 2.5
           and str(ug.get("mig_fees_unit")) == "sol"
           and int(ug.get("sniper_first_n") or 0) == 8
           and float(ug.get("sniper_max_total_usd") or 0) == 5000.0
           and float(ug.get("max_holder_percentage") or 0) == 10.0,
           "وقيمها = إعدادك فعلاً (لا افتراضيات)", str(ug)[:150])
        gs = js.get("gmgn_status") or {}
        ok("api_key_present" in gs and "discovery" in gs,
           "/api/state يعرّض حالة مصدر البيانات GMGN", str(list(gs)[:6]))

        rp = (js.get("config_summary") or {}).get("rug_protection") or {}
        ok(len(rp) == 18, "ملخّص الإعداد يعرّض مفاتيح rug_protection الثمانية عشر كلها",
           f"الموجود {len(rp)}")
        mismatch = [k for k in ("early_stop_pct", "tripwire_min_votes", "veto_bundlers_top20",
                                "tripwire_liq_pull_pct", "veto_avg_wallet_age_days")
                    if k in RUG and float(rp.get(k, -1)) != float(RUG[k])]
        ok(not mismatch, "القيم المعروضة = قيم إعدادك فعلاً (لا افتراضيات)",
           f"مختلف: {mismatch}" if mismatch else "")
        ok(all(rp.get(k) is True for k in ("fingerprints_enabled", "early_stop_enabled", "tripwire_enabled")),
           "الطبقات الثلاث تظهر مفعّلة كما في إعدادك")

        op = (js.get("open_positions") or {}).get(MINT) or {}
        ok(op.get("rug_flags") == FLAGS, "المركز المشبوب يصل إلى الواجهة بأعلامه",
           str(op.get("rug_flags")))
        ok(op.get("price_is_live") in (True, False) and "unrealized_pnl_pct" in op,
           "حقول السعر والربح غير المحقق ما زالت تصل (لم يكسرها التعديل)")
        closed = js.get("closed_positions") or []
        ok(any(str(c.get("reason", "")).startswith("RUG_TRIPWIRE") for c in closed),
           "صفقة RUG_TRIPWIRE تصل إلى جدول الصفقات")

        sysblk = js.get("system") or {}
        ok("is_paused" in sysblk and "mode" in sysblk, "كتلة النظام (الإيقاف/الوضع) سليمة")

        code, body = http("GET", base + "/api/activity")
        act = {}
        try:
            act = json.loads(body)
        except Exception:
            act = {}
        items = act.get("activities") or act.get("activity") or []
        ok(code == 200 and isinstance(items, list) and len(items) > 0,
           "GET /api/activity يعيد عناصر فعلية", f"HTTP {code}, {len(items)} عنصر")
        ok(len(items) <= ACTIVITY_LIMIT,
           f"activity_limit يُحترم على السلك فعلاً (≤ {ACTIVITY_LIMIT} من 7 مزروعة)",
           f"عاد {len(items)}")
        ok(any("RUG-FINGERPRINT" in json.dumps(i) for i in items),
           "رفض البصمة يظهر في النشاط — تراه بعينك لا في السجل فقط")

        code, _b = http("GET", base + "/api/prices")
        ok(code == 200, "GET /api/prices = 200", f"HTTP {code}")

        code, body = http("GET", base + "/api/definitely-not-a-route")
        ok(code == 404, "مسار غير معروف = 404 (لا صفحة فارغة توحي بالنجاح)", f"HTTP {code}")

        # control toggle: must flip the sandbox control file, twice
        ctl = os.path.join(SANDBOX, "config", "enzo-control.json")
        code, body = http("POST", base + "/api/control/toggle")
        paused1 = json.loads(body).get("paused") if body.strip().startswith("{") else None
        disk1 = json.load(open(ctl, encoding="utf-8")).get("paused") if os.path.exists(ctl) else None
        ok(code == 200 and paused1 is True and disk1 is True,
           "زر الإيقاف يوقف فعلاً (الخادم + القرص)", f"HTTP {code}, paused={paused1}, disk={disk1}")
        code, body = http("POST", base + "/api/control/toggle")
        paused2 = json.loads(body).get("paused") if body.strip().startswith("{") else None
        disk2 = json.load(open(ctl, encoding="utf-8")).get("paused") if os.path.exists(ctl) else None
        ok(code == 200 and paused2 is False and disk2 is False,
           "زر الاستئناف يستأنف فعلاً (لا يبقى البوت موقوفاً بالخطأ)", f"paused={paused2}, disk={disk2}")

        code, body = http("POST", base + "/api/scan")
        ok(code == 200, "POST /api/scan لا ينهار حين لا يكون المحرك شغالاً", f"HTTP {code}")

        jsdom_result = run_jsdom(base, html_path)
        return base, proc, jsdom_result
    finally:
        pass


def run_jsdom(base, html_path):
    section("4. متصفح وهمي (jsdom): نقر كل زر على خادم حي")
    candidates = [os.environ.get("ENZO_JSDOM_PATH"),
                  "/tmp/node_modules",
                  "/tmp/dashtest/node_modules",
                  os.path.join(ROOT, "node_modules"),
                  os.path.join(ROOT, "tests", "node_modules")]
    node_path = next((c for c in candidates if c and os.path.isdir(os.path.join(c, "jsdom"))), None)
    if node_path is None and shutil.which("node"):
        # Ask node itself where jsdom resolves from, then take its node_modules.
        try:
            r = subprocess.run(["node", "-e",
                                "try{console.log(require.resolve('jsdom'))}catch(e){}"],
                               capture_output=True, text=True, timeout=30)
            resolved = (r.stdout or "").strip()
            parts = resolved.split(os.sep)
            if "node_modules" in parts:
                node_path = os.sep.join(parts[:parts.index("node_modules") + 1])
        except Exception:                                      # noqa: BLE001
            node_path = None
    if not node_path or not shutil.which("node"):
        skip("نقر الأزرار داخل DOM", "jsdom غير متوفر — ثبّته وشغّل ENZO_JSDOM_PATH=/path/to/node_modules")
        return None
    script = os.path.join(ROOT, "tests", "dashboard_browser_test.js")
    env = dict(os.environ, NODE_PATH=node_path)
    try:
        r = subprocess.run(["node", script, base, html_path], capture_output=True, text=True,
                           env=env, timeout=180)
    except subprocess.TimeoutExpired as e:
        ok(False, "اختبار jsdom انتهى في وقته (لا تعليق)", "تجاوز 180 ثانية")
        return (0, 1)
    out = (r.stdout or "") + (r.stderr or "")
    for line in out.splitlines():
        if line.strip():
            print("   " + line.rstrip())
    m = re.search(r"RESULT:\s*(\d+)\s*passed,\s*(\d+)\s*failed", out)
    if not m:
        ok(False, "اختبار jsdom أعاد نتيجة قابلة للقراءة", out.strip()[-200:])
        return (0, 1)
    p, f = int(m.group(1)), int(m.group(2))
    global PASS, FAIL
    PASS += p
    FAIL += f
    ok(f == 0, f"كل نقرات الأزرار داخل DOM نجحت ({p} تحقّقاً)")
    return (p, f)


def main():
    html_path, _html = static_checks()
    seed_db()
    base, proc, _jsdom = live_checks(html_path)

    section("5. مالك البوت: لا شيء من أموالك أو إعدادك لُمس")
    if proc is not None and proc.poll() is None:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except Exception:
            proc.kill()
    after = {p: _fp(p) for p in (REAL_DB, REAL_CFG, REAL_CTL)}
    ok(after == BEFORE, "قاعدة البيانات الحقيقية والإعداد الحقيقي بلا تغيير",
       "" if after == BEFORE else f"{BEFORE} -> {after}")
    ok(os.path.exists(os.path.join(SANDBOX, "data", "enzo.db")),
       "كل ما كُتب كان داخل الصندوق المعزول")

    shutil.rmtree(SANDBOX, ignore_errors=True)
    print("\n" + "=" * 68)
    print(f"  RESULT: {PASS} passed, {FAIL} failed" + (f", {SKIP} skipped" if SKIP else ""))
    print("=" * 68)
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
