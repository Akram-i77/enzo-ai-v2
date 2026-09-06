#!/usr/bin/env python3
"""
ENZO - Professional Telegram Notification System
Delivers visually rich, structured HTML trade signals, multi-stage take-profit alerts,
stop-loss reports, risk warnings, and performance summaries with inline interactive links.
"""
import json
import os
import sys
import html
import time
from datetime import datetime, timezone
import urllib.request
import urllib.error
from typing import Dict, Any, Optional, List

from enzo.core.config import load_secrets, load_config
import enzo.core.log as log

_LOGGER = log.get_logger("enzo.notify")
TG_SEND_MSG = "https://api.telegram.org/bot{}/sendMessage"

# Per-(event,mint) cooldown to suppress duplicate notifications (15 min)
_NOTIFY_COOLDOWN_SEC = 15 * 60
_NOTIFY_LAST: dict = {}


def _cooldown_ok(event: str, mint: str) -> bool:
    key = f"{event}:{mint}"
    now = time.time()
    last = _NOTIFY_LAST.get(key)
    if last is not None and (now - last) < _NOTIFY_COOLDOWN_SEC:
        return False
    _NOTIFY_LAST[key] = now
    # Prevent unbounded growth of the cooldown map. It used to compute a cutoff
    # and then `clear()` the whole map, so every still-cooling event lost its
    # cooldown at once and the next alert for it was sent again - a duplicate
    # Telegram message for something the owner had just been told about. Prune
    # the expired entries instead, and only fall back to a full clear if the map
    # is still huge (i.e. the live entries themselves are the problem).
    if len(_NOTIFY_LAST) > 2000:
        cutoff = now - _NOTIFY_COOLDOWN_SEC
        for _k in [k for k, v in _NOTIFY_LAST.items() if v < cutoff]:
            _NOTIFY_LAST.pop(_k, None)
        if len(_NOTIFY_LAST) > 4000:
            _NOTIFY_LAST.clear()
        _NOTIFY_LAST[key] = now
    return True


def telegram_configured() -> bool:
    sec = load_secrets()
    return bool(sec.get("telegram_bot_token") and sec.get("telegram_chat_id"))


def _send_tg(html_text: str, reply_markup: dict = None) -> bool:
    """Send an HTML-formatted message to Telegram with optional inline buttons."""
    sec = load_secrets()
    token = sec.get("telegram_bot_token")
    chat_id = sec.get("telegram_chat_id")
    if not token or not chat_id:
        return False

    url = TG_SEND_MSG.format(token)
    payload = {
        "chat_id": chat_id,
        "text": html_text,
        "parse_mode": "HTML",
        "disable_web_page_preview": True
    }
    if reply_markup:
        payload["reply_markup"] = reply_markup

    try:
        data = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=12) as resp:
            return resp.status == 200
    except urllib.error.HTTPError as e:
        err = e.read().decode("utf-8", errors="ignore")
        _LOGGER.warning(f"Telegram HTTP {e.code} error: {err[:200]}")
        return False
    except Exception as e:
        _LOGGER.warning(f"Telegram notification error: {e}")
        return False


# ================================================================ Visual Formatters
def format_buy_signal(decision: dict, position: dict = None, paper: bool = True) -> tuple[str, dict]:
    """Format an elite-grade BUY entry signal with full multi-axis metrics & inline links."""
    sym = _esc_html(decision.get("token_symbol") or "UNKNOWN")
    mint = decision.get("mint_address") or decision.get("mint") or ""
    conf = float(decision.get("confidence_score") or decision.get("weighted_confidence") or 0.0)
    
    # Financial metrics
    entry_mc = float(decision.get("entry_market_cap") or decision.get("market_cap_usd") or 0.0)
    tp_mc = float(decision.get("take_profit_mc") or 0.0)
    sl_mc = float(decision.get("stop_loss_mc") or 0.0)
    roi_exp = decision.get("expected_roi") or "+150.0%"
    sl_exp = decision.get("expected_loss") or "-50.0%"
    rr = decision.get("risk_reward_ratio") or "3.0:1"
    
    size_usd = float(position.get("size_usd", 0.0)) if position else (entry_mc * 0.01 if entry_mc else 250.0)

    # 6-Axis AI Scores
    ax = decision.get("axis_scores") or {}
    def _s(k):
        v = ax.get(k)
        return int(v.get("score") if isinstance(v, dict) else (v or 50))
    
    sec_sc = _s("security")
    wal_sc = _s("wallet_behavior")
    dev_sc = _s("dev_behavior")
    mom_sc = _s("momentum")
    ms_sc = _s("market_structure")
    liq_sc = _s("liquidity")

    # Signals summary
    sup = decision.get("supporting_signals") or []
    signals_str = "\n".join([f"  • <i>{_esc_html(s)}</i>" for s in sup[:4]]) if sup else "  • <i>Passed all security & quality gates</i>"

    mode_badge = "🟢 <b>[PAPER SIMULATION]</b>" if paper else "⚡ <b>[LIVE ON-CHAIN]</b>"

    msg = f"""
🚀 <b>ENZO QUANT ENTRY SIGNAL</b> • {mode_badge}
━━━━━━━━━━━━━━━━━━━━━━━━━━
💎 <b>Token:</b> <code>${sym}</code>
📋 <b>Contract:</b> <code>{mint}</code>
⚡ <b>AI Confidence:</b> <b>{conf:.0f}/100</b>  •  <b>R:R:</b> <code>{rr}</code>

📊 <b>TRADE PARAMETERS</b>
  • <b>Position Size:</b> <code>${size_usd:,.2f}</code>
  • <b>Entry Market Cap:</b> <code>${entry_mc:,.0f}</code>
  • 🎯 <b>Target TP (150%):</b> <code>${tp_mc:,.0f}</code> ({roi_exp})
  • 🛑 <b>Hard Stop Loss:</b> <code>${sl_mc:,.0f}</code> ({sl_exp})

🧠 <b>6-AXIS DECISION MATRIX</b>
  🛡️ <b>Security:</b> <code>{sec_sc}/100</code>  |  👛 <b>Wallets:</b> <code>{wal_sc}/100</code>
  👨‍💻 <b>Dev Rep:</b> <code>{dev_sc}/100</code>  |  🔥 <b>Momentum:</b> <code>{mom_sc}/100</code>
  📊 <b>Structure:</b> <code>{ms_sc}/100</code>  |  💧 <b>Liquidity:</b> <code>{liq_sc}/100</code>

🔎 <b>KEY SUPPORTING SIGNALS</b>
{signals_str}
━━━━━━━━━━━━━━━━━━━━━━━━━━
⏰ <i>{datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")}</i>
"""
    # Inline action buttons
    buttons = {
        "inline_keyboard": [
            [
                {"text": "📊 GMGN Chart", "url": f"https://gmgn.ai/sol/token/{mint}"},
                {"text": "📈 DexScreener", "url": f"https://dexscreener.com/solana/{mint}"},
            ],
            [
                {"text": "💊 Pump.fun", "url": f"https://pump.fun/{mint}"},
                {"text": "🔍 Solscan Explorer", "url": f"https://solscan.io/token/{mint}"},
            ]
        ]
    }
    return msg.strip(), buttons


def format_exit_signal(record: dict, paper: bool = True) -> tuple[str, dict]:
    """Format an exit notification (Take-Profit, Trailing Stop, or Stop-Loss)."""
    sym = _esc_html(record.get("symbol") or "UNKNOWN")
    mint = record.get("mint") or ""
    pnl = float(record.get("pnl") or 0.0)
    pnl_pct = float(record.get("pnl_pct") or 0.0)
    reason = record.get("reason") or "CLOSED"
    entry_mc = float(record.get("entry_market_cap") or 0.0)
    exit_mc = float(record.get("exit_market_cap") or 0.0)

    is_win = pnl >= 0
    icon = "🎉" if is_win else "🛑"
    status_color = "🟢 PROFIT" if is_win else "🔴 LOSS"
    mode_badge = "<b>[PAPER]</b>" if paper else "<b>[LIVE]</b>"

    msg = f"""
{icon} <b>ENZO POSITION CLOSED</b> • {mode_badge}
━━━━━━━━━━━━━━━━━━━━━━━━━━
💎 <b>Token:</b> <code>${sym}</code>
📋 <b>Contract:</b> <code>{mint}</code>
🎯 <b>Exit Reason:</b> <b>{reason}</b>

💵 <b>FINANCIAL OUTCOME ({status_color})</b>
  • <b>Net PnL:</b> <b>{'+$' if pnl >= 0 else '-$'}{abs(pnl):,.2f}</b> (<b>{pnl_pct:+,.1f}%</b>)
  • <b>Entry Market Cap:</b> <code>${entry_mc:,.0f}</code>
  • <b>Exit Market Cap:</b> <code>${exit_mc:,.0f}</code>
  • <b>Closed At:</b> <code>{(record.get('closed_at') or '')[:19].replace('T', ' ')}</code>
━━━━━━━━━━━━━━━━━━━━━━━━━━
🧠 <i>Trade outcome recorded to self-calibrating machine learning engine.</i>
"""
    buttons = {
        "inline_keyboard": [
            [
                {"text": "📊 View on GMGN", "url": f"https://gmgn.ai/sol/token/{mint}"},
                {"text": "📈 DexScreener", "url": f"https://dexscreener.com/solana/{mint}"}
            ]
        ]
    }
    return msg.strip(), buttons


def format_partial_tp(record: dict, paper: bool = True) -> tuple[str, dict]:
    """Format a partial take-profit stage achievement."""
    sym = _esc_html(record.get("symbol") or "UNKNOWN")
    mint = record.get("mint") or ""
    pnl = float(record.get("pnl") or 0.0)
    pnl_pct = float(record.get("pnl_pct") or 0.0)
    reason = record.get("reason") or "TP_STAGE"
    exit_mc = float(record.get("exit_market_cap") or 0.0)
    fraction = float(record.get("fraction") or 0.3) * 100.0

    msg = f"""
🎯 <b>PARTIAL TAKE-PROFIT REALIZED!</b>
━━━━━━━━━━━━━━━━━━━━━━━━━━
💎 <b>Token:</b> <code>${sym}</code>
🎯 <b>Stage:</b> <b>{reason}</b> (Sold {fraction:.0f}% of position)
💰 <b>Realized Gain:</b> <b>+${pnl:,.2f} (+{pnl_pct:.1f}%)</b>
📈 <b>Current Market Cap:</b> <code>${exit_mc:,.0f}</code>
━━━━━━━━━━━━━━━━━━━━━━━━━━
🛡️ <i>Trailing Stop activated to protect remaining balance.</i>
"""
    buttons = {
        "inline_keyboard": [
            [{"text": "📊 GMGN Live Chart", "url": f"https://gmgn.ai/sol/token/{mint}"}]
        ]
    }
    return msg.strip(), buttons


def format_risk_alert(title: str, message: str) -> str:
    """Format a critical risk/circuit breaker alert."""
    return f"""
⚠️ <b>ENZO RISK & SAFETY ALERT</b>
━━━━━━━━━━━━━━━━━━━━━━━━━━
🚨 <b>Event:</b> <b>{title}</b>
📝 <b>Details:</b> {message}
⏰ <b>Time:</b> <code>{datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")}</code>
━━━━━━━━━━━━━━━━━━━━━━━━━━
🛡️ <i>Risk controls automatically enforced by ENZO engine.</i>
""".strip()


def format_daily_digest(state: dict, learning: dict) -> str:
    """Format a comprehensive performance digest."""
    init_cap = float(state.get("initial_capital", 10000.0))
    eq = float(state.get("equity", init_cap))
    rp = float(state.get("realized_pnl", 0.0))
    roi = (rp / init_cap * 100.0) if init_cap > 0 else 0.0
    open_n = len(state.get("open_positions", {}))
    closed = state.get("closed_positions", [])
    wins = [c for c in closed if float(c.get("pnl", 0)) > 0]
    total_trades = len(closed)
    win_rate = (len(wins) / total_trades * 100.0) if total_trades else 0.0

    return f"""
📊 <b>ENZO QUANT PERFORMANCE REPORT</b>
━━━━━━━━━━━━━━━━━━━━━━━━━━
💰 <b>Current Equity:</b> <code>${eq:,.2f}</code>
📈 <b>Realized Net PnL:</b> <b>{'+$' if rp >= 0 else '-$'}{abs(rp):,.2f}</b> (<b>{roi:+,.2f}%</b>)
🎯 <b>Win Rate:</b> <b>{win_rate:.1f}%</b> ({len(wins)}W / {total_trades - len(wins)}L)
⚡ <b>Active Positions:</b> <code>{open_n}</code>
📜 <b>Total Trades:</b> <code>{total_trades}</code>
🧠 <b>Learned AI Bias:</b> <code>{learning.get('confidence_bias', 0.0):+,.1f} pts</code>
━━━━━━━━━━━━━━━━━━━━━━━━━━
⏰ <i>Generated at {datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")}</i>
""".strip()


def _esc_html(text: str) -> str:
    return html.escape(str(text)) if text else ""


# ================================================================ Public API
def notify(decision: dict, position: dict = None, paper: bool = True, console: bool = True):
    """Main dispatcher for scan decisions."""
    cfg = load_config()
    ncfg = cfg.get("notifications", {}) or {}
    dec = decision.get("decision")
    conf = float(decision.get("confidence_score") or 0)
    min_conf = float(ncfg.get("min_confidence_for_notification", 75))

    msg, buttons = format_buy_signal(decision, position=position, paper=paper)

    if console:
        print(f"\n[ENZO][DECISION] {decision.get('token_symbol')} -> {dec} (Conf={conf})")

    if ncfg.get("send_decision_notifications", True):
        if dec == "BUY" or conf >= min_conf:
            _send_tg(msg, reply_markup=buttons)


def notify_exit(record: dict, paper: bool = True, console: bool = True):
    """Dispatcher for position closed exit signals."""
    if not _cooldown_ok("exit", record.get("mint") or record.get("symbol")):
        return
    msg, buttons = format_exit_signal(record, paper=paper)
    if console:
        print(f"[ENZO][EXIT] {record.get('symbol')} closed -> PnL: ${record.get('pnl', 0):+,.2f}")
    _send_tg(msg, reply_markup=buttons)


def notify_partial(record: dict, paper: bool = True, console: bool = True):
    """Dispatcher for partial take-profit milestones."""
    if not _cooldown_ok("partial", record.get("mint") or record.get("symbol")):
        return
    msg, buttons = format_partial_tp(record, paper=paper)
    if console:
        print(f"[ENZO][TP-PARTIAL] {record.get('symbol')} stage hit -> +${record.get('pnl', 0):,.2f}")
    _send_tg(msg, reply_markup=buttons)


def notify_risk(title: str, details: str):
    """Dispatcher for circuit breakers and emergency halts."""
    if not _cooldown_ok("risk", title):
        return
    msg = format_risk_alert(title, details)
    _send_tg(msg)


def notify_buy_failed(decision: dict, reason: str = "", paper: bool = False,
                      console: bool = True, reason_code: str = "") -> bool:
    """Alert the operator that a BUY signal could NOT be executed.

    This function did not exist before 2026-09-03. engine.py called it behind a
    `hasattr(notify, "notify_buy_failed")` guard, so the guard evaluated False
    and the call became a silent no-op: every failed live buy — executor not
    ready, no route, insufficient fees, below minimum size — was rolled back
    without a single message reaching Telegram. That is why live execution
    looked like it "never did anything" rather than "kept failing".
    """
    mint = decision.get("mint_address") or decision.get("mint") or ""
    sym = decision.get("token_symbol") or (mint[:8] if mint else "UNKNOWN")
    if not _cooldown_ok("buy_failed", mint or sym):
        return False

    conf = float(decision.get("confidence_score") or decision.get("weighted_confidence") or 0.0)
    mcap = float(decision.get("entry_market_cap") or decision.get("market_cap_usd") or 0.0)
    code = reason_code or ""

    # Translate the executor's reason codes into something actionable.
    hints = {
        "NO_ROUTE": "MoonPay/swaps.xyz has no route for this pair. Fresh pump.fun tokens still on the bonding curve cannot be swapped until they graduate to a DEX.",
        "INSUFFICIENT_SOL_FOR_FEES": "Top up SOL in the trading wallet — a small SOL balance is required for transaction fees.",
        "INSUFFICIENT_BALANCE": "The wallet does not hold enough of the base token for this position size.",
        "BELOW_MINIMUM_TRADE": "ENZO's own pre-flight gate stopped the order - MoonPay was never called. The sized amount is below execution.min_trade_usd, which happens when deployable capital is too small for the configured risk band (or when base_token does not match the asset the wallet actually holds). Fix: ./enzoctl wallet, then fund it, close a position, or lower execution.min_trade_usd.",
        "ABOVE_MAXIMUM_TRADE": "Position size exceeded execution.max_trade_usd.",
        "NOT_AUTHENTICATED": "Run: mp login --email you@example.com  then  mp verify --email you@example.com --code <code>",
        "CONSENT_REQUIRED": "Run once: mp consent accept",
        "CLI_NOT_FOUND": "Install the MoonPay CLI: npm i -g @moonpay/cli",
        "WALLET_NOT_FOUND": "The configured execution.wallet_name does not exist. Run: mp wallet list",
        "PAPER_MODE_ENABLED": "paper_mode is true in config/enzo-config.yaml — live execution is blocked by design.",
        "EXEC_NOT_READY": "The executor pre-flight failed. Run: python3 enzo.py doctor",
        "RATE_LIMITED": "MoonPay rate limit hit (60 req/min authenticated). The bot will retry next cycle.",
        "TIMEOUT": "The swap command timed out.",
        "UNKNOWN_OPTION": "The MoonPay CLI rejected a flag — the CLI may have been upgraded. Run: python3 enzo.py doctor",
    }
    hint = hints.get(code, "")

    lines = [
        "🚫 <b>ENZO — BUY EXECUTION FAILED</b>",
        "━━━━━━━━━━━━━━━━━━━━━━━━━━",
        f"💎 <b>Token:</b> <code>{_esc_html(sym)}</code>",
        f"🎯 <b>Confidence:</b> <code>{conf:.1f}</code>   💰 <b>MC:</b> <code>${mcap:,.0f}</code>",
        f"🧾 <b>Mode:</b> {'PAPER' if paper else '<b>LIVE</b>'}",
    ]
    if code:
        lines.append(f"⛔ <b>Reason code:</b> <code>{_esc_html(code)}</code>")
    if reason:
        lines.append(f"📄 <b>Detail:</b> <code>{_esc_html(str(reason)[:400])}</code>")
    if hint:
        lines.append(f"🔧 <b>Fix:</b> {_esc_html(hint)}")
    if mint:
        lines.append(f"🔗 <a href=\"https://gmgn.ai/sol/token/{_esc_html(mint)}\">GMGN</a> · "
                     f"<a href=\"https://dexscreener.com/solana/{_esc_html(mint)}\">DexScreener</a>")
    lines.append("━━━━━━━━━━━━━━━━━━━━━━━━━━")
    lines.append("<i>المركز أُعيد التراجع عنه — لم يُفتح أي مركز وهمي.</i>")

    msg = "\n".join(lines)
    if console:
        print(f"[ENZO][BUY-FAILED] {sym} ({code or 'n/a'}): {str(reason)[:160]}")
    return _send_tg(msg)


def notify_system(title: str, details: str, level: str = "INFO") -> bool:
    """Generic operator alert (startup, shutdown, config problems, dependency
    failures). Distinct from notify_risk so it uses its own cooldown bucket."""
    icon = {"INFO": "ℹ️", "WARN": "⚠️", "ERROR": "🛑"}.get(str(level).upper(), "ℹ️")
    if not _cooldown_ok(f"system_{level}", title):
        return False
    msg = (f"{icon} <b>ENZO — {_esc_html(title)}</b>\n"
           f"━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
           f"<code>{_esc_html(str(details)[:1200])}</code>")
    return _send_tg(msg)
