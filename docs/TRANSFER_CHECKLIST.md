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

# د) الفحص الحاسم
./enzoctl doctor                  # الهدف: 0 مشاكل حرجة، وكل البنود ✔

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
3. اللوحة ← **Activity** — يجب أن ترى دورات اكتشاف (لا `0 candidates` باستمرار).
4. `/health` — يجب ألا يبقى `degraded` بعد أول دورة.
5. `./enzoctl logs` — لا `Traceback` ولا `BLOCKED`.

إن ظهر `RUG-FINGERPRINT: …` في النشاط، فتلك الطبقة الأولى ترفض دخولاً مشبوهاً —
اقرأ السبب؛ فهو يذكر القياس الذي أطلقه.

---

## 5) الاختبارات (اختياري لكن مُوصى به بعد النقل)

```bash
for t in tests/test_*.py; do python3 "$t"; done    # 16 حزمة · 462 تحقّقاً
```

كلها تعمل في صناديق `ENZO_HOME` معزولة: **لا تلمس** قاعدة بياناتك ولا أموالك —
وهذا ما تتحقّق منه حزمة `test_suite_isolation` نفسها: `enzo.core.config` يحسب
مسارات الحالة عند الاستيراد، فأيّ اختبار يستورد قبل العزل كان يكتب في workspace
الحقيقي (كانت 8 حزم تفعل ذلك، بينها ما يكتب `data/run/enzo-capital.json` برصيد
وهمي قد يعتمده المحرك 300 ثانية عند فشل قراءة المحفظة).
اختبار اللوحة يشغّل خادماً حقيقياً على منفذ حر وينقر كل زر داخل DOM.

في نسخة النقل (بلا `.git`) يُظهر `test_executor` **44** بدل 48: التحقّقات
الأربعة الباقية تقارن السلوك بالكود القديم المسترجَع من تاريخ git، وتُتخطّى
تلقائياً عند غياب التاريخ. **صفر فشل** هو المطلوب في الحالتين.

> اللوحة نفسها تحذّرك قبل أول تشغيل: إن رأيت لافتة «رأس المال المعروض
> ($10,000) هو الرقم الافتراضي لا رصيدك الحقيقي» فمعناها أن الدفتر جديد ولم
> تُقرأ المحفظة بعد — تختفي تلقائياً بعد أول دورة ناجحة للمحرك.
