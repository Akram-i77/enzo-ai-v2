#!/usr/bin/env python3
"""
ENZO - Real Trading Executor via MoonPay CLI + Jupiter Aggregator
Handles on-chain buy/sell execution for Solana memecoins.
"""
import json
import os
import re
import subprocess
import time
from typing import Optional, Dict, Any, Tuple

from enzo.core.config import load_config, SECRETS_PATH, WORKSPACE_ROOT

# Add workspace root to path for imports
import sys
sys.path.insert(0, os.path.join(WORKSPACE_ROOT, "enzo"))

# ── Solana token addresses ──────────────────────────────────────────────────
# Correct addresses (verified against Solana token list):
#  - Wrapped SOL (wSOL): So11111111111111111111111111111111111111112  (44 chars)
#  - USDC on Solana:     EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v
WRAPPED_SOL = "So11111111111111111111111111111111111111112"
USDC_MAINNET = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"

# ── Token decimals (needed for MoonPay/Jupiter CLI: amounts in smallest units) ─
_SOL_DECIMALS = 9
_USDC_DECIMALS = 6
# Unknown SPL tokens default to 9 (most SPL tokens use 9 decimals)
_UNKNOWN_DECIMALS = 9

_TOKEN_DECIMALS = {
    WRAPPED_SOL.lower(): _SOL_DECIMALS,
    USDC_MAINNET.lower(): _USDC_DECIMALS,
}


def _to_smallest_unit(token_address: str, human_amount: float) -> int:
    """Convert human-readable amount → smallest units (lamports/smallest) for MoonPay CLI."""
    addr = token_address.lower()
    decimals = _TOKEN_DECIMALS.get(addr, _UNKNOWN_DECIMALS)
    return int(human_amount * (10 ** decimals))


def _from_smallest_unit(token_address: str, raw_amount: int) -> float:
    """Convert smallest units → human-readable amount."""
    addr = token_address.lower()
    decimals = _TOKEN_DECIMALS.get(addr, _UNKNOWN_DECIMALS)
    return raw_amount / (10 ** decimals)


# ── MoonPay CLI path ──────────────────────────────────────────────────────────
MOONPAY_BIN = os.path.expanduser("~/.npm-global/bin/moonpay")


def _run_moonpay(args: list, timeout: int = 120) -> Tuple[int, str, str]:
    """Run moonpay CLI command, return (exit_code, stdout, stderr)."""
    cmd = [MOONPAY_BIN] + args
    env = os.environ.copy()
    env["PATH"] = f"{os.path.dirname(MOONPAY_BIN)}:{env.get('PATH', '')}"
    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
            env=env,
        )
        return proc.returncode, proc.stdout, proc.stderr
    except subprocess.TimeoutExpired:
        return -1, "", "Command timed out"
    except FileNotFoundError:
        return -2, "", f"MoonPay CLI not found at {MOONPAY_BIN}"


# ── Wallet & Config helpers ────────────────────────────────────────────────────

def get_wallet_name(cfg: dict = None) -> str:
    """Get configured trading wallet name from config."""
    cfg = cfg or load_config()
    return cfg.get("execution", {}).get("wallet_name", "enzo-trading")


def get_base_token(cfg: dict = None) -> str:
    """Get base token for buys: USDC or SOL."""
    cfg = cfg or load_config()
    return cfg.get("execution", {}).get("base_token", "USDC")


# ── Balance checks ────────────────────────────────────────────────────────────

def _list_balances(wallet: str, chain: str = "solana") -> list:
    """Return list of balance items from MoonPay CLI."""
    args = ["token", "balance", "list", "--wallet", wallet, "--chain", chain, "--json"]
    code, out, err = _run_moonpay(args, timeout=20)
    if code != 0:
        return []
    try:
        data = json.loads(out)
        if isinstance(data, dict):
            return data.get("items", [])
        return data if isinstance(data, list) else []
    except json.JSONDecodeError:
        return []


def get_sol_balance(wallet: str = None) -> float:
    """Get SOL (native) balance for wallet. SOL is always the first item returned."""
    cfg = load_config()
    wallet = wallet or get_wallet_name(cfg)
    items = _list_balances(wallet, chain="solana")
    if items:
        return float(items[0].get("balance", {}).get("amount", 0))
    return 0.0


def get_usdc_balance(wallet: str = None) -> float:
    """Get USDC balance for wallet."""
    cfg = load_config()
    wallet = wallet or get_wallet_name(cfg)
    items = _list_balances(wallet, chain="solana")
    usdc_lower = USDC_MAINNET.lower()
    for item in items:
        if item.get("address", "").lower() == usdc_lower:
            return float(item.get("balance", {}).get("amount", 0))
    return 0.0


def check_balance(wallet: str = None, token: str = None) -> Optional[Dict[str, Any]]:
    """Check balance for a specific token. Returns the balance dict or None."""
    cfg = load_config()
    wallet = wallet or get_wallet_name(cfg)
    items = _list_balances(wallet, chain="solana")
    if not token:
        return items[0] if items else None
    token_lower = token.lower()
    for item in items:
        if item.get("address", "").lower() == token_lower:
            return item
    return None


# ── Quote (pre-flight check — does NOT execute) ───────────────────────────────

def get_quote(
    from_token: str,
    to_token: str,
    from_amount: float,
    chain: str = "solana",
) -> Optional[Dict[str, Any]]:
    """
    Get a Jupiter quote for a swap WITHOUT executing.
    Useful to check price/liquidity before placing a real order.
    """
    raw_amount = _to_smallest_unit(from_token, from_amount)
    args = [
        "token", "quote",
        "--from-token", from_token,
        "--to-token", to_token,
        "--from-amount", str(raw_amount),
        "--chain", chain,
    ]
    code, out, err = _run_moonpay(args, timeout=30)
    if code != 0:
        return None
    try:
        return json.loads(out)
    except json.JSONDecodeError:
        return None


# ── Transaction helpers ──────────────────────────────────────────────────────

def _parse_tx_hash(text: str) -> Optional[str]:
    """Extract Solana tx signature from MoonPay CLI output."""
    # Solana base58 tx sigs are 43-44 chars
    for line in text.splitlines():
        line = line.strip()
        matches = re.findall(r"[A-HJ-NP-Za-km-z]{43,44}", line)
        for m in matches:
            if len(m) > 60:  # tx sigs are long
                return m
    return None


def get_tx_status(tx_hash: str, chain: str = "solana") -> Optional[Dict[str, Any]]:
    """Check transaction status on-chain."""
    if not tx_hash:
        return None
    args = ["transaction", "retrieve", "--chain", chain, "--id", tx_hash]
    code, out, err = _run_moonpay(args, timeout=30)
    if code != 0:
        return None
    try:
        return json.loads(out)
    except json.JSONDecodeError:
        return None


# ── Core swap executor ────────────────────────────────────────────────────────

def execute_swap(
    from_token: str,
    to_token: str,
    from_amount: float,
    wallet: str = None,
    explanation: str = None,
    chain: str = "solana",
    timeout: int = 180,
) -> Dict[str, Any]:
    """
    Execute a token swap via MoonPay CLI → Jupiter Aggregator on Solana.
    Handles build, sign, broadcast, and registration in one call.

    Args:
        from_token:   Token contract address being sold (USDC or wSOL)
        to_token:     Token contract address being bought (memecoin mint)
        from_amount:  Human-readable amount to sell (e.g. 10.0 USDC)
        wallet:       MoonPay wallet name
        explanation:  Audit reason for the trade
        chain:        Blockchain (default: solana)
        timeout:      Max seconds to wait for confirmation

    Returns:
        {"ok": True, "tx_hash": "...", ...} or {"ok": False, "reason": "..."}
    """
    cfg = load_config()
    wallet = wallet or get_wallet_name(cfg)
    explanation = explanation or "Enzo trading bot position entry on Solana"

    # ── Pre-flight checks ──────────────────────────────────────────────────

    # Need SOL for fees (~0.005 SOL minimum)
    sol_bal = get_sol_balance(wallet)
    if sol_bal < 0.005:
        return {
            "ok": False,
            "reason": f"Insufficient SOL for fees (have {sol_bal:.4f}, need ≥0.005)",
            "tx_hash": None,
        }

    # Verify Jupiter can quote this pair
    quote = get_quote(from_token, to_token, from_amount, chain)
    if not quote:
        return {
            "ok": False,
            "reason": "Failed to get Jupiter quote — token pair may have no liquidity",
            "tx_hash": None,
        }

    # ── Execute swap ───────────────────────────────────────────────────────

    raw_amount = _to_smallest_unit(from_token, from_amount)
    args = [
        "token", "swap",
        "--wallet", wallet,
        "--chain", chain,
        "--from-token", from_token,
        "--from-amount", str(raw_amount),
        "--to-token", to_token,
        "--explanation", explanation,
        "--yes",  # Non-interactive: auto-confirm
    ]

    code, out, err = _run_moonpay(args, timeout=timeout)
    combined = out + "\n" + err

    tx_hash = _parse_tx_hash(combined)

    if code == 0 or tx_hash or "success" in combined.lower():
        return {
            "ok": True,
            "reason": "swap_executed",
            "tx_hash": tx_hash,
            "quote": quote,
            "wallet": wallet,
            "from_token": from_token,
            "to_token": to_token,
            "from_amount": from_amount,
            "raw_amount": raw_amount,
        }
    else:
        return {
            "ok": False,
            "reason": err.strip() or out.strip() or "Swap failed",
            "tx_hash": tx_hash,
            "from_token": from_token,
            "to_token": to_token,
            "amount": from_amount,
        }


# ── Buy / Sell convenience wrappers ──────────────────────────────────────────

def buy_token(
    mint: str,
    amount_usd: float,
    wallet: str = None,
    entry_price: float = None,
    explanation: str = None,
) -> Dict[str, Any]:
    """
    Buy a memecoin with USDC via Jupiter on Solana.

    Args:
        mint:        Token mint address to buy
        amount_usd:  USD value to spend (e.g. 50.0 for $50)
        wallet:      MoonPay wallet name
        entry_price: Token price (for record keeping in audit)
        explanation: Audit reason

    Returns:
        Execution result dict
    """
    cfg = load_config()
    wallet = wallet or get_wallet_name(cfg)
    base_token = get_base_token(cfg)

    if base_token.upper() == "USDC":
        from_token = USDC_MAINNET
        from_amount = amount_usd  # USDC is ~1:1 with USD, 6 decimals
    else:
        # Buying with SOL
        from_token = WRAPPED_SOL
        price = entry_price or 100.0  # approximate SOL price if unknown
        from_amount = amount_usd / price

    # ── Minimum trade size check ──────────────────────────────────────────
    min_trade = float(cfg.get("execution", {}).get("min_trade_usd", 1.0))
    if amount_usd < min_trade:
        return {
            "ok": False,
            "reason": f"Position size ${amount_usd:.2f} below minimum ${min_trade:.2f}",
            "tx_hash": None,
        }

    explanation = explanation or f"Enzo BUY {mint[:8]}... via Jupiter on Solana (${amount_usd:.2f})"
    result = execute_swap(
        from_token=from_token,
        to_token=mint,
        from_amount=from_amount,
        wallet=wallet,
        explanation=explanation,
    )

    result["amount_usd"] = amount_usd
    result["base_token"] = base_token
    return result


def sell_token(
    mint: str,
    amount_spl: float,
    wallet: str = None,
    to_token: str = None,
    explanation: str = None,
) -> Dict[str, Any]:
    """
    Sell a memecoin for USDC (or SOL) via Jupiter on Solana.

    Args:
        mint:        Token mint address to sell
        amount_spl:  Amount of SPL tokens to sell (human-readable)
        wallet:      MoonPay wallet name
        to_token:    Token to receive (USDC or SOL, default: USDC)

    Returns:
        Execution result dict
    """
    cfg = load_config()
    wallet = wallet or get_wallet_name(cfg)
    base_token = get_base_token(cfg)

    # Resolve target token address
    if to_token is None:
        to_token = USDC_MAINNET  # default: sell back to USDC
    elif to_token.upper() == "USDC":
        to_token = USDC_MAINNET
    elif to_token.upper() in ("SOL", "WSOL"):
        to_token = WRAPPED_SOL

    explanation = explanation or f"Enzo SELL {mint[:8]}... via Jupiter on Solana"
    result = execute_swap(
        from_token=mint,
        to_token=to_token,
        from_amount=amount_spl,
        wallet=wallet,
        explanation=explanation,
    )

    result["base_token"] = base_token
    return result


# ── Readiness check ───────────────────────────────────────────────────────────

def is_ready(cfg: dict = None) -> Tuple[bool, str]:
    """
    Check if Enzo is ready for real trading.
    Verifies: MoonPay CLI installed, wallet exists, SOL for fees, paper_mode off.

    Returns:
        (True, "Ready — Wallet: ..., SOL: ...") on success
        (False, "reason string") on failure
    """
    cfg = cfg or load_config()

    # 1. MoonPay CLI installed?
    if not os.path.exists(MOONPAY_BIN):
        return False, f"MoonPay CLI not found at {MOONPAY_BIN}"

    # 2. Wallet exists in MoonPay?
    wallet = get_wallet_name(cfg)
    code, out, _ = _run_moonpay(["wallet", "list"], timeout=10)
    if code != 0 or wallet not in out:
        return False, f"Wallet '{wallet}' not found in MoonPay CLI"

    # 3. Enough SOL for fees?
    sol_bal = get_sol_balance(wallet)
    if sol_bal < 0.005:
        return False, f"Insufficient SOL for fees ({sol_bal:.4f} < 0.005 SOL needed)"

    # 4. paper_mode must be off
    if cfg.get("paper_mode", True):
        return False, "paper_mode is still enabled in config — real trading blocked"

    return True, f"Ready — Wallet: {wallet}, SOL: {sol_bal:.4f}"