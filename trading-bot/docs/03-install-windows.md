# ۳) نصب روی کامپیوتر ویندوز

زمان لازم: حدود ۲۰ دقیقه.

## ۳.۱ پیش‌نیازها

- ویندوز ۱۰ یا ۱۱
- متاتریدر ۵ نصب‌شده و وارد حساب شده (اول **دمو**)
- پایتون ۳.۹ یا بالاتر — از [python.org](https://www.python.org/downloads/)
  هنگام نصب حتماً تیک **«Add Python to PATH»** را بزنید.

## ۳.۲ نصب خودکار (پیشنهادی)

PowerShell را باز کنید و در پوشه پروژه اجرا کنید:

```powershell
cd trading-bot\scripts
powershell -ExecutionPolicy Bypass -File install_windows.ps1
```

اسکریپت این کارها را انجام می‌دهد:
1. پوشه داده متاتریدر را پیدا می‌کند.
2. فایل‌های MQL5 را در جای درست کپی می‌کند.
3. محیط مجازی پایتون و وابستگی‌ها را نصب می‌کند.
4. `config.json` را با رمزهای تصادفی قوی می‌سازد و نمایش می‌دهد.

**رمز پنل و توکن EA را که نمایش می‌دهد، یادداشت کنید.**

سپس به بخش ۳.۵ بروید.

## ۳.۳ نصب دستی فایل‌های MQL5

### پیدا کردن پوشه داده
در متاتریدر: منوی **File ← Open Data Folder**. پنجره‌ای مثل این باز می‌شود:

```
C:\Users\<نام شما>\AppData\Roaming\MetaQuotes\Terminal\<کد طولانی>\
```

### کپی فایل‌ها

```
از  trading-bot\mql5\Experts\ApexAlgo\   →  به  MQL5\Experts\ApexAlgo\
از  trading-bot\mql5\Include\ApexAlgo\   →  به  MQL5\Include\ApexAlgo\
```

ساختار نهایی باید این باشد:

```
MQL5\
├── Experts\ApexAlgo\ApexAlgoEA.mq5
└── Include\ApexAlgo\
    ├── Dashboard.mqh    ├── Json.mqh            ├── RiskManager.mqh
    ├── Defs.mqh         ├── Logger.mqh          ├── Signals.mqh
    ├── Executor.mqh     ├── MarketView.mqh      └── SymbolCtx.mqh
    ├── Filters.mqh      ├── PositionManager.mqh
    └── RemoteControl.mqh
```

### کامپایل
1. در متاتریدر کلید **F4** را بزنید تا MetaEditor باز شود.
2. در درخت سمت چپ: `Experts ← ApexAlgo ← ApexAlgoEA.mq5` را باز کنید.
3. کلید **F7** را بزنید.
4. در پایین باید ببینید: `0 errors, 0 warnings`.

> اگر خطای «cannot open include file» دیدید، فایل‌های `.mqh` در مسیر درست
> نیستند. حتماً باید داخل `MQL5\Include\ApexAlgo\` باشند.

## ۳.۴ نصب دستی سرور کنترل

```powershell
cd trading-bot\server
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
pip install MetaTrader5        # فقط روی ویندوز، برای ابزار آمار
python app.py
```

بار اول، فایل `config.json` ساخته می‌شود و رمز پنل و توکن EA در ترمینال چاپ
می‌شوند. **آن‌ها را یادداشت کنید.**

بررسی کنید که کار می‌کند: در مرورگر `http://127.0.0.1:8800` را باز کنید.

## ۳.۵ مجوز WebRequest در متاتریدر

بدون این مرحله، EA نمی‌تواند به سرور کنترل وصل شود.

1. متاتریدر ← منوی **Tools ← Options ← Expert Advisors**
2. تیک **«Allow WebRequest for listed URL»** را بزنید.
3. این آدرس را دقیقاً اضافه کنید:

```
http://127.0.0.1:8800
```

> آدرس باید **دقیقاً** با چیزی که در `InpRemoteUrl` می‌گذارید یکی باشد،
> شامل `http` یا `https` و شماره پورت.

## ۳.۶ اجرای EA

1. چارت نمادی که می‌خواهید معامله کنید را باز کنید (مثلاً EURUSD، تایم‌فریم M15).
2. در پنجره Navigator: `Expert Advisors ← ApexAlgo ← ApexAlgoEA`
3. روی چارت بکشید (drag & drop).
4. در تب **Common**، تیک **Allow Algo Trading** را بزنید.
5. در تب **Inputs** تنظیمات را وارد کنید:

| ورودی | مقدار پیشنهادی اولیه |
|---|---|
| `InpMagic` | یک عدد یکتا، مثلاً `20260921` |
| `InpSymbols` | خالی بگذارید (از نماد چارت استفاده می‌کند) |
| `InpRiskPercent` | `0.5` |
| `InpRemoteEnabled` | `true` |
| `InpRemoteUrl` | `http://127.0.0.1:8800` |
| `InpRemoteToken` | توکنی که سرور چاپ کرد |

6. روی **OK** کلیک کنید.
7. در نوار ابزار بالا، دکمه **Algo Trading** باید **سبز** باشد.

## ۳.۷ تأیید نصب

### روی چارت
گوشه بالا-چپ باید پنلی ببینید:

```
APEX ALGO  v1.0.0
5012345 @ Broker-Demo (USD)
STATE: ACTIVE
  all guards clear
------------------------------------------
Balance       10000.00   Equity     10000.00
Day P/L           0.00   (+0.00%)
...
Remote  : connected
```

- اگر `Remote : OFFLINE` نوشته → بخش ۳.۵ را دوباره بررسی کنید.
- اگر `STATE: TERMINAL_NOT_READY` → دکمه Algo Trading خاموش است.

### در تب Experts
باید ببینید:

```
[ApexAlgo][INFO ] ApexAlgo v1.0.0 starting - account ...
[ApexAlgo][INFO ] symbol ready: EURUSD  digits=5 point=0.00001 ...
[ApexAlgo][INFO ] initialised with 1 symbol(s), magic=20260921, risk=0.50%
```

### در داشبورد
`http://127.0.0.1:8800` را باز کنید، با رمز پنل وارد شوید. باید نقطه سبز و
اطلاعات حساب را ببینید.

## ۳.۸ اجرای خودکار سرور در استارتاپ ویندوز

فایلی به نام `apex-server.bat` بسازید:

```bat
@echo off
cd /d C:\path\to\trading-bot\server
call .venv\Scripts\activate.bat
python -m waitress --host=127.0.0.1 --port=8800 app:create_app
```

سپس کلیدهای `Win + R` را بزنید، `shell:startup` را تایپ کنید و یک میان‌بر از
این فایل در پوشه‌ای که باز می‌شود قرار دهید.

> `waitress` سرور تولیدی است. سرور توسعه Flask را برای استفاده دائمی به کار نبرید.

## ۳.۹ هشدار مهم درباره کامپیوتر شخصی

اگر ربات روی کامپیوتر خودتان اجرا شود:

- **خواب یا خاموش شدن** = ربات متوقف می‌شود.
- **قطع اینترنت** = ارتباط با بروکر قطع می‌شود.
- **ری‌استارت ویندوز برای آپدیت** = ربات بدون اطلاع شما متوقف می‌شود.

در هر سه حالت، **حد ضرر پوزیشن‌های باز روی سرور بروکر باقی می‌ماند** و اجرا
می‌شود. پس حساب شما بی‌محافظ نیست. اما تریلینگ، برداشت پله‌ای و ورودهای جدید
متوقف می‌شوند.

برای کار جدی، **VPS** استفاده کنید. به `04-install-vps.md` بروید.

### حداقل تنظیمات اگر روی کامپیوتر شخصی می‌مانید
- Settings ← System ← Power & sleep ← هر دو گزینه را روی **Never** بگذارید.
- Windows Update را روی «Active hours» گسترده تنظیم کنید.
