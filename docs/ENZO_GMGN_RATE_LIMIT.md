# حظر GMGN: السبب الحقيقي (معدل الطلبات) والعلاج
# The GMGN ban: real cause (request rate) and the fix

> **ملخّص للمالك (غير المبرمج):** كلامك كان صحيحاً — الحظر سببه **كمية الطلبات**
> التي يرسلها البوت إلى GMGN، لا مشكلة في العملات ولا في المفتاح نفسه.
> قياسياً: **الدورة الواحدة كانت ترسل 63 طلباً**، والدورات كانت تتلاحق بلا توقف
> (≈ 48 طلباً في الدقيقة، 24 ساعة). والأسوأ: ثلاثة أرقام في ملف الإعداد كانت
> تبدو وكأنها تحميك (`max_analyses_per_min: 15` و`min_analysis_interval_sec: 3`
> و`max_candidates_per_scan: 40`) بينما **لا يوجد أي كود يقرؤها** — كانت زينة.
> الآن صارت مطبَّقة فعلاً، وأضفتُ الحدّ الأكبر: عملة فُحصت ولم تُشترَ **لا تُفحص
> مجدداً قبل 15 دقيقة** (كانت تُفحص كل 60 ثانية إلى الأبد).
> النتيجة المتوقّعة: من ~48 طلباً/دقيقة إلى **~10 طلبات/دقيقة** في الحالة العادية،
> وسقف نظري ~32 بدل ~63 لكل دورة.
>
> **ملاحظة صريحة:** لم أُلغِ تغييرات الإصلاح السابق بالكامل، لأن جزءاً منها
> (التوقّف عن فحص العملات أثناء الحظر) هو ما تطلبه وثائق GMGN نفسها:
> *«كل طلب أثناء فترة الحظر يمدّده 5 ثوانٍ»*. أما الجزء الذي خالف الوثائق
> (الانتظار وإعادة المحاولة داخل النداء، وقراءة وقت الحظر بصيغة غير موجودة)
> فقد **أُزيل واستُبدل**. التفاصيل في الأسفل.

---

## 1. What the operator actually saw

```
SNIPER_DATA_UNAVAILABLE: token traders failed: RateLimited: token/traders: still banned
```

Every coin in a sweep got that at once. It says nothing about the coins: the
early-sniper gate could not read GMGN, `on_unknown: reject` turned "no data" into
a veto, and the dashboard showed a wall of rejections. Underneath it was GMGN's
own rate-limit ban:

```json
{"code":429,"error":"RATE_LIMIT_BANNED","message":"...","reset_at":1775184222}
```

## 2. Proof it was a rate-limit ban (measured, not estimated)

The bundled mock CLI logs every invocation (`GMGN_ARGV_LOG`), so the request
volume was counted rather than guessed:

| what | requests |
|---|---|
| discovery (`market trenches` + `market trending`) | 2 |
| each deep analysis (`token info`, `security`, `holders`, `traders`, `created-tokens`) | ~5 |
| deep analyses per cycle (`max_depth_analyses: 12`) | 12 |
| **total per cycle** | **~63** |

And the pacing:

* configured local pace `requests_per_sec: 0.8` → a 63-request cycle needs **~79s**;
* the loop interval was **60s**, and `sleep_time = max(1.0, interval - elapsed)`
  → **1 second**;
* so the engine never idled: ~63 requests per ~80s ≈ **48 req/min, continuously**.

Three further traps made the config *look* safe while nothing was enforced:

1. `pump_monitor.max_analyses_per_min: 15`, `pump_monitor.min_analysis_interval_sec: 3`
   and `data_sources.gmgn.max_candidates_per_scan: 40` were **dead keys** — present
   in the shipped YAML and in `DEFAULTS`, read by no code (they sat in
   `tests/test_config_wiring.py`'s frozen dead-key inventory).
2. `max_depth_analyses` was shadowed: `engine.py` computed the depth cap as
   `discovery.max_depth_tokens_per_cycle or data_sources.gmgn.max_depth_analyses`,
   so `discovery`'s 12 always won and lowering `max_depth_analyses` changed nothing
   — while the log line printed the winning 12 and looked honest.
3. On a ban the provider **waited and retried inside the call**, after `gmgn-cli`
   had already retried once by itself, and parsed a `"resets at <datetime>"` string
   that GMGN never emits. Falling back to a 30s guess meant retrying 30s into a
   ~5-minute ban — and each such retry **extends the ban by 5s**.

## 3. GMGN's documented rate-limit model

Source: the installed CLI's own bundled docs
(`node_modules/gmgn-cli/skills/gmgn-market/SKILL.md` §"Rate Limit Handling",
`skills/gmgn-token-buy/references/pitfalls.md`, `dist/client/OpenApiClient.js`).

* Global **leaky bucket, rate = 20, capacity = 20**, and routes have **weights**:
  `market trenches` = 3, `market signal` = 3, `market hot-searches` = 3,
  `market kline` = 2, `market trending` = 1, `market search` = 1
  → sustained ≈ 20 ÷ weight requests/s per route.
* On top of that sits the **plan-tier quota** ("当前套餐的限频上限") — exceeding it
  is what produces `RATE_LIMIT_BANNED`.
* The ban payload carries **`reset_at` as a UNIX timestamp**, and the response has
  an **`X-RateLimit-Reset`** header. A ban is **typically ~5 minutes**.
* **"Repeated requests during the cooldown can extend the ban by 5 seconds each
  time, up to 5 minutes. Do not spam retries."**
* **"The CLI may wait and retry once automatically when the remaining cooldown is
  short. If it still fails, stop and tell the user the exact retry time instead of
  sending more requests."** (`OpenApiClient.js`: `maxAttempts = 2`,
  `DEFAULT_RATE_LIMIT_AUTO_RETRY_MAX_WAIT_MS = 5000`.)
* Named anti-pattern: calling `token info` per candidate — discovery already
  returns pool/volume/tx-counts/holders/age for ranking.

## 4. What changed

**Provider (`enzo/providers/gmgn.py`) — read the ban GMGN actually sends, then stop**

* `_ban_reset_wait()` reads, in order: body `reset_at` (UNIX seconds **or**
  milliseconds) → `X-RateLimit-Reset` header → a printed datetime (legacy human
  strings still work) → **300s**, GMGN's documented typical ban. The old 30s guess
  is gone; it was the direct cause of retrying into a live ban.
* A ban is **fail-fast**: the ban is registered for GMGN's own window and the call
  raises with the exact retry clock time (`retry at 19:10:53 (in 119s, from
  reset_at)`), plus the warning that every request during the cooldown adds 5s.
  **No in-call wait, no second retry** — `gmgn-cli` already retried once.
* A request counter (`gmgn.call_stats()`: lifetime total, per-minute average,
  refusals by the local limiter, bans seen) so the load is a number, not a theory.

**Engine (`enzo/core/engine.py`) — send less**

* The three dead knobs are now enforced: `max_candidates_per_scan` caps what is
  considered, `max_analyses_per_min` stops the cycle when the budget is spent
  (skips are counted, not silent), `min_analysis_interval_sec` spaces re-examination
  of the same coin.
* New `data_sources.gmgn.reanalysis_cooldown_sec` (**900s**): a coin whose outcome
  was **terminal** leaves the deep path for 15 minutes. It is stored in the DB
  cache (`enzoscan:<mint>`), so a restart does not re-burn the budget. This is the
  single biggest cut: discovery keeps returning the same trending tokens, and each
  of them used to cost ~5 requests every cycle, forever.

  The set is deliberately narrow, because this decides what a real-money bot may
  look at again (`engine.TERMINAL_DECISIONS`):

  | outcome | cooldown applied | why |
  |---|---|---|
  | `IGNORE` | **900s** | DANGEROUS security status / rug fingerprint — it will not become buyable in 15 minutes |
  | `NOT_TRADABLE` | **900s** | failed a hard gate (universe, rug veto, holder concentration) |
  | `WAIT` | only `min_analysis_interval_sec` (45s) | this is exactly the coin that may turn into a BUY two minutes later — freezing it would cost entries |
  | `BUY` | none | a position is open; the exit monitor owns it |
  | `DATA_ERROR` / `ANALYSIS_ERROR` / no result | only the 45s floor | a broken data source must not blacklist a coin (during a ban, every coin would otherwise be frozen) |

  Both windows are evaluated and the **longer** one is reported, so the logs and
  the skip counters never understate a freeze.
* `volume_caps()` makes the duplicate knobs honest: the **tightest** of
  `discovery.max_tokens_per_scan` / `data_sources.gmgn.max_candidates_per_scan`
  and of `discovery.max_depth_tokens_per_cycle` / `data_sources.gmgn.max_depth_analyses`
  wins, and the **effective** number is what gets logged and shown by doctor.
* `duty_cycle_advice()` warns when a cycle outlasts the interval — i.e. when the
  engine is streaming continuously — with the resulting requests/min.
* `engine.cycle_stats()` records what each cycle cost (requests, analyses, skips).
* `scan_once(..., force=True)` (used by `./enzoctl scan --force` and by
  `./enzoctl scan <MINT>`) bypasses the budget/cooldown, because that is an
  explicit human request; it still counts every request.

**Retuned defaults** (mirrored in `config/enzo-config.yaml`; the hygiene test
requires both files to agree):

| key | before | after |
|---|---|---|
| `pump_monitor.max_analyses_per_min` | 15 (dead) | **6** (enforced) |
| `pump_monitor.min_analysis_interval_sec` | 3 (dead) | **45** (enforced) |
| `data_sources.gmgn.max_depth_analyses` | 12 (shadowed) | **6** |
| `discovery.max_depth_tokens_per_cycle` | 12 (the one that applied) | **6** |
| `data_sources.gmgn.max_candidates_per_scan` | 40 (dead) | 40 (enforced) |
| `discovery.max_tokens_per_scan` | 40 (dead) | 40 (enforced) |
| `data_sources.gmgn.reanalysis_cooldown_sec` | — | **900** (new, terminal outcomes only) |
| `engine.scan_interval_sec` | hidden code default 60 | **60, visible in the YAML** |
| `data_sources.gmgn.requests_per_sec` | 0.8 | 0.8 (unchanged — pacing was never the problem) |
| `data_sources.gmgn.discovery_limit` | 50 | **30** per category (the deep path is capped at 6 anyway) |
| `data_sources.gmgn.trending_interval` | — | **"1m"** (new; `market trending` REQUIRES `--interval` — see below) |

**Visibility**

* Dashboard: a **Requests** row in the GMGN card — lifetime count, per-minute
  average, last cycle's cost and how many candidates the budget/cooldown skipped.
* `/api/status` → `gmgn.call_stats` + `gmgn.cycle_stats`.
* `./enzoctl doctor` → a `gmgn_request_budget` row that does the arithmetic
  (~requests/min ceiling, effective caps, observed volume, bans) and says which
  knobs to move when it is too high.
* `./enzoctl scan` prints the cycle's GMGN cost at the end.
* `./enzoctl unban` now points at the **volume** knobs (the real cure) instead of
  only `requests_per_sec`.

## 5. Effect on the request volume

| | before | after (steady state) |
|---|---|---|
| requests per cycle | ~63 | ~2 discovery + 5 × (new coins only) |
| cycles | back-to-back (sleep 1s) | spaced by `engine.scan_interval_sec` |
| hard ceiling | none (dead knobs) | `max_analyses_per_min` 6 → ~32 req/min |
| realistic sustained | ~48 req/min | **~10 req/min** (most cycles re-analyse nothing) |
| during a ban | 1 probe per candidate, +5s each | **zero requests** until `reset_at` |

## 6. Why the previous fix was not reverted wholesale

The operator asked to cancel the previous change if the ban turned out to be a
rate-limit ban. It was — and that is precisely why the **stop-probing** half of it
must stay: GMGN extends the ban by 5s per request sent during the cooldown, so a
sweep that probes coin by coin turns a 5-minute ban into a rolling one. What was
removed is the half that contradicted the docs:

| previous behaviour | now |
|---|---|
| wait for the ban inside the call, then retry | **fail fast** — the CLI already retried once |
| parse only `"resets at <datetime>"` | parse `reset_at` (UNIX) → header → datetime → **300s** |
| fallback guess 30s (retry into a live ban) | fallback = GMGN's documented 5 minutes |
| escalate by doubling an unparsable wait | unnecessary: the wait is now read correctly |
| keep the ban registered / clearable / visible | **kept** (that part was right) |

## 6b. A second rate-limit bug found while verifying the first

`market trending` in gmgn-cli v1.6.1 declares **two** required options:

```js
.requiredOption("--chain <chain>", ...)
.requiredOption("--interval <interval>", "Time interval: 1m / 5m / 1h / 6h / 24h")
```

ENZO built `["market", "trending", "--chain", ch, "--limit", N, "--platform", ...]`
— no `--interval` — so commander aborted **before any HTTP call** with
`error: required option '--interval <interval>' not specified`. Consequences:

* the category was listed in the config as a discovery source and returned **zero
  tokens on every cycle of the bot's life**, with one warning line as the only trace;
* it still cost a subprocess spawn per cycle, and it made "why are the coins bad?"
  unanswerable — the coins were never from `trending` at all;
* 700+ checks stayed green because `tests/mockbin/gmgn-cli` did not reproduce the
  requirement. It does now, so dropping the flag again fails the suite.

Fixed: `--interval` is sent from the new knob
`data_sources.gmgn.trending_interval` (owner's choice: **1m**, the tightest window),
an invalid value is refused locally with the valid list named instead of being sent,
and `discovery_limit` went 50 → 30 per category.

Note the rate-limit accounting: enabling `trending` adds **no** new requests — the
call was already being made every cycle and failing at argument parsing. What
changed is that it now returns tokens, and each decision carries
`discovery_source` (`gmgn_trenches` / `gmgn_trending` / `pumpdev` / `watchlist`) so
a source can be judged on its own record.

## 7. If bans still come back

In this order — each step is one number in `config/enzo-config.yaml`:

1. `pump_monitor.max_analyses_per_min: 6 → 4` (or 3). Each analysis is ~5 requests.
2. `data_sources.gmgn.max_depth_analyses` and `discovery.max_depth_tokens_per_cycle`
   `6 → 4` (both — the tighter one applies, keep them equal).
3. `data_sources.gmgn.reanalysis_cooldown_sec: 900 → 1800` (re-examine a terminally
   rejected coin — `IGNORE`/`NOT_TRADABLE` — only every 30 minutes).
4. `engine.scan_interval_sec: 60 → 120` (half the cycles, half the discovery cost).
5. `data_sources.gmgn.discovery_limit: 50 → 25` (smaller discovery payloads).
6. Only then consider the plan: GMGN's message names the quota of the current plan
   ("当前套餐的限频上限"). If the volume is already small and bans continue, the key
   or the IP is limited at the plan level, and an upgrade is the only lever left.

Check the result without guessing:

```bash
./enzoctl doctor          # gmgn_request_budget: ceiling, effective caps, observed volume
./enzoctl scan --force    # prints "GMGN cost: N request(s) this cycle ..."
./enzoctl unban           # how long the ban has left (dry run), --confirm clears it
```

…or open the dashboard and read the **Requests** row in the GMGN card.

## 8. Tests

```bash
python3 tests/test_rate_limit_budget.py   # 59 checks: the caps are real, counted in CLI calls
python3 tests/test_gmgn_cli_compat.py     # 161 checks, incl. §11 reset_at/fail-fast, §12 --interval, §13 kline, §14 trenches --type + data.pump, §15 momentum windows, §16 market_structure unknown-window
python3 tests/test_config_wiring.py       # 18 checks: no new dead keys, YAML == DEFAULTS
```

`tests/test_rate_limit_budget.py` counts **real `gmgn-cli` invocations** through the
bundled mock, so it proves the load GMGN would see: a wide-open cycle costs 20
token requests for 4 analyses, the next cycle costs **0** (cooldown), a fresh
process still respects it, `--force` bypasses it, and a whole cycle during a ban
issues **zero** calls. It also pins the classification above (a `WAIT` coin is held
only by the 45s floor, an `IGNORE` coin by the full 900s, a `DATA_ERROR` by neither)
and the effective-cap rule (`volume_caps` returns the tightest of each pair).

Full suite: **886 checks, 0 failures** across 20 suites (`bash` the list in the
README, or run any file alone — no config, network or real wallet needed).
