# ENZO ↔ GMGN Compatibility Audit — 2026-08-07

الهدف: التحقق العميق من مهارات GMGN، مقارنتها مع طريقة عمل ENZO، تحديد
مدى التوافق، وتطبيق التغييرات اللازمة.

## الخلاصة

**ENZO متوافق مع GMGN بنسبة 100% من ناحية تغطية البيانات** — كل محور من
محاور التحليل الستة له مصدر GMGN حقيقي، لا حقول وهمية، ولا أي استدعاء
لـ Helius/Birdeye/Jupiter/DexScreener. **تم إصلاح مشكلة حرجة** (حلقة
الـ rate-limit ban المزمنة) وإضافة 6 قدرات GMGN غير مستغلة سابقاً.

---

## 1) خريطة قدرات GMGN ↔ استهلاك ENZO

| قدرة GMGN CLI | الحالة قبل | الحالة بعد | مكان الاستهلاك |
|---|---|---|---|
| `token info` | ✅ مستخدمة | ✅ | `token_info()` — الأساس |
| `token security` | ✅ مستخدمة | ✅ | `token_security()` → `security_scan` |
| `token holders` | ✅ مستخدمة | ✅ | `deep_holder_analysis()` |
| `token traders` | ❌ غير مستخدمة | ✅ **جديدة** | `token_traders()` — هوية المتداولين + الربح (config `top_traders`) |
| `token pool` | ❌ غير مستخدمة | ✅ **جديدة** | `token_pool_info()` — سيولة حقيقية + pool address |
| `market kline` | ✅ مستخدمة | ✅ | `kline()` → `price_change_1h` + هيكل السوق |
| `market trending` | ✅ مستخدمة | ✅ | `discover()` |
| `market trenches` | ✅ مستخدمة | ✅ | `discover()` (pump pre-migration) |
| `market signal` | ❌ غير مستخدمة | ✅ **جديدة** (تُرجع [] من هذا المضيف) | `market_signals()` |
| `market hot-searches` | ❌ غير مستخدمة | ✅ **جديدة** (تُرجع [] من هذا المضيف) | `hot_searches()` |
| `portfolio stats` | ✅ مستخدمة | ✅ | `wallet_stats()` → Dev/Wallet Score |
| `portfolio created-tokens` | ✅ مستخدمة | ✅ | `dev_history()` — تاريخ المطور |
| `track smartmoney` | ✅ مستخدمة | ✅ | `discover()` |
| `track kol` | ✅ مستخدمة | ✅ | `discover()` |
| `track follow-wallet` | ❌ غير مستخدمة | ✅ **جديدة** (تُرجع [] — يتطلب follow قائمة) | `smart_wallet_activity()` |
| `swap / order / multi-swap` | — | — | غير مستخدم (paper mode) — جاهز للمستقبل |
| `gas-price` | ❌ | — | غير مدمج (لا حاجة حالياً) |
| `cooking` | — | — | خارج نطاق ENZO (تداول لا إنشاء) |

---

## 2) المشكلة الحرجة المكتشفة: حلقة الـ rate-limit ban

### الأعراض
- GMGN يفرض ban متجدد باستمرار (كل مرة "resets in ~90s" — يتجدد للأبد).
- `enzo_pump.py` كان يعمل منذ 21:41 ويضرب GMGN بلا توقف:
  - `discover()` كل 30 ثانية = 4 طلبات
  - `handle()` → `enzo_run.run()` لكل عملة = 3-5 طلبات (info + security + holders + deep)
  - الـ worker بمعدل 2 ثانية فقط بين العملات
  - **لا يوجد حد أقصى لعدد العملات المفحوصة في الدورة**

### الجذور (3 طبقات)
1. **لا تنسيق بين العمليات**: pump monitor وserve وengine — كل واحد لديه حالة ban
   خاصة في الذاكرة. عندما يبدأ pump حديثاً، يرسل فوراً رغم ban نشط.
2. **Thundering herd**: عدة عمليات تنتظر حتى موعد الإفلات ثم ترسل معاً في نفس
   اللحظة → ban يتجدد فوراً.
3. **لا ميزانية pipeline**: كل عملة جديدة في trenches تُفحص بالكامل (3-5 طلبات)
   بلا حد.

### الإصلاح (في `enzo_gmgn.py` + `enzo_pump.py` + `enzo_serve.py`)

**أ. قفل عالمي + pacing في `enzo_gmgn.py`:**
- `_rl_acquire()` — قفل threading يضمن ≥1.2s بين الطلبات، ويرفض فوراً
  (`RateLimited`) إذا كان ban المتبقي > 45s (بدلاً من النوم الطويل).
- `_rl_mark_ban()` / `_rl_mark_ok()` — تحديث نافذة الـ ban.
- **GRACE = 8s** بعد موعد الإفلات قبل أول طلب (يمنع herd).

**ب. حالة ban مشتركة عبر ملف `enzo-gmgn-ban.json`:**
- كل عملية (pump / serve / engine) تقرأ وتكتب نفس الملف.
- أي عملية تكتشف ban، الجميع يعرف — لا أحد يرسل أثناء ban.
- `ban_status()` — دالة عامة للاستعلام (يستخدمها serve).

**ج. ميزانية الدورة في `enzo_pump.py`:**
- `MAX_PIPELINE_PER_CYCLE = 6` — 6 عملات كحد أقصى لكل دورة 60 ثانية.
- الباقي "pipeline budget exhausted" — تخطي آمن بدون طلبات.

**د. serve يقرأ ban المشترك:**
- `_gmgn_ban_seconds()` — إذا ban نشط، يرجع آخر بيانات معروفة فوراً
  (بدون لمس GMGN) ويضبط `_next_retry` على +30s.

### النتيجة
- قبل: ban دائم (~90s يتجدد للأبد)، 0 تحليل ناجح.
- بعد: `banned_until: 0.0` مستقر، pump يفحص 6 عملات/دورة، serve يعمل،
  dashboard HTTP 200. GLOOP IGNORE (BUNDLE_DISTRIBUTION 100% + DEV_SOLD_ALL
  + DEV_FACTORY 15) — الرافضون يعملون.

---

## 3) قدرات GMGN الجديدة المضافة

| الدالة | الغرض | الطلبات | Cache |
|---|---|---|---|
| `market_signals()` | إشارات 1-21 (price spikes, smart buys...) | 1 | 25s |
| `token_traders()` | أفضل المتداولين + tags + profit | 1 | 5min |
| `token_pool_info()` | سيولة حقيقية + pool address + reserves | 1 | 5min |
| `hot_searches()` | الأكثر بحثاً (معنويات) | 1 | 10min |
| `smart_wallet_activity()` | نشاط متداول محدد (buy/sell records) | 1 | 5min |

### ملاحظات توافقية (learned)
- `market signal` و`hot-searches` و`follow-wallet` **تُرجع [] من هذا المضيف**
  (قيود API key/منطقة) — الوظائف موجودة وآمنة لكن بلا بيانات. `extra_discovery`
  يبقى `false` افتراضياً.
- `token traders` **تعمل** — أعطت 10 متداولين بـ tags (top_holder, fresh_wallet,
  fomo) + profit لكل واحد. تفعيلها عبر `top_traders: true`.
- `token pool` **تعمل** — أعطت liquidity حقيقية ($151) + initial_liquidity
  ($12,563) + pool address + reserves. أدق من تقدير الـ curve.

---

## 4) التغييرات المطبقة (ملفات)

1. **`enzo_gmgn.py`**:
   - `import threading`
   - نظام الـ rate-limit العالمي (قفل + grace + ملف مشترك + `ban_status()`)
   - 5 وظائف جديدة: `market_signals`, `token_traders`, `token_pool_info`,
     `hot_searches`, `smart_wallet_activity`
   - `discover()`: طبقة discovery إضافية اختيارية (hot-searches + signal)
   - `get_market_data()`: `top_traders` اختياري في signals
   - إصلاح `hot_searches` لقبول `list` مباشرة
2. **`enzo_pump.py`**: ميزانية الدورة (`MAX_PIPELINE_PER_CYCLE=6`)
3. **`enzo_serve.py`**: `_gmgn_ban_seconds()` — يحترم ban المشترك
4. **`enzo_wallet_behavior.py`**: استهلاك `top_traders` في الهوية
   (+12 smart / −20 rat / −20 bundler / −15 sniper)
5. **`enzo-config.yaml`**: مفاتيح جديدة `extra_discovery: false`,
   `top_traders: false` (معطلة افتراضياً — تُفعَّل عند الحاجة)

---

## 5) الوضع الحالي

- pump monitor PID 20932 (كود جديد، ميزانية دورة)
- serve PID 20896 (كود جديد، ban مشترك)
- botctl PID 8662
- `enzo-gmgn-ban.json`: `{"banned_until": 0.0}` — لا ban
- NIBZ: WAIT (conf 30, 1h -93.98% — انهيار فعلي)
- GLOOP: IGNORE (BUNDLE_DISTRIBUTION + DEV_SOLD_ALL + DEV_FACTORY)

## 6) توصيات مستقبلية

- عند الحاجة لزيادة العمق: فعّل `top_traders: true` (طلبان إضافيان فقط لكل
  عملة نهائية) — يستحق القيمة (هوية + ربح لكل متداول).
- `token pool` يمكن دمجه في `read_bonding_curve()` كبديل أدق للتقدير
  (طلب إضافي واحد لكل عملة، cached 5min).
- لا تفعّل `extra_discovery` من هذا المضيف — `signal` و`hot-searches` فارغان
  هنا؛ التفعيل يضيف طلبات بلا فائدة.
