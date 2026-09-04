#!/usr/bin/env python3
"""
ENZO - Live Interactive Web Dashboard Server
Provides real-time REST API endpoints, live activity streaming, and serves the ultra-modern ENZO control dashboard.
"""
import os
import sys
import json
import time
import socket
import threading
import traceback
import http.server
import socketserver
from urllib.parse import urlparse, parse_qs
from datetime import datetime, timezone

from enzo.core.config import (
    DASHBOARD_HTML_PATH,
    WORKSPACE_ROOT,
    HEALTH_PATH,
    PID_PATH,
    PORTFOLIO_DB_PATH,
    load_config,
)
from enzo.execution import portfolio as pf, exit_monitor
from enzo.core import db, learn, engine, audit
from enzo.ui import botctl, dashboard
from enzo.providers import gmgn, pump
import enzo.core.log as log

_LOGGER = log.get_logger("enzo.serve")

PORT = 8077
_STARTED_AT = time.time()
_REQ_COUNT = {"total": 0, "errors": 0}
# Filled by the engine loop so /health can prove the trading side is alive too.
ENGINE_HEARTBEAT = {"last_scan_ts": 0.0, "last_scan_status": "never", "cycles": 0,
                    "candidates": 0, "interval_sec": 0.0}


def beat(status: str = None, candidates: int = None, interval: float = None):
    """Record an engine heartbeat (called by core.engine after each cycle)."""
    ENGINE_HEARTBEAT["last_scan_ts"] = time.time()
    ENGINE_HEARTBEAT["cycles"] += 1
    if status is not None:
        ENGINE_HEARTBEAT["last_scan_status"] = status
    if candidates is not None:
        ENGINE_HEARTBEAT["candidates"] = candidates
    if interval is not None:
        ENGINE_HEARTBEAT["interval_sec"] = float(interval)


def _gmgn_discovery() -> dict:
    try:
        st = gmgn.discovery_status() or {}
    except Exception:
        return {}
    cats = st.get("categories_ok") or {}
    failed = [c for c, v in cats.items() if not v.get("ok")]
    return {"categories_failed": failed,
            "consecutive_empty_cycles": st.get("consecutive_empty"),
            "last_count": st.get("last_count"),
            "last_ok_age_sec": st.get("age_sec"),
            "last_error": st.get("last_error")}


def health_snapshot() -> dict:
    """Single source of truth for liveness — used by /health, /api/health,
    `enzoctl status` and the on-disk heartbeat file."""
    now = time.time()
    last_beat = ENGINE_HEARTBEAT["last_scan_ts"] or 0.0
    interval = ENGINE_HEARTBEAT["interval_sec"] or 60.0
    scan_age = round(now - last_beat, 1) if last_beat else None
    # A scan cycle can legitimately take a while (12 deep analyses × ~8 s), so
    # only call it stale after several missed intervals.
    scan_stale = bool(scan_age is not None and scan_age > max(180.0, interval * 4))

    try:
        state = pf.get_state()
        cfg = load_config()
        paused = botctl.is_paused()
    except Exception as e:
        return {"status": "error", "reason": f"state read failed: {e}", "ts": now}

    monitor_running = False
    try:
        monitor_running = exit_monitor.get_exit_monitor().is_running()
    except Exception:
        pass

    has_open = bool(state.get("open_positions"))
    problems = []
    if paused:
        problems.append("TRADING_PAUSED")
    if state.get("halted"):
        problems.append(f"RISK_HALTED: {state.get('halted')}")
    if scan_stale:
        problems.append(f"ENGINE_STALE: last scan {scan_age}s ago")
    if last_beat == 0.0:
        problems.append("ENGINE_NEVER_SCANNED")
    if has_open and not monitor_running:
        problems.append("EXIT_MONITOR_DOWN_WITH_OPEN_POSITIONS")
    ban = 0.0
    try:
        ban = max(0.0, gmgn.ban_status())
    except Exception:
        pass
    if ban > 0:
        problems.append(f"GMGN_RATE_LIMITED: {ban:.0f}s remaining")
    _gd = _gmgn_discovery()
    if _gd.get("categories_failed"):
        problems.append(f"GMGN_DISCOVERY_FAILED: {','.join(_gd['categories_failed'])}"
                        f"{(' — ' + str(_gd.get('last_error'))[:70]) if _gd.get('last_error') else ''}")

    # ── Capital: in LIVE mode an unreadable wallet means the bot cannot trade
    # at all, so it is a problem, not an "ok with a note". Previously /health
    # reported "ok" while position sizing was blocked at $0.00.
    paper = bool(cfg.get("paper_mode", True))
    cap = {}
    try:
        cap = pf.capital_info() or {}
    except Exception:
        pass
    cap_usd = float(cap.get("usd") or 0.0)
    cap_ok = bool(cap.get("ok"))
    if not paper and cap.get("blocked"):
        problems.append(f"CAPITAL_BLOCKED: {str(cap.get('detail') or 'wallet unreadable')[:110]}")
    elif not paper and not cap_ok:
        problems.append(f"CAPITAL_SYNC_FAILED: {str(cap.get('detail') or 'unknown')[:110]}")
    elif not paper and cap_usd < float((cfg.get("execution") or {}).get("min_trade_usd", 1.0)):
        problems.append(f"CAPITAL_BELOW_MIN_TRADE: ${cap_usd:,.2f} deployable")

    # ── Discovery feed: a dead WebSocket is why the bot "found 0 candidates"
    # for its entire life while every status page said everything was fine.
    pump_state = {}
    try:
        pump_state = pump.get_pumpdev_client().status()
    except Exception:
        pass
    pstate = str(pump_state.get("state") or "UNKNOWN")
    if pstate in ("DOWN", "RETRYING"):
        detail = str(pump_state.get("last_error") or "")[:90]
        problems.append(f"PUMPDEV_{pstate}" + (f": {detail}" if detail else ""))
    elif pump_state.get("stale"):
        problems.append(f"PUMPDEV_STALE: no message for "
                        f"{pump_state.get('last_message_age_sec')}s")

    faults = {}
    try:
        faults = engine.discovery_faults()
    except Exception:
        pass
    for src, msg in faults.items():
        problems.append(f"DISCOVERY_FAULT[{src}]: {str(msg)[:90]}")

    # ── Dashboard render failures used to be swallowed and the browser silently
    # kept getting the last HTML that worked.
    last_render = {}
    try:
        last_render = dashboard.last_render() or {}
    except Exception:
        pass
    if last_render.get("error"):
        problems.append(f"DASHBOARD_RENDER_ERROR: {str(last_render['error'])[:90]}")

    # ── Executor readiness (LIVE only): names the missing CLI / auth / wallet.
    exec_ready, exec_msg = True, ""
    if not paper:
        try:
            from enzo.execution import executor as _ex
            exec_ready, exec_msg = _ex.is_ready(cfg)
        except Exception as e:
            exec_ready, exec_msg = False, f"{type(e).__name__}: {e}"
        if not exec_ready:
            problems.append(f"EXECUTOR_NOT_READY: {str(exec_msg)[:110]}")

    snap = {
        "status": "degraded" if problems else "ok",
        "ts": now,
        "iso": datetime.now(timezone.utc).isoformat(),
        "uptime_sec": round(now - _STARTED_AT, 1),
        "requests": dict(_REQ_COUNT),
        "mode": "PAPER" if cfg.get("paper_mode", True) else "LIVE",
        "paused": paused,
        "halted": state.get("halted"),
        "equity": round(float(state.get("equity", 0.0) or 0.0), 2),
        "initial_capital": round(float(state.get("initial_capital", 0.0) or 0.0), 2),
        "realized_pnl": round(float(state.get("realized_pnl", 0.0) or 0.0), 2),
        "open_positions": len(state.get("open_positions") or {}),
        "closed_trades": len(state.get("closed_positions") or []),
        "engine": {
            "cycles": ENGINE_HEARTBEAT["cycles"],
            "last_scan_status": ENGINE_HEARTBEAT["last_scan_status"],
            "last_candidates": ENGINE_HEARTBEAT["candidates"],
            "last_scan_age_sec": scan_age,
            "interval_sec": interval,
            "stale": scan_stale,
        },
        "exit_monitor": {"running": monitor_running, "needed": has_open},
        "gmgn": {"ban_remaining_sec": round(ban, 1), **_gmgn_discovery()},
        "capital": {"usd": round(cap_usd, 2), "source": cap.get("source"),
                    "ok": cap_ok, "blocked": bool(cap.get("blocked")),
                    "age_sec": cap.get("age_sec"), "detail": cap.get("detail")},
        "pumpdev": {"state": pstate, "buffered_tokens": pump_state.get("buffered_tokens"),
                    "tokens_seen": pump_state.get("tokens_seen"),
                    "messages": pump_state.get("messages"),
                    "last_message_age_sec": pump_state.get("last_message_age_sec"),
                    "last_error": pump_state.get("last_error")},
        "executor": {"ready": bool(exec_ready), "detail": str(exec_msg)[:200]},
        "dashboard": {
            "last_render_age_sec": last_render.get("age_sec") if last_render.get("ts") else None,
            "last_error": last_render.get("error"),
        },
        "problems": problems,
    }
    # Persist for out-of-process readers (`enzoctl status` when the bot was
    # started by someone else, or OpenClaw inspecting the workspace directly).
    try:
        os.makedirs(os.path.dirname(HEALTH_PATH), exist_ok=True)
        tmp = HEALTH_PATH + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(snap, f, indent=2)
        os.replace(tmp, HEALTH_PATH)
    except Exception:
        pass
    return snap


class EnzoDashboardHandler(http.server.SimpleHTTPRequestHandler):
    # Quiet, structured access logging into data/logs/enzo.log instead of
    # stderr, where it was previously interleaved with engine output.
    def log_message(self, fmt, *args):
        try:
            _LOGGER.debug("%s - %s", self.address_string(), fmt % args)
        except Exception:
            pass

    def translate_path(self, path):
        if path.strip("/") in ("", "enzo-dashboard.html", "index.html"):
            return DASHBOARD_HTML_PATH
        # Never serve anything outside the workspace root.
        clean = os.path.normpath(path.lstrip("/")).lstrip("/")
        full = os.path.abspath(os.path.join(WORKSPACE_ROOT, clean))
        if not full.startswith(os.path.abspath(WORKSPACE_ROOT)):
            return DASHBOARD_HTML_PATH
        return full

    def _send_html(self, content: bytes, status: int = 200):
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(content)))
        self.send_header("Cache-Control", "no-cache, no-store, must-revalidate")
        self.send_header("Pragma", "no-cache")
        self.send_header("Expires", "0")
        self.end_headers()
        self.wfile.write(content)

    def _dashboard_html(self) -> bytes:
        """Return the freshest dashboard HTML we can, and never hide a failure.

        Order: regenerate -> in-memory last render -> last-good file on disk ->
        a self-contained error page. The old code did
        `try: generate() except Exception: pass; return super().do_GET()`,
        which silently served whatever stale file happened to be on disk — the
        direct cause of "the dashboard data is never updated".
        """
        try:
            dashboard.generate()
        except Exception as e:
            _LOGGER.error("dashboard.generate() FAILED — serving last good render: %s", e)
            _LOGGER.debug("dashboard traceback:\n%s", traceback.format_exc())
            res = dashboard.generate_safe()  # records the error for the banner
            _REQ_COUNT["errors"] += 1

            lr = dashboard.last_render()
            if lr.get("html"):
                html_txt = lr["html"]
                banner = (
                    '<div id="serverFault" class="fault-banner shown" style="display:flex;">'
                    '<span class="fault-icon">⚠</span>'
                    f'<span><b>This page is STALE.</b> Regeneration failed: '
                    f'{str(e)[:200].replace("<", "&lt;")}. '
                    f'Last successful render {lr.get("age_sec")}s ago.</span>'
                    '<span class="fault-hint">python3 enzo.py doctor</span></div>'
                )
                marker = '<div class="dashboard-container">'
                if marker in html_txt:
                    html_txt = html_txt.replace(marker, marker + banner, 1)
                return html_txt.encode("utf-8")

            for fallback in (dashboard.LAST_GOOD_PATH, DASHBOARD_HTML_PATH):
                try:
                    if os.path.exists(fallback):
                        with open(fallback, "rb") as f:
                            return f.read()
                except OSError:
                    continue

            return (
                "<!doctype html><meta charset='utf-8'><title>ENZO — dashboard error</title>"
                "<body style='background:#080c14;color:#f8fafc;font-family:monospace;padding:40px'>"
                "<h2>⚠ ENZO dashboard could not be generated</h2>"
                f"<pre style='color:#f43f5e'>{str(e)[:800].replace('<','&lt;')}</pre>"
                "<p>Run <code>python3 enzo.py doctor</code> for the full preflight report.</p>"
                "</body>".encode("utf-8")
            )

        with open(DASHBOARD_HTML_PATH, "rb") as f:
            return f.read()

    def _send_json(self, data, status=200):
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.send_header("Cache-Control", "no-cache, no-store, must-revalidate")
        self.send_header("Pragma", "no-cache")
        self.end_headers()
        self.wfile.write(json.dumps(data, ensure_ascii=False).encode("utf-8"))

    def do_OPTIONS(self):
        self._send_json({"status": "ok"})

    def do_GET(self):
        parsed = urlparse(self.path)

        # 1. Full State API
        if parsed.path == "/api/state":
            try:
                state = pf.get_state()
                cfg = load_config()
                paused = botctl.is_paused()
                learning = learn.get_state()
                ban_rem = gmgn.ban_status()

                # Calculate detailed analytics
                closed = state.get("closed_positions", [])
                wins = [c for c in closed if float(c.get("pnl", 0)) > 0]
                losses = [c for c in closed if float(c.get("pnl", 0)) <= 0]
                total_trades = len(closed)
                win_rate = (len(wins) / total_trades * 100) if total_trades else 0.0

                gross_profit = sum(float(c.get("pnl", 0)) for c in wins)
                gross_loss = abs(sum(float(c.get("pnl", 0)) for c in losses))
                profit_factor = round(gross_profit / gross_loss, 2) if gross_loss > 0 else (round(gross_profit, 2) if gross_profit > 0 else 1.0)

                # Open positions — use the DB-stored snapshot (refreshed by the
                # exit monitor every cycle) instead of live network fetches so
                # the dashboard never competes with the trading engine for
                # rate-limiter budget or blocks the HTTP thread on I/O.
                open_pos = {}
                for mint, p in (state.get("open_positions") or {}).items():
                    entry_mc = float(p.get("entry_market_cap") or 0.0)
                    cur_mc = float(p.get("current_market_cap") or entry_mc or 0.0)
                    ratio = (cur_mc / entry_mc - 1) if entry_mc > 0 else 0.0
                    upnl_usd = float(p.get("size_usd", 0.0)) * ratio
                    upnl_pct = ratio * 100.0

                    pos_data = dict(p)
                    pos_data["current_market_cap"] = cur_mc
                    pos_data["unrealized_pnl"] = round(upnl_usd, 2)
                    pos_data["unrealized_pnl_pct"] = round(upnl_pct, 2)
                    open_pos[mint] = pos_data

                # Chart equity points
                init_cap = float(state.get("initial_capital", 10000.0))
                chart_points = [{"label": "Start", "value": init_cap, "pnl": 0.0}]
                cum_pnl = 0.0
                for c in sorted(closed, key=lambda x: x.get("closed_at", "")):
                    cum_pnl += float(c.get("pnl", 0.0))
                    chart_points.append({
                        "label": (c.get("closed_at", "")[5:16] or c.get("symbol", "?")).replace("T", " "),
                        "value": round(init_cap + cum_pnl, 2),
                        "pnl": round(float(c.get("pnl", 0.0)), 2),
                        "symbol": c.get("symbol", "")
                    })
                chart_points.append({
                    "label": "Now",
                    "value": round(pf.equity(state), 2),
                    "pnl": round(cum_pnl, 2),
                    "symbol": "Current"
                })

                # Risk Limits Details
                rm = cfg.get("risk_management", {})
                max_daily = float(rm.get("max_daily_loss", 8.0))
                max_dd = float(rm.get("max_drawdown", 25.0))
                eq = float(state.get("equity", init_cap))
                peak = float(state.get("peak_equity", eq))
                current_dd = round(((peak - eq) / peak * 100.0) if peak > 0 else 0.0, 2)
                daily_loss = float(state.get("daily_loss", 0.0))
                current_dl_pct = round(abs(daily_loss) / eq * 100.0 if (daily_loss < 0 and eq > 0) else 0.0, 2)

                res = {
                    "status": "success",
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                    "system": {
                        "mode": "PAPER TRADING" if cfg.get("paper_mode", True) else "LIVE",
                        "is_paused": paused,
                        "is_halted": bool(state.get("halted")),
                        "halt_reason": state.get("halted"),
                        "gmgn_ban_remaining": max(0.0, ban_rem),
                    },
                    "portfolio": {
                        "equity": eq,
                        "initial_capital": init_cap,
                        "realized_pnl": float(state.get("realized_pnl", 0.0)),
                        "peak_equity": peak,
                        "daily_loss": daily_loss,
                        "consecutive_losses": int(state.get("consecutive_losses", 0)),
                        "open_positions_count": len(open_pos),
                        "closed_trades_count": total_trades,
                        "win_rate": round(win_rate, 1),
                        "wins": len(wins),
                        "losses": len(losses),
                        "profit_factor": profit_factor,
                        "gross_profit": round(gross_profit, 2),
                        "gross_loss": round(gross_loss, 2),
                        "current_drawdown_pct": current_dd,
                        "max_drawdown_limit_pct": max_dd,
                        "current_daily_loss_pct": current_dl_pct,
                        "max_daily_loss_limit_pct": max_daily,
                    },
                    "open_positions": open_pos,
                    "closed_positions": closed,
                    "chart_points": chart_points,
                    "learning": learning,
                    "config_summary": {
                        "risk_per_trade": float(rm.get("risk_per_trade", 2.5)),
                        "max_exposure": float(rm.get("max_exposure", 30.0)),
                        "take_profit_stages": cfg.get("exit_strategy", {}).get("take_profit_stages", []),
                        "stop_loss_percentage": cfg.get("exit_strategy", {}).get("stop_loss_percentage", 50.0),
                        "trailing_stop_percentage": cfg.get("exit_strategy", {}).get("trailing_stop_percentage", 30.0),
                        "weights": cfg.get("weighted_confidence", {})
                    }
                }
                return self._send_json(res)
            except Exception as e:
                return self._send_json({"status": "error", "message": str(e)}, status=500)

        # 2. Real-Time Bot Activity & Decisions Stream API
        if parsed.path == "/api/activity":
            try:
                activities = audit.get_recent_activities(limit=100)
                pump_client = pump.get_pumpdev_client()
                recent_stream_tokens = pump_client.get_recent_tokens(limit=20)
                
                # Every field below is OBSERVED, not asserted. The previous
                # version hardcoded exit_monitor="ARMED" and
                # database="CONCURRENT_SAFE" and derived the pump status from
                # "did we happen to buffer a token", so a dead WebSocket looked
                # like "CONNECTING" forever and a stopped exit monitor still
                # reported itself armed while open positions went unmonitored.
                pump_state = {}
                try:
                    pump_state = pump_client.status()
                except Exception as e:
                    pump_state = {"state": "UNKNOWN", "last_error": str(e)}

                gmgn_ban = 0.0
                try:
                    gmgn_ban = float(gmgn.ban_status() or 0.0)
                except Exception:
                    pass
                gmgn_faults = {}
                try:
                    from enzo.core import engine as _eng
                    gmgn_faults = _eng.discovery_faults()
                except Exception:
                    pass

                mon_running = False
                mon_cycles = None
                mon_age = None
                try:
                    mon = exit_monitor.get_exit_monitor()
                    mon_running = bool(mon.is_running())
                    mon_cycles = getattr(mon, "cycle_count", None)
                    last_ts = getattr(mon, "last_cycle_ts", None)
                    mon_age = round(time.time() - float(last_ts), 1) if last_ts else None
                except Exception:
                    pass

                db_ok, db_detail = False, "unreadable"
                try:
                    st = pf.load_state()
                    db_ok = True
                    db_detail = (f"{len(st.get('open_positions') or {})} open, "
                                 f"{len(st.get('closed_positions') or [])} closed")
                except Exception as e:
                    db_detail = f"{type(e).__name__}: {e}"

                n_open = 0
                try:
                    n_open = len(pf.load_state().get("open_positions") or {})
                except Exception:
                    pass

                subsystems = {
                    "pumpdev_ws": {
                        "name": "PumpDev WebSocket Engine",
                        "status": pump_state.get("state", "UNKNOWN"),
                        "buffered_tokens": len(recent_stream_tokens),
                        "tokens_seen": pump_state.get("tokens_seen"),
                        "messages": pump_state.get("messages"),
                        "connects": pump_state.get("connects"),
                        "last_message_age_sec": pump_state.get("last_message_age_sec"),
                        "stale": pump_state.get("stale"),
                        "live_trades_monitored": pump_state.get("subscribed_trades"),
                        "last_error": pump_state.get("last_error"),
                    },
                    "gmgn_provider": {
                        "name": "GMGN Market Scanner",
                        "status": ("RATE_LIMITED" if gmgn_ban > 0
                                   else ("FAULTED" if gmgn_faults.get("gmgn") else "OPERATIONAL")),
                        "ban_remaining_sec": round(gmgn_ban, 1),
                        "last_error": gmgn_faults.get("gmgn"),
                    },
                    "exit_monitor": {
                        "name": "Unified Exit Monitor",
                        "status": ("RUNNING" if mon_running
                                   else ("DOWN_WITH_OPEN_POSITIONS" if n_open else "STOPPED")),
                        "cycles": mon_cycles,
                        "last_cycle_age_sec": mon_age,
                        "open_positions": n_open,
                        "priority": "PumpDev -> GMGN -> Bonding Curve",
                    },
                    "database": {
                        "name": "SQLite WAL Engine",
                        "status": "OK" if db_ok else "ERROR",
                        "detail": db_detail,
                        "path": os.path.relpath(PORTFOLIO_DB_PATH),
                    },
                }

                snap = health_snapshot()
                return self._send_json({
                    "status": "success",
                    "health": snap.get("status"),
                    "engine": snap.get("engine"),
                    "activities": activities,
                    "subsystems": subsystems,
                    "stream_tokens": recent_stream_tokens
                })
            except Exception as e:
                return self._send_json({"status": "error", "message": str(e)}, status=500)

        # 3. Fast Prices Poller API
        if parsed.path == "/api/prices":
            try:
                state = pf.load_state()
                positions = {}
                for mint, p in (state.get("open_positions") or {}).items():
                    entry_mc = float(p.get("entry_market_cap") or 0.0)
                    cur_mc = pf.current_market_cap(mint) or p.get("current_market_cap") or entry_mc
                    ratio = (cur_mc / entry_mc - 1) if entry_mc else 0.0
                    upnl = float(p.get("size_usd", 0.0)) * ratio
                    positions[mint] = {
                        "entry_mc": entry_mc,
                        "live_mc": cur_mc,
                        "uPnL": round(upnl, 2),
                        "uPnL_pct": round(ratio * 100.0, 2)
                    }
                res = {
                    "equity": round(pf.equity(state), 2),
                    "realized": float(state.get("realized_pnl", 0.0)),
                    "open_n": len(state.get("open_positions") or {}),
                    "positions": positions
                }
                return self._send_json(res)
            except Exception as e:
                return self._send_json({"error": str(e)}, status=500)

        # 4. Liveness endpoints — cheap, no DB writes beyond the heartbeat file.
        #    /health is the contract a supervisor (or OpenClaw) polls: it must
        #    answer even when the trading engine is wedged.
        if parsed.path in ("/health", "/api/health"):
            _REQ_COUNT["total"] += 1
            snap = health_snapshot()
            code = 200 if snap.get("status") == "ok" else 503
            if parsed.path == "/health":
                # minimal, grep-friendly body for shell supervisors
                body = json.dumps({"status": snap.get("status"),
                                   "problems": snap.get("problems", [])}).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(body)
                return
            return self._send_json(snap, status=code)

        # 5. Serve Dashboard HTML Directly
        if parsed.path in ("/", "/enzo-dashboard.html", "/index.html"):
            _REQ_COUNT["total"] += 1
            try:
                self._send_html(self._dashboard_html())
            except (BrokenPipeError, ConnectionResetError):
                pass
            except Exception as e:
                _LOGGER.error("dashboard serve failed: %s", e)
                _REQ_COUNT["errors"] += 1
                self._send_json({"status": "error", "message": str(e)}, status=500)
            return

        _REQ_COUNT["total"] += 1
        return super().do_GET()

    def do_POST(self):
        parsed = urlparse(self.path)

        # Pause / Resume Control Toggle
        if parsed.path == "/api/control/toggle":
            try:
                cur = botctl.is_paused()
                botctl.set_paused(not cur, by="web-dashboard")
                audit.log_event(
                    category="SYSTEM",
                    level="INFO",
                    message=f"Trading {'PAUSED' if not cur else 'RESUMED'} via Web Dashboard"
                )
                # Regenerating the dashboard used to happen BEFORE the response
                # was built, so the NameError in generate() turned every press
                # of the Pause/Resume button into an HTTP 500 even though the
                # pause itself had already been written. Render failures are
                # now non-fatal and reported separately.
                render = dashboard.generate_safe()
                return self._send_json({
                    "status": "success",
                    "paused": not cur,
                    "message": "Bot paused" if not cur else "Bot resumed",
                    "dashboard_rendered": render.get("ok"),
                    "dashboard_error": render.get("error"),
                })
            except Exception as e:
                _LOGGER.error("toggle failed: %s\n%s", e, traceback.format_exc())
                return self._send_json({"status": "error", "message": str(e)}, status=500)

        # Trigger Manual Scan
        if parsed.path == "/api/scan":
            try:
                audit.log_event(
                    category="DISCOVERY",
                    level="INFO",
                    message="Manual market scan cycle triggered from Web Dashboard"
                )
                def run_bg_scan():
                    try:
                        res = engine.scan_once()
                        beat(status="manual_scan", candidates=len(res or []))
                    except Exception as e:
                        _LOGGER.error("manual scan failed: %s", e)
                        beat(status=f"error: {e}")
                    finally:
                        dashboard.generate_safe()

                if engine.SCAN_LOCK.locked():
                    return self._send_json({
                        "status": "busy",
                        "message": "A scan cycle is already running — this request was not queued."
                    })
                threading.Thread(target=run_bg_scan, daemon=True).start()
                return self._send_json({"status": "success", "message": "Scan initiated in background."})
            except Exception as e:
                return self._send_json({"status": "error", "message": str(e)}, status=500)

        return self._send_json({"status": "error", "message": "Endpoint not found"}, status=404)


class _ReusableServer(http.server.ThreadingHTTPServer):
    """One thread per request, and a clear error when the port is taken."""
    allow_reuse_address = True
    daemon_threads = True

    def handle_error(self, request, client_address):
        exc = sys.exc_info()[1]
        if isinstance(exc, (BrokenPipeError, ConnectionResetError)):
            return
        _REQ_COUNT["errors"] += 1
        _LOGGER.error("HTTP handler error from %s: %s", client_address, exc)
        _LOGGER.debug("%s", traceback.format_exc())


def run_server(host: str = None, port: int = None):
    cfg = {}
    try:
        cfg = load_config()
    except Exception:
        cfg = {}
    dash = (cfg.get("dashboard") or {})
    host = host or str(dash.get("host") or "0.0.0.0")
    port = int(port or dash.get("port") or PORT)

    try:
        httpd = _ReusableServer((host, port), EnzoDashboardHandler)
    except OSError as e:
        hint = ""
        if "Address already in use" in str(e) or getattr(e, "errno", None) == 48:
            hint = (f"\n  Port {port} is already in use — ENZO may already be running.\n"
                    f"  Check:  python3 enzo.py status\n"
                    f"  Stop:   python3 enzo.py stop\n"
                    f"  Or use another port:  python3 enzo.py serve --port {port + 1}")
        raise RuntimeError(f"Cannot bind dashboard server on {host}:{port}: {e}{hint}") from e

    # Render once at startup so the very first request is not the one that pays
    # for it (and so a broken template is reported immediately, not on page load).
    res = dashboard.generate_safe()
    if not res.get("ok"):
        _LOGGER.error("Initial dashboard render FAILED: %s", res.get("error"))
    _LOGGER.info("Dashboard server listening on http://%s:%d/  (health: /health)", host, port)
    print(f"[ENZO] Live Dashboard Server running at http://{host}:{port}/enzo-dashboard.html")
    print(f"[ENZO] Health endpoint: http://{host}:{port}/health")
    try:
        httpd.serve_forever(poll_interval=0.4)
    except KeyboardInterrupt:
        print("\n[ENZO] Dashboard server stopped.")
    finally:
        try:
            httpd.server_close()
        except Exception:
            pass


if __name__ == "__main__":
    port = int(sys.argv[1]) if len(sys.argv) > 1 else PORT
    run_server(port=port)
