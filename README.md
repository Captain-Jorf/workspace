# metacognition.hq — Reel 01: "Why metacognition?"

Instagram reel (1080×1920, 30 fps, ~96 s) + poster for the first post of
**@metacognition.hq**. English voice-over with word-by-word karaoke captions
(top, LTR) and clean Persian RTL subtitles (bottom).

## Pipeline
- `content/script.json` — narration lines (EN), subtitle lines (FA), scene map.
- `build/timing.py` — concatenates TTS chunks (`audio/c01..c10.mp3`) into
  `audio/full.wav` and builds the word-level timing map (`audio/timing.json`).
- `build/logo_make.py` — procedural vector rebuild of the brand emblem
  (marble ring + gold dendrites + watcher eye).
- `build/engine.py` — design system: palette, fonts, concept-web graph,
  karaoke + Persian subtitle pre-render.
- `build/scenes.py` — the nine animated scene foregrounds
  (hook, paper, word, halves, trap, methods, loop, hq, cta).
- `build/render.py` — frame loop → H.264/AAC mp4.
- `build/poster.py` — reel cover (1080×1920 + 4:5 crop).
- `build/fetch_fonts.sh` — downloads Vazirmatn + Sora and converts to TTF.

## Reproduce
    ./build/fetch_fonts.sh
    python3 build/logo_make.py
    python3 build/timing.py        # needs audio/c01..c10.mp3 (TTS)
    python3 build/render.py        # -> output/reel_metacognition_hq.mp4
    python3 build/poster.py        # -> output/poster_metacognition_hq.png

Large binaries (video, posters, TTS wav, fonts, hero renders) are kept out of
git — see `.gitignore`.
