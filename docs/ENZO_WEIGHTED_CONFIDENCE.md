# ENZO — نظام الثقة الموزونة بالمحاور (تقرير التطوير)

تاريخ: 2026-07-11 | المرحلة 1 (مكتملة وقابلة للتشغيل)

الهدف: تحويل ENZO من "بوت فلترة بشروط ثابتة" إلى "متداول يقيّم كل محور independently"
ويدمج النتائج بوزن، مع الحفاظ على الأدوات المجانية فقط واحترام الأداء.

---

## ١) الملفات الجديدة

| الملف | الوظيفة |
|---|---|
| `enzo_config.py` | محمّل إعدادات موحّد (`load_config`) + `clamp` — يتجنّب الاستيراد الدائري مع `enzo_analyze`. |
| `enzo_cache.py` | كاش بسيط على الملف بصلاحية TTL — يخفّف طلبات RPC المجانية (توزيع الحائزين، تتبّع المطور، عيّنات هيكل السوق). |
| `enzo_wallet_behavior.py` | محور **سلوك المحافظ** — يقيس جودة المحافظ ببيانات حقيقية متاحة فقط (توزيع الملكية + نمو المشترين). |
| `enzo_dev_analysis.py` | محور **سلوك المطور** — يراقب احتفاظ/بيع/شراء/توزيع/سحب سيولة المطور وترجمتها لأحداث ذات تأثير قابل للضبط. |
| `enzo_market_structure.py` | محور **هيكل السوق** — يأخذ عيّنات mc/liq/vol/buyers مع الوقت (مخزّنة + مخفّضة التردد) ويحسب سرعة النمو/التسارع. |

## ٢) الملفات المعدّلة

- `enzo_security.py`: أضيف `cached_holder_distribution()` (مخبّأ، يستثني حساب الكيرف لـ pump) + `security_axis()` (يحوّل نقاط الروگ إلى درجة أمان 0–100).
- `enzo_analyze.py`: **أُعيد بناؤه** ليحسب 6 محاور مستقلة ثم يدمجها بوزن → `confidence_score` (مع الحفاظ على البوابات الصلبة والحقول المتوافقة مع بقية النظام). يُنتج `axis_scores`, `weights_used`, `features`.
- `enzo_learn.py`: ترقية إلى تعلّم إحصائي — يخزّن **متجه خصائص** لكل صفقة، يحسب **نسبة نجاح كل خاصية/محور**، ويقترح **تعديل الأوزان** (`suggested_weights`) مع زر إيقاف. أُضيف أمر `reset`.
- `enzo_portfolio.py`: حجم صفقة **ديناميكي** حسب الثقة (أشرطة `position_sizing`) + تخزين `axis_scores`/`features` على المركز لإطعام التعلّم.
- `enzo-config.yaml`: أُضيفت أقسام `weighted_confidence`, `wallet_behavior`, `dev_behavior`, `market_structure`, `position_sizing`, `learning`, `cache`.
- `enzo_dashboard.py`: بطاقات **محاور التحليل** + قسم **التعلّم** (أعلى/أسوأ الخصائص، نسب نجاح المحاور).

---

## ٣) طريقة عمل النظام بعد التعديل

لكل توكن: `enzo_run.run` → `get_market_data` + `security_scan` → `enzo_analyze.analyze`:

1. **计算每个 محور بشكل مستقل (0–100، أعلى = أفضل):**
   - `security` ← `enzo_security.security_axis` (محوّل من نقاط الروگ/الصلاحيات).
   - `liquidity` ← سيولة حالية مقابل الحد الأدنى.
   - `momentum` ← زخم 1h/24h + ضغط الشراء.
   - `wallet_behavior` ← تنوّع/تركيز الملكية + نمو المشترين.
   - `dev_behavior` ← أحداث المطور (احتفاظ/بيع/شراء/توزيع/سحب سيولة).
   - `market_structure` ← سرعة نمو mc/liq/vol/buyers عبر الزمن.
2. **دمج بالوزن:** `confidence = Σ(axis_i × weight_i) / Σ(weight_i)`.
3. **البوابات الصلبة** (روگ/صلاحية/سيولة/حائزين/حجم/mcap) تفرض `IGNORE`.
4. **البوابات الناعمة** (ضغط شراء، زخم 1h سالب) تخفض محورها → تخفض الثقة طبيعياً.
5. إذا `confidence ≥ min_confidence_score` و`security_status == OK` → **BUY**، وإلا **WAIT**.

---

## ٤) حساب الثقة (مثال حقيقي من الاختبار)

أوزان افتراضية: `security:30, wallet:20, dev:20, momentum:15, market_structure:10, liquidity:5`

مثال BUY (توكن جيد):
```
security=90  wallet=90  dev=60  momentum=76  market_structure=50(neutral)  liquidity=66
confidence = 90*.30 + 90*.20 + 60*.20 + 76*.15 + 50*.10 + 66*.05 = 77  → BUY
```
مثال IGNORE (روگ): `security=0` + بوابة صلبة → `IGNORE` (الثقة 26 لكن القرار محسوم بالبوابة).
مثال WAIT (سيولة/ثقة منخفضة): بوابات ناجحة لكن `confidence < 55` → `WAIT`.

---

## ٥) نظام التعلّم (إحصائي، بدون ML)

- عند إغلاق كل صفقة: `enzo_learn.record_outcome(record)` يسحب `features` + `axis_scores`
  المخزّنة على المركز عند الفتح.
- يحسب **نسبة نجاح كل خاصية** (مثل `dev_holding`، `bundle_risk`، `wallet_diversity_high`):
  `win_rate = wins / n`.
- يحسب **نسبة نجاح كل محور** (متوسط درجة المحور في الصفقات الرابحة مقابل الخاسرة).
- `suggested_weights(base)` يعدّل أوزان المحاور تلقائياً حسب نسبة نجاحها:
  `weight_i *= (0.5 + win_rate_i)` (50% نجاح → ×1، 80% → ×1.3، 20% → ×0.7).
- التطبيق الفعلي محمي بزرّ: `learning.apply_weight_adjustments: false` (معطّل افتراضياً).
- إعادة الضبط: `python3 enzo_learn.py reset`.

---

## ٦) تحليل المحافظ (بيانات حقيقية فقط)

مؤشرات حقيقية مستخدمة:
- `top10_pct` / `top1_pct` (Helius `getTokenLargestAccounts`، مع استثناء حساب الكيرف لـ pump) → **تنوّع** (`100−top10`) و**تركيز** (`100−top1`).
- `uniqueWallet5m` مقابل تاريخه (Birdeye) → **نمو المشترين** ونسبة المحافظ الجديدة.
- **لا توجد محاكاة** لعمر المحفظة أو نجاحاتها السابقة (غير متاحة مجانياً) — تُرمى صراحةً كـ `None`/`unknown` ولا تُستخدم في الثقة.

التصنيف: خبيرة / طبيعية / مشبوهة (مركّزة) / مخصصة للقنص / غير معروفة.

---

## ٧) تحليل المطور (أحداث قابلة للضبط)

تحديد المطور:
- غير-pump: `mint_authority` من حساب المينت on-chain.
- pump: أكبر حائز **غير الكيرف** (proxy للفريق/الباندل).

أحداث (تُقارن عيّنة الحالة المخبّأة بعيّنة سابقة):
`DEV_HOLDING` (+), `DEV_SELLING` (− قوي), `DEV_BUYING_MORE` (+ قوي),
`DEV_DISTRIBUTING` (+ خفيف), `DEV_REMOVED_LIQUIDITY` (− قوي جداً).

كل تأثير في `enzo-config.yaml → dev_behavior.impact_*`.

---

## ٨) تحليل هيكل السوق (زمني)

- عيّنات mc/liq/vol/buyers تُحفظ في `enzo-market-structure.json` (نافذة متحركة،
  أدنى فاصل `min_sample_interval_sec: 60` لحماية RPC).
- تحسب: نمو mc، نمو السيولة، تسارع الحجم، زيادة المشترين، اختلال الميزان (من زخم 1h).
- توكن ينمو بسرعة → درجة أعلى من البطيء/الراكد.

---

## ٩) خيارات الإعداد الجديدة (`enzo-config.yaml`)

```yaml
weighted_confidence: {security:30, wallet_behavior:20, dev_behavior:20, momentum:15, market_structure:10, liquidity:5}
wallet_behavior: {neutral_score:50, weights:{diversity:0.4, concentration:0.3, growth:0.3}}
dev_behavior: {neutral_score:50, track_ttl_sec:1800, sell_threshold_pct:2, buy_threshold_pct:2,
               liq_remove_pct:0.2, impact_dev_holding:10, impact_dev_selling:-30,
               impact_dev_buying:25, impact_dev_distributing:8, impact_dev_remove_liq:-40}
market_structure: {neutral_score:50, min_sample_interval_sec:60, max_samples:30}
position_sizing:
  confidence_bands:
    - {min:55,max:60,risk_pct:1.0}
    - {min:61,max:70,risk_pct:2.0}
    - {min:71,max:80,risk_pct:3.0}
    - {min:81,max:90,risk_pct:4.0}
    - {min:91,max:100,risk_pct:5.0}
learning: {enabled:true, apply_weight_adjustments:false, min_samples_for_adjust:10}
cache: {holder_dist_ttl:600}
```

---

## ١٠) تأثير الأداء

- **كاش** (`enzo_cache`) يقلّل تكرار `getTokenLargestAccounts` / `getTokenSupply` وطلبات المطور.
- **هيكل السوق** محدود بفاصل 60s بين العيّنات و30 عيّنة كحد أقصى لكل توكن.
- المحاور الجديدة **لا تضيف أي طلب RPC في المسح الأول** (تُرجع neutral إن لا بيانات)، وتعيد استخدام نفس توزيع الحائزين الذي تجلبه طبقة الأمان.
- كل الطلبات عبر أدوات مجانية (Helius/Birdeye/DexScreener) — لا أي خدمة مدفوعة.

---

## ١١) خطوات التشغيل

1. `python3 enzo_run.py <MINT>` — فحص توكن واحد (يعرض المحاور + الثقة).
2. `python3 enzo_analyze.py` (من stdin بدمج JSON) — اختبار يدوي.
3. `python3 enzo_dashboard.py` — توليد لوحة التحكم ببطاقات المحاور + التعلّم.
4. `python3 enzo_learn.py` — عرض حالة التعلّم؛ `python3 enzo_learn.py reset` لمسحها.
5. التشغيل عبر تيليغرام ▶ تشغيل (يبقى البوت PAUSED حالياً؛ والـ risk halts معطّلة أثناء التعلّم).

## ١٢) القيود الصريحة (أمان/صدق)

- **لا توجد** بيانات عمر المحفظة أو نجاحاتها السابقة أو تاريخها الكامل (تتطلب أدوات مدفوعة) — لم تُحاكَى.
- `experienced_wallet_ratio` = `None` صراحةً (غير متاح مجانياً).
- `fresh_wallet_ratio` / `sniper_wallet_ratio` تقريبية من نمو المشترين + التركيز، وموسومة كذلك.
- PumpPortal محظور جغرافياً (403) من هذا المضيف — نعتمد Helius لاكتشاف التوكنات.
