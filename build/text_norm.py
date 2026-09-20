"""Single-sourced deterministic text normalization + production-font glyph coverage.

Issue #32 (run 35488263806, reel-2026-09-24): the burned-in subtitles showed
replacement boxes ("stand\ufffdup", "chat\ufffdAI") because the Groq script carried
Unicode non-breaking hyphens (U+2011) and related smart punctuation, and the
production subtitle font — Sora, fetched as the *latin subset* woff2 by
build/fetch_fonts.sh — has NO glyph for U+2010/U+2011/U+2012 (verified from the
font's actual cmap below). FreeType/Pillow then rendered the font's .notdef
(tofu) box for every such character. There is no OS font fallback on the CI
renderer, and none may be relied upon.

This module is the ONE normalization layer used before:
  * TTS input            (content_producer.spoken_form)
  * word timing          (timing.py display tokens)
  * subtitle wrapping    (reel_engine._build_captions)
  * burned-in rendering  (reel_engine.text_img / gold_text callers)
  * on-screen labels     (reel_engine compositions)
  * posters              (poster_auto.build)
  * captions             (content_producer caption assembly, caption.py)

Rules (deterministic, meaning-preserving, idempotent):
  * U+FFFD REPLACEMENT CHARACTER is ALWAYS a blocker. It is never silently
    rewritten — it means data was already corrupted upstream, and it must
    never reach TTS/render (fail closed before any media exists).
  * zero-width and directional-formatting characters are REMOVED
    (U+200B U+200C U+200D U+2060 U+FEFF U+00AD U+200E U+200F U+180E).
  * NBSP U+00A0, NNBSP U+202F and THIN SPACE U+2009 become ordinary spaces
    (identical meaning in spoken/display text; U+202F has no Sora glyph).
  * HYPHEN U+2010, NON-BREAKING HYPHEN U+2011, FIGURE DASH U+2012 and
    MINUS SIGN U+2212 become ASCII HYPHEN-MINUS '-' everywhere (word-internal
    compounds keep the hyphen: stand-up, chat-AI, AI-generated,
    pause-and-reflect — nothing is deleted).
  * EN DASH U+2013 / EM DASH U+2014: word-INTERNAL (letter/digit on both
    sides) → '-' ; otherwise kept when the production font covers them, else
    '-' (they are sentence punctuation, never deleted).
  * curly single quotes U+2018 U+2019 U+201A U+201B, modifier apostrophe
    U+02BC → ASCII apostrophe "'" (straight apostrophe — supported by every
    production font weight, and the form the contraction markers use).
  * curly double quotes U+201C U+201D U+201E, guillemets U+00AB U+00BB →
    ASCII double quote '"' (meaning unchanged).
  * every other character passes through UNTOUCHED (emoji included — captions
    keep their icons); the glyph gate below then verifies coverage.

Glyph coverage is checked against the ACTUAL production font cmap
(fontTools, en-400..en-800 — the intersection of every weight the renderer
and posters can select), never a guessed ASCII allowlist. No operating-system
font fallback is ever assumed.
"""
import os
import re

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FONT_DIR = os.path.join(ROOT, "assets", "fonts")

# The renderer/posters select en-400..en-800 via reel_engine.font(); coverage
# must hold for EVERY weight a frame can use → intersect all five cmaps.
PRODUCTION_FONT_WEIGHTS = (400, 500, 600, 700, 800)

REPLACEMENT_CHAR = "\ufffd"

# --- characters removed entirely (zero-width / directional formatting) ----
ZERO_WIDTH = {
    "\u200b",  # ZERO WIDTH SPACE
    "\u200c",  # ZERO WIDTH NON-JOINER
    "\u200d",  # ZERO WIDTH JOINER
    "\u2060",  # WORD JOINER
    "\ufeff",  # ZERO WIDTH NO-BREAK SPACE / BOM
    "\u00ad",  # SOFT HYPHEN
    "\u200e",  # LEFT-TO-RIGHT MARK
    "\u200f",  # RIGHT-TO-LEFT MARK
    "\u180e",  # MONGOLIAN VOWEL SEPARATOR
    "\u034f",  # COMBINING GRAPHEME JOINER
}

# --- space variants → ordinary space ---------------------------------------
SPACE_VARIANTS = {
    "\u00a0": " ",   # NO-BREAK SPACE
    "\u202f": " ",   # NARROW NO-BREAK SPACE (no Sora glyph)
    "\u2009": " ",   # THIN SPACE
    "\u200a": " ",   # HAIR SPACE
    "\u2007": " ",   # FIGURE SPACE
    "\u2008": " ",   # PUNCTUATION SPACE
    "\u205f": " ",   # MEDIUM MATHEMATICAL SPACE
}

# --- dash family ------------------------------------------------------------
# Always mapped to ASCII hyphen-minus (U+2010/U+2011/U+2012 have no glyph in
# the production font; U+2212 normalized for consistency):
DASH_TO_HYPHEN = {
    "\u2010": "-",   # HYPHEN
    "\u2011": "-",   # NON-BREAKING HYPHEN  ← the reel-2026-09-24 tofu
    "\u2012": "-",   # FIGURE DASH
    "\u2212": "-",   # MINUS SIGN
    "\ufe58": "-",   # SMALL EM DASH
    "\ufe63": "-",   # SMALL HYPHEN-MINUS
    "\uff0d": "-",   # FULLWIDTH HYPHEN-MINUS
}
# EN/EM dash: word-internal → '-', otherwise kept if the font covers them.
CONTEXTUAL_DASHES = {"\u2013", "\u2014"}

# --- quotes → supported, meaning-preserving ASCII forms ---------------------
QUOTE_MAP = {
    "\u2018": "'",   # LEFT SINGLE QUOTATION MARK
    "\u2019": "'",   # RIGHT SINGLE QUOTATION MARK (curly apostrophe)
    "\u201a": "'",   # SINGLE LOW-9 QUOTATION MARK
    "\u201b": "'",   # SINGLE HIGH-REVERSED-9 QUOTATION MARK
    "\u02bc": "'",   # MODIFIER LETTER APOSTROPHE
    "\uff07": "'",   # FULLWIDTH APOSTROPHE
    "\u201c": '"',   # LEFT DOUBLE QUOTATION MARK
    "\u201d": '"',   # RIGHT DOUBLE QUOTATION MARK
    "\u201e": '"',   # DOUBLE LOW-9 QUOTATION MARK
    "\u00ab": '"',   # LEFT-POINTING DOUBLE ANGLE QUOTATION MARK
    "\u00bb": '"',   # RIGHT-POINTING DOUBLE ANGLE QUOTATION MARK
}

_WORD_CHAR_RE = re.compile(r"[0-9A-Za-z]")


def contains_replacement_char(s):
    """True when U+FFFD is present — ALWAYS a blocker, never rewritten."""
    return REPLACEMENT_CHAR in (s or "")


def _word_internal(i, s):
    """Dash at index i is word-internal: letter/digit on both sides."""
    return (i > 0 and i + 1 < len(s)
            and _WORD_CHAR_RE.match(s[i - 1]) and _WORD_CHAR_RE.match(s[i + 1]))


def normalize_text(s, coverage=None):
    """Deterministic, idempotent, meaning-preserving normalization of ONE
    display/spoken string. `coverage` (an iterable of supported codepoints)
    is only consulted for the contextual EN/EM dash decision; when omitted
    the production font coverage is loaded (missing fonts degrade safely:
    a spaced EN/EM dash becomes '-' rather than risking an unsupported glyph).
    """
    if not s:
        return s or ""
    # pass 1: zero-width removals + unconditional 1:1 mappings
    buf = []
    for ch in s:
        if ch in ZERO_WIDTH:
            continue
        if ch in SPACE_VARIANTS:
            buf.append(SPACE_VARIANTS[ch])
        elif ch in DASH_TO_HYPHEN:
            buf.append("-")
        elif ch in QUOTE_MAP:
            buf.append(QUOTE_MAP[ch])
        else:
            buf.append(ch)
    t = "".join(buf)
    # pass 2: contextual EN/EM dashes with true lookahead/lookbehind
    if any(ch in CONTEXTUAL_DASHES for ch in t):
        cov = None
        if coverage is not None:
            cov = set(coverage)
        else:
            try:
                cov = supported_codepoints()
            except FileNotFoundError:
                cov = set()  # no font → keep nothing risky
        out = []
        for i, ch in enumerate(t):
            if ch in CONTEXTUAL_DASHES and (_word_internal(i, t) or ord(ch) not in cov):
                out.append("-")
            else:
                out.append(ch)
        t = "".join(out)
    # collapse doubled spaces created by removals; never touch line structure
    return re.sub(r" {2,}", " ", t)


def _contextual_word_internal(text):
    """Re-scan helper used by tests: positions of contextual dashes that are
    word-internal in `text`."""
    return [i for i, ch in enumerate(text) if ch in CONTEXTUAL_DASHES and _word_internal(i, text)]


# ---------------------------------------------------------------------------
# Production font glyph coverage — the ACTUAL cmap of the fetched fonts,
# never a guessed allowlist, never OS fallback.
# ---------------------------------------------------------------------------
_COVERAGE_CACHE = {}


def font_path(weight):
    return os.path.join(FONT_DIR, f"en-{weight}.ttf")


def _cmap_of(weight):
    """cmap (dict codepoint→glyphname) of ONE production font file."""
    path = font_path(weight)
    if not os.path.exists(path):
        raise FileNotFoundError(
            f"production font missing: {path} — run build/fetch_fonts.sh; "
            "glyph coverage cannot be verified and rendering must not start")
    from fontTools.ttLib import TTFont
    f = TTFont(path, fontNumber=0, lazy=True)
    try:
        cmap = f.getBestCmap() or {}
    finally:
        f.close()
    return set(cmap)


def supported_codepoints(weights=PRODUCTION_FONT_WEIGHTS):
    """Intersection of the cmaps of every production font weight a frame can
    use. A character is renderable ONLY if every selectable weight has it."""
    key = tuple(sorted(weights))
    if key not in _COVERAGE_CACHE:
        common = None
        for w in key:
            cmap = _cmap_of(w)
            common = cmap if common is None else (common & cmap)
        _COVERAGE_CACHE[key] = frozenset(common or ())
    return _COVERAGE_CACHE[key]


#: Characters that never need a glyph (whitespace control, never inked).
INVISIBLE_OK = {0x09, 0x0A, 0x0D, 0x20}


def unsupported_characters(s, weights=PRODUCTION_FONT_WEIGHTS):
    """Ordered, de-duplicated list of (char, 'U+XXXX') pairs in `s` that have
    no glyph in the production font(s). U+FFFD is always reported first-class
    by has_blockers(); it is also unsupported (the font has no glyph for it).
    """
    if not s:
        return []
    cov = supported_codepoints(weights)
    seen, out = set(), []
    for ch in s:
        cp = ord(ch)
        if cp in INVISIBLE_OK or cp in seen:
            continue
        if cp not in cov:
            seen.add(cp)
            out.append((ch, f"U+{cp:04X}"))
    return out


def has_blockers(s, weights=PRODUCTION_FONT_WEIGHTS):
    """Blocker strings for ONE piece of text: U+FFFD is always fatal; any
    other uncovered character is fatal too (after normalization nothing
    unsupported may remain — fail before TTS/render)."""
    blockers = []
    if contains_replacement_char(s):
        blockers.append("U+FFFD replacement character present — corrupted text "
                        "must never reach TTS/render")
    for ch, name in unsupported_characters(s, weights):
        if ch == REPLACEMENT_CHAR:
            continue
        blockers.append(f"character {name} ({ch!r}) has no glyph in the "
                        "production font after normalization")
    return blockers


# ---------------------------------------------------------------------------
# Script-level normalization + gates (single source for every stage)
# ---------------------------------------------------------------------------
def script_display_texts(script):
    """Every DISPLAYED string of a script: narration lines, scene tags,
    handle, caption hook/intro/section lines/CTAs, web labels, visual hint
    fields. These are exactly the strings TTS/timing/subtitles/labels/posters
    can render; QA and the renderer gate the SAME list."""
    texts = []
    for ch in (script or {}).get("chunks", []) or []:
        for en in ch.get("en", []) or []:
            texts.append(en.get("t", "") if isinstance(en, dict) else str(en))
    meta = (script or {}).get("meta", {}) or {}
    if meta.get("handle"):
        texts.append(str(meta["handle"]))
    for v in ((script or {}).get("scene_tags") or {}).values():
        texts.append(str(v))
    for v in (script or {}).get("web") or []:
        texts.append(str(v))
    cap = (script or {}).get("caption", {}) or {}
    for k in ("hook", "intro"):
        if cap.get(k):
            texts.append(str(cap[k]))
    for sec in cap.get("sections", []) or []:
        texts.append(str(sec.get("title", "")))
        texts += [str(x) for x in sec.get("lines", []) or []]
    for c in cap.get("ctas", []) or []:
        texts.append(str(c))
    return texts


def normalize_script(script):
    """Normalize a script IN PLACE (idempotent) and return the list of
    changed display strings (for audit). TTS forms are rebuilt from the
    normalized lines so spoken/display text derives from the SAME normalized
    representation. U+FFFD is never rewritten — the glyph gate blocks it."""
    changed = []

    def norm(s):
        n = normalize_text(s)
        if n != s:
            changed.append(s)
        return n

    import common  # local import: pronunciation overrides live in the policy
    pol = common.policy()
    overrides = (pol.get("tts", {}) or {}).get("pronunciation_overrides", {})
    for ch in (script or {}).get("chunks", []) or []:
        for en in ch.get("en", []) or []:
            if isinstance(en, dict):
                en["t"] = norm(en.get("t", ""))
        if "tts_text" in ch:
            # rebuild from the normalized lines → TTS == display source
            lines = [en.get("t", "") if isinstance(en, dict) else str(en)
                     for en in ch.get("en", []) or []]
            spoken = " ".join(_spoken_form(l, overrides) for l in lines if l)
            spoken = normalize_text(spoken)
            if spoken != ch.get("tts_text"):
                changed.append(ch.get("tts_text") or "")
            ch["tts_text"] = spoken
    meta = script.get("meta", {}) or {}
    if meta.get("handle"):
        meta["handle"] = norm(str(meta["handle"]))
    tags = script.get("scene_tags")
    if isinstance(tags, dict):
        for k in list(tags):
            tags[k] = norm(str(tags[k]))
    if script.get("web"):
        script["web"] = [norm(str(v)) for v in script["web"]]
    cap = script.get("caption")
    if isinstance(cap, dict):
        for k in ("hook", "intro"):
            if cap.get(k):
                cap[k] = norm(str(cap[k]))
        for sec in cap.get("sections", []) or []:
            if sec.get("title"):
                sec["title"] = norm(str(sec["title"]))
            sec["lines"] = [norm(str(x)) for x in sec.get("lines", []) or []]
        if cap.get("ctas"):
            cap["ctas"] = [norm(str(c)) for c in cap["ctas"]]
        # source labels keep their bibliographic form but get safe punctuation
        for s in cap.get("sources", []) or []:
            pass  # citations: unchanged (displayed only in the caption file)
    return changed


def _spoken_form(text, overrides):
    """Same spoken-form rule as content_producer.spoken_form (kept private
    here so TTS rebuild and producer share one behavior)."""
    s = text
    for k, v in sorted((overrides or {}).items(), key=lambda kv: -len(kv[0])):
        s = s.replace(k, v)
    s = re.sub(r"https?://\S+", "the link", s)
    return s


def glyph_gate_issues(script, extra_texts=()):
    """Deterministic glyph blocker list for the WHOLE visible text of a
    script (plus any extra render-bound strings, e.g. plan label sets).
    Empty list = every visible character has a glyph in the production font.
    Fail closed: an unreadable production font is itself a blocker."""
    try:
        supported_codepoints()
    except FileNotFoundError as e:
        return [str(e)]
    issues = []
    for t in script_display_texts(script):
        for b in has_blockers(t):
            issues.append(f"{b} — in: {(t or '')[:60]!r}")
    for t in extra_texts or ():
        for b in has_blockers(t):
            issues.append(f"{b} — in label/tag {t!r}")
    return issues


def normalization_ok_after(script):
    """True when a normalized script contains no U+FFFD anywhere in its
    display texts (the always-blocker check, independent of the font)."""
    return not any(contains_replacement_char(t) for t in script_display_texts(script))


# ---------------------------------------------------------------------------
# TTS / timing / display parity (single normalized representation)
# ---------------------------------------------------------------------------
def word_tokens(s):
    """The tokenization timing.py and the subtitle wrap share: split the
    normalized display text on whitespace."""
    return (s or "").split()


def parity_issues(script, timing):
    """Verify TTS/timing/display derive from ONE normalized representation:
    for every chunk, the timing word sequence must equal the normalized
    display words (punctuation-insensitive, same as timing._norm). A mismatch
    means some stage normalized on its own and the karaoke would drift."""
    import re as _re
    issues = []
    if not timing or not timing.get("chunks"):
        return issues

    def normtok(s):
        return [_re.sub(r"[^a-z0-9]+", "", w.lower()) for w in (s or "").split()]

    tchunks = {c.get("id") or f"c{i+1}": c for i, c in enumerate(timing.get("chunks", []))}
    for ch in (script or {}).get("chunks", []) or []:
        cid = ch.get("id")
        tc = tchunks.get(cid)
        if not tc:
            continue
        display = " ".join(en.get("t", "") if isinstance(en, dict) else str(en)
                           for en in ch.get("en", []) or [])
        want = [t for t in normtok(display) if t]
        got = []
        for ln in tc.get("lines", []) or []:
            for w in ln.get("words", []) or []:
                t = normtok(w.get("w", ""))
                got.extend(x for x in t if x)
        if want != got:
            issues.append(f"chunk {cid}: timing words diverge from the normalized "
                          f"display text ({len(got)} vs {len(want)} tokens)")
    return issues
