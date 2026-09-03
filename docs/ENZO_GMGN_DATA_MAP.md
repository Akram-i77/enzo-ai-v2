# ENZO ↔ GMGN — كل نقاط البيانات (Data Map)

**Date:** 2026-08-10 01:15 GMT+1 · **Mode:** AUDIT (no changes)
**مصدر:** enzo_gmgn.py (الطبقة الوحيدة للبيانات منذ 2026-08-05) + enzo-config.yaml

---

## 0. نظرة عامة — الـ Endpoints المستخدمة (13)

| # | gmgn-cli command | الدالة | Cache TTL | الاستخدام |
|---|---|---|---|---|
| 1 | `token info` | `token_info()` | 30s | **الأساس لكل شيء** (سعر/كاب/ليك/فول/هولدرز/ديف) |
| 2 | `token security` | `token_security()` | 300s | فحص الأمان (honeypot/tax/...) |
| 3 | `token holders` | `deep_holder_analysis()` | 300s | توزيع + هوية الهولدرز (smart/rat/bundler...) |
| 4 | `token traders` | `token_traders()` → `top_trader_identity()` | — | Phase-C فقط (تكوين تجار top20) |
| 5 | `token pool` | `token_pool_info()` | 300s | سيولة pool حقيقية + عنوان pool |
| 6 | `market kline` | `kline()` | 120s | شموع OHLCV (مومنتوم/اتجاه) |
| 7 | `market signal` | `market_signals()` | 25s | إشارات (spike/smart buy/rug...) — **يعيد [] من هذا الـ host** |
| 8 | `market trending` | `discover()` | 25s | اكتشاف (قائمة) |
| 9 | `market trenches` | `discover()` | 25s | اكتشاف (قائمة) |
| 10 | `market smartmoney` | `discover()` | 25s | اكتشاف — **يعيد [] من هذا الـ host** |
| 11 | `market kol` | `discover()` | 25s | اكتشاف — **يعيد [] من هذا الـ host** |
| 12 | `portfolio stats` | `wallet_stats()` | 600s | إحصاءات محفظة (winrate/pnl/created) |
| 13 | `portfolio created-tokens` | `dev_history()` | 900s | تاريخ إطلاقات الديف (serial launcher) |

**+ 1 مصدر مساعد:** `sol_price_usd()` عبر `token info` على Wrapped SOL (cache 60s).

---

## 1. `token info` — نقطة البيانات الأغنى (يغذي كل شيء تقريبًا)

### 1a. السعر والكاب والسيولة (core market)
| الحقل GMGN | مخرجات ENZO | تُستخدم في |
|---|---|---|
| `price` (سلسلة أو كائن nested) | `price_usd` | كل تحليل، دخول/خروج |
| `circulating_supply` / `total_supply` / `max_supply` | `market_cap_usd = price × supply` | كل شيء (الكاب ليس حقلًا مباشرًا من GMGN!) |
| `liquidity` / `liquidity_usd` | `liquidity_usd` | بوابات الدخول (min_liquidity) |
| `volume_24h` / `volume` | `volume_24h_usd` | بوابات (min_volume) |
| `holder_count` / `holders` | `holder_count` | بوابات (min_holders) |

### 1b. تغيّر السعر (momentum)
| الحقل | مخرجات | استخدام |
|---|---|---|
| `price_change_percent1h` / `price_1h` | `price_change_1h` | محور momentum |
| `price_change_percent5m` / `price_5m` | `price_change_5m` | محور momentum |
| `price_change_percent24h` / `price_24h` | `price_change_24h` | سياق عام |
| `buys` / `sells` / `swaps` | `buys` / `sells` | buy_pressure + الزخم |
| `hot_level` | `hot_level` | momentum boost |

### 1c. حالة pump.fun (phase)
| الحقل | مخرجات |
|---|---|
| `launchpad_progress` / `progress` | `progress_pct` (0-100) → phase (new_pair/about_to_migrate/migrated) |
| `launchpad` / `platform` | نوع المنصة (pump.fun/four.meme/...) |
| `migrated_pool` | تأكيد migration |
| `creation_timestamp` | عمر التوكن |

### 1d. هوية المحفظة (wallet_tags_stat — pre-aggregated, صفر استدعاءات)
| الحقل | مخرجات |
|---|---|
| `wallet_tags_stat.smart_wallets` | `smart_wallet_count` |
| `wallet_tags_stat.whale_wallets` | `whale_wallet_count` |
| `wallet_tags_stat.sniper_wallets` | `sniper_wallet_count` |
| `wallet_tags_stat.bundler_wallets` | `bundler_wallet_count` |
| `wallet_tags_stat.fresh_wallets` | `fresh_wallet_count` |
| `wallet_tags_stat.renowned_wallets` | `renowned_wallet_count` |
| `wallet_tags_stat.rat_trader_wallets` | `rat_trader_wallet_count` |
| `wallet_tags_stat.creator_wallets` | `creator_wallet_count` |
| `wallet_tags_stat.top_wallets` | `top_wallet_count` |

### 1e. كتلة `stat` (dev/توزيع — صفر استدعاءات)
| الحقل | مخرجات | استخدام |
|---|---|---|
| `stat.dev_team_hold_rate` / `creator_hold_rate` | `dev_team_hold_rate` | DEV_TEAM_HOLD penalty |
| `stat.creator_created_count` | `creator_created_count` | DEV_FACTORY penalty (≥100) |
| `stat.top70_sniper_hold_rate` | `top70_sniper_hold_rate` | penalty ≥30% |
| `stat.top_bundler_trader_percentage` | `top_bundler_trader_percentage` | معلومات |
| `stat.top_rat_trader_percentage` | `top_rat_trader_percentage` | معلومات |
| `stat.top_entrapment_trader_percentage` | `top_entrapment_trader_percentage` | معلومات |
| `stat.fresh_wallet_rate` | `fresh_wallet_rate` | معلومات |
| `stat.private_vault_hold_rate` | `private_vault_hold_rate` | معلومات |
| `stat.signal_count` | `signal_count` | معلومات |
| `stat.degen_call_count` | `degen_call_count` | معلومات |

### 1f. مخاطر مدمجة (list/ratio — من نفس الاستجابة)
| الحقل | مخرجات |
|---|---|
| `rug_ratio` | `rug_ratio` (0-1) |
| `bundler_rate` | `bundler_rate` |
| `entrapment_ratio` | `entrapment_ratio` |
| `top_10_holder_rate` | `top10_pct` |
| `is_honeypot` | `is_honeypot` |
| `renounced_mint` / `renounced_freeze_account` | mint/freeze authority |
| `creator_close` | `creator_close` → **DEV_SOLD_ALL hard reject** |
| `is_wash_trading` | `is_wash_trading` |
| `creator_balance_rate` | `creator_balance_rate` |
| `burn_ratio` | `burn_ratio` |
| `bluechip_owner_percentage` | `bluechip_owner_percentage` |
| `callout_count` | `callout_count` |
| `visiting_count` | `visiting_count` |
| `gas_fee` | `gas_fee` |
| `creation_tool` | `creation_tool` |
| `twitter_username` / `website` / `telegram` | social links |

---

## 2. `token security` — الأمان (cache 5 دقائق)

| الحقل | استخدام |
|---|---|
| `is_honeypot` | **HONEYPOT hard reject** |
| `is_blacklisted` | **BLACKLISTED hard reject** |
| `is_airdrop_scam` | **AIRDROP_SCAM hard reject** |
| `is_insider_trading` | soft flag |
| `buy_tax` | >10% → soft flag + penalty |
| `sell_tax` | >10% → soft flag + penalty |

---

## 3. `token holders` — الهولدرز العميق (cache 5 دقائق)

### لكل holder (top 20):
| الحقل | مخرجات |
|---|---|
| `address` | معرف |
| `balance` | نسبة الملكية |
| `maker_token_tags` | tags → smart/whale/kol/bundler/sniper/rat/fresh/renowned |
| `avg_cost` | متوسط سعر الشراء |
| `unrealized_pnl` / `realized_pnl` | الربح (ضغط البيع) |
| `buy_tx_count_cur` / `sell_tx_count_cur` | سلوك حالي (تراكم/تخلص) |
| `is_new` / `is_suspicious` | جودة المحفظة |
| `is_on_curve` / `exchange` | curve vs pool |
| عمر المحفظة / وقت الاحتفاظ | اشتقاقات محلية |

### مجمّعات:
- `top1_pct`, `top10_pct` → **BUNDLE_DISTRIBUTION hard reject** (أثناء pre-migration: top10>90 / top1>80)
- `smart_count`, `whale_count`, `kol_count`, `bundler_count`, `sniper_count`, `rat_count`, `fresh_count`, `suspicious_count`
- `top10_sell_pressure_count`, `top10_accumulating`, `top10_dumping` → **TOP10_DUMPING penalty** (≥5)
- `top10_avg_profit_usd`, `avg_wallet_age_days`

---

## 4. `token traders` — هوية كبار المتداولين (Phase-C فقط، config-gated)

| الحقل | مخرجات | استخدام |
|---|---|---|
| `is_smart` / `is_whale` / `is_kol` | counts | **بونص +12 max** (smart×2, whale×1.5, kol×1) |
| `is_rat` / `is_bundler` / `is_sniper` | counts | سلبي (يحسب في wallet_behavior) |
| `profit` / `buy_amount_usd` / `sell_amount_usd` | per-trader | معلومات |

**ملاحظة:** مفعّل عبر `data_sources.gmgn.top_traders: false` افتراضيًا (توفير rate limit).

---

## 5. `token pool` — سيولة الـ AMM (cache 5 دقائق)

| الحقل | استخدام |
|---|---|
| pool address | مرجع |
| السيولة الحقيقية + الاحتياطيات | تحقق من تقدير الـ curve ($151 مقابل تقدير $1) |
| pool وجوده = تأكيد migration | معلومات |

**لم يُستخدم في القرارات بعد** — فقط تحقق.

---

## 6. `market kline` — الشموع (cache 120s)

| الاستخدام |
|---|
| اتجاه 5 دقائق (نسبة الشموع الخضراء) |
| اتجاه/تسارع الحجم |
| fallback سعر (آخر close) |
| `price_change_1h` عبر شمعتين ساعيتين |

---

## 7. `market signal` — الإشارات (cache 25s) — **يعيد [] من هذا الـ host**

| الأنواع المعروفة (من GMGN) | ENZO يستخدم |
|---|---|
| price_spike, smart_money_buy, large_buy/sell, big_volume, gradual_buy, rug_hit, dev_sell/buy, new_holder_inflow, holder_outflow, fresh_wallet_buy, sniper_buy, insider_buy, whale_buy/sell, wash_trade, token_death, quick_pump/dump | ⚠️ **لا شيء فعليًا** — يعيد قائمة فارغة من هذا الـ host (API key/region limits) |

**تعويض:** عبر `buys/sells` + momentum + `smart_degen_count`.

---

## 8. الاكتشاف — `discover()` (cache 25s) — القوائم

| القائمة | حالة host | تغذي |
|---|---|---|
| `trending` | ✅ يعمل | مرشحين |
| `trenches` | ✅ يعمل | مرشحين (الرئيسي) |
| `smartmoney` | ⚠️ يعيد [] | — |
| `kol` | ⚠️ يعيد [] | — |

**كل عنصر قائمة يحمل ~30 حقلًا** (انظر 1a-1f + rug_ratio + bundler_rate + smart_degen_count + sniper_count + creator_created_count + progress...). **هذه تُستخدم في Phase-A list_screen: صفر استدعاءات إضافية.**

---

## 9. محفظة/ديف (portfolio endpoints)

### `portfolio stats` (cache 10 دقائق) — لتحليل محافظ smart money/KOL/ديف:
| الحقل | استخدام |
|---|---|
| `realized_profit` | Wallet Score / track record |
| `pnl_stat.winrate` | win rate |
| `pnl_stat.token_num` | عدد العملات المتداولة |
| `pnl_stat.avg_holding_period` | متوسط فترة الاحتفاظ |
| `common.created_token_count` | DEV_FACTORY |
| `common.tags` | هوية (smart/kol/...) |
| `common.fund_from` | معلومات |
| `followers_count`, `twitter_username` | KOL vetting |

### `portfolio created-tokens` (cache 15 دقيقة) — تاريخ الديف:
| الحقل | استخدام |
|---|---|
| `inner_count` / `total_count` | **serial launcher detection** (1473 token = MESSI example) |
| `open_count` / `open_ratio` | open ratio 0.7% = factory |
| `creator_ath_info.ath_mc` | ATH < $100K = no big hits |
| `tokens` | قائمة مفصلة (template_similarity) |

---

## 10. ماذا ينتج ENZO من كل هذا (المخرجات النهائية)

### 10a. القرار (enzo_analyze) — 6 محاور:
| المحور | مصادره الرئيسية |
|---|---|
| **security** (0-100) | token_security + holders + wallet_tags_stat + stat + deep holders |
| **wallet_behavior** | wallet_tags_stat + deep holders tags + top_trader_identity (Phase-C) |
| **dev_behavior** | creator_* + dev_events + dev_history + wallet_stats |
| **momentum** | price_change_5m/1h + buys/sells + smart_degen_count + hot_level |
| **market_structure** | mcap + liq + vol + kline 5m (rolling growth) |
| **liquidity** | liq + liq_to_vol_ratio + curve estimate |

### 10b. الحالة (security_scan):
- `security_status`: SAFE/WARNING/DANGEROUS
- `hard_reject`: [HONEYPOT, BLACKLISTED, AIRDROP_SCAM, MINT_AUTHORITY_ACTIVE, FREEZE_AUTHORITY_ENABLED, BUNDLE_DISTRIBUTION, DEV_SOLD_ALL]
- `safety_score` (5-100) مع penalties من config
- `quality` dict (~40 حقلًا)

### 10c. الـ signals (get_market_data) — ~35 حقلًا
(سعر، كاب، سيولة، فوليوم، buy_pressure، تغيرات، نسبة، عدادات محافظ...)

---

## 11. ما لا يوفره GMGN (Honest-None) — من هذا الـ host

| المعلومة | الحالة | التعويض |
|---|---|---|
| smartmoney list | [] | trenches + trending فقط |
| kol list | [] | — |
| market signal feed | [] | buys/sells + momentum + smart_degen_count |
| unique_wallet_5m | غير موجود | accumulation proxy من deep holders |
| experienced_wallet_ratio | غير موجود | None (محايد) |
| fund_from/gas_fee/creation_tool | موجود لكن غير مُسجَّل | (لا impact مبرر بعد) |
| per-smart-wallet deep PnL | موجود (عبر wallet_stats) لكن يُتخطى | توفير requests |
| curve PDA address | **غير مكشوف** | لا يمكن قراءة on-chain من GMGN |
| real/virtual SOL reserves | **غير مكشوف** | تقدير progress × 85 SOL |

---

## 12. ميزانية الطلبات (كلفة دورة كاملة)

```
اكتشاف:         5 استدعاءات (trending + trenches + smartmoney + kol + ...) → ~84 مرشح
Phase-A screen:    0 استدعاءات (من بيانات القائمة)
تحليل عميق: ~5 استدعاءات/توكن (info + security + holders + wallet_stats + dev_history)
            [Phase-C إضافي: +1 traders للتوكنات النهائية فقط]
دورة كاملة: ~20 استدعاء / 18.1s ≈ 1 استدعاء/ثانية (ضمن الميزانية)

الميزانية القصوى (free tier): ~1 req/s بأمان → بحد أقصى ~30-60 req/min
التسعير الفعلي: _RL_MIN_GAP = 1.2s → 50 req/min نظريًا
MAX_PIPELINE_PER_CYCLE = 6 → 6 تحليلات كاملة كحد أقصى كل 60 ثانية
```

---

## 13. خلاصة للمناقشة (نقاط القوة + الفجوات)

### ✅ قوي (متوفر وموثوق):
- **كل ما يتعلق بالتحليل**: السعر/الكاب/السيولة/الفوليوم/الهولدرز/الأمان/الديف/التوزيع — غني جدًا
- **Phase-A مجاني**: 30+ حقلًا من قوائم discovery بدون استدعاءات إضافية
- **كشف المصانع**: serial dev (1473 token), bundler floods, sniper floods, rat floods — كلها تعمل
- **لا يعتمد على Helius إطلاقًا** في المسارات النشطة

### ⚠️ فجوات (ما لا يوفره GMGN من هذا الـ host):
1. **smartmoney/kol lists**: يعيدان [] — الاكتشاف يعتمد على trenches + trending فقط
2. **market signal feed**: [] — تعويض عبر buys/sells + momentum
3. **لا يوجد push/stream**: كل شيء polling — لا يمكن sub-second
4. **curve PDA/reserves غير مكشوفة**: لا يمكن قراءة on-chain للـ curve عبر GMGN
5. **rate limit صارم**: ban متكرر عند الإفراط — يتطلب pacing صارم
6. **الكاب يُحسب (price × supply)** وليس حقلًا مباشرًا — مصدر خطأ محتمل إذا كانت supply غير دقيقة

### 🎯 الأسئلة للمناقشة (ما زلت أنتظر قرارك):
1. هل نقبل RPC on-chain (Helius/RPC عام) **فقط لمراقبة الصفقات المفتوحة** (السعر اللحظي)؟
   - الفائدة: لحظية 100%، بدون rate limit، يعمل أثناء ban GMGN
   - التكلفة: كسر GMGN-only حرفيًا (لكن للأسعار فقط — التحليل يبقى GMGN)
2. أم نبقى GMGN-only ونكتفي بـ: **exit-monitor دوري كل 10s** عبر GMGN مع tolerance للـ ban؟
   - الفائدة: المبدأ محفوظ 100%
   - التكلفة: تأخير 1-45s محتمل أثناء ban، rate limit قد يمنع المراقبة في أوقات الذروة
3. أم حل وسيط: **GMGN أولًا + curve تقديري (progress × 85 SOL)** كـ fallback عند ban؟
   - التقدير: progress_pct من آخر info مخزّن (حتى أثناء ban، الكاش يعمل)
   - لا يكسر المبدأ، لكنه تقريبي (يفترض curve كاملة عند 85 SOL)
