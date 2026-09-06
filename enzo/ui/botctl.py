#!/usr/bin/env python3
"""
ENZO - Interactive Telegram Control Center
Provides 2-way interactive Telegram bot with inline keyboards, command handlers,
live status queries, manual scan triggers, pause/resume execution, and auto-registration.
"""
from __future__ import annotations  # allow `int | str` annotations on Python < 3.10
import json
import os
import sys
import time
import threading
import urllib.request
import urllib.error
from datetime import datetime, timezone
from typing import Dict, Any, Optional, List

from enzo.core.config import CONTROL_PATH, SECRETS_PATH, load_secrets, load_config
from enzo.execution import portfolio as pf
from enzo.core import learn, engine
from enzo.ui import dashboard
import enzo.core.log as log

_LOGGER = log.get_logger("enzo.botctl")

TG_GET_UPDATES = "https://api.telegram.org/bot{}/getUpdates"
TG_SEND_MSG = "https://api.telegram.org/bot{}/sendMessage"
TG_EDIT_MSG = "https://api.telegram.org/bot{}/editMessageText"
TG_ANSWER_CB = "https://api.telegram.org/bot{}/answerCallbackQuery"


def is_paused() -> bool:
    """True when the operator has asked the bot to stop trading.

    A missing control file means nobody has ever written one, so the honest
    answer is "not paused". A file that EXISTS but cannot be parsed is a
    different matter: it means a pause may have been requested and the record of
    it was damaged. Returning False there would silently re-arm live trading
    against the operator's explicit instruction — the one failure mode this flag
    exists to prevent. So an unreadable control file fails CLOSED (stays paused)
    and logs loudly. Recovery is trivial: pressing Resume on the dashboard (or
    `enzoctl resume`) rewrites the file atomically.
    """
    if not os.path.exists(CONTROL_PATH):
        return False
    try:
        with open(CONTROL_PATH, "r", encoding="utf-8") as f:
            return bool(json.load(f).get("paused", False))
    except Exception as e:
        _LOGGER.error(
            "Control file %s is unreadable (%s: %s) - failing CLOSED, treating the "
            "bot as PAUSED so live trading cannot resume silently. Press Resume on "
            "the dashboard or run `enzoctl resume` to rewrite it.",
            CONTROL_PATH, type(e).__name__, e,
        )
        return True


def set_paused(paused: bool, by: str = "unknown"):
    """Persist the pause flag atomically, with an audit trail.

    The write goes to a temp file and is swapped in with os.replace(), matching
    the convention used by db.py / config.py / learn.py. A plain open(..., "w")
    could be interrupted mid-write and leave a truncated file, which is exactly
    the corruption is_paused() now fails closed on. `updated_at` / `updated_by`
    are recorded so it is always possible to tell when trading was stopped and
    by which surface (dashboard, Telegram, CLI).
    """
    try:
        os.makedirs(os.path.dirname(CONTROL_PATH), exist_ok=True)
        data = {}
        if os.path.exists(CONTROL_PATH):
            try:
                with open(CONTROL_PATH, "r", encoding="utf-8") as f:
                    loaded = json.load(f)
                if isinstance(loaded, dict):
                    data = loaded
                else:
                    _LOGGER.error(
                        "Control file %s held %s, not an object - rebuilding it.",
                        CONTROL_PATH, type(loaded).__name__,
                    )
            except Exception as e:
                _LOGGER.error(
                    "Control file %s could not be read (%s: %s) - rebuilding it.",
                    CONTROL_PATH, type(e).__name__, e,
                )
        data["paused"] = bool(paused)
        data["updated_at"] = datetime.now(timezone.utc).isoformat()
        data["updated_by"] = str(by or "unknown")
        tmp = CONTROL_PATH + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, CONTROL_PATH)  # atomic on POSIX
    except Exception as e:
        _LOGGER.error(f"Failed to set paused state: {e}")


def register_active_chat_id(chat_id: int | str):
    """Auto-register active user chat ID into enzo-secrets.json if changed or missing."""
    try:
        sec = load_secrets()
        current_id = str(sec.get("telegram_chat_id", ""))
        new_id = str(chat_id)
        if current_id != new_id:
            sec["telegram_chat_id"] = new_id
            os.makedirs(os.path.dirname(SECRETS_PATH), exist_ok=True)
            with open(SECRETS_PATH, "w", encoding="utf-8") as f:
                json.dump(sec, f, indent=2)
            _LOGGER.info(f"Auto-registered active Telegram chat_id: {new_id}")
    except Exception as e:
        _LOGGER.error(f"Failed to auto-register chat_id: {e}")


# ================================================================ Telegram API Helpers with Retry
def _tg_request(endpoint: str, payload: dict, max_retries: int = 3) -> Optional[dict]:
    sec = load_secrets()
    token = sec.get("telegram_bot_token")
    if not token:
        _LOGGER.warning("Telegram bot token missing in config/enzo-secrets.json")
        return None

    url = endpoint.format(token)
    data = json.dumps(payload).encode("utf-8")

    for attempt in range(max_retries):
        try:
            req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"})
            with urllib.request.urlopen(req, timeout=12) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            err_body = e.read().decode("utf-8", errors="ignore")
            _LOGGER.error(f"Telegram API HTTP {e.code} ({endpoint.split('/')[-1]}): {err_body[:200]}")
            if e.code in (400, 403, 404):
                return None  # Permanent error, do not retry
            time.sleep(0.5 * (attempt + 1))
        except Exception as e:
            _LOGGER.warning(f"Telegram network retry ({attempt + 1}/{max_retries}) on {endpoint.split('/')[-1]}: {e}")
            time.sleep(0.8 * (attempt + 1))

    return None


def send_message(chat_id: int | str, text: str, reply_markup: dict = None) -> Optional[dict]:
    payload = {
        "chat_id": chat_id,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": True
    }
    if reply_markup:
        payload["reply_markup"] = reply_markup
    return _tg_request(TG_SEND_MSG, payload)


def edit_message(chat_id: int | str, message_id: int, text: str, reply_markup: dict = None) -> Optional[dict]:
    payload = {
        "chat_id": chat_id,
        "message_id": message_id,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": True
    }
    if reply_markup:
        payload["reply_markup"] = reply_markup
    return _tg_request(TG_EDIT_MSG, payload)


def answer_callback(callback_query_id: str, text: str = None):
    payload = {"callback_query_id": callback_query_id}
    if text:
        payload["text"] = text
    return _tg_request(TG_ANSWER_CB, payload)


# ================================================================ Menu & Cards Builders
def build_main_menu_card() -> tuple[str, dict]:
    state = pf.get_state()
    paused = is_paused()
    halted = state.get("halted")
    init_cap = float(state.get("initial_capital", 10000.0))
    eq = float(state.get("equity", init_cap))
    rp = float(state.get("realized_pnl", 0.0))
    open_n = len(state.get("open_positions", {}))
    stats = state.get("stats", {})

    status_str = "⏸ <b>PAUSED (متوقف مؤقتاً)</b>" if paused else ("⚠️ <b>HALTED (موقوف احترازياً)</b>" if halted else "🟢 <b>ACTIVE (يعمل بنشاط)</b>")

    text = f"""
⚡ <b>ENZO QUANT TRADING TERMINAL</b>
━━━━━━━━━━━━━━━━━━━━━━━━━━
🤖 <b>System Status:</b> {status_str}
💰 <b>Total Equity:</b> <code>${eq:,.2f}</code>
📈 <b>Realized PnL:</b> <b>{'+$' if rp >= 0 else '-$'}{abs(rp):,.2f}</b>
🎯 <b>Win Rate:</b> <b>{stats.get('win_rate', 0.0):.1f}%</b> ({stats.get('wins', 0)}W / {stats.get('losses', 0)}L)
⚡ <b>Open Positions:</b> <code>{open_n} / 5 slots</code>
━━━━━━━━━━━━━━━━━━━━━━━━━━
<i>اختر أحد الإجراءات من اللوحة أدناه:</i>
"""
    buttons = {
        "inline_keyboard": [
            [
                {"text": "▶ استئناف البوت" if paused else "⏸ إيقاف مؤقت", "callback_data": "btn_toggle_pause"},
                {"text": "🔍 فحص السوق الآن", "callback_data": "btn_scan"}
            ],
            [
                {"text": "🎯 الصفقات المفتوحة", "callback_data": "btn_positions"},
                {"text": "📜 سجل الصفقات", "callback_data": "btn_trades"}
            ],
            [
                {"text": "🧠 الذكاء والتعلم", "callback_data": "btn_learn"},
                {"text": "🔄 تحديث اللوحة", "callback_data": "btn_refresh_menu"}
            ]
        ]
    }
    return text.strip(), buttons


def build_positions_card() -> tuple[str, dict]:
    state = pf.get_state()
    open_pos = state.get("open_positions", {})

    if not open_pos:
        text = """
🎯 <b>ACTIVE OPEN POSITIONS</b>
━━━━━━━━━━━━━━━━━━━━━━━━━━
<i>لا توجد أي صفقات مفتوحة حالياً في المحفظة.</i>
━━━━━━━━━━━━━━━━━━━━━━━━━━
"""
    else:
        lines = ["🎯 <b>ACTIVE OPEN POSITIONS</b>", "━━━━━━━━━━━━━━━━━━━━━━━━━━"]
        for mint, p in open_pos.items():
            sym = p.get("symbol", "UNKNOWN")
            size = float(p.get("size_usd", 0.0))
            entry_mc = float(p.get("entry_market_cap", 0.0))
            cur_mc = pf.current_market_cap(mint) or p.get("current_market_cap") or entry_mc
            upnl = float(p.get("unrealized_pnl", 0.0))
            ratio = (cur_mc / entry_mc - 1) * 100.0 if entry_mc else 0.0
            
            trail_status = "ON" if p.get("trailing_active") else "OFF"
            lines.append(f"💎 <b>${sym}</b> (<code>{mint[:6]}...{mint[-4:]}</code>)")
            lines.append(f"  • <b>الحجم:</b> <code>${size:,.2f}</code> | <b>الدخول:</b> <code>${entry_mc:,.0f}</code>")
            lines.append(f"  • <b>القيمة الحالية:</b> <code>${cur_mc:,.0f}</code>")
            lines.append(f"  • <b>الربح العائم:</b> <b>{'+$' if upnl >= 0 else '-$'}{abs(upnl):,.2f} ({ratio:+,.1f}%)</b>")
            lines.append(f"  • <b>Trailing Stop:</b> <code>{trail_status}</code>")
            lines.append("──────────────────────────")
        text = "\n".join(lines)

    buttons = {
        "inline_keyboard": [
            [
                {"text": "🔄 تحديث الصفقات", "callback_data": "btn_positions"},
                {"text": "🔙 القائمة الرئيسية", "callback_data": "btn_refresh_menu"}
            ]
        ]
    }
    return text.strip(), buttons


def build_trades_card() -> tuple[str, dict]:
    state = pf.get_state()
    closed = state.get("closed_positions", [])

    if not closed:
        text = """
📜 <b>TRADE HISTORY LEDGER</b>
━━━━━━━━━━━━━━━━━━━━━━━━━━
<i>لا توجد صفقات مغلقة مسجلة حتى الآن.</i>
━━━━━━━━━━━━━━━━━━━━━━━━━━
"""
    else:
        lines = ["📜 <b>RECENT CLOSED TRADES</b>", "━━━━━━━━━━━━━━━━━━━━━━━━━━"]
        for c in closed[-6:]:
            sym = c.get("symbol", "UNKNOWN")
            pnl = float(c.get("pnl", 0.0))
            pnl_pct = float(c.get("pnl_pct", 0.0))
            reason = c.get("reason", "CLOSED")
            closed_at = (c.get("closed_at") or "")[5:16].replace("T", " ")
            icon = "🟢" if pnl >= 0 else "🔴"
            lines.append(f"{icon} <b>${sym}</b>  |  <b>{reason}</b>")
            lines.append(f"  • <b>النتيجة:</b> <b>{'+$' if pnl >= 0 else '-$'}{abs(pnl):,.2f} ({pnl_pct:+,.1f}%)</b>")
            lines.append(f"  • <b>التاريخ:</b> <code>{closed_at}</code>")
            lines.append("──────────────────────────")
        text = "\n".join(lines)

    buttons = {
        "inline_keyboard": [
            [
                {"text": "🔄 تحديث السجل", "callback_data": "btn_trades"},
                {"text": "🔙 القائمة الرئيسية", "callback_data": "btn_refresh_menu"}
            ]
        ]
    }
    return text.strip(), buttons


def build_learning_card() -> tuple[str, dict]:
    learning = learn.get_state()
    text = f"""
🧠 <b>MACHINE LEARNING & AI MATRIX</b>
━━━━━━━━━━━━━━━━━━━━━━━━━━
📊 <b>إجمالي الصفقات المدروسة:</b> <code>{learning.get('total_trades', 0)}</code>
🎯 <b>نسبة النجاح التاريخية:</b> <b>{learning.get('win_rate', 0.0)}%</b>
⚡ <b>انحياز الثقة الذاتي:</b> <code>{learning.get('confidence_bias', 0.0):+,.1f} pts</code>
━━━━━━━━━━━━━━━━━━━━━━━━━━
<b>أعلى ميزات الأمان والربح:</b>
"""
    fw = learning.get("feature_win_rates", [])
    if fw:
        for f in fw[:4]:
            text += f"\n  • <code>{f.get('feature')}</code>: <b>{f.get('win_rate')}%</b> ({f.get('n')} samples)"
    else:
        text += "\n  • <i>قيد جمع العينات...</i>"

    buttons = {
        "inline_keyboard": [
            [{"text": "🔙 القائمة الرئيسية", "callback_data": "btn_refresh_menu"}]
        ]
    }
    return text.strip(), buttons


# ================================================================ Polling Engine Daemon
class TelegramBotListener:
    """Background polling worker for Telegram interactive commands and callbacks."""
    def __init__(self):
        self._stop_event = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._last_offset = 0

    def start(self):
        if self._thread and self._thread.is_alive():
            return
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._poll_loop, daemon=True, name="enzo-tg-bot")
        self._thread.start()
        _LOGGER.info("Telegram Interactive Bot Listener started.")

    def is_alive(self) -> bool:
        """True only while the polling thread is actually running.

        start() spawns a daemon thread that returns IMMEDIATELY when no bot token
        is configured (or dies on the first fatal API error), so "we called
        start()" is not evidence that Telegram commands are being received. The
        supervisor used to print "Listener Active" unconditionally.
        """
        return bool(self._thread and self._thread.is_alive())

    def stop(self):
        self._stop_event.set()
        if self._thread:
            self._thread.join(timeout=3.0)
            _LOGGER.info("Telegram Interactive Bot Listener stopped.")

    def _poll_loop(self):
        sec = load_secrets()
        if not sec.get("telegram_bot_token"):
            _LOGGER.error("Telegram bot token not configured in config/enzo-secrets.json")
            return

        while not self._stop_event.is_set():
            try:
                payload = {"offset": self._last_offset, "timeout": 10}
                res = _tg_request(TG_GET_UPDATES, payload)
                if res and res.get("ok") and res.get("result"):
                    for update in res["result"]:
                        self._last_offset = update["update_id"] + 1
                        self._handle_update(update)
            except Exception as e:
                _LOGGER.error(f"Telegram polling loop exception: {e}")
            time.sleep(0.5)

    def _handle_update(self, update: dict):
        # 1. Handle Callback Query
        if "callback_query" in update:
            cb = update["callback_query"]
            cb_id = cb["id"]
            data = cb.get("data")
            msg = cb.get("message", {})
            chat_id = msg.get("chat", {}).get("id")
            message_id = msg.get("message_id")

            if not chat_id or not message_id:
                answer_callback(cb_id)
                return

            register_active_chat_id(chat_id)

            try:
                if data == "btn_toggle_pause":
                    cur = is_paused()
                    set_paused(not cur, by="telegram:button")
                    dashboard.generate()
                    answer_callback(cb_id, "تم استئناف البوت" if cur else "تم إيقاف البوت مؤقتاً")
                    text, markup = build_main_menu_card()
                    edit_message(chat_id, message_id, text, markup)

                elif data == "btn_scan":
                    answer_callback(cb_id, "جارٍ فحص السوق...")
                    threading.Thread(target=lambda: (engine.scan_once(), dashboard.generate()), daemon=True).start()
                    text, markup = build_main_menu_card()
                    edit_message(chat_id, message_id, text + "\n\n<i>🔍 تم إطلاق فحص السوق في الخلفية...</i>", markup)

                elif data == "btn_positions":
                    answer_callback(cb_id)
                    text, markup = build_positions_card()
                    edit_message(chat_id, message_id, text, markup)

                elif data == "btn_trades":
                    answer_callback(cb_id)
                    text, markup = build_trades_card()
                    edit_message(chat_id, message_id, text, markup)

                elif data == "btn_learn":
                    answer_callback(cb_id)
                    text, markup = build_learning_card()
                    edit_message(chat_id, message_id, text, markup)

                elif data == "btn_refresh_menu":
                    answer_callback(cb_id, "تم التحديث")
                    text, markup = build_main_menu_card()
                    edit_message(chat_id, message_id, text, markup)
                else:
                    answer_callback(cb_id)
            except Exception as e:
                _LOGGER.error(f"Error handling callback {data}: {e}")
                answer_callback(cb_id, "حدث خطأ في الاستجابة")
            return

        # 2. Handle Direct Commands
        if "message" in update:
            msg = update["message"]
            text = (msg.get("text") or "").strip()
            chat_id = msg.get("chat", {}).get("id")

            if not text or not chat_id:
                return

            register_active_chat_id(chat_id)
            cmd = text.split()[0].lower()

            try:
                if cmd in ("/start", "/menu", "/help"):
                    card_text, markup = build_main_menu_card()
                    send_message(chat_id, card_text, markup)

                elif cmd == "/status":
                    card_text, markup = build_main_menu_card()
                    send_message(chat_id, card_text, markup)

                elif cmd == "/positions":
                    card_text, markup = build_positions_card()
                    send_message(chat_id, card_text, markup)

                elif cmd == "/trades":
                    card_text, markup = build_trades_card()
                    send_message(chat_id, card_text, markup)

                elif cmd == "/scan":
                    send_message(chat_id, "🔍 <b>جارٍ فحص واكتشاف العملات في السوق عبر GMGN و Pump.fun...</b>")
                    threading.Thread(target=lambda: (engine.scan_once(), dashboard.generate()), daemon=True).start()

                elif cmd == "/pause":
                    set_paused(True, by="telegram:/pause")
                    dashboard.generate()
                    send_message(chat_id, "⏸ <b>تم إيقاف التداول مؤقتاً (PAUSED).</b>")

                elif cmd == "/resume":
                    set_paused(False, by="telegram:/resume")
                    dashboard.generate()
                    send_message(chat_id, "▶ <b>تم استئناف التداول النشط (RESUMED).</b>")
            except Exception as e:
                _LOGGER.error(f"Error handling message command {cmd}: {e}")


_LISTENER = None


def get_telegram_listener() -> TelegramBotListener:
    global _LISTENER
    if _LISTENER is None:
        _LISTENER = TelegramBotListener()
    return _LISTENER


if __name__ == "__main__":
    listener = get_telegram_listener()
    print("[ENZO] Telegram Interactive Control Center started polling...")
    listener.start()
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        listener.stop()
        print("\n[ENZO] Telegram listener stopped.")
