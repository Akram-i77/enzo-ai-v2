#!/usr/bin/env python3
"""
ENZO - Autonomous Trading Engine
Executes discovery, pre-screening, depth-capped multi-axis deep analysis, position execution, and notification loops.
"""
import json
import os
import sys
import signal
import collections
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


# ── GMGN request budget ──────────────────────────────────────────────────────
# A ban from GMGN is a RATE-LIMIT ban, and its docs are explicit: every request
# sent during the cooldown extends it by 5 seconds. So the only real cure is to
# send fewer requests. Three knobs that were already in the shipped config were
# never read by any code (they sat in test_config_wiring's frozen dead-key list):
#   data_sources.gmgn.max_candidates_per_scan
#   pump_monitor.max_analyses_per_min
#   pump_monitor.min_analysis_interval_sec
# They are wired here, plus one new knob for the biggest cut of all: a coin that
# was just examined and rejected does not need to be examined again next cycle -
# and "next cycle" used to be every 60 seconds, forever, for the same trending
# coins (5 GMGN calls each).
_ANALYSIS_TIMES = collections.deque()
_LAST_CYCLE_STATS = {"analysed": 0, "skipped_cooldown": 0, "skipped_budget": 0,
                     "candidates": 0, "candidates_after_cap": 0, "gmgn_calls": 0,
                     "seconds": 0.0, "ts": 0.0}


def duty_cycle_advice(elapsed: float, interval_sec: float) -> str:
    """'' or a warning: a cycle longer than the interval means the engine never
    idles, so `sleep_time = max(1.0, interval - elapsed)` collapses to 1s and GMGN
    sees one uninterrupted request stream. That is the shape of the traffic that
    earned RATE_LIMIT_BANNED, and it is invisible unless it is said out loud."""
    try:
        elapsed = float(elapsed or 0.0)
        interval_sec = float(interval_sec or 0.0)
    except Exception:
        return ""
    if elapsed <= interval_sec or interval_sec <= 0:
        return ""
    return (f"Scan cycle took {elapsed:.0f}s but the interval is {interval_sec:.0f}s — "
            f"the engine never idles, so GMGN sees a continuous request stream "
            f"({float(_LAST_CYCLE_STATS.get('gmgn_calls') or 0):.0f} requests last "
            f"cycle, ~{float(_LAST_CYCLE_STATS.get('gmgn_calls') or 0) * 60.0 / max(1.0, elapsed):.0f}/min). "
            f"Lower pump_monitor.max_analyses_per_min or "
            f"data_sources.gmgn.max_depth_analyses, raise engine.scan_interval_sec, "
            f"or upgrade the GMGN plan.")


def cycle_stats() -> dict:
    """What the last scan cycle actually cost, in GMGN requests."""
    return dict(_LAST_CYCLE_STATS)


def volume_caps(cfg: dict) -> tuple:
    """(candidate_cap, depth_cap) actually in force — the TIGHTEST of the knobs
    that configure the same thing.

    Two pairs of keys control the same limits and used to disagree silently:
      discovery.max_tokens_per_scan      vs data_sources.gmgn.max_candidates_per_scan
      discovery.max_depth_tokens_per_cycle vs data_sources.gmgn.max_depth_analyses
    The depth pair was resolved with `a or b`, so `discovery.max_depth_tokens_per_cycle`
    (12) always won and lowering `max_depth_analyses` changed NOTHING - while the
    log line still printed the winning 12 and the config looked tightened. Each
    deep analysis costs ~5 GMGN requests, so that silent precedence is the
    difference between ~30 and ~60 requests per cycle. Tightest wins, and the
    effective number is what gets logged.
    """
    disc = (cfg or {}).get("discovery") or {}
    g = ((cfg or {}).get("data_sources") or {}).get("gmgn") or {}

    def _tightest(*vals, default):
        got = []
        for v in vals:
            try:
                v = int(v or 0)
            except Exception:
                v = 0
            if v > 0:
                got.append(v)
        return min(got) if got else default

    return (_tightest(disc.get("max_tokens_per_scan"), g.get("max_candidates_per_scan"),
                      default=40),
            _tightest(disc.get("max_depth_tokens_per_cycle"), g.get("max_depth_analyses"),
                      default=12))


def _budget_cfg(cfg: dict) -> tuple:
    pm = cfg.get("pump_monitor") or {}
    g = (cfg.get("data_sources") or {}).get("gmgn") or {}
    return (int(pm.get("max_analyses_per_min", 6) or 0),
            float(pm.get("min_analysis_interval_sec", 45) or 0),
            float(g.get("reanalysis_cooldown_sec", 900) or 0))


def _budget_left(per_min: int) -> int:
    if per_min <= 0:
        return 10 ** 9
    now = time.time()
    while _ANALYSIS_TIMES and now - _ANALYSIS_TIMES[0] > 60.0:
        _ANALYSIS_TIMES.popleft()
    return max(0, per_min - len(_ANALYSIS_TIMES))


# Which outcomes are TERMINAL, i.e. worth keeping out of the deep path for the
# whole reanalysis_cooldown_sec window. Deliberately narrow, because this decides
# what a real-money bot is allowed to look at again:
#   IGNORE        - DANGEROUS security status / rug fingerprint: it will not
#                   become buyable in 15 minutes.
#   NOT_TRADABLE  - failed a hard gate (universe/rug/holder concentration).
# Everything else stays re-examinable after min_analysis_interval_sec only:
#   WAIT          - confidence below threshold; this is exactly the coin that may
#                   turn into a BUY two minutes later, and freezing it for 15
#                   minutes would cost entries.
#   BUY           - a position is open; the exit monitor owns it from here.
#   DATA_ERROR / ANALYSIS_ERROR / unknown - a broken data source must not freeze a
#                   coin (during a GMGN ban every coin would be frozen for 15 min).
TERMINAL_DECISIONS = ("IGNORE", "NOT_TRADABLE")


def _cooldown_left(mint: str, min_gap: float, cooldown: float) -> tuple:
    """(seconds_left, reason) for re-examining this mint, from the shared cache
    so it survives a restart (a fresh process must not re-burn the budget)."""
    if min_gap <= 0 and cooldown <= 0:
        return 0.0, ""
    try:
        val, age, _ttl = db.cache_get(f"enzoscan:{mint}")
    except Exception:
        return 0.0, ""
    if age is None:
        return 0.0, ""
    decision = str((val or {}).get("decision") or "").upper()
    # Both windows are evaluated and the BINDING (longer) one is reported: checking
    # min_gap first used to hide the 900s freeze behind a 45s answer, so the log
    # and the skip counter understated how long a rejected coin was out.
    left_gap = (min_gap - age) if (min_gap > 0 and age < min_gap) else 0.0
    left_cool = ((cooldown - age)
                 if (cooldown > 0 and decision in TERMINAL_DECISIONS and age < cooldown)
                 else 0.0)
    if left_cool >= left_gap and left_cool > 0:
        return left_cool, (f"reanalysis_cooldown_sec={cooldown:.0f}s after "
                           f"a terminal {decision}")
    if left_gap > 0:
        return left_gap, f"min_analysis_interval_sec={min_gap:.0f}s"
    return 0.0, ""


def _remember_analysis(mint: str, res: dict) -> None:
    """Stamp this mint only when the analysis actually produced a decision."""
    if not res:
        return
    try:
        db.cache_set(f"enzoscan:{mint}",
                     {"decision": str((res or {}).get("decision") or ""),
                      "ts": time.time()}, ttl=86400.0)
    except Exception:
        pass


def scan_once(watchlist: List[str] = None, force: bool = False) -> List[dict]:
    """Execute a single discovery, ranking, depth-capped scan cycle.

    Serialized by SCAN_LOCK: the main loop, Telegram buttons and the web
    dashboard's POST /api/scan all funnel through here.
    """
    with SCAN_LOCK:
        return _scan_once_unlocked(watchlist, force=force)


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


def _scan_once_unlocked(watchlist: List[str] = None, force: bool = False) -> List[dict]:
    """Lock-free core of scan_once — called under SCAN_LOCK.

    `force` is the operator's `./enzoctl scan --force`: it bypasses the
    re-analysis cooldown and the per-minute budget (an explicit human request),
    but still counts every call so the volume stays visible.
    """
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
    # max_candidates_per_scan was configured (40) and read by nothing. It caps
    # how many discovered tokens are even considered, which bounds the deep calls.
    _max_cand, _depth_cap = volume_caps(cfg)
    if _max_cand > 0 and len(candidates) > _max_cand:
        _kept = dict(sorted(candidates.items(),
                            key=lambda kv: float(kv[1].get("rank_score") or 0.0),
                            reverse=True)[:_max_cand])
        _LOGGER.info("candidate cap %d (tightest of discovery.max_tokens_per_scan and "
                     "data_sources.gmgn.max_candidates_per_scan): keeping the top %d of %d",
                     _max_cand, len(_kept), len(candidates))
        candidates = _kept
    _LAST_CYCLE_STATS["candidates_after_cap"] = len(candidates)
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
    max_depth = _depth_cap

    sorted_candidates = sorted(candidates.values(), key=lambda c: c.get("rank_score", 0), reverse=True)
    top_candidates = sorted_candidates[:max_depth]

    _LOGGER.info(
        "Proceeding to deep 6-axis analysis on top %d / %d candidates "
        "(effective depth cap: %d = the tightest of discovery.max_depth_tokens_per_cycle "
        "and data_sources.gmgn.max_depth_analyses; ~5 GMGN requests each).",
        len(top_candidates), total_discovered, max_depth)

    _per_min, _min_gap, _cooldown = _budget_cfg(cfg)
    _calls_before = 0
    try:
        _calls_before = int((gmgn.call_stats() or {}).get("total") or 0)
    except Exception:
        _calls_before = 0

    results = []
    analysed = skipped_cool = skipped_budget = 0
    _skip_examples = []          # so the log can answer "why was X not scanned?"
    _t_loop = time.time()
    for cand in top_candidates:
        if botctl.is_paused():
            break
        mint = cand["mint"]
        card = cand.get("card")
        if not force:
            left, why = _cooldown_left(mint, _min_gap, _cooldown)
            if left > 0:
                skipped_cool += 1
                if len(_skip_examples) < 3:
                    _skip_examples.append(f"{str(mint)[:8]}… {left:.0f}s left ({why})")
                continue
            if _budget_left(_per_min) <= 0:
                skipped_budget += 1
                _LOGGER.info(
                    "max_analyses_per_min=%d reached — stopping this cycle with %d "
                    "candidate(s) unexamined. Each deep analysis costs ~5 GMGN "
                    "requests, and requests are what get the key banned.",
                    _per_min, len(top_candidates) - analysed - skipped_cool)
                break
        res = scan_mint(mint, pump_card=card)
        analysed += 1
        _ANALYSIS_TIMES.append(time.time())
        _remember_analysis(mint, res)
        if res:
            results.append(res)
        time.sleep(1.0)  # Safe rate pacing between deep scans

    try:
        _calls_after = int((gmgn.call_stats() or {}).get("total") or 0)
    except Exception:
        _calls_after = _calls_before
    _LAST_CYCLE_STATS.update({
        "analysed": analysed, "skipped_cooldown": skipped_cool,
        "skipped_budget": skipped_budget, "candidates": total_discovered,
        "gmgn_calls": max(0, _calls_after - _calls_before),
        "seconds": round(time.time() - _t_loop, 1), "ts": time.time()})
    _LOGGER.info(
        "Cycle cost: %d GMGN request(s) for %d deep analysis/analyses "
        "(%d skipped by cooldown, %d by the per-minute budget) in %.0fs%s",
        _LAST_CYCLE_STATS["gmgn_calls"], analysed, skipped_cool, skipped_budget,
        _LAST_CYCLE_STATS["seconds"],
        (" — e.g. " + "; ".join(_skip_examples)) if _skip_examples else "")

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
            advice = duty_cycle_advice(elapsed, interval_sec)
            if advice:
                _LOGGER.warning("%s", advice)
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
