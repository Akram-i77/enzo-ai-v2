# ENZO_GMGN — Engineering Review Final Report
# 2026-08-07 (GMT+1)

المراجعة الهندسية النهائية: هل ENZO_GMGN بسيط، متوازن، ويستغل GMGN بأفضل
صورة؟ — تحليل فقط، بلا تعديلات.

---

## 1) Duplicate Signals — هل توجد إشارات تؤثر على القرار أكثر من مرة؟

### 🔴 ازدواجية بنيوية #1: `DEV_SOLD_ALL` يُفسَّر في 3 طبقات (اثنتان تؤثران)

المصدر الجذري: حقل GMGN واحد (`info.creator_close`) يُقرأ في 3 أماكن:

| الموضع | التأثير | الحالة |
|---|---|---|
| `enzo_gmgn.security_scan` (سطر ~1432) | `hard_reject.append("DEV_SOLD_ALL")` → security axis = 0 + إجبار IGNORE | ✅ يعمل |
| `enzo_security.dev_events()` (سطر ~98) | يحسب `DEV_SOLD_ALL` في events | ⚪ **ميتة** — لا أحد يستدعيها |
| `enzo_dev_analysis.dev_analysis` (سطر ~110) | `score += -30×2 = -60` في محور المطور | ✅ يعمل |

**التشخيص:** ليس تكراراً *فعلياً* على القرار (الـ hard_reject يوقف كل شيء قبل
وصول محور المطور إلى وزنه الكامل) — لكنه **تضخيم مزدوج للوزن**: نفس الحقل
يكلف −100% من security (30 نقطة وزن) و −60 نقطة إضافية من dev_behavior (20
نقطة وزن). النتيجة النهائية (IGNORE) صحيحة في الحالتين، لكن هذا انتفاخ
بنيوي. الخطأ "الحي" الوحيد: `enzo_security.dev_events()` — دالة ميتة 100%.

### 🟢 لا ازدواجية: `smart_degen_count` (مصدر واحد، محور واحد)

- `smart_degen_count` (من token info) → يُستهلك في momentum فقط (enzo_analyze ~95).
- `wallet_behavior` يعتمد على `deep_holder_analysis` (endpoint holders — مصدر
  مختلف) لعدّ `smart_count` في top-20.
- `wallet_tags_stat` (من info، صفر طلبات إضافية) → fallback فقط عندما تتعذر
  deep_holders.
- **النتيجة:** ثلاثة مصادر مختلفة تُستهلك في محاور مختلفة — لا ازدواجية. ✅

### 🟢 لا ازدواجية: `dev_team_hold_rate` (طبقتان، بدون جمع)

- `q.get("dev_team_hold_rate")` (من security → info.stat)
- `sig.get("dev_team_hold_rate") or sig.get("creator_hold_rate")` (من
  get_market_data → نفس info.stat)
- في `dev_analysis` يُفضَّل الأول ثم الثاني **كبديل** — لا جمع. ✅

### 🟢 لا ازدواجية: `top10_pct` / توزيع الحائزين

- `security_scan` و `wallet_behavior` كلاهما يستدعي `deep_holder_analysis()`
  عبر cache مشترك (TTL 5 دقائق) → **طلب GMGN واحد فقط**. ✅

### ✅ خلاصة الازدواجية

ازدواجية بنيوية واحدة (DEV_SOLD_ALL بثلاث طبقات) + دالة ميتة
(`enzo_security.dev_events`). **صفر أخطاء قرار** — لكن تبسيط مطلوب.

---

## 2) الأوزان النهائية — التوازن

### أوزان المحاور (weighted_confidence) — المجموع = 100 ✅

| المحور | الوزن | % | المنطق |
|---|---|---|---|
| security | 30 | 30% | الحماية أولاً — أعلى وزن (صحيح) |
| wallet_behavior | 20 | 20% | جودة الحائزين |
| dev_behavior | 20 | 20% | سلوك المطور |
| momentum | 15 | 15% | الزخم |
| market_structure | 10 | 10% | هيكل السوق |
| liquidity | 5 | 5% | بوابة منفصلة (min_liq) — لا تحتاج وزناً كبيراً |

**التوازن: منطقي.** 70% حماية/جودة (security + wallet + dev) مقابل 30%
زخم/سوق (momentum + structure + liquidity) — النسبة الصحيحة لبيئة memecoin
عالية المخاطر حيث العدو الأول هو الاحتيال لا السوق. ✅

### أوزان wallet_behavior (weights) — المجموع = 1.35 ⚠️ بسيطة

| الوزن | القيمة | % من الإجمالي |
|---|---|---|
| diversity | 0.40 | 29.6% |
| concentration | 0.30 | 22.2% |
| growth | 0.30 | 22.2% |
| identity | 0.35 | 25.9% |

**ملاحظة:** الكود يقسّم على `sum(weights)` المتاح (`num/den` في
wallet_behavior ~208) — **لا خطأ حسابي**، لكن المجموع 1.35 يشير إلى أن
`identity` أُضيفت لاحقاً دون إعادة تسوية. الترتيب النسبي سليم (diversity >
identity > concentration/growth)، والتسوية إلى 1.0 (0.30/0.25/0.25/0.20)
ستوضح النية فقط.

---

## 3) تكلفة كل Request إضافي مقابل قيمته

### تدفق الطلبات لكل تحليل كامل (cache بارد):

```
المرحلة 1 — discovery (مشتركة بين كل العملات، cached 25s):
  trending 1 + trenches 1 + smartmoney 1 + kol 1 + sol_price (مرة واحدة) 1
  = 5 طلبات / دورة  →  0.2 req/s

المرحلة 2 — لكل عملة تصل إلى التحليل العميق (engine ≤12/دورة، pump ≤6/دورة):
  token_info 1 (TTL 30s) + token_security 1 (TTL 300s) + token_holders 1 (TTL 300s)
  = 3 طلبات / عملة  ← الأساس

المرحلة 3 — إضافات:
  wallet_stats 1 (TTL 600s) — إلزامية حالياً
  dev_history 1 (TTL 900s) — إلزامية حالياً
  kline 1 (بدون TTL!) — market_structure
  token_traders 1 (TTL 300s) — config OFF
  token_pool 1 (TTL 300s) — غير موصولة بعد
```

### جدول التكلفة/القيمة (مقاس من السجل: 1451 طلباً موثقاً):

| الوظيفة | مرات الاستدعاء | القيمة الفعلية | الحكم |
|---|---|---|---|
| `token_info` | 1/عملة | الأساس (سعر/سيولة/حجم/وسوم/إحصائيات) | ✅ أساسي |
| `token_security` | 1/عملة | honeypot/taxes/renounced — الحماية | ✅ أساسي |
| `token_holders` | 1/عملة | تركّز + هوية + سلوك top20 | ✅ أساسي |
| `wallet_stats` | 1/عملة (TTL 10m) | DEV_PROFIT_FACTORY (dev ربح $20K+ عبر 20+ عملة) | ✅ يستحق — يكشف pump-factory |
| `dev_history` | 1/عملة (TTL 15m) | DEV_FACTORY_DEEP + DEV_NO_BIG_HITS (ATH<$100K) | ✅ يستحق — يكشف serial launcher |
| `kline` (5m) | 1/عملة **بلا TTL** | green-ratio + vol-trend (عمق market_structure) | 🟡 يستحق لكنه **أغلى مما يجب** — يُستدعى في كل تحليل دون cache |
| `token_traders` | 1/عملة (OFF) | هوية المتداولين + ربحهم | 🟡 قيمة عالية **لكن موصولة في المكان الخاطئ** (في get_market_data → تُستدعى على كل mint قبل التصفية) |
| `token_pool` | 0 (غير موصولة) | سيولة حقيقية + pool address | 🟡 مكررة (تقدير curve يعمل) — تُفعَّل عند الحاجة فقط |
| `hot_searches` | 0 (فارغة من هذا المضيف) | — | ⚪ صفر قيمة هنا (تبقى OFF) |
| `market_signals` | 0 (فارغة من هذا المضيف) | — | ⚪ صفر قيمة هنا (تبقى OFF) |
| `smart_wallet_activity` | 0 (فارغة) | — | ⚪ صفر قيمة هنا (تبقى OFF) |

### تحليل التكلفة الكلية (الحد ≈ 1 req/s):

- **حلقة discovery:** 5 طلبات/25s = **0.2 req/s** ✅
- **تحليل عميق واحد:** 6 طلبات بفاصل 1.2s ≈ **7.2s/عملة**
- **دورة pump كاملة** (6 عملات، الميزانية): 36 + 5 = **41 طلب/60s = 0.68 req/s** ✅
- **دورة engine كاملة** (12 عملة): 72 + 5 = **77 طلب/دورة** — تلامس الحد
  عند اجتماعها مع serve (prices كل 2s) 🟡
- **مع تفعيل top_traders:** +6-12 طلب/دورة → **~1.1 req/s — يتجاوز الحد** 🔴

**الحكم:** الإضافات الأساسية (wallet_stats + dev_history) تستحق تكلفتها —
كلتاهما تكشف فئة "pump-factory" التي كانت تمر سابقاً. kline يستحق لكنه يحتاج
cache. top_traders ممتاز القيمة لكنه يجب أن ينتقل من get_market_data إلى
المرحلة النهائية فقط (قبل BUY مباشرة) وإلا كسر الميزانية.

---

## 4) تبسيطات مقترحة (بدون فقدان قدرة)

| # | الموقع | المشكلة | الاقتراح |
|---|---|---|---|
| A | `enzo_security.dev_events()` | دالة ميتة (لا مستدعي) | حذفها (أو تحويلها لـ alias على `quality.dev_events`) |
| B | `DEV_SOLD_ALL` ×2 | نفس الحقل: hard_reject (−100% security) + −60 في dev | إبقاء hard_reject كحارس، وتخفيف عقوبة dev إلى −30 (إزالة التضخيم ×2) |
| C | `token_traders` في `get_market_data` | تُستدعى على كل mint قبل التصفية | نقلها إلى نهاية التحليل (قبل BUY فقط) أو إبقاؤها OFF |
| D | `wallet_behavior.weights` = 1.35 | مجموعة غير موحّدة | تسوية إلى 1.0 (0.30/0.25/0.25/0.20) |
| E | `enzo_fetch_jupiter.py` | adapter شفاف 27 سطراً | اختياري: حذفه وتوجيه enzo_run إلى enzo_gmgn مباشرة (أو إبقاؤه للتوافق) |
| F | `kline` في market_structure | استدعاء لكل تحليل بلا TTL | إضافة cache 120s في enzo_gmgn.kline — يوفر ~12 طلب/دورة فوراً |
| G | `enzo_curve.py` | adapter سليم | **لا تغيير** — التبسيط تم داخله بالفعل |
| H | hot_searches/market_signals/smart_wallet_activity | فارغة من هذا المضيف | **لا حذف** — قد تعمل من مضيف/مفتاح أعلى؛ تبقى OFF افتراضياً |

---

## 5) الخلاصة الهندسية

### ✅ ما هو سليم ومثبت:
1. **طبقة بيانات موحدة** (enzo_gmgn.py = GMGN الوحيد) — كل الوحدات adapters
   رقيقة؛ لا Helius/Birdeye/Jupiter/DexScreener.
2. **توازن الأوزان منطقي:** 70% حماية/جودة مقابل 30% زخم/سوق.
3. **لا ازدواجية فعلية في الإشارات** — الحقول متعددة الاستخدام (top10_pct,
   smart_degen, dev_team_hold) تُقرأ من cache مشترك أو مصدر مختلف.
4. **الأمان متعدد الطبقات** يعمل (GLOOP IGNORE عبر BUNDLE_DISTRIBUTION 100% +
   DEV_SOLD_ALL + DEV_FACTORY 15؛ Smokey DANGEROUS conf 17→14).
5. **ميزانية الدورة** (MAX_PIPELINE_PER_CYCLE=6) + GRACE 8s + ban file مشترك
   — أوقفت حلقة الحظر المزمن نهائياً.
6. **معدل الطلبات الحالي 0.68 req/s** — تحت حد GMGN، مستقر.

### ⚠️ التحسينات الموصى بها (بالأولوية):
1. **cache لـ kline (TTL 120s)** — توفير ~12 طلب/دورة، أخفض أثراً على الحد.
2. **نقل token_traders إلى المرحلة النهائية** — قيمة عالية بدون كسر الميزانية.
3. **حذف الدالة الميتة + إزالة تضخيم DEV_SOLD_ALL** — نظافة بنيوية.
4. **تسوية weights إلى 1.0** — وضوح النية.

### الحكم النهائي:
**ENZO_GMGN جاهز ليُعتبر النسخة النهائية.** بسيط في المعمارية (طبقة بيانات
واحدة، 6 محاور واضحة)، متوازن في الأوزان (حماية أولاً)، ويستغل GMGN بكفاءة
(0.68 req/s مع قيمة قصوى لكل طلب). التحسينات الأربعة أعلاه تجميلية/وقائية
وليس فيها ما يمنع الإطلاق — يمكن تنفيذها لاحقاً على دفعة واحدة.

---
*المرجع: الكود الفعلي (enzo_*.py) + سجل API الفعلي (1451 طلباً) + أوزان
enzo-config.yaml*
