#!/usr/bin/env python3
"""
ENZO - Configuration Manager & Path Resolver
"""
import os
import json

try:
    import yaml
except ImportError:
    yaml = None

# Base Paths
WORKSPACE_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
CONFIG_DIR = os.path.join(WORKSPACE_ROOT, "config")
DATA_DIR = os.path.join(WORKSPACE_ROOT, "data")
DOCS_DIR = os.path.join(WORKSPACE_ROOT, "docs")
LOGS_DIR = os.path.join(DATA_DIR, "logs")

# Ensure required directories exist
os.makedirs(CONFIG_DIR, exist_ok=True)
os.makedirs(DATA_DIR, exist_ok=True)
os.makedirs(LOGS_DIR, exist_ok=True)

CONFIG_PATH = os.path.join(CONFIG_DIR, "enzo-config.yaml")
SECRETS_PATH = os.path.join(CONFIG_DIR, "enzo-secrets.json")
CONTROL_PATH = os.path.join(CONFIG_DIR, "enzo-control.json")
WATCHLIST_PATH = os.path.join(CONFIG_DIR, "enzo-watchlist.json")

PORTFOLIO_DB_PATH = os.path.join(DATA_DIR, "enzo.db")
PORTFOLIO_JSON_PATH = os.path.join(DATA_DIR, "enzo-portfolio.json")
STATE_JSON_PATH = os.path.join(DATA_DIR, "enzo-state.json")
LEARNING_PATH = os.path.join(DATA_DIR, "enzo-learning.json")
LEARNING_JSON_PATH = os.path.join(DATA_DIR, "enzo-learning.json")
CACHE_JSON_PATH = os.path.join(DATA_DIR, "enzo-cache.json")
LOG_JSONL_PATH = os.path.join(DATA_DIR, "enzo-log.jsonl")
AUDIT_JSONL_PATH = os.path.join(DATA_DIR, "enzo-audit.jsonl")
GMGN_BAN_FILE_PATH = os.path.join(DATA_DIR, "enzo-gmgn-ban.json")
BAN_JSON_PATH = os.path.join(DATA_DIR, "enzo-gmgn-ban.json")
PANEL_JSON_PATH = os.path.join(DATA_DIR, "enzo-panel.json")
MARKET_STRUCTURE_PATH = os.path.join(DATA_DIR, "enzo-market-structure.json")
DASHBOARD_HTML_PATH = os.path.join(DATA_DIR, "enzo-dashboard.html")


def clamp(v, lo=0, hi=100):
    try:
        return max(lo, min(hi, float(v)))
    except Exception:
        return lo


def load_config(path: str = None) -> dict:
    """Load configuration from enzo-config.yaml with robust defaults."""
    target_path = path or CONFIG_PATH
    if os.path.exists(target_path) and yaml is not None:
        try:
            with open(target_path, "r", encoding="utf-8") as f:
                cfg = yaml.safe_load(f)
                if isinstance(cfg, dict):
                    return cfg
        except Exception:
            pass

    return {
        "paper_mode": True,
        "market_analysis": {
            "min_liquidity": 150,
            "min_volume": 50,
            "min_holders": 20,
            "min_market_cap": 1000,
            "min_confidence_score": 55,
            "min_buy_pressure": 45,
            "max_scam_score": 12,
            "max_holder_percentage": 5.0,
        },
        "risk_management": {
            "risk_per_trade": 2.5,
            "max_exposure": 30.0,
            "max_daily_loss": 8.0,
            "max_drawdown": 25.0,
            "enable_risk_halts": True,
            "consecutive_losses_limit": 12,
            "max_open_positions": 5,
        },
        "exit_strategy": {
            "take_profit_percentage": 150.0,
            "take_profit_stages": [
                {"pct": 30, "sell": 0.3},
                {"pct": 70, "sell": 0.3},
                {"pct": 150, "sell": 0.4},
            ],
            "stop_loss_percentage": 50.0,
            "trailing_stop_percentage": 30.0,
            "max_holding_time_hours": 48,
        },
        "weighted_confidence": {
            "security": 30,
            "wallet_behavior": 20,
            "dev_behavior": 20,
            "momentum": 15,
            "market_structure": 10,
            "liquidity": 5,
        },
        "paper_trading": {
            "initial_capital": 10000.0,
            "slippage_tolerance": 0.5,
        },
    }


def load_secrets() -> dict:
    """Load API keys and Telegram credentials."""
    if os.path.exists(SECRETS_PATH):
        try:
            with open(SECRETS_PATH, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return {}
