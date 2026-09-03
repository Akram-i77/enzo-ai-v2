#!/usr/bin/env python3
"""
ENZO - Live Interactive Web Dashboard Server
Provides real-time REST API endpoints, live activity streaming, and serves the ultra-modern ENZO control dashboard.
"""
import os
import sys
import json
import threading
import http.server
import socketserver
from urllib.parse import urlparse, parse_qs
from datetime import datetime, timezone

from enzo.core.config import DASHBOARD_HTML_PATH, WORKSPACE_ROOT, load_config
from enzo.execution import portfolio as pf
from enzo.core import db, learn, engine, audit
from enzo.ui import botctl, dashboard
from enzo.providers import gmgn, pump

PORT = 8077


class EnzoDashboardHandler(http.server.SimpleHTTPRequestHandler):
    def translate_path(self, path):
        if path.strip("/") in ("", "enzo-dashboard.html", "index.html"):
            return DASHBOARD_HTML_PATH
        return os.path.join(WORKSPACE_ROOT, path.lstrip("/"))

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
                
                subsystems = {
                    "pumpdev_ws": {
                        "name": "PumpDev WebSocket Engine",
                        "status": "STREAMING" if recent_stream_tokens else "CONNECTING",
                        "buffered_tokens": len(recent_stream_tokens),
                        "live_trades_monitored": len(pump_client._subscribed_trades)
                    },
                    "gmgn_provider": {
                        "name": "GMGN Market Scanner",
                        "status": "RATE_LIMITED" if gmgn.ban_status() > 0 else "OPERATIONAL",
                        "ban_remaining_sec": round(gmgn.ban_status(), 1)
                    },
                    "exit_monitor": {
                        "name": "Unified Exit Monitor",
                        "status": "ARMED",
                        "priority": "PumpDev -> GMGN -> Bonding Curve",
                        "stale_protection": "10.0s threshold"
                    },
                    "database": {
                        "name": "SQLite WAL Engine",
                        "status": "CONCURRENT_SAFE",
                        "path": "data/enzo.db"
                    }
                }

                return self._send_json({
                    "status": "success",
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

        # 4. Serve Dashboard HTML Directly
        if parsed.path in ("/", "/enzo-dashboard.html", "/index.html"):
            try:
                dashboard.generate()
                if os.path.exists(DASHBOARD_HTML_PATH):
                    with open(DASHBOARD_HTML_PATH, "rb") as f:
                        content = f.read()
                    self.send_response(200)
                    self.send_header("Content-Type", "text/html; charset=utf-8")
                    self.send_header("Content-Length", str(len(content)))
                    self.send_header("Cache-Control", "no-cache, no-store, must-revalidate")
                    self.send_header("Pragma", "no-cache")
                    self.send_header("Expires", "0")
                    self.end_headers()
                    self.wfile.write(content)
                    return
            except Exception:
                pass
            return super().do_GET()

        return super().do_GET()

    def do_POST(self):
        parsed = urlparse(self.path)

        # Pause / Resume Control Toggle
        if parsed.path == "/api/control/toggle":
            try:
                cur = botctl.is_paused()
                botctl.set_paused(not cur)
                audit.log_event(
                    category="SYSTEM",
                    level="INFO",
                    message=f"Trading {'PAUSED' if not cur else 'RESUMED'} via Web Dashboard"
                )
                dashboard.generate()
                return self._send_json({
                    "status": "success",
                    "paused": not cur,
                    "message": "Bot paused" if not cur else "Bot resumed"
                })
            except Exception as e:
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
                    engine.scan_once()
                    dashboard.generate()
                threading.Thread(target=run_bg_scan, daemon=True).start()
                return self._send_json({"status": "success", "message": "Scan initiated in background."})
            except Exception as e:
                return self._send_json({"status": "error", "message": str(e)}, status=500)

        return self._send_json({"status": "error", "message": "Endpoint not found"}, status=404)


def run_server(host: str = "0.0.0.0", port: int = 8077):
    # ThreadingHTTPServer: one thread per request so slow clients or
    # dashboard.generate() never freeze the whole dashboard.
    http.server.ThreadingHTTPServer.allow_reuse_address = True
    with http.server.ThreadingHTTPServer((host, port), EnzoDashboardHandler) as httpd:
        print(f"[ENZO] High-Performance Live Dashboard Server running at http://{host}:{port}/enzo-dashboard.html")
        try:
            httpd.serve_forever()
        except KeyboardInterrupt:
            print("\n[ENZO] Dashboard server stopped.")


if __name__ == "__main__":
    port = int(sys.argv[1]) if len(sys.argv) > 1 else PORT
    run_server(port=port)
