# ENZO × OpenClaw — عقد التشغيل والمراقبة
### Deployment & supervision contract

هذا الملف يشرح **بالضبط** كيف يشغّل OpenClaw بوت ENZO ويراقبه، وما الذي يعنيه كل
رقم وكل حالة. كل ما هنا مبني على سلوك مُختبَر، لا على نوايا.

> **القاعدة الذهبية:** إذا شككت في أي شيء، شغّل
> `./enzoctl doctor` — فهو يفحص كل شيء ويعطيك سبب العطل وطريقة إصلاحه.

---

## 1. التشغيل لأول مرة / First run

### 1.0 قبل أي شيء: أداتان خارجيتان إلزاميتان / two external CLIs

البوت لا يحمل بيانات السوق ولا ينفّذ الصفقات بنفسه — هما أداتان منفصلتان، وغياب
إحداهما يعطّل كل شيء (مع فشل آمن صريح، لا تخمين):

```bash
# أ) MoonPay CLI — تنفيذ الصفقات الحقيقية (إلزامي في وضع LIVE)
npm i -g @moonpay/cli
mp consent accept
mp login --email you@example.com
mp verify --email you@example.com --code <CODE>
mp wallet list                 # يجب أن تظهر المحفظة: enzo-trading

# ب) gmgn-cli — مصدر بيانات السوق والاكتشاف الوحيد
#    ثبّته ثم تأكد أنه على PATH، أو ضع مساره الكامل في الإعداد:
#      data_sources.gmgn.cli: /full/path/to/gmgn-cli
which gmgn-cli || echo "غير موجود — الاكتشاف سيعيد 0 عملة"
```

بدون `mp`: لا تُنفَّذ أي صفقة، ويحجب البوت تحديد حجم المركز برسالة
`LIVE position sizing is BLOCKED`. وبدون `gmgn-cli`: لا يرى السوق إطلاقاً
(0 مرشَّحين كل دورة). كلاهما يظهر بنداً ✖ في `./enzoctl doctor`.

### 1.0.1 إن تعذّرت قراءة المحفظة / when the wallet cannot be read

رأس المال في وضع LIVE يُقرأ من محفظة MoonPay الحقيقية. إن فشلت القراءة
(غياب `mp`، انتهاء الجلسة، انقطاع الشبكة):

- يُستخدم آخر رقم ناجح **لمدة `execution.capital_sync_grace_sec` (300 ثانية) فقط**،
  ويُعلَّم `stale`، ويظهر في `doctor` كـ `✖ capital STALE $…` — **لا علامة خضراء**.
- بعد انتهاء النافذة: **حجم المركز محجوب** (`LIVE position sizing is BLOCKED`)
  ويُكتب حدث `RISK/ERROR` في سجلّ التدقيق. البوت لا يخمّن رصيداً أبداً.
- الختم الزمني لآخر قراءة ناجحة **لا يُجدَّد** عند الفشل، فالنافذة تنتهي في
  موعدها فعلاً (كان تجديدها يجعل القراءة القديمة صالحة للأبد — أُصلح ومُختبَر في
  `tests/test_capital_staleness.py`).

### 1.1 التشغيل / first run

```bash
cd <workspace>/enzo-ai-v2
bash bootstrap.sh          # يثبّت المتطلبات ويتحقق (لا يلمس أموالك ولا إعداداتك)
./enzoctl doctor           # يجب أن تكون كل البنود ✔
./enzoctl start            # يشغّل المحرك + اللوحة + تيليجرام
./enzoctl status           # للتأكد
```

`bootstrap.sh` يخرج بـ **exit code 0** عند النجاح و **1** عند الفشل، ويطبع لكل خطوة
سطراً بصيغة `[ OK ]` / `[FAIL]` / `[WARN]` — أي أنه قابل للفحص آلياً من OpenClaw.

---

## 2. نقاط المراقبة / Supervision endpoints

كل ما يحتاجه OpenClaw لمراقبة البوت، بلا قراءة ملفات أو تخمين:

| الغرض | الأمر / الرابط | ملاحظات |
|---|---|---|
| **هل هو حي؟** | `GET /health` | `200` = سليم، **`503` = توجد مشكلة**. الجسم صغير ومناسب لـ `grep` |
| الحالة الكاملة | `GET /api/health` | كل التفاصيل: رأس المال، التغذية، المنفّذ، المحرك |
| المحفظة والمراكز | `GET /api/state` | Equity، المراكز المفتوحة، الصفقات المغلقة، نقاط المخطط |
| نشاط الأنظمة | `GET /api/activity` | سجل الأحداث + حالة المحرك (دورات، آخر فحص) |
| أسعار المراكز | `GET /api/prices` | جسم صغير جداً — مناسب لنبض متكرر |
| إيقاف/استئناف | `POST /api/control/toggle` | يُحفظ ذرّياً في `config/enzo-control.json` مع المصدر والوقت |
| فحص فوري | `POST /api/scan` | يشغّل دورة اكتشاف في الخلفية ويعيد الرد فوراً |
| من الطرفية | `./enzoctl health` | نفسها، بلا HTTP |
| حالة مقروءة | `./enzoctl status` | **exit 0** = البوت يعمل **واللوحة التي على المنفذ لوحته فعلاً**؛ **exit 1** = متوقف، أو اللوحة ليست له / لا تعمل |
| **من يردّ على المنفذ؟** | ترويسات `X-Enzo-Pid` و`X-Enzo-Data` | كل ردّ من لوحة ENZO يحملهما. بهما تُنسب الصفحة إلى عملية ومجلّد بيانات محددين — فلا تُحتسب صفحة عملية أخرى على أنها بوتك |
| سبب فشل اللوحة | `data/run/enzo-dashboard-error.json` | يُكتب فور فشل ربط المنفذ (قبل رمي الخطأ)، ويُمحى عند `stop` أو عند إقلاع لوحة سليمة |
| فحص شامل | `./enzoctl doctor` | **exit 0** = سليم، **exit 1** = مشكلة حرجة |
| لأي آلة | أي أمر + `--json` | مثال: `./enzoctl status --json` |
| من القرص مباشرة | `data/run/enzo-health.json` | يُكتب تلقائياً — يُقرأ حتى لو ماتت العملية |
| معرّف العملية | `data/run/enzo.pid` | يُحذف عند الإيقاف النظيف |
| سجل المشرف | `data/logs/supervisor.log` | مخرجات `enzoctl start` |

### مثال فحص آلي / Example automated probe

```bash
code=$(curl -s -o /dev/null -w '%{http_code}' http://127.0.0.1:8077/health)
[ "$code" = "200" ] || echo "ENZO degraded or down"
```

### 2.1 تصديق اللوحة قبل تصديق أنها تعمل / Dashboard liveness contract

**ردّ `200` على المنفذ لا يثبت شيئاً.** قد يكون المنفذ محجوزاً من نسخة أقدم من
ENZO أو من أي برنامج آخر، فتُفتح صفحة مليئة بأرقام عملية أخرى — وهذا بالضبط ما
يُقرأ على أنه «اللوحة تعمل» بينما بوتك لا يقدّم شيئاً. لذلك:

1. كل ردّ من لوحة ENZO يحمل `X-Enzo-Pid` (رقم العملية) و`X-Enzo-Data` (مجلّد
   البيانات الذي بُنيت منه الصفحة).
2. `./enzoctl start` **يتحقّق** من المنفذ بعد الإقلاع: يقارن `X-Enzo-Pid` برقم
   المشرف في `data/run/enzo.pid`.
   * التطابق ← يطبع `verified : the page answered … identified itself as THIS bot`
     ويخرج بـ**exit 0**.
   * المنفذ يردّ لكن من عملية أخرى ← يطبع `✖ THE DASHBOARD IS NOT THIS BOT's`
     مع رقم العملية الغريبة ومجلّد بياناتها، ويخرج بـ**exit 1**. **المحرك لا
     يُقتل**: إيقافه ومعه مراكز مفتوحة يعني إيقاف مراقب الخروج أيضاً.
   * لا أحد يردّ ← يطبع `✖ THE DASHBOARD IS NOT SERVING` مع السبب المسجَّل في
     `data/run/enzo-dashboard-error.json` وآخر أسطر `data/logs/supervisor.log`،
     ويخرج بـ**exit 1**.
3. `./enzoctl status` يطبع الحكم نفسه في سطر `dashboard`، و**رمز خروجه يوافقه**
   (فالخروج 0 مع لوحة غريبة هو الكذبة نفسها بصيغة أخرى). الاستثناء: الإقلاع
   بـ`--no-dashboard` قرار مقصود وليس عطلاً، فيبقى exit 0 ويطبع
   `dashboard : disabled (--no-dashboard) — no page is being served`.
4. الفشل يُسجَّل في `data/run/enzo-dashboard-error.json` ويظهر في `problems[]`
   تحت الرمز `DASHBOARD_SERVER_DOWN`، ويُمحى عند `stop` حتى لا يبقى بوتٌ متوقف
   متهماً بلوحة ميتة.

فحص مقترح لـOpenClaw — «هل الصفحة التي سأفتحها هي صفحة هذا البوت؟»:

```bash
pid=$(cat data/run/enzo.pid 2>/dev/null)
hdr=$(curl -s -D - -o /dev/null http://127.0.0.1:8077/health | tr -d '\r')
said=$(printf '%s\n' "$hdr" | awk -F': ' '/^X-Enzo-Pid/{print $2}')
if [ -n "$pid" ] && [ "$said" = "$pid" ]; then
  echo "the dashboard IS this bot (pid $pid)"
else
  echo "the page on 8077 is NOT this bot (X-Enzo-Pid='${said:-none}', our pid='${pid:-none}')"
fi
```

أو ببساطة: `./enzoctl status` — فرمز الخروج يحمل الحكم نفسه.

---

## 3. ما الذي تعنيه الحالات / What the statuses mean

`/api/health` يعيد `status` بإحدى ثلاث قيم:

| القيمة | المعنى | ما يجب فعله |
|---|---|---|
| `ok` | كل الأنظمة تعمل ولا توجد مشاكل | لا شيء |
| `degraded` | البوت يعمل لكن توجد مشكلة في `problems[]` | اقرأ القائمة — كل عنصر يحمل سببه |
| `error` | تعذّرت قراءة الحالة أصلاً | `./enzoctl logs` ثم `./enzoctl doctor` |

### رموز المشاكل المعروفة / Known problem codes

كل رمز يظهر في `problems[]` مع سببه:

| الرمز | المعنى | الإصلاح |
|---|---|---|
| `TRADING_PAUSED` | البوت موقوف يدوياً — لا دورات فحص | `./enzoctl resume` |
| `RISK_HALTED: <سبب>` | قاطع المخاطر أوقف التداول | راجع `max_drawdown` / `max_daily_loss` |
| `ENGINE_STALE` | المحرك لم يفحص منذ مدّة طويلة | `./enzoctl logs` |
| `ENGINE_NEVER_SCANNED` | المحرك بدأ لكنه لم يكمل دورة بعد | انتظر دورة واحدة، ثم افحص السجل |
| `EXIT_MONITOR_DOWN_WITH_OPEN_POSITIONS` | **مراكز مفتوحة بلا مراقبة** — أخطر حالة | `./enzoctl restart` فوراً |
| `CAPITAL_BLOCKED` | تعذّرت قراءة المحفظة → **فتح المراكز محجوب** | `mp login` / `mp consent accept` |
| `CAPITAL_SYNC_FAILED` | قراءة المحفظة فشلت (ضمن فترة السماح) | يتعافى تلقائياً عند نجاح القراءة |
| `CAPITAL_BELOW_MIN_TRADE` | الرصيد أقل من `min_trade_usd` | موّل المحفظة |
| `INSUFFICIENT_CAPITAL_FOR_MINIMUM_TRADE` | رأس المال المتاح أقل من الحد الأدنى للصفقة، فلا يمكن تنفيذها حتى بعد رفع الحجم إلى الأرضية | موّل المحفظة، أو أغلق مركزاً، أو اخفض `min_trade_usd` |
| `BELOW_MINIMUM_TRADE` | **بوابة ENZO الداخلية قبل الإرسال — ليست رفضاً من MoonPay** (الأداة لم تُستدعَ أصلاً). الحجم المقرّر أقل من `execution.min_trade_usd` | `./enzoctl wallet` ثم موّل المحفظة أو اغلق مركزاً أو اخفض `min_trade_usd` |
| `ABOVE_MAXIMUM_TRADE` | الحجم أعلى من `execution.max_trade_usd` (يشترى فقط، لا يمنع البيع أبداً) | راجع `max_trade_usd` وحجم رأس المال |
| `INSUFFICIENT_SOL_FOR_FEES` | رصيد SOL أقل من احتياطي الرسوم (`sol_fee_reserve`) | موّل المحفظة بـSOL للرسوم |
| `EXECUTOR_NOT_READY` | CLI مفقود / غير مُصادَق / المحفظة غير موجودة | الرسالة تحمل السبب المحدد |
| `PUMPDEV_RETRYING` / `PUMPDEV_DOWN` | تغذية الإطلاقات الجديدة ميتة | تحقق من الشبكة وحزمة `websockets` |
| `PUMPDEV_STALE` | التغذية متصلة لكن بلا رسائل منذ 90+ ثانية | عادةً تُستأنف تلقائياً |
| `GMGN_RATE_LIMITED` | حظر مؤقت من GMGN (كل البوابات تقرأ «مجهول» أثناءه) | ينتهي تلقائياً، أو `./enzoctl unban --confirm`؛ وإن تكرر فخفّف `requests_per_sec` |
| `GMGN_DISCOVERY_FAILED` | أمر `gmgn-cli` فشل لكل الفئات | ثبّت `gmgn-cli` أو صحّح مساره |
| `DISCOVERY_FAULT[<مصدر>]` | مصدر اكتشاف معيّن رمى خطأً | السبب مرفق |
| `DASHBOARD_RENDER_ERROR` | تعذّر توليد اللوحة | `./enzoctl dashboard` لرؤية الخطأ |
| `CONFIG_UNREADABLE: <الملف: الخطأ>` | `config/enzo-config.yaml` لا يمكن قراءته (خطأ YAML غالباً). `/health` يعيد **503** و`/api/state` و`/api/activity` يعيدان **503** مع `reason=CONFIG_UNREADABLE` — لا 500 غامضاً | افتح الملف على السطر المذكور، ثم `./enzoctl doctor` |
| `STATE_UNREADABLE: <السبب>` | تعذّرت قراءة الحالة (قاعدة البيانات تالفة مثلاً) — عطلٌ مختلف عن عطل الإعدادات ويُسمّى باسمه | `./enzoctl doctor`؛ وإن لزم فاستعد `data/` من نسخة احتياطية |
| `DASHBOARD_SERVER_DOWN: <السبب> (port …)` | خيط اللوحة مات (المنفذ محجوز غالباً) **بينما المحرك يتداول**. التفاصيل في `data/run/enzo-dashboard-error.json` | حرّر المنفذ أو غيّر `dashboard.port` ثم `./enzoctl restart` |

---

## 4. سياسة إعادة التشغيل / Restart policy

`enzoctl start` يفصل العملية عن الطرفية (`setsid`) ويكتب `data/run/enzo.pid`.

- **SIGTERM** → إيقاف نظيف: المحرك يوقّف مراقب الخروج ويحذف ملف PID.
- إذا لم يستجب خلال 15 ثانية → `enzoctl stop` يرسل **SIGKILL** ويحذّر.
- المراكز المفتوحة تبقى في الدفتر، ومراقب الخروج يلتقطها عند إعادة التشغيل.

```bash
./enzoctl stop            # SIGTERM ثم SIGKILL بعد 15 ثانية
./enzoctl restart         # إيقاف ثم تشغيل
./enzoctl stop --grace 30 # مهلة أطول
```

**مهم:** إن كان OpenClaw يدير العملية بنفسه (systemd / pm2 / حاوية)، فاستخدم
`enzoctl _supervise --no-dashboard --no-telegram` كأمر التشغيل المباشر — فهو يعمل
في المقدمة (foreground) ولا يفصل نفسه، وهذا ما تتوقعه أدوات الإشراف.

---

## 5. دورة الحياة المالية / Money lifecycle

هذا أهم قسم. كل خطوة فيها حماية قابلة للمراقبة:

```
دورة فحص (كل 60 ثانية افتراضياً)
  │
  ├─ 1. مزامنة رأس المال من المحفظة الحقيقية (mp token balance list)
  │      ✗ فشلت + لا توجد قراءة حديثة  →  رأس المال = 0  →  فتح المراكز محجوب
  │      ✗ فشلت + توجد قراءة < 300 ثانية → تُستخدم القراءة السابقة (تُعلَّم stale)
  │      ✓ نجحت → USDC + (SOL القابل للنشر × السعر)، مع حجز رسوم SOL
  │
  ├─ 2. الاكتشاف: PumpDev WebSocket + GMGN (4 فئات) + قائمة المراقبة
  │      أي فشل → يُسجَّل كتحذير مرئي + يظهر في /health
  │
  ├─ 3. التحليل العميق (6 محاور) لأعلى 12 مرشحاً
  │      بيانات ناقصة → DATA_ERROR / NO_MARKET_DATA (لا يُخلط مع "عملة رديئة")
  │
  ├─ 4. قرار BUY؟  →  **بوابة القابلية للتداول أولاً**
  │      لا يوجد مسار عبر swaps.xyz → NO_ROUTE
  │      → لا يُفتح أي مركز، ويُبلَّغ المشغّل، وتُحفظ فترة انتظار ساعة
  │        (حتى لا يُحرق حدّ الطلبات على طريق مسدود)
  │
  ├─ 5. تحجيم المركز على رأس المال المُتحقَّق منه
  │      الحجم = رأس المال × نسبة المخاطرة ÷ نسبة وقف الخسارة
  │      الحجم > max_trade_usd → يُقصّ (ينطبق على الشراء فقط، لا يمنع البيع أبداً)
  │      الحجم < min_trade_usd → **يُرفع إلى min_trade_usd** (أرضية، لا عتبة رفض)
  │        ├─ المحفظة تملك الأرضية فعلاً → تُنفَّذ الصفقة
  │        └─ لا تملكها → INSUFFICIENT_CAPITAL_FOR_MINIMUM_TRADE (لا مركز وهمي)
  │
  ├─ 6. التنفيذ: mp --json token swap ...  (وحدات بشرية، بلا --yes)
  │      ✓ نجح → يُفتح المركز ويُربط بـ tx_hash
  │      ✗ فشل → يُتراجع عن المركز + **يُبلَّغ المشغّل بالسبب وطريقة الإصلاح**
  │
  └─ 7. مراقب الخروج (كل ثانيتين) يراقب المراكز المفتوحة
         ✗ فشل البيع الحقيقي → تنبيه "عملات يتيمة في المحفظة" مع الأمر اليدوي
```

### الحد الأدنى للصفقة أرضية لا عتبة رفض / min_trade_usd is a floor

`execution.min_trade_usd` هو **أصغر أمر تقبله المنصة**، لذلك يُعامل كأرضية يُرفع
إليها الحجم — لا كعتبة تُلغي الصفقة.

| رأس المال | الحجم المحسوب (4% مخاطرة، وقف 50%) | ما يُنفَّذ فعلياً |
|---|---|---|
| $2.06 | $0.1648 | **$1.00** (رُفع إلى الأرضية) |
| $1.00 | $0.08 | **$1.00** (رُفع إلى الأرضية) |
| $0.50 | $0.04 | **مرفوض** — المحفظة لا تملك $1 |
| $2.06 مع $1.50 معرَّضة | — | **مرفوض** — المتاح $0.56 فقط |
| $559.40 | $44.75 | $44.75 (الأرضية لا تتدخل) |
| $1,000,000 | $80,000 | **$500** (سقف `max_trade_usd`) |

**قيدان لا يُتجاوزان أبداً:**
1. الأرضية لا يمكن أن تتخطى ما تملكه المحفظة فعلاً وغير المعرَّض حالياً.
2. حدّ عدد المراكز المفتوحة `max_open_positions` يبقى سارياً.

أما `max_exposure` فيُتجاوز **عمداً** عند تطبيق الأرضية — لأن سقف 30% من
محفظة $2.06 هو $0.62، أي أقل من الحد الأدنى، فاحترامه يجعل التداول مستحيلاً
على أي محفظة صغيرة. كل تجاوز من هذا النوع:
- يُسجَّل بمستوى **WARNING** في السجل،
- ويُكتب في سجل التدقيق (`./enzoctl logs audit`)،
- ويُحفَظ على المركز نفسه في الحقلين `min_floor_applied` و`effective_risk_pct`
  (المخاطرة الفعلية كنسبة من رأس المال، لا النسبة المضبوطة).

لإلغاء هذا السلوك والعودة إلى الرفض: `position_sizing.min_trade_is_floor: false`

### مبدأ السلامة المعتمد / The safety principle

> **عند الشك، البوت يرفض التداول ويخبرك — لا يخمّن.**

مثال واقعي من الاختبار: عندما تعذّرت قراءة المحفظة، كان بإمكان الكود أن يرجع إلى
رقم الدفتر الاحتياطي ($10,000 وهمية) ويحسب حجم مركز $200 ضد محفظة لا تملكه. بدلاً
من ذلك يُحجب فتح المراكز ويُعلن `CAPITAL_BLOCKED`.

**مفتاح الإيقاف يفشل آمناً (fail-closed).** إن كان `config/enzo-control.json`
موجوداً لكنه تالف أو غير قابل للقراءة، يُعامل البوت نفسه على أنه **موقوف** ويسجّل
خطأً صريحاً — لا يُعيد تسليح التداول الحقيقي بصمت ضد رغبة المشغّل. الاسترداد بسيط:
`./enzoctl resume` (أو زر «استئناف» في اللوحة) يعيد كتابة الملف ذرّياً.
أما غياب الملف كلياً فيعني «لم يُطلب إيقاف قط» = غير موقوف.

**عطل مركز واحد لا يُسقط المراقبة كلها.** دورة الخروج تُطبّع بيانات كل مركز قبل
استخدامها، فمركز تالف في قاعدة البيانات لا يمنع إغلاق بقية المراكز.

---

## 6. الأوامر اليومية / Daily commands

```bash
./enzoctl status              # نظرة سريعة
./enzoctl positions            # المراكز المفتوحة + آخر الصفقات
./enzoctl wallet               # الأرصدة الحقيقية + رأس المال القابل للنشر
./enzoctl logs -f              # متابعة السجل حيّاً
./enzoctl logs audit -n 200    # آخر 200 حدث تدقيق (ملوّن)
./enzoctl logs supervisor      # سجل المشرف
./enzoctl scan --force         # دورة فحص إضافية الآن
./enzoctl pause                # إيقاف مؤقت (المراكز تبقى مُراقَبة)
./enzoctl resume               # استئناف
./enzoctl mode paper           # تحويل إلى محاكاة (يُنسخ احتياطياً تلقائياً)
./enzoctl mode live            # تحويل إلى تداول حقيقي
./enzoctl config execution     # عرض قسم معيّن من الإعدادات الفعّالة
./enzoctl dashboard            # إعادة توليد لوحة HTML
```

كل أمر يقبل `--json`.

---

## 7. ملفات الحالة / State files

| الملف | المحتوى |
|---|---|
| `config/enzo-config.yaml` | **المصدر الوحيد للحقيقة** — 24 قسماً، مع تعليقات |
| `config/enzo-control.json` | `paused` — يُدار عبر `enzoctl pause/resume` |
| `config/enzo-watchlist.json` | قائمة المراقبة. المفتاح `watchlist` (يقبل `mints`/`tokens` أيضاً) |
| `config/enzo-secrets.json` | **لا يُفترض أن يكون في git** — انظر التحذير أدناه |
| `data/enzo.db` | دفتر الحسابات (SQLite WAL): المراكز، الصفقات، رأس المال |
| `data/run/enzo.pid` | معرّف العملية |
| `data/run/enzo-health.json` | آخر لقطة صحة (يُقرأ حتى لو ماتت العملية) |
| `data/run/enzo-capital.json` | آخر قراءة لرأس المال + عمرها |
| `data/enzo-trade-gate.json` | ذاكرة العملات غير القابلة للتداول (فترة الانتظار) |
| `data/enzo-audit.jsonl` | سجل التدقيق. يُقرأ من النهاية (أسرع 40×) |
| `data/logs/supervisor.log` | مخرجات المشرف |

---

## 8. ⚠ تحذير أمني / Security warning

`config/enzo-secrets.json` **مُتتبَّع في git**، أي أن توكن تيليجرام بداخله مكشوف
لكل من يقرأ المستودع. تعامل معه كمسروق:

1. افتح **@BotFather** في تيليجرام → `/revoke` → أنشئ tokناً جديداً.
2. ضع التوكن الجديد في `config/enzo-secrets.json`.
3. أخرج الملف من تتبّع git:
   ```bash
   git rm --cached config/enzo-secrets.json
   echo "config/enzo-secrets.json" >> .gitignore
   echo "data/" >> .gitignore
   ```

> لم تُنفَّذ هذه الخطوات تلقائياً لأن نطاق العمل المتفق عليه كان **إصلاح الأعطال
> فقط** بلا إعادة هيكلة للمستودع أو تغيير في تاريخ git. القرار قرارك.

---

## 9. الاختبارات / Tests

**23 حزمة، 1057 تحقّقاً** (آخر تشغيل: 1057 نجح / 0 فشل). لا حاجة لضبط `PATH` —
واجهة MoonPay الوهمية مرفقة في `tests/mockbin/` وتُحلّ تلقائياً عبر
`tests/conftest_paths.py`:

```bash
for t in tests/test_*.py; do python3 "$t" || echo "FAILED: $t"; done
```

| الحزمة | تحقّقات | ماذا تثبت |
|---|---|---|
| `test_gmgn_cli_compat.py` | 161 | توافق gmgn-cli v1.6: المفاتيح، اللهجات، الميزانية، الحظر |
| `test_dashboard_e2e.py` | 144 | اللوحة من الحالة إلى HTML: كل بطاقة وجدول وبانر |
| `test_enzoctl_probe.py` | 72 | `doctor` (34 بنداً) و`probe <mint>`: صدق ✔/⚠/✖ |
| `test_rate_limit_budget.py` | 64 | ميزانية الطلبات وأوزانها و`429` |
| **`test_dashboard_buttons.py`** | **55** | **كل زر في اللوحة يُنقر في DOM حقيقي (jsdom) ضد خادم حي: 14 زراً، التبويبات، الفلاتر، الإيقاف/الاستئناف، الفحص اليدوي، التحديث، شارة الرغّ، نوافذ الزخم** |
| **`test_cli_honesty.py`** | **53** | **`wallet` و`logs` يعملان فعلاً + حارس ساكن يفحص كل `module.attribute` في المشروع (332 وصولاً) فلا تتكرر كارثة `get_balance_snapshot` + عقد 503/500 عند كسر الإعدادات** |
| **`test_dashboard_liveness.py`** | **53** | **نسبة الصفحة إلى صاحبها (`X-Enzo-Pid`)، ورموز خروج `start`/`status` عند ميناء محجوز أو حرّ أو `--no-dashboard`، ودورة حياة ملف الفشل، وصدق `enzo.py start`** |
| `test_min_trade_floor.py` | 51 | `min_trade_usd` أرضية لا عتبة رفض |
| `test_token_universe_gates.py` | 48 | بوابات الطبقة صفر: Pump V1، الأرضيات، القنّاصون، الرسوم |
| `test_executor.py` | 48 | تنفيذ MoonPay وترجمة الأخطاء |
| `test_exit_rules.py` | 44 | قواعد الخروج وأولوياتها |
| `test_engine_e2e.py` | 43 | المسار الكامل في صندوق معزول |
| `test_moonpay_chain.py` | 30 | ترجمة اسم الشبكة / `NO_ROUTE` |
| `test_control_pause.py` | 25 | سلامة مفتاح الإيقاف |
| `test_rug_layers.py` | 25 | طبقات الحماية 1 و3 و4 |
| `test_fresh_start_baseline.py` | 24 | بدء نظيف: ماذا يرى البوت في workspace جديد |
| `test_base_token_capital.py` | 23 | رأس المال بحسب عملة الأساس |
| `test_capital_staleness.py` | 21 | فترة السماح والقراءة القديمة |
| `test_rug_gate.py` | 19 | بوابة الرغّ قبل الشراء |
| `test_config_wiring.py` | 18 | كل مفتاح في YAML موصول فعلاً بالكود |
| `test_dashboard_js.py` | 17 | صحة JS المولَّد (`node --check`) |
| `test_suite_isolation.py` | 11 | لا حزمة تكتب في `data/` الحقيقي |
| `test_floor_last_gate.py` | 8 | الأرضية آخر بوابة قبل الإرسال |

حزمتا `test_dashboard_buttons.py` و`test_dashboard_js.py` تحتاجان `node`
(والأولى `jsdom`). عند غيابهما تُعلنان **التخطي بصوت مرتفع** (`⚠ SKIPPED … No
button was checked by this run — this is NOT a pass`) بدل أن تُحتسبا نجاحاً.
لتجهيز jsdom مرة واحدة:

```bash
mkdir -p /tmp/jsdom-env && cd /tmp/jsdom-env && npm init -y >/dev/null && npm i jsdom >/dev/null
ENZO_JSDOM_PATH=/tmp/jsdom-env/node_modules python3 tests/test_dashboard_buttons.py
```

`test_engine_e2e.py` يعمل في **صندوق معزول** (`ENZO_HOME` مؤقت) — لا يلمس قاعدة
بياناتك ولا سجلاتك ولا أموالك. وهو يثبت المسار الكامل: اكتشاف → تحليل → BUY →
بوابة القابلية → تحجيم على رأس المال الحقيقي → تنفيذ MoonPay → مركز مربوط بـ
tx_hash → تنبيه تيليجرام → لوحة → نبض للمشرف.

**قبل أي تشغيل بأموال حقيقية:** نفّذ الحزم الثلاث والعشرين وتأكد من `0 failed` في كلٍّ منها.

### قواعد الخروج المُختبَرة / Exit rules under test

`test_exit_rules.py` يثبّت السلوك عند الحدود بالضبط: وقف الخسارة -38%،
الوقف المتحرك (عتبة تشغيل +40% **و** عتبة تنفيذ -40% من القمة، ولا يُخفَّض
الزناد أبداً)، وخروج الركود (ربح ≥ 15% **و** لا قمة جديدة منذ ≥ 30 ثانية)،
مع أولوياتها: المتحرك ثم الركود ثم جني الربح ثم وقف الخسارة. كما يثبّت أن
ساعة الركود تنجو من إعادة التشغيل، وأن مركزاً تالفاً في قاعدة البيانات لا
يُسقط دورة الخروج بأكملها.

---

## 10. التشخيص السريع / Quick triage

| العَرَض | أول أمر | الأرجح |
|---|---|---|
| "البوت لا يتداول" | `./enzoctl doctor` | انظر أول بند ✖ حرج |
| `/health` يعيد 503 | `./enzoctl health` | قائمة `problems` تحمل السبب |
| "0 مرشحين" كل دورة | `./enzoctl logs -n 50` | `GMGN_DISCOVERY_FAILED` أو `PUMPDEV_*` |
| اللوحة لا تتحدث | `./enzoctl status` | انظر `engine.last_scan_status` |
| مركز مفتوح لا يُغلق | `./enzoctl status` | `EXIT_MONITOR_DOWN_WITH_OPEN_POSITIONS` |
| رفض شراء متكرر | `./enzoctl logs audit -n 100` | `NO_ROUTE` = عملة على منحنى الربط |
| الحجم أصغر من المتوقع | `./enzoctl wallet` | رأس المال الحقيقي مقابل `risk_per_trade` |
| الصفقة دائماً $1.00 | `./enzoctl wallet` | رأس المال صغير → الأرضية مطبَّقة (طبيعي) |
| `SNIPER_DATA_UNAVAILABLE` / `FEES_UNKNOWN` / `MCAP_UNKNOWN` على عملات كثيرة دفعة واحدة | اللوحة ← بطاقة `⚡ GMGN Data Source` ← سطر **Ban** | حظر من GMGN: ليست أحكاماً على العملات بل غياب بيانات. `./enzoctl unban` يعرض المتبقي، و`--confirm` يمحوه |
| أمر $1.00 مرفوض بـ`BELOW_MINIMUM_TRADE` | `./enzoctl logs -n 100` | بوابة ENZO لا MoonPay. أُصلحت 2026-09-05: كان الحجم يُشتقّ مرتين (دولار←SOL←دولار) فيرجع `$0.9999999999999999 < $1.00` عند ~18% من أسعار SOL. الآن المرجع هو الحجم المقرّر مع تسامح مليون من الدولار |
| `SOL/USD is a GUESSED $180.00` في السجل | `./enzoctl logs` | تعذّر قراءة السعر من DexScreener ⇒ الحجم المُرسَل قد لا يساوي الدولار المقصود. تحقّق من خروج HTTPS، والأمر يُسجَّل بمصدر سعره (`sol_price_source`) |
| `INSUFFICIENT_CAPITAL_FOR_MINIMUM_TRADE` | `./enzoctl wallet` | المتاح أقل من `min_trade_usd` |
| «OpenClaw يقول إن اللوحة فُتحت وتعمل، لكنها لا تعمل» | `./enzoctl status` | المنفذ محجوز من عملية أخرى (غالباً نسخة أقدم من ENZO). `start` و`status` يخرجان الآن بـ**exit 1** ويطبعان `✖ THE DASHBOARD IS NOT THIS BOT's` مع رقم العملية الغريبة. قارن `X-Enzo-Pid` بـ`data/run/enzo.pid` |
| اللوحة تعرض أرقاماً/مراكز لا تعرفها | `curl -sD - -o /dev/null http://127.0.0.1:8077/health` | اقرأ `X-Enzo-Pid` و`X-Enzo-Data`: إن اختلفا عن بوتك فالصفحة لعملية أخرى (القسم 2.1) |
| `./enzoctl logs` لا يطبع شيئاً مع أن السجل غير فارغ | `./enzoctl logs -n 100` | **أُصلح 2026-09-06**: كان يقرأ السطور عبر `audit._tail_lines` الذي يعيد صفوف JSON جاهزة ويسقط كل سطر غير JSON — فـ`logs audit` كان ينهار (`'dict' object has no attribute 'rstrip'`) و`logs enzo/supervisor` كان لا يطبع شيئاً إطلاقاً. الآن يعرض السطور كما كُتبت (بما فيها الـtraceback)، والسجل الفارغ يقول «يوجد لكن بلا أسطر بعد» |
| `./enzoctl wallet` يرمي `AttributeError` | `./enzoctl wallet` | **أُصلح 2026-09-06**: كان يستدعي `executor.get_balance_snapshot` — دالة لم توجد قط — فينهار الأمر في كل تشغيل. الآن يستدعي `executor.get_wallet_snapshot` نفسها التي يقرأ بها مسار التداول، ويعرض الأرصدة ورأس المال القابل للنشر، ولا يرمي traceback عند غياب الـCLI |
| شارة 🚩 لا تظهر على مركز مشبوه | `./enzoctl positions` | **أُصلح 2026-09-06**: `db.save_full_state` كان يعيد كتابة صف المركز بلا `extra_json` فيمحو `rug_flags` (ومعه الوقف المبكر المشدّد للطبقة الأولى) عند أي حفظ كامل للحالة |
