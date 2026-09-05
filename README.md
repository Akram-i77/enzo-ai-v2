# ENZO — بوت تداول العملات الرقمية الصغيرة
### Autonomous Solana memecoin trading bot

بوت يفحص السوق كل دقيقة، يحلّل العملات بستة محاور، ويفتح/يغلق المراكز عبر
**MoonPay CLI**. مصمَّم ليعمل داخل workspace يشرف عليه **OpenClaw**.

> **المال حقيقي.** الوضع الافتراضي `paper_mode: false` أي تداول فعلي.
> غيّر الوضع بأمر واحد: `./enzoctl mode paper`

---

## البدء بثلاث خطوات / Start in three steps

```bash
bash bootstrap.sh      # 1. ثبّت المتطلبات وتحقق
./enzoctl doctor       # 2. تأكد أن كل شيء سليم (كل البنود ✔)
./enzoctl start        # 3. شغّل البوت
```

ثم افتح اللوحة: `http://<host>:8077/enzo-dashboard.html`

**دليل التشغيل والمراقبة الكامل:** [`docs/OPENCLAW_DEPLOYMENT.md`](docs/OPENCLAW_DEPLOYMENT.md)

---

## الأوامر / Commands

```bash
./enzoctl status        # هل يعمل؟ موقوف؟ أي وضع؟ كم رأس المال؟
./enzoctl doctor        # فحص شامل مع سبب كل عطل وطريقة إصلاحه
                        # + الإصدار العامل (commit) وتمويل عملة الأساس
./enzoctl start|stop|restart
./enzoctl pause|resume  # إيقاف مؤقت (المراكز المفتوحة تبقى مُراقَبة)
./enzoctl rebase        # بعد خسارة مُ تحقّقة: قبولها كأساس جديد ورفع الإيقاف
                        #   الاحترازي (لا يغيّر شيئاً بلا --confirm)
./enzoctl mode live|paper
./enzoctl positions     # المراكز المفتوحة + آخر الصفقات
./enzoctl wallet        # الأرصدة الحقيقية + رأس المال القابل للنشر
./enzoctl logs -f       # متابعة السجل حيّاً
./enzoctl scan --force  # دورة فحص إضافية الآن
./enzoctl unban         # إن حظرت GMGN المفتاح: كم بقي، ومسحها بـ --confirm
./enzoctl probe <MINT>  # ماذا ترى البوابات في عملة واحدة، حيّاً:
                        #   الهوية والطور وأول 8 محافظ والرسوم وتركّز المحافظ
                        #   ثم قرار المحلّل الحقيقي (خروج 1 = مرفوضة)
./enzoctl config        # الإعدادات الفعّالة (كل الأقسام)
./enzoctl health        # لقطة الصحة
```

كل أمر يقبل **`--json`** للقراءة الآلية. وكل أمر يخرج برمز مناسب:
`0` = سليم، `1` = توجد مشكلة.

---

## المتطلبات / Requirements

| المتطلب | السبب | التحقق |
|---|---|---|
| Python 3.10+ | تشغيل البوت (مُثبت على 3.11 و3.12 و3.14) | `python3 --version` |
| **PyYAML** | قراءة `enzo-config.yaml` — **إلزامي** | `./enzoctl doctor` |
| websockets | تغذية إطلاقات PumpDev الحية | `./enzoctl doctor` |
| `@moonpay/cli` (`mp`) | تنفيذ الصفقات الحقيقية | `mp wallet list` |
| `gmgn-cli` | بيانات السوق والاكتشاف | `./enzoctl doctor` |
| **`GMGN_API_KEY`** | gmgn-cli v1.6 **يرفض كل نداء** بدونه — فيظهر كأنه «سوق هادئ» | `./enzoctl doctor` ← `gmgn_api_key` |

```bash
npm i -g @moonpay/cli
mp consent accept
mp login --email you@example.com
mp verify --email you@example.com --code <CODE>
```

**ملاحظة مهمة عن PyYAML:** إن لم يكن مثبّتاً، البوت **يتوقف بخطأ صريح** ولا
يكمل بإعدادات ناقصة. سابقاً كان يكمل بصمت بعد إسقاط 17 من 23 قسماً من الإعدادات —
وهذا وحده كان يكفي لتعطيل التداول بالكامل.

---

## كيف يعمل / How it works

```
كل 60 ثانية:
  1. اقرأ رأس المال الحقيقي من محفظة MoonPay
  2. اكتشف العملات: PumpDev (WebSocket) + GMGN (4 فئات) + قائمة المراقبة
  3. حلّل أعلى 12 مرشحاً بستة محاور (أمان، سيولة، مطوّر، زخم، هيكل سوق، محافظ)
  4. إن كان القرار BUY → تحقّق من قابلية التداول عبر swaps.xyz أولاً
  5. احسب الحجم من رأس المال المُتحقَّق
     (إن كان أصغر من min_trade_usd → يُرفع إليها، ما دامت المحفظة تملكها)
     → نفّذ mp token swap
  6. سجّل المركز مع tx_hash وأرسل تنبيه تيليجرام

كل ثانيتين:
  مراقب الخروج يفحص المراكز المفتوحة
  (وقف خسارة، جني أرباح مرحلي، وقف متحرك، خروج الركود، حد زمني)
```

### مبدأ السلامة / Safety principle

> **عند الشك، البوت يرفض التداول ويخبرك — لا يخمّن أبداً.**

أمثلة مطبَّقة فعلياً ومُختبَرة:
- تعذّرت قراءة المحفظة → **حجب فتح المراكز** بدل استخدام رقم وهمي.
- العملة على منحنى الربط (بلا مسار) → **لا يُفتح مركز أصلاً**، مع تنبيه وفترة انتظار ساعة.
- فشل بيع حقيقي → تنبيه **"عملات يتيمة في المحفظة"** مع الأمر اليدوي الكامل.
- `min_trade_usd` **أرضية لا عتبة رفض**: برأس مال $2.06 يُحسب حجم $0.16 فيُرفع إلى $1 وتُنفَّذ الصفقة — لكن الأرضية لا تتخطى أبداً ما تملكه المحفظة فعلاً.
- `max_trade_usd` ينطبق على **الشراء فقط** — لا يمنع البيع أبداً، وإلا حُبس البوت في مركز.
- **ملف التحكم تالف → البوت يبقى موقوفاً** (فشل آمن). مفتاح الإيقاف لا يُعيد
  تسليح التداول الحقيقي بصمت أبداً؛ الضغط على «استئناف» يُصلح الملف.
- **مطوّر باع كل حصصه = رفض قاطع، لا صوت واحد من ستة.** حدث `DEV_SOLD_ALL`
  يُدخل العملة في قائمة الرفض فتصير `IGNORE` وسببها يسمّي الـ rug صراحةً.
  قبل 2026-09-04 كانت إشارة المطور مجرد محورٍ مُوزون: خمسة محاور سليمة
  رجّحت عليه (56.15 مقابل عتبة 55) فاشترى البوت عملةً باع مطورها كل شيء.
- **السعر له مصدر وعمر ظاهران:** كل مركز يحمل `price_source` و`price_age_sec`
  و`price_is_live`، واللوحة تضع ⚠ بدل أن تعرض سعر الدخول بصمت وكأنه لحظي.
- **عملية اللوحة لا تفتح تغذية PumpDev أبداً** (تقرأ ما ينشره المحرك): اتصالان
  من نفس الـ IP كانا يجعلان pump.dev يحدّ الـ فتجوع الأسعار في كل مكان.
- **عطل في مركز واحد لا يُسقط دورة الخروج كلها** — قائمة مراحل جني الربح تُطبَّع
  قبل استخدامها، فلا يبقى مركز حقيقي عاجزاً عن الإغلاق.
- **الأرضية تُفرَض عند آخر بوابة أيضاً**: إن وصل حجم دون `min_trade_usd` إلى
  منفّذ الصفقات لأي سبب، يُرفع إلى الأرضية ما دامت المحفظة تملكها — لا يمكن
  لبوابتين أن تتناقضا فيرفض إحداهما ما أمرتَ به.
- **بطاقة الرصيد تعرض المحفظة الحيّة**: لقطة جديدة من `mp wallet list`، مع إبقاء
  رقم الدفتر (أساس الربح/التراجع) ظاهراً في السطر الفرعي حتى لا يُخفى شيء.
- **الأرضية لا ترفض نفسها** (أُصلح 2026-09-05): منفّذ الصفقات كان **يُعيد اشتقاق**
  حجم الأمر بالدولار من كمية SOL (دولار ← SOL بالقسمة ← دولار بالضرب). في الحساب
  الثنائي `(1.0/px)*px` ترجع `0.9999999999999999` عند **464 من 2501** سعر SOL واقعي
  حول $200، وبلا تسامح كان الأمر المُسعَّر عند الأرضية بالضبط يُرفض برسالة
  `$1.0000 < execution.min_trade_usd $1.00` — متناقضة، وتُقرأ في السجل كأنها رفض من
  MoonPay. الآن المرجع هو **الحجم المقرَّر** من التسعير (`usd_notional`) مع تسامح
  مليونٍ من الدولار، والرسالة تقول صراحةً «بوابة ENZO — لم تُستدعَ MoonPay».
  الفحص الحدودي القديم كان يمرّ لأنه يختبر مسار **USDC** حيث لا تحويل، وأنت
  تتداول بـSOL؛ أُضيف فحص يجتاز 45 سعراً «خاسراً» واحداً واحداً.
- **المحفظة تمول الأمر *و* الرسوم معاً**: بما أن `base_token: SOL`، كان فحص
  الاحتياطي ينظر إلى الرسوم وحدها (`sol_fee_reserve`)؛ فقد يفتح مركزاً يُبقي المحفظة
  تحت احتياطها — أي عاجزة عن دفع رسوم **إغلاقه**. الآن الشراء يتطلب
  `الرصيد ≥ الأمر + الاحتياطي` (والبيع لا يُقيَّد أبداً: لا حبس في مركز)،
  والرسالة تُفصّل الأمر والاحتياطي والنقص.
- **سعر SOL له مصدر معلَن**: إن تعذّرت قراءة DexScreener يُستخدم رقم ثابت،
  والآن يُعلن نفسه — تحذير في السجل، `sol_price_source` في النتيجة وفي لقطة
  رأس المال و`/api/state`، **ولافتة على اللوحة**؛ ولا يُخزَّن التخمين 60 ثانية
  كسعر حقيقي بل 5 ثم تُعاد المحاولة (تخمين $180 والسعر الحقيقي $203 يرسل
  SOL أكثر بنحو 13% مما قصدت).

---

## قواعد الخروج / Exit rules

تُقيَّم كل ثانيتين على كل مركز مفتوح، بهذا الترتيب:

| القاعدة | القيمة الحالية | المعنى |
|---|---|---|
| **جني أرباح مرحلي** | +30% → بيع 30% · +70% → بيع 30% · +150% → بيع 40% | يُقفل الربح على دفعات |
| **الوقف المتحرك** | `40%` | **يقوم بوظيفتين** — انظر أدناه |
| **خروج الركود** | مفعّل · +15% · 30 ثانية | ربح جامد لا يصنع قمة جديدة |
| **وقف الخسارة** | `-38%` | الحد الأدنى الصارم |
| **الحد الزمني** | 48 ساعة | لا مركز يُترك مفتوحاً للأبد |

### ⚠ الرقم `trailing_stop_percentage` يقوم بوظيفتين

1. **عتبة التشغيل:** الوقف المتحرك لا يستيقظ إلا بعد ارتفاع العملة **+40%** فوق سعر الدخول.
2. **عتبة التنفيذ:** بعد استيقاظه يُغلق إذا هبطت **40% من أعلى قمة** بلغتها.

النتيجة: عند التنفيذ تخرج بنحو **-16% من سعر الدخول** (‎1.40 × 0.60 = 0.84‎).
وهذه النقطة تقع **فوق** وقف الخسارة (-38%)، فالمتحرك يسبق دائماً ويحوّل خسارة
-38% محتملة إلى -16% مقفلة. لكن **قبل** بلوغ +40% يبقى المتحرك نائماً،
والحماية الوحيدة هي وقف الخسارة.

مقايضة يجب معرفتها: رقم أعلى = مساحة أوسع للعملة لترتفع (خروج مبكر أقل في
عملات شديدة التذبذب)، لكنه يُبقي ربحاً أقل حين يُنفَّذ فعلاً.

## الكون المسموح: عملات Pump القياسية فقط / Entry universe (Layer 0)

منذ 2026-09-05 البوت **لا يشتري إلا عملة pump.fun قياسية (Pump V1)**، ويطبّق
حدوداً مختلفة حسب طور العملة. التفاصيل الكاملة مع مصدر كل رقم من
`gmgn-cli v1.6.1` في **`docs/ENZO_PUMP_V1_FILTERS.md`**.

| الطور | كيف يُعرف | الحدود |
|---|---|---|
| **قبل الترحيل** | `launchpad_status = 1` (حيّ على منحنى الربط) | قيمة سوقية ≥ **$5,000** · عمليات بيع ≥ **10** |
| **بعد الترحيل** | `launchpad_status = 2` (أو `migrated_pool` / `exchange = pump_amm`) | قيمة سوقية ≥ **$10,000** · رسوم مدفوعة ≥ **2.5 SOL** |
| **طور مجهول** | لا دليل في الحمولة | `unknown_phase: strict` ⇒ يُعامل بالحدّ الأشد |
| **منصّة مجهولة** | لا `launchpad` ولا `launchpad_platform` | `reject_unknown_launchpad: true` ⇒ **رفض** (مجهول ≠ pump.fun) |

**قاعدة الرغ الجديدة (أول 8):** تُقرأ أول 8 محافظ دخلت بعد إنشاء العملة
(مرتّبة بـ `start_holding_at`)؛ إن كان منها **≥4 قنّاصين** ومجموع شرائهم
**> $5,000**، **أو** قنّاص واحد alone **> $5,000** ⇒ `SNIPER_FLOOD_EARLY` = رفض
نهائي، لا شراء أبداً.

> ⚠ **أمانة الحدّين:** `gmgn-cli v1.6.1` لا يوفّر شريط صفقات زمني، فأول 8
> **محافظ** هي أقرب بديل قابل للقراءة (وليس أول 8 صفقات حرفياً). وقيمة الرسوم
> تأتي من `portfolio created-tokens` بلا وحدة معلنة من الـAPI، فالوحدة مُعلَنة في
> الإعداد (`fees_unit: sol`). الاثنان موثّقان في §4 من التقرير، وكلاهما يظهر لك
> صراحةً في `./enzoctl probe`.

```yaml
token_universe:   {pump_v1_only: true, reject_unknown_launchpad: true,
                   discovery_min_market_cap: 5000}
phase_gates:
  pre_migration:  {min_market_cap: 5000,  min_sells: 10}
  migrated:       {min_market_cap: 10000, min_total_fees: 2.5, fees_unit: sol,
                   require_known_fees: true}
  unknown_phase:  strict
sniper_flood:     {enabled: true, first_n: 8, min_sniper_count: 4,
                   max_total_sniper_buy_usd: 5000,
                   max_single_sniper_buy_usd: 5000, on_unknown: reject}
market_analysis:  {max_holder_percentage: 10.0}   # أعلى محفظة، عدا المنحنى/المجمّع/الحرق
```

رموز الرفض التي ستراها في السجل وفي `rejected_signals`:
`LAUNCHPAD_UNKNOWN` · `NOT_PUMP_V1` · `PHASE_UNKNOWN` · `MCAP_BELOW_PRE_MIN` ·
`MCAP_BELOW_MIGRATED_MIN` · `SELLS_BELOW_MIN` · `FEES_BELOW_MIN` ·
`FEES_UNKNOWN` · `SNIPER_FLOOD_EARLY` · `SNIPER_DATA_UNAVAILABLE` ·
`HOLDER_CONCENTRATION`. «لا أعرف» **ليست** «نجح»: كل بوابة تجهل رقمها تقول ذلك
صراحةً وترفض (حسب `on_unknown` / `require_known_fees`).

**كل هذا ظاهر على اللوحة، لا في السجل فقط:**

- بطاقة **«🎯 الكون المسموح · الطبقة 0»** في صفحة التشخيص: البوابات الخمس
  (Pump V1 · حدّ ما قبل الترحيل · حدّ ما بعد الترحيل · قنّاصو البداية · سقف
  المحافظ) مع حالة كل واحدة `ARMED`/`OFF` وأرقامها **المقروءة من إعدادك** لا
  من الافتراضيات، وعدّاد `N/5 ARMED` في رأس البطاقة.
- زر **«🎯 Gate Vetoes»** في تدفق النشاط يعزل القرارات التي أسقطتها بوابة.
- كل رفض يعرض الآن **سببه ورموزه وأدلّته** (المنصّة/الطور/الرسوم/عدد
  القنّاصين/تركّز أعلى محفظة) — كان يظهر `SYM → IGNORE (conf=0)` بلا تفسير،
  لأن تحويل سجلّ القرارات إلى تدفق النشاط كان يُسقط `reason` و`rejected_signals`.
- بطاقة **«⚡ مصدر بيانات GMGN»**: وجود المفتاح، لهجة العناوين
  (`--address`/`--token`)، فئات الاكتشاف وعدد كل فئة، آخر مسح، وآخر خطأ من
  المزوّد. ولافتتان حمراوان: **بلا `GMGN_API_KEY`** (كل البوابات تقرأ «مجهول»)
  وحين **تموت فئات الاكتشاف كلها**.

## حماية الرغ: ثلاث طبقات / Rug protection layers

| الطبقة | متى تعمل | ماذا تفعل | ثمنها |
|---|---|---|---|
| **1 — بصمات مطلقة** | عند الدخول، أول نظرة | نقض قاطع: bundlers/snipers/rats ضمن أعلى 20، عمر محافظ القمة < 3 أيام، العشرة الكبار يبيعون الآن، مصنع серийный (≥50 عملة و<3% حيّ) | ≈ صفر — العملة العضوية لا تحمل هذه البصمات |
| **3 — وقف مبكر مشروط** | أول 10 دقائق من المركز | وقف -12% **فقط** للمراكز التي دخلت bearing أعلاماً (نصف عتبات النقض) | صفر على الدخول النظيف: يحتفظ بـ -38%/-40% كما أمرتَ |
| **4 — كاشف الرغّ الحي** | كل 20 ثانية على كل مركز مفتوح | صوتان من ثلاثة (سحب سيولة ≥40% · انهيار حاملين ≥15% · قفزة/انقلاب بيع العشرة) = خروج فوري بأي سعر | ≈ صفر على الرابح: الصاروخ لا تُسحب سيولته |

```yaml
rug_protection:
  veto_bundlers_top20: 6      # كل العتبات قابلة للضبط من هنا
  early_stop_pct: 12.0
  early_stop_window_min: 10.0
  tripwire_poll_sec: 20.0
  tripwire_min_votes: 2
```

الأسباب الجديدة التي ستراها في السجل: `RUG-FINGERPRINT: …` (رفض عند الدخول)،
`EARLY_STOP(-12%, flagged entry, …)`، و`RUG_TRIPWIRE(…)` — وكلها تذكر القياسات
التي أطلقتها.

## نظافة الإعداد / config hygiene

`tests/test_config_wiring.py` يحرس ثلاث حقائق:

1. **التطابق** — كل مفتاح في `DEFAULTS` موجود في `config/enzo-config.yaml` وبالعكس،
   فلا مفتاح مخفي عنك ولا مفتاح يتيم.
2. **الانحراف المقصود فقط** — ملفك لا يختلف عن الافتراضيات الآمنة إلا في قرارَيك:
   `paper_mode: false` (تداول حقيقي) و`execution.base_token: SOL`. أي انحراف جديد يفشل.
3. **لا مفاتيح ميتة جديدة** — 65 مفتاحاً قديماً لا يقرؤها أي كود (أكثرها
   `scam_detection.*` و`data_sources.pumpdev.thresholds` الأسماء القديمة) مجمَّدة في
   قائمة داخل الاختبار: الدين قد يصغر، ويُمنع أن يكبر. الأقسام الحرجة
   (`rug_protection`, `exit_strategy`, `position_sizing`, `dashboard`,
   `dev_behavior.factory_*`) مفحوصة أنها بلا مفتاح ميت إطلاقاً.

مفاتيح كانت معروضة ولا تعمل، وصارت تعمل الآن (بقيمها السابقة حرفياً، فالسلوك لم يتغير):

| المفتاح | أثره |
|---|---|
| `dashboard.refresh_seconds` | فترة تحديث اللوحة في المتصفح (كانت 10 ثوانٍ ثابتة) |
| `dashboard.activity_limit` | عدد الأحداث في تدفق النشاط (كان 100 ثابتة) |
| `dev_behavior.factory_dev_*` (7 مفاتيح جديدة + 6 قديمة) | سلالم عقوبة المطوِّر المصنعي وسقفها (كانت أرقاماً صلبة في `dev.py`) |

`tests/test_dashboard_e2e.py` + `tests/dashboard_browser_test.js` يشغّلان خادماً حقيقياً
في صندوق معزول ثم **ينقران كل زر** داخل DOM: 14 زراً، 6 مسارات HTTP، شارة العلم 🚩
على المراكز المشبوبة، بطاقتا الطبقة 0 وطبقات الرغّ في التشخيص، وتصفية
`Gate Vetoes` على **حدث رفض حقيقي** مزروع (لا على لوحة فارغة)، واختفاء لافتة
المفتاح بعودته (لا إنذار دائم على بوت سليم).

### خروج الركود / Stall exit

يُغلق المركز **كربح** حين يتجمّد: الشرطان معاً — ربح ≥ `stall_min_gain_pct`
(15%) **و** لا قمة سوقية جديدة منذ ≥ `stall_seconds` (30 ثانية). لا يُطلَق
أبداً على مركز خاسر (تلك مهمة وقف الخسارة). ساعة الركود تُحفظ مع المركز
فتنجو من إعادة التشغيل.

> 30 ثانية مدة قصيرة. إن وجدت الصفقات الرابحة تُغلق مبكراً أكثر مما تحب،
> ارفع `stall_seconds` إلى 60 أو 120 — التغيير يلتقطه البوت **دون** إعادة تشغيل.


---

## الإعدادات / Configuration

`config/enzo-config.yaml` هو **المصدر الوحيد للحقيقة** — 24 قسماً، مع تعليقات
عربية/إنجليزية. أهم المفاتيح:

```yaml
paper_mode: false              # false = أموال حقيقية · true = محاكاة

execution:
  wallet_name: enzo-trading    # كما يظهر في: mp wallet list
  base_token: SOL              # ⚠ العملة التي نشتري بها — يجب أن تكون
                               #   الأصل الذي تحمله محفظتك فعلاً (2026-09-04:
                               #   المحفظة تحمل SOL فحوّلناه من USDC)
  min_trade_usd: 1.0           # أرضية: يُرفع إليها الحجم إن كان أصغر
  max_trade_usd: 500.0         # سقف الصفقة (شراء فقط)
  capital_source: wallet       # اقرأ الرصيد الحقيقي كل دورة
  sol_fee_reserve: 0.02        # SOL محجوز للرسوم فقط
  not_routable_cooldown_sec: 3600

risk_management:
  risk_per_trade: 2.5          # % من رأس المال
  max_drawdown: 25.0           # % → قاطع الدائرة
  max_open_positions: 5

exit_strategy:                 # انظر قسم «قواعد الخروج» أعلاه
  stop_loss_percentage: 38.0
  trailing_stop_percentage: 40.0   # عتبة تشغيل + عتبة تنفيذ معاً
  stall_exit_enabled: true
  stall_min_gain_pct: 15.0
  stall_seconds: 30.0
  take_profit_stages: [{pct: 30, sell: 0.3}, {pct: 70, sell: 0.3}, {pct: 150, sell: 0.4}]
  max_holding_time_hours: 48

market_analysis:               # عتبات الاكتشاف — حدّدها المالك
  min_liquidity: 5000
  min_volume: 8000
  min_holders: 10
  min_market_cap: 5000
  min_confidence_score: 55
  min_buy_pressure: 30
  max_scam_score: 15           # ⚠ غير مفعّل في الكود
  max_holder_percentage: 10.0  # ⚠ غير مفعّل في الكود

data_sources:
  gmgn:
    cli: gmgn-cli              # اسم في PATH أو مسار كامل
```

**`chain: sol` لا تغيّرها** — GMGN تتطلب `sol` بينما MoonPay تتطلب `solana`؛
طبقة `moonpay_chain()` تترجم عند الحد الفاصل تلقائياً. تغييرها يدوياً يعيد
عطل `NO_ROUTE` على كل صفقة.

بعد أي تعديل: `./enzoctl doctor` ثم `./enzoctl restart`.

---

## البنية / Layout

```
enzoctl                        ← واجهة التحكم (ابدأ من هنا)
bootstrap.sh                   ← تثبيت + تحقق
enzo.py                        ← نقطة الدخول القديمة (start/scan/status/…)
requirements.txt

enzo/
  core/      config · engine · db · audit · log · learn · cache
  execution/ executor (MoonPay) · portfolio · exit_monitor
  providers/ gmgn (gmgn-cli) · pump (WebSocket)
  analyzers/ analyze · security · market_structure · wallet · dev
  ui/        dashboard · serve (HTTP) · notify (Telegram) · botctl

config/      enzo-config.yaml · enzo-control.json · enzo-watchlist.json
data/        enzo.db (الدفتر) · run/ (pid + health) · logs/ · audit
docs/        OPENCLAW_DEPLOYMENT.md · ENZO_FULL_DIAGNOSIS.md
tests/       8 حزم (258 تحقّقاً) · mockbin/ (واجهة MoonPay الوهمية) ·
             conftest_paths.py
```

---

## نقاط المراقبة / Supervision endpoints

| الغرض | الرابط |
|---|---|
| هل هو حي؟ (مناسب للمشرفين) | `GET /health` → `200` سليم، **`503` مشكلة** |
| الحالة الكاملة | `GET /api/health` |
| المحفظة والمراكز والإحصاءات | `GET /api/state` |
| بوابات الدخول (17 عتبة) وصحة مصدر البيانات | `GET /api/state` → `config_summary.universe_gates` + `gmgn_status` |
| نشاط الأنظمة الفرعية | `GET /api/activity` |
| أسعار المراكز المفتوحة (للمخطط) | `GET /api/prices` |
| اللوحة | `GET /enzo-dashboard.html` |
| إيقاف/استئناف التداول | `POST /api/control/toggle` |
| دورة فحص فورية | `POST /api/scan` |
| من القرص (لو ماتت العملية) | `data/run/enzo-health.json` |

المنفذ الافتراضي **8077** (`./enzoctl dashboard` أو `python3 -m enzo.ui.serve <port>`).
يرتبط بـ `0.0.0.0` فيقبل الطلبات من أي واجهة مضيفة.

كل حالة `degraded` تحمل قائمة `problems[]` برموز محددة وأسبابها —
الجدول الكامل في [`docs/OPENCLAW_DEPLOYMENT.md`](docs/OPENCLAW_DEPLOYMENT.md#3-ما-الذي-تعنيه-الحالات--what-the-statuses-mean).

---

## الاختبارات / Tests

تسع عشرة حزمة، **735 تحقّقاً**، كلها تعمل بلا إعداد وبلا شبكة وبلا محفظة حقيقية:

```bash
python3 tests/test_dashboard_e2e.py        # 124  اللوحة: خادم حي + نقر كل زر في DOM
python3 tests/test_gmgn_cli_compat.py      #  95  توافق gmgn-cli v1.6.1 والمسار الكامل + الحظر
python3 tests/test_enzoctl_probe.py        #  61  enzoctl probe/doctor/unban (صدق التقارير)
python3 tests/test_token_universe_gates.py #  48  بوابات الطبقة 0: Pump V1/الطور/الرسوم/القنّاصون
python3 tests/test_executor.py             #  48  تنفيذ MoonPay (شراء/بيع/رسوم/أخطاء)
python3 tests/test_exit_rules.py           #  44  قواعد الخروج: وقف/متحرك/ركود + أولوياتها
python3 tests/test_engine_e2e.py           #  43  المسار الكامل: اكتشاف ← قرار ← تنفيذ حي
python3 tests/test_min_trade_floor.py      #  51  min_trade_usd كأرضية لا كعتبة رفض
python3 tests/test_moonpay_chain.py        #  30  ترجمة اسم الشبكة ومنع NO_ROUTE
python3 tests/test_control_pause.py        #  25  سلامة مفتاح الإيقاف (فشل آمن + كتابة ذرّية)
python3 tests/test_rug_layers.py           #  25  طبقات الرغّ 1 (نقض) + 3 (وقف مبكر) + 4 (قاطع)
python3 tests/test_fresh_start_baseline.py #  24  بداية نظيفة: لا ledger قديم ينبعث
python3 tests/test_base_token_capital.py   #  23  رأس المال بحسب عملة الأساس
python3 tests/test_capital_staleness.py    #  21  قدم لقطة رأس المال ⇒ منع التداول
python3 tests/test_rug_gate.py             #  19  نقض البصمات عند الدخول
python3 tests/test_config_wiring.py        #  18  نظافة الإعداد: لا مفاتيح ميتة جديدة
python3 tests/test_dashboard_js.py         #  17  صحة JavaScript المولَّد في اللوحة
python3 tests/test_suite_isolation.py      #  11  عزل الحزم بعضها عن بعض
python3 tests/test_floor_last_gate.py      #   8  الأرضية كآخر بوابة قبل التنفيذ
```

لا حاجة لضبط `PATH`: واجهة MoonPay الوهمية مرفقة داخل المستودع في
`tests/mockbin/` وتُحلّ تلقائياً عبر `tests/conftest_paths.py`.

`tests/test_engine_e2e.py` يعمل في صندوق معزول عبر `ENZO_HOME` — لا يلمس قاعدة
بياناتك ولا أموالك. وهو يثبت المسار الكامل من الاكتشاف حتى التنفيذ الحي.

> **قبل أي تشغيل بأموال حقيقية:** نفّذ الحزم التسع عشرة وتأكد من `0 failed`.
> فحص المتصفّح الفعلي (نقر كل زر على خادم حي) يحتاج `npm i jsdom` ثم
> `ENZO_JSDOM_PATH=/path/to/node_modules python3 tests/test_dashboard_e2e.py`؛
> بدونه يُتخطّى ذلك الجزء وحده وتبقى بقية الحزمة تعمل. الفحص الثابت في
> `test_dashboard_js.py` يغطي الصحة النحوية وربط الأزرار دون أي اعتماديات.

---

## ⚠ تحذير أمني / Security

`config/enzo-secrets.json` مُتتبَّع في git → **توكن تيليجرام مكشوف**.
اعتبره مسروقاً وألغه عبر **@BotFather → /revoke**، ثم:

```bash
git rm --cached config/enzo-secrets.json
echo "config/enzo-secrets.json" >> .gitignore
```

التفاصيل في [`docs/OPENCLAW_DEPLOYMENT.md`](docs/OPENCLAW_DEPLOYMENT.md#8--تحذير-أمني--security-warning).

---

## التشخيص / Diagnosis

التشخيص الكامل المبني على الأدلة (سجلات، قاعدة بيانات، 14,528 حدث تدقيق،
وتفكيك MoonPay CLI الحقيقي) في:

- [`docs/ENZO_FULL_DIAGNOSIS.md`](docs/ENZO_FULL_DIAGNOSIS.md) — كل الأسباب الجذرية بالأدلة
- [`docs/OPENCLAW_DEPLOYMENT.md`](docs/OPENCLAW_DEPLOYMENT.md) — عقد التشغيل والمراقبة
