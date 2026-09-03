#!/usr/bin/env python3
"""
ENZO - Real Trading Executor (MoonPay CLI → swaps.xyz → Solana)

Rewritten 2026-09-03 against the ACTUAL contract of @moonpay/cli (verified by
downloading v1.96.0 from npm and reading its command builder in dist/index.js,
plus the bundled skills/*.md). The previous version could not execute a single
swap, for six independent reasons:

  1. it passed `--yes`, which is not an option of any command. Commander aborts
     with `error: unknown option '--yes'` and exit code 1 before doing anything.
  2. `token quote` was called with `--chain`, but the real schema is
     from:{chain,token,amount} + to:{chain,token,amount} → the required flags
     are `--from-chain` and `--to-chain`. The quote therefore always failed, and
     execute_swap() bailed out with "Failed to get Jupiter quote — token pair may
     have no liquidity" for EVERY token. That single misleading message is why
     it looked like MoonPay "only accepts trending coins".
  3. amounts were multiplied by 10**decimals. The CLI wants human token units
     ("Amount to sell in token units", e.g. 5 for 5 USDC); swaps.xyz does the
     decimal conversion internally. $50 was being sent as 50,000,000.
  4. output was parsed with json.loads(), but the CLI emits YAML-ish text unless
     the global `--json` flag is given. Only `token balance list` got it.
  5. _parse_tx_hash() matched 43–44 char base58 strings and then required
     len(m) > 60 — an impossible condition, so tx_hash was always None even on
     a successful swap. The real field is `signature`.
  6. `transaction retrieve --chain solana --id X` — the real parameter is
     `transactionId`, i.e. `--transactionId`. There is no `--id`/`--chain`.

Also fixed: the binary path was hardcoded to ~/.npm-global/bin/moonpay (now
resolved with shutil.which + config override + common prefixes), and
get_sol_balance() assumed items[0] was always SOL (now matched by address).

Canonical invocation (from MoonPay's own skills/moonpay-trading-automation):

    MP="$(which mp)"
    "$MP" --json token swap --wallet main --chain solana \
          --from-token <USDC> --from-amount 5 --to-token <mint>
"""
import json
import os
import re
import shutil
import subprocess
import threading
import time
import urllib.error
import urllib.request
from typing import Optional, Dict, Any, Tuple, List

from enzo.core.config import (
    load_config,
    SECRETS_PATH,
    WORKSPACE_ROOT,
    TRADE_GATE_PATH,
)
import enzo.core.log as log

_LOGGER = log.get_logger("enzo.executor")

# ── Solana token addresses ──────────────────────────────────────────────────
# wSOL mint (43 chars, ends in 2) — the real SPL wrapped-SOL token.
WRAPPED_SOL = "So11111111111111111111111111111111111111112"
USDC_MAINNET = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
# MoonPay's own docs/skills spell native SOL with a trailing 1. swaps.xyz
# accepts both, but we try the documented form first when the config asks for
# SOL as the base token, and fall back to the canonical wSOL mint.
MOONPAY_NATIVE_SOL = "So11111111111111111111111111111111111111111"

_SOL_DECIMALS = 9
_USDC_DECIMALS = 6
# Kept for callers that still want to display raw units. Swaps NEVER use it:
# the CLI takes human units.
_TOKEN_DECIMALS = {
    WRAPPED_SOL.lower(): _SOL_DECIMALS,
    MOONPAY_NATIVE_SOL.lower(): _SOL_DECIMALS,
    USDC_MAINNET.lower(): _USDC_DECIMALS,
}

# ── Failure taxonomy — surfaced verbatim in logs, audit, Telegram & dashboard
E_CLI_NOT_FOUND = "CLI_NOT_FOUND"
E_NOT_AUTHED = "NOT_AUTHENTICATED"
E_CONSENT = "CONSENT_REQUIRED"
E_UNKNOWN_OPTION = "UNKNOWN_OPTION"
E_NO_ROUTE = "NO_ROUTE"
E_INSUFFICIENT = "INSUFFICIENT_BALANCE"
E_INSUFFICIENT_FEE = "INSUFFICIENT_SOL_FOR_FEES"
E_RATE_LIMITED = "RATE_LIMITED"
E_WALLET_MISSING = "WALLET_NOT_FOUND"
E_BELOW_MIN = "BELOW_MINIMUM_TRADE"
E_ABOVE_MAX = "ABOVE_MAXIMUM_TRADE"
E_TIMEOUT = "TIMEOUT"
E_BAD_RESPONSE = "MALFORMED_RESPONSE"
E_PAPER_BLOCKED = "PAPER_MODE_ENABLED"
E_UNKNOWN = "UNKNOWN"

# Errors that mean "this specific token cannot be traded right now" — cached so
# we stop paying rate-limit budget to re-discover the same dead end.
_TOKEN_LEVEL_ERRORS = {E_NO_ROUTE, E_INSUFFICIENT}


def _to_smallest_unit(token_address: str, human_amount: float) -> int:
    """Human units → raw integer units. DISPLAY/LEDGER USE ONLY.

    Never pass this to the MoonPay CLI: it expects human token units and does
    the decimal conversion itself.
    """
    decimals = _TOKEN_DECIMALS.get(str(token_address).lower(), 9)
    return int(round(float(human_amount) * (10 ** decimals)))


def _from_smallest_unit(token_address: str, raw_amount: int) -> float:
    decimals = _TOKEN_DECIMALS.get(str(token_address).lower(), 9)
    return float(raw_amount) / (10 ** decimals)


def _fmt_amount(v: float) -> str:
    """Format a human-unit amount for the CLI without exponent notation."""
    v = float(v)
    if v == 0:
        return "0"
    if abs(v) >= 1:
        return f"{v:.9f}".rstrip("0").rstrip(".")
    return f"{v:.12f}".rstrip("0").rstrip(".")


# ─────────────────────────────────────────────────────────────────────────────
# Binary resolution
# ─────────────────────────────────────────────────────────────────────────────
_CANDIDATE_BINS = ("mp", "moonpay")
_CANDIDATE_DIRS = (
    os.path.expanduser("~/.npm-global/bin"),
    os.path.expanduser("~/.local/bin"),
    os.path.expanduser("~/.nvm/versions/node"),  # walked below
    "/usr/local/bin",
    "/usr/bin",
    "/opt/homebrew/bin",
)
_BIN_CACHE = {"path": None, "checked_at": 0.0}
_BIN_LOCK = threading.Lock()


def resolve_bin(cfg: dict = None, force: bool = False) -> Optional[str]:
    """Locate the MoonPay CLI. Config override → PATH → well-known prefixes.

    Replaces the hardcoded `~/.npm-global/bin/moonpay`, which made is_ready()
    report "MoonPay CLI not found" on any machine using the default npm prefix,
    nvm, pnpm, or an `mp`-only install.
    """
    with _BIN_LOCK:
        if not force and _BIN_CACHE["path"] and (time.time() - _BIN_CACHE["checked_at"]) < 300:
            return _BIN_CACHE["path"]
        cfg = cfg or {}
        found = None

        explicit = str((cfg.get("execution") or {}).get("moonpay_bin") or "").strip()
        if explicit:
            expanded = os.path.expanduser(explicit)
            if os.path.isfile(expanded) and os.access(expanded, os.X_OK):
                found = expanded
            elif shutil.which(expanded):
                found = shutil.which(expanded)
            else:
                _LOGGER.warning("execution.moonpay_bin=%r is not executable — falling back to PATH", explicit)

        if not found:
            for name in _CANDIDATE_BINS:
                p = shutil.which(name)
                if p:
                    found = p
                    break

        if not found:
            for d in _CANDIDATE_DIRS:
                if not os.path.isdir(d):
                    continue
                for name in _CANDIDATE_BINS:
                    p = os.path.join(d, name)
                    if os.path.isfile(p) and os.access(p, os.X_OK):
                        found = p
                        break
                if found:
                    break

        if not found:
            # nvm installs: ~/.nvm/versions/node/vX.Y.Z/bin/mp
            nvm_root = os.path.expanduser("~/.nvm/versions/node")
            if os.path.isdir(nvm_root):
                for ver in sorted(os.listdir(nvm_root), reverse=True):
                    for name in _CANDIDATE_BINS:
                        p = os.path.join(nvm_root, ver, "bin", name)
                        if os.path.isfile(p) and os.access(p, os.X_OK):
                            found = p
                            break
                    if found:
                        break

        _BIN_CACHE["path"] = found
        _BIN_CACHE["checked_at"] = time.time()
        if found:
            _LOGGER.info("MoonPay CLI resolved: %s", found)
        return found


# legacy name kept for any external caller
MOONPAY_BIN = os.path.expanduser("~/.npm-global/bin/moonpay")


# ─────────────────────────────────────────────────────────────────────────────
# CLI invocation
# ─────────────────────────────────────────────────────────────────────────────
def _run_moonpay(args: List[str], timeout: int = 120, want_json: bool = True,
                 cfg: dict = None) -> Tuple[int, Any, str]:
    """Run one MoonPay CLI command.

    The global `--json` flag is placed FIRST (before the subcommand path), which
    is how MoonPay's own automation skill invokes it, and is what makes the
    output parseable instead of YAML-ish text.

    Returns (exit_code, parsed_json_or_text, stderr_text).
    """
    binary = resolve_bin(cfg)
    if not binary:
        return -2, None, f"MoonPay CLI not found (looked for 'mp' and 'moonpay' on PATH and in {_CANDIDATE_DIRS[:3]}…)"

    argv = [binary]
    if want_json:
        argv.append("--json")
    argv += list(args)

    env = os.environ.copy()
    env["PATH"] = f"{os.path.dirname(binary)}:{env.get('PATH', '')}"
    # Never let the CLI try to prompt or open a browser mid-scan.
    env.setdefault("CI", "1")
    env.setdefault("NO_COLOR", "1")

    try:
        proc = subprocess.run(argv, capture_output=True, text=True, timeout=timeout, env=env)
    except subprocess.TimeoutExpired:
        return -1, None, f"timed out after {timeout}s"
    except FileNotFoundError:
        _BIN_CACHE["path"] = None  # force re-resolution next call
        return -2, None, f"MoonPay CLI disappeared from {binary}"
    except Exception as e:
        return -3, None, f"failed to spawn MoonPay CLI: {e}"

    out = (proc.stdout or "").strip()
    err = (proc.stderr or "").strip()
    combined = f"{out}\n{err}".strip()

    if not want_json:
        return proc.returncode, out, err

    # The CLI writes the JSON document to stdout; warnings/update notices can
    # share the stream, so find the outermost JSON value rather than assuming
    # stdout is pure.
    parsed = _extract_json(out)
    if parsed is None:
        parsed = _extract_json(combined)
    if parsed is None and out:
        return proc.returncode, out, err  # caller decides (probably YAML/text)
    return proc.returncode, parsed, err


_SIG_RE = re.compile(r"\b[1-9A-HJ-NP-Za-km-z]{64,128}\b")
_TX_RE = re.compile(r"\b[1-9A-HJ-NP-Za-km-z]{43,44}\b|\b[1-9A-HJ-NP-Za-km-z]{80,100}\b")


def _extract_json(text: str):
    """Pull the first complete JSON value out of a possibly-noisy stdout."""
    if not text:
        return None
    t = text.strip()
    if t and t[0] in "[{":
        try:
            return json.loads(t)
        except Exception:
            pass
    # scan for the first { or [ and try progressively
    for opener in ("{", "["):
        start = t.find(opener)
        if start < 0:
            continue
        closer = "}" if opener == "{" else "]"
        end = t.rfind(closer)
        if end > start:
            try:
                return json.loads(t[start:end + 1])
            except Exception:
                continue
    return None


def _parse_tx_hash(payload: Any) -> Optional[str]:
    """Extract a Solana signature from a CLI response.

    The real `token_swap` output schema is {signature, message}. Falls back to
    scanning text for a base58 blob. (The old implementation regex-matched
    43–44 chars and then required len > 60, so it could never return anything.)
    """
    if isinstance(payload, dict):
        for key in ("signature", "txSignature", "tx_hash", "txHash", "transactionId", "id", "hash"):
            v = payload.get(key)
            if isinstance(v, str) and len(v) >= 40:
                return v
        # nested: {"data": {...}} / {"transaction": {...}}
        for key in ("data", "transaction", "result"):
            sub = payload.get(key)
            if isinstance(sub, dict):
                got = _parse_tx_hash(sub)
                if got:
                    return got
    if isinstance(payload, list):
        for item in payload:
            got = _parse_tx_hash(item)
            if got:
                return got
    if isinstance(payload, str):
        m = _SIG_RE.search(payload)
        if m:
            return m.group(0)
        m = _TX_RE.search(payload)
        if m:
            return m.group(0)
    return None


def classify_error(code: int, out: Any, err: str) -> str:
    """Map a CLI failure onto a stable, human-readable reason code."""
    text = " ".join(str(x) for x in (err or "", out if isinstance(out, str) else json.dumps(out) if out else ""))
    low = text.lower()

    if code == -2 or "not found" in low and "cli" in low:
        return E_CLI_NOT_FOUND
    if code == -1 or "timed out" in low or "timeout" in low:
        return E_TIMEOUT
    if "unknown option" in low or "unknown argument" in low:
        return E_UNKNOWN_OPTION
    if "consent" in low and ("accept" in low or "required" in low):
        return E_CONSENT
    if any(k in low for k in ("unauthorized", "not authenticated", "please login", "mp login",
                              "invalid token", "credentials", "401", "session expired")):
        return E_NOT_AUTHED
    if "429" in low or "rate limit" in low or "too many requests" in low:
        return E_RATE_LIMITED
    if any(k in low for k in ("no route", "no quote", "unable to find", "not routable",
                              "no swap route", "unsupported token", "liquidity", "no market",
                              "could not find a route", "insufficient liquidity")):
        return E_NO_ROUTE
    if "insufficient" in low and ("sol" in low or "lamport" in low or "fee" in low):
        return E_INSUFFICIENT_FEE
    if "insufficient" in low or "not enough" in low or "exceeds balance" in low:
        return E_INSUFFICIENT
    if "wallet" in low and ("not found" in low or "does not exist" in low or "no such" in low):
        return E_WALLET_MISSING
    if code != 0 and not text.strip():
        return E_BAD_RESPONSE
    return E_UNKNOWN


# ─────────────────────────────────────────────────────────────────────────────
# Wallet & config helpers
# ─────────────────────────────────────────────────────────────────────────────
def get_wallet_name(cfg: dict = None) -> str:
    cfg = cfg or load_config()
    return str((cfg.get("execution") or {}).get("wallet_name") or "enzo-trading")


def get_base_token(cfg: dict = None) -> str:
    cfg = cfg or load_config()
    return str((cfg.get("execution") or {}).get("base_token") or "USDC").upper()


def base_token_address(cfg: dict = None) -> str:
    """Contract address used as the funding side of a buy."""
    if get_base_token(cfg) == "USDC":
        return USDC_MAINNET
    return MOONPAY_NATIVE_SOL


def _explanation(text: str) -> List[str]:
    """`--explanation` IS a valid optional flag on every MoonPay command
    (auto-added by the CLI's command builder), capped at 500 chars."""
    t = " ".join(str(text).split())[:480]
    return ["--explanation", t] if t else []


# ─────────────────────────────────────────────────────────────────────────────
# Balances
# ─────────────────────────────────────────────────────────────────────────────
def _balances_raw(wallet: str = None, chain: str = "solana", cfg: dict = None) -> List[dict]:
    cfg = cfg or load_config()
    wallet = wallet or get_wallet_name(cfg)
    args = ["token", "balance", "list", "--wallet", wallet, "--chain", chain]
    args += _explanation("Enzo reads wallet balances to size positions and verify fee reserve")
    code, out, err = _run_moonpay(args, timeout=30, cfg=cfg)
    if code != 0:
        _LOGGER.warning("balance list failed (rc=%s): %s [%s]", code, str(err or out)[:200],
                        classify_error(code, out, err))
        return []
    if isinstance(out, dict):
        for key in ("items", "balances", "data", "tokens"):
            v = out.get(key)
            if isinstance(v, list):
                return [x for x in v if isinstance(x, dict)]
        return [out]
    if isinstance(out, list):
        return [x for x in out if isinstance(x, dict)]
    return []


def _item_address(item: dict) -> str:
    for k in ("address", "tokenAddress", "mint", "contractAddress", "token_address"):
        v = item.get(k)
        if isinstance(v, str) and v:
            return v
    tok = item.get("token")
    if isinstance(tok, dict):
        for k in ("address", "contractAddress", "mint"):
            v = tok.get(k)
            if isinstance(v, str) and v:
                return v
    return ""


def _item_amount(item: dict) -> float:
    bal = item.get("balance")
    if isinstance(bal, dict):
        for k in ("amount", "value", "human", "formatted"):
            if bal.get(k) is not None:
                try:
                    return float(bal[k])
                except (TypeError, ValueError):
                    continue
    for k in ("amount", "balance", "value", "humanAmount"):
        if item.get(k) is not None:
            try:
                return float(item[k])
            except (TypeError, ValueError):
                continue
    return 0.0


def _item_symbol(item: dict) -> str:
    for k in ("symbol", "ticker"):
        v = item.get(k)
        if isinstance(v, str) and v:
            return v.upper()
    tok = item.get("token")
    if isinstance(tok, dict) and isinstance(tok.get("symbol"), str):
        return tok["symbol"].upper()
    return ""


def _find_balance(items: List[dict], address: str = None, symbol: str = None) -> Optional[dict]:
    """Match a balance row by contract address first, then by symbol.

    The old get_sol_balance() just took items[0] on the assumption that "SOL is
    always the first item returned" — when it isn't, the fee pre-flight reports
    a bogus 'Insufficient SOL for fees (have 0.0000)'.
    """
    if address:
        want = address.lower()
        for it in items:
            if _item_address(it).lower() == want:
                return it
    if symbol:
        want = symbol.upper()
        for it in items:
            if _item_symbol(it) == want:
                return it
    return None


def check_balance(wallet: str = None, token: str = None) -> Optional[Dict[str, Any]]:
    items = _balances_raw(wallet)
    if not items:
        return None
    if not token:
        return items[0]
    return _find_balance(items, address=token)


def get_sol_balance(wallet: str = None) -> float:
    items = _balances_raw(wallet)
    if not items:
        return 0.0
    hit = _find_balance(items, address=WRAPPED_SOL, symbol="SOL")
    if hit is None:
        hit = _find_balance(items, address=MOONPAY_NATIVE_SOL, symbol="WSOL")
    return _item_amount(hit) if hit else 0.0


def get_usdc_balance(wallet: str = None) -> float:
    items = _balances_raw(wallet)
    hit = _find_balance(items, address=USDC_MAINNET, symbol="USDC")
    return _item_amount(hit) if hit else 0.0


def get_token_balance(mint: str, wallet: str = None) -> float:
    """Balance of an arbitrary SPL token — used to size a real sell."""
    items = _balances_raw(wallet)
    hit = _find_balance(items, address=mint)
    return _item_amount(hit) if hit else 0.0


def get_wallet_snapshot(wallet: str = None, cfg: dict = None) -> Dict[str, Any]:
    """One CLI call → full picture of what the wallet can actually trade with."""
    cfg = cfg or load_config()
    items = _balances_raw(wallet, cfg=cfg)
    sol = _find_balance(items, address=WRAPPED_SOL, symbol="SOL") or \
          _find_balance(items, address=MOONPAY_NATIVE_SOL, symbol="WSOL")
    usdc = _find_balance(items, address=USDC_MAINNET, symbol="USDC")
    return {
        "ok": bool(items),
        "wallet": wallet or get_wallet_name(cfg),
        "rows": len(items),
        "sol": _item_amount(sol) if sol else 0.0,
        "usdc": _item_amount(usdc) if usdc else 0.0,
        "symbols": sorted({_item_symbol(i) for i in items if _item_symbol(i)}),
        "items": items,
    }


# ─────────────────────────────────────────────────────────────────────────────
# Quote (pre-flight — does NOT execute)
# ─────────────────────────────────────────────────────────────────────────────
def get_quote(from_token: str, to_token: str, from_amount: float,
              chain: str = "solana", cfg: dict = None) -> Optional[Dict[str, Any]]:
    """Get a swap quote WITHOUT executing.

    Real schema: from:{chain,token,amount} + to:{chain,token,amount}, so the
    flags are --from-chain/--from-token/--from-amount and --to-chain/--to-token.
    There is no --chain on this command. Amounts are HUMAN token units.
    """
    args = [
        "token", "quote",
        "--from-chain", chain,
        "--from-token", from_token,
        "--from-amount", _fmt_amount(from_amount),
        "--to-chain", chain,
        "--to-token", to_token,
    ]
    args += _explanation("Enzo pre-flight quote to confirm the pair is routable before opening a position")
    code, out, err = _run_moonpay(args, timeout=45, cfg=cfg)
    if code != 0:
        reason = classify_error(code, out, err)
        _LOGGER.info("quote failed %s→%s (%s): %s", from_token[:6], to_token[:6], reason,
                     str(err or out)[:180])
        return None
    if isinstance(out, dict):
        return out
    if isinstance(out, list) and out and isinstance(out[0], dict):
        return out[0]
    return None


def quote_ok(from_token: str, to_token: str, from_amount: float,
             chain: str = "solana", cfg: dict = None) -> Tuple[bool, str, Optional[dict]]:
    """(routable, reason_code, quote). Used by the tradability gate."""
    q = get_quote(from_token, to_token, from_amount, chain, cfg)
    if q is None:
        return False, E_NO_ROUTE, None
    return True, "ROUTABLE", q


# ─────────────────────────────────────────────────────────────────────────────
# Token research / tradability gate
# ─────────────────────────────────────────────────────────────────────────────
_GATE_LOCK = threading.Lock()


def _gate_load() -> dict:
    try:
        if os.path.exists(TRADE_GATE_PATH):
            with open(TRADE_GATE_PATH, "r", encoding="utf-8") as f:
                d = json.load(f)
                return d if isinstance(d, dict) else {}
    except Exception:
        pass
    return {}


def _gate_save(d: dict) -> None:
    try:
        os.makedirs(os.path.dirname(TRADE_GATE_PATH), exist_ok=True)
        tmp = TRADE_GATE_PATH + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(d, f, indent=2)
        os.replace(tmp, TRADE_GATE_PATH)
    except Exception:
        pass


def remember_not_routable(mint: str, reason: str, cfg: dict = None) -> None:
    """Cache a token-level failure so we stop burning rate-limit budget on it."""
    cfg = cfg or {}
    ttl = float((cfg.get("execution") or {}).get("not_routable_cooldown_sec", 3600))
    with _GATE_LOCK:
        d = _gate_load()
        d[mint] = {"reason": reason, "ts": time.time(), "until": time.time() + ttl}
        # keep the file small
        if len(d) > 500:
            d = dict(sorted(d.items(), key=lambda kv: kv[1].get("ts", 0), reverse=True)[:500])
        _gate_save(d)


def routable_block(mint: str) -> Optional[dict]:
    with _GATE_LOCK:
        d = _gate_load()
        e = d.get(mint)
        if not e:
            return None
        if float(e.get("until", 0)) < time.time():
            d.pop(mint, None)
            _gate_save(d)
            return None
        return e


def clear_gate(mint: str = None) -> int:
    with _GATE_LOCK:
        d = _gate_load()
        if mint:
            existed = 1 if d.pop(mint, None) else 0
            _gate_save(d)
            return existed
        n = len(d)
        _gate_save({})
        return n


def token_retrieve(mint: str, chain: str = "solana", cfg: dict = None) -> Optional[dict]:
    """`mp token retrieve` works for ANY mint — MoonPay's research endpoints are
    not limited to the trending list."""
    args = ["token", "retrieve", "--token", mint, "--chain", chain]
    args += _explanation("Enzo verifies the token is known and routable before committing capital")
    code, out, err = _run_moonpay(args, timeout=45, cfg=cfg)
    if code != 0:
        return None
    return out if isinstance(out, (dict, list)) else None


def check_tradable(mint: str, amount_usd: float, cfg: dict = None,
                   use_quote: bool = True) -> Dict[str, Any]:
    """Pre-flight gate run BEFORE a position is opened.

    This is what turns "the bot opened a position then immediately rolled it
    back because the swap failed" into "the bot never opened it and told you
    exactly why". Result is recorded in data/enzo-trade-gate.json.
    """
    cfg = cfg or load_config()
    blocked = routable_block(mint)
    if blocked:
        return {"tradable": False, "reason": blocked.get("reason", E_NO_ROUTE),
                "cached": True, "detail": "cooldown active"}

    from_token = base_token_address(cfg)
    chain = str(cfg.get("chain") or "solana")
    if get_base_token(cfg) == "USDC":
        from_amount = float(amount_usd)
    else:
        sol_px = _sol_price(cfg)
        from_amount = float(amount_usd) / sol_px if sol_px else 0.0

    if from_amount <= 0:
        return {"tradable": False, "reason": E_BELOW_MIN, "detail": "computed amount is zero"}

    if not use_quote:
        info = token_retrieve(mint, chain, cfg)
        ok = info is not None
        if not ok:
            remember_not_routable(mint, E_NO_ROUTE, cfg)
        return {"tradable": ok, "reason": "KNOWN" if ok else E_NO_ROUTE, "info": info}

    ok, reason, quote = quote_ok(from_token, mint, from_amount, chain, cfg)
    if not ok:
        remember_not_routable(mint, reason, cfg)
        return {"tradable": False, "reason": reason, "quote": None}

    out_amt = None
    if isinstance(quote, dict):
        for k in ("toAmount", "outAmount", "expectedOutput", "to_amount"):
            if quote.get(k) is not None:
                out_amt = quote.get(k)
                break
    return {"tradable": True, "reason": "ROUTABLE", "quote": quote, "expected_out": out_amt}


# ─────────────────────────────────────────────────────────────────────────────
# Transaction status
# ─────────────────────────────────────────────────────────────────────────────
def get_tx_status(tx_hash: str, chain: str = "solana", cfg: dict = None) -> Optional[Dict[str, Any]]:
    """`transaction retrieve` takes --transactionId (not --id, and no --chain)."""
    if not tx_hash:
        return None
    args = ["transaction", "retrieve", "--transactionId", str(tx_hash)]
    args += _explanation("Enzo confirms the swap landed on-chain before marking a position live")
    code, out, err = _run_moonpay(args, timeout=45, cfg=cfg)
    if code != 0:
        _LOGGER.info("tx status failed for %s: %s", str(tx_hash)[:12], classify_error(code, out, err))
        return None
    return out if isinstance(out, (dict, list)) else None


# ─────────────────────────────────────────────────────────────────────────────
# Optional warm local API transport (`mp serve`)
# ─────────────────────────────────────────────────────────────────────────────
def _local_api_call(tool: str, payload: dict, cfg: dict = None) -> Optional[Any]:
    """POST http://127.0.0.1:8787/api/tools/<tool>.

    Off by default (execution.use_local_api) so an OpenClaw workspace has one
    less long-lived process to supervise. Enable it if CLI spawn cost matters.
    """
    cfg = cfg or load_config()
    ex = cfg.get("execution") or {}
    if not ex.get("use_local_api"):
        return None
    base = str(ex.get("local_api_url") or "http://127.0.0.1:8787").rstrip("/")
    timeout = float(ex.get("local_api_timeout_sec") or 45)
    try:
        req = urllib.request.Request(
            f"{base}/api/tools/{tool}",
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json", "Accept": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except Exception as e:
        _LOGGER.debug("local API %s unavailable (%s) — falling back to CLI", tool, e)
        return None


# ─────────────────────────────────────────────────────────────────────────────
# Core swap
# ─────────────────────────────────────────────────────────────────────────────
def execute_swap(from_token: str, to_token: str, from_amount: float,
                 wallet: str = None, explanation: str = None,
                 chain: str = "solana", timeout: int = 180,
                 cfg: dict = None, direction: str = "buy") -> Dict[str, Any]:
    """Execute a token swap via the MoonPay CLI.

    `from_amount` is in HUMAN token units (5.0 = five USDC, 0.1 = 0.1 SOL).

    `direction` is "buy" or "sell". The min/max trade guards apply to BUYS only:
    `max_trade_usd` caps how much capital we deploy, but it must never block an
    exit. A sell of 1,000,000 memecoin units is not "$180M of exposure" — the
    notional is unknown from the raw amount alone, and refusing to sell would
    trap the bot in a position it could not close.
    """
    cfg = cfg or load_config()
    ex = cfg.get("execution") or {}
    wallet = wallet or get_wallet_name(cfg)
    explanation = explanation or "Enzo trading bot position entry on Solana"

    def fail(reason: str, detail: str = "", **extra) -> Dict[str, Any]:
        # `reason` holds the machine code and `detail` the human sentence. The
        # callers in engine.py and exit_monitor.py read `reason_code`, so it is
        # emitted explicitly as well — without it the Telegram failure alert
        # arrived with an empty code and notify_buy_failed() could not look up
        # its "Fix:" hint, which is the same class of silent contract mismatch
        # that made buy failures invisible in the first place.
        res = {"ok": False, "reason": reason, "reason_code": reason,
               "detail": detail, "tx_hash": None,
               "from_token": from_token, "to_token": to_token,
               "from_amount": from_amount, "wallet": wallet}
        res.update(extra)
        return res

    if from_amount <= 0:
        return fail(E_BELOW_MIN, f"amount is {from_amount}")

    usd_equiv = None
    if str(direction).lower() == "buy":
        min_trade = float(ex.get("min_trade_usd", 1.0))
        max_trade = float(ex.get("max_trade_usd", 0.0) or 0.0)
        usd_equiv = float(from_amount) if from_token == USDC_MAINNET else float(from_amount) * _sol_price(cfg)
        if usd_equiv < min_trade:
            return fail(E_BELOW_MIN, f"${usd_equiv:.4f} < execution.min_trade_usd ${min_trade:.2f}")
        if max_trade and usd_equiv > max_trade:
            return fail(E_ABOVE_MAX, f"${usd_equiv:.2f} > execution.max_trade_usd ${max_trade:.2f}")

    # ── Pre-flight: fee reserve ────────────────────────────────────────────
    sol_bal = get_sol_balance(wallet)
    reserve = float(ex.get("sol_fee_reserve", 0.02))
    if sol_bal < reserve:
        return fail(E_INSUFFICIENT_FEE,
                    f"have {sol_bal:.6f} SOL, need ≥{reserve:.4f} SOL for fees",
                    sol_balance=sol_bal)

    # ── Pre-flight: is this pair routable at this size? ────────────────────
    quote = get_quote(from_token, to_token, from_amount, chain, cfg)
    if not quote:
        reason = E_NO_ROUTE
        if str(direction).lower() == "buy":
            # Buying the token that cannot be routed: remember it so discovery
            # stops paying rate-limit budget to re-analyse the same dead end.
            remember_not_routable(to_token, reason, cfg)
            return fail(reason,
                        "MoonPay/swaps.xyz returned no route for this pair at this size. "
                        "Fresh pump.fun tokens still on the bonding curve are not routable "
                        "until they graduate to a DEX.",
                        quote=None)
        # SELLING: never abandon an exit because the quote endpoint hiccuped.
        # The swap call itself is the authority on whether a route exists.
        _LOGGER.warning("no quote for SELL %s — attempting the swap anyway (exits are never gated)",
                        str(from_token)[:8])

    # ── Execute ────────────────────────────────────────────────────────────
    # Optional warm-process transport first; CLI is the always-available path.
    local = _local_api_call("token_swap", {
        "wallet": wallet, "chain": chain,
        "from": {"token": from_token, "amount": float(from_amount)},
        "to": {"token": to_token, "amount": None},
        "explanation": explanation[:480],
    }, cfg)
    if isinstance(local, dict) and (local.get("signature") or local.get("data", {}).get("signature")):
        sig = _parse_tx_hash(local)
        _LOGGER.info("swap executed via local API: %s", sig)
        return {"ok": True, "reason": "swap_executed", "tx_hash": sig, "transport": "local_api",
                "quote": quote, "wallet": wallet, "from_token": from_token,
                "to_token": to_token, "from_amount": from_amount, "raw": local}

    args = [
        "token", "swap",
        "--wallet", wallet,
        "--chain", chain,
        "--from-token", from_token,
        "--from-amount", _fmt_amount(from_amount),
        "--to-token", to_token,
    ]
    # NOTE: deliberately NO `--yes`. It is not an option of `token swap` and
    # commander aborts the whole command on an unknown option. `--explanation`
    # IS valid (auto-added to every command, max 500 chars).
    args += _explanation(explanation)

    attempts = max(1, int(ex.get("retry_attempts", 2)))
    last_code, last_out, last_err = None, None, None
    for attempt in range(1, attempts + 1):
        last_code, last_out, last_err = _run_moonpay(args, timeout=timeout, cfg=cfg)
        reason = classify_error(last_code, last_out, last_err)
        sig = _parse_tx_hash(last_out) if last_code == 0 else None

        if last_code == 0 and sig:
            _LOGGER.info("✓ swap executed: %s → %s | %s %s | sig=%s",
                         from_token[:6], to_token[:6], _fmt_amount(from_amount),
                         "USDC" if from_token == USDC_MAINNET else "SOL", sig[:16])
            return {"ok": True, "reason": "swap_executed", "tx_hash": sig,
                    "transport": "cli", "attempt": attempt, "quote": quote,
                    "wallet": wallet, "from_token": from_token, "to_token": to_token,
                    "from_amount": from_amount, "message": _message_of(last_out),
                    "raw": last_out}

        if last_code == 0 and not sig:
            # Command succeeded but we could not find a signature — treat as
            # success-with-warning rather than rolling back a real on-chain trade.
            _LOGGER.warning("swap reported success but no signature was parseable: %s",
                            str(last_out)[:300])
            return {"ok": True, "reason": "swap_executed_no_signature", "tx_hash": None,
                    "transport": "cli", "attempt": attempt, "quote": quote,
                    "wallet": wallet, "from_token": from_token, "to_token": to_token,
                    "from_amount": from_amount, "message": _message_of(last_out),
                    "raw": last_out}

        # Retry only on transient problems
        if reason in (E_TIMEOUT, E_RATE_LIMITED) and attempt < attempts:
            wait = 2.0 * attempt if reason == E_TIMEOUT else 20.0
            _LOGGER.warning("swap attempt %d/%d failed (%s) — retrying in %.0fs",
                            attempt, attempts, reason, wait)
            time.sleep(wait)
            continue
        break

    reason = classify_error(last_code, last_out, last_err)
    detail = str(last_err or last_out or "")[:400]
    if reason in _TOKEN_LEVEL_ERRORS:
        remember_not_routable(to_token, reason, cfg)
    _LOGGER.error("✗ swap failed (%s): %s", reason, detail)
    return fail(reason, detail, quote=quote, exit_code=last_code, raw=last_out)


def _message_of(payload: Any) -> str:
    if isinstance(payload, dict):
        for k in ("message", "summary", "text"):
            if isinstance(payload.get(k), str):
                return payload[k]
        sub = payload.get("data")
        if isinstance(sub, dict):
            return _message_of(sub)
    return ""


_SOL_PRICE_CACHE = {"price": 0.0, "ts": 0.0}


def _sol_price(cfg: dict = None) -> float:
    """SOL/USD for sizing when the base token is SOL. Cached 60 s."""
    if time.time() - _SOL_PRICE_CACHE["ts"] < 60 and _SOL_PRICE_CACHE["price"] > 0:
        return _SOL_PRICE_CACHE["price"]
    price = 0.0
    try:
        from enzo.providers import gmgn
        price = float(gmgn.sol_price_usd() or 0.0)
    except Exception:
        price = 0.0
    if price <= 0:
        price = 180.0  # conservative fallback, matches gmgn.sol_price_usd()
    _SOL_PRICE_CACHE.update({"price": price, "ts": time.time()})
    return price


def sol_price_usd() -> float:
    return _sol_price()


# ─────────────────────────────────────────────────────────────────────────────
# Buy / Sell wrappers
# ─────────────────────────────────────────────────────────────────────────────
def buy_token(mint: str, amount_usd: float, wallet: str = None,
              entry_price: float = None, explanation: str = None,
              cfg: dict = None) -> Dict[str, Any]:
    """Buy a memecoin with the configured base token (USDC by default).

    `amount_usd` is a USD notional; it is converted to HUMAN token units of the
    base token here, exactly once. Nothing downstream multiplies by decimals.
    """
    cfg = cfg or load_config()
    ex = cfg.get("execution") or {}
    wallet = wallet or get_wallet_name(cfg)
    base = get_base_token(cfg)

    min_trade = float(ex.get("min_trade_usd", 1.0))
    if float(amount_usd) < min_trade:
        return {"ok": False, "reason": E_BELOW_MIN, "reason_code": E_BELOW_MIN,
                "tx_hash": None,
                "detail": f"Position size ${float(amount_usd):.2f} below execution.min_trade_usd ${min_trade:.2f}. "
                          f"Equity is too small for the configured risk band.",
                "amount_usd": amount_usd, "base_token": base}

    if base == "USDC":
        from_token = USDC_MAINNET
        from_amount = float(amount_usd)          # USDC ≈ 1 USD, human units
    else:
        from_token = MOONPAY_NATIVE_SOL
        px = float(entry_price) if entry_price and float(entry_price) > 0 else _sol_price(cfg)
        from_amount = float(amount_usd) / px if px > 0 else 0.0

    explanation = explanation or f"Enzo BUY {str(mint)[:8]} on Solana (${float(amount_usd):.2f})"
    result = execute_swap(from_token=from_token, to_token=mint, from_amount=from_amount,
                          wallet=wallet, explanation=explanation,
                          chain=str(cfg.get("chain") or "solana"), cfg=cfg,
                          direction="buy")
    result["amount_usd"] = float(amount_usd)
    result["base_token"] = base
    result["mint"] = mint
    return result


def sell_token(mint: str, amount_spl: float = None, wallet: str = None,
               to_token: str = None, explanation: str = None,
               cfg: dict = None) -> Dict[str, Any]:
    """Sell a memecoin back to the base token.

    If `amount_spl` is omitted or <= 0, the REAL on-chain balance is read from
    the wallet and sold in full — the ledger's `amount` is a paper figure and
    can diverge from what the wallet actually holds after a partial fill.
    """
    cfg = cfg or load_config()
    wallet = wallet or get_wallet_name(cfg)
    base = get_base_token(cfg)

    if to_token is None:
        to_token = USDC_MAINNET if base == "USDC" else MOONPAY_NATIVE_SOL
    elif str(to_token).upper() == "USDC":
        to_token = USDC_MAINNET
    elif str(to_token).upper() in ("SOL", "WSOL"):
        to_token = MOONPAY_NATIVE_SOL

    real_bal = get_token_balance(mint, wallet)
    want = float(amount_spl) if amount_spl and float(amount_spl) > 0 else 0.0
    if real_bal > 0:
        # never try to sell more than the wallet holds
        sell_amount = min(want, real_bal) if want > 0 else real_bal
    else:
        sell_amount = want
        if sell_amount <= 0:
            return {"ok": False, "reason": E_INSUFFICIENT,
                    "reason_code": E_INSUFFICIENT, "tx_hash": None,
                    "detail": f"No on-chain balance for {str(mint)[:8]}… and no ledger amount to fall back on.",
                    "base_token": base, "mint": mint}

    if sell_amount <= 0:
        return {"ok": False, "reason": E_INSUFFICIENT,
                "reason_code": E_INSUFFICIENT, "tx_hash": None,
                "detail": "Computed sell amount is zero.", "base_token": base, "mint": mint}

    explanation = explanation or f"Enzo SELL {str(mint)[:8]} on Solana"
    result = execute_swap(from_token=mint, to_token=to_token, from_amount=sell_amount,
                          wallet=wallet, explanation=explanation,
                          chain=str(cfg.get("chain") or "solana"), cfg=cfg,
                          direction="sell")
    result["base_token"] = base
    result["mint"] = mint
    result["ledger_amount"] = want
    result["wallet_balance"] = real_bal
    return result


# ─────────────────────────────────────────────────────────────────────────────
# Live capital sync (execution.capital_source: wallet)
# ─────────────────────────────────────────────────────────────────────────────
_CAPITAL_CACHE = {"total_usd": 0.0, "usdc": 0.0, "sol": 0.0, "ts": 0.0, "ok": False, "detail": ""}


def sync_wallet_capital(force: bool = False, cfg: dict = None) -> Dict[str, Any]:
    """Deployable capital read from the real wallet, in USD.

    usdc + (sol - fee_reserve) * sol_price, cached for capital_sync_ttl_sec.
    This replaces the static `initial_capital` (which sat at $2.06 and made
    every position size $0.04 — below min_trade_usd — so no trade could ever
    be executed even on a perfect BUY signal).
    """
    cfg = cfg or load_config()
    ex = cfg.get("execution") or {}
    ttl = float(ex.get("capital_sync_ttl_sec", 60))
    if not force and _CAPITAL_CACHE["ok"] and (time.time() - _CAPITAL_CACHE["ts"]) < ttl:
        return dict(_CAPITAL_CACHE)

    wallet = get_wallet_name(cfg)
    snap = get_wallet_snapshot(wallet, cfg)
    if not snap.get("ok"):
        _CAPITAL_CACHE.update({"ok": False, "ts": time.time(),
                               "detail": "balance list returned nothing (CLI/auth/wallet problem)"})
        return dict(_CAPITAL_CACHE)

    sol_px = _sol_price(cfg)
    reserve = float(ex.get("sol_fee_reserve", 0.02))
    deployable_sol = max(0.0, float(snap["sol"]) - reserve)
    total = float(snap["usdc"]) + deployable_sol * sol_px

    _CAPITAL_CACHE.update({
        "ok": True, "ts": time.time(),
        "usdc": round(float(snap["usdc"]), 6),
        "sol": round(float(snap["sol"]), 9),
        "sol_price": round(sol_px, 2),
        "sol_reserve": reserve,
        "deployable_sol": round(deployable_sol, 9),
        "total_usd": round(total, 2),
        "wallet": wallet,
        "symbols": snap.get("symbols", []),
        "detail": "",
    })
    _LOGGER.info("Wallet capital synced: $%.2f deployable (USDC $%.2f + %.4f SOL @ $%.2f, reserve %.4f SOL)",
                 total, snap["usdc"], deployable_sol, sol_px, reserve)
    return dict(_CAPITAL_CACHE)


def cached_capital() -> Dict[str, Any]:
    return dict(_CAPITAL_CACHE)


# ─────────────────────────────────────────────────────────────────────────────
# Readiness
# ─────────────────────────────────────────────────────────────────────────────
def is_ready(cfg: dict = None) -> Tuple[bool, str]:
    """Full pre-flight for LIVE trading. Returns (ready, human_readable_reason)."""
    try:
        cfg = cfg or load_config()
    except Exception as e:
        return False, f"config could not be loaded: {e}"

    ex = cfg.get("execution") or {}

    if cfg.get("paper_mode", True):
        return False, f"{E_PAPER_BLOCKED}: paper_mode is true in config/enzo-config.yaml — real trading blocked"

    binary = resolve_bin(cfg, force=True)
    if not binary:
        return False, f"{E_CLI_NOT_FOUND}: could not find 'mp' or 'moonpay'. Install with: npm i -g @moonpay/cli"

    wallet = get_wallet_name(cfg)
    code, out, err = _run_moonpay(["wallet", "list"], timeout=25, cfg=cfg)
    if code != 0:
        reason = classify_error(code, out, err)
        if reason == E_NOT_AUTHED:
            return False, (f"{E_NOT_AUTHED}: run 'mp login --email you@example.com' then "
                           f"'mp verify --email you@example.com --code <code>'")
        if reason == E_CONSENT:
            return False, f"{E_CONSENT}: run 'mp consent accept' once"
        return False, f"wallet list failed ({reason}): {str(err or out)[:160]}"

    blob = json.dumps(out) if not isinstance(out, str) else out
    if wallet not in blob:
        return False, (f"{E_WALLET_MISSING}: wallet '{wallet}' is not in 'mp wallet list'. "
                       f"Create it with: mp wallet create --name {wallet}")

    snap = get_wallet_snapshot(wallet, cfg)
    if not snap.get("ok"):
        return False, f"{E_BAD_RESPONSE}: could not read balances for wallet '{wallet}'"

    reserve = float(ex.get("sol_fee_reserve", 0.02))
    if float(snap["sol"]) < reserve:
        return False, (f"{E_INSUFFICIENT_FEE}: wallet has {snap['sol']:.6f} SOL, "
                       f"need ≥{reserve:.4f} SOL for transaction fees")

    cap = sync_wallet_capital(force=True, cfg=cfg)
    min_trade = float(ex.get("min_trade_usd", 1.0))
    if not cap.get("ok"):
        return False, f"{E_BAD_RESPONSE}: capital sync failed — {cap.get('detail')}"
    if float(cap.get("total_usd", 0.0)) < min_trade:
        return False, (f"{E_INSUFFICIENT}: deployable capital ${cap.get('total_usd', 0.0):.2f} "
                       f"is below execution.min_trade_usd ${min_trade:.2f} — no position can be sized")

    return True, (f"Ready — wallet '{wallet}', deployable ${cap.get('total_usd', 0.0):.2f} "
                  f"(USDC ${cap.get('usdc', 0.0):.2f} + {cap.get('deployable_sol', 0.0):.4f} SOL), "
                  f"fee reserve {snap['sol']:.4f} SOL, CLI {binary}")


def preflight_report(cfg: dict = None) -> Dict[str, Any]:
    """Structured readiness report for `enzoctl doctor` / the dashboard."""
    cfg = cfg or {}
    try:
        cfg = cfg or load_config()
    except Exception as e:
        return {"ready": False, "reason": f"config error: {e}", "checks": []}

    checks = []

    def add(name, ok, detail):
        checks.append({"name": name, "ok": bool(ok), "detail": detail})

    binary = resolve_bin(cfg, force=True)
    add("moonpay_cli_installed", binary, binary or "not found (npm i -g @moonpay/cli)")

    paper = bool(cfg.get("paper_mode", True))
    add("mode", True, "PAPER (no real trades)" if paper else "LIVE (real trades)")

    wallet = get_wallet_name(cfg)
    if binary:
        code, out, err = _run_moonpay(["wallet", "list"], timeout=25, cfg=cfg)
        if code == 0:
            blob = json.dumps(out) if not isinstance(out, str) else out
            add("wallet_exists", wallet in blob, f"'{wallet}' " + ("found" if wallet in blob else "NOT in wallet list"))
            snap = get_wallet_snapshot(wallet, cfg)
            add("balances_readable", snap.get("ok"),
                f"{snap.get('rows', 0)} rows, SOL {snap.get('sol', 0):.6f}, USDC {snap.get('usdc', 0):.4f}")
            if not paper:
                cap = sync_wallet_capital(force=True, cfg=cfg)
                add("capital_sufficient", cap.get("ok") and float(cap.get("total_usd", 0)) >=
                    float((cfg.get("execution") or {}).get("min_trade_usd", 1.0)),
                    f"deployable ${cap.get('total_usd', 0.0):.2f}" + (f" — {cap.get('detail')}" if cap.get('detail') else ""))
        else:
            reason = classify_error(code, out, err)
            add("moonpay_authenticated", reason not in (E_NOT_AUTHED, E_CONSENT),
                reason + ": " + str(err or out)[:140])
    ready, why = is_ready(cfg) if not paper else (True, "paper mode — execution checks skipped")
    return {"ready": ready, "reason": why, "mode": "PAPER" if paper else "LIVE",
            "wallet": wallet, "binary": binary, "checks": checks}
