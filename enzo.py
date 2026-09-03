#!/usr/bin/env python3
"""
ENZO - Unified Trading System CLI Controller
Provides command-line interface for autonomous memecoin trading, analysis, and portfolio operations.

Usage:
  python3 enzo.py start                   # Start complete system supervisor (serve + botctl + loop)
  python3 enzo.py scan [<mint>]           # Run analysis on a mint or entire discovery pool
  python3 enzo.py loop [--interval 60]    # Run continuous trading engine loop
  python3 enzo.py status                  # Display portfolio state, equity, and limits
  python3 enzo.py trades                  # List open positions and closed trade history
  python3 enzo.py dashboard               # Regenerate dashboard HTML file
  python3 enzo.py serve [--port 8077]     # Start live web dashboard server
  python3 enzo.py botctl / telegram       # Start Telegram interactive bot control listener
  python3 enzo.py learn                   # Display machine-learned insights and feature stats
  python3 enzo.py pause / resume          # Pause or resume trading operations
"""
import argparse
import json
import os
import sys
import time
import threading

# Ensure workspace root is in sys.path
WORKSPACE_ROOT = os.path.dirname(os.path.abspath(__file__))
if WORKSPACE_ROOT not in sys.path:
    sys.path.insert(0, WORKSPACE_ROOT)

from enzo.core import config, db, engine, learn
from enzo.execution import portfolio, exit_monitor
from enzo.ui import dashboard, serve, botctl


def cmd_start(args):
    """Unified master supervisor: starts web dashboard, Telegram bot, and trading engine."""
    db.init_db()
    port = args.port or 8077
    interval = args.interval or 60.0
    
    print("=" * 65)
    print("         ⚡ ENZO QUANT PROTOCOL — SYSTEM SUPERVISOR ⚡")
    print("=" * 65)
    print(f"  • Live Dashboard: http://0.0.0.0:{port}/enzo-dashboard.html")
    print(f"  • Telegram Bot:   Interactive Control Listener Active")
    print(f"  • Trading Engine: Autonomous Scan Loop ({interval}s interval)")
    print("=" * 65)

    # 1. Start Dashboard Web Server in daemon thread
    t_serve = threading.Thread(target=lambda: serve.run_server(host="0.0.0.0", port=port), daemon=True, name="enzo-serve")
    t_serve.start()

    # 2. Start Telegram Botctl Listener in daemon thread
    listener = botctl.get_telegram_listener()
    listener.start()

    # 3. Run Autonomous Trading Loop in main thread
    try:
        engine.run_loop(interval_sec=interval)
    except KeyboardInterrupt:
        print("\n[*] Shutting down ENZO Quantum Supervisor...")
        listener.stop()
        print("[✓] All subsystems stopped cleanly.")


def cmd_scan(args):
    db.init_db()
    if args.mint:
        print(f"[*] Scanning mint: {args.mint} ...")
        res = engine.scan_mint(args.mint)
        if res:
            print(json.dumps(res, indent=2, ensure_ascii=False))
        else:
            print(f"[!] Scan failed for {args.mint}")
    else:
        print("[*] Running full discovery and analysis scan cycle...")
        results = engine.scan_once()
        print(f"[✓] Completed scan cycle. Evaluated {len(results)} candidate tokens.")


def cmd_loop(args):
    db.init_db()
    interval = args.interval or 60.0
    print(f"[*] Starting ENZO continuous autonomous engine (interval: {interval}s)...")
    engine.run_loop(interval_sec=interval)


def cmd_status(args):
    db.init_db()
    st = portfolio.get_state()
    paused = botctl.is_paused()
    
    print("=" * 60)
    print("           ENZO PORTFOLIO & SYSTEM STATUS           ")
    print("=" * 60)
    print(f"  Mode:            {'PAPER TRADING' if config.load_config().get('paper_mode', True) else 'LIVE'}")
    print(f"  System State:    {'⏸ PAUSED' if paused else ('⚠ HALTED: ' + str(st.get('halted')) if st.get('halted') else '▶ ACTIVE')}")
    print(f"  Current Equity:  ${st.get('equity', 0):,.2f}")
    print(f"  Initial Capital: ${st.get('initial_capital', 0):,.2f}")
    print(f"  Realized PnL:    ${st.get('realized_pnl', 0):+,.2f}")
    print(f"  Open Positions:  {len(st.get('open_positions', {}))}")
    print(f"  Closed Trades:   {len(st.get('closed_positions', []))}")
    
    stats = st.get("stats", {})
    print(f"  Win Rate:        {stats.get('win_rate', 0.0):.1f}% ({stats.get('wins', 0)}W / {stats.get('losses', 0)}L)")
    print(f"  Daily Loss:      ${st.get('daily_loss', 0.0):+,.2f}")
    print(f"  Consecutive Loss:{st.get('consecutive_losses', 0)}")
    print("=" * 60)


def cmd_trades(args):
    db.init_db()
    st = portfolio.get_state()
    open_pos = st.get("open_positions", {})
    closed_pos = st.get("closed_positions", [])

    print(f"\n--- Open Positions ({len(open_pos)}) ---")
    if not open_pos:
        print("  No open positions.")
    else:
        for mint, p in open_pos.items():
            sym = p.get("symbol", "UNKNOWN")
            size = float(p.get("size_usd", 0))
            entry_mc = float(p.get("entry_market_cap", 0))
            upnl = float(p.get("unrealized_pnl", 0))
            print(f"  • [{sym}] {mint[:8]}... | Size: ${size:,.2f} | Entry MC: ${entry_mc:,.0f} | uPnL: ${upnl:+,.2f}")

    print(f"\n--- Closed Trades History ({len(closed_pos)}) ---")
    if not closed_pos:
        print("  No closed trades.")
    else:
        for c in closed_pos[-15:]:
            sym = c.get("symbol", "UNKNOWN")
            pnl = float(c.get("pnl", 0))
            pnl_pct = float(c.get("pnl_pct", 0))
            reason = c.get("reason", "CLOSED")
            closed_at = (c.get("closed_at") or "")[:16].replace("T", " ")
            print(f"  • [{sym}] PnL: ${pnl:+,.2f} ({pnl_pct:+,.1f}%) | Reason: {reason} | Closed: {closed_at}")
    print()


def cmd_dashboard(args):
    db.init_db()
    path = dashboard.generate()
    print(f"[✓] Dashboard regenerated: {path}")


def cmd_serve(args):
    db.init_db()
    port = args.port or 8077
    serve.run_server(host="0.0.0.0", port=port)


def cmd_botctl(args):
    db.init_db()
    print("[*] Starting ENZO Telegram Interactive Bot Listener...")
    listener = botctl.get_telegram_listener()
    listener.start()
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        listener.stop()
        print("\n[✓] Telegram bot listener stopped.")


def cmd_learn(args):
    st = learn.get_state()
    print("=" * 60)
    print("              ENZO MACHINE LEARNING STATE              ")
    print("=" * 60)
    print(learn.insights())
    print("-" * 60)
    print("Top Feature Win Rates:")
    for f in st.get("feature_win_rates", [])[:8]:
        print(f"  • {f.get('feature')}: {f.get('win_rate')}% (samples: {f.get('n')})")
    print("-" * 60)
    print("Axis Historical Performance:")
    for a in st.get("axis_win_rates", [])[:6]:
        print(f"  • {a.get('axis')}: Win Rate {a.get('win_rate')}% (Avg Score: {a.get('avg_score')})")
    print("=" * 60)


def cmd_pause(args):
    botctl.set_paused(True)
    print("[✓] ENZO trading paused.")


def cmd_resume(args):
    botctl.set_paused(False)
    print("[✓] ENZO trading resumed.")


def main():
    parser = argparse.ArgumentParser(description="ENZO - Memecoin Autonomous Trading Bot")
    subparsers = parser.add_subparsers(dest="command", help="Available commands")

    # Start master supervisor
    p_start = subparsers.add_parser("start", help="Start unified supervisor (serve + botctl + loop)")
    p_start.add_argument("--port", "-p", type=int, default=8077, help="Dashboard port (default: 8077)")
    p_start.add_argument("--interval", "-i", type=float, default=60.0, help="Scan interval (default: 60s)")
    p_start.set_defaults(func=cmd_start)

    # Scan command
    p_scan = subparsers.add_parser("scan", help="Scan a single mint or run discovery scan")
    p_scan.add_argument("mint", nargs="?", help="Specific token mint address (optional)")
    p_scan.set_defaults(func=cmd_scan)

    # Loop command
    p_loop = subparsers.add_parser("loop", help="Run continuous scan & trade loop")
    p_loop.add_argument("--interval", "-i", type=float, default=60.0, help="Interval in seconds between scan cycles")
    p_loop.set_defaults(func=cmd_loop)

    # Status command
    p_status = subparsers.add_parser("status", help="Display current portfolio and system status")
    p_status.set_defaults(func=cmd_status)

    # Trades command
    p_trades = subparsers.add_parser("trades", help="Display open positions and closed trades")
    p_trades.set_defaults(func=cmd_trades)

    # Dashboard command
    p_dash = subparsers.add_parser("dashboard", help="Regenerate dashboard HTML")
    p_dash.set_defaults(func=cmd_dashboard)

    # Serve command
    p_serve = subparsers.add_parser("serve", help="Start local web dashboard server")
    p_serve.add_argument("--port", "-p", type=int, default=8077, help="Server port (default: 8077)")
    p_serve.set_defaults(func=cmd_serve)

    # Botctl command
    p_botctl = subparsers.add_parser("botctl", aliases=["telegram"], help="Start interactive Telegram bot listener")
    p_botctl.set_defaults(func=cmd_botctl)

    # Learn command
    p_learn = subparsers.add_parser("learn", help="Show learning insights and signal stats")
    p_learn.set_defaults(func=cmd_learn)

    # Pause / Resume
    p_pause = subparsers.add_parser("pause", help="Pause trading")
    p_pause.set_defaults(func=cmd_pause)

    p_resume = subparsers.add_parser("resume", help="Resume trading")
    p_resume.set_defaults(func=cmd_resume)

    args = parser.parse_args()
    if hasattr(args, "func"):
        args.func(args)
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
