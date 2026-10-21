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
import palette_qa  # noqa: E402  (single source of the cold-color family)
import text_norm  # noqa: E402  (single source of normalized visible text)

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
    """DECLARED-color cold check (a named palette swatch), delegated to the one
    module that defines the cold family (palette_qa). Rendered decoded pixels
    are judged by palette_qa.analyze_frame, which additionally requires real
    luminance/chroma and coherent regions — the raw inequality here would flag
    codec noise on near-black frames (issue #26)."""
    return palette_qa.declared_color_is_cold(rgb)


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

# --- MINIMAL STYLE (issue #33) — the default production format -------------
# Six stable scenes, one per beat, one dominant visual each:
#   s1 mn-banner (opening banner)   s4 mn-card (technology example)
#   s2 mn-two-state (the problem)   s5 mn-three-step (Try This)
#   s3 mn-dial (what your mind is doing)  s6 mn-cta (final follow CTA)
# Zero photo slots, zero code, zero dense graphics. The renderer draws these
# with the minimal composition system (centered safe text measured with
# textbbox, one restrained gold treatment, transform-only motion). The
# kinetic text of every scene IS the spoken line (karaoke subtitles), so the
# on-screen text volume follows the narration, not a separate label layer.
_cat("mn-banner", "mn_banner", ["brand", "hook", "open"],
     "opening banner — brand line, the hook in the first frame, handle and one gold underline",
     [["METACOGNITION", "FOR THE AI AGE"]],
     content_top=470, content_bottom=1500, animation="fade", beats=("hook",))
_cat("mn-two-state", "mn_two_state", ["contrast", "gap", "before", "after"],
     "one simple two-state comparison — the problem understandable in three seconds",
     [["STATE A"], ["STATE B"]],
     animation="fade", beats=("problem",))
_cat("mn-dial", "mn_dial", ["mind", "drift", "bias", "gauge"],
     "one progress dial — what the mind is doing, at a glance",
     [["DIAL"]],
     animation="fill", beats=("explain",))
_cat("mn-card", "mn_card", ["example", "case", "think"],
     "one clean card — the technology example as a single idea",
     [["EXAMPLE"]],
     animation="fade", beats=("example",))
_cat("mn-three-step", "mn_three_step", ["try", "step", "practice", "pause"],
     "one three-step flow — the single actionable technique",
     [["STEP 1"], ["STEP 2"], ["STEP 3"]],
     animation="steps", beats=("technique",))
_cat("mn-cta", "mn_cta", ["follow", "ending", "your turn"],
     "final CTA — the explicit follow ask with the handle, centered and readable",
     [["FOLLOW"]],
     content_top=470, content_bottom=1500, animation="fade", beats=("ending",))

# minimal categories are brand moments (profile assets used as accents) and
# style-level compositions allowed for EVERY pillar
MINIMAL_CATEGORIES = ("mn-banner", "mn-two-state", "mn-dial", "mn-card",
                      "mn-three-step", "mn-cta")
MINIMAL_BRAND_CATEGORIES = ("mn-banner", "mn-cta")
MINIMAL_BODY_CATEGORIES = ("mn-two-state", "mn-dial", "mn-card", "mn-three-step")
# the stable six-scene structure: one scene per beat, in this order
MINIMAL_BEAT_ORDER = ("hook", "problem", "explain", "example", "technique", "ending")
MINIMAL_BEAT_CATEGORIES = {
    "hook": "mn-banner", "problem": "mn-two-state", "explain": "mn-dial",
    "example": "mn-card", "technique": "mn-three-step", "ending": "mn-cta",
}
# per-scene meaningful-label budget (labels are derived from the narration;
# the spoken line itself is the kinetic text and is not counted here)
MINIMAL_LABEL_BUDGET = {"mn-banner": 0, "mn-two-state": 2, "mn-dial": 1,
                        "mn-card": 1, "mn-three-step": 3, "mn-cta": 0}

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
     [["EXPECTED", "OBSERVED", "GAP"]], photo_ok=True)
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
     [["EVIDENCE", "SOURCED", "TIERED"]], photo_ok=True)
_cat("source-comparison", "dual", ["source", "compare", "comparison", "claim", "fact"],
     "compare the fluent claim with the grounded fact",
     [["THE CLAIM", "THE FACT"], ["SOUNDS LIKE", "IS"]],
     beats=("problem", "explain", "example"), photo_ok=True)
_cat("human-review-gate", "ladder", ["human review", "review", "approve", "gate", "sign off"],
     "mark the point where a human reviews the AI output",
     [["AI OUTPUT", "HUMAN CHECK", "SHIPPED"]])
_cat("hallucination-trap", "dual", ["hallucinat", "invented", "fluent", "plausible", "wrong"],
     "contrast the fluent invented answer with the grounded one",
     [["INVENTED, FLUENT", "ACTUALLY TRUE"]], beats=("problem", "explain", "example"),
     photo_ok=True)
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
     [["PARTS", "CONNECTS", "BREAKS"]], beats=("explain", "example", "technique"),
     photo_ok=True)
_cat("failure-chain", "ladder", ["failure", "cascade", "incident", "cause", "effect",
                                  "chain"],
     "show the cause-and-effect failure chain",
     [["TRIGGER", "SPREAD", "INCIDENT", "LESSON"]], photo_ok=True)
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
     [["STEP 1", "STEP 2", "DONE"]], photo_ok=True)
_cat("skill-progression", "ladder", ["skill", "progress", "practice", "level", "drill"],
     "show the skill progressing with practice",
     [["STUCK", "HELPED", "SOLID", "OWN"]], photo_ok=True)

# --- DIGITAL_ATTENTION -----------------------------------------------------------
# Issue #32 §4: ATTENTION scenes must communicate ONE clear concept an ordinary
# viewer reads in under three seconds (notification → attention shift,
# listening vs drafting a reply, focus before/after, pause → count → refocus,
# the pause-and-reflect loop) — never a generic dense network.
_cat("notification-cascade", "notifications", ["notification", "alert", "ping", "slack",
                                                "message", "badge", "suggestion"],
     "show a notification arriving and attention shifting to it",
     [["NEW MESSAGE", "ANOTHER PING", "FOCUS LOST"],
      ["NEW MESSAGE", "SECOND PING", "FOCUS DROPS"]],
     beats=("problem", "example"))
_cat("focus-meter", "gauges", ["focus", "concentration", "deep work", "interrupted"],
     "show focus before and after the interruption",
     [["FOCUS", "DISTRACTED"], ["FOCUS", "AFTER SWITCH"]],
     beats=("problem", "explain"))
_cat("attention-drift", "dual", ["stand", "wander", "drift", "nod", "screen",
                                 "thoughts", "elsewhere", "updates"],
     "contrast eyes on the screen with a mind drifting elsewhere",
     [["EYES ON SCREEN", "MIND ELSEWHERE"], ["LISTENING", "WANDERING"]],
     photo_ok=True, beats=("problem", "explain"))
_cat("listen-vs-draft", "dual", ["listening", "drafting", "reply", "replies",
                                 "hearing", "gap", "typing", "imagine"],
     "contrast actually listening with drafting a reply in your head",
     [["HEARING", "DRAFTING A REPLY"], ["LISTENING", "TYPING"]],
     photo_ok=True, beats=("problem", "explain", "example"))
_cat("refocus-steps", "ladder", ["pause", "count", "refocus", "urge", "notice",
                                 "hold", "silently", "voice"],
     "the pause-count-refocus steps that bring attention back",
     [["PAUSE", "COUNT", "REFOCUS"], ["NOTICE", "PAUSE", "RETURN"]],
     beats=("technique", "explain"))
_cat("reflect-loop", "loop", ["repeat", "loop", "reflect", "meeting", "train",
                              "muscle", "practice", "each"],
     "the repeat-pause-reflect loop that trains your attention",
     [["PAUSE", "REFLECT", "REPEAT"], ["NOTICE", "PAUSE", "REFLECT"]],
     photo_ok=True, beats=("technique",))
_cat("task-queue", "bars", ["task", "queue", "switch", "context", "batch",
                            "assignment", "missed"],
     "show the tasks waiting while attention switches",
     [["TASK A", "QUEUED", "WAITING"], ["TASK", "QUEUED", "WAITING"]])
_cat("attention-residue", "bars", ["residue", "left behind", "minutes", "return",
                                   "reload", "note", "jot"],
     "show the attention left behind after each switch",
     [["TASK A", "RESIDUE", "TASK B"]])
_cat("context-timeline", "ladder", ["context", "timeline", "day", "window", "batch"],
     "lay the context switches along the day",
     [["9:00", "11:00", "14:00", "17:00"]], photo_ok=True)

# --- HUMAN_AI_COLLABORATION --------------------------------------------------------
_cat("handoff-chain", "ladder", ["handoff", "hand off", "pass", "relay", "next"],
     "show the handoffs between human and AI",
     [["YOU", "AI", "YOU", "SHIPPED"]])
_cat("verification-gate", "ladder", ["verify", "gate", "before ship", "approve"],
     "the verification gate before the handoff ships",
     [["DRAFT", "VERIFY", "APPROVE", "SHIP"]])
_cat("role-complement", "dual", ["complement", "roles", "strength", "each other"],
     "show the complementary roles side by side",
     [["HUMAN: JUDGE", "AI: DRAFT"]], beats=("problem", "explain", "example"),
     photo_ok=True)
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
    "ATTENTION": ["notification-cascade", "focus-meter", "attention-drift",
                  "listen-vs-draft", "refocus-steps", "reflect-loop",
                  "task-queue", "attention-residue", "context-timeline",
                  "prediction-graph", "concept-web"],
    "HUMAN_AI": ["handoff-chain", "verification-gate", "role-complement",
                 "approval-gate", "shared-decision-loop", "human-ai-network",
                 "concept-web"],
}

BRAND_CATEGORIES = ("brand-mark", "brand-close") + MINIMAL_BRAND_CATEGORIES
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
    # style-level minimal compositions are allowed for every pillar — they are
    # the default production format, not a topic-dependent decoration
    return (list(PILLAR_CATEGORIES.get(pillar, PILLAR_CATEGORIES["AI_JUDGMENT"]))
            + list(BRAND_CATEGORIES) + list(MINIMAL_BODY_CATEGORIES))


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
# Only the two PROFILE BRAND assets may be repository scene visuals. The
# hero images (hero_brain / hero_desk) are deliberately excluded: a normal
# reel's photographs come from the license-aware source (CC0/PDM), with a
# distinct procedural fallback — never a reused repository hero image.
REPO_ASSETS = {
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


# Category keywords are cheap matching stems ("hallucinat", "collaborat").
# A SEARCH QUERY must be human-readable words, so every stem that can reach an
# Openverse query is expanded here (the mapping is asserted by the tests).
STEM_WORD_FIXES = {
    "hallucinat": "hallucination",
    "collaborat": "collaboration",
    "concentrat": "concentration",
    "verif": "verification",
    "uncertain": "uncertainty",
    "substit": "substitution",
    "distract": "distraction",
    "complex": "complexity",
}


def human_query_term(category):
    """The human-readable search subject for a scene category.

    Openverse receives real words, never a stem: ``hallucination-trap`` →
    ``hallucination``. Deterministic, no remote text involved.
    """
    keywords = C.get(category, {}).get("keywords") or [category]
    term = str(keywords[0]).strip().lower()
    term = STEM_WORD_FIXES.get(term, term)
    term = re.sub(r"[^a-z0-9 ]+", " ", term).strip()
    return term or str(category).replace("-", " ")


def external_asset_query(category, topic_kw, max_topic_words=4):
    """The exact human-readable query string handed to the $0 photo source."""
    subject = human_query_term(category)
    return " ".join([w for w in ((topic_kw or [])[:max_topic_words] + [subject]) if w])


def _external_asset_for(scene_narration, topic_kw, allow_external,
                        category="", used_urls=()):
    """$0 license-aware photo for ONE photo-designated scene.

    The query carries the topic keywords PLUS the scene category's human
    subject word so different slots retrieve different images; already-used
    asset URLs are skipped so a reel can never silently repeat one photograph.
    Any problem → None → deterministic procedural fallback (the reel never
    fails because the external service is unavailable, and never reuses an
    image to compensate).

    Returns (asset_or_None, reason): the reason is one of the safe
    asset_fetch failure categories (no URLs, no remote bodies) so the plan —
    and the daily Issue — can explain WHY a slot fell back. A slot that was
    never attempted (external retrieval disabled) reports "disabled".
    """
    if not allow_external:
        return None, "disabled"
    try:
        import asset_fetch
    except Exception:
        return None, "disabled"
    if not asset_fetch.enabled():
        return None, "disabled"
    asset = asset_fetch.fetch_image(external_asset_query(category, topic_kw),
                                    skip_urls=used_urls)
    reason = asset_fetch.last_outcome() or (
        "retrieved" if asset is not None else "no_results")
    return asset, reason


# A normal 60-120 s reel intentionally carries a balanced visual mix
# (issue #24 follow-up): up to PHOTO_TARGET distinct topic-relevant LICENSED
# photographs, the remaining content scenes as topic-specific procedural
# diagrams/comparisons, and profile brand assets ONLY at hook/ending. Repo
# hero images are never scene backgrounds — photo slots come from the
# license-aware source or fall back to a distinct procedural visual.
PHOTO_TARGET = 3
PHOTO_MIN = 2


def _designate_photo_slots(skeleton, cats, pillar, used_cat):
    """Explicitly designate which content scenes will carry a topic-relevant
    photograph: up to PHOTO_TARGET content scenes, at most one per beat,
    photo-capable non-code categories only. When the category pass left
    fewer candidates than the target, a later scene per beat is re-pointed
    to a photo-capable category that fits the beat (gate-accepted via the
    beat-role rule). Mutates cats/used_cat on re-point; returns the set of
    scene indices."""
    slots, photo_beats = set(), set()
    for i, sk in enumerate(skeleton):
        if len(slots) >= PHOTO_TARGET:
            break
        beat = sk["beat"]
        if beat in ("hook", "ending") or beat in photo_beats:
            continue
        cat = cats[i]
        if C[cat]["code"] or not C[cat].get("photo_ok"):
            continue
        slots.add(i)
        photo_beats.add(beat)
    # Re-point pass: reach the TARGET whenever the pillar offers photo-capable
    # categories for a beat without a photo yet (a normal reel carries 2-3
    # photos, not just the minimum).
    for beat in ("explain", "example", "problem", "technique"):
        if len(slots) >= PHOTO_TARGET:
            break
        if beat in photo_beats:
            continue
        idxs = [i for i, sk in enumerate(skeleton)
                if sk["beat"] == beat and i not in slots]
        for i in reversed(idxs):  # prefer the later scene of the beat
            prev = cats[i - 1] if i > 0 else None
            cands = [c for c in PILLAR_CATEGORIES[pillar]
                     if C[c].get("photo_ok") and not C[c]["code"]
                     and beat in C[c].get("beats", ())
                     and c != prev and c != cats[i]
                     and used_cat.get(c, 0) < 2]
            if not cands:
                continue
            old = cats[i]
            cats[i] = cands[0]
            used_cat[old] = max(0, used_cat.get(old, 0) - 1)
            used_cat[cats[i]] = used_cat.get(cats[i], 0) + 1
            slots.add(i)
            photo_beats.add(beat)
            break
    return slots


# ---------------------------------------------------------------------------
# MINIMAL STYLE PLAN (issue #33) — the default scheduled production format.
#
# Six stable scenes, one per beat (the six-scene contract):
#   1 opening banner/hook · 2 the problem · 3 what your mind is doing
#   4 technology example  · 5 Try This (one actionable technique)
#   6 final follow CTA.
#
# Hard properties (enforced here and re-checked by the visual gate + QA):
#   * ZERO photo slots, ZERO external assets, ZERO Openverse calls —
#     `allow_external` is IGNORED in minimal style;
#   * ZERO code/terminal/cursor (code_justified/cursor_justified always False);
#   * at most MINIMAL_LABEL_BUDGET[cat] derived labels per scene (<= 3),
#     each a short word taken FROM the narration (meaningful, not decorative);
#   * the kinetic text is the spoken line itself (karaoke subtitles), so no
#     second competing text layer is ever drawn;
#   * banner carries brand line + hook + handle; CTA carries the explicit
#     follow ask + handle; both are deterministic, never LLM-chosen.
# ---------------------------------------------------------------------------

MINIMAL_BRAND_LINE = "METACOGNITION FOR THE AI AGE"
MINIMAL_HANDLE_DEFAULT = "@metacognition.hq"
# The canonical spoken + displayed CTA line: an explicit follow ask, the
# handle, and the page-value connection — conversational, no bait, and
# deliberately short (~4 s at the policy narration rate) so the CTA scene
# stays in its 3-5 s duration contract.
MINIMAL_CTA_LINE = ("Follow @metacognition.hq for practical ways to think "
                    "better with technology.")
# one beat = one scene (no split) in minimal. 25s is a SANITY bound: every
# static playbook beat is <= 21.7s (at the 150wpm+5% rate), and the kinetic
# text is the streaming karaoke line, so long body scenes are fine — but a
# 30s+ single scene is a broken script and must block.
MINIMAL_MAX_SCENE_SECONDS = 25.0
# soft targets (QA warning level, issue #33 §4): the banner sits ~2.5-4s,
# the CTA ~3-5s. The hard windows below are SANITY blockers only (too short
# to read / pathologically long) — they admit any policy-conformant hook
# (hook_policy max 15 words ~= 5.7s at the 150wpm+5% narration rate).
MINIMAL_BANNER_SECONDS = (2.5, 4.0)
MINIMAL_BANNER_HARD_SECONDS = (1.5, 5.8)
MINIMAL_CTA_SECONDS = (3.0, 5.0)
MINIMAL_CTA_HARD_SECONDS = (2.2, 6.0)


def _content_words(text):
    """Deterministic content words of a narration line (longest first)."""
    words = re.findall(r"[A-Za-z][A-Za-z'\-]{2,}", text or "")
    return [w for w in words if w.lower() not in STOP]


def _minimal_labels(narration, budget):
    """Up to `budget` meaningful labels for a minimal scene.

    Derived FROM the scene narration only (never LLM text, never decorative
    vocabulary): the most specific content words (longest first, first
    occurrence wins ties), uppercased, each capped at 12 characters so a
    compound word can never become microtext or overflow its container.
    """
    if budget <= 0:
        return []
    seen, out = set(), []
    ranked = sorted(_content_words(narration), key=lambda w: (-len(w), w.lower()))
    for w in ranked:
        key = w.lower()
        if key in seen:
            continue
        # never truncate a word mid-word — a word longer than the label
        # limit is skipped for the next candidate (deterministic)
        if len(w) > 12:
            continue
        seen.add(key)
        out.append(w.upper())
        if len(out) >= budget:
            break
    return out


def _minimal_gold_keyword(hook_line):
    """The ONE gold-highlighted keyword of the opening banner.

    Deterministic: the most specific content word (longest, len>=4, first
    occurrence wins ties) of the FIRST hook line — never a random choice,
    never LLM-chosen. Falls back to the first content word.
    """
    words = _content_words(hook_line)
    specific = [w for w in words if len(w) >= 4]
    pool = specific or words
    if not pool:
        return ""
    return sorted(pool, key=lambda w: (-len(w), w.lower()))[0]


def _build_minimal_plan(script, pol):
    """The deterministic minimal scene plan (six stable scenes, one per beat).

    Raises ValueError when the script does not carry all six beats — minimal
    is a strict contract and fails closed instead of degrading.
    """
    pol = pol or common.policy()
    meta = (script or {}).get("meta", {}) or {}
    pillar = derive_pillar(meta, pol)
    tkw = topic_keywords(script)
    wps = _wps(pol)
    band = subtitle_band(pol)

    beats = _beat_lines(script)
    by_beat = {}
    for beat, lines in beats:
        if beat in MINIMAL_BEAT_ORDER:
            by_beat.setdefault(beat, []).extend(lines)
    missing = [b for b in MINIMAL_BEAT_ORDER if b not in by_beat]
    if missing:
        raise ValueError(
            f"minimal style requires all six beats (one scene each); "
            f"missing: {', '.join(missing)}")

    handle = meta.get("handle") or MINIMAL_HANDLE_DEFAULT
    scenes = []
    for i, beat in enumerate(MINIMAL_BEAT_ORDER):
        lines = by_beat[beat]
        narration = " ".join(t for t, _ in lines)
        words = sum(w for _, w in lines)
        dur = round(words / wps, 2)
        cat = MINIMAL_BEAT_CATEGORIES[beat]
        spec = C[cat]
        if beat == "hook":
            asset = _asset_repo("emblem", REPO_ASSETS["emblem"])
        elif beat == "ending":
            asset = _asset_repo("eye", REPO_ASSETS["eye"])
        else:
            asset = _asset_proc(cat, spec["comp"], i)
        scene = {
            "scene_id": f"s{i + 1}",
            "beat": beat,
            "beat_split": 0,
            "narration": narration,
            "topic_keywords": tkw,
            "pillar": pillar,
            "visual_category": cat,
            "visual_purpose": spec["purpose"],
            "asset": asset,
            "asset_query": None,
            "asset_query_sent": None,
            "asset_query_planned": None,
            "asset_retrieval": "brand" if beat in ("hook", "ending") else "procedural",
            "animation": spec["animation"],
            "content_top": spec["content_top"],
            "content_bottom": spec["content_bottom"],
            "source": {"origin": asset.get("origin", ""), "license": asset.get("license", ""),
                       "path": asset.get("path") or asset.get("template", "")},
            "expected_duration": dur,
            "code_justified": False,
            "cursor_justified": False,
            "photo_designated": False,
            "retrieval_reason": "brand" if beat in ("hook", "ending") else "procedural",
            "labels": _minimal_labels(narration, MINIMAL_LABEL_BUDGET[cat]),
            "style": "minimal",
        }
        if beat == "hook":
            first_line = text_norm.normalize_text(lines[0][0])
            scene["banner"] = {
                "brand_line": MINIMAL_BRAND_LINE,
                "hook_text": first_line,
                "handle": handle,
                "gold_keyword": _minimal_gold_keyword(first_line),
            }
        elif beat == "ending":
            scene["cta"] = {
                "follow_line": MINIMAL_CTA_LINE,
                "handle": handle,
                "gold_phrase": handle,
            }
        scenes.append(scene)

    plan = {
        "version": 1,
        "style": "minimal",
        "pillar": pillar,
        "topic_keywords": tkw,
        "palette": {k: list(v) for k, v in BRAND_PALETTE.items()},
        "forbidden_hues": list(COLD_COLOR_NAMES),
        "subtitle_band": list(band),
        "scenes": scenes,
        "photo_policy": {
            "target": 0, "minimum": 0, "designated": [],
            "source": "none — minimal style performs ZERO external asset calls",
            "fallback": "n/a",
            "repo_hero_as_background": False,
            "retrieval_outcomes": {},
            "photos_retrieved": 0,
            "degraded": False,
        },
        "banner_contract": {
            "brand_line": MINIMAL_BRAND_LINE,
            "handle": handle,
            "duration_target_s": list(MINIMAL_BANNER_SECONDS),
            "duration_hard_s": list(MINIMAL_BANNER_HARD_SECONDS),
            "actual_s": scenes[0]["expected_duration"],
        },
        "cta_contract": {
            "follow_line": MINIMAL_CTA_LINE,
            "handle": handle,
            "duration_target_s": list(MINIMAL_CTA_SECONDS),
            "duration_hard_s": list(MINIMAL_CTA_HARD_SECONDS),
            "actual_s": scenes[-1]["expected_duration"],
        },
        "generation": "deterministic-minimal-plan v1 (no LLM, zero external assets)",
    }
    return plan


def build_visual_plan(script, pol=None, allow_external=None, style=None):
    """Deterministic topic/pillar-aware scene plan for an approved script.

    Generated ONLY from the approved script + editorial policy (the caller
    runs this after Producer/Reviewer/Revision + the deterministic pre-render
    text QA). `allow_external` enables the $0 Openverse photo fetch for the
    designated photo slots (fail-soft: any problem falls back to a distinct
    deterministic procedural visual — the reel never fails on network, and
    never reuses a repository hero image to compensate).

    `style` selects the production format: None keeps the legacy rich plan
    (back-compat for existing callers/tests); "minimal" builds the six-scene
    minimal plan (zero external assets — `allow_external` is ignored);
    "experimental-rich" is the legacy path under its explicit name.
    """
    if style is not None:
        import style_config
        style_config.normalize_style(style, allow_rich=True)
    if style == "minimal":
        return _build_minimal_plan(script, pol)
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

    # ---- phase 1: category assignment (deterministic, pillar-aware) --------
    cats = []
    used_cat = {}
    prev_cat = None
    for i, sk in enumerate(skeleton):
        beat = sk["beat"]
        if beat == "hook":
            cat = "brand-mark"
        elif beat == "ending":
            cat = "brand-close"
        else:
            narration = sk["narration"]
            slot_pref = None
            if beat == "problem" and sk["split"] == 0:
                slot_pref = ("interview-notes", "evidence-filter", "hypothesis-ladder",
                             "said-vs-did", "notification-cascade", "handoff-chain",
                             "attention-drift", "listen-vs-draft")
            elif beat == "example" and sk["split"] == 0:
                slot_pref = ("said-vs-did", "hallucination-trap", "source-comparison",
                             "tradeoff-scale", "worked-example", "failure-chain")
            elif beat == "explain" and sk["split"] == 0:
                slot_pref = ("evidence-filter", "hypothesis-ladder", "confidence-gauge",
                             "retrieval-loop", "debug-trace", "notification-cascade",
                             "focus-meter")
            elif beat == "technique" and sk["split"] == 0:
                slot_pref = ("verification-checklist", "hypothesis-ladder", "retrieval-loop",
                             "decision-matrix", "task-queue", "approval-gate",
                             "refocus-steps", "reflect-loop")
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
        cats.append(cat)
        used_cat[cat] = used_cat.get(cat, 0) + 1
        prev_cat = cat

    # ---- phase 2: explicit photo-slot designation (balanced visual mix) ----
    photo_slots = _designate_photo_slots(skeleton, cats, pillar, used_cat)

    scenes = []
    ext_used_urls = []
    retrieval_outcomes = {}
    for i, sk in enumerate(skeleton):
        beat = sk["beat"]
        narration = sk["narration"]
        dur = round(sk["words"] / wps, 2)
        cat = cats[i]
        purpose = C[cat]["purpose"]
        retrieval_reason = "procedural"
        if beat == "hook":
            asset = _asset_repo("emblem", REPO_ASSETS["emblem"])
        elif beat == "ending":
            asset = _asset_repo("eye", REPO_ASSETS["eye"])
        elif i in photo_slots:
            # a photo-designated scene: official license-aware source first
            # (CC0/PDM only); on any failure the topic-specific procedural
            # visual for this scene — deterministic, distinct, never a reused
            # repository hero image
            asset, retrieval_reason = _external_asset_for(
                narration, tkw, allow_external,
                category=cat, used_urls=ext_used_urls)
            retrieval_outcomes[retrieval_reason] = \
                retrieval_outcomes.get(retrieval_reason, 0) + 1
            if asset is None:
                asset = _asset_proc(cat, C[cat]["comp"], i)
            else:
                ext_used_urls.append(asset.get("asset_url"))
        else:
            asset = _asset_proc(cat, C[cat]["comp"], i)

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
            "asset_query": external_asset_query(cat, tkw),
            # Issue #26 §8: record the EXACT sanitized human-readable query the
            # $0 source actually received (the fetcher stores what it sent) —
            # not a different planned query — plus the retrieval outcome, so the
            # daily report can show real photo provenance without exposing URLs,
            # remote metadata or downloaded filenames.
            "asset_query_sent": (asset.get("query_sanitized") or None)
            if asset.get("kind") == "external" else None,
            "asset_query_planned": external_asset_query(cat, tkw)
            if (beat not in ("hook", "ending") and i in photo_slots) else None,
            "asset_retrieval": ("retrieved" if asset.get("kind") == "external"
                                else "procedural-fallback"
                                if (beat not in ("hook", "ending") and i in photo_slots)
                                else "procedural" if asset.get("kind") == "procedural"
                                else "repository-brand-accent"),
            "animation": C[cat]["animation"],
            "content_top": C[cat]["content_top"],
            "content_bottom": C[cat]["content_bottom"],
            "source": {"origin": asset.get("origin", ""), "license": asset.get("license", ""),
                       "path": asset.get("path") or asset.get("asset_url") or asset.get("template", "")},
            "expected_duration": dur,
            "code_justified": bool(just_code),
            "cursor_justified": bool(just_code and cursor_justified(pillar, narration)),
            "photo_designated": bool(beat not in ("hook", "ending") and i in photo_slots),
            # Safe retrieval/fallback reason for this slot (issue #32 §8-9):
            # one of the asset_fetch failure categories for a photo-designated
            # scene, "procedural" for a scene that was never a photo slot, and
            # "brand" for the hook/ending brand moments. No URLs, no bodies.
            "retrieval_reason": ("brand" if beat in ("hook", "ending")
                                 else retrieval_reason),
        }
        if C[cat]["code"] and just_code:
            scene["code_lines"] = code_snippet_for(meta, narration)
        scenes.append(scene)

    # photo-mix honesty (issue #32 §9): a reel whose designated photo slots
    # all fell back is still technically renderable, but it must NEVER be
    # reported as fully visually complete — the plan carries an explicit
    # degradation flag and the aggregate reason counts that explain WHY.
    photos_retrieved = sum(1 for sc in scenes if sc["asset"].get("kind") == "external")
    photo_degraded = bool(photo_slots) and photos_retrieved == 0

    plan = {
        "version": 1,
        "pillar": pillar,
        "topic_keywords": tkw,
        "palette": {k: list(v) for k, v in BRAND_PALETTE.items()},
        "forbidden_hues": list(COLD_COLOR_NAMES),
        "subtitle_band": list(band),
        "scenes": scenes,
        "photo_policy": {
            "target": PHOTO_TARGET, "minimum": PHOTO_MIN,
            "designated": [f"s{i + 1}" for i in sorted(photo_slots)],
            "source": "openverse-api (CC0/PDM only; CC-BY disabled until public "
                      "attribution exists), $0, keyless",
            "fallback": "distinct topic-specific procedural visual per scene",
            "repo_hero_as_background": False,
            # Aggregate safe reason counts for the designated photo slots
            # (issue #32 §8): retrieved / cache_hit / search_http_error /
            # search_timeout / no_results / no_allowed_license / ... — never
            # URLs or remote bodies. An empty dict means no photo slot was
            # attempted (external retrieval disabled).
            "retrieval_outcomes": {k: v for k, v in
                                   sorted(retrieval_outcomes.items())},
            "photos_retrieved": photos_retrieved,
            "degraded": photo_degraded,
        },
        "generation": "deterministic-visual-plan v2 (no LLM in the visual path)",
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
# Must mirror asset_fetch.ALLOWED_LICENSES: attribution-free public-domain
# licenses only. A CC-BY asset can never pass this gate (and can never be
# fetched), so it can never reach publication without public attribution.
ALLOWED_EXTERNAL_LICENSES = ("cc0", "pdm")
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


def _minimal_plan_issues(plan, script):
    """Minimal-style contract blockers (issue #33 §3-6, 11).

    Runs AFTER the generic gate checks (palette, metadata, provenance,
    asset safety all apply identically). These are the minimal-specific
    hard contracts:
      * exactly six scenes, one per beat, in the stable order
        hook → problem → explain → example → technique → ending;
      * the right minimal composition per beat (banner/two-state/dial/
        card/three-step/cta);
      * ZERO photo slots / ZERO external assets / ZERO code / ZERO cursor;
      * at most MINIMAL_LABEL_BUDGET derived labels per scene (<= 3);
      * opening banner: brand line + hook text + handle + one gold keyword;
      * final CTA: explicit follow ask + handle;
      * banner duration inside the hard window (2.5-4s target), CTA inside
        its hard window (3-5s target) — outside the hard window is a
        blocker; inside the hard but outside the target is a warning-level
        note recorded in details by final QA (not a gate blocker).
    """
    issues = []
    B = lambda msg: issues.append(f"[visual_semantics] {msg}")  # noqa: E731
    scenes = (plan or {}).get("scenes") or []

    if len(scenes) != 6:
        B(f"minimal style requires exactly six stable scenes, got {len(scenes)}")
        return issues
    for i, sc in enumerate(scenes):
        beat = MINIMAL_BEAT_ORDER[i]
        want_cat = MINIMAL_BEAT_CATEGORIES[beat]
        if sc.get("beat") != beat:
            B(f"scene {i + 1} must be the {beat!r} beat (stable six-scene "
              f"contract), got {sc.get('beat')!r}")
        if sc.get("visual_category") != want_cat:
            B(f"scene {i + 1} ({beat}) must use composition {want_cat!r}, "
              f"got {sc.get('visual_category')!r}")
    for sc in scenes:
        sid = sc.get("scene_id") or "?"
        if sc.get("photo_designated"):
            B(f"{sid}: minimal style has ZERO photo slots")
        if (sc.get("asset") or {}).get("kind") == "external":
            B(f"{sid}: minimal style has ZERO external assets")
        if sc.get("code_justified") or sc.get("code_lines"):
            B(f"{sid}: minimal style has ZERO code/terminal visuals")
        if sc.get("cursor_justified"):
            B(f"{sid}: minimal style has ZERO cursors")
        labels = sc.get("labels") or []
        budget = MINIMAL_LABEL_BUDGET.get(sc.get("visual_category"), 3)
        if len(labels) > budget:
            B(f"{sid}: {len(labels)} labels exceed the minimal budget of {budget}")
        for lab in labels:
            if not (isinstance(lab, str) and 1 <= len(lab) <= 12):
                B(f"{sid}: label {lab!r} is not a short readable word (<=12 chars)")
    banner = scenes[0]
    bb = banner.get("banner") or {}
    if not bb.get("brand_line"):
        B("opening banner is missing the brand line")
    if not (bb.get("hook_text") or "").strip():
        B("opening banner is missing the hook text (must be visible in the FIRST frame)")
    if not (bb.get("handle") or "").startswith("@"):
        B("opening banner is missing the @handle")
    if not bb.get("gold_keyword"):
        B("opening banner has no single gold-highlighted keyword")
    cta = scenes[-1]
    cc = cta.get("cta") or {}
    if not (cc.get("follow_line") or "").strip():
        B("final CTA is missing the explicit follow line")
    if not (cc.get("handle") or "").startswith("@"):
        B("final CTA is missing the @handle")
    if "follow" not in cc.get("follow_line", "").lower():
        B("final CTA does not explicitly ask to follow")
    # durations: hard windows are blockers (the soft 2.5-4s / 3-5s targets
    # are verified with warnings by final QA on the rendered frames)
    bd = banner.get("expected_duration") or 0
    lo, hi = MINIMAL_BANNER_HARD_SECONDS
    if not (lo <= bd <= hi):
        B(f"opening banner duration {bd:.1f}s outside the hard window "
          f"{lo}-{hi}s (target {MINIMAL_BANNER_SECONDS[0]}-{MINIMAL_BANNER_SECONDS[1]}s)")
    cd = cta.get("expected_duration") or 0
    lo, hi = MINIMAL_CTA_HARD_SECONDS
    if not (lo <= cd <= hi):
        B(f"final CTA duration {cd:.1f}s outside the hard window "
          f"{lo}-{hi}s (target {MINIMAL_CTA_SECONDS[0]}-{MINIMAL_CTA_SECONDS[1]}s)")
    return issues


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
    is_minimal = (plan or {}).get("style") == "minimal"

    scenes = (plan or {}).get("scenes") or []
    has_chunks = bool(((script or {}).get("chunks") or []))
    if not scenes:
        if has_chunks:
            B("visual plan has no scenes — a reel cannot render without a gated scene plan")
        return issues
    if is_minimal:
        issues.extend(_minimal_plan_issues(plan, script))

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
        # minimal: one stable scene per beat (the six-scene contract) — long
        # beats stay one scene; the kinetic text is the spoken line, so the
        # rich cut-cadence rule does not apply
        max_scene = MINIMAL_MAX_SCENE_SECONDS if is_minimal else MAX_SCENE_SECONDS
        if dur and dur > max_scene:
            B(f"{sid}: scene {dur:.1f}s exceeds the "
              f"{'minimal one-beat' if is_minimal else 'rich ~3-6s'} scene bound {max_scene:.0f}s")
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
            if lic == "cc-by" and not asset.get("creator"):
                B(f"{sid}: cc-by image without recorded creator — and CC-BY has no "
                  "public attribution channel, so it is never publishable")
            if not asset.get("retrieved_utc") or not asset.get("query_sanitized"):
                B(f"{sid}: external provenance incomplete (retrieved_utc/query_sanitized)")
            if not re.fullmatch(r"[0-9a-f]{64}", str(asset.get("sha256") or "")):
                B(f"{sid}: external content hash missing/not sha256")
        elif kind == "repo":
            p = asset.get("path") or ""
            if ".." in p or p.startswith("/") or not p.startswith("assets/"):
                B(f"{sid}: unsafe repository asset path {p!r}")
            # repository images are BRAND references only: a scene visual may
            # never be an existing hero image as its dominant background —
            # photo slots come from the license-aware source (or fall back to
            # a distinct procedural visual)
            if p not in (REPO_ASSETS["emblem"], REPO_ASSETS["eye"]):
                B(f"{sid}: repository image {p!r} is not a profile brand asset — "
                  "existing hero images are not scene backgrounds")
            elif sc.get("photo_designated"):
                B(f"{sid}: a photo-designated scene must use the license-aware "
                  "source or its procedural fallback, never a brand asset")
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


def photo_provenance_summary(plan):
    """Safe photo/variety observability for the daily Issue (issue #26 §8).

    Counts only — no URLs, no query strings, no remote metadata, no downloaded
    filenames. A procedural fallback is NEVER reported as a retrieved
    photograph.
    """
    scenes = (plan or {}).get("scenes") or []
    brand = [sc for sc in scenes if sc.get("visual_category") in BRAND_CATEGORIES]
    designated = [sc for sc in scenes if sc.get("photo_designated")]
    retrieved = [sc for sc in scenes if (sc.get("asset") or {}).get("kind") == "external"]
    licenses = {}
    for sc in retrieved:
        lic = str((sc.get("asset") or {}).get("license") or "?").lower()
        licenses[lic] = licenses.get(lic, 0) + 1
    procedural = [sc for sc in scenes if (sc.get("asset") or {}).get("kind") == "procedural"]
    brand_repo = [sc for sc in scenes if (sc.get("asset") or {}).get("origin") == "repository-assets"]
    brand_outside = [sc for sc in brand_repo if sc.get("visual_category") not in BRAND_CATEGORIES]
    pp = (plan or {}).get("photo_policy") or {}
    outcomes = pp.get("retrieval_outcomes") or {}
    # per-scene aggregate of the safe failure categories (also computable from
    # scenes when the plan predates the photo_policy block)
    if not outcomes:
        for sc in designated:
            r = sc.get("retrieval_reason") or "unknown"
            outcomes[r] = outcomes.get(r, 0) + 1
    degraded = bool(pp.get("degraded")) if "degraded" in pp else bool(
        designated) and not retrieved
    return {
        "scenes": len(scenes),
        "photo_designated": len(designated),
        "photos_retrieved": len(retrieved),
        "photo_fallbacks": sum(1 for sc in designated
                               if (sc.get("asset") or {}).get("kind") != "external"),
        "procedural_scenes": len(procedural),
        "brand_scenes": len(brand),
        "brand_repo_assets": len(brand_repo),
        "brand_accents_only": not brand_outside,
        "distinct_assets": len({(sc.get("asset") or {}).get("id") for sc in scenes}),
        "licenses": licenses,
        "licenses_ok": all(k in ALLOWED_EXTERNAL_LICENSES for k in licenses),
        "retrieval_outcomes": outcomes,
        # issue #32 §9: a zero-photo reel is not "fully visually complete" —
        # QA must surface this as an explicit human-review warning.
        "degraded": degraded,
    }


def plan_summary(plan):
    scenes = (plan or {}).get("scenes") or []
    aids = {sc.get("asset", {}).get("id") for sc in scenes}
    return {"scenes": len(scenes), "distinct_assets": len(aids),
            "categories": sorted({sc.get("visual_category") for sc in scenes}),
            "external": sum(1 for sc in scenes if sc.get("asset", {}).get("kind") == "external"),
            "photo_designated": sum(1 for sc in scenes if sc.get("photo_designated")),
            "procedural": sum(1 for sc in scenes if sc.get("asset", {}).get("kind") == "procedural"),
            "brand": sum(1 for sc in scenes if sc.get("visual_category") in BRAND_CATEGORIES),
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
    """Fraction of pixels whose blue channel dominates — the LEGACY raw cold
    inequality, kept as a comparative diagnostic only.

    The gate itself is ``palette_qa`` (issue #26): the raw inequality cannot
    distinguish H.264 chroma noise in near-black regions from a real
    blue/navy/cyan/purple object. This wrapper exists so the formula lives in
    exactly one module (build/palette_qa.py) and tests share that source.
    """
    import palette_qa
    return palette_qa.raw_cold_pixel_fraction(arr, tol)


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
    "attention-drift": "attention drift", "listen-vs-draft": "listening vs drafting",
    "refocus-steps": "pause-count-refocus", "reflect-loop": "pause-and-reflect loop",
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
