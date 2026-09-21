# ۴) نصب روی VPS برای کار ۲۴ ساعته

## ۴.۱ چرا VPS؟

| موضوع | کامپیوتر شخصی | VPS |
|---|---|---|
| کارکرد ۲۴/۷ | نه | بله |
| قطع برق | ربات متوقف | تأثیری ندارد |
| تأخیر تا سرور بروکر | ۵۰ تا ۲۰۰ میلی‌ثانیه | ۱ تا ۱۰ میلی‌ثانیه |
| ری‌استارت آپدیت ویندوز | خودسرانه | کنترل‌شده |
| هزینه ماهانه | ۰ | ۱۰ تا ۳۰ دلار |

تأخیر کم مهم است: هرچه سفارش سریع‌تر برسد، لغزش کمتر می‌شود.

## ۴.۲ دو گزینه

### گزینه الف: VPS خود متاتریدر (ساده‌ترین)
در متاتریدر: روی حساب راست‌کلیک ← **Register a Virtual Server**.

- ✅ راه‌اندازی در چند دقیقه، انتخاب خودکار نزدیک‌ترین دیتاسنتر به بروکر.
- ❌ فقط EA را اجرا می‌کند. **نمی‌توانید سرور کنترل پایتون را روی آن نصب کنید.**

اگر این را انتخاب کردید، سرور کنترل را جای دیگری اجرا کنید (یک VPS لینوکسی
ارزان) یا کلاً `InpRemoteEnabled=false` بگذارید و فقط از اپ موبایل متاتریدر
برای مشاهده استفاده کنید.

### گزینه ب: VPS ویندوز (کامل‌ترین)
یک Windows VPS با حداقل ۲ هسته CPU، ۴ گیگابایت رم و ۴۰ گیگابایت SSD.

ارائه‌دهنده‌ای را انتخاب کنید که دیتاسنترش نزدیک سرور بروکر باشد (اکثر بروکرها
در لندن `LD4` یا نیویورک `NY4` هستند).

هر دو بخش روی همان ماشین اجرا می‌شوند. توصیه‌شده.

## ۴.۳ راه‌اندازی VPS ویندوز

1. با **Remote Desktop** متصل شوید.
2. متاتریدر ۵ را نصب و وارد حساب شوید.
3. مراحل `03-install-windows.md` را انجام دهید.
4. در **Power Options** حالت **High performance** را انتخاب کنید.
5. در Task Scheduler، ری‌استارت خودکار ویندوز را به آخر هفته منتقل کنید.

## ۴.۴ دسترسی امن از بیرون

سرور کنترل به‌صورت پیش‌فرض فقط روی `127.0.0.1` گوش می‌دهد — یعنی از بیرون
قابل دسترسی نیست. این عمدی و درست است.

> ⛔ **هرگز** پورت ۸۸۰۰ را مستقیماً روی اینترنت باز نکنید. روی HTTP ساده،
> توکن و رمز شما قابل شنود است.

سه راه امن، از ساده به پیشرفته:

### الف) Cloudflare Tunnel (پیشنهادی)
هیچ پورتی باز نمی‌شود و HTTPS رایگان می‌گیرید.

```powershell
# cloudflared را از سایت Cloudflare دانلود کنید، سپس:
cloudflared tunnel login
cloudflared tunnel create apex
cloudflared tunnel route dns apex apex.yourdomain.com
cloudflared tunnel run --url http://127.0.0.1:8800 apex
```

سپس در `config.json` سرور:
```json
{ "host": "127.0.0.1", "port": 8800 }
```
و متغیر محیطی `APEX_BEHIND_PROXY=1` را تنظیم کنید.

در متاتریدر، به لیست URL های مجاز `https://apex.yourdomain.com` را اضافه کنید و
در EA همان را در `InpRemoteUrl` بگذارید.

### ب) Tailscale (ساده‌ترین)
یک شبکه خصوصی بین VPS و گوشی شما می‌سازد. هیچ چیزی روی اینترنت عمومی قرار
نمی‌گیرد.

1. Tailscale را روی VPS و روی گوشی نصب کنید.
2. از گوشی به آدرس `http://<نام-tailscale-vps>:8800` بروید.

محدودیت: چون HTTP است، PWA کامل کار نمی‌کند. برای استفاده شخصی کافی است.

### ج) Nginx یا Caddy با گواهی Let's Encrypt
اگر دامنه و VPS لینوکسی دارید. نمونه Caddyfile:

```
apex.yourdomain.com {
    reverse_proxy 127.0.0.1:8800
}
```

Caddy خودش گواهی HTTPS را می‌گیرد و تمدید می‌کند.

## ۴.۵ اجرای سرور کنترل روی لینوکس

اگر EA روی VPS ویندوز است و سرور کنترل را روی یک VPS لینوکسی ارزان می‌خواهید:

```bash
cd trading-bot/server
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

# باید روی همه رابط‌ها گوش بدهد چون EA از ماشین دیگری وصل می‌شود
export APEX_HOST=0.0.0.0
export APEX_BEHIND_PROXY=1
python -m waitress --host=$APEX_HOST --port=8800 app:create_app
```

یا با `scripts/run_server.sh`.

### سرویس systemd

```ini
# /etc/systemd/system/apex.service
[Unit]
Description=ApexAlgo control server
After=network.target

[Service]
Type=simple
User=apex
WorkingDirectory=/opt/apex/trading-bot/server
Environment="APEX_HOST=127.0.0.1"
Environment="APEX_BEHIND_PROXY=1"
ExecStart=/opt/apex/trading-bot/server/.venv/bin/python -m waitress \
          --host=127.0.0.1 --port=8800 app:create_app
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl enable --now apex
sudo systemctl status apex
sudo journalctl -u apex -f
```

## ۴.۶ چک‌لیست امنیتی

- [ ] رمز Remote Desktop طولانی و یکتا است
- [ ] پورت RDP از آدرس‌های ناشناس بسته است (فایروال)
- [ ] سرور کنترل پشت HTTPS است، نه HTTP عمومی
- [ ] `config.json` سطح دسترسی محدود دارد (اسکریپت این کار را می‌کند)
- [ ] توکن EA حداقل ۳۲ کاراکتر تصادفی است
- [ ] رمز پنل با هیچ رمز دیگری مشترک نیست
- [ ] در تلگرام، `allowed_chat_ids` پر شده است
- [ ] از فایل `apex.db` و `config.json` پشتیبان گرفته‌اید

## ۴.۷ پایش سلامت

```bash
# آیا سرور زنده است؟
curl -s https://apex.yourdomain.com/healthz

# لاگ سرویس
sudo journalctl -u apex -n 100 --no-pager
```

در VPS ویندوز، لاگ CSV خود EA اینجاست:
```
MQL5\Files\ApexAlgo\log_<شماره حساب>_<تاریخ>.csv
```
