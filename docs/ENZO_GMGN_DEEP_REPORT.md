# ENZO GMGN-DEEP — التقرير النهائي (2026-08-05)

**المرحلة:** استغلال بيانات GMGN بأقصى درجة ممكنة داخل معمارية ENZO الحالية
**القيد:** GMGN فقط (لا Helius / DexScreener / Birdeye / Jupiter) — حُترم بالكامل
**الفلسفة:** كل معلومة مضافة لها تأثير منطقي ومبرر على جودة القرار؛ لا مؤشرات "لأجل الوجود"

---

## 1) ما الذي تم تعديله

### 1.1 `enzo_gmgn.py` — طبقة البيانات (قلب التغيير)

| الوظيفة الجديدة | مصدر GMGN | الكاش | القيمة المنطقية |
|---|---|---|---|
| `deep_holder_analysis(mint)` | `token holders` | 5 دقائق | هوية + سلوك كل حامل في top-20: maker_token_tags (smart/whale/kol/bundler/sniper/rat/fresh)، تكلفة الدخول (avg_cost)، PnL غير المحقق، عدد صفقات الشراء/البيع الحالية، عمر المحفظة، ساعات الحيازة، + إجماليات (top10_accumulating/dumping، متوسط الربح، متوسط عمر المحافظ) |
| `holder_distribution()` | نفس endpoint | نفس الكاش | أعيد بناؤها فوق `deep_holder_analysis` → **استدعاء واحد** لكل عملة لكل TTL (كانت دعوتين منفصلتين سابقاً) |
| `wallet_stats(wallet)` | `portfolio stats` | 10 دقائق | **Wallet Score**: realized_profit، winrate، token_num، avg_holding_period، created_token_count، tags — لجودة محفظة المطوّر/الذكي |
| `dev_history(wallet)` | `portfolio created-tokens` | 15 دقيقة | **Dev History**: عدد العملات المطلقة، open_ratio، أفضل ATH — لكشف المصنّعين المتسلسلين |
| `list_screen(candidate)` | بيانات الـ **list فقط** | — | **Phase-A بـ 0 طلبات**: rug_ratio، bundler_rate، entrapment_ratio، sniper_count، smart_degen_count، dev_hold_pct، creator_close، honeypot، renounced_mint/freeze، بوابات liquidity/mcap/top10 |
| عقوبات `safety_score` الجديدة | info (stat + wallet_tags_stat) + holders | — | bundler_flood (≥500/100/30)، sniper_flood (≥100)، rat_flood (≥20)، bundlers_in_top20 (≥10)، top10_dumping (≥5)، dev_team_hold (≥30%)، dev_factory (≥100 عملة)، top70_sniper (≥30%) — **كلها من config** |

### 1.2 المحاور (كلها داخل المحاور الموجودة — لا طبقات مكررة)

- **`enzo_wallet_behavior.py`** (أُعيدت كتابتها): استهلاك كامل لـ deep holder + wallet_tags_stat. **درجة هوية جديدة** (identity): مكافأة smart/whale/kol، عقاب bundler/sniper/rat، عقاب ضغط البيع والبيع الحالي، مكافأة التراكم، كشف محافظ جديدة. وزن `identity: 0.35` في config. تصنيفات: خبيرة / طبيعية / باندرر-مشبوهة / ضغط بيع / مركزة.
- **`enzo_dev_analysis.py`** (أُعيدت كتابتها): `dev_team_hold_rate` الحقيقي (بدل وكيل top1)، `creator_token_status` (DEV_SOLD_ALL = رفض أمني + عقوبة مضاعفة)، `creator_created_count` + open_ratio (DEV_FACTORY)، `dev_history` (مصنّع متسلسل + ATH <$100K = DEV_NO_BIG_HITS)، `wallet_stats` (DEV_PROFIT_FACTORY: ربح محقق >$20K عبر ≥20 عملة)، تتبع تغيّر حصة المطوّر عبر الزمن (DEV_SELLING/BUYING/HOLDING).
- **`enzo_market_structure.py`** (أُعيدت كتابتها): نافذة متحركة (نمو mcap/liq/vol/المشترين + تسارع الحجم) + **kline 5m** من GMGN (نسبة الشموع الخضراء، اتجاه الحجم، اتجاه آخر إغلاق) — تفصل الارتفاع الصحي عن القنبلة الواحدة. العينة الأولى **متحفظة** (لا 100 أبداً).
- **`enzo_analyze.py`**: محور momentum يستخدم الآن price_change_5m، smart_degen_count، hot_level، buy_ratio (شراء/بيع). **الميزات 17** (dev_factory، smart_money_in، whale_in، bundler_in، rat_in، top10_dumping، dev_selling...). **DANGEROUS بدون hard_reject يُرفض الآن** (إشارات rug عميقة متراكمة).
- **`enzo_engine.py`**: **Phase-A قبل التحليل العميق** — البحث عن كل mint في قوائم discovery المخزنة (0 طلبات) → `list_screen` → IGNORE + إدخال dashboard عند التخطي. عدّاد `state.list_skips`.
- **`enzo-config.yaml`**: حدود وعقوبات جديدة كلها ضمن الأقسام القائمة (scam_detection / wallet_behavior / dev_behavior) — لا أقسام جديدة.

---

## 2) كيف أصبحت ENZO تستفيد من GMGN مقارنة بالسابق

| البُعد | قبل | بعد |
|---|---|---|
| هوية الحاملين | عدّاد حاملي فقط | **maker_token_tags لكل حامل top-20** + wallet_tags_stat لكل المحفظة (smart/whale/kol/bundler/sniper/rat/fresh/renowned) |
| سلوك الحاملين | — | avg_cost + PnL + صفقات بيع/شراء حالية + عمر المحفظة + top10_accumulating/dumping + ضغط بيع top-10 |
| المطوّر | وكيل top1 فقط | dev_team_hold_rate الحقيقي + creator_token_status + عدد العملات المطلقة + open_ratio + **سجل الإطلاق العميق** (dev_history) + **ربح المطوّر المحقق** (wallet_stats) |
| الأمان | تركّز فقط | + bundler_flood / sniper_flood / rat_flood / bundlers_in_top20 / top10_dumping / dev_factory / DEV_SOLD_ALL → **DANGEROUS من إشارات عميقة وحدها** |
| السوق | لقطة لحظية | نمو mcap/liq/vol/المشترين عبر نافذة + تسارع الحجم + **kline 5m** (شموع خضراء/اتجاه حجم) |
| الزخم | 1h فقط | + 5m، smart_degen_count، hot_level، نسبة الشراء/البيع |
| فلترة المرشحين | كل مرشح يتحلل عميقاً | **list_screen بـ 0 طلبات** يرفض 50%+ قبل أي استدعاء |
| الثقة | 6 محاور ثابتة | 17 ميزة تعلم + هوية + عمق مطوّر/حاملين |

---

## 3) ما بقي غير مستغل ولماذا

| الحقل | الحالة | السبب |
|---|---|---|
| قوائم smartmoney/kol | غير متاحة من هذا المضيف (فارغة) | قيد GMGN خارجي — عُوّض بهويات الحاملين والـ degen count |
| `unique_wallet_5m` / `unique_wallet_history_5m` | غير مقدَّمة من GMGN | النمو البديل = مؤشر تراكم الحاملين العميق (accumulating) |
| `experienced_wallet_ratio` | يبقى None | يتطلب تاريخ محافظ مدفوع — لا نختلق أرقاماً |
| `fund_from`، `gas_fee`، `creation_tool`، `bluechip_owner_percentage`، `callout_count` | مُجمَّعة في list items لكن غير مُحرَزة | لا تأثير منطقي مبرر بعد — تُحفظ للسجل/التدقيق |
| فحص PnL تفصيلي لكل smart wallet على حدة | مؤجّل | N طلبات لـ N محافظ ذكية — فقط عند الطلب صراحةً (حفظ الحد) |
| محاور جديدة (اجتماعية/لغة/صور) | غير مضاف | خارج بيانات GMGN المتاحة للعملات؛ لا مبرر منطقي |

---

## 4) نتائج الاختبارات الحية

### 4.1 Smokey `2bKAMjEsZ3mTf9Gmtm3HFFsdpghd4gajFVB4c4hppump` — قضية الإثبات

| | قبل | بعد |
|---|---|---|
| security | **50 WARNING** (كان سيمر) | **DANGEROUS** — BUNDLER_FLOOD(1000), BUNDLERS_IN_TOP20=11-17, DEV_FACTORY(10077) |
| dev_behavior | 60 (DEV_HOLDING افتراضياً) | **0** — DEV_SOLD_ALL + DEV_FACTORY(10077) + عقوبة 53 |
| wallet_behavior | 25-47 | 25-31 — 17 باندرر في top20، تصنيف "باندرر/مشبوهة" |
| القرار | WAIT conf ~41 | **IGNORE conf 17 → 14** |

**دليل قيمة wallet_stats:** محفظة مطوّر Smokey: **ربح محقق $137,570 عبر 10,077 عملة (winrate 83%)** — نمط مصنع ضخ/تفريغ مكتمل، أصبح مكشوفاً.

### 4.2 جولة كاملة

```
discovery:          5 طلبات  → 84 مرشحاً (3.2s)
list_screen:        0 طلبات  → 40 PASS / 44 SKIP (0.00s)
تحليل عميق ×3:      15 طلباً (5/عملة) → 15.0s  (~2.5s/عملة)
المجموع:            20 طلباً في 18.1s ≈ 1.1 طلب/ثانية
endpoints:          gmgn_token_info, gmgn_token_security, gmgn_token_holders,
                    gmgn_wallet_stats, gmgn_dev_history
```

- **قرارات حية نموذجية:** CORIN IGNORE 45 (أمن) | Slots WAIT 22 | MCM WAIT 38 | NUKUTA WAIT 21 (wallet 3) | BBABAY WAIT 46 | $Shame IGNORE 42 (أمن) | 0.67 IGNORE 31 (أمن) | POSTER WAIT 44.
- **18 وحدة** تُترجم وتُستورد بدون أخطاء.
- `scan_once` يحترم إيقاف Telegram (يولّد الـ dashboard فقط) — لم يُكسر.
- حادثة 3DCAT (نتيجة null) = اصطدام مؤقت بالـ rate limit في حلقة اختبار سريعة — تعافى النظام عبر ban-aware retry (مصمَّم).

---

## 5) تأثير التعديلات

| المقياس | قبل | بعد |
|---|---|---|
| جودة القرار | Smokey يمر (WARNING) | **يُرفض بـ IGNORE conf 14** عبر 3 مسارات مستقلة (أمن + مطوّر + محافظ) |
| اكتشاف المصنّعين | غير ممكن | DEV_FACTORY + DEV_PROFIT_FACTORY + DEV_NO_BIG_HITS |
| اكتشاف الباندرر | عدّاد حاملي فقط | 3 طبقات: list bundler_rate (0 طلب) + wallet_tags bundler flood + bundlers_in_top20 |
| طلبات/جولة | ~25 (كل مرشح يتحلل) | **20 لجولة كاملة** (45-53% مرشحين يُرفضون بـ 0 طلبات عبر list_screen) |
| طلبات/تحليل عميق | 3-4 | **5** (info, security, holders, wallet_stats, dev_history — كلها مخزّنة وتحدث للمرشحين المارين فقط) |
| زمن الجولة | ~25s (12 تحليلاً) | ~18s (3 تحليلات عميقة كاملة + فحص 84 مرشحاً) |
| احترام 1 req/s | نعم | نعم (قياس 1.1/s مع فجوة 350ms + ban-aware retry) |

---

## 6) ملفات متأثرة

- **معدَّلة:** `enzo_gmgn.py`، `enzo_wallet_behavior.py` (إعادة كتابة)، `enzo_dev_analysis.py` (إعادة كتابة)، `enzo_market_structure.py` (إعادة كتابة)، `enzo_analyze.py`، `enzo_engine.py`، `enzo-config.yaml`، `MEMORY.md`، `memory/2026-08-05.md`
- **غير مكسورة (تحقّق):** Telegram (botctl)، Dashboard، Paper Trading، Audit، Learning (17 ميزة)، Risk Management، Confidence System (6 أوزان ثابتة)، enzo_pump، enzo_portfolio، enzo_daily

## 7) الخطوات التالية المقترحة (بانتظار قرارك)

1. إعادة تفعيل التدفقات — البوت **متوقف** عبر Telegram منذ 2026-07-16
2. إعادة تفعيل كرون مسح الواتشليست (c8b31bd6) — معطّل بسبب انقطاع المزوّد سابقاً وليس ENZO
3. اختياري: وضع تنفيذ حقيقي (محفظة/مفاتيح — مخاطرة عالية، موافقة صريحة)
4. اختياري: أول git commit للشجرة الحالية
