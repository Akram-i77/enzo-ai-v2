#!/usr/bin/env python3
"""
ENZO - Configuration Manager & Path Resolver

Design rules (fixed 2026-09-03 after the "silent fallback" incident):

1. NEVER silently fall back to built-in defaults. If PyYAML is missing or the
   config file cannot be parsed, raise `EnzoConfigError` so the caller (and the
   operator, and OpenClaw) sees a loud, actionable failure instead of a bot that
   quietly runs with 17 of its 23 config sections missing.

2. Built-in DEFAULTS are a FLOOR, not a REPLACEMENT: the YAML is deep-merged
   over them, so a section you omit from the file still gets a sane value while
   every section you *do* write is honoured exactly.

3. `load_config()` is called ~33 places per scan cycle, so the parsed result is
   cached and only re-read when the file's mtime/size changes.
"""
import os
import json
import threading

try:
    import yaml
except ImportError:  # pragma: no cover - exercised by require_dependencies()
    yaml = None

# ─────────────────────────────────────────────────────────────────────────────
# Base Paths — everything resolves from the repository root, never from CWD,
# so the bot behaves identically no matter where OpenClaw launches it from.
# ─────────────────────────────────────────────────────────────────────────────
# ENZO_HOME lets a supervisor (OpenClaw) or a test harness point the bot at a
# different workspace root without editing code — the config/, data/ and log
# trees all follow it. Unset (the normal case) resolves to the repository root.
WORKSPACE_ROOT = os.path.abspath(
    os.environ.get("ENZO_HOME")
    or os.path.join(os.path.dirname(__file__), "..", "..")
)
CONFIG_DIR = os.path.join(WORKSPACE_ROOT, "config")
DATA_DIR = os.path.join(WORKSPACE_ROOT, "data")
DOCS_DIR = os.path.join(WORKSPACE_ROOT, "docs")
LOGS_DIR = os.path.join(DATA_DIR, "logs")
RUN_DIR = os.path.join(DATA_DIR, "run")

for _d in (CONFIG_DIR, DATA_DIR, LOGS_DIR, RUN_DIR):
    os.makedirs(_d, exist_ok=True)

CONFIG_PATH = os.path.join(CONFIG_DIR, "enzo-config.yaml")
SECRETS_PATH = os.path.join(CONFIG_DIR, "enzo-secrets.json")
SECRETS_EXAMPLE_PATH = os.path.join(CONFIG_DIR, "enzo-secrets.example.json")
CONTROL_PATH = os.path.join(CONFIG_DIR, "enzo-control.json")
WATCHLIST_PATH = os.path.join(CONFIG_DIR, "enzo-watchlist.json")

PORTFOLIO_DB_PATH = os.path.join(DATA_DIR, "enzo.db")
PORTFOLIO_JSON_PATH = os.path.join(DATA_DIR, "enzo-portfolio.json")
STATE_JSON_PATH = os.path.join(DATA_DIR, "enzo-state.json")
LEARNING_PATH = os.path.join(DATA_DIR, "enzo-learning.json")
LEARNING_JSON_PATH = LEARNING_PATH
CACHE_JSON_PATH = os.path.join(DATA_DIR, "enzo-cache.json")
LOG_JSONL_PATH = os.path.join(DATA_DIR, "enzo-log.jsonl")
AUDIT_JSONL_PATH = os.path.join(DATA_DIR, "enzo-audit.jsonl")
GMGN_BAN_FILE_PATH = os.path.join(DATA_DIR, "enzo-gmgn-ban.json")
BAN_JSON_PATH = GMGN_BAN_FILE_PATH
PANEL_JSON_PATH = os.path.join(DATA_DIR, "enzo-panel.json")
MARKET_STRUCTURE_PATH = os.path.join(DATA_DIR, "enzo-market-structure.json")
DASHBOARD_HTML_PATH = os.path.join(DATA_DIR, "enzo-dashboard.html")

# Supervisor / control-plane artefacts (used by enzoctl + the health endpoint)
PID_PATH = os.path.join(RUN_DIR, "enzo.pid")
HEALTH_PATH = os.path.join(RUN_DIR, "enzo-health.json")
SUPERVISOR_LOG_PATH = os.path.join(LOGS_DIR, "supervisor.log")
TRADE_GATE_PATH = os.path.join(DATA_DIR, "enzo-trade-gate.json")

# ─────────────────────────────────────────────────────────────────────────────
# Errors
# ─────────────────────────────────────────────────────────────────────────────


class EnzoError(Exception):
    """Base class for every deliberate ENZO failure."""


class EnzoDependencyError(EnzoError):
    """A required third-party package or external binary is missing."""


class EnzoConfigError(EnzoError):
    """The configuration could not be loaded or is invalid."""


# ─────────────────────────────────────────────────────────────────────────────
# DEFAULTS — the floor the user's YAML is merged on top of.
# Mirrors config/enzo-config.yaml so behaviour is identical whether a section
# is written explicitly or inherited.
# ─────────────────────────────────────────────────────────────────────────────
DEFAULTS = {
    # Safety default: a bot that cannot read its config must never trade real money.
    "paper_mode": True,
    "chain": "sol",
    "risk_management": {
        "risk_per_trade": 2.5,
        "max_exposure": 30.0,
        "max_daily_loss": 8.0,
        "max_drawdown": 25.0,
        "enable_risk_halts": True,
        "consecutive_losses_limit": 12,
        "max_open_positions": 5,
        "exit_monitor_interval_seconds": 1,
        "circuit_breaker_drop_pct": 15.0,
    },
    "exit_monitor": {
        "enabled": True,
        "interval_seconds": 2.0,
        "max_stale_age_seconds": 10.0,
        "failure_threshold": 3,
        "price_sources": ["pumpdev", "gmgn", "bonding_curve"],
        "never_auto_sell_on_failure": True,
        "stale_event_log_every_n_cycles": 30,
    },
    "market_analysis": {
        # Defaults mirror config/enzo-config.yaml (tightened 2026-09-03) so that
        # a missing YAML key cannot silently revert to the old loose floors.
        "min_liquidity": 5000,
        "min_volume": 8000,
        "min_holders": 10,
        "min_market_cap": 5000,
        "min_confidence_score": 55,
        "min_buy_pressure": 30,
        "max_scam_score": 15,
        "max_holder_percentage": 10.0,
    },
    # ── Which coins ENZO trades at all (owner decision 2026-09-05) ───────────
    # Standard pump.fun coins only ("Pump V1"): GMGN reports the launchpad as
    # `launchpad` = "pump" and `launchpad_platform` = "Pump.fun". Anything else
    # (letsbonk, moonshot, fourmeme, bags, a bare Raydium pair) is refused, and
    # so is a coin whose launchpad cannot be determined.
    "token_universe": {
        "pump_v1_only": True,
        "reject_unknown_launchpad": True,
        # Server-side floor applied to the discovery lists themselves, so the
        # request budget is spent on coins that could pass anyway.
        "discovery_min_market_cap": 5000,
    },
    # ── Different floors before and after the bonding curve graduates ────────
    # pre-migration: cap >= $5,000 and at least 10 SELL transactions (proof the
    # token can be sold by someone other than the dev).
    # migrated: cap >= $10,000 and total fees paid >= 2.5 SOL.
    "phase_gates": {
        "unknown_phase": "strict",      # strict | pre | reject
        "pre_migration": {
            "min_market_cap": 5000,
            "min_sells": 10,
        },
        "migrated": {
            "min_market_cap": 10000,
            "min_total_fees": 2.5,
            "fees_unit": "sol",         # GMGN does not state the unit; declare it
            "min_sells": None,
            "require_known_fees": True,
        },
    },
    # ── The rug signature: snipers take the first wallets with huge size ─────
    # gmgn-cli exposes no trade tape, so the window is reconstructed from
    # `token traders`: start_holding_at gives entry order, buy_volume_cur the USD
    # size, and GMGN's `sniper` tag marks launch buyers. If enough of the first
    # N wallets are snipers and their combined (or any single) buy exceeds the
    # threshold, the coin is refused permanently.
    "sniper_flood": {
        "enabled": True,
        "first_n": 8,
        "min_sniper_count": 4,
        "max_total_sniper_buy_usd": 5000,
        "max_single_sniper_buy_usd": 5000,
        "sniper_tags": ["sniper"],
        "include_bundler": False,
        "traders_limit": 100,
        "order_by": "buy_volume_cur",
        "on_unknown": "reject",         # reject | allow — when the window cannot be read
    },
    "scam_detection": {
        "honeypot_risk_threshold": 0.7,
        "rug_pull_risk_threshold": 0.7,
        "fake_liquidity_threshold": 0.7,
        "fake_volume_threshold": 0.7,
        "concentrated_holders_threshold": 0.8,
        "suspicious_ownership_threshold": 0.7,
        "fake_social_engagement_threshold": 0.7,
        "bundle_top10_threshold": 60.0,
        "bundle_single_threshold": 45.0,
        "bundler_flood_penalty": 30,
        "sniper_flood_penalty": 15,
        "rat_flood_penalty": 10,
        "bundlers_in_top20_penalty": 20,
        "top10_dumping_penalty": 10,
        "dev_team_hold_penalty": 15,
        "dev_factory_penalty": 15,
        "top70_sniper_hold_penalty": 10,
        "deep_dangerous_threshold": 15,
    },
    "entry_strategy": {
        "min_risk_reward_ratio": 2.0,
        "trend_confirmation_periods": 5,
    },
    "exit_strategy": {
        "take_profit_percentage": 150.0,
        "take_profit_stages": [
            {"pct": 30, "sell": 0.3},
            {"pct": 70, "sell": 0.3},
            {"pct": 150, "sell": 0.4},
        ],
        # Defaults mirror config/enzo-config.yaml (owner-tuned 2026-09-03) so a
        # missing YAML key cannot silently revert to the old looser exits.
        "stop_loss_percentage": 38.0,
        "trailing_stop_percentage": 40.0,
        "stall_exit_enabled": True,
        "stall_min_gain_pct": 15.0,
        "stall_seconds": 30.0,
        "max_holding_time_hours": 48,
    },
    # ── rug protection layers (owner-approved 2026-09-05) ──────────────────
    # Layer 1: absolute fingerprint VETOES at entry (bundled wallets, fresh
    #   sybil holders, serial factory). Organic launches do not carry these,
    #   so vetoing them costs no legitimate entry.
    # Layer 3: a TIGHTER stop for the first minutes, applied ONLY to entries
    #   that carried soft flags. Clean entries keep the owner's -38%/-40%.
    # Layer 4: a post-entry TRIPWIRE on the rug in motion (liquidity pulled,
    #   holders collapsing, top10 flipping to selling). Exits at any PnL.
    "rug_protection": {
        "fingerprints_enabled": True,
        "veto_bundlers_top20": 6,
        "veto_snipers_top20": 8,
        "veto_rats_top20": 5,
        "veto_avg_wallet_age_days": 3.0,
        "veto_top10_cur_sells": 25,
        "veto_factory_created": 50,
        "veto_factory_open_ratio": 0.03,
        "soft_flag_ratio": 0.5,
        "early_stop_enabled": True,
        "early_stop_pct": 12.0,
        "early_stop_window_min": 10.0,
        "tripwire_enabled": True,
        "tripwire_poll_sec": 20.0,
        "tripwire_liq_pull_pct": 40.0,
        "tripwire_holder_drop_pct": 15.0,
        "tripwire_top10_sells_jump": 15,
        "tripwire_min_votes": 2,
    },
    "scoring_weights": {
        "price_action": 0.15,
        "volume": 0.1,
        "liquidity": 0.1,
        "social_momentum": 0.15,
        "on_chain_activity": 0.2,
        "scam_indicators": 0.3,
    },
    "logging": {
        "level": "INFO",
        "log_api_errors": True,
        "log_network_latency": True,
    },
    "notifications": {
        "send_decision_notifications": True,
        "min_confidence_for_notification": 60,
        "send_scan_summary": True,
    },
    "data_sources": {
        "gmgn": {
            "cli": "gmgn-cli",
            "chain": "sol",
            "request_gap_ms": 350,
            # Sustained ceiling for the token bucket (was hardcoded at 0.8/s).
            "requests_per_sec": 0.8,
            # Burst size of that bucket: how many calls may fire back-to-back
            # before settling onto requests_per_sec. It used to be a hardcoded
            # default inside db.rl_acquire (i.e. not configurable), and the DB row
            # kept the value from the FIRST call, so editing requests_per_sec
            # later changed nothing until the row was deleted.
            "burst_capacity": 2.5,
            # gmgn-cli v1.6 has `market trenches|trending|hot-searches|signal`.
            # `market smartmoney` / `market kol` do NOT exist (smart money and KOL
            # live under `track`, which returns trade records for a wallet, not a
            # token list) - keeping them in the list burned a rate-limit slot and
            # logged a failure on every cycle.
            "discovery": ["trenches", "trending"],
            # 30, not 50: the limit is per category, and every extra candidate is
            # only ranking fodder - the deep path is capped at 6 analyses/cycle
            # anyway. Smaller payloads answer faster and cost less of the plan
            # quota (which is what earns RATE_LIMIT_BANNED).
            "discovery_limit": 30,
            # `market trending` REQUIRES --interval in gmgn-cli v1.6.1 (1m / 5m /
            # 1h / 6h / 24h). It was never sent, so trending failed on every cycle
            # and contributed nothing while still being listed as a source. 1m is
            # the owner's choice: the tightest momentum window, i.e. the closest
            # thing to fresh coins that `market rank` can give.
            "trending_interval": "1m",
            # Which trenches lifecycle stages to query (`market trenches --type`,
            # repeatable). new_creation is REMOVED at the owner's instruction:
            # tokens seconds old almost never clear the entry gates (min 10 sells,
            # min market cap) and are the stage where a dev dumps first. Keep
            # near_completion + completed. An empty list restores the CLI default
            # (all three). Note: fresh launches still arrive through the PumpDev
            # stream, which is a separate source.
            "trenches_types": ["near_completion", "completed"],
            # Server-side launchpad filter: trenches takes --launchpad-platform,
            # trending takes --platform. Empty string disables the filter.
            "launchpad_platform_filter": "Pump.fun",
            # Volume caps, ENFORCED by the scan loop. They were in this file and
            # read by NOTHING (test_config_wiring listed them as dead keys) while
            # the engine burned ~60 GMGN requests per cycle, back-to-back, 24/7 -
            # which is exactly how the key earned RATE_LIMIT_BANNED. Each deep
            # analysis costs ~5 requests (info, security, holders, traders,
            # created-tokens).
            "max_candidates_per_scan": 40,
            "max_depth_analyses": 6,
            # How long a coin that was examined and NOT bought stays out of the
            # deep path. Without it the same trending tokens were re-analysed
            # every 60 seconds forever - the single biggest request sink, and one
            # GMGN's own docs call out as an anti-pattern.
            "reanalysis_cooldown_sec": 900,
            "extra_discovery": True,
            "top_traders": True,
        },
        "pumpdev": {
            "enabled": True,
            "request_gap_ms": 150,
            "batch_size": 30,
            "price_refresh_secs": 1.0,
            "use_kolscan": True,
            "use_recent": True,
            "thresholds": {
                "max_twitter_reuse": 5,
                "max_telegram_reuse": 5,
                "max_website_reuse": 5,
                "dev_hold_hard_pct": 35,
                "dev_hold_soft_pct": 15,
                "bundler_hard_pct": 50,
                "bundler_soft_pct": 30,
                "sniper_hard": 100,
                "sniper_soft": 40,
                "sniper_owned_hard_pct": 40,
                "top10_hard_pct": 90,
                "top10_soft_pct": 70,
                # Fallbacks only — screen_pump_card prefers market_analysis.*
                # Kept aligned so the two sections cannot silently disagree.
                # min_holders stays 0: pre-migration pump.fun tokens legitimately
                # report no holder data yet (see pre_migration_exempt).
                "min_mcap": 5000,
                "min_volume": 8000,
                "min_holders": 0,
                "pre_migration_exempt": True,
            },
            "penalties": {
                "bundler_penalty_threshold": 0.3,
                "bundler_penalty": 25,
                "twitter_reuse_penalty_threshold": 3,
                "twitter_reuse_penalty": 20,
                "telegram_reuse_penalty_threshold": 4,
                "telegram_reuse_penalty": 10,
                "banned_penalty": 30,
            },
        },
    },
    "discovery": {
        "dedupe_window_minutes": 720,
        # Now enforced (it was a dead key): caps how many discovered tokens are
        # considered at all, together with data_sources.gmgn.max_candidates_per_scan
        # (the tighter of the two wins).
        "max_tokens_per_scan": 40,
        # The depth cap that ACTUALLY applied: engine.py used `discovery.… or
        # data_sources.gmgn.max_depth_analyses`, so this key always won and
        # tightening the other one changed nothing. The engine now takes the
        # tightest of the two; keep them equal to avoid surprises. 12 analyses x
        # ~5 GMGN requests = ~60 requests/cycle, which is what got the key banned.
        "max_depth_tokens_per_cycle": 6,
    },
    "paper_trading": {
        "initial_capital": 10000.0,
        "slippage_tolerance": 0.5,
    },
    # The autonomous loop's spacing. It used to live only as a code default (60),
    # invisible in the config file - and it matters for rate limits: when a cycle
    # takes longer than this, `sleep = max(1, interval - elapsed)` collapses to 1s
    # and GMGN sees one uninterrupted request stream. Now it is a knob the owner
    # can see and raise.
    "engine": {
        "scan_interval_sec": 60,
    },
    "pump_monitor": {
        "interval_sec": 30,
        "max_candidates": 40,
        "min_initial_buy_sol": 1.0,
        # The request budget. 15/min x ~5 requests was a 75 req/min ceiling that
        # no code enforced; 6/min is ~30 req/min and, together with
        # min_analysis_interval_sec and reanalysis_cooldown_sec, keeps the steady
        # state near ~10 req/min. GMGN's ban is ~5 minutes and every request sent
        # during it adds 5 more seconds, so the budget IS the protection.
        "min_analysis_interval_sec": 45,
        "max_analyses_per_min": 6,
    },
    "weighted_confidence": {
        "security": 30,
        "wallet_behavior": 20,
        "dev_behavior": 20,
        "momentum": 15,
        "market_structure": 10,
        "liquidity": 5,
    },
    "wallet_behavior": {
        "neutral_score": 50,
        "weights": {"diversity": 0.4, "concentration": 0.3, "growth": 0.3, "identity": 0.35},
    },
    "dev_behavior": {
        "neutral_score": 50,
        "track_ttl_sec": 1800,
        "sell_threshold_pct": 2.0,
        "buy_threshold_pct": 2.0,
        "liq_remove_pct": 0.2,
        "impact_dev_selling": -30,
        "impact_dev_buying": 25,
        "impact_dev_holding": 10,
        "impact_dev_distributing": 8,
        "factory_dev_min_created": 10,
        "factory_dev_heavy_created": 200,
        "factory_dev_penalty": 15,
        "factory_dev_heavy_penalty": 45,
        "factory_dev_low_open_ratio": 0.1,
        "factory_dev_low_open_penalty": 15,
        "factory_dev_no_open_ratio_penalty": 8,
        "factory_dev_mid_created": 50,
        "factory_dev_mid_penalty": 30,
        "factory_dev_dead_open_ratio": 0.03,
        "factory_dev_dead_open_penalty": 25,
        "factory_dev_watching_open_ratio": 0.25,
        "factory_dev_watching_open_penalty": 5,
        "factory_dev_penalty_cap": 80.0,
        "no_big_hits_max_ath_mc": 100000,
        "no_big_hits_penalty": 10,
    },
    "market_structure": {
        "neutral_score": 50,
        "min_sample_interval_sec": 60,
        "max_samples": 30,
    },
    "position_sizing": {
        "confidence_bands": [
            {"min": 55, "max": 60, "risk_pct": 1.0},
            {"min": 61, "max": 70, "risk_pct": 2.0},
            {"min": 71, "max": 80, "risk_pct": 3.0},
            {"min": 81, "max": 90, "risk_pct": 4.0},
            {"min": 91, "max": 100, "risk_pct": 5.0},
        ],
        # Live-mode floor: never size a position below what the executor can send.
        "min_position_usd": 1.0,
        "min_trade_is_floor": True,
    },
    "learning": {
        "enabled": True,
        "apply_weight_adjustments": False,
        "min_samples_for_adjust": 10,
    },
    "cache": {"holder_dist_ttl": 600},
    "execution": {
        "wallet_name": "enzo-trading",
        "capital_sync_grace_sec": 300,
        "base_token": "USDC",
        "slippage_bps": 50,
        "min_trade_usd": 1.0,
        "max_trade_usd": 500.0,
        "confirm_blocks": 1,
        "retry_attempts": 2,
        "rollback_on_failure": True,
        # Live capital source: "wallet" (sync from on-chain balance) or "fixed".
        "capital_source": "wallet",
        "capital_sync_ttl_sec": 60,
        # Reserve kept aside for network fees, never deployed into positions.
        "sol_fee_reserve": 0.02,
        # Optional warm local API (`mp serve` at 127.0.0.1:8787). Off by default
        # so OpenClaw has one less long-lived process to supervise.
        "use_local_api": False,
        "local_api_url": "http://127.0.0.1:8787",
        "local_api_timeout_sec": 45,
        # Tokens proven unroutable are skipped for this long (seconds).
        "not_routable_cooldown_sec": 3600,
        "moonpay_bin": "",
        # MoonPay spells Solana "solana"; the shared `chain` key spells it
        # "sol" for GMGN. Empty = translate automatically at the MoonPay boundary.
        "moonpay_chain": "",
        # On a failed quote, run `mp token check` to tell TOKEN_NOT_SUPPORTED
        # apart from NO_ROUTE. Costs one request only on failure.
        "diagnose_no_route": True,
    },
    "dashboard": {
        "port": 8077,
        "host": "0.0.0.0",
        "refresh_seconds": 10,
        "activity_limit": 100,
    },
}

# Keys whose presence/type we verify at load time — a typo here silently
# changed trading behaviour before, so it is now reported loudly.
_CRITICAL_KEYS = [
    ("paper_mode", bool),
    ("chain", str),
    ("execution", dict),
    ("risk_management", dict),
    ("exit_strategy", dict),
    ("market_analysis", dict),
    ("token_universe", dict),
    ("phase_gates", dict),
    ("sniper_flood", dict),
    ("data_sources", dict),
    ("weighted_confidence", dict),
]

# ─────────────────────────────────────────────────────────────────────────────
# helpers
# ─────────────────────────────────────────────────────────────────────────────

_CFG_LOCK = threading.RLock()
_CFG_CACHE = {"mtime": None, "size": None, "path": None, "cfg": None}


def clamp(v, lo=0, hi=100):
    try:
        return max(lo, min(hi, float(v)))
    except Exception:
        return lo


def deep_merge(base: dict, override: dict) -> dict:
    """Recursively merge `override` onto a copy of `base`.

    Dicts merge key-by-key; every other type (lists, scalars) is replaced
    wholesale, so `take_profit_stages:` in the YAML replaces the default list
    instead of being concatenated with it.
    """
    out = dict(base)
    if not isinstance(override, dict):
        return override
    for k, v in override.items():
        if k in out and isinstance(out[k], dict) and isinstance(v, dict):
            out[k] = deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def get_path(cfg: dict, dotted: str, default=None):
    """Read a dotted config path: get_path(cfg, 'execution.min_trade_usd')."""
    cur = cfg
    for part in str(dotted).split("."):
        if isinstance(cur, dict) and part in cur:
            cur = cur[part]
        else:
            return default
    return cur


def set_path(cfg: dict, dotted: str, value) -> bool:
    """Write a dotted config path, creating intermediate dicts. Returns success."""
    parts = str(dotted).split(".")
    cur = cfg
    for part in parts[:-1]:
        nxt = cur.get(part)
        if not isinstance(nxt, dict):
            nxt = {}
            cur[part] = nxt
        cur = nxt
    cur[parts[-1]] = value
    return True


# ─────────────────────────────────────────────────────────────────────────────
# dependency preflight
# ─────────────────────────────────────────────────────────────────────────────
REQUIRED_PACKAGES = [
    ("yaml", "PyYAML", "reads config/enzo-config.yaml — without it EVERY setting is ignored"),
    ("websockets", "websockets", "PumpDev real-time launch stream — without it discovery finds 0 tokens"),
]


def check_dependencies() -> dict:
    """Report on every third-party dependency without raising."""
    report = {"ok": True, "missing": [], "present": []}
    for module, package, why in REQUIRED_PACKAGES:
        try:
            __import__(module)
            report["present"].append({"module": module, "package": package})
        except ImportError:
            report["ok"] = False
            report["missing"].append({"module": module, "package": package, "why": why})
    return report


def require_dependencies(hint: str = "") -> None:
    """Fail loudly and early if a required package is missing.

    Called at every entry point (start/loop/scan/serve/botctl/enzoctl) so the
    operator gets one clear message instead of a bot that silently runs on
    built-in defaults.
    """
    rep = check_dependencies()
    if rep["ok"]:
        return
    lines = ["", "=" * 68, "  ENZO — MISSING REQUIRED DEPENDENCIES (refusing to start)", "=" * 68]
    for m in rep["missing"]:
        lines.append(f"  ✗ {m['package']:<12} {m['why']}")
    lines.append("")
    lines.append("  Fix:")
    pkgs = " ".join(m["package"] for m in rep["missing"])
    lines.append(f"    python3 -m pip install {pkgs}")
    lines.append("    # or:  bash bootstrap.sh")
    if hint:
        lines.append(f"  Note: {hint}")
    lines.append("=" * 68)
    raise EnzoDependencyError("\n".join(lines))


# ─────────────────────────────────────────────────────────────────────────────
# config loading
# ─────────────────────────────────────────────────────────────────────────────

_STRICT_LOADER = None


def _strict_loader():
    """A SafeLoader that REFUSES duplicate keys in the same mapping.

    PyYAML's default loader silently keeps only the LAST duplicate, so a
    hand-edited enzo-config.yaml with the same key twice loads the wrong value
    and nothing anywhere complains. This is a money-critical footgun for a file
    the operator edits by hand: a duplicated `requests_per_sec` once made a test
    sandbox run at the production 0.8 requests/second (three minutes per run)
    while the file *looked* like it asked for 200/s.
    """
    global _STRICT_LOADER
    if _STRICT_LOADER is not None:
        return _STRICT_LOADER

    class _Loader(yaml.SafeLoader):
        pass

    def _construct_no_duplicates(loader, node, deep=False):
        out = {}
        lines = {}
        for key_node, value_node in node.value:
            key = loader.construct_object(key_node, deep=deep)
            ln = key_node.start_mark.line + 1
            if key in out:
                raise EnzoConfigError(
                    f"duplicate config key '{key}' — it appears on line {lines[key]} "
                    f"and again on line {ln}. YAML would silently keep ONLY the last "
                    f"one, so the bot could trade with settings you never meant. "
                    f"Delete one of the two lines.")
            lines[key] = ln
            out[key] = loader.construct_object(value_node, deep=deep)
        return out

    _Loader.add_constructor(yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG,
                            _construct_no_duplicates)
    _STRICT_LOADER = _Loader
    return _Loader


def _parse_yaml(text: str) -> dict:
    """Parse YAML, with a tiny built-in fallback for the simple flat/nested
    subset ENZO uses. The fallback exists only so a missing PyYAML degrades to
    a *loud warning plus best-effort parse*, never to silently-wrong settings.
    """
    if yaml is not None:
        data = yaml.load(text, Loader=_strict_loader())
        return data if isinstance(data, dict) else {}
    return _mini_yaml(text)


def _coerce_scalar(tok: str):
    t = tok.strip()
    if t == "" or t is None:
        return None
    if t in ("~", "null", "Null", "NULL"):
        return None
    low = t.lower()
    if low in ("true", "yes", "on"):
        return True
    if low in ("false", "no", "off"):
        return False
    if (t.startswith('"') and t.endswith('"')) or (t.startswith("'") and t.endswith("'")):
        return t[1:-1]
    try:
        return int(t)
    except ValueError:
        pass
    try:
        return float(t)
    except ValueError:
        pass
    return t


def _mini_yaml(text: str) -> dict:
    """Indentation-based parser for the YAML subset used by enzo-config.yaml.

    Only reached when PyYAML is missing AND the caller asked for strict=False
    (diagnostics). Supports nested mappings, `- ` sequences of scalars and of
    inline `{...}` maps, `#` comments, and the coercions in `_coerce_scalar`.
    """
    lines = []
    for raw in text.splitlines():
        if not raw.strip() or raw.lstrip().startswith("#"):
            continue
        stripped = raw.split(" #", 1)[0].rstrip() if " #" in raw else raw.rstrip()
        if not stripped.strip():
            continue
        lines.append((len(stripped) - len(stripped.lstrip(" ")), stripped.strip()))

    root: dict = {}
    # Each frame: (indent, container, kind)
    stack = [(-1, root, "map")]

    def peek_kind(idx, indent):
        """Is the next line at deeper indent a sequence item?"""
        for j in range(idx + 1, len(lines)):
            ind, txt = lines[j]
            if ind <= indent:
                return "map"
            return "list" if txt.startswith("- ") or txt == "-" else "map"
        return "map"

    i = 0
    while i < len(lines):
        indent, line = lines[i]
        while len(stack) > 1 and indent <= stack[-1][0]:
            stack.pop()
        _, container, kind = stack[-1]

        if line.startswith("- ") or line == "-":
            item = line[2:].strip() if line != "-" else ""
            if kind != "list":
                i += 1
                continue
            if item.startswith("{") and item.endswith("}"):
                body = item[1:-1]
                sub = {}
                for part in body.split(","):
                    if ":" in part:
                        k, _, v = part.partition(":")
                        sub[k.strip().strip("\"'")] = _coerce_scalar(v)
                container.append(sub)
            elif ":" in item:
                sub = {}
                container.append(sub)
                k, _, v = item.partition(":")
                sub[k.strip()] = _coerce_scalar(v)
                stack.append((indent + 2, sub, "map"))
            else:
                container.append(_coerce_scalar(item))
            i += 1
            continue

        if ":" not in line:
            i += 1
            continue

        key, _, val = line.partition(":")
        key = key.strip().strip("\"'")
        val = val.strip()
        if val:
            container[key] = _coerce_scalar(val)
            i += 1
            continue

        nxt = peek_kind(i, indent)
        if nxt == "list":
            newlist: list = []
            container[key] = newlist
            stack.append((indent, newlist, "list"))
        else:
            newmap: dict = {}
            container[key] = newmap
            stack.append((indent, newmap, "map"))
        i += 1

    return root


def validate_config(cfg: dict) -> list:
    """Return a list of human-readable problems (empty list = healthy)."""
    problems = []
    for key, typ in _CRITICAL_KEYS:
        if key not in cfg:
            problems.append(f"missing section '{key}'")
        elif not isinstance(cfg[key], typ):
            problems.append(f"'{key}' should be {typ.__name__}, got {type(cfg[key]).__name__}")

    ex = cfg.get("execution") or {}
    if not ex.get("wallet_name"):
        problems.append("execution.wallet_name is empty")
    if cfg.get("paper_mode") is False:
        try:
            if float(ex.get("min_trade_usd", 0)) <= 0:
                problems.append("execution.min_trade_usd must be > 0 in live mode")
        except Exception:
            problems.append("execution.min_trade_usd is not a number")
        if ex.get("capital_source") not in ("wallet", "fixed"):
            problems.append("execution.capital_source must be 'wallet' or 'fixed'")

    wc = cfg.get("weighted_confidence") or {}
    try:
        total = sum(float(v) for v in wc.values())
        if abs(total - 100.0) > 0.01:
            problems.append(f"weighted_confidence sums to {total}, expected 100")
    except Exception:
        problems.append("weighted_confidence contains a non-numeric weight")

    stages = (cfg.get("exit_strategy") or {}).get("take_profit_stages") or []
    if not isinstance(stages, list):
        problems.append("exit_strategy.take_profit_stages must be a list")
    return problems


def load_config(path: str = None, strict: bool = True) -> dict:
    """Load config/enzo-config.yaml deep-merged over DEFAULTS.

    Args:
        path:   alternative config file (used by tests / `enzoctl config --file`).
        strict: raise EnzoConfigError on a missing/unparsable file. When False
                the DEFAULTS floor is returned instead — used only by read-only
                helpers (status/doctor) that must never crash.

    Raises:
        EnzoDependencyError: PyYAML is not installed and the fallback parser
                             could not be trusted with a live-trading config.
        EnzoConfigError:     the file is missing, empty or malformed.
    """
    target_path = path or CONFIG_PATH

    with _CFG_LOCK:
        if path is None:
            try:
                st = os.stat(target_path)
                sig = (st.st_mtime_ns, st.st_size)
            except OSError:
                sig = None
            if sig is not None and _CFG_CACHE["cfg"] is not None and _CFG_CACHE["path"] == target_path:
                if (_CFG_CACHE["mtime"], _CFG_CACHE["size"]) == sig:
                    return _CFG_CACHE["cfg"]

        if not os.path.exists(target_path):
            if strict:
                raise EnzoConfigError(
                    f"Config file not found: {target_path}\n"
                    f"  ENZO refuses to start without its configuration.\n"
                    f"  Restore it from git, or copy config/enzo-config.yaml.example."
                )
            return json.loads(json.dumps(DEFAULTS))

        try:
            with open(target_path, "r", encoding="utf-8") as f:
                raw = f.read()
        except OSError as e:
            if strict:
                raise EnzoConfigError(f"Cannot read {target_path}: {e}")
            return json.loads(json.dumps(DEFAULTS))

        if not raw.strip():
            if strict:
                raise EnzoConfigError(f"{target_path} is empty — refusing to run on defaults.")
            return json.loads(json.dumps(DEFAULTS))

        # PyYAML is a hard requirement. Before this guard existed, a missing
        # PyYAML made load_config() return built-in defaults and silently drop
        # 17 of the 23 sections of enzo-config.yaml (including paper_mode).
        if yaml is None:
            if strict:
                raise EnzoDependencyError(
                    "PyYAML is not installed — config/enzo-config.yaml cannot be read.\n"
                    "  ENZO refuses to guess at your settings.\n"
                    "  Fix:  python3 -m pip install PyYAML   (or: bash bootstrap.sh)"
                )
            # Non-strict callers (doctor/status) get a best-effort parse so the
            # report can still show what is on disk.
            try:
                user_cfg = _mini_yaml(raw)
            except Exception:
                user_cfg = {}
            return deep_merge(DEFAULTS, user_cfg)

        try:
            user_cfg = _parse_yaml(raw)
        except Exception as e:
            if strict:
                raise EnzoConfigError(f"Failed to parse {target_path}: {e}")
            return json.loads(json.dumps(DEFAULTS))

        if not isinstance(user_cfg, dict):
            if strict:
                raise EnzoConfigError(
                    f"{target_path} did not parse to a mapping (got {type(user_cfg).__name__})."
                )
            return json.loads(json.dumps(DEFAULTS))

        cfg = deep_merge(DEFAULTS, user_cfg)

        if path is None:
            try:
                st = os.stat(target_path)
                _CFG_CACHE.update(
                    {"mtime": st.st_mtime_ns, "size": st.st_size, "path": target_path, "cfg": cfg}
                )
            except OSError:
                pass
        return cfg


def reload_config(path: str = None) -> dict:
    """Drop the cache and re-read from disk (used by `enzoctl config set`)."""
    with _CFG_LOCK:
        _CFG_CACHE.update({"mtime": None, "size": None, "cfg": None})
    return load_config(path)


def config_problems(path: str = None) -> list:
    """Validate without raising — returns [] when healthy."""
    try:
        cfg = load_config(path, strict=False)
    except EnzoError as e:
        return [str(e)]
    return validate_config(cfg)


def load_secrets() -> dict:
    """Load API keys and Telegram credentials (never raises)."""
    if os.path.exists(SECRETS_PATH):
        try:
            with open(SECRETS_PATH, "r", encoding="utf-8") as f:
                data = json.load(f)
                return data if isinstance(data, dict) else {}
        except Exception:
            return {}
    return {}


def save_secrets(data: dict) -> None:
    """Atomically write the secrets file (used by botctl chat-id registration)."""
    os.makedirs(os.path.dirname(SECRETS_PATH), exist_ok=True)
    tmp = SECRETS_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)
    os.replace(tmp, SECRETS_PATH)
