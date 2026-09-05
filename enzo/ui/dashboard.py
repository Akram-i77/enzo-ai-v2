#!/usr/bin/env python3
"""
ENZO - High-Performance Cyber/Fintech Live Dashboard Generator
Produces a self-contained, interactive, modern dashboard with real-time charting,
multi-tab workflows, 6-axis AI matrices, live bot activity stream, and responsive controls.
"""
import html
import os
import json
import time
from datetime import datetime, timezone
from typing import Dict, Any, List

from enzo.core.config import (
    load_config,
    DASHBOARD_HTML_PATH,
    HEALTH_PATH,
)
from enzo.execution import portfolio as pf
from enzo.core import learn, audit
from enzo.ui import botctl
from enzo.providers import gmgn, pump

# Written next to the HTML whenever generate() fails, so the server can show a
# real error banner instead of silently serving a stale page.
LAST_ERROR_PATH = DASHBOARD_HTML_PATH + ".error"
LAST_GOOD_PATH = DASHBOARD_HTML_PATH + ".last-good"
# In-memory copy of the last successful render + its timestamp. Lets the HTTP
# server answer instantly and lets the client show a "data is N seconds old"
# warning when regeneration has started failing.
_LAST_RENDER = {"html": None, "ts": 0.0, "error": None}


def _esc(x) -> str:
    return html.escape(str(x if x is not None else ""))


def runtime_health() -> dict:
    """Read the supervisor/runtime heartbeat (written by enzoctl + the engine).

    Returns {} when the bot has never been started through the supervisor.
    """
    try:
        if os.path.exists(HEALTH_PATH):
            with open(HEALTH_PATH, "r", encoding="utf-8") as f:
                data = json.load(f)
                return data if isinstance(data, dict) else {}
    except Exception:
        pass
    return {}


def runtime_scan_interval() -> int:
    return int(runtime_health().get("scan_interval_sec") or 0)


def last_render() -> dict:
    """{'html': str|None, 'ts': float, 'age_sec': float, 'error': str|None}"""
    ts = _LAST_RENDER.get("ts") or 0.0
    return {
        "html": _LAST_RENDER.get("html"),
        "ts": ts,
        "age_sec": round(time.time() - ts, 1) if ts else None,
        "error": _LAST_RENDER.get("error"),
    }


def generate_safe() -> dict:
    """generate() that never raises — used by background loops.

    Returns {"ok": bool, "path": str|None, "error": str|None}. On failure the
    previously rendered HTML stays on disk AND the error is recorded so the UI
    can display it, instead of the old behaviour where the exception vanished
    into `except: pass` and the browser silently got a stale page.
    """
    try:
        path = generate()
        return {"ok": True, "path": path, "error": None}
    except Exception as e:
        import traceback
        err = f"{type(e).__name__}: {e}"
        tb = traceback.format_exc(limit=6)
        _LAST_RENDER["error"] = err
        try:
            with open(LAST_ERROR_PATH, "w", encoding="utf-8") as f:
                f.write(json.dumps({"error": err, "traceback": tb,
                                    "ts": datetime.now(timezone.utc).isoformat()}, indent=2))
        except Exception:
            pass
        return {"ok": False, "path": None, "error": err}


def generate() -> str:
    """Generate and write the comprehensive dashboard HTML file.

    Raises on failure — callers must NOT swallow this silently. Before
    2026-09-03 an undefined `wallet_name` made every call raise NameError, the
    `except: pass` in serve.py hid it, and the HTTP server then served the
    stale `data/enzo-dashboard.html` left over from an older code revision.
    That is why the dashboard looked "never updated".
    """
    state = pf.get_state()
    cfg = load_config()
    learning = learn.get_state()
    paused = botctl.is_paused()
    halted = state.get("halted")

    init_cap = float(state.get("initial_capital", 10000.0))
    eq = float(state.get("equity", init_cap))
    # Live wallet value (fresh snapshot from sync_capital_base). The ledger
    # equity is the ROI/drawdown baseline and can lag the real wallet for a
    # long time; an operator holding $7 of SOL was shown "$2.06" and rightly
    # called it wrong. Show the wallet when we have a fresh reading, and keep
    # the ledger figure visible in the sub-line so nothing is hidden.
    wallet_usd = pf.live_wallet_usd()
    eq_display = float(wallet_usd) if wallet_usd else eq
    rp = float(state.get("realized_pnl", 0.0))
    closed = state.get("closed_positions", [])
    wins = [c for c in closed if float(c.get("pnl", 0)) > 0]
    losses = [c for c in closed if float(c.get("pnl", 0)) <= 0]
    total_trades = len(closed)
    win_rate = (len(wins) / total_trades * 100) if total_trades else 0.0

    # ── Values the template interpolates. Every one of these must exist or the
    #    whole f-string raises NameError and no dashboard is produced at all. ──
    paper = bool(cfg.get("paper_mode", True))
    ex_cfg = cfg.get("execution", {}) or {}
    rm_cfg = cfg.get("risk_management", {}) or {}
    wallet_name = str(ex_cfg.get("wallet_name") or "not-set")
    base_token = str(ex_cfg.get("base_token") or "USDC").upper()
    mode_label = "PAPER MODE • " if paper else "REAL TRADING ✅ • "
    executor_label = "MOONPAY CLI" if paper else f"MOONPAY CLI ({base_token} base)"
    max_open = int(rm_cfg.get("max_open_positions", 5))
    max_exposure = float(rm_cfg.get("max_exposure", 30.0))
    risk_per_trade = float(rm_cfg.get("risk_per_trade", 2.5))
    max_daily_loss = float(rm_cfg.get("max_daily_loss", 8.0))
    max_drawdown = float(rm_cfg.get("max_drawdown", 25.0))
    cons_limit = int(rm_cfg.get("consecutive_losses_limit", 12))
    # Rug-protection layers (1/3/4) — baked into the Diagnostics tab so the owner
    # can see at a glance whether each layer is armed and with which numbers,
    # instead of having to open the YAML. Values come from the loaded config, so
    # they are always the ones the engine is actually running with.
    rug_cfg = cfg.get("rug_protection", {}) or {}
    _on = lambda b: ("ARMED" if b else "OFF")
    _cls = lambda b: ("color-pos" if b else "color-neg")
    rug_l1_on = bool(rug_cfg.get("fingerprints_enabled", True))
    rug_l3_on = bool(rug_cfg.get("early_stop_enabled", True))
    rug_l4_on = bool(rug_cfg.get("tripwire_enabled", True))
    rug_l1_txt = _on(rug_l1_on); rug_l1_cls = _cls(rug_l1_on)
    rug_l3_txt = _on(rug_l3_on); rug_l3_cls = _cls(rug_l3_on)
    rug_l4_txt = _on(rug_l4_on); rug_l4_cls = _cls(rug_l4_on)
    rug_veto_bundlers = int(rug_cfg.get("veto_bundlers_top20", 6))
    rug_veto_snipers = int(rug_cfg.get("veto_snipers_top20", 8))
    rug_veto_rats = int(rug_cfg.get("veto_rats_top20", 5))
    rug_veto_sells = int(rug_cfg.get("veto_top10_cur_sells", 25))
    rug_veto_age = float(rug_cfg.get("veto_avg_wallet_age_days", 3.0))
    rug_veto_factory = int(rug_cfg.get("veto_factory_created", 50))
    rug_veto_open_ratio = float(rug_cfg.get("veto_factory_open_ratio", 0.03)) * 100.0
    rug_flag_ratio = float(rug_cfg.get("soft_flag_ratio", 0.5)) * 100.0
    rug_es_pct = float(rug_cfg.get("early_stop_pct", 12.0))
    rug_es_win = float(rug_cfg.get("early_stop_window_min", 10.0))
    rug_tw_poll = float(rug_cfg.get("tripwire_poll_sec", 20.0))
    rug_tw_votes = int(rug_cfg.get("tripwire_min_votes", 2))
    rug_tw_liq = float(rug_cfg.get("tripwire_liq_pull_pct", 40.0))
    rug_tw_hold = float(rug_cfg.get("tripwire_holder_drop_pct", 15.0))
    rug_tw_sells = int(rug_cfg.get("tripwire_top10_sells_jump", 15))
    # dashboard.refresh_seconds drives the browser polling interval. It was a
    # hardcoded 10000ms while the config advertised the knob.
    _dash_cfg = cfg.get("dashboard", {}) or {}
    dash_refresh_ms = max(2000, int(float(_dash_cfg.get("refresh_seconds", 10)) * 1000))
    rug_armed = sum(1 for b in (rug_l1_on, rug_l3_on, rug_l4_on) if b)
    rug_hdr_txt = f"{rug_armed}/3 ARMED"
    rug_hdr_cls = "color-pos" if rug_armed == 3 else ("color-neg" if rug_armed == 0 else "color-warn")
    rug_l1_pill = "hit" if rug_l1_on else ""
    rug_l3_pill = "hit" if rug_l3_on else ""
    rug_l4_pill = "hit" if rug_l4_on else ""
    capital_source = str(ex_cfg.get("capital_source", "wallet"))
    generated_at = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    scan_interval = 0
    try:
        scan_interval = int(runtime_scan_interval())
    except Exception:
        scan_interval = 0
    open_count = len(state.get("open_positions", {}) or {})
    exposure_used = sum(float(p.get("size_usd", 0.0) or 0.0)
                        for p in (state.get("open_positions") or {}).values())
    exposure_pct = (exposure_used / eq * 100.0) if eq > 0 else 0.0
    _gm = ((cfg.get("data_sources", {}) or {}).get("gmgn", {}) or {})
    try:
        _rps = float(_gm.get("requests_per_sec", 0.8))
        _gap_ms = float(_gm.get("request_gap_ms", 350))
        _burst = float(_gm.get("burst_capacity", 2.5))
        # Honest label. The old one derived "req/s" from the gap alone
        # (1000/350 = 2.9 req/s) while the token bucket actually refills at
        # requests_per_sec (0.8) - the page advertised a speed the bot never had.
        gmgn_rate_label = f"{_rps:.1f} req/s · {_gap_ms:.0f} ms gap · burst {_burst:.1f}"
    except Exception:
        gmgn_rate_label = "default pacing"

    # GMGN is the SOLE market-data source, so its health belongs on the page:
    # a missing API key or a CLI whose flags changed makes every gate read
    # nothing while the dashboard still looks perfectly healthy.
    try:
        _pst = gmgn.provider_status() or {}
    except Exception:
        _pst = {}
    try:
        _dst = gmgn.discovery_status() or {}
    except Exception:
        _dst = {}
    gmgn_key_ok = bool(_pst.get("api_key_present"))
    gmgn_last_err = str(_pst.get("last_error") or "")
    gmgn_last_ep = str(_pst.get("last_error_endpoint") or "")
    _dialect = _pst.get("addr_dialect") or {}
    gmgn_dialect_label = ", ".join(f"{k}={v}" for k, v in list(_dialect.items())[:4]) \
        or "not negotiated yet (no token call since start)"
    _cats = _dst.get("categories_ok") or {}
    _cat_bits = []
    for _cn, _cv in _cats.items():
        if _cv.get("skipped"):
            _cat_bits.append(f"{_cn}: skipped (not a gmgn-cli v1.6 command)")
        elif _cv.get("ok"):
            _cat_bits.append(f"{_cn}: {_cv.get('count')} token(s)")
        else:
            _cat_bits.append(f"{_cn}: FAILED")
    gmgn_cats_label = " · ".join(_cat_bits) if _cat_bits else "no discovery sweep in this process yet"
    _cats_tried = [c for c, v in _cats.items() if not v.get("skipped")]
    _cats_failed = [c for c, v in _cats.items() if v.get("ok") is False and not v.get("skipped")]
    gmgn_dead = bool(_cats_tried) and len(_cats_failed) == len(_cats_tried)
    gmgn_last_count = _dst.get("last_count")
    if not gmgn_key_ok:
        gmgn_hdr_cls, gmgn_hdr_txt = "color-neg", f"NO API KEY · {gmgn_rate_label}"
    elif gmgn_dead:
        gmgn_hdr_cls, gmgn_hdr_txt = "color-neg", f"ALL CATEGORIES FAILED · {gmgn_rate_label}"
    elif gmgn_last_err:
        gmgn_hdr_cls, gmgn_hdr_txt = "color-warn", f"DEGRADED · {gmgn_rate_label}"
    else:
        gmgn_hdr_cls, gmgn_hdr_txt = "color-pos", f"NORMAL · {gmgn_rate_label}"

    # ── Entry universe (Layer 0): Pump V1 only, phase floors, sniper flood ───
    _tu = cfg.get("token_universe", {}) or {}
    _pg = cfg.get("phase_gates", {}) or {}
    _sf = cfg.get("sniper_flood", {}) or {}
    _pre = _pg.get("pre_migration", {}) or {}
    _mig = _pg.get("migrated", {}) or {}
    _ma = cfg.get("market_analysis", {}) or {}
    uni_pump_only = bool(_tu.get("pump_v1_only", True))
    uni_reject_unknown = bool(_tu.get("reject_unknown_launchpad", True))
    uni_pre_mc = _pre.get("min_market_cap")
    uni_pre_sells = _pre.get("min_sells")
    uni_mig_mc = _mig.get("min_market_cap")
    uni_mig_fees = _mig.get("min_total_fees")
    uni_fees_unit = str(_mig.get("fees_unit", "sol") or "sol").upper()
    uni_require_fees = bool(_mig.get("require_known_fees", True))
    uni_unknown_phase = str(_pg.get("unknown_phase", "strict") or "strict")
    uni_sniper_on = bool(_sf.get("enabled", True))
    uni_first_n = int(_sf.get("first_n", 8) or 0)
    uni_min_snipers = int(_sf.get("min_sniper_count", 4) or 0)
    uni_max_total = float(_sf.get("max_total_sniper_buy_usd", 5000) or 0)
    uni_max_single = float(_sf.get("max_single_sniper_buy_usd", 5000) or 0)
    uni_on_unknown = str(_sf.get("on_unknown", "reject") or "reject")
    uni_holder_cap = _ma.get("max_holder_percentage")
    uni_holder_on = uni_holder_cap is not None and float(uni_holder_cap or 0) > 0

    def _usd(v):
        return f"${float(v):,.0f}" if v is not None else "not set"

    def _num(v):
        return f"{float(v):g}" if v is not None else "not set"

    uni_pre_on = uni_pre_mc is not None or uni_pre_sells is not None
    uni_mig_on = uni_mig_mc is not None or uni_mig_fees is not None
    uni_checks = [uni_pump_only, uni_pre_on, uni_mig_on, uni_sniper_on, uni_holder_on]
    uni_armed = sum(1 for b in uni_checks if b)
    uni_hdr_txt = f"{uni_armed}/5 ARMED"
    uni_hdr_cls = "color-pos" if uni_armed == 5 else ("color-neg" if uni_armed == 0 else "color-warn")
    uni_p1_pill = "hit" if uni_pump_only else ""
    uni_pre_pill = "hit" if uni_pre_on else ""
    uni_mig_pill = "hit" if uni_mig_on else ""
    uni_sn_pill = "hit" if uni_sniper_on else ""
    uni_hc_pill = "hit" if uni_holder_on else ""

    # Server-side fault banner. Rendered only when the previous regeneration
    # failed (so a recovered bot shows a clean page again on the next render).
    _prev_err = None
    try:
        if os.path.exists(LAST_ERROR_PATH):
            with open(LAST_ERROR_PATH, "r", encoding="utf-8") as _ef:
                _prev_err = (json.load(_ef) or {}).get("error")
    except Exception:
        _prev_err = None
    if _prev_err:
        banner_html = (
            '<div id="serverFault" class="fault-banner shown">'
            '<span class="fault-icon">⚠</span>'
            f'<span>The previous dashboard render failed: {_esc(_prev_err)}. '
            'This page was regenerated successfully.</span>'
            '<span class="fault-hint">See data/enzo-dashboard.html.error for the traceback.</span>'
            '</div>'
        )
    else:
        banner_html = ""

    # A brand-new LIVE ledger starts on the fictitious 10,000 default and the
    # engine rebases it onto the real wallet at the first successful read. Until
    # then the KPI cards would show $10,000 of equity that does not exist, and
    # the drawdown breaker would be anchored to it. `enzoctl doctor` reports this
    # as `ledger_baseline`; the page must say it too, because the page is what the
    # owner actually looks at.
    _fictitious_baseline = (
        not bool(cfg.get("paper_mode", True))
        and not (state.get("closed_positions") or [])
        and not (state.get("open_positions") or {})
        and abs(init_cap - 10000.0) < 1e-6)
    if _fictitious_baseline:
        banner_html += (
            '<div id="baselineFault" class="fault-banner shown">'
            '<span class="fault-icon">⚠</span>'
            '<span>رأس المال المعروض ($10,000) هو <b>الرقم الافتراضي</b> لا رصيدك الحقيقي: '
            'الدفتر جديد ولم تُقرأ المحفظة بعد. شغّل المحرك دورة واحدة ليُضبط الأساس على '
            'رصيدك تلقائياً، أو نفّذ <code>./enzoctl rebase --confirm</code>. '
            'حتى ذلك الحين أساس التراجع وROI محسوبان على رقم غير موجود.</span>'
            '<span class="fault-hint">Equity baseline is the placeholder default until the wallet is read.</span>'
            '</div>'
        )

    if not gmgn_key_ok:
        banner_html += (
            '<div id="gmgnKeyFault" class="fault-banner shown">'
            '<span class="fault-icon">⚠</span>'
            '<span><b>GMGN_API_KEY غير موجود</b> — أداة gmgn-cli (v1.6) ترفض كل نداء بدونه، '
            'فلا تصل أي بيانات سوق: الاكتشاف يُرجع صفراً وكل بوابات الدخول (Pump V1، الطور، '
            'القيمة السوقية، البيع، الرسوم، القنّاصون، تركّز المحافظ) تقرأ «مجهول». '
            'صدّره في البيئة التي يُقلع منها البوت (أو ضعه في <code>~/.config/gmgn/.env</code>) '
            'ثم أعد التشغيل.</span>'
            '<span class="fault-hint">Verify with: ./enzoctl doctor (gmgn_api_key) · ./enzoctl probe &lt;MINT&gt;</span>'
            '</div>'
        )
    elif gmgn_dead:
        banner_html += (
            '<div id="gmgnDeadFault" class="fault-banner shown">'
            '<span class="fault-icon">⚠</span>'
            '<span>كل فئات الاكتشاف فشلت في آخر دورة — النتيجة <b>لم تُخزَّن</b> كإجابة، '
            'وستُعاد المحاولة الدورة القادمة. هذا يعني مصدر بيانات معطلاً أو محدوداً، '
            'لا سوقاً هادئاً.</span>'
            f'<span class="fault-hint">{_esc(gmgn_cats_label)} · ./enzoctl scan --force ثم ./enzoctl logs</span>'
            '</div>'
        )

    html_content = f"""<!DOCTYPE html>
<html lang="en" class="dark">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1, maximum-scale=1">
  <title>ENZO — Quantum Memecoin Trading Terminal</title>
  <style>
    :root {{
      --bg-base: #080c14;
      --bg-surface: #0e1422;
      --bg-card: #121a2d;
      --bg-card-hover: #162038;
      --border-subtle: #1e293b;
      --border-glow: rgba(59, 130, 246, 0.2);
      --text-primary: #f8fafc;
      --text-secondary: #94a3b8;
      --text-muted: #64748b;
      --accent-emerald: #10b981;
      --accent-emerald-glow: rgba(16, 185, 129, 0.15);
      --accent-rose: #f43f5e;
      --accent-rose-glow: rgba(244, 63, 94, 0.15);
      --accent-cyan: #06b6d4;
      --accent-amber: #f59e0b;
      --accent-violet: #8b5cf6;
      --font-mono: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, "Liberation Mono", "Courier New", monospace;
    }}

    * {{ box-sizing: border-box; margin: 0; padding: 0; }}
    body {{
      background: radial-gradient(circle at 50% 0%, #111a30 0%, var(--bg-base) 70%);
      color: var(--text-primary);
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Oxygen, Ubuntu, Cantarell, "Open Sans", "Helvetica Neue", sans-serif;
      min-height: 100vh;
      padding: 20px;
      line-height: 1.5;
      -webkit-font-smoothing: antialiased;
    }}

    /* Scrollbar */
    ::-webkit-scrollbar {{ width: 6px; height: 6px; }}
    ::-webkit-scrollbar-track {{ background: var(--bg-base); }}
    ::-webkit-scrollbar-thumb {{ background: var(--border-subtle); border-radius: 3px; }}
    ::-webkit-scrollbar-thumb:hover {{ background: var(--text-muted); }}

    /* Layout Containers */
    .dashboard-container {{ max-width: 1440px; margin: 0 auto; display: flex; flex-direction: column; gap: 20px; }}

    /* Top Navigation Header */
    .top-header {{
      background: rgba(14, 20, 34, 0.85);
      backdrop-filter: blur(12px);
      border: 1px solid var(--border-subtle);
      border-radius: 16px;
      padding: 16px 24px;
      display: flex;
      justify-content: space-between;
      align-items: center;
      flex-wrap: wrap;
      gap: 16px;
      box-shadow: 0 8px 32px rgba(0, 0, 0, 0.3);
    }}
    .brand-section {{ display: flex; align-items: center; gap: 14px; }}
    .brand-logo {{
      width: 42px; height: 42px;
      background: linear-gradient(135deg, #06b6d4, #8b5cf6);
      border-radius: 10px;
      display: flex;
      align-items: center;
      justify-content: center;
      font-weight: 900;
      font-size: 22px;
      color: #fff;
      box-shadow: 0 0 20px rgba(6, 182, 212, 0.4);
    }}
    .brand-text h1 {{ font-size: 20px; font-weight: 800; letter-spacing: -0.5px; display: flex; align-items: center; gap: 8px; }}
    .brand-text p {{ font-size: 12px; color: var(--text-secondary); font-family: var(--font-mono); }}

    .header-actions {{ display: flex; align-items: center; gap: 12px; flex-wrap: wrap; }}
    .status-badge {{
      display: inline-flex; align-items: center; gap: 6px;
      padding: 6px 12px; border-radius: 20px;
      font-size: 12px; font-weight: 600; text-transform: uppercase; letter-spacing: 0.5px;
    }}
    .status-badge.live {{ background: var(--accent-emerald-glow); color: var(--accent-emerald); border: 1px solid rgba(16, 185, 129, 0.3); }}
    .status-badge.paused {{ background: rgba(245, 158, 11, 0.15); color: var(--accent-amber); border: 1px solid rgba(245, 158, 11, 0.3); }}
    .status-badge.halted {{ background: var(--accent-rose-glow); color: var(--accent-rose); border: 1px solid rgba(244, 63, 94, 0.3); }}

    .pulse-dot {{
      width: 8px; height: 8px; border-radius: 50%;
      background: currentColor; display: inline-block;
      animation: pulse 1.8s infinite;
    }}
    @keyframes pulse {{ 0% {{ opacity: 1; transform: scale(1); }} 50% {{ opacity: 0.4; transform: scale(1.3); }} 100% {{ opacity: 1; transform: scale(1); }} }}

    .btn {{
      background: var(--bg-card);
      border: 1px solid var(--border-subtle);
      color: var(--text-primary);
      padding: 8px 16px; border-radius: 10px;
      font-size: 13px; font-weight: 600;
      cursor: pointer; display: inline-flex; align-items: center; gap: 8px;
      transition: all 0.2s ease;
      text-decoration: none;
    }}
    .btn:hover {{ background: var(--bg-card-hover); border-color: var(--text-muted); transform: translateY(-1px); }}
    .btn-primary {{ background: linear-gradient(135deg, #0ea5e9, #3b82f6); border: none; color: #fff; box-shadow: 0 4px 14px rgba(14, 165, 233, 0.3); }}
    .btn-primary:hover {{ background: linear-gradient(135deg, #0284c7, #2563eb); }}
    .btn-danger {{ background: linear-gradient(135deg, #e11d48, #be123c); border: none; color: #fff; }}

    /* KPI Stat Cards Grid */
    .kpi-grid {{
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(210px, 1fr));
      gap: 16px;
    }}
    .kpi-card {{
      background: var(--bg-surface);
      border: 1px solid var(--border-subtle);
      border-radius: 16px;
      padding: 20px;
      position: relative;
      overflow: hidden;
      transition: all 0.3s ease;
      box-shadow: 0 4px 20px rgba(0, 0, 0, 0.2);
    }}
    .kpi-card:hover {{ border-color: rgba(59, 130, 246, 0.4); transform: translateY(-2px); }}
    .kpi-card::before {{
      content: ""; position: absolute; top: 0; left: 0; right: 0; height: 3px;
      background: linear-gradient(90deg, transparent, var(--border-glow), transparent);
    }}
    .kpi-card.emerald::before {{ background: linear-gradient(90deg, transparent, var(--accent-emerald), transparent); }}
    .kpi-card.cyan::before {{ background: linear-gradient(90deg, transparent, var(--accent-cyan), transparent); }}
    .kpi-card.violet::before {{ background: linear-gradient(90deg, transparent, var(--accent-violet), transparent); }}
    .kpi-card.rose::before {{ background: linear-gradient(90deg, transparent, var(--accent-rose), transparent); }}

    .kpi-title {{ font-size: 12px; font-weight: 600; text-transform: uppercase; letter-spacing: 0.6px; color: var(--text-secondary); margin-bottom: 8px; display: flex; justify-content: space-between; }}
    .kpi-value {{ font-size: 26px; font-weight: 800; letter-spacing: -0.5px; font-family: var(--font-mono); margin-bottom: 4px; }}
    .kpi-subtext {{ font-size: 12px; color: var(--text-muted); display: flex; align-items: center; gap: 6px; }}

    /* Tab Navigation */
    .tab-bar {{
      display: flex; gap: 8px;
      border-bottom: 1px solid var(--border-subtle);
      padding-bottom: 8px;
      overflow-x: auto;
    }}
    .tab-btn {{
      background: transparent; border: none;
      color: var(--text-secondary);
      padding: 10px 18px; border-radius: 10px;
      font-size: 14px; font-weight: 600;
      cursor: pointer; display: flex; align-items: center; gap: 8px;
      transition: all 0.2s ease;
      white-space: nowrap;
    }}
    .tab-btn:hover {{ color: var(--text-primary); background: rgba(255, 255, 255, 0.05); }}
    .tab-btn.active {{
      color: #fff; background: var(--bg-card);
      border: 1px solid var(--border-subtle);
      box-shadow: 0 4px 12px rgba(0, 0, 0, 0.2);
    }}
    .tab-badge {{
      background: rgba(255, 255, 255, 0.1); padding: 2px 7px;
      border-radius: 12px; font-size: 11px; font-family: var(--font-mono);
    }}
    .tab-btn.active .tab-badge {{ background: var(--accent-cyan); color: #000; font-weight: 700; }}

    /* Tab Content Areas */
    .tab-pane {{ display: none; }}
    .tab-pane.active {{ display: flex; flex-direction: column; gap: 20px; }}

    /* Section Cards */
    .panel {{
      background: var(--bg-surface);
      border: 1px solid var(--border-subtle);
      border-radius: 16px;
      padding: 24px;
      box-shadow: 0 4px 24px rgba(0, 0, 0, 0.25);
    }}
    .panel-header {{
      display: flex; justify-content: space-between; align-items: center;
      margin-bottom: 20px; flex-wrap: wrap; gap: 12px;
    }}
    .panel-title {{
      font-size: 16px; font-weight: 700; letter-spacing: -0.3px;
      display: flex; align-items: center; gap: 10px;
    }}
    .panel-title .icon {{ color: var(--accent-cyan); font-size: 18px; }}

    /* Activity Terminal Console */
    .activity-feed-container {{
      background: #090e18;
      border: 1px solid var(--border-subtle);
      border-radius: 12px;
      padding: 16px;
      max-height: 540px;
      overflow-y: auto;
      display: flex;
      flex-direction: column;
      gap: 10px;
      font-family: var(--font-mono);
    }}
    .activity-item {{
      background: var(--bg-surface);
      border-left: 3px solid var(--accent-cyan);
      border-radius: 8px;
      padding: 12px 16px;
      display: flex;
      flex-direction: column;
      gap: 6px;
      transition: all 0.2s ease;
    }}
    .activity-item:hover {{
      background: var(--bg-card);
      transform: translateX(3px);
    }}
    .activity-item.SUCCESS {{ border-left-color: var(--accent-emerald); }}
    .activity-item.WARNING {{ border-left-color: var(--accent-amber); }}
    .activity-item.ERROR {{ border-left-color: var(--accent-rose); }}

    .act-header {{
      display: flex;
      justify-content: space-between;
      align-items: center;
      font-size: 12px;
    }}
    .act-tag {{
      display: inline-flex;
      align-items: center;
      gap: 5px;
      padding: 2px 8px;
      border-radius: 6px;
      font-size: 10px;
      font-weight: 700;
      text-transform: uppercase;
    }}
    .act-tag.DISCOVERY {{ background: rgba(6, 182, 212, 0.15); color: var(--accent-cyan); }}
    .act-tag.ANALYSIS {{ background: rgba(139, 92, 246, 0.15); color: var(--accent-violet); }}
    .act-tag.TRADE {{ background: rgba(16, 185, 129, 0.15); color: var(--accent-emerald); }}
    .act-tag.EXIT {{ background: rgba(245, 158, 11, 0.15); color: var(--accent-amber); }}
    .act-tag.SYSTEM {{ background: rgba(148, 163, 184, 0.15); color: var(--text-secondary); }}

    .act-time {{ color: var(--text-muted); font-size: 11px; }}
    .act-msg {{ font-size: 13px; color: var(--text-primary); }}
    .act-details {{
      font-size: 11px;
      color: var(--text-secondary);
      background: rgba(0, 0, 0, 0.3);
      padding: 6px 10px;
      border-radius: 6px;
      display: flex;
      gap: 12px;
      flex-wrap: wrap;
    }}

    /* Interactive Chart Card */
    .chart-box {{
      width: 100%; height: 320px;
      position: relative; background: #0b0f19;
      border-radius: 12px; border: 1px solid var(--border-subtle);
      overflow: hidden;
    }}
    canvas#equityCanvas {{ width: 100%; height: 100%; display: block; }}

    /* Data Tables */
    .table-responsive {{ width: 100%; overflow-x: auto; }}
    table.enzo-table {{
      width: 100%; border-collapse: collapse; text-align: left; font-size: 13px;
    }}
    table.enzo-table th {{
      background: var(--bg-card); color: var(--text-secondary);
      font-size: 11px; text-transform: uppercase; letter-spacing: 0.6px;
      padding: 12px 16px; font-weight: 700; border-bottom: 1px solid var(--border-subtle);
    }}
    table.enzo-table td {{
      padding: 14px 16px; border-bottom: 1px solid var(--border-subtle);
      vertical-align: middle;
    }}
    table.enzo-table tbody tr {{ transition: background 0.15s ease; }}
    table.enzo-table tbody tr:hover {{ background: rgba(255, 255, 255, 0.03); }}

    /* Token Cell & Badges */
    .token-cell {{ display: flex; align-items: center; gap: 12px; }}
    .token-icon {{
      width: 34px; height: 34px; border-radius: 50%;
      background: linear-gradient(135deg, #1e293b, #334155);
      display: flex; align-items: center; justify-content: center;
      font-weight: 800; font-size: 12px; color: var(--accent-cyan);
      border: 1px solid var(--border-subtle);
    }}
    .token-info {{ display: flex; flex-direction: column; }}
    .token-sym {{ font-weight: 700; font-size: 14px; color: var(--text-primary); }}
    .token-ca {{
      font-size: 11px; color: var(--text-muted); font-family: var(--font-mono);
      display: flex; align-items: center; gap: 4px; cursor: pointer;
    }}
    .token-ca:hover {{ color: var(--accent-cyan); }}

    /* Target Stages Roadmap */
    .stages-roadmap {{
      display: flex; gap: 6px; align-items: center; font-size: 11px; font-family: var(--font-mono);
    }}
    .stage-pill {{
      padding: 3px 7px; border-radius: 6px;
      background: var(--bg-card); border: 1px solid var(--border-subtle);
      color: var(--text-muted); font-weight: 600;
    }}
    .stage-pill.hit {{ background: var(--accent-emerald-glow); border-color: var(--accent-emerald); color: var(--accent-emerald); }}
    .stage-pill.rug {{ background: rgba(244,63,94,.16); border-color: rgba(244,63,94,.5); color: #fb7185; }}

    /* 6-Axis AI Matrix */
    .axes-grid {{
      display: grid; grid-template-columns: repeat(auto-fit, minmax(280px, 1fr)); gap: 16px;
    }}
    .axis-card {{
      background: var(--bg-card); border: 1px solid var(--border-subtle);
      border-radius: 12px; padding: 16px; display: flex; flex-direction: column; gap: 10px;
    }}
    .axis-header {{ display: flex; justify-content: space-between; align-items: center; }}
    .axis-name {{ font-size: 13px; font-weight: 700; color: var(--text-primary); }}
    .axis-score {{ font-size: 18px; font-weight: 800; font-family: var(--font-mono); }}

    /* Color Helpers */
    .color-pos {{ color: var(--accent-emerald); }}
    .color-neg {{ color: var(--accent-rose); }}
    .color-warn {{ color: var(--accent-amber); }}
    .color-info {{ color: var(--accent-cyan); }}

    /* Search & Filters */
    .search-input {{
      background: var(--bg-card); border: 1px solid var(--border-subtle);
      color: var(--text-primary); padding: 8px 14px; border-radius: 8px;
      font-size: 13px; width: 220px; outline: none; transition: border 0.2s;
    }}
    .search-input:focus {{ border-color: var(--accent-cyan); }}

    /* Live Toast */
    #toast {{
      position: fixed; bottom: 24px; right: 24px;
      background: var(--bg-card); border: 1px solid var(--accent-cyan);
      color: #fff; padding: 12px 20px; border-radius: 10px;
      box-shadow: 0 8px 24px rgba(0,0,0,0.5);
      display: none; z-index: 1000; font-size: 13px; font-weight: 600;
    }}

    /* Fault banner — makes a broken/stale dashboard impossible to miss */
    .fault-banner {{
      display: none; align-items: center; gap: 12px; flex-wrap: wrap;
      background: rgba(244, 63, 94, 0.10);
      border: 1px solid rgba(244, 63, 94, 0.45);
      border-left: 4px solid var(--accent-rose);
      border-radius: 12px; padding: 12px 16px;
      font-size: 13px; color: #fecdd3;
    }}
    .fault-banner.shown {{ display: flex; }}
    .fault-banner.warn {{
      background: rgba(245, 158, 11, 0.10);
      border-color: rgba(245, 158, 11, 0.45);
      border-left-color: var(--accent-amber); color: #fde68a;
    }}
    .fault-icon {{ font-size: 18px; }}
    .fault-hint {{ color: var(--text-muted); font-family: var(--font-mono); font-size: 11px; }}
  </style>
</head>
<body>

<div class="dashboard-container">

  <!-- Persistent fault banner: shows when the page could not be regenerated or
       when the browser has lost contact with the API. Hidden by default. -->
  <div id="faultBanner" class="fault-banner" style="display:none;">
    <span class="fault-icon">⚠</span>
    <span id="faultBannerText"></span>
    <span class="fault-hint" id="faultBannerHint"></span>
  </div>
  {banner_html}

  <!-- Top Header Navigation -->
  <header class="top-header">
    <div class="brand-section">
      <div class="brand-logo">⚡</div>
      <div class="brand-text">
        <h1>ENZO QUANT TERMINAL <span style="font-size: 12px; font-weight: 600; color: var(--accent-cyan); background: rgba(6,182,212,0.12); padding: 2px 8px; border-radius: 12px;">v2.5 PRO</span></h1>
        <p>AUTONOMOUS SOLANA MEMECOIN • {mode_label}{executor_label} • WALLET: {_esc(wallet_name)}</p>
        <p style="font-size: 11px; color: var(--text-muted); font-family: var(--font-mono);">
          RENDERED {generated_at} • CAPITAL SOURCE: {capital_source.upper()} • POLL <span id="hdrPollAge">—</span>
        </p>
      </div>
    </div>

    <div class="header-actions">
      <div id="engineStatusBadge" class="status-badge {'paused' if paused else ('halted' if halted else 'live')}">
        <span class="pulse-dot"></span>
        <span id="engineStatusText">{'⏸ PAUSED' if paused else ('⚠ HALTED' if halted else '● LIVE SCANNING')}</span>
      </div>

      <button id="toggleBotBtn" class="btn" onclick="toggleBotPause()">
        <span>{'▶ Resume Bot' if paused else '⏸ Pause Bot'}</span>
      </button>

      <button class="btn btn-primary" onclick="triggerManualScan()">
        <span>🔍 Scan Market Now</span>
      </button>

      <button class="btn" onclick="refreshData(true)" title="Force Refresh">
        <span>🔄 Refresh</span>
      </button>
    </div>
  </header>

  <!-- KPI Overview Ribbon (6 Hero Cards) -->
  <div class="kpi-grid">
    <!-- Card 1: Equity -->
    <div class="kpi-card cyan">
      <div class="kpi-title">
        <span>{'Wallet Balance (live)' if wallet_usd else 'Total Equity (ledger)'}</span>
        <span>💵 USD</span>
      </div>
      <div class="kpi-value" id="kpiEquity">${eq_display:,.2f}</div>
      <div class="kpi-subtext">
        <span>Init: ${init_cap:,.0f}</span> • <span id="kpiPeakEquity">Peak: ${float(state.get('peak_equity', eq)):,.0f}</span>{f" • <span>Ledger: ${eq:,.2f}</span>" if wallet_usd else ""}
      </div>
    </div>

    <!-- Card 2: Realized PnL -->
    <div class="kpi-card {'emerald' if rp >= 0 else 'rose'}">
      <div class="kpi-title">
        <span>Realized Net PnL</span>
        <span>📈 ROI</span>
      </div>
      <div class="kpi-value {'color-pos' if rp >= 0 else 'color-neg'}" id="kpiRealizedPnL">${rp:+,.2f}</div>
      <div class="kpi-subtext">
        <span id="kpiRoiPct">ROI: {(rp / init_cap * 100):+,.1f}%</span> • <span id="kpiProfitFactor">Factor: 1.0</span>
      </div>
    </div>

    <!-- Card 3: Win Rate & Trades -->
    <div class="kpi-card violet">
      <div class="kpi-title">
        <span>Win Rate</span>
        <span>🎯 Precision</span>
      </div>
      <div class="kpi-value" id="kpiWinRate">{win_rate:.1f}%</div>
      <div class="kpi-subtext">
        <span id="kpiTradesBreakdown">{len(wins)}W / {len(losses)}L</span> • <span>{total_trades} Total Trades</span>
      </div>
    </div>

    <!-- Card 4: Open Positions -->
    <div class="kpi-card">
      <div class="kpi-title">
        <span>Active Positions</span>
        <span>⚡ Open</span>
      </div>
      <div class="kpi-value" id="kpiOpenCount">{len(state.get('open_positions', {}))}</div>
      <div class="kpi-subtext">
        <span>Max Slots: {max_open}</span> • <span id="kpiExposure">Risk Cap: {max_exposure:.0f}%</span>
      </div>
    </div>

    <!-- Card 5: Drawdown & Safety -->
    <div class="kpi-card">
      <div class="kpi-title">
        <span>Risk & Drawdown</span>
        <span>🛡️ Safety</span>
      </div>
      <div class="kpi-value" id="kpiDrawdown">0.0%</div>
      <div class="kpi-subtext">
        <span>Max DD Limit: 25.0%</span> • <span>Daily Loss: 0.0%</span>
      </div>
    </div>

    <!-- Card 6: AI Confidence Bias -->
    <div class="kpi-card">
      <div class="kpi-title">
        <span>AI Learned Bias</span>
        <span>🧠 Matrix</span>
      </div>
      <div class="kpi-value" id="kpiAiBias">{learning.get('confidence_bias', 0.0):+,.1f}</div>
      <div class="kpi-subtext">
        <span>Self-Calibrating Multi-Axis Matrix</span>
      </div>
    </div>
  </div>

  <!-- Tab Navigation -->
  <div class="tab-bar">
    <button class="tab-btn active" onclick="switchTab('tabActivity')">
      <span>🛰️ Live Bot Operations & Activity</span>
      <span class="tab-badge" id="tabActivityBadge" style="background:var(--accent-cyan); color:#000;">LIVE</span>
    </button>
    <button class="tab-btn" onclick="switchTab('tabOverview')">
      <span>📊 Overview & Growth Chart</span>
    </button>
    <button class="tab-btn" onclick="switchTab('tabPositions')">
      <span>🎯 Active Positions</span>
      <span class="tab-badge" id="tabPositionsBadge">{len(state.get('open_positions', {}))}</span>
    </button>
    <button class="tab-btn" onclick="switchTab('tabTrades')">
      <span>📜 Trade History</span>
      <span class="tab-badge" id="tabTradesBadge">{total_trades}</span>
    </button>
    <button class="tab-btn" onclick="switchTab('tabIntelligence')">
      <span>🧠 AI Matrix & Learning</span>
    </button>
    <button class="tab-btn" onclick="switchTab('tabDiagnostics')">
      <span>🛡️ Subsystems & Health</span>
    </button>
  </div>

  <!-- TAB 0: Live Bot Operations & Activity Stream -->
  <div id="tabActivity" class="tab-pane active">
    <!-- Subsystem Status Ribbon -->
    <div style="display: grid; grid-template-columns: repeat(auto-fit, minmax(220px, 1fr)); gap: 12px;">
      <div class="axis-card" style="padding: 12px;">
        <div class="axis-header">
          <span style="font-size: 12px; font-weight: 700; color: var(--text-secondary);">⚡ PumpDev WebSocket</span>
          <span id="subPumpDevStatus" class="stage-pill hit">STREAMING</span>
        </div>
        <span id="subPumpDevTokens" style="font-size: 11px; color: var(--text-muted); font-family: var(--font-mono);">Buffered Tokens: 0</span>
      </div>

      <div class="axis-card" style="padding: 12px;">
        <div class="axis-header">
          <span style="font-size: 12px; font-weight: 700; color: var(--text-secondary);">🔍 GMGN Scanner</span>
          <span id="subGmgnStatus" class="stage-pill hit">OPERATIONAL</span>
        </div>
        <span style="font-size: 11px; color: var(--text-muted); font-family: var(--font-mono);">Trenches + Trending + SmartMoney</span>
      </div>

      <div class="axis-card" style="padding: 12px;">
        <div class="axis-header">
          <span style="font-size: 12px; font-weight: 700; color: var(--text-secondary);">⏱️ Exit Monitor</span>
          <span class="stage-pill hit">ARMED</span>
        </div>
        <span style="font-size: 11px; color: var(--text-muted); font-family: var(--font-mono);">Stale Guard: 10s | Poll: 2.0s</span>
      </div>

      <div class="axis-card" style="padding: 12px;">
        <div class="axis-header">
          <span style="font-size: 12px; font-weight: 700; color: var(--text-secondary);">💾 SQLite WAL DB</span>
          <span class="stage-pill hit">CONCURRENT</span>
        </div>
        <span style="font-size: 11px; color: var(--text-muted); font-family: var(--font-mono);">ACID Atomic Transactions</span>
      </div>
    </div>

    <!-- Live Activity Console -->
    <div class="panel">
      <div class="panel-header">
        <div class="panel-title">
          <span class="icon">🛰️</span>
          <span>Real-Time Bot Decision & Activity Stream</span>
        </div>
        <div style="display: flex; gap: 8px; align-items: center; flex-wrap: wrap;">
          <button class="btn" onclick="filterActivity('ALL')"><span>All Events</span></button>
          <button class="btn" onclick="filterActivity('TRADE')"><span>Trades & Exits</span></button>
          <button class="btn" onclick="filterActivity('ANALYSIS')"><span>6-Axis Scans</span></button>
          <button class="btn" onclick="filterActivity('DISCOVERY')"><span>Discovery</span></button>
          <button class="btn" id="universeFilterBtn" onclick="filterActivity('UNIVERSE')"
                  title="قرارات رُفضت ببوابة من بوابات الدخول: Pump V1، الطور، القيمة السوقية، البيع، الرسوم، القنّاصون، تركّز المحافظ"><span>🎯 Gate Vetoes</span></button>
        </div>
      </div>

      <div class="activity-feed-container" id="activityFeedContainer">
        <div style="text-align: center; color: var(--text-muted); padding: 40px;">
          Listening to real-time bot activities...
        </div>
      </div>
    </div>
  </div>

  <!-- TAB 1: Overview & Chart -->
  <div id="tabOverview" class="tab-pane">
    <!-- Chart Panel -->
    <div class="panel">
      <div class="panel-header">
        <div class="panel-title">
          <span class="icon">📈</span>
          <span>Portfolio Equity Growth Curve (ACID SQLite Realized + uPnL)</span>
        </div>
        <div style="display: flex; gap: 8px; font-size: 12px; color: var(--text-secondary);">
          <span>● Live Auto-Sync Active (1.5s)</span>
        </div>
      </div>
      <div class="chart-box">
        <canvas id="equityCanvas"></canvas>
      </div>
    </div>

    <!-- 6-Axis AI Snapshot -->
    <div class="panel">
      <div class="panel-header">
        <div class="panel-title">
          <span class="icon">⚡</span>
          <span>AI 6-Axis Decision Matrix Framework (Multi-Vector Architecture)</span>
        </div>
      </div>
      <div class="axes-grid">
        <div class="axis-card">
          <div class="axis-header">
            <span class="axis-name">🛡️ Security & Authorities</span>
            <span class="axis-score color-info">30% Weight</span>
          </div>
          <p style="font-size: 12px; color: var(--text-secondary);">Renounced Mint & Freeze, Honeypot detection, Top 10 holder concentration limits.</p>
        </div>
        <div class="axis-card">
          <div class="axis-header">
            <span class="axis-name">👛 Wallet Quality & Smart Money</span>
            <span class="axis-score color-info">20% Weight</span>
          </div>
          <p style="font-size: 12px; color: var(--text-secondary);">Smart degen / whale tracking, bundler detection, insider rat identification, dumping velocity.</p>
        </div>
        <div class="axis-card">
          <div class="axis-header">
            <span class="axis-name">👨‍💻 Dev Reputation & Factory Smell</span>
            <span class="axis-score color-info">20% Weight</span>
          </div>
          <p style="font-size: 12px; color: var(--text-secondary);">Creator holding rate, serial-launcher factory detection, historical ATH performance tracking.</p>
        </div>
        <div class="axis-card">
          <div class="axis-header">
            <span class="axis-name">🔥 Momentum & Volume Pressure</span>
            <span class="axis-score color-info">15% Weight</span>
          </div>
          <p style="font-size: 12px; color: var(--text-secondary);">1h / 5m acceleration, buy/sell pressure ratio, hot level indexing, smart trader inflows.</p>
        </div>
        <div class="axis-card">
          <div class="axis-header">
            <span class="axis-name">📊 Market Structure & Growth</span>
            <span class="axis-score color-info">10% Weight</span>
          </div>
          <p style="font-size: 12px; color: var(--text-secondary);">Rolling multi-sample mcap growth, liquidity acceleration, 5m kline green-candle trend.</p>
        </div>
        <div class="axis-card">
          <div class="axis-header">
            <span class="axis-name">💧 Liquidity Health</span>
            <span class="axis-score color-info">5% Weight</span>
          </div>
          <p style="font-size: 12px; color: var(--text-secondary);">Tradeable liquidity pool depth check, slippage tolerance, pool backing verification.</p>
        </div>
      </div>
    </div>
  </div>

  <!-- TAB 2: Active Positions -->
  <div id="tabPositions" class="tab-pane">
    <div class="panel">
      <div class="panel-header">
        <div class="panel-title">
          <span class="icon">🎯</span>
          <span>Live Active Positions & Execution Trailing Stops</span>
        </div>
      </div>

      <div class="table-responsive">
        <table class="enzo-table" id="positionsTable">
          <thead>
            <tr>
              <th>Token & Contract</th>
              <th>Position Size</th>
              <th>Entry Market Cap</th>
              <th>Live Market Cap</th>
              <th>Unrealized PnL</th>
              <th>Target Stages (30% / 70% / 150%)</th>
              <th>Trailing Stop / SL</th>
              <th>Actions</th>
            </tr>
          </thead>
          <tbody id="positionsTableBody">
            <tr><td colspan="8" style="text-align: center; color: var(--text-muted); padding: 30px;">No open positions at the moment.</td></tr>
          </tbody>
        </table>
      </div>
    </div>
  </div>

  <!-- TAB 3: Trade History -->
  <div id="tabTrades" class="tab-pane">
    <div class="panel">
      <div class="panel-header">
        <div class="panel-title">
          <span class="icon">📜</span>
          <span>Historical Trade Ledger & Exit Executions</span>
        </div>
        <div style="display: flex; gap: 10px;">
          <input type="text" id="tradeSearchInput" class="search-input" placeholder="Search by symbol or CA..." oninput="filterTradesTable()">
        </div>
      </div>

      <div class="table-responsive">
        <table class="enzo-table" id="tradesTable">
          <thead>
            <tr>
              <th>Token</th>
              <th>Exit Reason</th>
              <th>Entry MC</th>
              <th>Exit MC</th>
              <th>Realized PnL ($)</th>
              <th>Realized PnL (%)</th>
              <th>Opened At</th>
              <th>Closed At</th>
            </tr>
          </thead>
          <tbody id="tradesTableBody">
            <tr><td colspan="8" style="text-align: center; color: var(--text-muted); padding: 30px;">No closed trades recorded yet.</td></tr>
          </tbody>
        </table>
      </div>
    </div>
  </div>

  <!-- TAB 4: AI Intelligence & Learning -->
  <div id="tabIntelligence" class="tab-pane">
    <div class="panel">
      <div class="panel-header">
        <div class="panel-title">
          <span class="icon">🧠</span>
          <span>Self-Calibrating Machine Learning Insights</span>
        </div>
      </div>

      <div style="display: grid; grid-template-columns: repeat(auto-fit, minmax(320px, 1fr)); gap: 20px;">
        <!-- Feature Leaderboard -->
        <div style="background: var(--bg-card); border: 1px solid var(--border-subtle); border-radius: 12px; padding: 18px;">
          <h3 style="font-size: 14px; font-weight: 700; margin-bottom: 12px; color: var(--accent-cyan);">Top Predictive On-Chain Features</h3>
          <div class="table-responsive">
            <table class="enzo-table">
              <thead>
                <tr>
                  <th>Feature Signal</th>
                  <th>Win Rate</th>
                  <th>Samples</th>
                </tr>
              </thead>
              <tbody id="learningFeaturesBody">
                <tr><td colspan="3" style="text-align: center; color: var(--text-muted);">Awaiting trade outcomes...</td></tr>
              </tbody>
            </table>
          </div>
        </div>

        <!-- Axis Historical Performance -->
        <div style="background: var(--bg-card); border: 1px solid var(--border-subtle); border-radius: 12px; padding: 18px;">
          <h3 style="font-size: 14px; font-weight: 700; margin-bottom: 12px; color: var(--accent-violet);">Axis Historical Win Rates</h3>
          <div class="table-responsive">
            <table class="enzo-table">
              <thead>
                <tr>
                  <th>Analysis Axis</th>
                  <th>Historical Win%</th>
                  <th>Avg Score</th>
                </tr>
              </thead>
              <tbody id="learningAxesBody">
                <tr><td colspan="3" style="text-align: center; color: var(--text-muted);">Awaiting trade outcomes...</td></tr>
              </tbody>
            </table>
          </div>
        </div>
      </div>
    </div>
  </div>

  <!-- TAB 5: Subsystems & Diagnostics -->
  <div id="tabDiagnostics" class="tab-pane">
    <div class="panel">
      <div class="panel-header">
        <div class="panel-title">
          <span class="icon">🛡️</span>
          <span>System Diagnostics, Rate Limits & Risk Limits</span>
        </div>
      </div>

      <div style="display: grid; grid-template-columns: repeat(auto-fit, minmax(300px, 1fr)); gap: 16px;">
        <div class="axis-card" id="gmgnSourceCard">
          <div class="axis-header">
            <span class="axis-name">⚡ GMGN Data Source</span>
            <span class="axis-score {gmgn_hdr_cls}" id="gmgnBanStatus">{gmgn_hdr_txt}</span>
          </div>
          <div style="display: flex; flex-direction: column; gap: 6px; margin-top: 4px; font-size: 12px; color: var(--text-secondary);">
            <div><b>API key:</b> <span id="gmgnKeyStatus" class="{ 'color-pos' if gmgn_key_ok else 'color-neg' }">{ 'present' if gmgn_key_ok else 'MISSING — every call is refused' }</span></div>
            <div><b>CLI dialect:</b> <span id="gmgnDialect">{_esc(gmgn_dialect_label)}</span></div>
            <div><b>Discovery:</b> <span id="gmgnCats">{_esc(gmgn_cats_label)}</span></div>
            <div><b>Last sweep:</b> <span id="gmgnLastCount">{ 'no sweep yet' if gmgn_last_count is None else str(gmgn_last_count) + ' candidate(s)' }</span></div>
            <div><b>Last error:</b> <span id="gmgnLastError" class="{ 'color-warn' if gmgn_last_err else '' }">{ _esc((gmgn_last_ep + ': ' + gmgn_last_err)[:150]) if gmgn_last_err else 'none' }</span></div>
            <p style="margin-top:2px;">Token bucket with automatic backoff and unban coordination. The pace above is the configured one (requests_per_sec / request_gap_ms / burst_capacity) — not a number derived from the gap.</p>
          </div>
        </div>

        <div class="axis-card">
          <div class="axis-header">
            <span class="axis-name">🛡️ Circuit Breakers</span>
            <span class="axis-score color-pos">ARMED</span>
          </div>
          <p style="font-size: 12px; color: var(--text-secondary);">Auto-halts on {max_daily_loss:.0f}% daily loss, {max_drawdown:.0f}% max drawdown, or {cons_limit} consecutive losses. Risk/trade: {risk_per_trade:.1f}%.</p>
        </div>

        <div class="axis-card">
          <div class="axis-header">
            <span class="axis-name">💾 Storage & Database</span>
            <span class="axis-score color-pos">SQLite WAL Mode</span>
          </div>
          <p style="font-size: 12px; color: var(--text-secondary);">ACID compliant concurrent multi-process transaction ledger active at data/enzo.db.</p>
        </div>

        <div class="axis-card" id="universeGateCard">
          <div class="axis-header">
            <span class="axis-name">🎯 Entry Universe · Layer 0</span>
            <span class="axis-score {uni_hdr_cls}" id="universeGateStatus">{uni_hdr_txt}</span>
          </div>
          <div style="display: flex; flex-direction: column; gap: 10px; margin-top: 4px;">
            <div>
              <span class="stage-pill {uni_p1_pill}" id="uniPumpPill">PUMP V1 ONLY · { 'ON' if uni_pump_only else 'OFF' }</span>
              <p style="font-size: 12px; color: var(--text-secondary); margin-top: 4px;">
                Buys standard pump.fun coins only (<code>launchpad=pump</code> / <code>launchpad_platform=Pump.fun</code>). Unknown launchpad is {'REJECTED — unknown is not treated as pump.fun' if uni_reject_unknown else 'allowed'} (<code>LAUNCHPAD_UNKNOWN</code> / <code>NOT_PUMP_V1</code>).
              </p>
            </div>
            <div>
              <span class="stage-pill {uni_pre_pill}">PRE-MIGRATION · { 'ON' if uni_pre_on else 'OFF' }</span>
              <p style="font-size: 12px; color: var(--text-secondary); margin-top: 4px;">
                On the bonding curve (<code>launchpad_status=1</code>): market cap &ge; {_esc(_usd(uni_pre_mc))} and sell transactions &ge; {_esc(_num(uni_pre_sells))}. A missing counter is reported as <code>SELLS_UNKNOWN</code>, never read as zero and never as "enough".
              </p>
            </div>
            <div>
              <span class="stage-pill {uni_mig_pill}">MIGRATED · { 'ON' if uni_mig_on else 'OFF' }</span>
              <p style="font-size: 12px; color: var(--text-secondary); margin-top: 4px;">
                After graduation (<code>launchpad_status=2</code>): market cap &ge; {_esc(_usd(uni_mig_mc))} and global fees paid &ge; {_esc(_num(uni_mig_fees))} {_esc(uni_fees_unit)}. Fees come from the dev's launch book (<code>portfolio created-tokens</code>), which does not label its unit — the unit is declared in config. {'"Could not measure" is a REJECTION (require_known_fees).' if uni_require_fees else '"Could not measure" is allowed.'} Unknown phase &rarr; <code>{_esc(uni_unknown_phase)}</code>.
              </p>
            </div>
            <div>
              <span class="stage-pill {uni_sn_pill}">EARLY-SNIPER RUG · { 'ON' if uni_sniper_on else 'OFF' }</span>
              <p style="font-size: 12px; color: var(--text-secondary); margin-top: 4px;">
                The first {uni_first_n} wallets in after the dev's create transaction: &ge; {uni_min_snipers} sniper-tagged AND &gt; {_esc(_usd(uni_max_total))} combined, <b>or any single wallet &gt; {_esc(_usd(uni_max_single))}</b> &rarr; <code>SNIPER_FLOOD_EARLY</code>, never bought. gmgn-cli has no trade tape, so this reads <code>start_holding_at</code> + <code>buy_volume_cur</code> per wallet — both approximation errors push toward rejecting. Unmeasurable &rarr; <code>{_esc(uni_on_unknown)}</code>.
              </p>
            </div>
            <div>
              <span class="stage-pill {uni_hc_pill}">HOLDER CAP · { 'ON' if uni_holder_on else 'OFF' }</span>
              <p style="font-size: 12px; color: var(--text-secondary); margin-top: 4px;">
                Top-1 <b>wallet</b> must hold &le; {_esc(_num(uni_holder_cap))}% of supply (<code>HOLDER_CONCENTRATION</code>). The bonding curve, AMM vault and burn addresses (<code>addr_type</code> 1/2) are excluded, so a healthy migrated token whose pool holds 40% is not vetoed. If it cannot be measured the cap is reported as NOT enforced rather than silently passed.
              </p>
            </div>
          </div>
        </div>

        <div class="axis-card" id="rugProtectionCard">
          <div class="axis-header">
            <span class="axis-name">🚩 Rug Protection Layers</span>
            <span class="axis-score {rug_hdr_cls}" id="rugProtectionStatus">{rug_hdr_txt}</span>
          </div>
          <div style="display: flex; flex-direction: column; gap: 10px; margin-top: 4px;">
            <div>
              <span class="stage-pill {rug_l1_pill}">L1 · FINGERPRINT VETO · {rug_l1_txt}</span>
              <p style="font-size: 12px; color: var(--text-secondary); margin-top: 4px;">
                Rejects at entry on absolute fingerprints: bundlers &ge; {rug_veto_bundlers}, snipers &ge; {rug_veto_snipers}, rats &ge; {rug_veto_rats} inside top-20, top-10 selling &ge; {rug_veto_sells}, avg wallet age &lt; {rug_veto_age:.1f}d, factory &ge; {rug_veto_factory} mints with &lt; {rug_veto_open_ratio:.0f}% still alive. Soft flags fire at {rug_flag_ratio:.0f}% of each veto and mark the position for L3.
              </p>
            </div>
            <div>
              <span class="stage-pill {rug_l3_pill}">L3 · EARLY STOP · {rug_l3_txt}</span>
              <p style="font-size: 12px; color: var(--text-secondary); margin-top: 4px;">
                -{rug_es_pct:.0f}% stop during the first {rug_es_win:.0f} minutes for FLAGGED entries only. Clean entries keep the normal stop/trailing untouched.
              </p>
            </div>
            <div>
              <span class="stage-pill {rug_l4_pill}">L4 · LIVE TRIPWIRE · {rug_l4_txt}</span>
              <p style="font-size: 12px; color: var(--text-secondary); margin-top: 4px;">
                {rug_tw_votes} of 3 votes closes the position at any price, checked every {rug_tw_poll:.0f}s: liquidity pulled &ge; {rug_tw_liq:.0f}%, holders down &ge; {rug_tw_hold:.0f}%, top-10 sells jump &ge; {rug_tw_sells} or flip to dumping.
              </p>
            </div>
          </div>
        </div>
      </div>
    </div>
  </div>

</div>

<!-- Interactive Toast Feedback -->
<div id="toast"></div>

<!-- JavaScript Client Engine -->
<script>
  var stateCache = null;
  var allTrades = [];
  var allActivities = [];
  var currentActivityFilter = 'ALL';

  function showToast(msg) {{
    var t = document.getElementById('toast');
    t.textContent = msg;
    t.style.display = 'block';
    setTimeout(function() {{ t.style.display = 'none'; }}, 3000);
  }}

  function copyCA(ca) {{
    navigator.clipboard.writeText(ca).then(function() {{
      showToast('Contract Address copied: ' + ca.substring(0, 8) + '...');
    }});
  }}

  function switchTab(tabId) {{
    document.querySelectorAll('.tab-pane').forEach(function(el) {{ el.classList.remove('active'); }});
    document.querySelectorAll('.tab-btn').forEach(function(el) {{ el.classList.remove('active'); }});
    document.getElementById(tabId).classList.add('active');
    
    var btns = document.querySelectorAll('.tab-btn');
    if (tabId === 'tabActivity') btns[0].classList.add('active');
    if (tabId === 'tabOverview') btns[1].classList.add('active');
    if (tabId === 'tabPositions') btns[2].classList.add('active');
    if (tabId === 'tabTrades') btns[3].classList.add('active');
    if (tabId === 'tabIntelligence') btns[4].classList.add('active');
    if (tabId === 'tabDiagnostics') btns[5].classList.add('active');

    if (tabId === 'tabOverview' && stateCache) {{
      renderEquityChart(stateCache.chart_points || []);
    }}
  }}

  function filterActivity(cat) {{
    currentActivityFilter = cat;
    renderActivities();
  }}

  function toggleBotPause() {{
    fetch('/api/control/toggle', {{ method: 'POST' }})
      .then(function(r) {{ return r.json(); }})
      .then(function(j) {{
        showToast(j.message || 'Updated');
        refreshData(true);
      }}).catch(function(e) {{ showToast('Error toggling bot status'); }});
  }}

  function triggerManualScan() {{
    fetch('/api/scan', {{ method: 'POST' }})
      .then(function(r) {{ return r.json(); }})
      .then(function(j) {{
        showToast('Market scan initiated in background.');
      }}).catch(function(e) {{ showToast('Scan error'); }});
  }}

  // ── Liveness tracking ────────────────────────────────────────────────
  // Previously every fetch failure was swallowed by `.catch(function(e) {{}})`,
  // so a dead/stalled backend looked identical to a healthy one: the page just
  // sat there showing the numbers from the last successful poll. Now failures
  // are counted and surfaced in the fault banner.
  var lastGoodPoll = Date.now();
  var pollFailures = 0;
  var POLL_STALE_MS = 35000;   // 3.5 missed 10s polls

  function setFault(kind, text, hint) {{
    var b = document.getElementById('faultBanner');
    var t = document.getElementById('faultBannerText');
    var h = document.getElementById('faultBannerHint');
    if (!b) return;
    if (!kind) {{ b.className = 'fault-banner'; b.style.display = 'none'; return; }}
    b.className = 'fault-banner shown' + (kind === 'warn' ? ' warn' : '');
    b.style.display = 'flex';
    t.textContent = text;
    h.textContent = hint || '';
  }}

  function notePollOk() {{
    pollFailures = 0;
    lastGoodPoll = Date.now();
    // only clear a client-side fault; a server-rendered banner keeps its own id
    if (!document.getElementById('serverFault')) setFault(null);
  }}

  function notePollFail(what, err) {{
    pollFailures++;
    setFault('error',
      'Lost contact with the ENZO API (' + what + ') — ' + pollFailures +
      ' consecutive failed poll(s). The numbers on this page are frozen at the last successful update.',
      'Check that the bot process is alive: python3 enzo.py status  ·  error: ' + (err || 'n/a'));
  }}

  function tickPollAge() {{
    var el = document.getElementById('hdrPollAge');
    if (!el) return;
    var age = Math.round((Date.now() - lastGoodPoll) / 1000);
    el.textContent = age + 's ago';
    el.style.color = age > POLL_STALE_MS / 1000 ? 'var(--accent-rose)' : 'var(--text-muted)';
    if (age > POLL_STALE_MS / 1000 && pollFailures === 0) {{
      setFault('warn',
        'Data is ' + age + 's old — the API has not answered a poll recently.',
        'The engine may be busy in a long scan cycle, or the server thread may have died.');
    }}
  }}

  function refreshData(force) {{
    // 1. Fetch State
    fetch('/api/state', {{ cache: 'no-store' }})
      .then(function(r) {{
        if (!r.ok) throw new Error('HTTP ' + r.status);
        return r.json();
      }})
      .then(function(res) {{
        if (!res || res.status !== 'success') throw new Error((res && res.message) || 'bad payload');
        stateCache = res;
        updateUI(res);
        notePollOk();
      }}).catch(function(e) {{ notePollFail('/api/state', e.message || e); }});

    // 2. Fetch Activity Stream
    fetch('/api/activity', {{ cache: 'no-store' }})
      .then(function(r) {{
        if (!r.ok) throw new Error('HTTP ' + r.status);
        return r.json();
      }})
      .then(function(res) {{
        if (!res || res.status !== 'success') throw new Error((res && res.message) || 'bad payload');
        allActivities = res.activities || [];
        renderActivities();
        updateSubsystems(res.subsystems);
      }}).catch(function(e) {{ /* state poll owns the banner */ }});
  }}

  function updateSubsystems(sub) {{
    if (!sub) return;
    if (sub.pumpdev_ws) {{
      var pStatus = document.getElementById('subPumpDevStatus');
      if (pStatus) {{
        pStatus.textContent = sub.pumpdev_ws.status;
        pStatus.className = sub.pumpdev_ws.status === 'STREAMING' ? 'stage-pill hit' : 'stage-pill';
      }}
      var pTok = document.getElementById('subPumpDevTokens');
      if (pTok) {{
        pTok.textContent = 'Buffered Tokens: ' + sub.pumpdev_ws.buffered_tokens + ' | Active Trades: ' + sub.pumpdev_ws.live_trades_monitored;
      }}
    }}
  }}

  // Layer-0 veto codes (Pump V1 / phase / market cap / sells / fees / early
  // snipers / holder cap). The "Gate Vetoes" filter answers the owner's most
  // common question - "which coins did my NEW rules turn down, and why?" -
  // without reading the audit log by hand.
  var UNIVERSE_CODES = ['NOT_PUMP_V1', 'LAUNCHPAD_UNKNOWN', 'PHASE_UNKNOWN',
    'MCAP_UNKNOWN', 'MCAP_BELOW_PRE_MIN', 'MCAP_BELOW_MIGRATED_MIN', 'MCAP_BELOW_UNKNOWN_MIN',
    'SELLS_UNKNOWN', 'SELLS_BELOW_MIN', 'FEES_NOT_CHECKED', 'FEES_UNKNOWN', 'FEES_BELOW_MIN',
    'SNIPER_FLOOD_NOT_CHECKED', 'SNIPER_FLOOD_EARLY', 'SNIPER_DATA_UNAVAILABLE',
    'HOLDER_CONCENTRATION'];

  function isUniverseVeto(a) {{
    if (!a) return false;
    var d = a.data || {{}};
    var hay = String(a.message || '') + ' ' + String(d.reason || '') + ' ' +
              (d.rejected_signals || []).join(' ');
    for (var i = 0; i < UNIVERSE_CODES.length; i++) {{
      if (hay.indexOf(UNIVERSE_CODES[i]) >= 0) return true;
    }}
    return false;
  }}

  function renderActivities() {{
    var container = document.getElementById('activityFeedContainer');
    if (!container) return;
    var filtered = allActivities.filter(function(a) {{
      if (currentActivityFilter === 'ALL') return true;
      if (currentActivityFilter === 'TRADE') return a.category === 'TRADE' || a.category === 'EXIT';
      if (currentActivityFilter === 'UNIVERSE') return isUniverseVeto(a);
      return a.category === currentActivityFilter;
    }});

    if (filtered.length === 0) {{
      container.innerHTML = '<div style="text-align: center; color: var(--text-muted); padding: 40px;">No events matching ' + currentActivityFilter + ' filter.</div>';
      return;
    }}

    var html = '';
    filtered.forEach(function(act) {{
      var lvl = act.level || 'INFO';
      var cat = act.category || 'SYSTEM';
      var timeStr = act.time_str || (act.ts ? act.ts.substring(11, 19) : '');
      var msg = act.message || '';
      var data = act.data || {{}};

      var detailsStr = '';
      if (data.axes) {{
        // Read the EXACT keys analyze.py writes: wallet_behavior / dev_behavior.
        // The old code read .wallet and .dev, which are undefined, so `|| 0`
        // printed 0 for EVERY token and made each one look like it had a dead
        // wallet and a dead dev. Show 'n/a' (never a fake 0) when an axis has no
        // data: unavailable axes are excluded from the weighted confidence, so
        // a zero here would misrepresent the score.
        var ax = function(k) {{ var v = data.axes[k]; return (typeof v === 'number') ? v : 'n/a'; }};
        detailsStr = '<div class="act-details">' +
          '<span>🛡️ Security: ' + ax('security') + '</span>' +
          '<span>👛 Wallet: ' + ax('wallet_behavior') + '</span>' +
          '<span>👨‍💻 Dev: ' + ax('dev_behavior') + '</span>' +
          '<span>🔥 Momentum: ' + ax('momentum') + '</span>' +
          '<span>📊 Structure: ' + ax('market_structure') + '</span>' +
          '<span>💧 Liq: ' + ax('liquidity') + '</span>' +
          (data.market_cap_usd ? '<span>💰 MC: $' + Number(data.market_cap_usd).toLocaleString('en-US') + '</span>' : '') +
          '</div>';
      }}

      // WHY it was turned down. The audit row always carried the reason and the
      // veto codes, but the feed dropped them and showed only
      // "SYMBOL -> IGNORE (conf=0)" - useless for a bot trading real money.
      var vetoStr = '';
      var rej = data.rejected_signals || [];
      if (rej.length) {{
        vetoStr += '<div class="act-details">' + rej.map(function(r) {{
          return '<span style="color:#fb7185;">✘ ' + String(r).slice(0, 150) + '</span>';
        }}).join('') + '</div>';
      }} else if (data.reason) {{
        vetoStr += '<div class="act-details"><span>' + String(data.reason).slice(0, 170) + '</span></div>';
      }}
      var uni = data.universe || null;
      if (uni) {{
        var bits = [];
        bits.push(uni.pump_v1 ? '🎯 Pump V1 ✔' : '🎯 not Pump V1 ✘');
        if (uni.platform) bits.push('platform: ' + uni.platform);
        if (uni.phase) bits.push('phase: ' + uni.phase);
        if (uni.fees && uni.fees.value !== null && uni.fees.value !== undefined) {{
          bits.push('fees: ' + uni.fees.value + ' ' + String(uni.fees.unit || '').toUpperCase());
        }}
        if (uni.snipers) {{
          bits.push('early snipers: ' + (uni.snipers.sniper_count || 0) +
                    ' / $' + Number(uni.snipers.sniper_total_usd || 0).toLocaleString('en-US'));
        }}
        if (data.top_holder_pct !== null && data.top_holder_pct !== undefined) {{
          bits.push('top wallet: ' + data.top_holder_pct + '%');
        }}
        vetoStr += '<div class="act-details">' + bits.map(function(b) {{
          return '<span>' + b + '</span>';
        }}).join('') + '</div>';
      }}

      html += '<div class="activity-item ' + lvl + '">' +
        '<div class="act-header">' +
        '<span class="act-tag ' + cat + '">' + cat + '</span>' +
        '<span class="act-time">' + timeStr + '</span>' +
        '</div>' +
        '<div class="act-msg">' + msg + '</div>' +
        detailsStr +
        vetoStr +
        '</div>';
    }});

    container.innerHTML = html;
  }}

  function updateUI(data) {{
    var pf = data.portfolio;
    var sys = data.system;

    // Header Badge
    var badge = document.getElementById('engineStatusBadge');
    var badgeText = document.getElementById('engineStatusText');
    var toggleBtn = document.querySelector('#toggleBotBtn span');

    if (sys.is_paused) {{
      badge.className = 'status-badge paused';
      badgeText.textContent = '⏸ PAUSED';
      toggleBtn.textContent = '▶ Resume Bot';
    }} else if (sys.is_halted) {{
      badge.className = 'status-badge halted';
      badgeText.textContent = '⚠ HALTED: ' + (sys.halt_reason || '');
      toggleBtn.textContent = '⏸ Pause Bot';
    }} else {{
      badge.className = 'status-badge live';
      badgeText.textContent = '● LIVE SCANNING';
      toggleBtn.textContent = '⏸ Pause Bot';
    }}

    // KPIs
    var eqShow = (typeof pf.wallet_usd === 'number' && pf.wallet_usd > 0) ? pf.wallet_usd : pf.equity;
    document.getElementById('kpiEquity').textContent = '$' + eqShow.toLocaleString('en-US', {{ minimumFractionDigits: 2, maximumFractionDigits: 2 }});
    document.getElementById('kpiPeakEquity').textContent = 'Peak: $' + pf.peak_equity.toLocaleString('en-US', {{ maximumFractionDigits: 0 }});
    
    var rpEl = document.getElementById('kpiRealizedPnL');
    rpEl.textContent = (pf.realized_pnl >= 0 ? '+$' : '-$') + Math.abs(pf.realized_pnl).toLocaleString('en-US', {{ minimumFractionDigits: 2, maximumFractionDigits: 2 }});
    rpEl.className = 'kpi-value ' + (pf.realized_pnl >= 0 ? 'color-pos' : 'color-neg');

    var roi = pf.initial_capital > 0 ? (pf.realized_pnl / pf.initial_capital * 100) : 0;
    document.getElementById('kpiRoiPct').textContent = 'ROI: ' + (roi >= 0 ? '+' : '') + roi.toFixed(1) + '%';
    document.getElementById('kpiProfitFactor').textContent = 'Factor: ' + pf.profit_factor;

    document.getElementById('kpiWinRate').textContent = pf.win_rate.toFixed(1) + '%';
    document.getElementById('kpiTradesBreakdown').textContent = pf.wins + 'W / ' + pf.losses + 'L';
    
    document.getElementById('kpiOpenCount').textContent = pf.open_positions_count;
    document.getElementById('tabPositionsBadge').textContent = pf.open_positions_count;
    document.getElementById('tabTradesBadge').textContent = pf.closed_trades_count;
    document.getElementById('kpiDrawdown').textContent = pf.current_drawdown_pct.toFixed(1) + '%';
    
    if (data.learning) {{
      document.getElementById('kpiAiBias').textContent = (data.learning.confidence_bias >= 0 ? '+' : '') + Number(data.learning.confidence_bias).toFixed(1);
    }}

    // A position whose size was raised to execution.min_trade_usd because the
    // wallet is small. Without this badge a $1.00 position looks like a config
    // mistake; with it the operator can see the risk model was overridden and by
    // how much.
    function floorBadge(p) {{
      if (!p || !p.min_floor_applied) return '';
      var er = Number(p.effective_risk_pct || 0);
      // NOTE: the escape below must stay doubled (backslash backslash n) in
      // the Python source, because this whole template is a NON-RAW f-string.
      // A single backslash-n would be expanded by Python into a REAL newline
      // inside a single-quoted JS string -> SyntaxError -> the entire script
      // block dies: every button and the activity stream stop working while
      // the static HTML still looks alive. Same applies to any backslash
      // escape written anywhere in this template, comments included.
      var tip = 'الحجم رُفع إلى الحد الأدنى للصفقة لأن رأس المال صغير.\\n' +
                'المخاطرة الفعلية: ' + er.toFixed(1) + '% من رأس المال.';
      return ' <span title="' + tip.replace(/"/g, '&quot;') + '"' +
             ' style="display:inline-block;margin-top:3px;padding:1px 6px;border-radius:6px;' +
             'font-size:10px;font-weight:700;letter-spacing:.3px;cursor:help;' +
             'background:rgba(255,193,7,.16);color:#ffc107;border:1px solid rgba(255,193,7,.35);">' +
             'الأرضية · مخاطرة ' + er.toFixed(1) + '%</span>';
    }}

    // Rug badge: this entry carried SOFT flags (half the veto thresholds) from
    // layer 1, so layer 3 arms a tighter early stop on it. The owner must see
    // which open positions are in that state and why. No backslash escapes are
    // used here on purpose - this template is a non-raw f-string.
    function rugBadge(p) {{
      if (!p) return '';
      var flags = p.rug_flags || [];
      if (!flags.length) return '';
      var tip = 'دخول مشبوب: ' + flags.join(' · ') +
                ' — الوقف المبكر -{rug_es_pct:.0f}% مفعّل لأول {rug_es_win:.0f} دقيقة، وكاشف الرغّ يراقبه كل {rug_tw_poll:.0f} ثانية.';
      return ' <span title=\"' + tip.replace(/\"/g, '&quot;') + '\"' +
             ' style=\"display:inline-block;margin-top:3px;padding:1px 6px;border-radius:6px;' +
             'font-size:10px;font-weight:700;letter-spacing:.3px;cursor:help;' +
             'background:rgba(244,63,94,.16);color:#fb7185;border:1px solid rgba(244,63,94,.35);\">' +
             '🚩 ' + flags.length + ' علم</span>';
    }}

    // Open Positions Table
    var posTable = document.getElementById('positionsTableBody');
    var openMints = Object.keys(data.open_positions || {{}});
    if (openMints.length === 0) {{
      posTable.innerHTML = '<tr><td colspan="8" style="text-align: center; color: var(--text-muted); padding: 30px;">No open positions at the moment.</td></tr>';
    }} else {{
      var rows = '';
      openMints.forEach(function(m) {{
        var p = data.open_positions[m];
        var sym = p.symbol || 'UNKNOWN';
        var size = Number(p.size_usd || 0);
        var entryMc = Number(p.entry_market_cap || 0);
        var liveMc = Number(p.current_market_cap || entryMc);
        var upnl = Number(p.unrealized_pnl || 0);
        var upnlPct = Number(p.unrealized_pnl_pct || 0);
        
        var stages = (p.stages_hit || [false, false, false]);
        var s1 = stages[0] ? 'stage-pill hit' : 'stage-pill';
        var s2 = stages[1] ? 'stage-pill hit' : 'stage-pill';
        var s3 = stages[2] ? 'stage-pill hit' : 'stage-pill';

        var trailStr = p.trailing_active ? '<span class="color-pos" style="font-weight:700;">ACTIVE ($' + Number(p.trailing_stop_mc||0).toLocaleString('en-US') + ')</span>' : '<span style="color:var(--text-muted)">SL: $' + Number(p.stop_loss_mc||0).toLocaleString('en-US') + '</span>';

        rows += '<tr>' +
          '<td><div class="token-cell"><div class="token-icon">' + sym.substring(0, 3) + '</div>' +
          '<div class="token-info"><span class="token-sym">' + sym + '</span>' +
          '<span class="token-ca" onclick="copyCA(\\'' + m + '\\')">' + m.substring(0, 6) + '...' + m.substring(m.length - 4) + ' 📋</span></div></div></td>' +
          '<td><strong>$' + size.toLocaleString('en-US') + '</strong>' + floorBadge(p) + rugBadge(p) + '</td>' +
          '<td>$' + entryMc.toLocaleString('en-US') + '</td>' +
          '<td><strong style="color:var(--accent-cyan);">' + (liveMc ? '$' + liveMc.toLocaleString('en-US') : '—') + (p.price_is_live === false ? ' <span title="no live price right now (feed stale) - last known value shown" style="color:#f59e0b;">⚠</span>' : '') + '</strong></td>' +
          '<td class="' + (upnl >= 0 ? 'color-pos' : 'color-neg') + '"><strong>' + (upnl >= 0 ? '+$' : '-$') + Math.abs(upnl).toFixed(2) + ' (' + (upnlPct >= 0 ? '+' : '') + upnlPct.toFixed(1) + '%)</strong></td>' +
          '<td><div class="stages-roadmap"><span class="' + s1 + '">T1: 30%</span><span class="' + s2 + '">T2: 70%</span><span class="' + s3 + '">T3: 150%</span></div></td>' +
          '<td>' + trailStr + '</td>' +
          '<td><a href="https://gmgn.ai/sol/token/' + m + '" target="_blank" class="btn" style="padding: 4px 8px; font-size: 11px;">GMGN ↗</a></td>' +
          '</tr>';
      }});
      posTable.innerHTML = rows;
    }}

    // Closed Trades
    allTrades = data.closed_positions || [];
    renderTradesTable(allTrades);

    // Learning Table
    if (data.learning) {{
      var fBody = document.getElementById('learningFeaturesBody');
      var fw = data.learning.feature_win_rates || [];
      if (fw.length === 0) {{
        fBody.innerHTML = '<tr><td colspan="3" style="text-align: center; color: var(--text-muted);">Awaiting trade data...</td></tr>';
      }} else {{
        var fRows = '';
        fw.slice(0, 6).forEach(function(f) {{
          fRows += '<tr><td><strong>' + f.feature + '</strong></td><td class="color-pos">' + f.win_rate + '%</td><td>' + f.n + '</td></tr>';
        }});
        fBody.innerHTML = fRows;
      }}

      var aBody = document.getElementById('learningAxesBody');
      var aw = data.learning.axis_win_rates || [];
      if (aw.length === 0) {{
        aBody.innerHTML = '<tr><td colspan="3" style="text-align: center; color: var(--text-muted);">Awaiting trade data...</td></tr>';
      }} else {{
        var aRows = '';
        aw.slice(0, 6).forEach(function(a) {{
          aRows += '<tr><td><strong>' + a.axis + '</strong></td><td class="color-info">' + a.win_rate + '%</td><td>' + a.avg_score + '</td></tr>';
        }});
        aBody.innerHTML = aRows;
      }}
    }}

    // Render Equity Chart
    if (data.chart_points) {{
      renderEquityChart(data.chart_points);
    }}
  }}

  function renderTradesTable(trades) {{
    var tBody = document.getElementById('tradesTableBody');
    if (!tBody) return;
    if (!trades || trades.length === 0) {{
      tBody.innerHTML = '<tr><td colspan="8" style="text-align: center; color: var(--text-muted); padding: 30px;">No closed trades recorded yet.</td></tr>';
      return;
    }}
    var rows = '';
    trades.slice().reverse().forEach(function(c) {{
      var sym = c.symbol || 'UNKNOWN';
      var pnl = Number(c.pnl || 0);
      var pnlPct = Number(c.pnl_pct || 0);
      var reason = c.reason || 'CLOSED';
      var reasonClass = (reason.indexOf('RUG_TRIPWIRE') >= 0 || reason.indexOf('EARLY_STOP') >= 0)
        ? 'stage-pill rug'
        : (reason.indexOf('TP') >= 0 ? 'stage-pill hit' : (reason.indexOf('STOP') >= 0 ? 'stage-pill' : 'stage-pill'));
      var closedAt = (c.closed_at || '').substring(0, 16).replace('T', ' ');
      var openedAt = (c.opened_at || '').substring(0, 16).replace('T', ' ');

      rows += '<tr>' +
        '<td><strong>' + sym + '</strong></td>' +
        '<td><span class="' + reasonClass + '">' + reason + '</span></td>' +
        '<td>$' + Number(c.entry_market_cap || 0).toLocaleString('en-US') + '</td>' +
        '<td>$' + Number(c.exit_market_cap || 0).toLocaleString('en-US') + '</td>' +
        '<td class="' + (pnl >= 0 ? 'color-pos' : 'color-neg') + '"><strong>' + (pnl >= 0 ? '+$' : '-$') + Math.abs(pnl).toFixed(2) + '</strong></td>' +
        '<td class="' + (pnlPct >= 0 ? 'color-pos' : 'color-neg') + '"><strong>' + (pnlPct >= 0 ? '+' : '') + pnlPct.toFixed(1) + '%</strong></td>' +
        '<td>' + openedAt + '</td>' +
        '<td>' + closedAt + '</td>' +
        '</tr>';
    }});
    tBody.innerHTML = rows;
  }}

  function filterTradesTable() {{
    var q = (document.getElementById('tradeSearchInput').value || '').toLowerCase();
    var filtered = allTrades.filter(function(t) {{
      return (t.symbol && t.symbol.toLowerCase().indexOf(q) >= 0) ||
             (t.mint && t.mint.toLowerCase().indexOf(q) >= 0) ||
             (t.reason && t.reason.toLowerCase().indexOf(q) >= 0);
    }});
    renderTradesTable(filtered);
  }}

  // Canvas Equity Chart Engine
  function renderEquityChart(pts) {{
    var canvas = document.getElementById('equityCanvas');
    if (!canvas) return;
    var ctx = canvas.getContext('2d');
    if (!ctx) return;
    var rect = canvas.getBoundingClientRect();
    canvas.width = rect.width * window.devicePixelRatio || 800;
    canvas.height = rect.height * window.devicePixelRatio || 320;
    ctx.scale(window.devicePixelRatio || 1, window.devicePixelRatio || 1);

    var w = rect.width;
    var h = rect.height;
    var padL = 60, padR = 20, padT = 30, padB = 40;

    ctx.clearRect(0, 0, w, h);

    if (!pts || pts.length < 2) {{
      ctx.fillStyle = '#64748b';
      ctx.font = '13px sans-serif';
      ctx.textAlign = 'center';
      ctx.fillText('Awaiting initial trade executions to graph equity growth...', w / 2, h / 2);
      return;
    }}

    var vals = pts.map(function(p) {{ return p.value; }});
    var vmin = Math.min.apply(null, vals);
    var vmax = Math.max.apply(null, vals);
    var span = (vmax - vmin) || 100;
    vmin = Math.floor(vmin - span * 0.05);
    vmax = Math.ceil(vmax + span * 0.05);
    span = vmax - vmin;

    function getX(i) {{ return padL + i * (w - padL - padR) / (pts.length - 1); }}
    function getY(v) {{ return padT + (1 - (v - vmin) / span) * (h - padT - padB); }}

    // Gridlines
    ctx.strokeStyle = '#1e293b';
    ctx.lineWidth = 1;
    ctx.fillStyle = '#64748b';
    ctx.font = '11px monospace';
    ctx.textAlign = 'right';

    for (var k = 0; k <= 4; k++) {{
      var gVal = vmin + (span * k / 4);
      var gy = getY(gVal);
      ctx.beginPath();
      ctx.moveTo(padL, gy);
      ctx.lineTo(w - padR, gy);
      ctx.stroke();
      ctx.fillText('$' + Math.round(gVal).toLocaleString('en-US'), padL - 8, gy + 4);
    }}

    // Area Gradient
    var grad = ctx.createLinearGradient(0, padT, 0, h - padB);
    grad.addColorStop(0, 'rgba(6, 182, 212, 0.25)');
    grad.addColorStop(1, 'rgba(6, 182, 212, 0.0)');

    ctx.beginPath();
    ctx.moveTo(getX(0), h - padB);
    for (var i = 0; i < pts.length; i++) {{
      ctx.lineTo(getX(i), getY(pts[i].value));
    }}
    ctx.lineTo(getX(pts.length - 1), h - padB);
    ctx.closePath();
    ctx.fillStyle = grad;
    ctx.fill();

    // Line Path
    ctx.beginPath();
    ctx.strokeStyle = '#06b6d4';
    ctx.lineWidth = 2.5;
    for (var i = 0; i < pts.length; i++) {{
      if (i === 0) ctx.moveTo(getX(i), getY(pts[i].value));
      else ctx.lineTo(getX(i), getY(pts[i].value));
    }}
    ctx.stroke();

    // Data Dots
    pts.forEach(function(p, i) {{
      var px = getX(i);
      var py = getY(p.value);
      ctx.beginPath();
      ctx.arc(px, py, 4, 0, Math.PI * 2);
      ctx.fillStyle = '#06b6d4';
      ctx.fill();
      ctx.lineWidth = 2;
      ctx.strokeStyle = '#0e1422';
      ctx.stroke();
    }});
  }}

  // Auto-Refresh Poll Engine (every 10 seconds — light on the DB & rate limiter)
  refreshData(false);
  setInterval(function() {{ refreshData(false); }}, {dash_refresh_ms});
  setInterval(tickPollAge, 1000);
  // Re-poll immediately when the tab regains focus or connectivity returns.
  document.addEventListener('visibilitychange', function() {{
    if (!document.hidden) refreshData(true);
  }});
  window.addEventListener('online', function() {{ refreshData(true); }});
</script>
</body>
</html>
"""

    # Atomic write (temp + os.replace) so concurrent generate() calls from
    # multiple threads never leave a truncated/partial HTML file.
    tmp = DASHBOARD_HTML_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(html_content)
    os.replace(tmp, DASHBOARD_HTML_PATH)

    # A render succeeded: clear any recorded failure, keep a last-good copy for
    # the HTTP server to fall back on, and remember it in-process so the server
    # can answer instantly without re-reading from disk.
    _LAST_RENDER["html"] = html_content
    _LAST_RENDER["ts"] = time.time()
    _LAST_RENDER["error"] = None
    try:
        if os.path.exists(LAST_ERROR_PATH):
            os.remove(LAST_ERROR_PATH)
        with open(LAST_GOOD_PATH, "w", encoding="utf-8") as f:
            f.write(html_content)
    except Exception:
        pass

    return DASHBOARD_HTML_PATH


if __name__ == "__main__":
    path = generate()
    print(f"[✓] Ultra-Modern ENZO Dashboard generated at: {path}")
