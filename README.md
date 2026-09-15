# metacognition.hq — کارخانهٔ خودکار ریلز روزانه (رایگان، Buffer رسمی)

Autonomous daily Instagram-Reel factory for **@metacognition.hq**:
topic → source validation → EN script → clean FA translation → Edge-TTS →
9:16 render → independent **Quality Supervisor** → (only if approved) **official
Buffer API** queue → Buffer publishes at **19:30 Asia/Tehran** → exactly **one**
GitHub Issue per day.

No `/publish` step, no Meta Graph API, no Selenium/instagrapi/cookies/passwords,
no paid services. A bad or unverifiable reel simply **cancels that day's post**.

---

## ۱) توقف اضطراری (Emergency stop)

هر کدام از این‌ها به‌تنهایی انتشار واقعی را متوقف می‌کند (از سریع‌ترین به کندترین):

| روش | اثر | زمان |
| --- | --- | --- |
| **Settings → Secrets and variables → Actions → Variables → `AUTO_PUBLISH_ENABLED` را به `false` تغییر دهید یا حذف کنید** | از اجرای بعدی، همه‌چیز ساخته می‌شود اما `createPost` هرگز صدا زده نمی‌شود (وضعیت `approved-dry-run`) | فوری |
| **Actions → daily-trend-draft → ⋯ → Disable workflow** | هیچ اجرایی (کرون یا دستی) شروع نمی‌شود | فوری |
| **Secret `BUFFER_TOKEN` را حذف کنید** (یا کلید را در Buffer باطل کنید) | مرحلهٔ Buffer با `buffer-error` می‌ایستد؛ هیچ پستی ساخته نمی‌شود | فوری |
| **Buffer → Channel → Pause queue** | پست‌های در صف ارسال نمی‌شوند؛ اسکریپت هم کانالِ pause‌شده را رد می‌کند | فوری |
| پستی که همین الان در صف Buffer است را حذف کنید | خود Buffer؛ شناسهٔ پست در Issue روز و `content/editorial_memory.json` هست | دستی |

> اجرای در حال انجام را می‌توانید از صفحهٔ Actions با **Cancel workflow** متوقف کنید. اگر
> مرحلهٔ Buffer قبلاً پست را ساخته باشد، Cancel آن را برنمی‌گرداند — پست را در Buffer حذف کنید.

---

## ۲) کنترل‌ها (Variables / Secrets / Inputs)

| نام | نوع | مقدار | معنی |
| --- | --- | --- | --- |
| `BUFFER_TOKEN` | **Secret** | کلید شخصی Buffer | فقط در دو استپ (صف Buffer، همگام‌سازی متریک) دیده می‌شود؛ هرگز چاپ نمی‌شود |
| `AUTO_PUBLISH_ENABLED` | Variable | دقیقاً `true` | فقط رشتهٔ دقیق `true`. نبودن/`True`/`1`/`yes` ⇒ غیرفعال (`approved-dry-run`) |
| `TARGET_BUFFER_CHANNEL` | Variable | `metacognition.hq` (پیش‌فرض) | کانال فقط وقتی معتبر است که نامش **دقیقاً** برابر این باشد **و** سرویسش Instagram باشد |
| `MIN_QA_SCORE` | Variable | `85` (پیش‌فرض) | حداقل امتیاز ناظر کیفیت؛ مقدار کمتر از **۸۰** پذیرفته نمی‌شود (به ۸۰ بالا می‌رود) |
| `DRY_RUN_CRON` | Variable | خالی / `true` | اگر `true` باشد اجرای کرون هم dry-run است (برای دورهٔ آزمایش) |
| ورودی `dry_run` | dispatch | پیش‌فرض **true** | اجرای دستی همیشه dry-run است مگر آن را خاموش کنید؛ روی `AUTO_PUBLISH_ENABLED` اولویت دارد |
| ورودی `calendar_only` | dispatch | false | منابع زندهٔ ترند را رد کن و از `content/calendar.json` بساز |
| ورودی `date_tag` | dispatch | خالی = امروز | ساخت/بازسازی برای تاریخ مشخص (`YYYY-MM-DD`) |

انتشار واقعی فقط وقتی اتفاق می‌افتد که **همهٔ** این‌ها برقرار باشند:
`AUTO_PUBLISH_ENABLED == "true"` **و** `dry_run == false` **و**
`approved && score >= MIN_QA_SCORE && blocking_errors == []` **و** URL عمومی ویدئو
با MIME درست پاسخ دهد **و** کانال دقیقاً پیدا شود **و** صف پر نباشد **و** پستی با
همین `reel-id` از قبل در صف نباشد.

---

## ۳) راه‌اندازی مرحله‌ای (Staged rollout)

| مرحله | کار | پیش‌نیاز |
| --- | --- | --- |
| **0** | تست‌های محلی: `python3 -m unittest discover -s tests` (۱۳۴ تست، همه mock) | هیچ |
| **1** | **Actions → buffer-connection-check → Run workflow** — فقط خواندن: حساب، سازمان، کانال دقیق، وضعیت صف. هیچ پستی ساخته نمی‌شود | Secret `BUFFER_TOKEN` |
| **2** | **Actions → daily-trend-draft → Run workflow** با `dry_run = true` (پیش‌فرض): ریل کامل + ناظر + Issue با پیش‌نمایش؛ `createPost` صدا زده نمی‌شود | مرحلهٔ ۱ سبز |
| **3** | یک تست کنترل‌شدهٔ صف — فقط با تأیید صریح مالک: `AUTO_PUBLISH_ENABLED=true` + اجرای دستی با `dry_run = false` | Issue مرحلهٔ ۲ بازبینی شده |
| **4** | فعال‌سازی: `AUTO_PUBLISH_ENABLED=true` بماند؛ کرون `23 3 * * *` (۰۶:۵۳ تهران؛ GitHub معمولاً چند ساعت تأخیر دارد) هر روز حداکثر **یک** پست به صف می‌فرستد؛ Buffer ۱۹:۳۰ منتشر می‌کند | ورک‌فلو روی شاخهٔ پیش‌فرض merge شده |

> کرون فقط روی **شاخهٔ پیش‌فرض** اجرا می‌شود؛ تا زمانی که PR merge نشده، فقط اجرای دستی از
> شاخهٔ PR ممکن است (در صفحهٔ Run workflow شاخه را انتخاب کنید).

---

## ۴) خواندن Issue روزانه و عیب‌یابی بر اساس برچسب

هر اجرا دقیقاً **یک** Issue با عنوان `Daily reel <date> — <status> — <topic>` می‌سازد
یا همان را به‌روزرسانی می‌کند (شناسهٔ `content_id` در بدنه). برچسب‌ها خودکار ساخته می‌شوند.

| برچسب | معنی | چه کنم؟ |
| --- | --- | --- |
| `queued-in-buffer` | پست در صف Buffer است؛ شناسهٔ پست در Issue | هیچ؛ ۱۹:۳۰ منتشر می‌شود. برای لغو: در Buffer حذف کنید |
| `approved-dry-run` | ریل تأیید شد ولی انتشار غیرفعال/dry-run بود | اگر می‌خواهید منتشر شود: مرحلهٔ ۳/۴ |
| `qa-failed` | ناظر کیفیت رد کرد (پس از حداکثر یک بازسازی) | جدول QA و «blocking errors» را در Issue ببینید؛ معمولاً نیازی به کار نیست، فردا موضوع تازه‌ای می‌آید. اگر تکرار شد: `output/auto-<date>_qa.md` در artifact |
| `trend-error` | هیچ موضوع قابل قبولی (نه ترند نه تقویم) | `content/calendar.json` را غنی کنید یا `content/editorial_memory.json` را بررسی کنید (شاید همهٔ موضوعات اخیراً استفاده شده‌اند) |
| `source-error` | منبع شواهد قابل دسترس/معتبر نبود | معمولاً گذراست؛ اگر تکرار شد URL منبع playbook را در `build/content_producer.py` اصلاح کنید |
| `script-error` | ساخت اسکریپت شکست خورد | لاگ اجرا؛ باگ کد است — Issue را باز نگه دارید |
| `translation-error` | ترجمهٔ فارسی معتبر به دست نیامد (Google و MyMemory) | گذرا؛ اگر چند روز تکرار شد سرویس‌های ترجمه از runner مسدود شده‌اند |
| `tts-error` | Edge-TTS پاسخ نداد | گذرا (سرویس مایکروسافت)؛ Re-run کنید یا منتظر فردا بمانید |
| `render-error` | رندر/ffmpeg شکست خورد | لاگ اجرا؛ فونت‌ها (`build/fetch_fonts.sh`) و ffmpeg را چک کنید |
| `buffer-error` | کانال پیدا نشد / توکن نامعتبر / خطای API | `buffer-connection-check` را اجرا کنید؛ نام کانال و `TARGET_BUFFER_CHANNEL` را مقایسه کنید؛ توکن را در Buffer تازه کنید |
| `queue-full` | صف کانال پر است (پلن رایگان ۱۰ پست) | چند پست در Buffer منتشر/حذف شوند؛ فردا دوباره تلاش می‌شود |
| `duplicate-prevented` | پستی با همین `reel-id` از قبل در صف بود (اجرای تکراری) | هیچ؛ همان پست قبلی معتبر است |
| `automation-error` | خطای غیرمنتظرهٔ ورک‌فلو (push شاخهٔ عمومی، CDN، …) | لاگ اجرا؛ Re-run معمولاً کافی است (اجرای مجدد پست تکراری نمی‌سازد) |

**اجرای مجدد امن است:** قبل از هر `createPost`، صف کانال برای خط `reel-id: reel-<date>` جست‌وجو
می‌شود؛ اگر پست وجود داشته باشد همان «پذیرفته» می‌شود و پست دومی ساخته نمی‌شود. هنگام
timeout/پاسخ مبهم هم ابتدا وجود پست بررسی می‌شود.

---

## ۵) معماری

```
build/trend_scout.py      Trend Scout: HN Algolia + Google Trends RSS + arXiv (+ calendar fallback), negative
                          keywords, pillar rotation, dedupe vs editorial memory, "speakable noun phrase" gate
                          (a headline that cannot be spoken as a topic is skipped) → topic.json
build/content_producer.py Content Producer: 22 playbooks + trend lenses → EN script (Hook→Problem→Explanation→
                          Example→Technique→Ending), FA translation (Google→MyMemory), sources w/ tiers
build/tts_edge.py         Edge-TTS (voice/rate from policy) ; build/timing.py → word timings
build/reel_engine.py      1080×1920 renderer (node/edge mesh, EN karaoke top, FA RTL pill bottom, layout.json)
build/render_auto.py      full render (+ --safe re-render) ; build/poster_auto.py 9:16 + 4:5 posters
build/caption.py          caption ≤ 2200 chars, hashtags separated for first comment
build/qa_supervisor.py    Quality Supervisor: 11 weighted checks, JSON + Markdown, exit 0/20/21
build/buffer_publish.py   official Buffer GraphQL client: check / find-channel / queue / publish
build/pipeline.py         produce (controlled retries) / verify (public URL) / record (manifest + memory)
build/report_issue.py     exactly one issue per content id, labels, previews, QA table
build/performance_analyst.py  Buffer metrics → content/performance_history.json + weekly report (read-only)
build/retention.py        deletes old drafts/<date> branches only after Buffer status is terminal
content/editorial_policy.json  every threshold and layout number lives here (QA never lowers them)
content/editorial_memory.json  topics/hashes/QA scores/Buffer post ids (no secrets, no PII)
```

**Quality Supervisor** (`build/qa_supervisor.py`) — مستقل از تولیدکننده، خروجی JSON+Markdown با
`approved`, `score`, `checks{topic_relevance, source_quality, script_quality, english_quality,
persian_quality, subtitle_layout, audio_quality, video_quality, caption_quality, duplicate_check,
buffer_readiness}`, `warnings`, `blocking_errors`. هر خطای blocking ⇒ رد، فارغ از امتیاز.
بررسی‌ها: ربط موضوع، clickbait، هوک ≤۳ ثانیه، انسجام، تکنیک پس از توضیح، URL منبع، آمار بدون منبع،
انگلیسی طبیعی، فارسی معتبر (نسبت حروف فارسی، بدون انگلیسی/placeholder، ZWNJ)، CTA غیر اسپم، تکراری نبودن،
MP4 قابل decode/9:16/۶۰–۱۲۰ ثانیه/A-V match/بدون سیاهی و سکوت طولانی/bitrate، پوسترها، caption ≤۲۲۰۰،
URL عمومی + MIME، safe zone از `layout.json`، نمونه‌برداری فریم (contact sheet فقط داخلی).

**Retry policy:** Scout رد شد → کاندیدای بعدی → تقویم؛ اسکریپت/ترجمه رد شد → فقط **یک** بار
بازتولید (`--variant 1`)؛ رندر رد شد → فقط **یک** رندر امن‌تر (`--safe`)؛ شکست دوم → بدون پست،
Issue با `qa-failed`. آستانه‌ها هرگز پایین نمی‌آیند.

---

## ۶) اجرای محلی (بدون شبکه)

```bash
pip install pillow numpy arabic_reshaper python-bidi fonttools brotli imageio-ffmpeg edge-tts deep-translator pyyaml
bash build/fetch_fonts.sh && python3 build/logo_make.py
python3 -m unittest discover -s tests                      # 134 tests, all mocked (Buffer + GitHub + CDN mocked; local bare git origin)
python3 build/pipeline.py produce --tag 2026-09-16 --calendar-only --synthetic-tts --fixture-translation --skip-network
python3 build/report_issue.py --tag 2026-09-16 --state output/auto-2026-09-16_state.json --repo o/r --run-url x --dry-run
```

حالت‌های تست (`--synthetic-tts`, `--fixture-translation`, `--fixture`) در GitHub Actions رد
می‌شوند مگر `ALLOW_TEST_MODES=1`؛ ناظر هم ترجمهٔ fixture را بدون `QA_ALLOW_FIXTURE=1` رد می‌کند.

## ۷) فایل‌ها و نگه‌داری

- روی شاخهٔ پیش‌فرض فقط JSON کوچک commit می‌شود (`content/editorial_memory.json`,
  `content/performance_history.json`, `output/auto-<date>_manifest.json`).
- ویدئو/پوستر/پیش‌نمایش هر روز روی شاخهٔ تک‌کامیتی `drafts/<date>` می‌رود (URL عمومی برای Buffer).
  `build/retention.py` آن را فقط وقتی حذف می‌کند که وضعیت Buffer نهایی شده باشد (`sent`/`error`)
  و حداقل `retention.draft_branch_days` روز گذشته باشد.
- شاخه‌های `drafts/2026-09-13..15` و Issueهای #5/#7/#12/#13 متعلق به ورک‌فلوی قدیمی‌اند؛ هیچ‌وقت
  منتشر نمی‌شوند و در حافظهٔ تحریریه به‌عنوان `legacy-not-published` ثبت شده‌اند.
- `build/insta_publish.py` و `publish-approved-draft.yml` بازنشسته‌اند و هیچ قابلیت انتشاری ندارند.
- لوگوی فعلی stand-in است (`build/logo_make.py`) و هیچ‌جا «رسمی» خوانده نمی‌شود؛ فایل لوگوی واقعی را
  در `assets/img/` بگذارید تا جایگزین شود.
