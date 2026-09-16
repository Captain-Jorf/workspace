"""Topic-aware, pillar-aware visual storyboarding + pre-render visual-semantic gate.

Issue #24 (run 35058904480, reel-2026-09-19): the first technically successful
English dry-run was rejected by human review because the rendered Reel showed a
generic animated code card (`def solve(): ... assert test(result)`) with a moving
cursor on a PRODUCT / user-interview topic, and leaned on the same two profile
images for the whole Reel. Root causes:

  * the static fallback appended a fixed "code visual" phrase to EVERY playbook's
    visual_direction, and the renderer picked templates by substring-matching
    that phrase ("code" in visual_direction);
  * the renderer had only three backgrounds (marble / hero_brain / hero_desk);
  * final QA measured subtitle contrast only — nothing inspected what was shown.

This module is the single source of truth for scene visuals:

  * PILLAR_CATEGORIES — the allowed visual categories per editorial pillar
    (code/terminal categories exist ONLY for the CODING pillar, and are only
    used when the scene narration genuinely justifies code);
  * build_visual_plan() — a deterministic scene plan (machine-readable metadata
    per scene: narration segment, topic keywords, pillar, visual category,
    purpose, asset + provenance, animation, duration, code/cursor justification);
  * visual_semantic_issues() — the deterministic PRE-RENDER visual-semantic gate
    that blocks, before TTS/render, every visual defect a human would reject:
    generic code on non-coding topics, unjustified cursors, off-pillar
    categories, repeated/dominant assets, missing purpose, scene/narration
    mismatch, broken provenance, unsafe URLs, insufficient variety, cold
    (blue/navy/cyan/purple) palette, text-heavy slides, subtitle obstruction.

The gate is a pure function of the plan + script: no LLM output, approval flag
or score can override it. The same function is re-run by the renderer on load
and by the final QA supervisor, so static fallback, safe re-render and every
recovery path obey the same rules.
"""
import hashlib
import json
import math
import os
import re
import unicodedata

import sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import common  # noqa: E402

# ---------------------------------------------------------------------------
# Brand palette — matte black / charcoal / warm metallic gold / amber / bronze /
# ivory. Blue, navy, cyan and purple are EXCLUDED by construction: every color
# this pipeline can emit is drawn from these warm entries, and brand_grade()
# re-grades any external photo onto the same warm ramp.
# ---------------------------------------------------------------------------
MATTE_BLACK = (14, 12, 10)
CHARCOAL = (32, 27, 22)
CARD_DARK = (26, 20, 13)
GOLD = (233, 180, 74)
GOLD_HI = (255, 228, 158)
GOLD_LO = (122, 88, 32)
AMBER = (255, 186, 90)
BRONZE = (150, 106, 44)
IVORY = (246, 240, 228)
WARM_DIM = (168, 148, 116)

BRAND_PALETTE = {
    "matte_black": MATTE_BLACK, "charcoal": CHARCOAL, "card_dark": CARD_DARK,
    "gold": GOLD, "gold_hi": GOLD_HI, "gold_lo": GOLD_LO, "amber": AMBER,
    "bronze": BRONZE, "ivory": IVORY, "warm_dim": WARM_DIM,
}

# Forbidden hue families (issue #24): blue, navy, cyan, purple — and cold
# corporate gradients built from them.
COLD_COLOR_NAMES = ("blue", "navy", "cyan", "purple")

# Subtitle band geometry (from editorial_policy layout): en_top .. en_top+3*row_h
SUBTITLE_BAND_MARGIN = 20
SCENE_ZONE_TOP = 460          # first y a scene composition may occupy
SCENE_ZONE_BOTTOM = 1320      # last y a scene composition may occupy


def _layout(pol):
    return (pol or common.policy()).get("layout", {})


def subtitle_band(pol):
    L = _layout(pol)
    top = int(L.get("en_top", 200))
    bottom = int(top) + int(L.get("en_max_rows", 3)) * int(L.get("en_row_height", 78))
    return (top - SUBTITLE_BAND_MARGIN, bottom + SUBTITLE_BAND_MARGIN)


def _is_cold(rgb):
    r, g, b = (int(rgb[0]), int(rgb[1]), int(rgb[2]))
    return b > r + 6 and b >= g - 6


def assert_no_cold_colors(name, rgb):
    """Fail closed if a brand color is cold (blue/navy/cyan/purple)."""
    if _is_cold(rgb):
        raise ValueError(f"brand color {name}={tuple(rgb)} is cold (blue/cyan/purple) — "
                         "profile palette is matte black / charcoal / gold / amber / bronze / ivory only")
    return rgb


for _n, _c in list(BRAND_PALETTE.items()):
    assert_no_cold_colors(_n, _c)

# ---------------------------------------------------------------------------
# Visual category registry.
#   comp            — renderer composition (implemented in reel_engine)
#   keywords        — topic terms that make the category relevant (relevance
#                     scoring for scene assignment; never a prompt)
#   purpose         — the machine-readable visual purpose of a scene
#   label_sets      — on-screen label sets (deterministic, short, warm palette)
#   content_top/bottom — composition bounding box (the gate enforces it stays
#                     clear of the subtitle band and the handle area)
#   code            — True only for code/terminal categories
#   photo_ok        — a brand-graded photo may be the primary visual
# ---------------------------------------------------------------------------
C = {}


ALL_CONTENT_BEATS = ("problem", "explain", "example", "technique")


def _cat(name, comp, keywords, purpose, label_sets, code=False, photo_ok=False,
         content_top=SCENE_ZONE_TOP, content_bottom=SCENE_ZONE_BOTTOM,
         animation="draw", beats=ALL_CONTENT_BEATS):
    assert_no_cold_colors(name, GOLD)
    C[name] = {
        "comp": comp, "keywords": [k.lower() for k in keywords], "purpose": purpose,
        "label_sets": [list(ls) for ls in label_sets], "code": bool(code),
        "photo_ok": bool(photo_ok), "content_top": content_top,
        "content_bottom": content_bottom, "animation": animation,
        "beats": tuple(beats),
    }


# brand moments (profile assets used ONLY as brand references)
_cat("brand-mark", "brand_mark", ["open", "hook"],
     "brand open — the emblem reveal over the dark gold field",
     [["METACOGNITION", "FOR THE AI AGE"]], animation="pulse",
     content_top=470, content_bottom=1500, beats=("hook",))
_cat("brand-close", "brand_close", ["your turn", "ending"],
     "brand close — the orbit and eye send the viewer to the question",
     [["YOUR TURN"]], animation="pulse", content_top=470, content_bottom=1500,
     beats=("ending",))

# --- PRODUCT / STARTUP -------------------------------------------------------
_cat("said-vs-did", "dual", ["said", "did", "interview", "behavior", "politeness",
                              "evidence", "observation", "yes", "sure"],
     "contrast what users said with what they did",
     [["WHAT USERS SAID", "WHAT USERS DID"], ["FEELS LIKE", "ACTUALLY"]], photo_ok=True,
     beats=("problem", "explain", "example"))
_cat("interview-notes", "notes", ["interview", "notes", "assumption", "question",
                                   "leading", "ask", "user"],
     "show interview notes with the flagged assumption highlighted",
     [["INTERVIEW NOTES", "ASSUMPTION", "HIGHLIGHTED"], ["ASK", "LISTEN", "LOG"]], photo_ok=True)
_cat("hypothesis-ladder", "ladder", ["hypothesis", "ladder", "step", "prove wrong",
                                      "expectation"],
     "trace a hypothesis toward the evidence that could refute it",
     [["HYPOTHESIS", "INTERVIEW", "OBSERVATION", "EVIDENCE", "DECISION"],
      ["ASSUME", "ASK", "WATCH", "CHECK", "DECIDE"]])
_cat("evidence-filter", "funnel", ["confirmation", "bias", "filter", "evidence",
                                    "contradict", "notice", "ignore"],
     "show confirmation bias filtering out contradictory evidence",
     [["EVERY ANSWER", "FITS THE STORY", "KEPT"], ["EVERY ANSWER", "CONTRADICTS", "DROPPED"]],
     beats=("problem", "explain", "example"))
_cat("decision-matrix", "matrix", ["decision", "matrix", "option", "trade-off", "weigh",
                                    "choose", "criteria"],
     "weigh the options against the real criteria",
     [["OPTIONS", "COST", "VALUE", "RISK", "NOW?"]],
     beats=("explain", "example", "technique"))
_cat("funnel", "funnel", ["funnel", "narrow", "signal", "noise", "priority"],
     "narrow the raw signal down to the decision",
     [["RAW SIGNAL", "SIGNAL", "DECISION"]], beats=("problem", "explain", "example"))
_cat("observation-log", "notes", ["observe", "log", "behavior", "watch", "session"],
     "log observed behavior against the expectation",
     [["EXPECTED", "OBSERVED", "GAP"]])
_cat("tradeoff-scale", "dual", ["trade-off", "tradeoff", "balance", "versus", "cost",
                                 "benefit", "sunk cost"],
     "balance the trade-off between the two sides",
     [["KEEP IT", "REBUILD IT"], ["YESTERDAY'S COST", "TOMORROW'S COST"]],
     beats=("problem", "explain", "example"))
_cat("prediction-graph", "graph", ["predict", "graph", "expectation", "result", "estimate",
                                    "planning fallacy", "gap"],
     "compare the prediction with the actual result",
     [["PREDICTION", "ACTUAL"]])

# --- AI_JUDGMENT -------------------------------------------------------------
_cat("confidence-gauge", "gauges", ["confidence", "calibration", "sure", "certainty",
                                     "calibrate", "trust"],
     "show the gap between felt confidence and actual correctness",
     [["FEELS LIKE", "ACTUALLY"], ["CONFIDENT", "CORRECT"]],
     beats=("problem", "explain"))
_cat("uncertainty-range", "bars", ["uncertain", "uncertainty", "range", "spread", "doubt"],
     "show the spread of what the answer could be",
     [["SURE", "POSSIBLE", "UNKNOWN"]])
_cat("verification-checklist", "ladder", ["verify", "check", "checklist", "docs",
                                           "test", "confirm"],
     "the verification steps before accepting an answer",
     [["ASK", "CHECK ONE SOURCE", "RUN IT", "ACCEPT"]])
_cat("evidence-card", "notes", ["evidence", "source", "tier", "citation", "grounding"],
     "show the evidence and its grounding",
     [["EVIDENCE", "SOURCED", "TIERED"]])
_cat("source-comparison", "dual", ["source", "compare", "comparison", "claim", "fact"],
     "compare the fluent claim with the grounded fact",
     [["THE CLAIM", "THE FACT"], ["SOUNDS LIKE", "IS"]],
     beats=("problem", "explain", "example"))
_cat("human-review-gate", "ladder", ["human review", "review", "approve", "gate", "sign off"],
     "mark the point where a human reviews the AI output",
     [["AI OUTPUT", "HUMAN CHECK", "SHIPPED"]])
_cat("hallucination-trap", "dual", ["hallucinat", "invented", "fluent", "plausible", "wrong"],
     "contrast the fluent invented answer with the grounded one",
     [["INVENTED, FLUENT", "ACTUALLY TRUE"]], beats=("problem", "explain", "example"))
_cat("human-ai-network", "loop", ["human", "ai", "collaborat", "loop", "handoff", "work with"],
     "show the human and the AI working in a loop",
     [["HUMAN", "AI", "CHECK", "AGREE"]],
     beats=("problem", "explain", "example", "technique"))

# --- CODING (code categories live ONLY here) ----------------------------------
_cat("code-scene", "code", ["code", "coding", "function", "signature", "autocomplete",
                             "ghost text", "tab"],
     "the code concept the narration is discussing",
     [["CODE", "CONCEPT"]], code=True)
_cat("debug-trace", "code", ["debug", "bug", "trace", "stuck", "rubber duck", "root cause"],
     "step through the failure path line by line",
     [["WATCH THE PATH"]], code=True)
_cat("test-suite", "code", ["test", "tests", "edge case", "assert", "suite"],
     "the tests that guard the behavior",
     [["EDGE CASE", "PASS"]], code=True)
_cat("system-diagram", "matrix", ["system", "architecture", "service", "component", "flow"],
     "show the parts and how they connect",
     [["PARTS", "CONNECTS", "BREAKS"]], beats=("explain", "example", "technique"))
_cat("failure-chain", "ladder", ["failure", "cascade", "incident", "cause", "effect",
                                  "chain"],
     "show the cause-and-effect failure chain",
     [["TRIGGER", "SPREAD", "INCIDENT", "LESSON"]])
_cat("stack-trace", "code", ["stack trace", "traceback", "exception", "error message",
                              "crash"],
     "read the traceback where the real error hides",
     [["TRACEBACK", "ROOT CAUSE"]], code=True)

# --- TECHNICAL_LEARNING (LEARNING_TECH) ----------------------------------------
_cat("memory-map", "matrix", ["memory", "recall", "remember", "forget", "store", "offload"],
     "map where the skill lives in memory",
     [["RECALL", "RECOGNIZE", "FORGET"]], beats=("explain", "example", "technique"))
_cat("concept-map", "web", ["concept", "map", "connect", "idea", "link"],
     "connect the concepts that make the skill",
     [["CONNECT", "IDEAS"]])
_cat("retrieval-loop", "loop", ["retrieval", "recall", "close the book", "rebuild", "peek"],
     "show the recall-check-reinforce loop",
     [["CLOSE", "RECALL", "CHECK", "REINFORCE"]], beats=("explain", "example", "technique"))
_cat("worked-example", "notes", ["example", "worked", "step", "walkthrough", "tutorial"],
     "one worked example, step by step",
     [["STEP 1", "STEP 2", "DONE"]])
_cat("skill-progression", "ladder", ["skill", "progress", "practice", "level", "drill"],
     "show the skill progressing with practice",
     [["STUCK", "HELPED", "SOLID", "OWN"]])

# --- DIGITAL_ATTENTION -----------------------------------------------------------
_cat("notification-cascade", "notifications", ["notification", "alert", "ping", "slack",
                                                "message", "badge"],
     "show the notifications cascading into focus loss",
     [["PING", "PING", "FOCUS LOST"]], beats=("problem", "example"))
_cat("focus-meter", "gauges", ["focus", "concentration", "deep work", "interrupted"],
     "show focus dropping under interruption",
     [["FOCUS", "AFTER SWITCH"]], beats=("problem", "explain"))
_cat("task-queue", "bars", ["task", "queue", "switch", "context", "batch"],
     "show the tasks waiting while attention switches",
     [["TASK", "QUEUED", "WAITING"]])
_cat("attention-residue", "bars", ["residue", "left behind", "minutes", "return", "reload"],
     "show the attention left behind after each switch",
     [["TASK A", "RESIDUE", "TASK B"]])
_cat("context-timeline", "ladder", ["context", "timeline", "day", "window", "batch"],
     "lay the context switches along the day",
     [["9:00", "11:00", "14:00", "17:00"]])

# --- HUMAN_AI_COLLABORATION --------------------------------------------------------
_cat("handoff-chain", "ladder", ["handoff", "hand off", "pass", "relay", "next"],
     "show the handoffs between human and AI",
     [["YOU", "AI", "YOU", "SHIPPED"]])
_cat("verification-gate", "ladder", ["verify", "gate", "before ship", "approve"],
     "the verification gate before the handoff ships",
     [["DRAFT", "VERIFY", "APPROVE", "SHIP"]])
_cat("role-complement", "dual", ["complement", "roles", "strength", "each other"],
     "show the complementary roles side by side",
     [["HUMAN: JUDGE", "AI: DRAFT"]], beats=("problem", "explain", "example"))
_cat("approval-gate", "ladder", ["approval", "sign-off", "owner", "final"],
     "mark the approval gate the AI never crosses alone",
     [["AI PROPOSAL", "HUMAN APPROVES", "DONE"]])
_cat("shared-decision-loop", "loop", ["shared", "decision", "loop", "iterate", "together"],
     "show the shared human-AI decision loop",
     [["PROPOSE", "CHECK", "DECIDE", "REPEAT"]], beats=("explain", "example", "technique"))

# --- shared neutral compositions -----------------------------------------------------
_cat("concept-web", "web", ["concept", "network", "web", "idea"],
     "map the concept web for the topic",
     [["IDEA", "LINK", "IDEA"]])

CODE_CATEGORIES = frozenset(name for name, spec in C.items() if spec["code"])

# ---------------------------------------------------------------------------
# Pillar → allowed categories (issue #24 §5). Code/terminal categories are
# allowed ONLY under CODING; everything else is a hard pre-render block.
# ---------------------------------------------------------------------------
PILLAR_CATEGORIES = {
    "AI_JUDGMENT": ["confidence-gauge", "uncertainty-range", "verification-checklist",
                    "evidence-card", "source-comparison", "human-review-gate",
                    "hallucination-trap", "prediction-graph", "concept-web",
                    "human-ai-network"],
    "CODING": ["debug-trace", "test-suite", "code-scene", "stack-trace",
               "system-diagram", "failure-chain", "confidence-gauge", "concept-web"],
    "LEARNING_TECH": ["retrieval-loop", "memory-map", "concept-map", "worked-example",
                      "skill-progression", "confidence-gauge", "concept-web"],
    "PRODUCT": ["said-vs-did", "interview-notes", "hypothesis-ladder", "evidence-filter",
                "decision-matrix", "funnel", "observation-log", "tradeoff-scale",
                "prediction-graph", "concept-web"],
    "ATTENTION": ["notification-cascade", "focus-meter", "task-queue",
                  "attention-residue", "context-timeline", "prediction-graph",
                  "concept-web"],
    "HUMAN_AI": ["handoff-chain", "verification-gate", "role-complement",
                 "approval-gate", "shared-decision-loop", "human-ai-network",
                 "concept-web"],
}

BRAND_CATEGORIES = ("brand-mark", "brand-close")
PILLAR_KEYWORDS = {
    "AI_JUDGMENT": ["ai", "judge", "confidence", "calibration", "trust", "verify"],
    "CODING": ["code", "coding", "debug", "test", "trace", "function"],
    "LEARNING_TECH": ["learn", "memory", "recall", "skill", "tutorial", "practice"],
    "PRODUCT": ["product", "user", "interview", "research", "decision", "metric",
                "hypothesis", "estimate"],
    "ATTENTION": ["attention", "focus", "notification", "context", "interrupt"],
    "HUMAN_AI": ["human", "ai", "collaborat", "handoff", "together"],
}


def allowed_categories(pillar):
    return list(PILLAR_CATEGORIES.get(pillar, PILLAR_CATEGORIES["AI_JUDGMENT"])) + list(BRAND_CATEGORIES)


def derive_pillar(meta, pol=None):
    """Pillar of a script: meta.pillar when it is a real editorial pillar,
    otherwise a deterministic keyword match on the technology angle / concept
    (never an LLM claim)."""
    pol = pol or common.policy()
    pillars = list(pol.get("pillars", {}))
    p = str((meta or {}).get("pillar") or "").strip()
    if p in pillars:
        return p
    text = " ".join(str((meta or {}).get(k) or "") for k in
                    ("technology_angle", "metacognition_concept", "topic")).lower()
    for cand in pillars:
        if any(kw in text for kw in PILLAR_KEYWORDS.get(cand, [])):
            return cand
    return "AI_JUDGMENT"  # brand core: metacognition for the AI age

# ---------------------------------------------------------------------------
# Code + cursor justification (issue #24 §6). Code is allowed ONLY when the
# pillar is a coding pillar AND the spoken segment itself genuinely discusses
# coding/debugging/testing/API behavior. A cursor (typing caret) is allowed
# ONLY when the narration explicitly demonstrates code entry.
# ---------------------------------------------------------------------------
CODE_JUSTIFY_RE = re.compile(
    r"\b(code|coding|debug|debugging|debugger|terminal|command line|stack trace|"
    r"traceback|unit test|test suite|tests? (that )?(pass|fail|run|it)|run the code|"
    r"api call|function signature|compile|compiled|runtime error|exception|breakpoint|"
    r"syntax|refactor|autocomplete|ghost text)\b", re.IGNORECASE)

CURSOR_JUSTIFY_RE = re.compile(r"\b(typ|typing|typed|type it|keyboard)\b", re.IGNORECASE)


def code_justified(pillar, narration):
    if pillar not in ("CODING", "LEARNING_TECH"):
        return False
    return bool(CODE_JUSTIFY_RE.search(narration or ""))


def cursor_justified(pillar, narration):
    return code_justified(pillar, narration) and bool(CURSOR_JUSTIFY_RE.search(narration or ""))

# ---------------------------------------------------------------------------
# Curated code snippets (the ONLY code the renderer may ever show). Each is a
# small, syntactically coherent, concept-carrying Python fragment drawn from
# this repository's own playbooks — never a generic `def solve()` template,
# never an unexplained pseudo-function, never a meaningless bare assert.
# ---------------------------------------------------------------------------
CODE_SNIPPETS = {
    "debug": [
        "# the bug hides where you don't look",
        "def find_root_cause(hypotheses):",
        "    checked = []",
        "    for h in hypotheses:",
        "        if test(h):            # evidence, not intuition",
        "            checked.append(h)",
        "    return checked or 'look again'",
    ],
    "autocomplete": [
        "# recognition is not retrieval",
        "sig = recall('sha256')          # try from memory first",
        "if sig is None:",
        "    sig = lookup('sha256')      # suggestion as a check",
        "return sig",
    ],
    "test": [
        "def check_before_accept(result):",
        "    edge = one_edge_case(result)",
        "    reason = explain(result)    # you can say why",
        "    assert reason               # no unexplained trust",
        "    return edge",
    ],
    "trace": [
        "for frame in traceback.readlines():",
        "    show(frame)                 # read from the bottom up",
        "root = traceback.root_cause()   # the real error hides there",
    ],
}
CODE_SNIPPET_KEYS = ("debug", "autocomplete", "test", "trace")
BANNED_CODE_PATTERNS = (r"\bdef solve\b", r"\bincomplete\s*\(", r"\bai\.complete\s*\(",
                        r"\bassert test\(result\)\b")


def code_snippet_for(meta, narration):
    """Deterministic curated snippet for a justified code scene."""
    text = " ".join([str((meta or {}).get(k) or "") for k in
                     ("technology_angle", "metacognition_concept")] + [narration or ""]).lower()
    if any(k in text for k in ("debug", "bug", "trace", "stuck")):
        return list(CODE_SNIPPETS["debug"])
    if "autocomplete" in text or "ghost text" in text:
        return list(CODE_SNIPPETS["autocomplete"])
    if any(k in text for k in ("stack trace", "traceback", "exception")):
        return list(CODE_SNIPPETS["trace"])
    if any(k in text for k in ("test", "edge case", "accept")):
        return list(CODE_SNIPPETS["test"])
    return list(CODE_SNIPPETS["debug"])


def snippet_is_clean(lines):
    """No banned generic/pseudo-code pattern may survive into a rendered scene."""
    for ln in lines or []:
        for pat in BANNED_CODE_PATTERNS:
            if re.search(pat, ln or ""):
                return False
    return bool(lines)

# ---------------------------------------------------------------------------
# Topic keywords (deterministic, from the script meta — never from remote text)
# ---------------------------------------------------------------------------
STOP = common.STOP


def _content_words(text, limit=12):
    s = unicodedata.normalize("NFKD", (text or "").lower())
    s = re.sub(r"[^a-z0-9\s\-]", " ", s)
    out, seen = [], set()
    for t in s.split():
        t = t.strip("-")
        if len(t) < 4 or t in STOP or t.isdigit() or t in seen:
            continue
        seen.add(t)
        out.append(t)
        if len(out) >= limit:
            break
    return out


def topic_keywords(script):
    meta = (script or {}).get("meta", {}) or {}
    pillar = derive_pillar(meta)
    kws = _content_words(meta.get("technology_angle")) + _content_words(meta.get("metacognition_concept"))
    kws += PILLAR_KEYWORDS.get(pillar, [])
    out, seen = [], set()
    for k in kws:
        if k not in seen:
            seen.add(k)
            out.append(k)
    return out[:16]

# ---------------------------------------------------------------------------
# Narration duration estimates (same single-sourced rate as common.pre_render)
# ---------------------------------------------------------------------------


def _wps(pol):
    p = common.pre_render_params(pol)
    rate = common.parse_speech_rate(((pol or common.policy()).get("tts", {}) or {}).get("rate"))
    return max(0.5, p["wpm_base"] * rate / 60.0)


def _beat_lines(script):
    """Ordered list of (beat, [(text, n_words), ...]) from the script chunks."""
    beats = []
    for ch in (script or {}).get("chunks", []) or []:
        beat = ch.get("beat") or ch.get("scene") or "explain"
        lines = [(ln.get("t", "") if isinstance(ln, dict) else str(ln)) for ln in ch.get("en", []) or []]
        lines = [l for l in lines if l.strip()]
        if not lines:
            continue
        if beats and beats[-1][0] == beat:
            beats[-1][1].extend((l, common.word_count(l)) for l in lines)
        else:
            beats.append((beat, [(l, common.word_count(l)) for l in lines]))
    return beats


def _split_lines(lines, n):
    """Split [(text, words)] into n contiguous near-equal groups (by words)."""
    total = sum(w for _, w in lines) or 1
    groups, cur, acc = [], [], 0
    for i, (text, w) in enumerate(lines):
        cur.append((text, w))
        acc += w
        target = total * (len(groups) + 1) / n
        if len(groups) < n - 1 and acc >= target and i < len(lines) - 1:
            groups.append(cur)
            cur, acc = [], 0
    if cur:
        groups.append(cur)
    return groups


def _split_count(dur, first=7.0, second=13.0):
    if dur <= first:
        return 1
    if dur <= second:
        return 2
    return 3


def _skeleton(beats, wps, tight):
    sk = []
    for beat, lines in beats:
        dur = sum(w for _, w in lines) / wps
        # hook/ending are single brand moments — never split them (the brand
        # assets are references, and splitting would repeat the same asset)
        if beat in ("hook", "ending"):
            n = 1
        else:
            n = _split_count(dur, first=9.0 if tight else 7.0,
                             second=17.0 if tight else 13.0)
            if tight:
                n = min(n, 2)
        for g_i, group in enumerate(_split_lines(lines, n)):
            sk.append({"beat": beat, "split": g_i,
                       "narration": " ".join(t for t, _ in group),
                       "words": sum(w for _, w in group)})
    return sk

# ---------------------------------------------------------------------------
# Plan building
# ---------------------------------------------------------------------------
REPO_ASSETS = {
    "brain": "assets/img/hero_brain.png",
    "desk": "assets/img/hero_desk.png",
    "emblem": "assets/img/logo_emblem.png",
    "eye": "assets/img/logo_eye.png",
}


def _asset_repo(name, path):
    return {"kind": "repo", "id": f"repo:{path}", "path": path,
            "origin": "repository-assets", "license": "internal"}


def _asset_proc(category, comp, idx):
    return {"kind": "procedural", "id": f"proc-{category}-{idx}", "template": comp,
            "origin": "generated-locally", "license": "internal"}


def _score(category, narration_low, topic_kw):
    spec = C[category]
    s = 0.0
    for kw in spec["keywords"]:
        if kw in narration_low:
            s += 3.0
        elif any(kw in k for k in topic_kw if len(kw) >= 4):
            s += 1.0
    return s


def _pick_category(scene_narration, topic_kw, pillar, used, prev_cat, slot_pref,
                   beat="explain"):
    pool = PILLAR_CATEGORIES[pillar]
    narration_low = scene_narration.lower()
    # Stay consistent with the pre-render visual gate: a scene is accepted
    # when its narration overlaps the category/topic vocabulary OR the
    # category is a legitimate visual for that beat's narrative role. Prefer
    # categories whose beats include this beat so the plan always gate-passes
    # even on a zero-keyword match.
    in_beat = [c for c in pool if beat in C[c].get("beats", ())]
    search = in_beat if in_beat else list(pool)
    best, best_score = None, -1.0
    for cat in search:
        if cat == prev_cat:
            continue
        if used.get(cat, 0) >= 2:
            continue
        s = _score(cat, narration_low, topic_kw)
        if slot_pref and cat in slot_pref:
            s += 2.0
        # a category that fits the beat's role is a safe floor even at score 0
        if cat in in_beat:
            s += 0.5
        # reuse penalty: prefer a FRESH composition for every scene — variety
        # is the whole point of the visual plan (issue #24)
        s -= 1.5 * used.get(cat, 0)
        if s > best_score:
            best, best_score = cat, s
    if best is None:
        for cat in pool:
            if used.get(cat, 0) < 2 and cat != prev_cat:
                best = cat
                break
    if best is None:
        best = pool[0]
    return best


def _external_asset_for(scene_narration, topic_kw, allow_external):
    if not allow_external:
        return None
    try:
        import asset_fetch
    except Exception:
        return None
    if not asset_fetch.enabled():
        return None
    query = " ".join((topic_kw or [])[:4]) or scene_narration[:60]
    return asset_fetch.fetch_image(query)


def build_visual_plan(script, pol=None, allow_external=None):
    """Deterministic topic/pillar-aware scene plan for an approved script.

    Generated ONLY from the approved script + editorial policy (the caller runs
    this after Producer/Reviewer/Revision + the deterministic pre-render text
    QA). `allow_external` enables the $0 Openverse photo fetch (fail-soft:
    any problem falls back to the deterministic procedural visual).
    """
    pol = pol or common.policy()
    meta = (script or {}).get("meta", {}) or {}
    if allow_external is None:
        allow_external = os.environ.get("ASSET_FETCH", "0") == "1"
    pillar = derive_pillar(meta, pol)
    tkw = topic_keywords(script)
    wps = _wps(pol)
    band = subtitle_band(pol)

    beats = _beat_lines(script)
    # scene skeletons: split long beats so a visual change lands every ~3-6 s
    skeleton = _skeleton(beats, wps, tight=False)
    if len(skeleton) > 12:  # keep the reel art-directable: at most 12 scenes
        skeleton = _skeleton(beats, wps, tight=True)

    scenes = []
    used_cat = {}
    prev_cat = None
    n_img_brain = n_img_desk = 0
    for i, sk in enumerate(skeleton):
        beat = sk["beat"]
        narration = sk["narration"]
        dur = round(sk["words"] / wps, 2)
        if beat == "hook":
            cat = "brand-mark"
            asset = _asset_repo("emblem", REPO_ASSETS["emblem"])
            purpose = C[cat]["purpose"]
        elif beat == "ending":
            cat = "brand-close"
            asset = _asset_repo("eye", REPO_ASSETS["eye"])
            purpose = C[cat]["purpose"]
        else:
            slot_pref = None
            if beat == "problem" and sk["split"] == 0:
                slot_pref = ("interview-notes", "evidence-filter", "hypothesis-ladder",
                             "said-vs-did", "notification-cascade", "handoff-chain")
            elif beat == "example" and sk["split"] == 0:
                slot_pref = ("said-vs-did", "hallucination-trap", "source-comparison",
                             "tradeoff-scale", "worked-example", "failure-chain")
            elif beat == "explain" and sk["split"] == 0:
                slot_pref = ("evidence-filter", "hypothesis-ladder", "confidence-gauge",
                             "retrieval-loop", "debug-trace", "notification-cascade",
                             "focus-meter")
            elif beat == "technique" and sk["split"] == 0:
                slot_pref = ("verification-checklist", "hypothesis-ladder", "retrieval-loop",
                             "decision-matrix", "task-queue", "approval-gate")
            cat = _pick_category(narration, tkw, pillar, used_cat, prev_cat, slot_pref,
                                 beat=sk["beat"])
            # code categories only when the narration genuinely justifies code;
            # the swap must stay in the gate's reach: prefer an alternative
            # whose beats fit this scene's narrative role
            if C[cat]["code"] and not code_justified(pillar, narration):
                alts = [a for a in PILLAR_CATEGORIES[pillar]
                        if not C[a]["code"] and a != prev_cat and used_cat.get(a, 0) < 2]
                fit = [a for a in alts if sk["beat"] in C[a].get("beats", ())]
                if fit:
                    cat = fit[0]
                elif alts:
                    cat = alts[0]
            purpose = C[cat]["purpose"]
            # deterministic, semantically matched primary visuals
            if beat == "explain" and sk["split"] >= 1 and n_img_brain < 1 \
                    and os.path.exists(os.path.join(common.ROOT, "assets", "img", "hero_brain.png")):
                asset = _asset_repo("brain", REPO_ASSETS["brain"])
                n_img_brain += 1
            elif beat == "technique" and sk["split"] >= 1 and n_img_desk < 1 \
                    and os.path.exists(os.path.join(common.ROOT, "assets", "img", "hero_desk.png")):
                asset = _asset_repo("desk", REPO_ASSETS["desk"])
                n_img_desk += 1
            elif beat == "problem" and sk["split"] == 0 and C[cat].get("photo_ok") \
                    and allow_external:
                ext = _external_asset_for(narration, tkw, allow_external)
                if ext:
                    asset = ext
                else:
                    asset = _asset_proc(cat, C[cat]["comp"], i)
            else:
                asset = _asset_proc(cat, C[cat]["comp"], i)
        used_cat[cat] = used_cat.get(cat, 0) + 1
        prev_cat = cat

        just_code = C[cat]["code"] and code_justified(pillar, narration)
        scene = {
            "scene_id": f"s{i + 1}",
            "beat": beat,
            "beat_split": sk["split"],
            "narration": narration,
            "topic_keywords": tkw,
            "pillar": pillar,
            "visual_category": cat,
            "visual_purpose": purpose,
            "asset": asset,
            "asset_query": " ".join(tkw[:4]),
            "animation": C[cat]["animation"],
            "content_top": C[cat]["content_top"],
            "content_bottom": C[cat]["content_bottom"],
            "source": {"origin": asset.get("origin", ""), "license": asset.get("license", ""),
                       "path": asset.get("path") or asset.get("asset_url") or asset.get("template", "")},
            "expected_duration": dur,
            "code_justified": bool(just_code),
            "cursor_justified": bool(just_code and cursor_justified(pillar, narration)),
        }
        if C[cat]["code"] and just_code:
            scene["code_lines"] = code_snippet_for(meta, narration)
        scenes.append(scene)

    plan = {
        "version": 1,
        "pillar": pillar,
        "topic_keywords": tkw,
        "palette": {k: list(v) for k, v in BRAND_PALETTE.items()},
        "forbidden_hues": list(COLD_COLOR_NAMES),
        "subtitle_band": list(band),
        "scenes": scenes,
        "generation": "deterministic-visual-plan v1 (no LLM in the visual path)",
    }
    return plan

# ---------------------------------------------------------------------------
# PRE-RENDER visual-semantic gate — deterministic blockers only.
# A structured LLM approval cannot override any of them.
# ---------------------------------------------------------------------------
MAX_TEXT_WORDS_PER_SCENE = 14
MIN_SCENES = 5
MIN_SCENES_LONG = 6
# hook/ending are single brand moments with continuous animation; content
# scenes are kept shorter by the split logic — this is a backstop, not a target
MAX_SCENE_SECONDS = 13.0
MIN_DISTINCT_RATIO = 0.6
MAX_ASSET_TIME_SHARE = 0.45
ALLOWED_EXTERNAL_LICENSES = ("cc0", "cc-by")
MAX_EXTERNAL_BYTES = 8 * 1024 * 1024


def _vocab_overlap(words_a, words_b):
    """Light deterministic stem match: exact equality, or a shared prefix of
    at least 3 chars ('ask' ~ 'asked', 'evid' ~ 'evidence')."""
    for a in words_a:
        for b in words_b:
            if a == b:
                return True
            short, long = (a, b) if len(a) <= len(b) else (b, a)
            if len(short) >= 3 and long.startswith(short):
                return True
    return False


def _valid_url(u):
    """Deterministic URL safety: https only, sane length, no control chars /
    whitespace / path traversal, resolvable hostname. Remote text may NEVER
    influence commands or paths — it is data for the manifest only."""
    if not u or not isinstance(u, str):
        return False
    u = u.strip()
    if not re.match(r"^https://", u):
        return False
    if len(u) > 2000 or any(ord(ch) < 32 for ch in u) or ".." in u or " " in u:
        return False
    try:
        from urllib.parse import urlparse
        host = (urlparse(u).hostname or "").lower()
    except Exception:
        return False
    if not host or "." not in host:
        return False
    if re.search(r"[^\w.\-]", host):
        return False
    return True


def _scene_text_words(scene):
    """Total number of on-screen words (across all label sets) for a scene."""
    spec = C.get(scene.get("visual_category"), {})
    sets = spec.get("label_sets") or []
    return sum(len(w.split()) for ls in sets for w in ls)


def visual_semantic_issues(plan, script, pol=None):
    """Deterministic pre-render visual-semantic gate. Returns blocker strings
    ('[visual_semantics] ...'); an empty list means the plan is renderable.

    This is the SAME function the renderer runs on load and the final QA
    supervisor runs on the episode, so every recovery path (static fallback,
    safe re-render, verify) obeys identical rules. No approval flag, score or
    LLM output is an input — structured approval cannot override it.
    """
    pol = pol or common.policy()
    issues = []
    B = lambda msg: issues.append(f"[visual_semantics] {msg}")  # noqa: E731

    scenes = (plan or {}).get("scenes") or []
    has_chunks = bool(((script or {}).get("chunks") or []))
    if not scenes:
        if has_chunks:
            B("visual plan has no scenes — a reel cannot render without a gated scene plan")
        return issues

    meta = (script or {}).get("meta", {}) or {}
    pillar = derive_pillar(meta, pol)
    if plan.get("pillar") != pillar:
        B(f"plan pillar {plan.get('pillar')!r} != script pillar {pillar!r}")
    tkw = plan.get("topic_keywords") or topic_keywords(script)

    # palette: the plan must declare exactly the warm brand palette, no cold hues
    pal = plan.get("palette") or {}
    for name, rgb in BRAND_PALETTE.items():
        if tuple(pal.get(name, ())) != rgb:
            B(f"brand palette entry {name} was altered from the profile palette")
            break
    for name, rgb in pal.items():
        try:
            rgb = tuple(int(x) for x in rgb)
        except Exception:
            B(f"brand palette entry {name!r} is malformed")
            break
        if _is_cold(rgb):
            B(f"cold hue in the declared palette ({name}) — "
              f"blue/navy/cyan/purple are forbidden in brand treatments")
            break

    band_top, band_bottom = subtitle_band(pol)
    seen_assets = {}
    total_dur = 0.0
    for sc in scenes:
        sid = sc.get("scene_id") or "?"
        cat = sc.get("visual_category") or ""
        narration = (sc.get("narration") or "").strip()
        # --- required metadata
        if not cat or cat not in C:
            B(f"{sid}: unknown visual category {cat!r}")
            continue
        if not narration:
            B(f"{sid}: missing narration segment")
        if not (sc.get("visual_purpose") or "").strip():
            B(f"{sid}: missing visual purpose")
        elif len(str(sc["visual_purpose"]).split()) < 3:
            B(f"{sid}: visual purpose is not meaningful ({sc.get('visual_purpose')!r})")
        if not (sc.get("topic_keywords") or []):
            B(f"{sid}: missing topic keywords")
        asset = sc.get("asset") or {}
        if not asset.get("id") or not asset.get("kind"):
            B(f"{sid}: asset missing id/kind")
        if not (sc.get("source") or {}).get("origin"):
            B(f"{sid}: asset provenance/source missing — never an image without provenance")
        if not sc.get("animation"):
            B(f"{sid}: missing animation type")
        dur = sc.get("expected_duration") or 0
        if not (isinstance(dur, (int, float)) and dur > 0):
            B(f"{sid}: expected duration missing/invalid")
        if not isinstance(sc.get("code_justified"), bool):
            B(f"{sid}: code_justified must be a boolean")
        if not isinstance(sc.get("cursor_justified"), bool):
            B(f"{sid}: cursor_justified must be a boolean")
        total_dur += float(dur or 0)
        if dur and dur > MAX_SCENE_SECONDS:
            B(f"{sid}: scene {dur:.1f}s — cut cadence must stay ~3-6s between visual changes")
        # --- pillar rule
        if cat not in allowed_categories(pillar):
            B(f"{sid}: category {cat!r} is not allowed for pillar {pillar} "
              f"(allowed: {', '.join(allowed_categories(pillar))})")
        # --- code rules
        if C[cat]["code"]:
            if not sc.get("code_justified"):
                B(f"{sid}: code/terminal visual on a non-coding topic ({pillar}) — "
                  "a generic code card is never allowed as decoration")
            elif not code_justified(pillar, narration):
                B(f"{sid}: code claimed justified but the narration does not genuinely "
                  "discuss coding/debugging/testing — decorative code is blocked")
            lines = sc.get("code_lines")
            if lines is not None and not snippet_is_clean(lines):
                B(f"{sid}: code lines contain a banned generic/pseudo-code pattern")
        if sc.get("cursor_justified"):
            if cat != "code-scene" or not sc.get("code_justified"):
                B(f"{sid}: moving/typing cursor without a justified code scene")
            elif not cursor_justified(pillar, narration):
                B(f"{sid}: cursor claimed justified but the narration does not demonstrate "
                  "code entry")
        # --- asset reuse / dominance
        aid = asset.get("id") or ""
        brand = cat in BRAND_CATEGORIES
        seen_assets.setdefault(aid, {"count": 0, "dur": 0.0, "brand": brand})
        seen_assets[aid]["count"] += 1
        seen_assets[aid]["dur"] += float(dur or 0)
        if brand and aid not in (f"repo:{REPO_ASSETS['emblem']}", f"repo:{REPO_ASSETS['eye']}"):
            B(f"{sid}: brand category must use a profile brand asset, got {aid!r}")
        # --- provenance / URL safety
        kind = asset.get("kind")
        if kind == "external":
            if not _valid_url(asset.get("url")):
                B(f"{sid}: external source URL missing/unsafe ({str(asset.get('url'))[:40]!r})")
            if not _valid_url(asset.get("asset_url")):
                B(f"{sid}: external asset URL missing/unsafe")
            lic = str(asset.get("license") or "").lower()
            if lic not in ALLOWED_EXTERNAL_LICENSES:
                B(f"{sid}: external image license {lic!r} not in {ALLOWED_EXTERNAL_LICENSES} — "
                  "no provenance, no image")
            if not asset.get("creator") and lic == "cc-by":
                B(f"{sid}: cc-by image without recorded creator — attribution cannot be rendered")
            if not asset.get("retrieved_utc") or not asset.get("query_sanitized"):
                B(f"{sid}: external provenance incomplete (retrieved_utc/query_sanitized)")
            if not re.fullmatch(r"[0-9a-f]{64}", str(asset.get("sha256") or "")):
                B(f"{sid}: external content hash missing/not sha256")
        elif kind == "repo":
            p = asset.get("path") or ""
            if ".." in p or p.startswith("/") or not p.startswith("assets/"):
                B(f"{sid}: unsafe repository asset path {p!r}")
            if p and not os.path.exists(os.path.join(common.ROOT, p)):
                B(f"{sid}: repository asset {p!r} missing")
        elif kind == "procedural":
            if asset.get("template") not in {spec["comp"] for spec in C.values()}:
                B(f"{sid}: unknown procedural template {asset.get('template')!r}")
        else:
            B(f"{sid}: unknown asset kind {kind!r}")
        # --- scene ↔ narration relevance (deterministic keyword overlap)
        narr_words = {w for w in re.findall(r"[a-z]{3,}", narration.lower())
                      if w not in STOP}
        related = set(tkw) | {k for k in C[cat]["keywords"] if len(k) >= 3} | set(
            re.findall(r"[a-z]{3,}", (sc.get("visual_purpose") or "").lower()))
        related = {r for r in related if len(r) >= 3 and r not in STOP}
        # A scene is related to its narration segment when a topic/category/
        # purpose word overlaps it, OR the category is a legitimate visual for
        # that beat's narrative role (e.g. a decision matrix during the
        # technique segment). A hand-crafted plan with the wrong combination
        # is blocked. The pillar allowlist above is the topical guarantee.
        if narr_words and not _vocab_overlap(narr_words, related) \
                and sc.get("beat") not in C[cat].get("beats", ()):
            B(f"{sid}: scene {cat!r} is unrelated to its narration segment")
        # --- text-heavy guard
        if _scene_text_words(sc) > MAX_TEXT_WORDS_PER_SCENE:
            B(f"{sid}: text-heavy slide — on-screen text must stay part of a "
              f"meaningful composition (<= {MAX_TEXT_WORDS_PER_SCENE} words)")
        # --- subtitle safe zone (overlay must never obstruct the subtitles)
        # Full-frame photos (repo/external) are exempt from the plan-level
        # geometry check: the renderer applies a dedicated warm subtitle shade
        # to them, and the final QA verifies the band on the RENDERED frame.
        ctop = sc.get("content_top")
        cbot = sc.get("content_bottom")
        if not isinstance(ctop, (int, float)) or not isinstance(cbot, (int, float)):
            B(f"{sid}: composition bounds missing")
        elif asset.get("kind") not in ("repo", "external"):
            if ctop < band_bottom or cbot > SCENE_ZONE_BOTTOM or cbot < ctop:
                B(f"{sid}: composition y {int(ctop)}-{int(cbot)} obstructs the subtitle "
                  f"band y {band_top}-{band_bottom} or the handle zone")

    # --- variety / dominance across the reel
    n = len(scenes)
    if n < MIN_SCENES:
        B(f"only {n} scenes — a 60-120s Reel needs at least {MIN_SCENES} distinct visual scenes")
    est_total = total_dur or common.estimate_spoken_seconds(script, pol)
    if est_total >= 75 and n < MIN_SCENES_LONG:
        B(f"only {n} scenes for a {est_total:.0f}s Reel — needs at least {MIN_SCENES_LONG}")
    primary = {aid: v for aid, v in seen_assets.items() if not v["brand"]}
    if not primary:
        B("no primary (non-brand) scene image — the profile assets are brand "
          "references, not the reel's imagery")
    distinct = len(primary)
    need = max(4, math.ceil(MIN_DISTINCT_RATIO * n))
    if distinct < need:
        B(f"insufficient visual variety: {distinct} distinct primary assets for {n} "
          f"scenes (need >= {need})")
    for aid, v in primary.items():
        if total_dur and v["dur"] / total_dur > MAX_ASSET_TIME_SHARE:
            B(f"asset {aid!r} dominates {100 * v['dur'] / total_dur:.0f}% of the Reel")
        if v["count"] > 1:
            B(f"asset {aid!r} reused in {v['count']} scenes — a repeated underlying "
              "image is not a new scene")
    # brand moments: at most the hook/ending brand assets, never dominating
    brand_total = sum(v["dur"] for v in seen_assets.values() if v["brand"])
    if total_dur and brand_total / total_dur > 0.30:
        B("brand imagery dominates the Reel — profile assets are references, not scenes")
    return issues


def plan_summary(plan):
    scenes = (plan or {}).get("scenes") or []
    aids = {sc.get("asset", {}).get("id") for sc in scenes}
    return {"scenes": len(scenes), "distinct_assets": len(aids),
            "categories": sorted({sc.get("visual_category") for sc in scenes}),
            "external": sum(1 for sc in scenes if sc.get("asset", {}).get("kind") == "external"),
            "code_scenes": [sc["scene_id"] for sc in scenes if sc.get("code_justified")],
            "pillar": plan.get("pillar")}

# ---------------------------------------------------------------------------
# Brand grading (issue #24 §3): bring ANY external image into the black/gold
# world — warm duotone, charcoal shadows, gold edge warmth, ivory highlights.
# The output is warm by construction (no blue/navy/cyan/purple can survive).
# ---------------------------------------------------------------------------


def brand_grade(arr):
    """Deterministic warm duotone grade of an RGB numpy array (HxWx3 uint8)."""
    import numpy as np
    a = arr.astype(np.float32) / 255.0
    lum = 0.2126 * a[..., 0] + 0.7152 * a[..., 1] + 0.0722 * a[..., 2]
    g = lum ** 0.94
    shadow = np.array([16, 13, 10], np.float32)     # matte black / charcoal
    mid = np.array([122, 88, 32], np.float32)       # bronze
    highlight = np.array([246, 228, 190], np.float32)  # ivory-gold
    lo = 0.42
    below = g[..., None] < lo
    t_lo = np.clip(g[..., None] / lo, 0, 1)
    t_hi = np.clip((g[..., None] - lo) / (1 - lo), 0, 1)
    out = np.where(below, shadow + (mid - shadow) * t_lo, mid + (highlight - mid) * t_hi)
    # subtle gold midtone lift for warmth
    band = (g > 0.30) & (g < 0.75)
    out = out + np.array([10, 7, 2], np.float32) * band[..., None]
    return np.clip(out, 0, 255).astype(np.uint8)


def cold_pixel_fraction(arr, tol=4):
    """Fraction of pixels whose blue channel dominates — the cold-theme
    signature (blue/navy/cyan/purple) the final frame QA blocks."""
    import numpy as np
    a = np.asarray(arr)
    if a.size == 0:
        return 0.0
    r, g, b = a[..., 0].astype(np.int16), a[..., 1].astype(np.int16), a[..., 2].astype(np.int16)
    cold = (b > r + tol) & (b >= g - 6)
    return float(cold.mean())


def perceptual_hash(img, size=16):
    """Deterministic aHash of a PIL image → int (256 bits)."""
    g = img.convert("L").resize((size, size))
    px = list(g.getdata())
    m = sum(px) / len(px)
    bits = 0
    for v in px:
        bits = (bits << 1) | (1 if v > m else 0)
    return bits


def hamming(a, b):
    return (a ^ b).bit_count()

# ---------------------------------------------------------------------------
# Pillar-appropriate art direction (replaces the old fixed "code visual" string
# that broke reel-2026-09-19) — used by the producer for the script's
# visual_direction field and the poster hint card.
# ---------------------------------------------------------------------------
_CATEGORY_NICE = {
    "said-vs-did": "said-vs-did contrast", "interview-notes": "interview notes",
    "hypothesis-ladder": "hypothesis ladder", "evidence-filter": "evidence filter",
    "decision-matrix": "decision matrix", "funnel": "signal funnel",
    "observation-log": "observation log", "tradeoff-scale": "trade-off scale",
    "prediction-graph": "prediction-vs-actual graph", "concept-web": "concept web",
    "confidence-gauge": "confidence gauge", "uncertainty-range": "uncertainty range",
    "verification-checklist": "verification checklist", "evidence-card": "evidence card",
    "source-comparison": "source comparison", "human-review-gate": "human review gate",
    "hallucination-trap": "hallucination trap", "human-ai-network": "human-AI loop",
    "code-scene": "code concept", "debug-trace": "debug trace", "test-suite": "test suite",
    "system-diagram": "system diagram", "failure-chain": "failure chain",
    "stack-trace": "traceback read", "memory-map": "memory map", "concept-map": "concept map",
    "retrieval-loop": "retrieval loop", "worked-example": "worked example",
    "skill-progression": "skill progression", "notification-cascade": "notification cascade",
    "focus-meter": "focus meter", "task-queue": "task queue", "attention-residue": "attention residue",
    "context-timeline": "context timeline", "handoff-chain": "handoff chain",
    "verification-gate": "verification gate", "role-complement": "complementary roles",
    "approval-gate": "approval gate", "shared-decision-loop": "shared decision loop",
    "brand-mark": "brand open", "brand-close": "brand close",
}


def pillar_visual_direction(pillar, n=4):
    cats = PILLAR_CATEGORIES.get(pillar, PILLAR_CATEGORIES["AI_JUDGMENT"])[:n]
    names = [_CATEGORY_NICE.get(c, c) for c in cats]
    tail = " — matte black and gold duotone, ivory type"
    if pillar != "CODING":
        tail += ", no code or terminal visuals"
    return ", ".join(names) + tail


def suggest_categories(pillar, topic_text=""):
    t = (topic_text or "").lower()
    pool = PILLAR_CATEGORIES.get(pillar, PILLAR_CATEGORIES["AI_JUDGMENT"])
    scored = sorted(pool, key=lambda c: -sum(1 for kw in C[c]["keywords"] if kw in t))
    return scored[:4]
