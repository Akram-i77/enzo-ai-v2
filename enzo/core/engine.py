#!/usr/bin/env python3
"""
ENZO - Autonomous Trading Engine
Executes discovery, pre-screening, depth-capped multi-axis deep analysis, position execution, and notification loops.
"""
import json
import os
import sys
import signal
import time
import threading
from datetime import datetime, timezone
from typing import List, Dict, Any, Optional

from enzo.core.config import load_config, WATCHLIST_PATH
import enzo.core.log as log
import enzo.core.audit as audit
import enzo.core.db as db
from enzo.providers import gmgn, pump
from enzo.analyzers import analyze
from enzo.execution import portfolio, exit_monitor, executor
from enzo.ui import notify, botctl, dashboard

_LOGGER = log.get_logger("enzo.engine")

# Global scan lock: serializes the main loop, the Telegram scan button and the
# POST /api/scan trigger so concurrent cycles can never double-scan / double-open.
SCAN_LOCK = threading.Lock()


def load_watchlist() -> List[str]:
    """Read the operator watchlist.

    The shipped file uses the key "watchlist", but this function only ever read
    "mints" — so every mint the operator added was silently discarded and the
    highest-priority candidate source contributed nothing, ever. All three
    spellings are now accepted, and a malformed file is reported instead of
    swallowed.
    """
    if not os.path.exists(WATCHLIST_PATH):
        return []
    try:
        with open(WATCHLIST_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception as e:
        _LOGGER.error("Watchlist %s is not valid JSON (%s) — ignored", WATCHLIST_PATH, e)
        try:
            audit.log_event(category="SYSTEM", level="ERROR",
                            message=f"Watchlist unreadable: {e}",
                            data={"path": WATCHLIST_PATH})
        except Exception:
            pass
        return []

    items: Any = data
    if isinstance(data, dict):
        for key in ("watchlist", "mints", "tokens", "addresses", "items"):
            if isinstance(data.get(key), list):
                items = data[key]
                break
        else:
            _LOGGER.error("Watchlist %s has no recognised key (looked for watchlist/mints/tokens); got keys=%s",
                          WATCHLIST_PATH, sorted(data.keys()))
            return []

    out: List[str] = []
    for it in items:
        if isinstance(it, str):
            m = it.strip()
        elif isinstance(it, dict):
            m = str(it.get("mint") or it.get("address") or it.get("token") or "").strip()
        else:
            m = ""
        if m and m not in out:
            out.append(m)
    if out:
        _LOGGER.info("Watchlist loaded: %d mint(s) from %s", len(out), os.path.basename(WATCHLIST_PATH))
    return out


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
            mint_address = decision.get("mint_address") or decision.get("mint") or mint

            # ── LIVE ONLY: tradability gate BEFORE committing to a position ──
            # MoonPay routes swaps through swaps.xyz, so a pump.fun token still
            # on its bonding curve has no route at all. Checking first avoids
            # opening a ledger position that must then be rolled back, and the
            # cooldown cache stops the bot re-analysing the same dead mint every
            # cycle (which is what burned the rate-limit budget and produced the
            # "only accepts trending coins" symptom).
            if not paper:
                try:
                    prospective = portfolio.prospective_size(decision, cfg)
                    gate_amount = float(prospective.get("size_usd") or 0.0)
                except Exception as e:
                    _LOGGER.warning("prospective size failed (%s) — gating on min_trade_usd", e)
                    gate_amount = float((cfg.get("execution", {}) or {}).get("min_trade_usd", 1.0))
                gate = executor.check_tradable(mint_address, gate_amount, cfg=cfg)
                if not gate.get("tradable"):
                    reason = gate.get("reason", "NOT_TRADABLE")
                    detail = gate.get("detail", "")
                    _LOGGER.warning("BUY suppressed for %s — %s: %s",
                                    decision.get("token_symbol", mint_address[:8]), reason, detail)
                    audit.log_event(
                        category="TRADE", level="WARNING",
                        message=f"BUY suppressed (not routable via MoonPay): "
                                f"{decision.get('token_symbol')} [{reason}]",
                        data={"mint": mint_address, "reason": reason, "detail": detail[:300],
                              "confidence": conf_score},
                    )
                    try:
                        notify.notify_buy_failed(decision, reason=detail or reason,
                                                 paper=False, reason_code=reason)
                    except Exception as e:
                        _LOGGER.error("notify_buy_failed raised: %s", e)
                    return {**decision, "decision": "NOT_TRADABLE",
                            "failure_reason": reason, "failure_detail": detail}

            open_res = portfolio.open_position(decision, cfg)
            _LOGGER.info(f"Open position result: {open_res}")
            if open_res.get("ok"):
                # ── LIVE TRADING: execute real swap via MoonPay CLI ──
                if not paper:
                    ready, ready_msg = executor.is_ready(cfg)
                    if not ready:
                        _LOGGER.warning(f"Executor not ready — rolling back position: {ready_msg}")
                        _rollback_position(decision, cfg, "EXEC_NOT_READY")
                        try:
                            # This call used to be guarded by
                            # `if hasattr(notify, "notify_buy_failed")` — the
                            # function did not exist, so the guard was always
                            # False and live buy failures were NEVER reported.
                            notify.notify_buy_failed(decision, reason=ready_msg,
                                                     paper=False, reason_code="EXEC_NOT_READY")
                        except Exception as e:
                            _LOGGER.error("notify_buy_failed raised: %s", e)
                        return decision

                    # Get position size (calculated by portfolio.open_position)
                    pos = open_res.get("position", {})
                    size_usd = float(pos.get("size_usd", 0))
                    entry_price = float(decision.get("entry_price") or pos.get("entry_price", 0))

                    if size_usd <= 0 or not mint_address:
                        _LOGGER.error(f"Invalid position data — rolling back")
                        _rollback_position(decision, cfg, "INVALID_POSITION")
                        try:
                            notify.notify_buy_failed(decision, reason="position size or mint missing",
                                                     paper=False, reason_code="INVALID_POSITION")
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
                        detail = swap_res.get("detail", "")
                        code = swap_res.get("reason_code", "")
                        _LOGGER.error(f"Swap failed — rolling back position: {reason}")
                        _rollback_position(decision, cfg, f"SWAP_FAILED:{reason[:40]}")
                        audit.log_event(
                            category="TRADE", level="ERROR",
                            message=f"BUY FAILED — {decision.get('token_symbol')}: {reason}",
                            data={"mint": mint_address, "size_usd": size_usd,
                                  "reason_code": code, "detail": str(detail)[:300],
                                  "swap_result": {k: v for k, v in swap_res.items()
                                                  if k not in ("raw",)}},
                        )
                        # notify() only formats BUY/WAIT/exit signals — it has no
                        # failed-buy template, so this alert used to arrive as a
                        # generic message with no actionable detail.
                        try:
                            notify.notify_buy_failed(decision, reason=detail or reason,
                                                     paper=False, reason_code=code or reason)
                        except Exception as e:
                            _LOGGER.error("notify_buy_failed raised: %s", e)
                        return decision
                    else:
                        tx_hash = swap_res.get("tx_hash", "")
                        _LOGGER.info(f"✓ REAL SWAP EXECUTED — tx: {tx_hash or 'pending'}")
                        audit.log_event(
                            category="TRADE", level="BUY",
                            message=f"REAL BUY: {decision.get('token_symbol')} @ ${size_usd:.2f} tx={tx_hash or 'pending'} conf={conf_score:.0f}",
                            data={"mint": mint_address, "tx": tx_hash,
                                  "amount_usd": size_usd,
                                  "swap": {k: v for k, v in swap_res.items()
                                           if k not in ("raw",)}},
                        )
                        if tx_hash:
                            try:
                                db.atomic_update_position_extra(mint_address, {"tx_hash": tx_hash})
                            except Exception:
                                _LOGGER.debug("could not attach tx_hash to position")
                
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


def _rollback_position(decision: dict, cfg: dict, reason: str) -> None:
    """Close a ledger position that was opened but never actually executed.

    Centralised because the previous inline copies used different mint keys
    (mint_address vs token_symbol) and swallowed every failure, which could
    leave a phantom open position in the ledger."""
    mint = decision.get("mint_address") or decision.get("mint") or decision.get("token_symbol") or ""
    if not mint:
        _LOGGER.error("cannot roll back position — no mint in decision")
        return
    entry_mc = float(decision.get("entry_market_cap") or decision.get("market_cap_usd") or 0.0)
    try:
        portfolio.close_position(portfolio.load_state(), mint, entry_mc, reason)
    except Exception as e:
        _LOGGER.error("rollback failed for %s (%s): %s", mint[:8], reason, e)


_LAST_DISCOVERY_FAULTS: Dict[str, str] = {}


def _discovery_fault(source: str, err: Exception) -> None:
    """Remember a discovery failure so the dashboard can surface it and the
    operator can tell 'no tokens available' from 'the data source is broken'."""
    msg = f"{type(err).__name__}: {err}"
    changed = _LAST_DISCOVERY_FAULTS.get(source) != msg
    _LAST_DISCOVERY_FAULTS[source] = msg[:300]
    if changed:
        try:
            audit.log_event(category="DISCOVERY", level="WARNING",
                            message=f"{source} discovery failed — {msg[:200]}",
                            data={"source": source, "error": msg[:500]})
        except Exception:
            pass


def discovery_faults() -> Dict[str, str]:
    return dict(_LAST_DISCOVERY_FAULTS)


def _heartbeat(status: str = None, candidates: int = None, err: str = None,
               interval: float = None) -> None:
    """Publish liveness so the dashboard and /health can tell a running engine
    from a dead one. Without this the UI had no way to detect that the engine
    had stopped — it simply kept serving the last HTML forever.

    Maps onto serve.beat(status=, candidates=, interval=). A non-None `err` is
    folded into the status string so /health surfaces it.
    """
    try:
        from enzo.ui import serve as _serve
        st = status
        if err:
            st = f"{status or 'error'}: {str(err)[:160]}"
        _serve.beat(status=st, candidates=candidates, interval=interval)
    except Exception as e:
        _LOGGER.debug("heartbeat publish failed: %s", e)


def _render_dashboard() -> None:
    """Render the dashboard, reporting failures instead of swallowing them."""
    try:
        res = dashboard.generate_safe() or {}
        if not res.get("ok"):
            _LOGGER.error("Dashboard render failed: %s", res.get("error"))
            audit.log_event(category="SYSTEM", level="ERROR",
                            message=f"Dashboard render failed: {str(res.get('error'))[:200]}",
                            data={})
    except Exception as e:
        _LOGGER.error("Dashboard render raised: %s", e)


def _scan_once_unlocked(watchlist: List[str] = None) -> List[dict]:
    """Lock-free core of scan_once — called under SCAN_LOCK."""
    if botctl.is_paused():
        _LOGGER.info("ENZO is paused via botctl — skipping scan.")
        _heartbeat(status="paused")
        _render_dashboard()
        return []

    cfg = load_config()
    paper = bool(cfg.get("paper_mode", True))
    _heartbeat(status="scanning")

    # Refresh deployable capital from the real wallet before sizing anything.
    # In live+wallet mode this is what position sizing is based on; in paper
    # mode it is a no-op that keeps the ledger numbers.
    try:
        cap = portfolio.sync_capital_base(cfg=cfg, rebase=True)
        if cap.get("source") == "wallet":
            _LOGGER.info("Deployable capital: $%s (live wallet)", f"{float(cap.get('usd') or 0):,.2f}")
        elif not cap.get("ok"):
            _LOGGER.warning("Deployable capital falling back to ledger: %s", cap.get("detail"))
    except Exception as e:
        _LOGGER.warning("Capital sync skipped (%s) — sizing will use ledger cash", e)

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
        # Was _LOGGER.debug(): the failure that caused "Discovered 0 candidates"
        # for the bot's entire life was written below the console/log threshold
        # (INFO), so it was never seen by the operator.
        _LOGGER.warning("PumpDev discovery failed (%s) — %s", type(e).__name__, e)
        _discovery_fault("pumpdev", e)

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
        _LOGGER.warning("GMGN discovery failed (%s) — %s", type(e).__name__, e)
        _discovery_fault("gmgn", e)

    total_discovered = len(candidates)
    _LOGGER.info(f"Discovered {total_discovered} pre-screened candidate tokens.")

    if not candidates:
        gstat = {}
        try:
            gstat = gmgn.discovery_status()
        except Exception:
            pass
        cats = gstat.get("categories_ok") or {}
        failed_cats = [c for c, v in cats.items() if not v.get("ok")]
        if failed_cats:
            _LOGGER.error(
                "No candidates: GMGN categories %s FAILED — %s",
                failed_cats, str(gstat.get("last_error"))[:160])
        elif gstat.get("consecutive_empty"):
            _LOGGER.warning(
                "No candidates: GMGN answered but returned 0 tokens for %s cycle(s) in a row",
                gstat.get("consecutive_empty"))
        else:
            _LOGGER.warning(
                "No candidates this cycle. Discovery faults: %s",
                _LAST_DISCOVERY_FAULTS or "none (sources responded but returned nothing)",
            )
        _errs = [f"{k}: {v}" for k, v in _LAST_DISCOVERY_FAULTS.items()]
        if failed_cats:
            _errs.append(f"gmgn categories failed: {','.join(failed_cats)}")
        _heartbeat(status="idle", candidates=0, err="; ".join(_errs) or None)
        _render_dashboard()
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

    _heartbeat(status="completed", candidates=total_discovered,
               interval=float((load_config().get("engine", {}) or {}).get("scan_interval_sec") or 60.0))
    _render_dashboard()

    return results


def run_loop(interval_sec: float = 60.0):
    """Run the continuous autonomous trading engine.

    Supervisor-aware: writes a PID file and a heartbeat so an external watcher
    (OpenClaw) can tell a live engine from a dead one, and shuts the exit monitor
    down cleanly on SIGTERM instead of leaving positions unmonitored.
    """
    from enzo.core.config import PID_PATH, RUN_DIR

    _LOGGER.info(f"Starting ENZO Engine loop (interval: {interval_sec}s)...")
    _LOGGER.info("Mode: %s", "PAPER" if bool(load_config().get("paper_mode", True)) else "LIVE")

    try:
        os.makedirs(RUN_DIR, exist_ok=True)
        with open(PID_PATH, "w", encoding="utf-8") as f:
            f.write(str(os.getpid()))
    except Exception as e:
        _LOGGER.warning("could not write PID file %s: %s", PID_PATH, e)

    # Start background exit monitor
    monitor = exit_monitor.get_exit_monitor()
    monitor.start()

    # Publish the feed state once immediately so status pages have something
    # truthful before the first cycle finishes.
    try:
        from enzo.providers import pump as _pump_pub
        _pump_pub.publish_status()
    except Exception:
        pass

    stopping = {"flag": False}

    def _handle_term(signum, frame):
        stopping["flag"] = True
        _LOGGER.info("Received signal %s — shutting down engine", signum)

    for sig_name in ("SIGTERM", "SIGINT"):
        sig = getattr(signal, sig_name, None)
        if sig is not None:
            try:
                signal.signal(sig, _handle_term)
            except Exception:
                pass

    cycles = 0
    try:
        while not stopping["flag"]:
            t0 = time.time()
            cycles += 1
            try:
                scan_once()
            except Exception as e:
                # A single broken cycle must not kill the engine, but it must
                # not be silent either.
                _LOGGER.error("Scan cycle %d raised %s: %s", cycles, type(e).__name__, e,
                              exc_info=True)
                _heartbeat(status="error", err=f"{type(e).__name__}: {e}")
                try:
                    audit.log_event(category="SYSTEM", level="ERROR",
                                    message=f"Scan cycle crashed: {type(e).__name__}: {e}",
                                    data={"cycle": cycles})
                except Exception:
                    pass
            try:
                from enzo.providers import pump as _pump_pub
                _pump_pub.publish_status()
            except Exception:
                pass
            elapsed = time.time() - t0
            sleep_time = max(1.0, interval_sec - elapsed)
            # Sleep in small slices so SIGTERM is honoured promptly.
            deadline = time.time() + sleep_time
            while not stopping["flag"] and time.time() < deadline:
                time.sleep(min(1.0, max(0.0, deadline - time.time())))
    except KeyboardInterrupt:
        _LOGGER.info("Stopping ENZO Engine loop...")
    finally:
        _LOGGER.info("Stopping exit monitor (engine ran %d cycles)", cycles)
        try:
            monitor.stop()
        except Exception as e:
            _LOGGER.warning("exit monitor stop failed: %s", e)
        _heartbeat(status="stopped")
        try:
            if os.path.exists(PID_PATH):
                with open(PID_PATH, "r", encoding="utf-8") as f:
                    if f.read().strip() == str(os.getpid()):
                        os.remove(PID_PATH)
        except Exception:
            pass
