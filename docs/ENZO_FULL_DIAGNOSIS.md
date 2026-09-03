# ENZO — Full System Diagnosis (Evidence-Based)

Date: 2026-09-03 · Branch: `arena/01a067ae-enzo-ai-v2` · Base commit: `3322c75`

Every finding below was **reproduced or verified against a primary source**
(the running code, the on-disk data/logs, or the decompiled `@moonpay/cli@1.96.0`
package). Severity: `[C]` critical · `[H]` high · `[M]` medium · `[L]` low.

---

## 0. How the bot is wired today (architecture map)

```
enzo.py  (argparse CLI: start | scan | loop | status | trades | dashboard | serve | botctl | learn | pause | resume)
   │
   ├─ cmd_start ──┬─ thread 1: ui/serve.run_server(0.0.0.0:8077)     ← HTTP dashboard + REST API
   │              ├─ thread 2: ui/botctl.TelegramBotListener          ← Telegram polling
   │              └─ main    : core/engine.run_loop(interval)         ← trading engine
   │                             └─ thread 3: execution/exit_monitor  ← SL/TP/trailing/time exits
   │
   ├─ core/engine      discovery → pre-screen → rank → deep 6-axis analysis → open position → execute
   ├─ providers/gmgn   shells out to `gmgn-cli`  (info/security/holders/traders/kline/market lists)
   ├─ providers/pump   PumpDev WebSocket + pump.fun HTTP + PumpPortal + DexScreener fallbacks
   ├─ analyzers/*      security · wallet · dev · momentum · market_structure · liquidity
   ├─ execution/portfolio  SQLite ledger, sizing, risk halts, staged exits
   ├─ execution/executor   MoonPay CLI → swaps.xyz → Solana
   ├─ core/db          SQLite (WAL) — portfolio_state, open_positions, closed_trades, rate_limiter, cache_store
   ├─ core/learn       self-calibrating confidence bias from closed trades
   ├─ core/audit       append-only JSONL: decisions + UI/system events
   └─ ui/dashboard     one 1,180-line f-string → data/enzo-dashboard.html
```

Runtime data: `data/enzo.db` (source of truth) + `data/enzo-portfolio.json` (sync cache)
+ `data/enzo-learning.json` + `data/enzo-audit.jsonl` + `data/enzo-dashboard.html`.
Control: `config/enzo-control.json` (`paused`), `config/enzo-watchlist.json`,
`config/enzo-secrets.json`, `config/enzo-config.yaml`.

---

## A. "The bot never analyses coins and never enters a trade"

### A1 `[C]` PyYAML is missing → the **entire config file is silently ignored**

`enzo/core/config.py`:

```python
try:
    import yaml
except ImportError:
    yaml = None
...
if os.path.exists(target_path) and yaml is not None:   # ← False when yaml is missing
    ...
return { ...hardcoded fallback... }                    # ← this runs instead
```

Reproduced:

| | on disk (`enzo-config.yaml`) | actually loaded |
|---|---|---|
| top-level sections | **23** | **6** |
| `paper_mode` | `false` | **`True`** |
| `execution` | present | **absent** |
| `data_sources` | present | **absent** |

Sections dropped: `exit_monitor`, `scam_detection`, `entry_strategy`,
`scoring_weights`, `logging`, `notifications`, `data_sources`, `discovery`,
`pump_monitor`, `wallet_behavior`, `dev_behavior`, `market_structure`,
`position_sizing`, `learning`, `cache`, `chain`, `execution`.

Knock-on effects:
- `paper_mode=True` → `executor.is_ready()` returns
  `"paper_mode is still enabled in config — real trading blocked"`. **Live trading can never run.**
- `position_sizing.confidence_bands` ignored → flat 2.5 % risk instead of 1–5 %.
- `market_analysis.min_holders` becomes 20 (you set 10) and `min_buy_pressure` 45 (you set 40)
  → **stricter gates than you configured → more rejections.**
- `data_sources.gmgn.discovery` falls back to 3 categories (drops `kol`).
- Every threshold you ever tuned in the YAML has **no effect at all**.

### A2 `[C]` `config/enzo-control.json` says `{"paused": true}` → every scan is skipped

`engine._scan_once_unlocked()` returns `[]` on line 1. Confirmed in
`data/logs/enzo.log` — thousands of consecutive
`ENZO is paused via botctl — skipping scan.` entries, including the last run.

### A3 `[C]` `websockets` is not installed → discovery tier 1 is dead → 0 candidates

`data/logs/enzo.log`:
```
[WARNING] [enzo.pump] Python 'websockets' package not installed. PumpDev real-time streaming inactive.
[INFO]    [enzo.engine] Discovered 0 pre-screened candidate tokens.
```
Fallbacks don't save it: `frontend-api-v2.pump.fun` is deprecated, PumpPortal returns
no mcap, and the DexScreener tier deliberately sets `marketCap = 0.0` →
`screen_pump_card()` rejects 100 % of them (`mcap $0 < min $1,000`).

### A4 `[H]` The watchlist can never load — key mismatch

| file | code |
|---|---|
| `config/enzo-watchlist.json` → `{"watchlist": [...]}` | `engine.load_watchlist()` → `data.get("mints", [])` |

So the highest-priority candidate source (`rank_score = 1000`) is permanently empty,
even when you fill the file in by hand.

### A5 `[C]` The equity base is **$2.06** → position size **$0.04** → below `min_trade_usd`

`data/enzo.db` → `portfolio_state.initial_capital = 2.06`.

```
risk 1 % × $2.06 = $0.0206   →  / stop 0.5  →  size = $0.0412
max_exposure 30 % × $2.06    = $0.618       →  size = min(...) = $0.0412
executor.min_trade_usd       = $1.00        →  "Position size $0.04 below minimum $1.00"
```

Even a perfect BUY signal cannot be executed. Design flaw underneath:
`initial_capital` is a **static number never synced with the real on-chain balance**,
yet it is the base for all sizing, exposure and drawdown math.

### A6 `[H]` GMGN market data resolves to `$0` for most tokens → hard gates reject everything

From `data/enzo-audit.jsonl.bak.20260902_183915` (14,528 rows):

| rejected signal | count |
|---|---|
| `Market cap $0 < min $1,000` | 1,649 |
| `Liquidity $0 < min $150` | 1,645 |
| `Volume24h $0 < min $50` | 1,635 |
| `Holders 3.0 < min 10.0` | 16 |
| `SECURITY: HONEYPOT` | 15 |
| `SECURITY: MINT_AUTHORITY_ACTIVE` / `FREEZE_AUTHORITY_ACTIVE` | 14 / 14 |

`data/logs/engine.log` also shows `Scanned UNKNOWN (DoqTZhaB...)` — the symbol is not
resolving either, i.e. `gmgn.get_market_data()` / `_norm_list_item()` do not match the
real `gmgn-cli` payload shape. (The 555 historical `BUY` rows are synthetic fixtures —
mints `P1`, `P2`, `G1`, `G2` — not real trades.)

### A7 `[M]` Discovery errors are logged at `DEBUG` → invisible

```python
except Exception as e:
    _LOGGER.debug(f"PumpDev discovery error: {e}")   # default level is INFO
```
Both the PumpDev and GMGN discovery blocks fail silently. Nothing reaches the log,
the dashboard or Telegram.

---

## B. "MoonPay CLI won't accept any coin that isn't on its trending list"

Verified by downloading `@moonpay/cli@1.96.0` from npm and reading its command builder
(`dist/index.js` → `be()` / `ne()` / `Ne()`) plus the bundled `skills/*.md`.

**How the CLI really builds flags:** options are generated from each tool's schema.
Nested objects are flattened with a dash (`from.token` → `--from-token`).
Every command additionally gets one optional `--explanation`.
The only global flag is `--json` ("Output as JSON instead of YAML"); **default output is YAML-ish text.**

Official `token_swap` schema:
```
wallet (required) · chain (required)
from.token (required) → --from-token     from.amount (nullable) → --from-amount
to.token   (required) → --to-token       to.amount   (nullable) → --to-amount
output: { signature, message }
```
Official `token_quote` schema:
```
from.chain (required) → --from-chain     from.token (required) → --from-token     from.amount → --from-amount
to.chain   (required) → --to-chain       to.token   (required) → --to-token       to.amount   → --to-amount
```

### B1 `[C]` `--yes` is not a real option → the command dies before it does anything

`executor.execute_swap()` appends `"--yes"`. Reproduced with commander 12.1.0
(the exact version the CLI pins):

```
$ node t.mjs token swap --wallet w --chain solana --from-token A --from-amount 50 --to-token B --explanation "x" --yes
error: unknown option '--yes'
exit=1
```
(`--explanation` **is** valid — it's auto-added to every command. Only `--yes` breaks it.)

### B2 `[C]` `token quote` is called with the wrong flags → quote always fails → swap aborts pre-flight

Bot sends: `token quote --from-token A --to-token B --from-amount N --chain solana`
Real flags: `--from-chain` + `--to-chain` are **required**, and `--chain` does not exist.

→ exit 1 → `get_quote()` returns `None` → `execute_swap()` returns
`"Failed to get Jupiter quote — token pair may have no liquidity"`.

**This is the actual source of the "only trending coins work" impression:** the same
liquidity error is produced for *every* token, because the quote call is malformed,
not because MoonPay rejected the coin.

### B3 `[C]` Amounts are multiplied by 10^decimals, but the CLI wants human units

```python
def _to_smallest_unit(token_address, human_amount):
    return int(human_amount * (10 ** decimals))     # $50 USDC → 50000000
```
Official schema text: *"Amount to sell in token units"*; official skill example:
`--from-amount 5` for 5 USDC, `--from-amount 0.1` for 0.1 SOL, and
*"Builds unsigned transaction via swaps.xyz (**handles decimal conversion**)"*.
So $50 is sent as 50,000,000 USDC.

### B4 `[H]` Output is parsed as JSON, but the CLI emits YAML unless `--json` is passed

`get_quote()`, `get_tx_status()` and `execute_swap()` never pass `--json`
(only `_list_balances()` does) → `json.loads()` on YAML text → `JSONDecodeError` → `None`.
Official pattern: `mp --json token swap ...`.

### B5 `[H]` `_parse_tx_hash()` can never return a hash

```python
matches = re.findall(r"[A-HJ-NP-Za-km-z]{43,44}", line)   # length is always 43 or 44
for m in matches:
    if len(m) > 60:          # ← impossible
        return m
return None
```
So even a successful swap reports `tx_hash = None`. The real field is `signature`.

### B6 `[M]` `transaction retrieve` uses non-existent flags

Bot: `transaction retrieve --chain solana --id <sig>`.
Real schema: `{ transactionId (required), explanation }` → `--transactionId`.

### B7 `[H]` `MOONPAY_BIN` is hardcoded to one npm prefix

```python
MOONPAY_BIN = os.path.expanduser("~/.npm-global/bin/moonpay")
if not os.path.exists(MOONPAY_BIN):
    return False, f"MoonPay CLI not found at {MOONPAY_BIN}"
```
Any other prefix (`/usr/local/bin`, `~/.local/bin`, nvm, pnpm, a `mp`-only install)
→ `is_ready()` fails. Official guidance is `MP="$(which mp)"`.

### B8 `[M]` `get_sol_balance()` assumes `items[0]` is SOL

```python
return float(items[0].get("balance", {}).get("amount", 0))   # "SOL is always the first item"
```
If the wallet's first balance row is a memecoin, the fee check produces a bogus
`Insufficient SOL for fees (have 0.0000, need ≥0.005)`.

### B9 `[M]` The one *genuine* MoonPay constraint

Swaps are routed by **swaps.xyz**. A token needs a real DEX market:
- pump.fun tokens **still on the bonding curve** → usually no route → real "no quote".
- **graduated** tokens (PumpSwap / Raydium / Meteora / Orca) → routable.

Also: `mp consent accept` + `mp login` / `mp verify` are prerequisites, and the rate
limit is **5 req/min anonymous, 60 req/min authenticated**.
`mp token retrieve` / `token check` / `token search` work for **any** mint —
MoonPay does *not* restrict research to its trending list.

---

## C. "The dashboard is broken and its data is stale"

### C1 `[C]` `dashboard.generate()` raises `NameError` on **every** call

```python
# enzo/ui/dashboard.py line 396 (inside the f-string)
<p>AUTONOMOUS SOLANA MEMECOIN • {{"PAPER MODE • " if paper else ...}}JUPITER + MOONPAY • WALLET: {wallet_name}</p>
```
`wallet_name` is never defined in `generate()`. Reproduced:
```
NameError: name 'wallet_name' is not defined
```
An AST scan of all 39 interpolation points in the 1,180-line template confirms
`wallet_name` is the **only** undefined name.

### C2 `[C]` The crash is swallowed → the server serves a stale artifact

`serve.do_GET("/")`:
```python
try:
    dashboard.generate()
    ...serve fresh file...
except Exception:
    pass
return super().do_GET()      # → serves data/enzo-dashboard.html as-is
```
`data/enzo-dashboard.html` (47 KB) is from an **older code revision** — it does not
contain the `v2.5 PRO` / `AUTONOMOUS SOLANA MEMECOIN` header the current template emits.
**This is exactly "the information in it is not updated and not synchronised".**

### C3 `[H]` `POST /api/control/toggle` returns HTTP 500 → Pause/Resume button is dead

The handler calls `dashboard.generate()` **before** building its response, so the
`NameError` propagates into `except → _send_json({"status":"error"}, 500)`.
Same defect breaks Telegram `btn_toggle_pause`, `/pause` and `/resume`
(they log `Error handling callback btn_toggle_pause`).

### C4 `[M]` `{{...}}` escaping leaks Python source into the page

The paper/live indicator is double-braced, so the browser renders the literal text
`{"PAPER MODE • " if paper else "REAL TRADING ✅ • "}`.

### C5 `[C]` No supervision — nothing restarts, nothing reports health

- `cmd_start` runs the HTTP server as a **daemon thread** and the engine on the main
  thread. Any unhandled exception in `run_loop` kills the process → **dashboard dies with it.**
- No PID file, no `/health` endpoint, no auto-restart, no boot integration,
  no `stop`/`restart` commands. Hence "it never runs until I ask OpenClaw to start it".

### C6 `[H]` `/api/activity` re-parses the **whole** audit file on every 10-second poll

```python
def load_audit(n=None):
    for line in f:                      # every line, always
        rows.append(json.loads(line))
    return rows[-n:] if n else rows     # slice afterwards
```
The audit file already reached **4.7 MB / 14,528 rows**, of which
**12,110 rows (83 %) were `PRICE`/`WARNING` spam** — the exit monitor writes one row
per stuck position per 2-second cycle, with no dedupe or rate cap:

| category / level | rows |
|---|---|
| `PRICE` / `WARNING` | **12,110** |
| `TRADE` / `BUY` | 37 |
| `TRADE` / `TP` | 22 |
| `EXIT` / `INFO` | 11 |

No rotation, no cap, no compaction → the dashboard degrades until it stalls.

### C7 `[M]` Server-rendered HTML *and* a polling SPA at the same time

The page is regenerated with baked-in data **and** immediately overwrites everything
from `/api/state`. Two sources of truth for one screen; the baked-in half is the half
that goes stale (and, because of C1, is frozen at an old revision).

---

## D. File cohesion — your intuition was correct

### D1 `[C]` No dependency manifest, no README, no `.gitignore`

No `requirements.txt`, no `pyproject.toml`, no `setup.py`, no README, no `.gitignore`.
Both third-party dependencies (`PyYAML`, `websockets`) fail **silently** (A1, A3)
instead of stopping the bot with a clear message.

### D2 `[H]` Orphaned modules and dead data files

| item | status |
|---|---|
| `enzo/providers/pricefeed.py` (112 lines) | imported by **nobody**; duplicates `exit_monitor` + `portfolio.current_market_cap` |
| `enzo/core/cache.py` (L1+L2 cache) | used **only** by `dev.py`; the L2 `cache_store` table has **0 rows** because `gmgn.py` keeps its own private `_CACHE` dict → two parallel cache systems |
| `data/enzo-state.json` (76 KB, 1,059 `discovery_seen` entries) | `STATE_JSON_PATH` has **0 usages** — pure legacy orphan |
| `data/enzo-cache.json`, `enzo-panel.json`, `enzo-gmgn-ban.json`, `enzo-market-structure.json` | all **0 bytes** |
| `LEARNING_JSON_PATH`, `BAN_JSON_PATH`, `LOG_JSONL_PATH`, `PANEL_JSON_PATH`, `DOCS_DIR` | duplicate/dead constants, **0 usages** |
| `data/*.bak.20260902_183915` (5 files, 8.7 MB in `data/`) | ad-hoc backups committed to git |

### D3 `[M]` Config keys nothing reads

`data_sources.gmgn.cli` (the binary is hardcoded as `"gmgn-cli"` in `gmgn.py:121`),
`scoring_weights`, `entry_strategy`, `cache.holder_dist_ttl`, `pump_monitor.*`,
`scam_detection.*`, `logging.*`.

### D4 `[H]` Three sources of truth for the same state

- Portfolio: SQLite `enzo.db` **+** `enzo-portfolio.json` (sync cache) **+**
  `_fallback_state()` which reads the JSON when the DB read fails. If they diverge,
  the bot sizes trades on stale numbers.
- Prices: `gmgn._CACHE` + `pump._PUMP_CACHE` + `pricefeed._cache` + `db.cache_store`.
- Dashboard data: baked into HTML **+** served by `/api/state`.

### D5 `[C]` Secrets and runtime data are committed to git

`git ls-files` → **148 tracked files**, including:

- `config/enzo-secrets.json` — **a live Telegram bot token** and a PumpDev WS API key
- `data/enzo.db`, `data/enzo-audit.jsonl` (4.7 MB), 5 `.bak` dumps, 7 `.log` files
- **54 `.pyc` files** (`__pycache__` for both cpython-312 and cpython-314)
- `node_modules/ws` (15 `.js` files) — an unused dependency for a Python project

> **The Telegram bot token in this repo must be treated as compromised and revoked
> via @BotFather → /revoke.** Anyone with read access to the repository can drive the bot.

### D6 `[M]` `notify.notify_buy_failed()` does not exist

`engine.py:75` calls it behind a `hasattr()` guard, so it is a silent no-op —
**failed live buys are never reported to Telegram.**

### D7 `[H]` ~50 silently swallowed exceptions

`except Exception: pass` / `except Exception: _LOGGER.debug(...)` counts:
`engine.py` 11, `db.py` 11, `portfolio.py` 8, `pump.py` 7, `exit_monitor.py` 4,
`audit.py` 3, `config.py` 2, `analyze.py` 2, `market_structure.py` 2, `pricefeed.py` 3.
This is the single biggest reason "nothing is reported anywhere".

---

## E. The CLI question

A CLI already exists (`enzo.py`, 11 subcommands) — but it is a **launcher**, not a
**controller**. What's missing for the workflow you described:

| need | today |
|---|---|
| is the bot alive? | no way to tell — no PID file, no `status` on a *running* process |
| stop / restart | doesn't exist — you have to `pkill` |
| why did nothing happen? | no `doctor`; failures are `DEBUG`/`pass` |
| is MoonPay/gmgn-cli installed & authenticated? | discovered only mid-trade |
| pause | writes a file; never tells you whether anything read it |
| tail logs | manual |

**Proposal — `enzoctl`, one entry point, process-aware:**

```
enzoctl start [--foreground] [--port] [--interval]   # supervised: PID file, auto-restart, health
enzoctl stop | restart | status | health             # lifecycle
enzoctl doctor                                       # 25-point preflight, pass/fail + fix hints
enzoctl logs [-f] [--level] [--tail N]
enzoctl scan [mint] | pause | resume | mode [paper|live]
enzoctl positions | trades | equity | learn
enzoctl config get|set <path> <value>                # dot-path, validated
enzoctl wallet [balance|ready]                       # MoonPay preflight without trading
enzoctl shell                                        # interactive REPL (same commands)
```
Exit codes are meaningful (`0` ok, `2` not running, `3` unhealthy, `4` dependency missing)
so an agent such as OpenClaw can act on them programmatically.

---

## F. OpenClaw-readiness gap list

1. `requirements.txt` (+ pinned lock) and a `bootstrap.sh` that installs and **verifies**.
2. Fail-fast dependency check at startup — never silently fall back (A1, A3).
3. Supervisor with PID file, auto-restart, crash report, and `/health`.
4. `enzoctl doctor` covering: Python version, deps, `gmgn-cli`, `moonpay`/`mp`,
   MoonPay auth + consent, wallet existence, SOL/USDC balance, Telegram token,
   config schema, DB integrity, disk space, clock skew, port availability.
5. Machine-readable status (`enzoctl status --json`) for the agent to parse.
6. Log + audit rotation with size caps (C6).
7. Secrets out of git; `config/enzo-secrets.json` → `.gitignore` + `.example` template (D5).
8. `AGENTS.md` / `README.md` describing the contract, commands and invariants.
9. Deterministic paths (already OK — everything resolves from the repo root).
10. Idempotent start: a second `start` must detect the live PID instead of double-binding port 8077.

---

## G. Recommended fix order

| # | fix | unblocks |
|---|---|---|
| 1 | Fail-fast deps + `requirements.txt`; stop silently ignoring the YAML (A1, A3, D1) | everything else — the config actually loads |
| 2 | `dashboard.generate()` `wallet_name` + `{{paper}}` + stop swallowing the error (C1–C4) | dashboard, pause/resume button, Telegram buttons |
| 3 | Rewrite `executor.py` against the real CLI contract (B1–B8) | live trading |
| 4 | Un-pause + watchlist key + sizing/capital source (A2, A4, A5) | the bot actually scanning and entering |
| 5 | GMGN payload normalization + visible discovery errors (A6, A7) | candidates surviving pre-screen |
| 6 | Supervisor + PID + `/health` + `enzoctl` (C5, E, F) | "it runs by itself and stays running" |
| 7 | Audit rotation + tail-read + event dedupe (C6) | dashboard responsiveness over time |
| 8 | Repo hygiene: `.gitignore`, untrack secrets/DB/pyc/node_modules, delete orphans (D2–D5) | safe to hand to OpenClaw |
| 9 | Replace `except: pass` with structured, surfaced errors (D7) | diagnosability |
