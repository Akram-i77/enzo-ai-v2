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
./enzoctl start|stop|restart
./enzoctl pause|resume  # إيقاف مؤقت (المراكز المفتوحة تبقى مُراقَبة)
./enzoctl mode live|paper
./enzoctl positions     # المراكز المفتوحة + آخر الصفقات
./enzoctl wallet        # الأرصدة الحقيقية + رأس المال القابل للنشر
./enzoctl logs -f       # متابعة السجل حيّاً
./enzoctl scan --force  # دورة فحص إضافية الآن
./enzoctl config        # الإعدادات الفعّالة (كل الأقسام)
./enzoctl health        # لقطة الصحة
```

كل أمر يقبل **`--json`** للقراءة الآلية. وكل أمر يخرج برمز مناسب:
`0` = سليم، `1` = توجد مشكلة.

---

## المتطلبات / Requirements

| المتطلب | السبب | التحقق |
|---|---|---|
| Python 3.9+ | تشغيل البوت | `python3 --version` |
| **PyYAML** | قراءة `enzo-config.yaml` — **إلزامي** | `./enzoctl doctor` |
| websockets | تغذية إطلاقات PumpDev الحية | `./enzoctl doctor` |
| `@moonpay/cli` (`mp`) | تنفيذ الصفقات الحقيقية | `mp wallet list` |
| `gmgn-cli` | بيانات السوق والاكتشاف | `./enzoctl doctor` |

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
  مراقب الخروج يفحص المراكز المفتوحة (وقف خسارة، جني أرباح مرحلي، تتبع)
```

### مبدأ السلامة / Safety principle

> **عند الشك، البوت يرفض التداول ويخبرك — لا يخمّن أبداً.**

أمثلة مطبَّقة فعلياً ومُختبَرة:
- تعذّرت قراءة المحفظة → **حجب فتح المراكز** بدل استخدام رقم وهمي.
- العملة على منحنى الربط (بلا مسار) → **لا يُفتح مركز أصلاً**، مع تنبيه وفترة انتظار ساعة.
- فشل بيع حقيقي → تنبيه **"عملات يتيمة في المحفظة"** مع الأمر اليدوي الكامل.
- `min_trade_usd` **أرضية لا عتبة رفض**: برأس مال $2.06 يُحسب حجم $0.16 فيُرفع إلى $1 وتُنفَّذ الصفقة — لكن الأرضية لا تتخطى أبداً ما تملكه المحفظة فعلاً.
- `max_trade_usd` ينطبق على **الشراء فقط** — لا يمنع البيع أبداً، وإلا حُبس البوت في مركز.

---

## الإعدادات / Configuration

`config/enzo-config.yaml` هو **المصدر الوحيد للحقيقة** — 24 قسماً، مع تعليقات
عربية/إنجليزية. أهم المفاتيح:

```yaml
paper_mode: false              # false = أموال حقيقية · true = محاكاة

execution:
  wallet_name: enzo-trading    # كما يظهر في: mp wallet list
  base_token: USDC             # العملة التي نشتري بها
  min_trade_usd: 1.0           # أرضية: يُرفع إليها الحجم إن كان أصغر
  max_trade_usd: 500.0         # سقف الصفقة (شراء فقط)
  capital_source: wallet       # اقرأ الرصيد الحقيقي كل دورة
  sol_fee_reserve: 0.02        # SOL محجوز للرسوم فقط
  not_routable_cooldown_sec: 3600

risk_management:
  risk_per_trade: 2.5          # % من رأس المال
  max_drawdown: 25.0           # % → قاطع الدائرة
  max_open_positions: 5

data_sources:
  gmgn:
    cli: gmgn-cli              # اسم في PATH أو مسار كامل
```

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
tests/       test_executor.py (47) · test_engine_e2e.py (43) ·
             test_min_trade_floor.py (34)
```

---

## نقاط المراقبة / Supervision endpoints

| الغرض | الرابط |
|---|---|
| هل هو حي؟ (مناسب للمشرفين) | `GET /health` → `200` سليم، **`503` مشكلة** |
| الحالة الكاملة | `GET /api/health` |
| نشاط الأنظمة الفرعية | `GET /api/activity` |
| اللوحة | `GET /enzo-dashboard.html` |
| من القرص (لو ماتت العملية) | `data/run/enzo-health.json` |

كل حالة `degraded` تحمل قائمة `problems[]` برموز محددة وأسبابها —
الجدول الكامل في [`docs/OPENCLAW_DEPLOYMENT.md`](docs/OPENCLAW_DEPLOYMENT.md#3-ما-الذي-تعنيه-الحالات--what-the-statuses-mean).

---

## الاختبارات / Tests

```bash
PATH=/tmp/mockbin:$PATH python3 tests/test_executor.py        # 47 assertion
PATH=/tmp/mockbin:$PATH python3 tests/test_engine_e2e.py      # 43 assertion
PATH=/tmp/mockbin:$PATH python3 tests/test_min_trade_floor.py # 34 assertion
```

`tests/test_engine_e2e.py` يعمل في صندوق معزول عبر `ENZO_HOME` — لا يلمس قاعدة
بياناتك ولا أموالك. وهو يثبت المسار الكامل من الاكتشاف حتى التنفيذ الحي.

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
