#!/usr/bin/env python3
"""
ENZO - Autonomous Trading Engine
Executes discovery, pre-screening, depth-capped multi-axis deep analysis, position execution, and notification loops.
"""
import json
import os
import sys
import time
import threading
from datetime import datetime, timezone
from typing import List, Dict, Any, Optional

from enzo.core.config import load_config, WATCHLIST_PATH
import enzo.core.log as log
import enzo.core.audit as audit
from enzo.providers import gmgn, pump
from enzo.analyzers import analyze
from enzo.execution import portfolio, exit_monitor, executor
from enzo.ui import notify, botctl, dashboard

_LOGGER = log.get_logger("enzo.engine")

# Global scan lock: serializes the main loop, the Telegram scan button and the
# POST /api/scan trigger so concurrent cycles can never double-scan / double-open.
SCAN_LOCK = threading.Lock()


def load_watchlist() -> List[str]:
    if not os.path.exists(WATCHLIST_PATH):
        return []
    try:
        with open(WATCHLIST_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
            if isinstance(data, list):
                return data
            elif isinstance(data, dict):
                return data.get("mints", [])
    except Exception:
        pass
    return []


def scan_mint(mint: str, pump_card: dict = None) -> Optional[dict]:
    """Analyze a single mint through the full pipeline."""
    try:
        decision = analyze.run_pipeline(mint, pump_card=pump_card)
        cfg = load_config()
        paper = bool(cfg.get("paper_mode", True))

        dec_str = decision.get("decision", "IGNORE")
        conf_score = decision.get("confidence_score", 0)
        _LOGGER.info(f"Scanned {decision.get('token_symbol', mint[:8])} ({mint[:8]}...) -> {dec_str} (conf={conf_score})")

        if dec_str == "BUY":
            open_res = portfolio.open_position(decision, cfg)
            _LOGGER.info(f"Open position result: {open_res}")
            if open_res.get("ok"):
                # ── LIVE TRADING: execute real swap via MoonPay CLI ──
                if not paper:
                    ready, ready_msg = executor.is_ready(cfg)
                    if not ready:
                        _LOGGER.warning(f"Executor not ready — rolling back position: {ready_msg}")
                        try:
                            entry_mc = decision.get("entry_market_cap") or decision.get("market_cap_usd") or 0
                            portfolio.close_position(
                                portfolio.load_state(),
                                decision.get("mint_address") or decision.get("token_symbol"),
                                float(entry_mc),
                                "EXEC_NOT_READY",
                            )
                        except Exception:
                            pass
                        _LOGGER.error(f"Position rolled back: executor not ready")
                        notify. notify_buy_failed(
                            decision, reason=ready_msg, paper=False
                        ) if hasattr(notify, "notify_buy_failed") else None
                        return decision

                    # Get position size (calculated by portfolio.open_position)
                    pos = open_res.get("position", {})
                    size_usd = float(pos.get("size_usd", 0))
                    entry_price = float(decision.get("entry_price") or pos.get("entry_price", 0))
                    mint_address = decision.get("mint_address") or decision.get("token_symbol")

                    if size_usd <= 0 or not mint_address:
                        _LOGGER.error(f"Invalid position data — rolling back")
                        try:
                            entry_mc = decision.get("entry_market_cap") or 0
                            portfolio.close_position(
                                portfolio.load_state(), mint_address, float(entry_mc), "INVALID_POSITION"
                            )
                        except Exception:
                            pass
                        return decision

                    _LOGGER.info(f"Executing REAL swap: ${size_usd:.2f} into {mint_address[:8]}...")
                    swap_res = executor.buy_token(
                        mint=mint_address,
                        amount_usd=size_usd,
                        entry_price=entry_price,
                        explanation=f"Enzo BUY {decision.get('token_symbol', mint_address[:8])} conf={conf_score:.0f}",
                    )
                    if not swap_res.get("ok"):
                        reason = swap_res.get("reason", "unknown")
                        _LOGGER.error(f"Swap failed — rolling back position: {reason}")
                        try:
                            entry_mc = decision.get("entry_market_cap") or 0
                            portfolio.close_position(
                                portfolio.load_state(),
                                mint_address,
                                float(entry_mc),
                                f"SWAP_FAILED:{reason[:40]}",
                            )
                        except Exception:
                            pass
                        audit.log_event(
                            category="TRADE", level="ERROR",
                            message=f"BUY FAILED — {decision.get('token_symbol')}: {reason}",
                            data={"mint": mint_address, "swap_result": swap_res},
                        )
                        notify.notify(
                            {**decision, "decision": "BUY_FAILED", "failure_reason": reason},
                            paper=False, console=True,
                        )
                        return decision
                    else:
                        tx_hash = swap_res.get("tx_hash", "")
                        _LOGGER.info(f"✓ REAL SWAP EXECUTED — tx: {tx_hash or 'pending'}")
                        audit.log_event(
                            category="TRADE", level="BUY",
                            message=f"REAL BUY: {decision.get('token_symbol')} @ ${size_usd:.2f} tx={tx_hash or 'pending'} conf={conf_score:.0f}",
                            data={"mint": mint_address, "tx": tx_hash, "swap": swap_res},
                        )
                
                notify.notify(decision, paper=paper, console=True)
                if paper:
                    audit.log_event(
                        category="TRADE", level="BUY",
                        message=f"Opened position: {decision.get('token_symbol')} @ ${float(decision.get('entry_market_cap') or 0):,.0f} conf={conf_score:.0f}",
                        data={"mint": mint, "confidence": conf_score}
                    )
            else:
                reason = open_res.get("reason", "")
                _LOGGER.info(f"BUY skipped for {decision.get('token_symbol')}: {reason}")
                if str(reason).startswith("HALTED"):
                    try:
                        notify.notify_risk("Trading Halted", str(reason))
                    except Exception:
                        pass
        elif dec_str == "WAIT" and conf_score >= float((cfg.get("notifications") or {}).get("min_confidence_for_notification", 60)):
            notify.notify(decision, paper=paper, console=True)

        # Live decision audit trail (feeds /api/activity)
        try:
            audit.record(decision)
        except Exception:
            pass

        return decision
    except Exception as e:
        _LOGGER.error(f"Error scanning mint {mint}: {e}")
        return {"decision": "ANALYSIS_ERROR", "mint_address": mint, "reason": str(e)}


def scan_once(watchlist: List[str] = None) -> List[dict]:
    """Execute a single discovery, ranking, depth-capped scan cycle.

    Serialized by SCAN_LOCK: the main loop, Telegram buttons and the web
    dashboard's POST /api/scan all funnel through here.
    """
    with SCAN_LOCK:
        return _scan_once_unlocked(watchlist)


def _scan_once_unlocked(watchlist: List[str] = None) -> List[dict]:
    """Lock-free core of scan_once — called under SCAN_LOCK."""
    if botctl.is_paused():
        _LOGGER.info("ENZO is paused via botctl — skipping scan.")
        try:
            dashboard.generate()
        except Exception:
            pass
        return []

    cfg = load_config()
    candidates: Dict[str, dict] = {}

    # 1. Watchlist candidates (highest priority)
    wl = watchlist or load_watchlist()
    for m in wl:
        candidates[m] = {"mint": m, "source": "watchlist", "rank_score": 1000.0}

    # 2. PumpDev Real-Time launches (high priority)
    try:
        recent_pump = pump.get_recent_creations(limit=40)
        for p in recent_pump:
            card_screen = pump.screen_pump_card(p, cfg)
            mint = card_screen.get("mint")
            if mint and card_screen.get("pass") and mint not in candidates:
                mcap = float(card_screen.get("market_cap") or 0.0)
                candidates[mint] = {
                    "mint": mint,
                    "source": "pumpdev",
                    "card": p,
                    "rank_score": min(mcap / 1000.0, 100.0) + 50.0
                }
    except Exception as e:
        _LOGGER.debug(f"PumpDev discovery error: {e}")

    # 3. GMGN multi-category discovery (trenches + trending + smartmoney)
    try:
        discovered = gmgn.discover()
        for d in discovered:
            mint = d.get("mint") or d.get("address")
            if mint and mint not in candidates:
                screen = gmgn.list_screen(d, cfg)
                if screen.get("pass"):
                    mcap = float(d.get("market_cap") or 0.0)
                    vol = float(d.get("volume_24h") or 0.0)
                    candidates[mint] = {
                        "mint": mint,
                        "source": f"gmgn_{d.get('source', 'sweep')}",
                        "data": d,
                        "rank_score": min(mcap / 1000.0, 50.0) + min(vol / 100.0, 50.0)
                    }
    except Exception as e:
        _LOGGER.debug(f"GMGN discovery error: {e}")

    total_discovered = len(candidates)
    _LOGGER.info(f"Discovered {total_discovered} pre-screened candidate tokens.")

    if not candidates:
        try:
            dashboard.generate()
        except Exception:
            pass
        return []

    # Depth limiting: sort by rank_score and take top N
    disc_cfg = cfg.get("discovery", {}) or {}
    gmgn_cfg = (cfg.get("data_sources", {}) or {}).get("gmgn", {})
    max_depth = int(disc_cfg.get("max_depth_tokens_per_cycle") or gmgn_cfg.get("max_depth_analyses") or 12)

    sorted_candidates = sorted(candidates.values(), key=lambda c: c.get("rank_score", 0), reverse=True)
    top_candidates = sorted_candidates[:max_depth]

    _LOGGER.info(f"Proceeding to deep 6-axis analysis on top {len(top_candidates)} / {total_discovered} candidates (depth cap: {max_depth}).")

    results = []
    for cand in top_candidates:
        if botctl.is_paused():
            break
        mint = cand["mint"]
        card = cand.get("card")
        res = scan_mint(mint, pump_card=card)
        if res:
            results.append(res)
        time.sleep(1.0)  # Safe rate pacing between deep scans

    try:
        dashboard.generate()
    except Exception:
        pass

    return results


def run_loop(interval_sec: float = 60.0):
    """Run continuous autonomous trading engine."""
    _LOGGER.info(f"Starting ENZO Engine loop (interval: {interval_sec}s)...")
    
    # Start background exit monitor
    monitor = exit_monitor.get_exit_monitor()
    monitor.start()

    try:
        while True:
            t0 = time.time()
            scan_once()
            elapsed = time.time() - t0
            sleep_time = max(1.0, interval_sec - elapsed)
            time.sleep(sleep_time)
    except KeyboardInterrupt:
        _LOGGER.info("Stopping ENZO Engine loop...")
        monitor.stop()
