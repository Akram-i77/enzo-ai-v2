# مرشّحات التداول الجديدة: Pump V1 + بوابات الطور + رغ القنّاصين
# New trading filters: Pump V1 only, phase gates, sniper-flood rug rule

> **ملخّص للمالك (غير المبرمج):** البوت صار يشتري **عملات pump.fun القياسية فقط**،
> ويرفض أي عملة لا يعرف منصّة إطلاقها، ويطبّق حداً أدنى للقيمة السوقية وعدد
> عمليات البيع **قبل الترحيل** وحدّاً أعلى للقيمة السوقية + رسوماً مدفوعة **بعد
> الترحيل**، ويرفض نهائياً أي عملة دخلها قنّاصون مبكراً بأحجام كبيرة. كل رقم تراه
> في القرار مأخوذ من `gmgn-cli` الحقيقي، ويمكنك التحقق منه بنفسك بأمر واحد:
> `./enzoctl probe <عنوان_العملة>`.

Verified against **gmgn-cli v1.6.1** (the version actually installed), using the
CLI's own bundled schema docs (`node_modules/gmgn-cli/skills/*.md`) rather than
the older `docs/ENZO_GMGN_DATA_MAP.md`, which predates the v1.6 rewrite.

---

## 1) What was asked, and whether the data exists

| # | Requirement | Verdict | Where the number comes from (v1.6.1) |
|---|---|---|---|
| 1 | Trade **standard pump coins (Pump V1) only** | ✅ direct | `token info` → `launchpad` (`pump`), `launchpad_platform` (`Pump.fun`). At discovery time both `market trenches` and `market trending` rows already carry `launchpad_platform`, and trenches accepts `--launchpad-platform Pump.fun` so the filter is applied **server-side**. |
| 2 | Rug rule: the **first 8 transactions after creation** (after the dev's add) — if they are **snipers** and their **combined size > $5,000** → rug, never buy | ⚠️ **proxy** (see §4) | gmgn-cli v1.6.1 has **no chronological trade tape**. Its token commands are `info / security / pool / holders / traders`. What *is* available: `token traders` rows carry `start_holding_at` (unix ts of that wallet's **first buy**), `buy_volume_cur` (USD bought since creation) and `maker_token_tags` (`sniper`, `bundler`, …). So the first 8 **wallets** (ordered by first-buy time) are used as the closest readable equivalent of the first 8 transactions. |
| 3a | Pre-migration: min market cap **$5,000** | ✅ direct | trenches → `usd_market_cap`; trending → `market_cap`; `token info` has **no** market cap, so it is computed as `price.price × circulating_supply` (the CLI's own docs say to do this). |
| 3b | Pre-migration: min **sell transactions = 10** | ✅ direct | `token info` → `price.sells_24h` (and `sells_1m/5m/1h/6h`); trenches → `sells_24h`; trending → `sells`. trenches also accepts `--min-sells-24h`. |
| 3c | Migrated: min market cap **$10,000** | ✅ direct | same sources as 3a. |
| 3d | Migrated: **Global Fees Paid (SOL) ≥ 2.5** | ⚠️ **partial** (see §4) | Not on `token info`, trenches or trending. It **is** on `portfolio created-tokens --address <creator>` → `tokens[].total_fee` (with `coin_creator_fee` beside it) — the same numbers GMGN's UI shows in the dev's launch book. The API does **not** state the unit, so the unit is declared in config (`phase_gates.migrated.fees_unit: sol`) and the raw value is always reported next to it. |
| — | Pre- vs post-migration phase | ✅ direct | `launchpad_status` is authoritative: `0` = not opened, `1` = live on the bonding curve, `2` = **migrated**. Fallbacks when it is missing: `launchpad_progress`, `migrated_pool`, `complete_timestamp`, `exchange == pump_amm`. |

---

## 2) What the bot now does

Order matters: the cheap gates run first, and the two gates that cost an extra
`gmgn-cli` call (fees, early snipers) only run for coins that already survived
everything free.

```
discovery (trenches + trending, --launchpad-platform Pump.fun, --min-marketcap)
   ↓  list_screen  (mcap floor, top-10 / bundler / sniper hold rates, dev hold)
   ↓  market_analysis gates (liquidity, volume, holders, buy pressure, mcap)
   ↓  NO_MARKET_DATA gate (a dead provider is reported, never read as "bad token")
   ↓  rug gate + Layer-1 fingerprints
   ↓  Layer 0 — the entry universe:
        1. Pump V1 only          → NOT_PUMP_V1 / LAUNCHPAD_UNKNOWN
        2. phase must be known   → PHASE_UNKNOWN
        3. pre-migration floors  → MCAP_BELOW_PRE_MIN / SELLS_BELOW_MIN
        4. migrated floors       → MCAP_BELOW_MIGRATED_MIN / FEES_BELOW_MIN
        5. early-sniper flood    → SNIPER_FLOOD_EARLY
   ↓  holder-concentration cap (market_analysis.max_holder_percentage)
   ↓  decision + sizing + executor preflight
```

### Veto codes (all of them appear verbatim in `rejected_signals`)

| Code | Meaning |
|---|---|
| `LAUNCHPAD_UNKNOWN` | the payload says nothing about the launchpad, and `token_universe.reject_unknown_launchpad` is on — unknown is **not** treated as pump.fun |
| `NOT_PUMP_V1` | a different launchpad (e.g. `letsbonk`) or a non-pump pool |
| `PHASE_UNKNOWN` | migration state cannot be determined; `phase_gates.unknown_phase: strict` applies the tougher floor |
| `MCAP_UNKNOWN` / `MCAP_BELOW_PRE_MIN` / `MCAP_BELOW_MIGRATED_MIN` / `MCAP_BELOW_UNKNOWN_MIN` | market-cap floors, per phase |
| `SELLS_UNKNOWN` / `SELLS_BELOW_MIN` | pre-migration sell-count floor (a missing counter is **never** read as 0 sells, and never as "enough") |
| `FEES_NOT_CHECKED` / `FEES_UNKNOWN` / `FEES_BELOW_MIN` | migrated fees floor; `require_known_fees: true` means "cannot measure" is a rejection, not a pass |
| `SNIPER_FLOOD_NOT_CHECKED` / `SNIPER_FLOOD_EARLY` / `SNIPER_DATA_UNAVAILABLE` | the rug rule; `sniper_flood.on_unknown: reject` |
| `HOLDER_CONCENTRATION` | top-1 **wallet** share above `market_analysis.max_holder_percentage` |

Every decision payload carries `universe` = `{pump_v1, launchpad_known, launchpad,
platform, phase, phase_evidence[], progress_pct, fees, snipers}` plus
`top_holder_pct` / `top_holder_source`, so any rejection can be audited after the
fact without re-querying GMGN.

### Config (all of it in `config/enzo-config.yaml`, mirrored in `enzo/core/config.py` DEFAULTS)

```yaml
token_universe:
  pump_v1_only: true               # only standard pump.fun coins
  reject_unknown_launchpad: true   # unknown ≠ pump.fun
  discovery_min_market_cap: 5000   # pushed to the CLI as --min-marketcap

phase_gates:
  unknown_phase: strict            # unknown phase gets the tougher floor
  pre_migration:
    min_market_cap: 5000
    min_sells: 10
  migrated:
    min_market_cap: 10000
    min_total_fees: 2.5
    fees_unit: sol                 # the API does not state the unit — you declare it
    require_known_fees: true       # "couldn't measure" = rejection

sniper_flood:
  enabled: true
  first_n: 8                       # the owner's "first 8 transactions"
  min_sniper_count: 4              # ≥4 snipers AND the sum over the line = rug
  max_total_sniper_buy_usd: 5000   # …OR any single wallet over the line = rug
  max_single_sniper_buy_usd: 5000
  sniper_tags: [sniper]
  include_bundler: false
  traders_limit: 100               # gmgn-cli caps --limit at 100
  order_by: buy_volume_cur
  on_unknown: reject

market_analysis:
  max_holder_percentage: 10.0      # top-1 wallet; curve/AMM/burn rows excluded

data_sources:
  gmgn:
    discovery: [trenches, trending]        # smartmoney/kol do NOT exist in v1.6
    launchpad_platform_filter: Pump.fun    # server-side filter
    discovery_limit: 50
    requests_per_sec: 0.8
    request_gap_ms: 350
    burst_capacity: 2.5
```

---

## 3) How to verify it yourself (no programming needed)

```bash
./enzoctl doctor              # 32 checks; the new ones are listed below
./enzoctl probe <MINT>        # every number the gates read, next to its threshold
./enzoctl probe <MINT> --json # the same, machine-readable (stdout is pure JSON)
```

`doctor` now also reports: `gmgn_api_key`, `gmgn_cli_dialect` (which address flag
the installed build accepts), `gmgn_discovery_categories`, `gmgn_rate_config`,
`universe_gates` (your thresholds echoed back) and `holder_concentration_cap`.
An unverifiable check is printed as ⚠ — never as ✔.

`probe` prints, for one coin: the provider and CLI dialect, identity
(`launchpad`, `launchpad_platform`, `launchpad_status`, curve progress, creation
time, dev), the numbers the gates compare (market cap, liquidity, volume,
buys/sells, price), the **first-8 window** wallet by wallet with "seconds after
open", size and whether it is sniper-tagged, the fees value with its unit and
source, the holder concentration with the pool/burn rows it excluded and the
tradeable float, and finally the **real** `analyze.run_pipeline` verdict — the
same code path the trading loop runs, so the tool and the bot cannot disagree.
Exit code is `1` when the coin is vetoed, `0` when it is clean.

### And on the dashboard (no terminal at all)

| Where | What it shows |
|---|---|
| Diagnostics → `🎯 Entry Universe · Layer 0` | all five gates as pills with `ARMED`/`OFF`, a `N/5 ARMED` counter, and your real thresholds read from `config/enzo-config.yaml` — `$5,000` / `$10,000` + `2.5 SOL` / `first 8 wallets` / `10%` — plus the sniper-proxy limitation stated on the page itself |
| Diagnostics → `⚡ GMGN Data Source` | API-key `present`/`MISSING`, the address dialect the installed CLI accepted, every discovery category with its last count, the last sweep and the last provider error (never swallowed) |
| Activity → `🎯 Gate Vetoes` button | isolates the decisions a gate killed; each one prints its `reason`, its veto codes in red, and the evidence line (`pump_v1 / platform / phase / fees / snipers / top wallet`) |
| Red banner at the top | `GMGN_API_KEY` not set ⇒ **every gate reads "unknown"**; or all discovery categories dead ⇒ the bot is looking at an empty market. Both clear themselves once the cause is gone |
| `GET /api/state` | `config_summary.universe_gates` (17 thresholds) and `gmgn_status` for any external monitor |

Until this change a vetoed coin appeared as `SYM → IGNORE (conf=0)` with no
explanation: the audit row always carried `reason` and `rejected_signals`, but the
converter that feeds the activity list dropped them. It now carries them, plus
`universe` and `top_holder_pct`, which `audit.record()` persists as of this commit.

---

### When a veto code is about the DATA SOURCE, not the coin

`SNIPER_DATA_UNAVAILABLE`, `FEES_UNKNOWN`, `MCAP_UNKNOWN`, `SELLS_UNKNOWN` and
`SNIPER_FLOOD_NOT_CHECKED` mean **"I could not read the number"**, not "this coin
is bad". The usual cause is a GMGN ban or a missing `GMGN_API_KEY`: while a ban is
active every call is refused, so a whole sweep comes back with these codes at once
- including for coins that would have passed. Because your rule is
`on_unknown: reject`, the bot refuses to buy without evidence, which is the safe
side of the trade, but it is not a verdict on the coin.

Where to look: the dashboard's `⚡ GMGN Data Source` card now has a **Ban** row
(`ACTIVE - 47s left`) and a red banner explaining exactly this; `./enzoctl doctor`
prints `BAN ACTIVE 47s left (./enzoctl unban)`; `./enzoctl unban` shows what is
registered and, with `--confirm`, clears it. If bans keep returning, lower
`data_sources.gmgn.requests_per_sec` / raise `request_gap_ms` rather than clearing
them repeatedly - each probe of a live ban can extend it.

## 4) The two honest limitations

1. **"First 8 transactions" is approximated by "first 8 wallets".**
   gmgn-cli v1.6.1 exposes no trade tape. `token traders` gives per-wallet
   aggregates: `start_holding_at` (first-buy timestamp), `buy_volume_cur` (USD
   bought since creation), `buy_tx_count_cur`, `maker_token_tags`. Consequences:
   * a wallet that bought 5 times in the first minute appears **once**, with the
     *sum* of its buys — so the rule is slightly **stricter** than a literal
     transaction tape for repeat buyers, and slightly **looser** for a single
     transaction split across many wallets;
   * `buy_volume_cur` is *since creation*, not *within the first 8 slots*, so a
     wallet that sniped small and later bought more reports the larger total.
     Both errors push toward **rejecting**, never toward buying;
   * the window is ordered by first-buy time, and rows without
     `start_holding_at` are counted and reported (`rows_seen` vs
     `rows_with_ts`) rather than silently dropped. If no row carries a
     timestamp the gate returns `SNIPER_DATA_UNAVAILABLE` and, per
     `on_unknown: reject`, the coin is **not** bought.
   Free co-signals that are read at discovery time and shown in `probe`:
   `sniper_count`, `top70_sniper_hold_rate`, `bundler_rate`, `rug_ratio`.

2. **Fees: the number is real, the unit is declared.**
   `portfolio created-tokens` → `tokens[].total_fee` is what GMGN's UI shows as
   fees paid, but the API does not label the unit, and the list is capped at
   roughly the 101 newest tokens of that creator — so for a serial deployer an
   old coin may be absent (`FEES_UNKNOWN`, which `require_known_fees: true`
   turns into a rejection). `fees_unit: sol` states the assumption in one place;
   if GMGN ever labels it differently, that single key changes the meaning
   without touching code. `probe` always prints the raw value **and** the unit.

---

## 5) Bugs found while verifying this (all fixed, all now covered by tests)

These were not part of the request; they were found by reading the v1.6.1 schema
against the code that consumes it. Each one silently produced zeros or "no data".

| # | Bug | Effect before the fix |
|---|---|---|
| 1 | `analyze()`'s decision tail was orphaned inside `rug_rejection()` (dead code after a `return`) by commit `40a19f6` | **`analyze()` returned `None`** → `run_pipeline` → `None` → the engine could never decide anything. It survived 462 green checks because `test_engine_e2e` *stubs* `run_pipeline`. |
| 2 | `_normalize_holders` looked for `percent`/`percentage`/`share`/`pct` — v1.6 sends **`amount_percentage`** | every holder share was `None`: top-1/top-10 concentration, average profit and average wallet age were all silently zero |
| 3 | `holder_distribution(exclude_curve_ata=True)` accepted the flag but never implemented it | the bonding curve / AMM vault (often 40–80% of supply) counted as "the top holder" — any real concentration cap would have vetoed every healthy migrated token |
| 4 | `market_analysis.max_holder_percentage` was read into a variable and **never used**; its intended source `sec["top_holder_pct"]` has no producer anywhere | the owner-set 10% holder cap never fired on any coin, ever |
| 5 | `deep_holder_analysis._tag_count` searched only `tags`; v1.6 puts `sniper`/`bundler`/`rat_trader`/`whale`/`dev_team` in **`maker_token_tags`** | sniper / bundler / rat-trader counts were permanently 0 |
| 6 | `sell_amount_percentage` was not mapped, so `sell_pct`/`buy_pct` were `None` | `top10_dumping` and `top10_accumulating` were both 0, so `dumping >= accumulating` could never flag sell pressure |
| 7 | `token_info` / `token_security` / one `token traders` call site hardcoded `--address` instead of using the dialect negotiator | on an older gmgn-cli those three (the most important calls) failed outright |
| 8 | every read helper did `except Exception: return {}` with no record | a missing `GMGN_API_KEY` or a rejected flag looked **exactly** like "GMGN answered and there was nothing to buy" |
| 9 | `db.rl_acquire` inserted `rate_per_sec`/`capacity` once and the refill expression read the **stored** column | editing `data_sources.gmgn.requests_per_sec` had **no effect at all** until the DB row was deleted (this is why 0.8/s looked hardcoded) |
| 10 | PyYAML silently keeps the **last** duplicate key | a hand-edited config with a key twice loads the wrong value with no warning anywhere — now a hard error naming both line numbers |
| 11 | `--json` was only accepted *before* the subcommand | `enzoctl probe MINT --json` died with "unrecognized arguments" |
| 12 | the shared logger wrote to **stdout** | `enzoctl --json … | jq` received log lines before the JSON |
| 13 | `_swap_count(info, "sells")` built the path `sellss_24h` | a plural argument silently returned `None`, i.e. "unknown", to the sells gate |

---

## 6) Test coverage

```
tests/test_token_universe_gates.py   48 checks — every veto code, both phases,
                                          boundary values, "no sacrifice" (a
                                          coin that should pass still passes),
                                          and an AST + subprocess guard that
                                          analyze() returns a decision dict
tests/test_gmgn_cli_compat.py        80 checks — v1.6 dialect + the legacy
                                          fallback, API-key guard, every
                                          discovery envelope, dead categories,
                                          the flags pushed to the CLI, field
                                          shapes, holder normalisation and pool
                                          exclusion, the config-driven rate
                                          limiter, and the FULL path with
                                          nothing stubbed (engine.scan_mint →
                                          analyze → provider → mock CLI → ledger)
tests/test_enzoctl_probe.py          51 checks — probe's numbers, window, fees,
                                          holders, verdict and exit codes in both
                                          output modes; doctor's new checks in
                                          their passing AND failing states

tests/test_dashboard_e2e.py         116 checks — a real HTTP server in an
                                          isolated sandbox: every route, every
                                          one of the 14 buttons clicked inside a
                                          jsdom DOM, the Layer-0 card and its
                                          thresholds present, the Gate-Vetoes
                                          filter matching a SEEDED veto event
                                          (reason + codes + evidence rendered),
                                          the missing-API-key banner appearing
                                          and disappearing again, and
                                          /api/state exposing universe_gates
                                          with your values, not defaults

Full suite: 19 files · 685 checks · 0 failures · ~82s
```

All of it runs against `tests/mockbin/gmgn-cli`, a mock that reproduces v1.6.1's
subcommands, envelopes, flag validation and error text (and can emulate the old
`--token` build), plus `tests/mockbin/mp` for MoonPay. No test touches the
operator's real DB, portfolio or Telegram bot.
