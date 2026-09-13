# راه‌اندازی انتشار خودکار روی اینستاگرام (رسمی و امن)

## پیش‌نیازها (یک‌بار، ~۱۰ دقیقه، فقط توسط صاحب پیج)
1. اینستاگرام تو: Settings → Account type → **Professional account** (Business).
2. در فيسبوك: یک **Page** بساز (یا موجود را استفاده کن) و در تنظیمات Page:
   Settings → Linked accounts → Instagram → Connect (لاگین پیج خودت).
3. برو به https://developers.facebook.com → **Create App** → نوع **Business**.
4. داخل اپ: Add product → **Instagram Graph API** (و در صورت نیاز Facebook Login for Business).
5. Graph API Explorer (داخل همان اپ):
   - انتخاب اپ خودت، و Get Token با اسکوپ‌ها:
     `instagram_basic`, `instagram_content_publish`, `pages_show_list`, `pages_read_engagement`
     (اختیاری برای آمار: `instagram_manage_insights` | برای بات کامنت/DM: `pages_manage_metadata`, `instagram_manage_messages`)
   - توکن اولیه کوتاه‌مدت است؛ آن را LONG-LIVED کن:
     ```
     GET /oauth/access_token?grant_type=fb_exchange_token&client_id=APP_ID&client_secret=APP_SECRET&fb_exchange_token=USER_TOKEN
     ```
   - سپس توکن Page بلندمدت را بگیر:
     ```
     GET /me/accounts   (با توکن بلندمدت user)  →  access_tokenِ Page همان توکن بلندمدت است
     ```
   - آی‌دی پیج اینستاگرام: `GET /me?fields=instagram_business_account` → `instagram_business_account.id`
6. فایل `secrets/insta.env` بساز (در git نادیده گرفته می‌شود):
   ```
   IG_USER_ID=178414XXXXXXXXX
   PAGE_TOKEN=EAAG...
   APP_ID=...
   APP_SECRET=...
   ```

## میزبانی عمومی ویدئو (Graph API ویدئو را از URL می‌خواند)
- ساده‌ترین: مخزن گیت‌هاب را **public** کن (فایل‌های ریل همان‌جا هستند) →
  URL: `https://github.com/Captain-Jorf/workspace/raw/arena/01a09602-workspace/output/reel_metacognition_hq_lite.mp4`
- یا هر هاست عمومی خودت (S3/R2/هاست شخصی). فایل محلی سندباکس قابل استفاده نیست.

## انتشار
```
python3 build/insta_publish.py token-info                 # تست توکن
python3 build/insta_publish.py publish \
   --video "URL_PUBLIC" --caption output/ep02_caption.txt          # انتشار فوری
python3 build/insta_publish.py publish --video URL --caption F --at "2026-09-14 19:30"   # صف زمان‌بندی
python3 build/insta_publish.py run-due                    # cron هر دقیقه / یا ورکر پس‌زمینه
python3 build/insta_publish.py refresh-token              # هر ~۵۵ روز
```
کپشن به‌صورت خودکار تقسیم می‌شود: متن اصلی = caption پست، خط‌های شروع‌شونده با `#` = **کامنت اول**.

## cron نمونه
```
* * * * * cd /path/to/workspace && python3 build/insta_publish.py run-due >> /tmp/ig.log 2>&1
```

## محدودیت‌ها و قانون‌ها
- ~۲۵ پست API در ۲۴ ساعت؛ توکن بلندمدت ~۶۰ روزه (refresh کن).
- هرگز از ابزارهای غیررسمی (رمز عبور + selenium/instagrapi) استفاده نکن → ریسک بن.
- بات کامنت/DM مرحله‌ی بعد است (webhook + اپ در حالت Live با App Review).
