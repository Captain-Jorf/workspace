"""Content Producer — English-only, Technology × Metacognition, GitHub Models + Static Fallback.

- CONTENT_LANGUAGE=en fail-closed (via common.assert_content_language_en)
- No translator calls, no Persian fixture, no FA layer
- Output JSON schema: title, technology_angle, metacognition_concept, hook, scenes, narration, on_screen_text, visual_direction, actionable_technique, ending, caption, claims, sources
- 70-105s target max 120s, English conversational, strong hook 3s, no "In today's video", no filler, no fake stats, no medical claim, one main idea, one tech example, one technique
- Uses GitHubModelsProducer/Reviewer via build/llm_provider.py, with StaticEnglishFallback
- Evidence packet sanitized, untrusted web content must not inject prompt
- Max daily: 1 Producer, 1 Reviewer, if rejected max 1 Revision + final Reviewer
- On quota 429/outage limited retry then static fallback
- Metadata: language=en, generation_mode=github-models or static-fallback

usage:
  python3 build/content_producer.py --topic <topic.json> --out <epdir> [--variant 0|1]
"""

import argparse
import json
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import common
import llm_provider

HANDLE = "@metacognition.hq"

# English-only tech×metacognition playbooks (fallback only, filtered to tech domain)
# Each must have tech relevance, not general metacognition without tech use
PLAYBOOKS = {
    "automation-bias": {
        "pillar": "AI_JUDGMENT",
        "tags": ["automation-bias", "AI", "calibration"],
        "technology_angle": "automation bias in AI assistants",
        "metacognition_concept": "automation bias",
        "hook": "When does your AI assistant make you think less?",
        "problem": ["You ask AI for code, it gives you an answer instantly.", "It feels productive, and you move on without checking.", "The speed hides the need to verify."],
        "explain": ["Your brain treats the AI's fluency as your own understanding.", "That's automation bias: trusting the tool because it sounds confident.", "The more you use it, the less you verify.", "Fluency is not accuracy, but it feels like it."],
        "example": ["Think of the last time autocomplete finished your function.", "Did you read it line by line, or just accept it?", "Most people accept, because checking feels slower."],
        "technique": ["Try this: before you accept AI code, explain it out loud in one sentence.", "Then run one edge-case test yourself.", "If you can't explain it, you haven't learned it.", "Make that pause your new habit."],
        "ending": "Where did AI make you skip the thinking this week?",
        "cta_type": "share-experience",
        "web": ["ASK AI", "FLUENT", "BIAS", "CHECK", "EXPLAIN", "LEARN"],
        "claim_note": "Bansal et al. (2021) style findings on AI and human judgment, no numbers invented",
    },
    "hallucination-confidence": {
        "pillar": "AI_JUDGMENT",
        "tags": ["hallucination", "confidence", "calibration"],
        "technology_angle": "hallucination and confidence calibration in LLMs",
        "metacognition_concept": "confidence calibration",
        "hook": "Why does AI sound so sure, even when it's wrong?",
        "problem": ["AI answers in a calm, confident voice.", "That confidence feels like evidence, but it's just style.", "You trust the tone, not the facts."],
        "explain": ["Language models are trained to be fluent, not calibrated.", "Fluency and accuracy are two different skills.", "Your job is to add the calibration they don't have.", "Without you, fluent errors slip through."],
        "example": ["Ask AI for a library function that doesn't exist. It will invent one, confidently.", "Your brain wants to trust the fluency.", "That's when you need to pause and check."],
        "technique": ["Try this: whenever AI gives you a fact, ask 'how would I verify this in 30 seconds?'", "Open the docs, run the code, check one source.", "Make verification the habit, not trust.", "One quick check beats blind trust."],
        "ending": "What did AI state confidently that you had to correct recently?",
        "cta_type": "question",
        "web": ["CONFIDENT", "FLUENT", "NOT TRUE", "CHECK", "VERIFY", "CALIBRATE"],
    },
    "tutorial-hell": {
        "pillar": "LEARNING_TECH",
        "tags": ["tutorial-hell", "illusion-of-competence"],
        "technology_angle": "tutorial hell and illusion of competence in learning to code",
        "metacognition_concept": "illusion of competence",
        "hook": "You finished the tutorial. Can you build it from scratch?",
        "problem": ["Watching someone code feels like learning to code.", "Until you close the video and the blank file stares back."],
        "explain": ["Tutorials give you the answers before you feel the problem.", "That removes the struggle your memory needs to grow.", "Fluency while watching is not skill when building."],
        "example": ["You followed a React tutorial and it worked.", "Next day, try to recreate the same component without the video. Notice the gap."],
        "technique": ["Try this: after any tutorial, close it and rebuild one small piece from memory.", "When you get stuck, peek only for the next step, then close again.", "That peek is the real learning."],
        "ending": "Which tutorial left you feeling skilled until you tried alone?",
        "cta_type": "question",
        "web": ["WATCH", "FEELS EASY", "BLANK FILE", "GAP", "REBUILD", "LEARN"],
    },
    "cognitive-offloading": {
        "pillar": "HUMAN_AI",
        "tags": ["cognitive-offloading", "AI", "memory"],
        "technology_angle": "cognitive offloading to AI and memory",
        "metacognition_concept": "cognitive offloading",
        "hook": "If AI remembers everything, what does your brain stop doing?",
        "problem": ["You used to remember the function signature.", "Now you ask AI every time, and it works, so why bother?", "The shortcut works, so the memory never gets used."],
        "explain": ["Offloading is useful: it saves mental effort for harder problems.", "But your brain learns what you practice recalling.", "If you never recall it, the memory quietly fades.", "Useful offloading is a choice, not a default."],
        "example": ["Think of phone numbers: you stopped memorizing them when your phone did.", "Same thing happens with code patterns you always ask AI for.", "You recognize them, but can't write them."],
        "technique": ["Try this: keep a 'no-AI' list of 10 things you want to remember.", "For those, recall first, then check AI.", "For everything else, offload happily.", "That list is your memory insurance."],
        "ending": "What have you offloaded so much you can't do without AI anymore?",
        "cta_type": "question",
        "web": ["OFFLOAD", "SAVES EFFORT", "MEMORY", "FADES", "RECALL", "CHOOSE"],
    },
    "planning-fallacy": {
        "pillar": "PRODUCT",
        "tags": ["planning-fallacy", "estimation", "software"],
        "technology_angle": "planning fallacy in software estimation",
        "metacognition_concept": "planning fallacy",
        "hook": "Why does every software task take twice as long as you promised?",
        "problem": ["You plan the sprint and it looks doable. Even relaxed.", "Then Thursday arrives and half the code tickets are still open."],
        "explain": ["When we plan software, we imagine the best-case version: focused, healthy, uninterrupted coding.", "We forget interruptions because they aren't part of the product story we tell.", "That's the planning fallacy in software engineering, and it's reliable."],
        "example": ["Think about the last feature you estimated. Your guess before, and the real time after.", "The gap almost always points the same way in software teams."],
        "technique": ["Try this: before you estimate, ask how long similar software tasks took last time.", "Use that number, not the hopeful one.", "Then add the interruptions you already know will come."],
        "ending": "What software task did you finish on time this month? Anything?",
        "cta_type": "question",
        "web": ["PLAN", "BEST CASE", "REALITY", "LAST TIME", "BUFFER", "DONE"],
    },
    "confirmation-bias-research": {
        "pillar": "PRODUCT",
        "tags": ["confirmation-bias", "user-research"],
        "technology_angle": "confirmation bias in product and user research for software",
        "metacognition_concept": "confirmation bias",
        "hook": "Your user interviews proved you right. Did they?",
        "problem": ["You had a product hypothesis and you went looking for evidence in user research.", "You found it, because you were looking for it in software feedback."],
        "explain": ["Confirmation bias means we notice what fits our idea and ignore what doesn't.", "In product user research, the question you ask shapes the answer you get.", "You hear 'yes' because you asked in a way that invites yes."],
        "example": ["You ask 'Would you use this software feature?' and they say 'sure, maybe.'", "That's not evidence for product decisions. That's politeness."],
        "technique": ["Try this: before any product interview, write what would prove you wrong.", "Then ask questions that could give you that answer.", "If you can't be proven wrong, you're not doing research."],
        "ending": "When did you last seek evidence that you were wrong in product research?",
        "cta_type": "question",
        "web": ["HYPOTHESIS", "SEARCH", "YES", "BIAS", "PROVE WRONG", "LEARN"],
    },
    "goodhart-metrics": {
        "pillar": "PRODUCT",
        "tags": ["goodhart", "metrics", "product"],
        "technology_angle": "Goodhart's law in product metrics",
        "metacognition_concept": "Goodhart's law",
        "hook": "When your metric became the target, what did your team stop measuring?",
        "problem": ["You set a metric: daily active users, story points, lines of code.", "People hit the metric, and the product got worse.", "The metric was hit, but the goal was missed."],
        "explain": ["Goodhart's law: when a measure becomes a target, it stops being a good measure.", "People optimize for what you measure, not what you meant.", "The metric becomes the game.", "And the game replaces the work."],
        "example": ["You measure PRs merged, so PRs get smaller and more trivial.", "The number goes up. Learning doesn't.", "You optimized the number, not the outcome."],
        "technique": ["Try this: for every metric you track, write what it could make people fake.", "Then add one counter-metric that catches the fake.", "If you can't name the counter, don't ship the metric.", "That counter keeps the metric honest."],
        "ending": "Which metric in your team is being gamed right now?",
        "cta_type": "question",
        "web": ["METRIC", "TARGET", "GAME", "FAKE", "COUNTER", "REAL"],
    },
    "context-switching": {
        "pillar": "ATTENTION",
        "tags": ["context-switching", "notifications", "attention"],
        "technology_angle": "context switching and notifications",
        "metacognition_concept": "attention and task switching",
        "hook": "That notification just cost you twenty minutes. Not one.",
        "problem": ["You check Slack for a second and you're still reading about it twenty minutes later.", "Each small tap restarts your focus from zero."],
        "explain": ["Your attention runs on cues, not on plans.", "A notification is a cue that hijacks the plan.", "The cost isn't the look. It's the minutes to get back."],
        "example": ["You were coding, a notification popped, you answered, and the variable name you held in mind is gone.", "You have to reload the whole context."],
        "technique": ["Try this: batch notifications into two windows a day.", "When a topic pulls at you, write it on a note: I'll read at six.", "Half the time, by six you won't care."],
        "ending": "What pulled your focus today that you never actually chose?",
        "cta_type": "question",
        "web": ["CUE", "TAP", "RESTART", "NOTE IT", "LATER", "CHOOSE"],
    },
    "sunk-cost-architecture": {
        "pillar": "PRODUCT",
        "tags": ["sunk-cost", "architecture"],
        "technology_angle": "sunk cost in software architecture decisions",
        "metacognition_concept": "sunk cost fallacy",
        "hook": "You know this software architecture is wrong. So why are you still defending it?",
        "problem": ["You spent three months building this code architecture.", "Throwing it away feels like throwing away the work in your product."],
        "explain": ["Sunk cost fallacy: we keep investing in software because we've already invested.", "The code work is already gone. The question is only: what helps your product from now on?", "Past cost should not decide future cost in software engineering."],
        "example": ["You built a microservice that now costs more than it saves.", "You keep it because 'we already built it', not because it helps your product code."],
        "technique": ["Try this: ask 'If we hadn't built this code, would we build it now?'", "If the answer is no, write the cost of keeping this software for six more months.", "Then decide from there, not from the past."],
        "ending": "What software architecture are you keeping only because you built it?",
        "cta_type": "question",
        "web": ["BUILT", "COST", "KEEP", "FALLACY", "NOW?", "DECIDE"],
    },
    "metacognition-debugging": {
        "pillar": "CODING",
        "tags": ["debugging", "metacognition"],
        "technology_angle": "metacognition in debugging",
        "metacognition_concept": "metacognitive monitoring",
        "hook": "The bug isn't in the code. It's in how you're looking at the code.",
        "problem": ["You've stared at the same function for an hour.", "The more you look, the less you see."],
        "explain": ["Debugging needs two minds: one that writes, one that watches.", "When you're stuck, you're usually running the same mental path.", "Metacognition is noticing that path and choosing a different one."],
        "example": ["You assume the bug is in the new code, so you never check the old config.", "Your assumption is the bug."],
        "technique": ["Try this: when stuck for 20 minutes, explain the bug to a rubber duck out loud.", "Say what you know, what you assume, and what you haven't checked.", "The gap you hear is where the bug lives."],
        "ending": "What bug taught you to doubt your first assumption?",
        "cta_type": "share-experience",
        "web": ["STUCK", "SAME PATH", "WATCHER", "ASSUME", "EXPLAIN", "FOUND"],
    },
    "learning-code-with-ai": {
        "pillar": "LEARNING_TECH",
        "tags": ["learning", "AI", "coding"],
        "technology_angle": "learning to code with AI without losing skill",
        "metacognition_concept": "desirable difficulties",
        "hook": "AI can write the code, but can you learn from it?",
        "problem": ["You ask AI, it writes it, you paste it, it works.", "Next week, you can't write it without AI."],
        "explain": ["Learning needs some difficulty: recalling, mixing, waiting before you review.", "AI removes that difficulty, so it feels good but teaches less.", "Smooth practice feels like progress. Rough practice is progress."],
        "example": ["Two students: one rereads AI code, one closes it and tries to rebuild it from memory.", "Next week, who explains it better?"],
        "technique": ["Try this: after AI gives you code, close it and write the logic in plain English.", "Then rebuild the code from your English.", "If you can't, you didn't learn, you just copied."],
        "ending": "What's one thing you learned to do without AI this month?",
        "cta_type": "question",
        "web": ["AI WRITES", "FEELS EASY", "RECALL", "REBUILD", "STRUGGLE", "LEARN"],
    },
    "deskilling-autocomplete": {
        "pillar": "CODING",
        "tags": ["deskilling", "autocomplete", "AI"],
        "technology_angle": "deskilling through AI autocomplete",
        "metacognition_concept": "skill decay and monitoring",
        "hook": "Autocomplete makes you faster today, and slower next year. Why?",
        "problem": ["You used to know the syntax by heart.", "Now you wait for the suggestion, and accept.", "The suggestion is faster, so you stop trying."],
        "explain": ["Skill is kept by retrieval: pulling it from memory.", "Autocomplete replaces retrieval with recognition.", "Recognition is easier, but it doesn't keep the skill alive.", "Easy now means weak later."],
        "example": ["You used to write regex from memory.", "Now you ask AI, and next month you can't write it at all.", "You recognize the pattern, but can't produce it."],
        "technique": ["Try this: one hour a week, code with autocomplete off.", "Write the hard parts from memory, then check.", "That hour is your skill insurance.", "One hour keeps the skill alive."],
        "ending": "What skill are you losing because AI does it for you?",
        "cta_type": "question",
        "web": ["FAST NOW", "SLOW LATER", "RECALL", "RECOGNIZE", "OFF", "KEEP"],
    },
}

EXTRA = {
    "automation-bias": {"bridge": "It's not laziness. It's your brain trusting fluency over evidence.", "recap": ["So: fluency is not understanding.", "Explain before you accept, test one edge case, and keep the watcher on."]},
    "hallucination-confidence": {"bridge": "That's not lying. It's fluency without calibration.", "recap": ["So: confident language is not confident knowledge.", "Ask how you'd verify in 30 seconds, and make that the habit."]},
    "tutorial-hell": {"bridge": "It's not that you're bad at coding. You're practicing recognition, not recall.", "recap": ["So: close the tutorial, rebuild from memory, peek only for the next step.", "The struggle you feel is the learning."]},
    "cognitive-offloading": {"bridge": "Offloading is smart, until you offload the skill you want to keep.", "recap": ["So: choose what to remember, recall before you ask, offload the rest.", "Your brain keeps what you practice."]},
    "planning-fallacy": {"bridge": "It's not laziness. It's a bias so reliable it has a name.", "recap": ["So: your gut plans the movie version. Your history knows the real one.", "Estimate from history, not hope."]},
    "confirmation-bias-research": {"bridge": "It's not dishonesty. It's your brain protecting the first idea.", "recap": ["So: write what would prove you wrong before you start.", "If you can't be proven wrong, it's not research."]},
    "goodhart-metrics": {"bridge": "The metric didn't fail. It succeeded at being gamed.", "recap": ["So: every metric needs a counter-metric that catches the fake.", "If you can't name it, don't ship it."]},
    "context-switching": {"bridge": "That's how a small tap becomes a lost hour without a decision.", "recap": ["So: write it down, give it a time, let your attention choose later on purpose."]},
    "sunk-cost-architecture": {"bridge": "The work is already gone. Only future cost matters.", "recap": ["So: ask if you'd build it now, write the cost of keeping it, decide from there."]},
    "metacognition-debugging": {"bridge": "The fix isn't more looking. It's looking differently.", "recap": ["So: when stuck, explain what you know, assume, and haven't checked.", "The watcher finds the bug your eyes missed."]},
    "learning-code-with-ai": {"bridge": "AI removes the difficulty your memory needs to grow.", "recap": ["So: after AI writes, close it, explain in English, rebuild from memory.", "Smooth feels good. Rough teaches."]},
    "deskilling-autocomplete": {"bridge": "That's how a shortcut becomes a skill you lose.", "recap": ["So: one hour a week, autocomplete off, write from memory.", "Recognition is easy. Recall keeps the skill."]},
}

for k, e in EXTRA.items():
    if k in PLAYBOOKS:
        PLAYBOOKS[k]["bridge"] = e["bridge"]
        PLAYBOOKS[k]["recap"] = e["recap"]

# calendar id → playbook key (tech-filtered)
CALENDAR_MAP = {
    2: "automation-bias",
    3: "planning-fallacy",
    4: "tutorial-hell",
    6: "cognitive-offloading",
    7: "learning-code-with-ai",
    9: "planning-fallacy",
    10: "hallucination-confidence",
    11: "deskilling-autocomplete",
    12: "context-switching",
    14: "confirmation-bias-research",
    15: "goodhart-metrics",
    16: "hallucination-confidence",
    18: "sunk-cost-architecture",
    19: "metacognition-debugging",
    20: "automation-bias",
    21: "metacognition-debugging",
    23: "context-switching",
    24: "cognitive-offloading",
    25: "tutorial-hell",
    26: "hallucination-confidence",
    29: "hallucination-confidence",
    30: "learning-code-with-ai",
    31: "confirmation-bias-research",
}

# Trend lenses — tech-only policy
TREND_LENSES = {
    "AI_JUDGMENT": {
        "hook": "{short} is everywhere right now. But how sure should you be when AI sounds sure?",
        "problem": ["When a tech topic gets loud, confidence goes up faster than knowledge.", "You hear strong takes all day and start to feel you have one too."],
        "explain": ["Your brain reads fluency as truth. Hear a confident AI answer often enough and it feels obvious.", "That feeling is automation bias, not knowledge.", "So the loudest week is when your judgment is least reliable."],
        "example": ["Take {short}. Ask: what would I have said a month ago?", "If the answer is nothing, your new certainty came from repetition, not checking."],
        "technique": ["Try this: before you repeat any hot take, give it a confidence number out loud.", "Then name one thing that would change your mind.", "If you can't name it, you don't have an opinion yet. You have a headline."],
        "bridge": "The topic isn't the problem. The speed of your certainty is.",
        "recap": ["So: repetition makes things feel true, and a loud week is when that feeling peaks.", "Ask how sure you are before you repeat anything."],
        "ending": "What's the last strong opinion about AI you picked up without checking?",
        "cta_type": "share-experience",
        "web": ["HEADLINE", "REPEAT", "FEELS TRUE", "HOW SURE?", "WHAT CHANGES?", "OPINION"],
        "tags": ["trend", "AI", "calibration"],
    },
    "CODING": {
        "hook": "Everyone's suddenly coding with {short}. Most will forget how it works by Friday.",
        "problem": ["Watching someone build with {short} feels like learning it.", "But recognizing code and writing it are two different skills."],
        "explain": ["When you watch, the solution is in front of you, so everything feels clear.", "Close the tab and that clarity disappears, because you never had to produce it.", "Understanding appears when you rebuild, not when you nod."],
        "example": ["Try it with {short}. Close this video and explain how you'd build it in three sentences.", "Notice where you stall. That gap was invisible a minute ago."],
        "technique": ["Try this: after any tutorial on {short}, close it and rebuild one small piece from memory.", "Fix only what you got wrong.", "One round of that beats ten more videos."],
        "bridge": "That's not a memory problem. It's a testing problem: you never checked.",
        "recap": ["So: watch, close, rebuild, fix. One round.", "The gap you find is worth more than the next video."],
        "ending": "Which part of {short} could you actually build right now?",
        "cta_type": "try-it",
        "web": ["WATCH", "FEELS CLEAR", "CLOSE", "REBUILD", "GAP", "FIX"],
        "tags": ["trend", "coding", "learning"],
    },
}

GENERIC_LENS = {
    "hook": "Everyone's talking about {short}. But does it make you think better, or just faster?",
    "problem": ["A new tool spreads because it's interesting, not because it's checked.", "By the time you've formed an opinion, you rarely remember where it came from."],
    "explain": ["Your brain treats a fluent tool as a smart tool.", "Hear it three times and it starts to feel obvious.", "That feeling is fluency, not understanding."],
    "example": ["Take {short}. Ask who measured its benefit, who benefits from you believing it, and what would prove it wrong.", "Usually at least one answer is missing, and that's the part worth noticing."],
    "technique": ["Try this before you adopt anything: pause for ten seconds.", "Name one task you'd still do without it, and why.", "If you can't, you're holding hype, not a tool."],
    "bridge": "That's how a headline quietly becomes your workflow.",
    "recap": ["So: pause, name what you'd keep without it, name one way it could fail.", "If you can't, you're holding hype, not a tool."],
    "ending": "What tool did you adopt without checking what it makes you worse at?",
    "cta_type": "question",
    "web": ["HEADLINE", "REPEAT", "FEELS TRUE", "KEEP", "FAIL?", "TOOL"],
    "tags": ["trend", "tech", "metacognition"],
}

def short_title(title, limit=44):
    import re
    t = re.sub(r"\s+", " ", title or "").strip().rstrip(".?!")
    t = re.sub(r'^[\"\'“”‘’]+|[\"\'“”‘’]+$', "", t)
    STOP_LEAD = re.compile(r"^(show hn|ask hn|tell hn|launch hn|new study|a new study|study|research|paper|opinion|analysis|breaking|i built|we built|i made|we made|i wrote|introducing|why|how|what|when|where|the|a|an|is|are|do|does|did|can|should|could|would|will)\b[:\s-]*", re.I)
    for _ in range(3):
        t2 = STOP_LEAD.sub("", t)
        if t2 == t:
            break
        t = t2
    CUT_AT = re.compile(r"\s+(?:is|are|was|were|and why|and how|and the|because|but|that|which|who|when|while|so that|explained|—|–|-|:|;|,|\(|\[)\s+", re.I)
    m = CUT_AT.search(t)
    while m and len(t[:m.start()].split()) < 2:
        m = CUT_AT.search(t, m.end())
    if m:
        t = t[:m.start()]
    t = re.split(r"[:;(\[]", t)[0].strip(" -–—,")
    if len(t.split()) > 5:
        CUT_LATE = re.compile(r"\s+(?:to|for|in|on|at|with|without|from|by|of|and)\s+", re.I)
        m = CUT_LATE.search(t)
        while m and len(t[:m.start()].split()) < 3:
            m = CUT_LATE.search(t, m.end())
        if m:
            t = t[:m.start()]
    if len(t) > limit:
        t = t[:limit].rsplit(" ", 1)[0]
    words = t.split()
    if not words:
        words = (title or "this topic").split()[:4]
    return " ".join(words)

def cap_first(s):
    return s[:1].upper() + s[1:] if s else s

def trend_playbook(topic_title, pillar):
    lens = TREND_LENSES.get(pillar, GENERIC_LENS)
    short = short_title(topic_title)
    pb = {"pillar": pillar, "claim_note": "trend topic: tech angle, no fake stats"}
    for k, v in lens.items():
        if isinstance(v, str):
            pb[k] = cap_first(v.format(short=short))
        elif k in ("web", "tags", "cta_type"):
            pb[k] = list(v) if isinstance(v, list) else v
        else:
            pb[k] = [cap_first(x.format(short=short)) for x in v]
    return pb

SCENE_FOR_BEAT = {"hook": "hook", "problem": "problem", "explain": "explain", "example": "example", "technique": "technique", "ending": "ending"}

def lines_for(pb, beat):
    v = pb[beat]
    return [v] if isinstance(v, str) else list(v)

def chunk_plan(pb, variant=0):
    plan = []
    plan.append(("hook", lines_for(pb, "hook")))
    prob = lines_for(pb, "problem") + ([pb["bridge"]] if pb.get("bridge") else [])
    plan.append(("problem", prob))
    ex = lines_for(pb, "explain")
    if len(ex) >= 3:
        plan.append(("explain", ex[:2]))
        plan.append(("explain", ex[2:]))
    else:
        plan.append(("explain", ex))
    plan.append(("example", lines_for(pb, "example")))
    te = lines_for(pb, "technique")
    if len(te) >= 3:
        plan.append(("technique", te[:2]))
        plan.append(("technique", te[2:]))
    else:
        plan.append(("technique", te))
    if pb.get("recap"):
        plan.append(("ending", list(pb["recap"])))
    plan.append(("ending", lines_for(pb, "ending")))
    return plan

def spoken_form(text, overrides):
    s = text
    for k, v in sorted(overrides.items(), key=lambda kv: -len(kv[0])):
        s = s.replace(k, v)
    s = re.sub(r"https?://\S+", "the link", s)
    return s

def build_script_from_playbook(topic, pol, pb, playbook_key, variant=0, generation_mode="static-fallback"):
    """Build script.json from a playbook (fallback) — English-only."""
    plan = chunk_plan(pb, variant)
    overrides = pol.get("tts", {}).get("pronunciation_overrides", {})
    chunks = []
    all_en = []
    for i, (beat, lines) in enumerate(plan, 1):
        en = [{"t": l, "scene": SCENE_FOR_BEAT[beat], "beat": beat} for l in lines]
        chunks.append({"id": f"c{i}", "beat": beat, "en": en, "fa": [], "tts_text": " ".join(spoken_form(l, overrides) for l in lines)})
        all_en += lines

    tag = topic["content_date"]
    sources = []
    cal = topic.get("calendar") or {}
    if cal:
        for s in cal.get("sources", [])[:3]:
            sources.append({"label": s, "url": "", "tier": "A", "role": "evidence"})
    disc = topic.get("discovery_source") or {}
    if disc.get("url"):
        sources.append({"label": disc.get("name", "discovery"), "url": disc["url"], "tier": disc.get("tier", "C"), "role": "discovery"})

    hashtag_pool = {
        "AI_JUDGMENT": ["#AI", "#metacognition", "#calibration"],
        "CODING": ["#coding", "#debugging", "#metacognition"],
        "LEARNING_TECH": ["#learningtocode", "#AI", "#metacognition"],
        "PRODUCT": ["#productmanagement", "#startup", "#metacognition"],
        "ATTENTION": ["#focus", "#deepwork", "#metacognition"],
        "HUMAN_AI": ["#AI", "#humanAI", "#metacognition"],
    }
    tags = list(dict.fromkeys(pol["hashtag_policy"]["always"] + hashtag_pool.get(pb["pillar"], [])[:3] + ["#cognitivescience"]))[:pol["hashtag_policy"]["max"]]

    caption = {
        "hook": pb["hook"],
        "intro": " ".join(lines_for(pb, "problem")),
        "sections": [
            {"icon": "🧠", "title": "WHAT'S GOING ON", "lines": lines_for(pb, "explain")},
            {"icon": "✦", "title": "TRY THIS", "lines": lines_for(pb, "technique")},
        ],
        "sources": [s["label"] + (f" — {s['url']}" if s["url"] else "") for s in sources],
        "ctas": [pb["ending"]],
        "hashtags": tags,
    }

    script = {
        "meta": {
            "title": f"Auto reel {tag} — {topic['title']}",
            "topic": topic["title"],
            "content_id": topic["content_id"],
            "content_date": tag,
            "pillar": pb["pillar"],
            "technology_angle": pb.get("technology_angle", "automation bias in AI assistants"),
            "metacognition_concept": pb.get("metacognition_concept", pb["pillar"]),
            "playbook": playbook_key,
            "variant": variant,
            "tags": pb.get("tags", []),
            "cta_type": pb.get("cta_type", "question"),
            "evidence_mode": topic.get("evidence_mode"),
            "claim_note": pb.get("claim_note", ""),
            "language": "en",
            "content_language": "en",
            "generation_mode": generation_mode,
            "note": topic.get("fallback_reason", ""),
            "handle": HANDLE,
            "fps": 30, "w": 1080, "h": 1920, "gap": 0.34, "lead": 0.55, "tail": 1.2,
            "logo": "stand-in",
        },
        "scene_tags": {"hook": "01 · THE QUESTION", "problem": "02 · THE PROBLEM", "explain": "03 · WHAT'S GOING ON", "example": "04 · IN REAL LIFE", "technique": "05 · TRY THIS", "ending": "06 · YOUR TURN"},
        "web": pb["web"],
        "sources": sources,
        "chunks": chunks,
        "caption": caption,
        "visual_direction": pb.get("technology_angle", "") + " + code visual + confidence meter + human-AI network",
    }
    return script

def build_script_from_llm(topic, pol, llm_output, generation_mode="github-models"):
    """Build script.json from LLM producer output — English-only, validates schema."""
    # Validate required fields
    required = ["title", "technology_angle", "metacognition_concept", "hook", "scenes", "narration", "on_screen_text", "visual_direction", "actionable_technique", "ending"]
    for f in required:
        if f not in llm_output:
            raise ValueError(f"LLM output missing required field: {f}")

    # Language check: no Persian characters
    combined = json.dumps(llm_output, ensure_ascii=False)
    if common.persian_ratio(combined) > 0.05:
        raise ValueError("LLM output contains Persian characters — English-only required")

    # Build chunks from narration (dict or list)
    narration = llm_output["narration"]
    if isinstance(narration, str):
        # Split into beats heuristically
        narration_dict = {"hook": [llm_output["hook"]], "problem": [narration], "explain": [narration], "example": [narration], "technique": [llm_output["actionable_technique"]], "ending": [llm_output["ending"]]}
    elif isinstance(narration, dict):
        narration_dict = {}
        for beat in ["hook", "problem", "explain", "example", "technique", "ending"]:
            v = narration.get(beat, [])
            if isinstance(v, str):
                v = [v]
            narration_dict[beat] = v
    else:
        raise ValueError("narration must be string or dict")

    overrides = pol.get("tts", {}).get("pronunciation_overrides", {})
    chunks = []
    idx = 1
    for beat in ["hook", "problem", "explain", "example", "technique", "ending"]:
        lines = narration_dict.get(beat, [])
        if not lines:
            continue
        en = [{"t": l, "scene": beat, "beat": beat} for l in lines]
        chunks.append({"id": f"c{idx}", "beat": beat, "en": en, "fa": [], "tts_text": " ".join(spoken_form(l, overrides) for l in lines)})
        idx += 1

    tag = topic["content_date"]
    sources = llm_output.get("sources", []) or []
    # Ensure sources have tier
    for s in sources:
        if "tier" not in s:
            s["tier"] = "B"

    # Caption from LLM or build
    caption_in = llm_output.get("caption", {})
    if isinstance(caption_in, dict):
        caption = {
            "hook": caption_in.get("hook", llm_output["hook"]),
            "intro": caption_in.get("intro", " ".join(narration_dict.get("problem", []))),
            "sections": caption_in.get("sections", [
                {"title": "WHAT'S GOING ON", "lines": narration_dict.get("explain", [])},
                {"title": "TRY THIS", "lines": narration_dict.get("technique", [])},
            ]),
            "sources": [s.get("label","") for s in sources],
            "ctas": [llm_output["ending"]],
            "hashtags": caption_in.get("hashtags", ["#metacognition", "#AI", "#coding"]),
        }
    else:
        caption = {
            "hook": llm_output["hook"],
            "intro": " ".join(narration_dict.get("problem", [])),
            "sections": [
                {"title": "WHAT'S GOING ON", "lines": narration_dict.get("explain", [])},
                {"title": "TRY THIS", "lines": narration_dict.get("technique", [])},
            ],
            "sources": [s.get("label","") for s in sources],
            "ctas": [llm_output["ending"]],
            "hashtags": ["#metacognition", "#AI", "#coding"],
        }

    # Hashtag limits
    max_tags = pol["hashtag_policy"]["max"]
    if len(caption.get("hashtags", [])) > max_tags:
        caption["hashtags"] = caption["hashtags"][:max_tags]

    script = {
        "meta": {
            "title": llm_output.get("title", f"Auto reel {tag} — {topic['title']}"),
            "topic": topic["title"],
            "content_id": topic["content_id"],
            "content_date": tag,
            "pillar": topic.get("pillar", "AI_JUDGMENT"),
            "technology_angle": llm_output.get("technology_angle", "automation bias in AI assistants"),
            "metacognition_concept": llm_output.get("metacognition_concept", "automation bias"),
            "playbook": "llm-generated",
            "variant": 0,
            "tags": [llm_output.get("metacognition_concept", ""), llm_output.get("technology_angle", "")],
            "cta_type": "question",
            "evidence_mode": topic.get("evidence_mode"),
            "claim_note": "",
            "language": "en",
            "content_language": "en",
            "generation_mode": generation_mode,
            "handle": HANDLE,
            "fps": 30, "w": 1080, "h": 1920, "gap": 0.34, "lead": 0.55, "tail": 1.2,
            "logo": "stand-in",
        },
        "scene_tags": {"hook": "01 · THE QUESTION", "problem": "02 · THE PROBLEM", "explain": "03 · WHAT'S GOING ON", "example": "04 · IN REAL LIFE", "technique": "05 · TRY THIS", "ending": "06 · YOUR TURN"},
        "web": llm_output.get("on_screen_text", ["ASK AI", "FLUENT", "BIAS", "CHECK", "EXPLAIN", "LEARN"])[:6],
        "sources": sources,
        "chunks": chunks,
        "caption": caption,
        "visual_direction": llm_output.get("visual_direction", "code visual + confidence meter"),
        "claims": llm_output.get("claims", []),
    }
    return script

def main():
    common.assert_content_language_en()
    ap = argparse.ArgumentParser()
    ap.add_argument("--topic", required=True)
    ap.add_argument("--out", required=True, help="episode dir")
    ap.add_argument("--variant", type=int, default=0)
    ap.add_argument("--policy", default=None)
    a = ap.parse_args()
    pol = common.policy(a.policy)
    topic = common.load_json(a.topic)
    if not topic:
        raise SystemExit("[producer] topic.json missing — trend-error")

    # Build evidence packet (sanitized)
    recent = common.recent_entries(common.load_memory(), 10)
    recent_topics = [e.get("topic","") for e in recent]
    evidence_packet = llm_provider.build_evidence_packet(topic, pol, recent_topics)

    producer_mode = os.environ.get("CONTENT_PRODUCER", "github-models")
    fallback_mode = os.environ.get("CONTENT_FALLBACK", "static-english")

    script = None
    generation_mode = "static-fallback"
    producer_report = {}
    reviewer_report = {}

    # Try GitHub Models Producer if requested
    if producer_mode == "github-models":
        try:
            prod = llm_provider.GitHubModelsProducer()
            llm_out, raw = prod.produce(evidence_packet)
            # Honesty gate: mock/fixture output must NEVER be recorded as
            # generation_mode=github-models. That label is reserved for genuine
            # API responses. Mock in the daily path falls back to static.
            if isinstance(raw, dict) and raw.get("mock"):
                raise ValueError("mock output rejected in daily path — explicit mock is test-only")
            producer_report = {"model": prod.model, "raw": raw, "output": llm_out, "mode": "github-models"}
            # Validate and build script
            script = build_script_from_llm(topic, pol, llm_out, generation_mode="github-models")
            generation_mode = "github-models"

            # Reviewer step
            try:
                reviewer_mode = os.environ.get("CONTENT_REVIEWER", "github-models")
                if reviewer_mode == "github-models":
                    rev = llm_provider.GitHubModelsReviewer()
                    review_out, review_raw = rev.review(llm_out, evidence_packet)
                    reviewer_report = {"model": rev.model, "raw": review_raw, "output": review_out}
                    # If rejected, try one revision
                    if not review_out.get("approved", False) or review_out.get("score", 0) < 85:
                        # One revision attempt
                        if review_out.get("required_changes"):
                            evidence_packet["revision_request"] = review_out["required_changes"]
                            try:
                                llm_out2, raw2 = prod.produce(evidence_packet)
                                if isinstance(raw2, dict) and raw2.get("mock"):
                                    raise ValueError("mock revision rejected in daily path")
                                review_out2, review_raw2 = rev.review(llm_out2, evidence_packet)
                                if isinstance(review_raw2, dict) and review_raw2.get("mock"):
                                    raise ValueError("mock reviewer output rejected in daily path")
                                if review_out2.get("approved") and review_out2.get("score",0) >=85 and review_out2.get("technology_relevance") and review_out2.get("metacognition_relevance"):
                                    script = build_script_from_llm(topic, pol, llm_out2, generation_mode="github-models")
                                    reviewer_report = {"model": rev.model, "raw": review_raw2, "output": review_out2, "revision": True}
                                else:
                                    # Second rejection → fallback
                                    raise ValueError(f"Reviewer rejected after revision: {review_out2}")
                            except Exception as e_rev:
                                print(f"[producer] reviewer second rejection, falling back: {e_rev}")
                                script = None
                        else:
                            script = None
            except Exception as e:
                print(f"[producer] reviewer error, will fallback if needed: {e}")

        except Exception as e:
            print(f"[producer] GitHub Models producer failed: {e} — trying fallback")
            script = None
            producer_report["error"] = str(e)

    # Fallback to static English if needed
    if script is None:
        cal = topic.get("calendar") or {}
        key = CALENDAR_MAP.get(cal.get("id")) if cal else None
        if key and key in PLAYBOOKS:
            pb = PLAYBOOKS[key]
            playbook_key = key
        elif topic.get("evidence_mode") == "calendar":
            # Fallback to first available tech playbook
            playbook_key = next(iter(PLAYBOOKS))
            pb = PLAYBOOKS[playbook_key]
        else:
            # Trend fallback: use trend lens if pillar maps, else generic
            pb = trend_playbook(topic["title"], topic.get("pillar", "AI_JUDGMENT"))
            playbook_key = "trend"
        script = build_script_from_playbook(topic, pol, pb, playbook_key, variant=a.variant, generation_mode="static-fallback")
        generation_mode = "static-fallback"
        if not producer_report:
            producer_report = {"mode": "static-fallback", "playbook": playbook_key}

    os.makedirs(a.out, exist_ok=True)
    common.save_json(os.path.join(a.out, "script.json"), script)
    # Also save producer/reviewer reports for QA and final report
    common.save_json(os.path.join(a.out, "producer_report.json"), producer_report)
    if reviewer_report:
        common.save_json(os.path.join(a.out, "reviewer_report.json"), reviewer_report)

    words = sum(common.word_count(l["t"]) for ch in script["chunks"] for l in ch["en"])
    print(f"[producer] script → {a.out}/script.json  playbook={script['meta']['playbook']} chunks={len(script['chunks'])} words={words} mode={generation_mode} lang=en")

if __name__ == "__main__":
    main()
