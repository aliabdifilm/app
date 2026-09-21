# ۱۱) مرجع کامل تنظیمات

> این جدول مستقیماً از کد `ApexAlgoEA.mq5` تولید شده است، پس همیشه با
> نسخه واقعی EA هماهنگ است.

مجموع **97 ورودی** در **10 گروه**.


## عمومی

| ورودی | نوع | پیش‌فرض | توضیح |
|---|---|---|---|
| `InpMagic` | `long` | `20260921` | Magic number (unique per EA instance) |
| `InpSymbols` | `string` | `""` | Symbols, comma separated (empty = chart symbol) |
| `InpSignalTF` | `ENUM_TIMEFRAMES` | `PERIOD_M15` | Signal timeframe |
| `InpBiasTF` | `ENUM_TIMEFRAMES` | `PERIOD_H4` | Higher timeframe bias |
| `InpTradeComment` | `string` | `"ApexAlgo"` | Order comment |
| `InpLogLevel` | `ENUM_APEX_LOGLEVEL` | `APEX_LOG_INFO` | Log level |
| `InpLogToFile` | `bool` | `true` | Write a CSV audit log |

## استراتژی

| ورودی | نوع | پیش‌فرض | توضیح |
|---|---|---|---|
| `InpStrategyMode` | `ENUM_APEX_STRATMODE` | `APEX_STRAT_AUTO` | Strategy selection |
| `InpRequireBias` | `bool` | `true` | Trend trades must agree with higher timeframe |
| `InpMinScore` | `double` | `0.45` | Minimum ensemble score to trade (0..1) |
| `InpAtrPeriod` | `int` | `14` | ATR period |
| `InpAdxPeriod` | `int` | `14` | ADX period |
| `InpRsiPeriod` | `int` | `14` | RSI period |
| `InpBBPeriod` | `int` | `20` | Bollinger period |
| `InpBBDev` | `double` | `2.0` | Bollinger deviation |
| `InpEmaFast` | `int` | `21` | Fast EMA (signal TF) |
| `InpEmaSlow` | `int` | `55` | Slow EMA (signal TF) |
| `InpBiasEmaFast` | `int` | `50` | Fast EMA (bias TF) |
| `InpBiasEmaSlow` | `int` | `200` | Slow EMA (bias TF) |
| `InpDonchianPeriod` | `int` | `20` | Donchian breakout period |
| `InpErPeriod` | `int` | `20` | Efficiency ratio period |
| `InpAtrAvgPeriod` | `int` | `100` | ATR average / percentile window |

## آستانه‌های تشخیص رژیم

| ورودی | نوع | پیش‌فرض | توضیح |
|---|---|---|---|
| `InpAdxTrendMin` | `double` | `23.0` | ADX above this = trending |
| `InpAdxRangeMax` | `double` | `18.0` | ADX below this = ranging |
| `InpErTrendMin` | `double` | `0.30` | Efficiency ratio above this = trending |
| `InpErRangeMax` | `double` | `0.22` | Efficiency ratio below this = ranging |
| `InpRsiOversold` | `double` | `30.0` | RSI oversold |
| `InpRsiOverbought` | `double` | `70.0` | RSI overbought |
| `InpPullbackLookback` | `int` | `5` | Bars to look back for a pullback |

## هندسه حد ضرر و هدف

| ورودی | نوع | پیش‌فرض | توضیح |
|---|---|---|---|
| `InpAtrStopTrend` | `double` | `2.0` | Stop = ATR x this (trend trades) |
| `InpAtrStopRange` | `double` | `1.5` | Stop = ATR x this (range trades) |
| `InpRRTrend` | `double` | `2.0` | Reward:risk target (trend) |
| `InpRRRange` | `double` | `1.2` | Reward:risk target (range) |

## ریسک (محدودیت‌های سخت)

| ورودی | نوع | پیش‌فرض | توضیح |
|---|---|---|---|
| `InpRiskPercent` | `double` | `0.5` | Risk per trade, % of equity |
| `InpMaxRiskPercent` | `double` | `2.0` | Ceiling remote control cannot exceed |
| `InpDailyLossLimitPct` | `double` | `3.0` | Stop for the day at this % loss |
| `InpMaxDrawdownPct` | `double` | `10.0` | Kill switch at this % drawdown from peak |
| `InpDDThrottleStartPct` | `double` | `4.0` | Start reducing risk at this drawdown |
| `InpDDThrottleFloor` | `double` | `0.35` | Minimum risk multiplier when throttled |
| `InpMaxPositionsTotal` | `int` | `4` | Max simultaneous positions |
| `InpMaxPositionsPerSym` | `int` | `1` | Max positions per symbol |
| `InpMaxTradesPerDay` | `int` | `10` | Max new trades per day |
| `InpLossStreakLimit` | `int` | `3` | Consecutive losses before cooldown |
| `InpCooldownMinutes` | `int` | `120` | Cooldown length in minutes |
| `InpMaxLotsPerTrade` | `double` | `5.0` | Absolute lot ceiling |
| `InpMaxMarginUtilPct` | `double` | `20.0` | Max % of free margin per trade |
| `InpMaxExposurePerCcy` | `int` | `2` | Max open positions per currency |

## مدیریت معامله باز

| ورودی | نوع | پیش‌فرض | توضیح |
|---|---|---|---|
| `InpUseBreakEven` | `bool` | `true` | Move stop to break-even |
| `InpBeTriggerR` | `double` | `1.0` | Break-even after this many R |
| `InpBeOffsetR` | `double` | `0.10` | Lock in this many R past entry |
| `InpUsePartial` | `bool` | `true` | Take partial profit |
| `InpPartialTriggerR` | `double` | `1.0` | Partial close at this many R |
| `InpPartialPercent` | `double` | `50.0` | % of position to close |
| `InpUseTrailing` | `bool` | `true` | ATR trailing stop |
| `InpTrailStartR` | `double` | `1.2` | Start trailing after this many R |
| `InpTrailAtrMult` | `double` | `2.0` | Trail distance = ATR x this |
| `InpUseTimeStop` | `bool` | `true` | Close stagnant trades |
| `InpMaxBarsInTrade` | `int` | `96` | Max bars before time stop |
| `InpTimeStopMinR` | `double` | `0.5` | Only time-stop trades below this R |

## فیلترها

| ورودی | نوع | پیش‌فرض | توضیح |
|---|---|---|---|
| `InpMaxSpreadAtr` | `double` | `0.25` | Reject if spread > this x ATR |
| `InpMaxSpreadPoints` | `int` | `0` | Hard spread ceiling in points (0 = off) |
| `InpMinAtrPoints` | `double` | `0.0` | Minimum ATR in points (0 = off) |
| `InpMaxAtrMultiple` | `double` | `4.0` | Reject if ATR > this x average (0 = off) |
| `InpMinBarsBetweenTrd` | `int` | `3` | Minimum bars between entries per symbol |
| `InpUseSessions` | `bool` | `false` | Restrict to trading sessions |
| `InpSess1Start` | `int` | `7` | Session 1 start hour (server time) |
| `InpSess1End` | `int` | `16` | Session 1 end hour |
| `InpSess2Start` | `int` | `0` | Session 2 start hour (0 = unused) |
| `InpSess2End` | `int` | `0` | Session 2 end hour |
| `InpTradeSunday` | `bool` | `false` | Trade Sunday (crypto/CFD only) |
| `InpTradeMonday` | `bool` | `true` | Trade Monday |
| `InpTradeTuesday` | `bool` | `true` | Trade Tuesday |
| `InpTradeWednesday` | `bool` | `true` | Trade Wednesday |
| `InpTradeThursday` | `bool` | `true` | Trade Thursday |
| `InpTradeFriday` | `bool` | `true` | Trade Friday |
| `InpTradeSaturday` | `bool` | `false` | Trade Saturday (crypto/CFD only) |
| `InpWeekendFlat` | `bool` | `true` | Flatten before the weekend |
| `InpFridayCloseHour` | `int` | `20` | Stop and flatten from this hour on Friday |
| `InpMondayOpenHour` | `int` | `1` | Do not trade before this hour on Monday |
| `InpUseNewsFilter` | `bool` | `true` | Block around high-impact news |
| `InpNewsMinutesBefore` | `int` | `30` | Blackout minutes before news |
| `InpNewsMinutesAfter` | `int` | `30` | Blackout minutes after news |
| `InpNewsMinImportance` | `int` | `3` | 1=low 2=moderate 3=high |

## اجرای سفارش

| ورودی | نوع | پیش‌فرض | توضیح |
|---|---|---|---|
| `InpSlippagePoints` | `int` | `20` | Max deviation in points |
| `InpMaxRetries` | `int` | `3` | Order retry attempts |
| `InpRetryDelayMs` | `int` | `250` | Delay between retries (ms) |

## کنترل از راه دور (گوشی / مرورگر)

| ورودی | نوع | پیش‌فرض | توضیح |
|---|---|---|---|
| `InpRemoteEnabled` | `bool` | `false` | Enable the control server link |
| `InpRemoteUrl` | `string` | `"http://127.0.0.1:8800"` | Control server base URL |
| `InpRemoteToken` | `string` | `""` | Shared secret token |
| `InpBotId` | `string` | `"apex-1"` | This bot's id on the panel |
| `InpRemoteTimeoutMs` | `int` | `3000` | HTTP timeout (ms) |
| `InpRemotePollSeconds` | `int` | `10` | Heartbeat interval (seconds) |
| `InpRemoteStaleSeconds` | `int` | `180` | Link considered dead after this |
| `InpOfflinePolicy` | `ENUM_APEX_OFFLINE` | `APEX_OFFLINE_KEEP_TRADING` | If the server is unreachable |

## نمایش

| ورودی | نوع | پیش‌فرض | توضیح |
|---|---|---|---|
| `InpShowPanel` | `bool` | `true` | Show the on-chart panel |
| `InpPanelX` | `int` | `12` | Panel X offset |
| `InpPanelY` | `int` | `22` | Panel Y offset |

---

## کدام تنظیمات واقعاً مهم‌اند؟

از این ۹۷ ورودی، فقط چند مورد نیاز به تصمیم شما دارند. بقیه پیش‌فرض‌های معقولی
دارند.

### باید تنظیم کنید

| ورودی | راهنما |
|---|---|
| `InpMagic` | برای هر نسخه EA روی هر حساب، یک عدد **یکتا**. اگر دو EA یک Magic داشته باشند، معاملات یکدیگر را مدیریت می‌کنند. |
| `InpSymbols` | خالی = نماد چارت. برای چند نماد: `EURUSD,GBPUSD`. **نام دقیق بروکر** را بنویسید (ممکن است `EURUSD.raw` باشد). |
| `InpRiskPercent` | مهم‌ترین عدد کل سیستم. با `0.25` تا `0.5` شروع کنید. |
| `InpMaxDrawdownPct` | جایی که می‌پذیرید سیستم شکست خورده و باید متوقف شود. |
| `InpRemoteToken` | همان توکنی که سرور کنترل چاپ می‌کند. |

### احتمالاً باید تنظیم کنید

| ورودی | راهنما |
|---|---|
| `InpSignalTF` / `InpBiasTF` | تایم‌فریم سیگنال باید **کوچک‌تر یا مساوی** تایم‌فریم جهت باشد. |
| `InpDailyLossLimitPct` | برای حساب پراپ، بسیار سخت‌گیرانه‌تر از قانون شرکت. |
| `InpMaxSpreadPoints` | برای طلا و شاخص‌ها لازم است (مثلاً `50`). |
| `InpUseSessions` + ساعت‌ها | بر اساس **ساعت سرور بروکر**، نه ساعت شما. |

### تقریباً هرگز دست نزنید

`InpMaxRiskPercent`، `InpMaxMarginUtilPct`، `InpMaxExposurePerCcy`،
`InpSlippagePoints`، `InpMaxRetries` — این‌ها محافظ‌های ایمنی‌اند و
پیش‌فرض‌هایشان محافظه‌کارانه است.

### ورودی‌هایی که بهینه‌ساز می‌تواند لمس کند

فقط این شش تا، و فقط در Walk-Forward:

```
InpMinScore  InpAtrStopTrend  InpRRTrend
InpAdxTrendMin  InpDonchianPeriod  InpTrailAtrMult
```

### ورودی‌هایی که در بک‌تست باید خاموش باشند

```
InpRemoteEnabled = false     WebRequest در Strategy Tester کار نمی‌کند
InpUseNewsFilter = false     تقویم اقتصادی در تستر در دسترس نیست
InpLogToFile     = false     سرعت تست را بالا می‌برد
InpShowPanel     = false
```
