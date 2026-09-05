# قائمة فحص النقل إلى OpenClaw / Transfer checklist

هذه القائمة لك ولـ OpenClaw: كل بند قابل للتحقق بأمر واحد، والنتيجة المتوقعة
مكتوبة بجانبه. لا تنتقل للبند التالي قبل أن يتحقق السابق.

---

## 0) قبل النقل: توكن تيليجرام

المستودع **عام** على GitHub وملف `config/enzo-secrets.json` متتبَّع فيه، فالتوكن
الحالي مكشوف للعالم. اعتبره مسروقاً:

1. `@BotFather` ← `/revoke` ← اختر البوت ← احصل على توكن جديد.
2. ضع الجديد في `config/enzo-secrets.json` مكان `telegram_bot_token`.
3. **لا** تنشر الملف بعد ذلك (قرارك الحالي: إبقاؤه متتبَّعاً — يعني أن أي توكن
   جديد سيُرفع إلى مستودع عام ما لم تخرجه من التتبع لاحقاً).

> لا يوجد في المستودع مفتاح خاص ولا عبارة استرداد لمحفظتك — مفاتيح MoonPay
> مولَّدة محلياً على جهازك ولا تغادره. ما سبق يخص توكن البوت فقط.

---

## 1) ما الذي تنسخه

انسخ المجلد كاملاً كما هو، **باستثناء** ملفات الحالة التشغيلية (تُولَّد جديدة):

```
data/enzo.db*            data/enzo-state.json*    data/enzo-cache.json
data/enzo-audit.jsonl*   data/enzo-learning.json  data/enzo-portfolio.json
data/enzo-market-structure.json  data/enzo-panel.json  data/enzo-gmgn-ban.json
data/enzo-dashboard.html data/logs/*   data/run/*
config/enzo-control.json    (غيابه = غير موقوف، وهذا ما تريده عند البدء)
__pycache__/  *.pyc  node_modules/
```

النسخة المُجهَّزة في `enzo-transfer/enzo-ai-v2` مُنظَّفة هكذا مسبقاً: `data/`
فيها **فارغ تماماً** (مجلدا `logs/` و`run/` فقط) ⇒ **بدء نظيف**: قاعدة بيانات
جديدة، ورأس المال يُقرأ من محفظتك الحقيقية عند أول دورة للمحرك.

**فخّ انتبه له إن نسختَ يدوياً:** حذف `data/enzo.db` وحده **لا يكفي**. عند غياب
قاعدة البيانات يستورد `db.py` الدفتر القديم من `data/enzo-portfolio.json`
(`_migrate_from_json_if_needed`)، فتعود أرقامك السابقة كأن شيئاً لم يكن —
وهذا ما حدث فعلاً في أول نسخة جُرّبت: أظهر `doctor` الأساس القديم رغم حذف
قاعدة البيانات. لذلك يجب حذف **كل** ملفات الحالة في `data/`، لا قاعدة البيانات فقط.

**لا تنسخ مجلد `.git`.** النسخة تُبنى من `git archive` (الملفات المتتبَّعة فقط)
وتُكتب مراجعتها في `TRANSFER_REVISION.txt`. السبب: لو كان `.git` موجوداً لكان
`data/enzo.db` متتبَّعاً، وأي `git checkout .` أو `git pull` في workspace الجديد
سيُعيد الدفتر القديم ويلغي البدء النظيف. `doctor` يقرأ `TRANSFER_REVISION.txt`
ويُظهر `commit <hash> (no git metadata here)`.

---

## 2) في workspace الجديد — بالترتيب

```bash
cd <workspace>/enzo-ai-v2

# أ) بايثون والتبعيات (PyYAML + websockets إلزاميان؛ بلا PyYAML يتوقف البوت بخطأ صريح)
python3 --version                 # ≥ 3.10  (مُثبت على 3.11 و3.12 و3.14)
bash bootstrap.sh                 # يثبّت ويتحقق؛ exit 0 = جاهز

# ب) MoonPay CLI — تنفيذ الصفقات (إلزامي في LIVE)
npm i -g @moonpay/cli
mp consent accept
mp login --email you@example.com
mp verify  --email you@example.com --code <CODE>
mp wallet list                    # يجب أن تظهر: enzo-trading
mp token balance list --wallet enzo-trading --chain solana   # يجب أن تردّ بأرصدة

# ج) gmgn-cli — مصدر بيانات السوق والاكتشاف (بدونه: 0 مرشَّحين كل دورة)
which gmgn-cli                    # أو ضع مساره الكامل في data_sources.gmgn.cli
gmgn-cli --version                # المتوقع 1.6.x (البوت يتعامل مع اللهجتين)

# ج2) GMGN_API_KEY — **إلزامي منذ v1.6**: بدونه ترفض الأداة كل نداء،
#     فيظهر لك «0 مرشَّحين» كأن السوق هادئ وهو في الحقيقة غير مقروء.
export GMGN_API_KEY="<مفتاحك من gmgn.ai>"      # في البيئة التي يُقلع منها البوت
#     أو اجعله دائماً في:  ~/.config/gmgn/.env   (سطر: GMGN_API_KEY=...)
#     ⚠ إن كان OpenClaw هو من يُقلع البوت، فالمفتاح يجب أن يكون في بيئته هو،
#       وإلا عمل الفحص يدوياً وفشل عند الإقلاع التلقائي.

# د) الفحص الحاسم
./enzoctl doctor                  # الهدف: 0 مشاكل حرجة، وكل البنود ✔
./enzoctl scan --force            # دورة حقيقية واحدة (تُثبت أن GMGN تردّ)
./enzoctl doctor                  # الآن gmgn_discovery يجب أن يقول: last sweep returned N token(s)

# د2) إثبات المرشّحات على عملة حقيقية (اختياري لكنه قاطع)
./enzoctl probe <MINT>            # يعرض كل رقم تقرأه البوابات + القرار الحقيقي
                                  #   خروج 1 = العملة مرفوضة (ومطبوع سبب الرفض)

# هـ) التشغيل
./enzoctl start
./enzoctl status
# اللوحة:  http://<host>:8077/enzo-dashboard.html
# الصحة:   http://<host>:8077/health
```

---

## 3) ماذا يعني كل بند في `doctor`

| البند | ✔ المتوقع | ✖ معناه |
|---|---|---|
| `python_version` | 3.10 أو أحدث | لن يعمل `bootstrap` |
| `pyyaml` / `websockets` | مثبّتان | الإعداد يُتجاهل / الاكتشاف لا يرى إطلاقات |
| `config_valid` | no problems | إعداد تالف — لا تشغّل |
| `mode` | LIVE (real money) | إن كان PAPER فلن ينفّذ صفقات حقيقية |
| `gmgn_cli` | مسار الأداة | لا بيانات سوق إطلاقاً |
| `gmgn_api_key` | `GMGN_API_KEY found` | **✖ حرج**: v1.6 يرفض كل نداء بدونه ⇒ 0 مرشَّحين وكل البوابات عمياء |
| `gmgn_cli_dialect` | `1.6.x — token commands take --address` | ⚠ الأداة لا تقبل `--address` ولا `--token` ⇒ حدّثها: `npm i -g gmgn-cli@latest` |
| `gmgn_discovery` | `last sweep returned N token(s)` | ⚠ `no discovery sweep has run yet` ⇒ نفّذ `./enzoctl scan --force` ثم أعد الفحص |
| `gmgn_discovery_categories` | `configured: ['trenches','trending']` | ⚠ فئة غير موجودة في v1.6 (مثل `smartmoney`/`kol`) تحرق طلباً وتُسجّل فشلاً كل دورة |
| `gmgn_rate_config` | `0.8 req/s · gap 350ms · burst 2.5` | (معلومة) هذه هي الوتيرة الفعلية التي سيعمل بها البوت |
| `universe_gates` | `Pump V1 only=True · pre cap>=$5000 and sells>=10 · migrated cap>=$10000 and fees>=2.5 SOL` | **✖**: مرشّحات الدخول مطفأة أو ناقصة ⇒ سيشتري ما لا تريده |
| `holder_concentration_cap` | `top-1 WALLET must hold <= 10.0%` | **✖**: `max_holder_percentage` مفقود أو صفر ⇒ السقف مطفأ |
| `moonpay_cli` | مسار الأداة | لا تنفيذ لأي صفقة |
| `capital` | `$X deployable (wallet)` | المحفظة غير مقروءة ⇒ **التحجيم محجوب** |
| `ledger_baseline` | `wallet-anchored` | الأساس ما زال 10,000 الوهمية ⇒ `./enzoctl rebase --confirm` |
| `code_revision` | `commit <hash>` (من `TRANSFER_REVISION.txt` في نسخة بلا git) | ⚠ `cannot identify the running code` ⇒ انسخ `TRANSFER_REVISION.txt` من المصدر |
| `base_token_funding` | جزء من الرصيد قابل للإنفاق كـ SOL | كل المال USDC ⇒ كل شراء يُرفض |
| `secrets_not_in_git` | (تحذير) | التوكن متتبَّع في git |
| `process` | running after start | لم يبدأ |

**مبدأ السلامة:** البوت لا يخمّن أبداً. إن تعذّرت قراءة المحفظة فهو يحجب
تحجيم المراكز برسالة صريحة (`LIVE position sizing is BLOCKED`) بدل التداول
برقم قديم أو افتراضي — ويُسمح بآخر قراءة ناجحة لمدة `capital_sync_grace_sec`
(300 ثانية) فقط، ثم يُحجب.

---

## 4) أول ساعة بعد التشغيل (مراقبة)

1. `./enzoctl status` — رأس المال يجب أن يساوي رصيد محفظتك الحقيقي (لا 10,000).
2. اللوحة ← **Diagnostics** ← بطاقة `🚩 Rug Protection Layers` يجب أن تقول `3/3 ARMED`.
3. اللوحة ← **Diagnostics** ← بطاقة `🎯 Entry Universe · Layer 0` يجب أن تقول
   `5/5 ARMED`، وأن تعرض أرقامك: `$5,000` (قبل الترحيل) · `$10,000` و`2.5 SOL`
   (بعد الترحيل) · `first 8 wallets` · سقف المحافظ `10%`. إن رأيت `OFF` في
   بوابة، فالإعداد لم يصل — قارن بـ `config/enzo-config.yaml`.
4. اللوحة ← **Diagnostics** ← بطاقة `⚡ GMGN Data Source`: المفتاح `present`،
   ولهجة العناوين `--address` (لا `--token` بعد أول رفض)، وفئات الاكتشاف
   بأعدادها. **لافتتان حمراوان محتملتان:** «`GMGN_API_KEY` غير مضبوط» (تعني أن
   كل البوابات تقرأ «مجهول» ⇒ صفر مرشَّحين) و«فئات الاكتشاف ميتة كلها».
   كلتاهما تختفي بنفسها حين يزول سببها.
5. اللوحة ← **Activity** ← زر `🎯 Gate Vetoes` — يعزل القرارات التي أسقطتها
   بوابة، وكل قرار يعرض سببه ورموزه وأدلّته (المنصّة/الطور/الرسوم/عدد
   القنّاصين/تركّز أعلى محفظة). لا يجب أن ترى `IGNORE (conf=0)` بلا سبب.
6. اللوحة ← **Activity** — يجب أن ترى دورات اكتشاف (لا `0 candidates` باستمرار).
7. `/health` — يجب ألا يبقى `degraded` بعد أول دورة.
8. `./enzoctl logs` — لا `Traceback` ولا `BLOCKED`.
9. **تأكيد المرشّحات على عملة حقيقية:** خذ عنوان عملة من Activity (مرفوضة أو
   مقبولة) وشغّل `./enzoctl probe <MINT>`. يجب أن ترى: `launchpad: pump` و
   `platform: pump.fun`، الطور (قبل/بعد الترحيل) مع دليله، القيمة السوقية وعدد
   عمليات البيع مقابل حدّهما، نافذة أول 8 محافظ بأحجامها ووسومها، الرسوم
   (لبعد الترحيل)، وتركّز المحافظ بعد استثناء المنحنى/المجمّع. إن رأيت
   `GMGN_API_KEY : MISSING` أو `not measured` في كل الأسطر، فالمشكلة في
   البيانات لا في العملة — لا تحكم على المرشّحات قبل حلّها.

إن ظهر `RUG-FINGERPRINT: …` في النشاط، فتلك الطبقة الأولى ترفض دخولاً مشبوهاً —
اقرأ السبب؛ فهو يذكر القياس الذي أطلقه. وإن ظهر `NOT_PUMP_V1` أو
`LAUNCHPAD_UNKNOWN` أو `SNIPER_FLOOD_EARLY` أو `HOLDER_CONCENTRATION` فتلك
بوابات الدخول الجديدة تعمل كما ضبطتَها.

---

## 5) الاختبارات (اختياري لكن مُوصى به بعد النقل)

```bash
for t in tests/test_*.py; do python3 "$t"; done    # 19 حزمة · 640 تحقّقاً (~80 ثانية)
```

كلها تعمل في صناديق `ENZO_HOME` معزولة: **لا تلمس** قاعدة بياناتك ولا أموالك —
وهذا ما تتحقّق منه حزمة `test_suite_isolation` نفسها: `enzo.core.config` يحسب
مسارات الحالة عند الاستيراد، فأيّ اختبار يستورد قبل العزل كان يكتب في workspace
الحقيقي (كانت 8 حزم تفعل ذلك، بينها ما يكتب `data/run/enzo-capital.json` برصيد
وهمي قد يعتمده المحرك 300 ثانية عند فشل قراءة المحفظة).
اختبار اللوحة يشغّل خادماً حقيقياً على منفذ حر وينقر كل زر داخل DOM.

في نسخة النقل (بلا `.git`) يُظهر `test_executor` **44** بدل 48 (المجموع 636 بدل
640): التحقّقات الأربعة الباقية تقارن السلوك بالكود القديم المسترجَع من تاريخ
git، وتُتخطّى تلقائياً عند غياب التاريخ. **صفر فشل** هو المطلوب في الحالتين.

الحزم الثلاث الجديدة تخصّ مرشّحات 2026-09: `test_token_universe_gates` (48 — كل
رموز الرفض والطورين والقيم الحدّية)، `test_gmgn_cli_compat` (79 — توافق
gmgn-cli v1.6.1 + المسار الكامل **بلا أي استبدال**)، و`test_enzoctl_probe`
(51 — أداتا التشخيص `doctor` و`probe`).

> اللوحة نفسها تحذّرك قبل أول تشغيل: إن رأيت لافتة «رأس المال المعروض
> ($10,000) هو الرقم الافتراضي لا رصيدك الحقيقي» فمعناها أن الدفتر جديد ولم
> تُقرأ المحفظة بعد — تختفي تلقائياً بعد أول دورة ناجحة للمحرك.
