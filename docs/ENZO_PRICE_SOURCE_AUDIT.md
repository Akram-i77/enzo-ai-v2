# ENZO — Price-Source Audit (read_bonding_curve / pricefeed / check_exits)

**Date:** 2026-08-10 01:05 GMT+1 · **Mode:** AUDIT ONLY (no files modified)
**Requested by:** Akram (question: "لا نستطيع الحصول على السعر اللحظي عند فتح صفقة لأن GMGN لا يسمح بذلك؟")

---

## 1. هل `read_bonding_curve()` يعطي market cap/price صالح فعلاً؟

**نعم — لكنه ليس on-chain إطلاقًا. إنه GMGN.**

`enzo_curve.py` (الملف كامل، 130 سطر) بعد ترحيل 2026-08-05:

```python
def _rpc_url():
    """No on-chain RPC anymore — GMGN-only. Returns '' so old WS code no-ops."""
    return ""

def _rpc(method, params):
    raise RuntimeError("on-chain RPC disabled (GMGN-only mode)")

def read_bonding_curve(mint: str) -> dict:
    """GMGN-only bonding-curve replacement (same output contract)."""
    return enzo_gmgn.read_bonding_curve(mint)
```

**المصدر الحقيقي** = `enzo_gmgn.read_bonding_curve()` (سطر 1591)، والذي:
1. `token_info(mint)` — GMGN CLI `token info` (cache 30s)
2. يملأ الفراغات من discovery cache (صفر استدعاءات إضافية)
3. `progress` من `launchpad_progress` GMGN
4. السعر = `_price_of(info)` → حقل `price` في استجابة GMGN (سطر 313)
5. الكاب = `price × circulating_supply` (pump ~1e9) (سطر 343)
6. fallback سعر: آخر شمعة kline 1m (أيضًا GMGN)

**الخلاصة:** القيم صالحة (مصدر GMGN موثوق) لكنها **ليست لحظية من السلسلة** — بل لقطة من API مع cache 10-30 ثانية.

---

## 2. هل يعمل بعد migration إلى Raydium/AMM؟

**نعم، جزئيًا — لكن يتدهور:**

| الحالة | progress | ماذا يفعل `read_bonding_curve` |
|---|---|---|
| Pre-migration (bonding) | 0-99% | يعمل: سعر/كاب/progress من GMGN info ✅ |
| لحظة migration | 100% | `complete=True`, phase="migrated" — لكن **كل حقول الـ curve (real_sol_reserves, virtual_sol_reserves, curve_address) = None** — GMGN لا يعرض الـ curve PDA |
| بعد migration (Raydium) | >100 → `_progress_pct` يقلص إلى 100 | يستمر عبر `token_info` — **السعر/الكاب يبقى صالحًا** (GMGN يتبع token عبر AMM) ✅ |
| فشل GMGN / ban | — | يرجع `{"exists": False}` فقط إذا لم يكن pump token معروفًا |

**لكن الأهم:** بعد migration **لا يوجد أي استخدام للـ curve** — `get_market_data` يعمل عبر GMGN مباشرة. لا يوجد انتقال "تلقائي" من curve إلى AMM — كلاهما نفس المصدر (GMGN info).

---

## 3. المصدر الحالي للسعر في check_exits و pricefeed

### `check_exits(current_mcaps: dict)` — enzo_portfolio.py:313
- **لا يجلب الأسعار بنفسه إطلاقًا.** يستقبل `{mint: live_market_cap}` جاهزًا من المتصل.
- يقرأ `entry_market_cap` المخزن في الموضع ويحسب `pct = mcap/entry_mc - 1`.
- **المتصلون:** (فحص شامل — فقط هذان)
  - `enzo_pump.py:122-124` — بعد فتح صفقة: `mc = current_market_cap(mint)` ثم check_exits **مرة واحدة فقط** (لحظة الفتح)
  - `enzo_engine.py:227-228` — عند تحليل mint موجود في open_positions: `mc = decision.get("market_cap_usd") or current_market_cap(mint)` ثم check_exits (أثناء scan، مرة واحدة لكل دورة)

### `enzo_pricefeed.py` — (GMGN-only polling منذ الترحيل)
- `PriceFeed._run()` loop: لكل mint مشترك، `get_price(mint)` كل `fresh_secs` (افتراضي 5s)
- `get_price()` → `enzo_gmgn.get_market_data(mint)` → `price_usd`، fallback `read_bonding_curve`
- **cache داخلي** `fresh_secs` + كاش GMGN (market_data TTL 10s)
- **اكتشاف حرج:** `get_price()` **لا يغذي check_exits إطلاقًا!** — pricefeed يجمع الأسعار في `self._cache` الخاصة به، ولا أحد يستدعي `get_price()` من خارج pricefeed نفسه (الفهرس: فقط `subscribe()` و`_run()` و`__main__`). **الـ pricefeed thread شبه معطل وظيفيًا — لا يشارك في exit monitoring.**

### أين يمر السعر الحي فعلاً لـ check_exits؟
- **فقط عند فتح صفقة** (pump) أو **عند تحليل mint مفتوح** (engine scan).
- **لا يوجد loop دوري يغذي check_exits بالأسعار الحية.** إذا لم يُحلل mint المفتوح من جديد (الـ scan يركز على الجديد)، **لا تحديث للـ uPnL ولا exits إلا بالصدفة.**

---

## 4. Polling interval الفعلي

| المكوّن | الفاصل | ملاحظات |
|---|---|---|
| pump `_pump_loop` | 30s | اكتشاف عملات جديدة فقط |
| pump worker (pipeline) | ≥2s بين التشغيلات + ميزانية 6/دورة/60s | تحليل عملة واحدة |
| pricefeed thread | 5s لكل mint مشترك | **لا يغذي exits** (عيب) |
| engine `--loop` | 60s افتراضي | ليس مشغّلاً الآن (لا process) |
| **check_exits الفعلي** | **عند الفتح فقط + عند إعادة تحليل mint المفتوح** | **لا يوجد جدولة دورية ثابتة** |

**الواقع اليوم:** exits تُراقب تقريبًا **بشكل متقطع** — عندما يعيد pump/engine تحليل mint مفتوح (نادر) أو عند فتح صفقة جديدة.

---

## 5. هل القراءة on-chain عبر Helius أم RPC آخر؟

**لا يوجد أي قراءة on-chain في النظام النشط.**

- `enzo_curve.py`: `_rpc()` يرمي `RuntimeError("on-chain RPC disabled")` — **معطّل عمدًا**.
- `enzo_security.py:59-69`: `helius_holder_distribution()` / `helius_mint_info()` موجودة لكنها **legacy helpers** — docstring: "never touch Helius/Birdeye again".
- `enzo_pump.py:62`: `helius_poll()` → **no-op** (يعيد `[]`).
- لا يوجد `getAccountInfo` / `getSignaturesForAddress` / WebSocket في أي مسار نشط.
- دوال base58/PDA في enzo_curve.py: رياضيات محلية فقط (لا شبكة).

---

## 6. هل هناك dependency على Helius رغم قرار GMGN-only؟

**لا في مسارات التنفيذ النشطة.** ✅

| الملف | مرجع Helius | نشط؟ |
|---|---|---|
| enzo_curve.py | تعليق تاريخي + `_rpc()` معطّل | ❌ لا |
| enzo_pricefeed.py | تعليق تاريخي فقط | ❌ لا |
| enzo_pump.py | `helius_poll()` no-op | ❌ لا |
| enzo_security.py | legacy helpers غير مستدعاة | ❌ لا |
| enzo_gmgn.py | docstring يذكر Helius تاريخيًا | ❌ لا |
| enzo_analyze.py:376 | `"data_sources_used": [..., "Helius"]` | ⚠️ **قيمة افتراضية خاطئة** (قد تظهر في القرارات) |
| main.py / src/ | blueprint قديم (يوليو)، غير مستورد | ❌ لا |
| enzo-secrets.json | لا يحتوي helius/rpc مفاتيح | ❌ لا |

**الاستثناء الوحيد:** `enzo_analyze.py` سطر 376 — `data_sources_used` يذكر Helius كافتراضي في merged dict. غير ضار لكنه غير دقيق.

---

## 7. كيف نبني `current_price()` باختيار المصدر الصحيح؟

### الاقتراح (لا تنفيذ بعد):

```
current_price(mint, cached_mc=None):
    # 1) استخدم قيمة جاهزة من المتصل إن وُجدت (صفر تكلفة)
    if cached_mc: return cached_mc

    # 2) أثناء bonding (progress < 100): on-chain bonding-curve account
    if is_bonding(mint):            # من progress مخزّن/GMGN cached
        curve = read_curve_onchain(mint)   # getAccountInfo على PDA bonding-curve
        if curve.ok: return curve.market_cap_usd

    # 3) بعد migration: سعر AMM عبر pool (مصدر جديد مطلوب — GMGN token_pool موجود!)
    pool = gmgn.token_pool(mint)    # GMGN يوفر pool address + reserves
    if pool: return amm_price_from_reserves(pool)   # أو GMGN price مباشرة

    # 4) GMGN fallback (آخر ملاذ)
    return gmgn.get_live_market_cap(mint)
```

**ملاحظة جوهرية:** الخيار (2) يتطلب **إعادة تفعيل on-chain RPC** (Helius أو أي RPC) — وهو ما يُلغى حاليًا. الخيار (3) يمكن أن يبقى GMGN-only (token_pool موجود فعلًا ومُختبر — $151 مقابل تقدير curve).

---

## 8. هل سيؤثر هذا على مبدأ GMGN-only؟ وما الملفات المتأثرة؟

### التأثير على المبدأ:
- **الخيار A (GMGN فقط):** `current_price()` = `get_live_market_cap` (موجود) + تحسين الترتيب. **لا يمس المبدأ إطلاقًا.** لكن لا يحل مشكلة "اللحظية أثناء ban" — يبقى عرضة للـ ban.
- **الخيار B (On-chain للصفقات المفتوحة فقط):** إعادة تفعيل `getAccountInfo` على **bonding-curve PDA فقط** (للمراكز المفتوحة أثناء pre-migration). هذا **يخرق GMGN-only حرفيًا** لكنه:
  - محدود: استدعاء RPC واحد لكل صفقة مفتوحة كل N ثانية
  - لا يمس الاكتشاف/التحليل/الأمان (تبقى GMGN 100%)
  - يعمل أثناء ban GMGN
  - **التزام المبدأ من قبل:** "قرار GMGN-only" كان للبيانات (discovery/security/holders). تتبع السعر اللحظي للمراكز المفتوحة لم يكن موضوع القرار — كانت Helius هي المصدر الأصلي للـ pricefeed قبل الترحيل.

### الملفات المتأثرة (تقديري):
| الملف | التغيير |
|---|---|
| `enzo_gmgn.py` | إضافة `current_price()` + تحسين `read_bonding_curve` (لا إزالة) |
| `enzo_curve.py` | إعادة تفعيل `_rpc()` على PDA curve (إن اخترنا B) — استعادة الكود القديم من git/trash |
| `enzo_portfolio.py` | `check_exits` يستخدم `current_price()` داخليًا + backfill محسّن |
| `enzo_pricefeed.py` | `get_price()` → `current_price()`؛ ربط thread بـ check_exits |
| `enzo_pump.py` | loop exit-monitor دوري (كل 10-15s) يغذي check_exits |
| `enzo_dashboard.py` | `_live_mc()` → `current_price()` (أكثر موثوقية من curve-only) |
| `enzo-secrets.json` | إضافة RPC URL إن اخترنا B |

---

## 9. Latency المتوقعة ومعدل RPC requests

### Latency الفعلية (من enzo-log.jsonl، 1681 عينة ناجحة):
```
GMGN token_info  : avg 857ms (min -301ms* / max 41.6s)
GMGN security    : avg 631ms
GMGN trenches    : avg 1281ms (اكتشاف)
GMGN sol_price   : avg 702ms
الكل (1681)      : avg 849ms, median 653ms
```
\* قيم سالبة = اختلاف توقيت (clock skew)، تجاهلها.

### تكلفة دورة exit-check كاملة (GMGN-only، مع pacing 1.2s):
```
1  صفقة مفتوحة : ~2.1s  (1.2s gap + 0.86s call)
2  صفقات       : ~4.1s
5  صفقات       : ~10.3s
10 صفقات       : ~20.6s
```
**لكن** — أثناء ban: `_rl_acquire` ينام حتى 45s ثم يرفض (RateLimited). مع ban نشط، **دورة exit-check تتعطل تمامًا** (الوضع الحالي).

### معدل RPC على-chain (الخيار B):
```
getAccountInfo على PDA bonding-curve:
  ~250-400ms لكل استدعاء (RPC عام مثل api.mainnet-beta.solana.com)
  لا rate limit عملي (RPC عام: 40 req/10s عادة؛ Helius free: 150k req/شهر)
  لـ N صفقات مفتوحة كل 10s:
    N=1 → 0.1 req/s
    N=5 → 0.5 req/s
    N=10 → 1.0 req/s
  مقارنة بميزانية GMGN: ~0.8 req/s مستهلكة — فوارق هائلة
```

### مقارنة شاملة:

| | GMGN (الحالي) | On-chain (مقترح B) |
|---|---|---|
| Latency لكل سعر | ~0.9s + pacing 1.2s | ~0.3s (لا pacing) |
| أثناء ban | ✗ يتجمد | ✓ يعمل |
| بعد migration | ✓ (لكن نفس مصدر GMGN) | ✓ (pool عبر GMGN أو RPC) |
| Rate limit | 1.2s بين الاستدعاءات، ban متكرر | عمليًا غير محدود |
| التزام GMGN-only | ✓ كامل | ✗ لمراقبة الأسعار فقط |
| تكلفة تنفيذ | منخفضة (تحسين ترتيب فقط) | متوسطة (استعادة الكود القديم + secrets) |

---

## 10. المخاطر

### مخاطر الخيار B (on-chain):
1. **كسر مبدأ GMGN-only** — قرار معمارية يحتاج موافقة صريحة من Akram (تذكر: "GMGN-only" كان قرارًا صريحًا في 2026-08-05).
2. **يحتاج RPC**: Helius key غير موجود في secrets حاليًا (جرّده الترحيل). بدون key → RPC عام بطيء/محدود (api.mainnet-beta.solana.com).
3. **الكود القديم في trash**: `enzo_ws.py`, الكود القديم في `.trash-gmgn-migration/` — استعادته تتطلب اختبارًا.
4. **PDA derivation للتوكنات غير pump**: العملية تعمل فقط لـ pump.fun. التوكنات الأخرى (FourMeme/Bonk على BSC/Base) لا تملك bonding-curve PDA بنفس الطريقة.
5. **الصيانة المزدوجة**: مصدران للأسعار = احتمالان للتباعد (سعر GMGN cache vs on-chain).

### مخاطر الخيار A (GMGN فقط):
1. **لا يحل مشكلة ban** — يبقى exit monitoring متقطعًا أثناء الحظر.
2. **الـ 45s MAX_WAIT** يسبب تجمدًا مؤقتًا في check_exits.
3. **لا يوجد تحسين فعلي** لللحظية — فقط ترتيب أنظف.

### مخاطر عدم فعل أي شيء:
1. **uPnL وexits غير موثوقين** — الصفقات تُراقَب فقط عند الفتح/إعادة التحليل (نادر).
2. **خطر خسارة غير مراقب**: صفقة تنهار بين دورات scan → STOP_LOSS لا يُفعَّل في الوقت المناسب (رأينا CIRCUIT_BREAKER -75% في يوليو).

---

## 11. التوصية

**الخيار المختلط (الأفضل):**
1. **`current_price()` في enzo_gmgn** — ترتيب المصادر: curve-cached →   (GMGN cached curve → لا استدعاء) → **on-chain getAccountInfo** (للصفقات المفتوحة
   pre-migration فقط، بميزانية محددة) → GMGN live → pool/AMM عبر GMGN.
2. **pricefeed thread يعيد ربطه بـ check_exits** — كل 10s: `get_price()` لكل mint مفتوح
   ثم `check_exits()` — يغلق فجوة "لا مراقبة دورية".
3. **exit-monitor دوري مستقل** في pump (أو process خفيف) — لا يعتمد على scan العشوائي.
4. **on-chain لـ pre-migration فقط** — بعد migration يبقى GMGN (كما هو اليوم).
5. **GMGN يبقى المصدر الوحيد للاكتشاف/التحليل/الأمان** — المبدأ محفوظ في جوهره.

**قبل التنفيذ يحتاج قرارك (Akram):**
- [ ] هل نقبل إعادة تفعيل RPC على السلسلة لمراقبة الصفقات المفتوحة فقط؟ (نعم/لا)
- [ ] إن نعم: هل لديك Helius API key أم نستخدم RPC عام؟
- [ ] هل نضيف exit-monitor دوري مستقل (10s) بغض النظر عن قرار RPC؟ (موصى به بشدة)
