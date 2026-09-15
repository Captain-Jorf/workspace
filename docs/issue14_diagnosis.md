# Issue #14 Diagnosis — The planning fallacy: your to-do list is lying
Date: 2026-09-15 — content_id: reel-2026-09-15 — engine: mymemory — QA score 92 (false positive)

## Summary
The reel was labeled approved-dry-run with score 92, but Persian subtitles were garbled due to MyMemory-only translation, lack of curated glossary, and renderer bidi/shaping bugs. The 16 lines below are the full playbook for planning-fallacy (hook + problem + bridge + explain + example + technique + recap + ending).

Root causes identified:
- MyMemory semantics: literal idioms, wrong sense (reliable → قابل اعتماد, history → تاریخچه, pad → پوشش دهید)
- Vocab: planning fallacy → مغالطه (wrong) instead of خطای برنامه‌ریزی (glossary)
- Formality mismatch: شما/کنید vs required informal conversational تو/کن (policy: EN informal, FA natural)
- Length: 4 lines > 85 chars, not mobile-friendly (policy max 90, ideal 45-60)
- Punctuation: English ? and , not converted to ؟ ، ; ZWNJ missing (میکشه, دست نخورده)
- RTL/bidi: Question mark at start in raw, not mirrored; mixed EN/FA not handled
- Latin leak: None in this reel, but risk if allowlist not enforced
- Reshaper/bidi bug: renderer used PIL without raqm, logical rendering broken for presentation forms
- Subtitle size: QA only warned about length, did not block

## Line-by-line (16 lines)

| # | EN original | Gloss input (translator_source) | Raw FA (MyMemory) | Normalized FA (polish_fa) | Rendered FA (old renderer) | Problem | Suggested fix | Causes |
|---|---|---|---|---|---|---|---:|---|
| 1 | Why does everything take twice as long as you promised yourself? | — | چرا همه چیز دو برابر زمانی که به خودت قول دادی طول میکشه؟ | چرا همه چیز دو برابر زمانی که به خودت قول دادی طول می‌کشد؟ | (logical, LTR ? at wrong side) چرا همه چیز دو برابر زمانی که به خودت قول دادی طول میکشه؟ | Colloquial میکشه, ZWNJ missing, همه چیز vs هر کاری, informal but inconsistent | Curated: چرا هر کاری دو برابرِ چیزی که فکر می‌کردی طول می‌کشد؟ | vocab, punctuation, ZWNJ, informal consistency |
| 2 | You plan the week, and it looks doable. Even relaxed. | — | شما برای هفته برنامه‌ریزی می‌کنید، و به نظر می‌رسد که شدنی است. حتی با آرامش. | شما برای هفته برنامه‌ریزی می‌کنید، و به نظر می‌رسد که شدنی است. حتی با آرامش. | شما برای هفته برنامه‌ریزی می‌کنید، و به نظر می‌رسد که شدنی است. حتی با آرامش. | Formal شما/می‌کنید, 89 chars too long, two sentences crammed, literal | Curated: برای هفته برنامه می‌ریزی و همه‌چیز شدنی به نظر می‌آید، حتی راحت. | vocab, length, formality |
| 3 | Then Thursday arrives and half the list is untouched. | — | سپس پنجشنبه از راه می‌رسد و نیمی از لیست دست نخورده باقی مانده است. | سپس پنجشنبه از راه می‌رسد و نیمی از لیست دست‌نخورده باقی مانده است. | ... دست نخورده ... | ZWNJ missing in دست نخورده, formal, long | Curated: بعد پنج‌شنبه می‌رسد و نصفِ لیست هنوز دست‌نخورده مانده. | ZWNJ, length, vocab |
| 4 | It's not laziness. It's a bias so reliable it has a name: the planning fallacy. | — | این تنبلی نیست. این یک سوگیریِ آنقدر قابل اعتماد است که اسمی هم دارد: مغالطه‌ی برنامه‌ریزی. | این تنبلی نیست. این یک سوگیریِ آنقدر قابل اعتماد است که اسمی هم دارد: مغالطه‌ی برنامه‌ریزی. | ... مغالطه‌ی برنامه‌ریزی. | Semantic: reliable → قابل اعتماد wrong sense, planning fallacy → مغالطه (logical fallacy) wrong, should be خطا; also long 91 chars | Curated: تنبلی نیست. یک سوگیریِ همیشگی است که اسم هم دارد: خطای برنامه‌ریزی. | MyMemory semantics, vocab/glossary, length |
| 5 | When we plan, we imagine the best-case version of the task. | — | وقتی برنامه‌ریزی می‌کنیم، بهترین حالت ممکنِ انجام کار را تصور می‌کنیم. | ... | ... | Formal ما, okay but long, missing ZWNJ? | Curated: وقتی برنامه می‌ریزیم، بهترین حالتِ ممکن را تصور می‌کنیم. | length, formality |
| 6 | We picture ourselves focused, healthy, and uninterrupted. | — | ما خودمان را متمرکز، سالم و بی‌وقفه تصور می‌کنیم. | ... بی‌وقفه ... | ... | بی‌وقفه = non-stop, wrong for uninterrupted (should be بدون وقفه/مزاحمت) | Curated: خودمان را متمرکز، سرحال و بدونِ وقفه تصور می‌کنیم. | MyMemory semantics, vocab |
| 7 | We forget the interruptions, because they are not part of the story we tell. | — | ما وقفه‌ها را فراموش می‌کنیم، چون آنها بخشی از داستانی که تعریف می‌کنیم نیستند. | ... | ... | Formal, آنها redundant, long | Curated: وقفه‌ها را فراموش می‌کنیم، چون در داستانی که برای خودمان می‌سازیم جایی ندارند. | length, vocab |
| 8 | Think about the last essay, tax form, or apartment move. | — | به آخرین انشا، فرم مالیات یا نقل مکان به آپارتمان فکر کنید. | ... | ... | Formal فکر کنید, انشا = school essay, better مقاله/اظهارنامه, long | Curated: به آخرین مقاله، اظهارنامه یا اسباب‌کشی فکر کن. | formality, vocab |
| 9 | Your guess before, and the real time after. Notice the gap. | — | حدس شما قبل و زمان واقعی بعد از آن. به فاصله بین حدس‌ها توجه کنید. | ... | ... | Added بین حدس‌ها (hallucination), changes meaning, formal شما, two sentences forced | Curated: حدسِ قبل و زمانِ واقعیِ بعد. فاصله را ببین. | MyMemory semantics, hallucination, length |
| 10 | The gap is rarely small, and it almost always points the same way. | — | این شکاف به ندرت کوچک است، و تقریباً همیشه به یک سمت اشاره دارد. | ... | ... | Literal points → اشاره دارد, idiom wrong, should be یک‌طرفه است | Curated: این فاصله معمولاً کم نیست و تقریباً همیشه یک‌طرفه است. | MyMemory idiom, semantics |
| 11 | Try this: before you estimate, ask how long similar tasks took last time. | — | این را امتحان کنید: قبل از اینکه تخمین بزنید، بپرسید دفعه قبل کارهای مشابه چقدر طول کشیده است. | ... | ... | Formal, 94 chars too long, بپرسید formal | Curated: این را امتحان کن: قبل از تخمین، بپرس دفعه‌ی قبل کارهای مشابه چقدر طول کشید. | length, formality |
| 12 | Use that number, not the hopeful one. | — | از آن عدد استفاده کنید، نه از عدد امیدوارکننده. | ... | ... | Formal, امیدوارکننده unnatural, should be امیدوارانه | Curated: همان عدد را مبنا بگذار، نه عددِ امیدوارانه را. | vocab, formality |
| 13 | Then add the interruptions you already know will come. | — | سپس وقفه‌هایی را که می‌دانید پیش خواهد آمد، اضافه کنید. | ... | ... | Formal, future خواهد آمد vs می‌آید, long | Curated: بعد وقفه‌هایی را که می‌دانی پیش می‌آید اضافه کن. | formality, tense |
| 14 | So: your gut plans the movie version. Your history knows the real one. | So: your intuition plans the ideal version. Your history knows the real one. | بنابراین: شهود شما نسخه ایده‌آل را برنامه‌ریزی می‌کند. تاریخچه شما نسخه واقعی را می‌داند. | بنابراین: شهود شما نسخه ایده‌آل را برنامه‌ریزی می‌کند. تاریخچه شما نسخه واقعی را می‌داند. | ... | Gloss simplified gut→intuition, movie→ideal, but MyMemory still literal: تاریخچه = history as in past, not personal experience; شهود شما formal; should be حست/تجربه‌ات; also 2 sentences | Curated: حست نسخه‌ی ایده‌آل را می‌چیند، تجربه‌ات نسخه‌ی واقعی را می‌داند. | MyMemory semantics, idiom, glossary (gut, movie version) |
| 15 | Estimate from history, not from hope, and pad for the interruptions you already know about. | — | از روی تاریخ تخمین بزنید، نه از روی امید، و وقفه‌هایی را که از قبل می‌دانید، پوشش دهید. | ... | ... | تاریخ ambiguous, pad → پوشش دهید literal (cover) wrong, should be جا بگذار; formal | Curated: بر اساسِ تجربه تخمین بزن، نه امید، و برای وقفه‌های قابل پیش‌بینی جا بگذار. | MyMemory semantics, vocab, idiom |
| 16 | What did you finish on time this month? Anything? | — | این ماه چه کاری را به موقع تمام کردی؟ هر کاری؟ | این ماه چه کاری را به موقع تمام کردی؟ هر کاری؟ | ... هر کاری؟ at end with ؟ misplaced? | Duplicate هر کاری, informal but awkward; question mark handling | Curated: این ماه چه کاری را سر وقت تمام کردی؟ چیزی بود؟ | punctuation, vocab, naturalness |

## QA false positive analysis
- QA checked persian_ratio >=0.7 (passed, all lines Persian), placeholder, identical, latin words <=2, length < 2*max, ratio 0.35-2.6 — all passed because MyMemory output is Persian, just low quality.
- No check for: glossary consistency (planning fallacy → مغالطه vs خطا), idiom literalness (gut→شهود, movie version, pad→پوشش), formality (شما vs تو), ZWNJ, Yeh/Kaf normalization, bidi controls, terminology conflicts, provenance (mymemory-only should be blocking for auto-publish), curated_coverage, hash match.
- Score 92 because only warnings about length and CTA repetition, no blocking_errors.

## Renderer issues observed in preview images
- Old renderer: PIL ImageFont without raqm, direction="rtl" alone does not shape Arabic; presentation forms needed but font has them, yet shaping not applied → broken joining.
- Punctuation: English ? left as ? not ؟, and appears on wrong side due to bidi.
- Wrapping: wrap before shaping → breaks in middle of shaped word, causes disconnected letters.
- Digits: Latin digits not converted, mixed LTR/RTL numbers.
- Safe zones: not enforced? QA layout passed but visually crowded.

## Suggested fixes (implemented in this PR)
- Curated FA catalog with stable hash (en_hash) for all 347 calendar lines, versioned, 100% coverage test.
- Glossary v1 with 40+ terms, allowlist_latin, used by producer and QA.
- Trend templates curated bilingual with {short} placeholder, fail-closed if short not in glossary.
- Producer: calendar → curated only, trend → curated-trend only, mymemory blocked for auto-publish (QA blocking error).
- QA supervisor: new persian_translation block with approved, provenance, curated_coverage, latin_leakage, bidi_errors, terminology_conflicts, blocking_errors; gates: provenance, hash match, fixture ban, mymemory-only ban, Latin leak, Yeh/Kaf, bidi controls, punctuation, safe zone, duplicate, glossary consistency.
- Renderer: RTL direction with raqm, arabic_reshaper + python-bidi, wrap before shaping fixed to wrap after logical but before visual? Actually wrap before shaping breaks joining → should wrap after shaping or use logical wrap with reshaper; implement safe zones, punctuation fix, question mark, digits, mixed spans, font fallback, spacing, alignment.
- Quarantine: content/quarantine.json denylist, buffer_publish.py checks it, editorial_memory status updated to translation-rejected/qa-failed.
- Idempotency: rerun produces same hash, quarantine cannot be overridden by AUTO_PUBLISH.

## Idempotency guarantee
Rerunning pipeline for same date/content_id with same catalog version produces identical script_hash (en_hash stable) and same FA. Quarantine prevents any future Buffer queue for this id/date even if AUTO_PUBLISH_ENABLED=true.

