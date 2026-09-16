"""Content Producer — English-only, Technology × Metacognition, Groq + Static Fallback.

- CONTENT_LANGUAGE=en fail-closed (via common.assert_content_language_en)
- No translator calls, no Persian fixture, no FA layer
- Output JSON schema: title, technology_angle, metacognition_concept, hook, scenes, narration, on_screen_text, visual_direction, actionable_technique, ending, caption, claims, sources
- 70-105s target max 120s, English conversational, strong hook 3s, no "In today's video", no filler, no fake stats, no medical claim, one main idea, one tech example, one technique
- Uses GroqProducer/Reviewer via build/llm_provider.py, with StaticEnglishFallback
- Evidence packet sanitized, untrusted web content must not inject prompt
- Max daily: 1 Producer, 1 Reviewer, if rejected max 1 Revision + final Reviewer
- Deterministic PRE-RENDER script gate (reel-2026-09-17 fix): the ACTUAL spoken words
  are counted with the QA single-sourced logic (common.spoken_word_count, never a
  model-reported count) and must sit in 150-260 with a duration preflight (estimated
  from the configured narration rate) safe for the 60-120 s render. An out-of-range
  Producer output goes through the ONE allowed Revision with explicit instructions
  (one main idea, one actionable technique, conversational English, 175-210 words);
  if the Revision was used and it is still out of range, the Static English Fallback
  is VALIDATED and used; if even the fallback fails, the producer skips BEFORE
  rendering (exit 3, no script.json). Structured reviewer approval never overrides
  the gate. No padding, no extra retries, no paid services.
- Issue #22 extension: the pre-render gate is now the FULL deterministic TEXT QA
  gate — it runs qa_supervisor.pre_render_text_gate (the exact final-QA blocker
  functions: source-quality claim words + unsupported statistics, English-only,
  banned English terms, script-beat/hook/word blockers, technology, metacognition,
  topic relevance, caption/hashtag blockers, structured Reviewer contradictions)
  plus an invented-citation guard (source URLs must exist in the sanitized packet).
  Calendar evidence sources are grounded into the LLM script so the QA tier
  matcher sees what the packet actually provides — tiers are DERIVED, never
  trusted from a model-claimed field, and nothing (citation, URL, author, stat or
  tier) is ever invented to make a blocker pass.
- On quota 429 (Retry-After honored, max 2 retries) / auth / outage → static fallback
- Metadata: language=en, generation_mode=groq or static-fallback

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
import qa_supervisor as qa

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
        "example": ["Think of the last time autocomplete finished your function.", "Did you read it line by line, or just accept it?", "Most people accept, because checking feels slower.", "If it never surprised you, you probably never checked it."],
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
        "example": ["Ask AI for a library function that doesn't exist. It will invent one, confidently.", "Your brain wants to trust the tone instead of understanding.", "You'll ship the invented name, and the traceback will find it.", "You can't know which answers are right just by how sure they sound.", "That's when you need to pause and check."],
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
        "explain": ["Tutorials give you the answers before you feel the problem.", "That removes the struggle your memory needs to grow.", "Fluency while watching is not skill when building.", "Recognition is not recall, and the video keeps recognizing for you."],
        "example": ["You followed a React tutorial and it worked.", "Next day, try to recreate the same component without the video. Notice the gap."],
        "technique": ["Try this: after any tutorial, close it and rebuild one small piece from memory.", "When you get stuck, peek only for the next step, then close again.", "If you can't rebuild it, that's your syllabus, not your failure.", "That peek is the real learning."],
        "ending": "Which tutorial left you feeling skilled until you tried alone?",
        "cta_type": "question",
        "web": ["WATCH", "FEELS EASY", "BLANK FILE", "GAP", "REBUILD", "LEARN"],
    },
    "cognitive-offloading": {
        "pillar": "HUMAN_AI",
        "tags": ["cognitive-offloading", "AI", "memory"],
        "technology_angle": "cognitive offloading to AI and software memory",
        "metacognition_concept": "cognitive offloading",
        "hook": "If AI remembers everything, what does your brain stop doing?",
        "problem": ["You used to remember the function signature.", "Now you ask AI every time, and it works, so why bother?", "The shortcut works, so the memory never gets used.", "You don't notice the muscle going quiet."],
        "explain": ["Offloading is useful: it saves mental effort for harder problems.", "But your brain learns what you practice recalling.", "If you never recall it, the memory quietly fades.", "Useful offloading is a choice, not a default."],
        "example": ["Think of phone numbers: you stopped memorizing them when your phone did.", "Same thing happens with API and code patterns you always ask AI for.", "You recognize them, but can't write them."],
        "technique": ["Try this: keep a 'no-AI' list of ten things you want to remember.", "For those, explain it out loud first, then check AI.", "For everything else, offload happily.", "That list is your memory insurance."],
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
        "explain": ["When we plan software, we imagine the best-case version: focused, healthy, uninterrupted coding.", "We forget interruptions because they aren't part of the product story we tell.", "The best case quietly assumes no reviews, no incidents, no flaky tests.", "That's the planning fallacy in software engineering, and it's reliable."],
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
        "explain": ["Confirmation bias means we notice what fits our idea and ignore what doesn't.", "In product user research, the question you ask shapes the answer you get.", "That's not curiosity; it's your judgment defending itself.", "You hear 'yes' because you asked in a way that invites yes."],
        "example": ["You ask 'Would you use this software feature?' and they say 'sure, maybe.'", "That's not evidence for product decisions. That's politeness.", "A leading question is a survey that agrees with you in advance."],
        "technique": ["Try this: before any product interview, write what would prove you wrong.", "Then ask questions that could give you that answer.", "Monitor whether you're asking to understand the user or only to agree with yourself.", "If you can't be proven wrong, you're not doing research."],
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
        "explain": ["Goodhart's law: when a measure becomes a target, it stops being a good measure.", "People optimize for what you measure, not what you meant.", "The metric becomes the game, and nobody monitors the thing it replaced.",
                    "The number climbs while the learning flatlines.", "And the game replaces the work."],
        "example": ["You measure PRs merged, so PRs get smaller and more trivial.", "The number goes up. Learning doesn't.", "You optimized the number, not the outcome."],
        "technique": ["Try this: for every metric you track, write what it could make people fake.", "Then add one counter-metric that catches the fake.", "Compare the pair in review every week: judge the outcome, not the graph.", "If you can't name the counter, don't ship the metric."],
        "ending": "Which metric in your team is being gamed right now?",
        "cta_type": "question",
        "web": ["METRIC", "TARGET", "GAME", "FAKE", "COUNTER", "REAL"],
    },
    "context-switching": {
        "pillar": "ATTENTION",
        "tags": ["context-switching", "notifications", "attention"],
        "technology_angle": "context switching, notifications and digital distraction",
        "metacognition_concept": "attention and task switching",
        "hook": "That notification just cost you twenty minutes. Not one.",
        "problem": ["You check Slack for a second and you're still reading about it twenty minutes later.", "Each small tap restarts your focus from zero."],
        "explain": ["Your attention runs on cues, not on plans.", "A notification is a cue that hijacks the plan.", "The cost isn't the look. It's the minutes to get back.", "Start by monitoring the pull: name the cue before you answer.",
                    "Your working memory is small; every switch dumps it."],
        "example": ["You were coding, a notification popped, you answered, and the variable name you held in mind is gone.", "You have to reload the whole context.", "By the time you're back, the file has scrolled and the plan is cold.", "Check where your mind landed; call it what it is."],
        "technique": ["Try this: batch notifications into two windows a day.", "When a topic pulls at you, write it on a note: I'll read at six.", "Tell your team when you'll answer, so quiet doesn't read as absent.", "Half the time, by six you won't care."],
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
        "problem": ["You spent three months building this code architecture.", "Throwing it away feels like throwing away the work in your product.", "Confidence in the old design grew from effort, not evidence."],
        "explain": ["Sunk cost fallacy: we keep investing in software because we've already invested.", "The code work is already gone. The question is only: what helps your product from now on?", "Past cost should not decide future cost in software engineering.", "It's a habit defending itself, not an argument.", "Monitor the slide from 'it works' to 'we built it'."],
        "example": ["You built a microservice that now costs more than it saves.", "You keep it because 'we built it', not because it helps your product code."],
        "technique": ["Try this: ask 'If we hadn't built this code, would we build it now?'", "If the answer is no, write the cost of keeping this software for six more months.", "Judge tomorrow's cost, not yesterday's effort.", "Then decide from there, not from the past."],
        "ending": "What software architecture are you keeping only because you built it?",
        "cta_type": "question",
        "web": ["BUILT", "COST", "KEEP", "FALLACY", "NOW?", "DECIDE"],
    },
    "metacognition-debugging": {
        "pillar": "CODING",
        "tags": ["debugging", "metacognition"],
        "technology_angle": "metacognition in debugging",
        "metacognition_concept": "metacognitive monitoring",
        "hook": "The bug isn't in the code. It's in what you're not looking at yet.",
        "problem": ["You've stared at the same function for an hour.", "The more you look, the less you see."],
        "explain": ["Debugging needs two minds: one that writes, one that watches.", "When you're stuck, you're usually running the same mental path.", "Metacognition is noticing that path and choosing a different one.", "Watching yourself debug is the skill above the skill."],
        "example": ["You assume the bug is in the new code, so you never check the old config.", "Senior engineers call it the new-code bias; it catches everyone.", "Your assumption is the bug."],
        "technique": ["Try this: when stuck for 20 minutes, explain the bug to a rubber duck out loud.", "Say what you know, what you assume, and what you haven't checked.", "If the explanation changes nothing, change the question you're asking the code.", "The gap you hear is where the bug lives."],
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
        "problem": ["You ask AI, it writes it, you paste it, it works.", "Next week, you can't write it without AI.", "The demo passed, but the practice never happened."],
        "explain": ["Learning needs some difficulty: recalling, mixing, waiting before you review.", "AI removes that difficulty, so it feels good but teaches less.", "Recall is the exercise; rereading the answer is just watching again.", "Smooth practice feels like progress. Rough practice is progress."],
        "example": ["Two students: one rereads AI code, one closes it and tries to rebuild it from memory.", "Next week, who explains it better?", "The AI demo shipped; the software is still theirs to own."],
        "technique": ["Try this: after AI gives you code, close it and write the logic in plain English.", "Then rebuild the code from your English.", "Do it while the problem still feels fresh, not after the demo fades.", "If you can't, you didn't learn, you just copied."],
        "ending": "What's one thing you learned to do without AI this month?",
        "cta_type": "question",
        "web": ["AI WRITES", "FEELS EASY", "RECALL", "REBUILD", "STRUGGLE", "LEARN"],
    },
    "deskilling-autocomplete": {
        "pillar": "CODING",
        "tags": ["deskilling", "autocomplete", "AI"],
        "technology_angle": "deskilling through AI autocomplete in code editors",
        "metacognition_concept": "skill decay and monitoring",
        "hook": "Autocomplete makes you faster today, and slower next year. Why?",
        "problem": ["You used to know the syntax by heart.",
                    "Now you wait for the ghost text, then hit Tab and move on.",
                    "The suggestion is faster, so you stop trying to recall.",
                    "Every accepted line makes waiting the habit."],
        "explain": ["Skill is kept by retrieval: pulling it from memory.",
                    "Autocomplete replaces retrieval with recognition.",
                    "Recognition is easy, but it doesn't keep the skill alive.",
                    "So the decay is silent: you can still read code, you can't write it."],
        "example": ["You used to write regex from memory.",
                    "Now the assistant drafts it, you glance, accept, and it ships.",
                    "Next month the same pattern defeats you and costs you a debugging hour.",
                    "You recognize the shape, but you can't produce it."],
        "technique": ["Try this: one hour a week, code with autocomplete off.",
                      "Write the hard parts from memory first, then use the suggestion as your check.",
                      "Notice the stall; stay in it ten seconds before you tab.",
                      "That hour is your skill insurance."],
        "ending": "What skill are you losing because AI does it for you?",
        "cta_type": "question",
        "web": ["FAST NOW", "SLOW LATER", "RECALL", "RECOGNIZE", "OFF", "KEEP"],
    },
}

EXTRA = {
    "automation-bias": {"bridge": "It's not laziness. It's your brain trusting fluency over evidence.", "recap": ["So: fluency is not understanding.", "Explain before you accept, test one edge case, and keep the watcher on."]},
    "hallucination-confidence": {"bridge": "That's not lying. It's fluency without calibration.", "recap": ["So: confident language is not confident knowledge.", "Ask how you'd verify in 30 seconds, and make that the habit."]},
    "tutorial-hell": {"bridge": "It's not that you're bad at coding. With autocomplete on, you're practicing recognition, not recall.", "recap": ["So: close the tutorial, rebuild from memory, peek only for the next step.", "The struggle you feel is the learning."]},
    "cognitive-offloading": {"bridge": "Offloading is smart, until you offload the skill you want to keep.", "recap": ["So: choose what to remember, recall before you ask, offload the rest.", "Your brain keeps what you practice."]},
    "planning-fallacy": {"bridge": "It's not laziness. It's a bias so reliable it has a name.", "recap": ["So: your gut plans the movie version. Your history knows the real one.", "Estimate from history, not hope."]},
    "confirmation-bias-research": {"bridge": "It's not dishonesty. It's your brain protecting the first idea.", "recap": ["So: write what would prove you wrong before you start.", "If you can't be proven wrong, it's not research."]},
    "goodhart-metrics": {"bridge": "The metric didn't fail. It succeeded at being gamed.", "recap": ["So: every metric needs a counter-metric that catches the fake.", "If you can't name it, don't ship it."]},
    "context-switching": {"bridge": "That's how a small tap becomes a lost hour without a decision.", "recap": ["So: write it down, give it a time, let your attention choose later on purpose."]},
    "sunk-cost-architecture": {"bridge": "That's how sunk cost works: the past doesn't vote, but it keeps talking.", "recap": ["So: ask if you'd build it now, write the cost of keeping it, decide from there."]},
    "metacognition-debugging": {"bridge": "The fix isn't more looking. It's looking differently.", "recap": ["So: when stuck, explain what you know, assume, and haven't checked.", "The watcher finds the bug your eyes missed."]},
    "learning-code-with-ai": {"bridge": "AI removes the difficulty your memory needs to grow.", "recap": ["So: after AI writes, close it, explain in English, rebuild from memory.", "Smooth feels good. Rough teaches."]},
    "deskilling-autocomplete": {"bridge": "That's how a shortcut becomes a skill you lose, one accepted line at a time.", "recap": ["So: recall first, then let the suggestion check you.", "Recognition feels like skill. Retrieval is the skill."]},
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

# --------------------------------------------------------------------- CTA diversity
# Rolling editorial memory drives CTA type variety (cta_policy.avoid_same_type_consecutive).
# The label is derived deterministically from the ending text, so it never trusts
# the LLM for a label it cannot verify. With no memory the fallback is the plain
# classification — still deterministic.

CTA_TYPE_KEYWORDS = {
    "save": ("save this", "save it", "bookmark", "keep this", "save for"),
    "try-it": ("try this", "try it", "do this", "test it", "run it", "practice"),
    "share-experience": ("share", "tell me", "what did you", "when did you", "which time",
                          "what's the last", "what was the last", "last time"),
}

def classify_cta_type(ending_text):
    """Deterministic CTA-type label for an ending line (default: question)."""
    t = (ending_text or "").lower()
    if not t.strip():
        return "question"
    for ctype in ("save", "try-it", "share-experience"):
        if any(k in t for k in CTA_TYPE_KEYWORDS[ctype]):
            return ctype
    return "question"

def _cta_compatible(ctype, ending_text):
    """Can an ending plausibly be labeled with `ctype`?"""
    t = (ending_text or "").lower()
    if ctype == "question":
        return True                     # any ending can be read as a question CTA
    if ctype == "save":
        return any(k in t for k in CTA_TYPE_KEYWORDS["save"])
    if ctype == "try-it":
        return any(k in t for k in CTA_TYPE_KEYWORDS["try-it"])
    if ctype == "share-experience":
        return ("?" in t) and any(k in t for k in ("you", "your", "share", "tell"))
    return True

def choose_cta_type(ending_text, memory, pol):
    """Pick this reel's CTA type from the allowed set, avoiding the recent ones.

    Rolling memory rule: skip the CTA types of the most recent recorded reels
    (newest first) in order; take the first remaining type compatible with the
    ending text. Deterministic fallback when memory is unavailable/empty:
    the plain classification of the ending text.
    """
    allowed = list(pol.get("cta_policy", {}).get("allowed_types",
                                                 ["question", "try-it", "share-experience", "save"]))
    if not allowed:
        return classify_cta_type(ending_text)
    classified = classify_cta_type(ending_text)
    if classified not in allowed:
        classified = allowed[0]
    if not pol.get("cta_policy", {}).get("avoid_same_type_consecutive", True):
        return classified
    recent = common.recent_cta_types(memory, n=len(allowed))
    if not recent:
        return classified                            # no memory → deterministic fallback
    # Prefer the type the ending text actually expresses (it is compatible by
    # construction); only switch when that type repeats a recent reel.
    order = [classified] + [c for c in allowed if c != classified]
    for cand in order:
        if cand in recent:
            continue
        if _cta_compatible(cand, ending_text):
            return cand
    return classified                                # nothing fits → truthful label

# --------------------------------------------------------------------- numeric pre-gate
def narration_text_of(llm_output):
    n = (llm_output or {}).get("narration", "")
    if isinstance(n, str):
        return n
    if isinstance(n, dict):
        parts = []
        for v in n.values():
            parts.append(" ".join(v) if isinstance(v, list) else str(v))
        return " ".join(parts)
    return ""

def numeric_guard(llm_output, evidence_packet, ctx=""):
    """Deterministic mirror of the QA source_quality statistic blocker.

    Unsupported numbers in LLM output are rejected BEFORE tts/render/QA so the
    expensive stages never run on content the supervisor is certain to block,
    and the static fallback (which obeys the same rule) is used instead.
    Returns the list of unsupported claims (empty = clean).
    """
    bad = common.unsupported_numeric_claims(narration_text_of(llm_output), evidence_packet)
    if bad:
        print(f"[producer] numeric pre-gate ({ctx}): unsupported numeric claims {bad} "
              f"— evidence packet supports none; rejecting LLM output (static fallback obeys the same rule)",
              flush=True)
    return bad

def citation_guard(sources, evidence_packet, ctx=""):
    """Invented-citation guard (issue #22, deterministic — mirrors the QA "no fake
    citation" source_quality principle at generation time).

    A source URL inside an LLM script may ONLY be a URL the sanitized evidence
    packet itself provides. Anything else — a guessed arXiv/DOI link, a "fixed"
    tier, a conveniently added paper — is an invented citation and blocks
    adoption, because the pre-render gate would otherwise let a model escape an
    attribution blocker simply by fabricating evidence. No domain allowlist, no
    network check: the packet is the only ground truth. Returns the ungrounded
    URLs (empty = clean).
    """
    bad = common.ungrounded_source_urls(sources, evidence_packet)
    if bad:
        shown = [common.scrub_secrets(str(u))[:60] for u in bad]
        print(f"[producer] citation pre-gate ({ctx}): source URLs the evidence packet does not "
              f"provide {shown} — rejecting LLM output (invented citation; leave 'url' empty "
              f"unless the packet provides it)", flush=True)
    return bad

# --------------------------------------------------------------------- pre-render gate
def gate_report(script, pol, ctx, topic=None, packet=None, reviewer_output=None):
    """Deterministic PRE-RENDER gate on a built script — ONE combined verdict:

      1. the fast length gate: the SAME single-sourced word count QA uses
         (common.spoken_word_count) in the required 150-260 band plus the
         duration preflight from the configured narration rate;
      2. the full PRE-RENDER TEXT QA gate (issue #22): qa_pre_render_text_gate —
         every deterministic final-QA blocker decidable from text alone
         (source-quality claim words, unsupported statistics, English-only,
         banned English terms, script-beat/hook blockers, technology,
         metacognition and topic relevance, caption/hashtag blockers and
         structured Reviewer contradictions) through the EXACT QA functions in
         build/qa_supervisor.py — no second implementation, no copied rules.

    Never trusts a model-reported word count and never pads: a bad report routes
    through the one allowed Revision, then the validated Static English
    Fallback, then skip. Counts and structured blocker codes are reported — the
    narration text never enters the report.
    """
    p = common.pre_render_params(pol)
    words = common.spoken_word_count(script)
    est = common.estimate_spoken_seconds(script, pol)
    length_issues = common.pre_render_issues(script, pol)
    text = qa.pre_render_text_gate(script, topic, pol, reviewer_output=reviewer_output)
    issues = list(length_issues) + list(text["blocking"])
    in_target = p["target_min"] <= words <= p["target_max"]
    rep = {"ctx": ctx, "words": words, "estimated_seconds": est,
           "required_words": [p["words_min"], p["words_max"]],
           "target_words": [p["target_min"], p["target_max"]],
           "target_ok": in_target, "issues": issues,
           "length_issues": length_issues, "text_blocking": list(text["blocking"]),
           "text_warnings": list(text["warnings"])}
    if issues:
        print(f"[producer] pre-render gate ({ctx}): {issues} (words={words}, est={est:.1f}s) "
              f"— structured approval cannot override this deterministic check", flush=True)
    else:
        print(f"[producer] pre-render gate ({ctx}): ok words={words} est={est:.1f}s "
              f"target_ok={in_target} text_qa=clean", flush=True)
    return rep

def text_qa_revision_instructions(blocking):
    """Safe STRUCTURED blocker codes for the ONE allowed Revision (issue #22).

    Every item is generated from deterministic QA blocker codes — never raw web
    text or evidence content. Each claim-word item states the hard rule: the
    unsupported attribution must be REMOVED (or rewritten as a direct,
    appropriately qualified observation when editorially valid), and NO citation,
    URL, author, statistic or tier may be invented to justify keeping it.
    """
    out = []
    for b in blocking or []:
        check = b[1:b.index("]")].strip() if b.startswith("[") and "]" in b else ""
        msg = b.split("] ", 1)[1] if "] " in b else b
        if check == "source_quality" and "claim words" in msg:
            out.append(
                "evidence blocker (deterministic pre-render text QA — the exact QA source_quality "
                f"rule): {msg}. The sanitized evidence packet contains NO Tier A/B evidence, so the "
                "narration must not attribute anything to researchers, studies, science, experts, "
                "data or experiments. REMOVE the attribution: rewrite each affected sentence as a "
                "direct, appropriately qualified observation (hedged honestly, no fabricated certainty) "
                "or drop the claim. Do NOT add, restore, adjust or invent a citation, paper, author name, "
                "URL, statistic or evidence tier to justify the wording — invented evidence is itself an "
                "automatic rejection by the deterministic citation guard.")
        elif check == "source_quality" and "limited-claims" in msg:
            out.append("evidence blocker: limited-claims mode must not make research claims — remove every "
                       "attribution from the narration without adding a citation.")
        elif check == "source_quality" and ("statistics" in msg or "cannot be verified" in msg):
            out.append("evidence blocker (statistics): the flagged numbers are not in the packet's numeric "
                       "evidence — remove them or rewrite the sentence without the number; never keep a "
                       "number by inventing a citation (the numeric fix rule also applies).")
        elif check == "source_quality" and "fake" in msg:
            out.append("evidence blocker: the flagged citation/URL fails QA's fake-source rule — remove it "
                       "entirely; a source URL may only be one the evidence packet itself provides.")
        elif check == "source_quality" and "certainty" in msg:
            out.append(f"evidence blocker: remove the unsupported-certainty phrase from the narration ({msg}) "
                       "and rewrite the line as a direct, appropriately qualified observation.")
        elif check in ("english_quality",):
            out.append(f"language blocker (deterministic pre-render text QA): {msg} — rewrite the line in "
                       "plain conversational English without the banned expression or URL.")
        elif check in ("content_language", "english_only"):
            out.append(f"language blocker (deterministic pre-render text QA): {msg} — production is "
                       "English-only; remove the non-English/forbidden content entirely.")
        elif check == "caption_quality":
            out.append(f"caption blocker (deterministic pre-render text QA): {msg} — fix the caption text "
                       "within caption_policy and hashtag_policy (brand tags kept, spam tags dropped, body "
                       "length in range); never delete the CTA just to fit.")
        elif check == "reviewer_check":
            out.append("reviewer contradiction (deterministic pre-render text QA): the structured Reviewer "
                       f"report itself flags this ({msg}) — fix the underlying content; a score or an "
                       "approval flag cannot override deterministic QA blockers.")
        elif check in ("technology_relevance", "metacognition_relevance", "topic_relevance"):
            out.append(f"relevance blocker (deterministic pre-render text QA): {msg} — name the concrete "
                       "technology context and the genuine metacognitive mechanism in the narration itself; "
                       "never pad with filler to satisfy a keyword count.")
        elif check == "script_quality":
            if "words" in msg:
                continue  # the length/duration item already carries the word contract
            out.append(f"script blocker (deterministic pre-render text QA): {msg} — fix the script itself "
                       "(beats, hook, actionable technique, placeholders, banned wording); never by padding.")
        else:
            out.append(f"deterministic pre-render text QA blocker [{check}]: {msg} — fix the content; never "
                       "by padding and never by adding or inventing citations.")
    return out

def gate_revision_instructions(rep):
    """The length/duration items sent to the ONE allowed Revision — ONLY for the
    length problems the fast gate actually found (issue #22: a claim-word-only
    failure must not receive a bogus duration lecture). Explicit so the model
    fixes the budget with real content: one main idea, one actionable technique,
    conversational English, 175-210 spoken words."""
    p = common.pre_render_params()
    words, est = rep["words"], rep["estimated_seconds"]
    length_issues = rep.get("length_issues", rep.get("issues", []))
    out = []
    if words < p["words_min"] or words > p["words_max"]:
        side = "EXPAND" if words < p["words_min"] else "CONDENSE"
        out.append(
            f"length blocker (deterministic pre-render gate): the script has {words} spoken words; "
            f"QA hard-rejects anything outside {p['words_min']}-{p['words_max']} — {side} the SAME "
            f"narration to a target of ~{p['target_min']}-{p['target_max']} spoken words total across all "
            "narration lines. Preserve exactly one main idea and one actionable technique, and keep "
            "conversational English with natural contractions. Deepen that single idea — its concrete tech "
            "context, its genuine metacognitive mechanism, its example, its practical exercise — instead of "
            "adding topics. Never pad with filler, disclaimers or a repeated CTA; the gate counts actual "
            "words and ignores any word count you report.")
    elif any("estimated spoken duration" in i for i in length_issues):
        out.append(
            f"duration blocker (deterministic pre-render gate): estimated spoken duration {est:.1f}s is not "
            f"safe for the final 60-120s render — fix it with the word budget (target {p['target_min']}-"
            f"{p['target_max']} spoken words at the configured narration rate), NEVER with silence, slowed "
            "speech, repeated CTAs or filler. Preserve one main idea, one actionable technique and "
            "conversational English with contractions.")
    return out

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
            # Tier DERIVED with QA's own matcher (single source: common.source_tier)
            # — a stored tier is never asserted, and an undated calendar label is
            # honestly "?" so the claim-word blocker stays real (issue #22).
            sources.append({"label": s, "url": "", "tier": common.source_tier("", s, pol),
                            "role": "evidence"})
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
    # Same deterministic rule as the LLM path: brand tags guaranteed, spam tags
    # dropped, capped at hashtag_policy.max.
    tags = common.normalize_hashtags(hashtag_pool.get(pb["pillar"], [])[:3] + ["#cognitivescience"], pol)

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

def build_script_from_llm(topic, pol, llm_output, generation_mode="groq"):
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
    sources = list(llm_output.get("sources", []) or [])
    # Evidence grounding (issue #22, run 35054292820 / reel-2026-09-18): the LLM
    # path used to ship ONLY the model's own source list, so the calendar packet's
    # curated evidence never reached the QA tier matcher — a dated Mark et al.
    # citation existed in the packet yet the script evaluated best tier "?" and
    # the narration "researchers" blocked AFTER rendering. The packet's evidence
    # entries are therefore merged into script["sources"] verbatim (label copied
    # from the calendar — nothing is invented), and every stored "tier" is
    # DERIVED with QA's own matcher because QA ignores claimed tiers by design.
    cal = topic.get("calendar") or {}
    grounded = []
    for s in cal.get("sources", [])[:3]:
        grounded.append({"label": s, "url": "", "tier": common.source_tier("", s, pol),
                         "role": "evidence"})
    disc = topic.get("discovery_source") or {}
    if disc.get("url"):
        grounded.append({"label": disc.get("name", "discovery"), "url": disc["url"],
                         "tier": disc.get("tier", "C"), "role": "discovery"})
    seen = set()
    merged = []
    for s in grounded + sources:
        if not isinstance(s, dict):
            continue
        key = (str(s.get("label", "")).strip(), str(s.get("url", "")).strip())
        if key in seen:
            continue
        seen.add(key)
        s = dict(s)
        # Never trust a model-claimed tier: QA re-derives it from url/label with
        # the same matcher, so the recorded field must say the same thing.
        s["tier"] = common.source_tier(s.get("url", ""), s.get("label", ""), pol)
        merged.append(s)
    sources = merged

    # Caption from LLM or build. Hashtags are normalized deterministically:
    # brand tags (hashtag_policy.always, incl. #metacognitionhq) are guaranteed,
    # banned/spam tags are dropped, and the set stays within the non-spam cap.
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
            "hashtags": common.normalize_hashtags(caption_in.get("hashtags", []), pol),
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
            "hashtags": common.normalize_hashtags(["#AI", "#coding"], pol),
        }

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

    # Build evidence packet (sanitized) — with rolling editorial memory for
    # topic de-duplication AND CTA diversity.
    memory = common.load_memory()
    recent = common.recent_entries(memory, 10)
    recent_topics = [e.get("topic","") for e in recent]
    evidence_packet = llm_provider.build_evidence_packet(topic, pol, recent_topics,
                                                         recent_ctas=common.recent_cta_types(memory))

    producer_mode = os.environ.get("CONTENT_PRODUCER", "groq")
    fallback_mode = os.environ.get("CONTENT_FALLBACK", "static-english")

    script = None
    generation_mode = "static-fallback"
    producer_report = {}
    reviewer_report = {}

    # Try Groq Producer if requested
    if producer_mode == "groq":
        try:
            # One authenticated discovery for the whole daily run; producer and
            # reviewer share it, and the selection report is recorded.
            discovered, disc_meta = llm_provider.discover_models()
            prod = llm_provider.GroqProducer()
            llm_out, raw = prod.produce(evidence_packet, _discovered=discovered)
            # Honesty gate: mock/fixture output must NEVER be recorded as
            # generation_mode=groq. That label is reserved for genuine
            # API responses. Mock in the daily path falls back to static.
            if isinstance(raw, dict) and raw.get("mock"):
                raise ValueError("mock output rejected in daily path — explicit mock is test-only")
            producer_report = {"model": prod.model, "raw": raw, "output": llm_out, "mode": "groq",
                               "discovered_count": len(discovered),
                               "selection": raw.get("selection") if isinstance(raw, dict) else None}
            # Deterministic numeric pre-gate (mirror of the QA source_quality
            # statistic blocker): unsupported numbers force a revision and can
            # never reach the render/QA stages from the LLM path.
            numeric_bad = numeric_guard(llm_out, evidence_packet, ctx="first output")
            # Validate and build script
            script = build_script_from_llm(topic, pol, llm_out, generation_mode="groq")
            generation_mode = "groq"
            # Invented-citation guard (issue #22): a model that "fixes" an
            # attribution blocker by adding a plausible-but-unsupported URL is
            # rejected the same way a fake statistic is — the packet is the only
            # ground truth for source URLs.
            citation_bad = citation_guard(script.get("sources", []), evidence_packet, ctx="first output")
            # Deterministic PRE-RENDER gate on the Producer's first output. Runs
            # BEFORE TTS/subtitle/render: the fast length gate (QA single-sourced
            # word count + duration preflight, reel-2026-09-17 fix) AND the full
            # deterministic TEXT QA gate — the exact final-QA blocker functions
            # including source_quality claim words (issue #22 / reel-2026-09-18:
            # "researchers" in the spoken script with no Tier A/B evidence was
            # only caught after a wasted render). Never overridden by the
            # reviewer's structured approval.
            gate1 = gate_report(script, pol, "first output", topic=topic, packet=evidence_packet)
            producer_report["gate_first"] = {k: gate1[k] for k in
                                             ("words", "estimated_seconds", "target_ok", "issues",
                                              "text_blocking")}
            rejected_by_gate = bool(gate1["issues"])

            # Reviewer step
            rev = None
            review_out = None
            reviewer_rejected = False
            try:
                reviewer_mode = os.environ.get("CONTENT_REVIEWER", "groq")
                if reviewer_mode == "groq":
                    rev = llm_provider.GroqReviewer()
                    review_out, review_raw = rev.review(llm_out, evidence_packet,
                                                       _discovered=discovered,
                                                       producer_model=prod.model)
                    reviewer_report = {"model": rev.model, "raw": review_raw, "output": review_out}
                    # Requirement (issue #22): the Reviewer must not approve
                    # evidence-requiring claim words when no qualifying evidence
                    # exists. Its own structured report is checked too — an
                    # "approved: true" flag cannot contradict a non-empty
                    # blocking_errors / unsupported_claims list, so those lists
                    # reject the candidate exactly like a missing approval does.
                    reviewer_rejected = (not review_out.get("approved", False)
                                         or review_out.get("score", 0) < 85
                                         or bool(review_out.get("blocking_errors"))
                                         or bool(review_out.get("unsupported_claims")))
            except Exception as e:
                print(f"[producer] reviewer error, will fallback if needed: "
                      f"{common.scrub_secrets(str(e))}")

            # Rejection is any of: reviewer rejection, the deterministic numeric
            # pre-gate, the citation guard, or the deterministic pre-render TEXT
            # QA gate. A reviewer APPROVAL cannot keep an out-of-range or
            # ungrounded script — structured output never overrides deterministic
            # checks — and the failing Producer output is routed through the one
            # allowed Revision.
            if reviewer_rejected or numeric_bad or citation_bad or rejected_by_gate:
                if rev is None:
                    # No reviewer → no compliant final review; go straight to the
                    # validated static fallback (no unbounded retries, no new steps
                    # beyond Producer → Reviewer → Revision → final Reviewer).
                    print("[producer] reviewer unavailable — static fallback (policy caps the chain at "
                          "Producer → Reviewer → one Revision → final Reviewer)", flush=True)
                    script = None
                else:
                    required_changes = list((review_out or {}).get("required_changes") or [])
                    for n in (numeric_bad or []):
                        required_changes.append(
                            f"remove or rewrite the unsupported numeric claim '{n}' — "
                            "do not keep it by adding a citation")
                    for u in (citation_bad or []):
                        required_changes.append(
                            f"remove the source URL '{common.scrub_secrets(str(u))[:60]}' — the evidence "
                            "packet does not provide it; a citation may only be one the packet lists, and "
                            "leave 'url' empty otherwise — never invent or guess URLs, papers, authors or "
                            "evidence tiers")
                    if rejected_by_gate:
                        required_changes += gate_revision_instructions(gate1)
                        # issue #22: safe STRUCTURED blocker codes from the pre-render
                        # text QA gate (claim words etc.), never raw web text
                        required_changes += text_qa_revision_instructions(gate1["text_blocking"])
                    if not required_changes and reviewer_rejected:
                        # Structured contradiction: approved/score looked fine
                        # but the reviewer's OWN blocking_errors or
                        # unsupported_claims lists are non-empty — that is a
                        # rejection, and it must still consume the one allowed
                        # Revision with a safe instruction (no invented
                        # evidence), never silently ship the rejected script.
                        required_changes = [
                            "the reviewer's structured report contradicts its approval (non-empty "
                            "blocking_errors/unsupported_claims) — remove the unsupported attribution "
                            "from the narration and rewrite those lines as direct, appropriately "
                            "qualified observations; NEVER answer by inventing citations, URLs, author "
                            "names, statistics or evidence tiers"]
                    if required_changes:
                        evidence_packet["revision_request"] = required_changes
                        try:
                            # ONE revision attempt + final review (existing policy,
                            # no additional retries; 429/timeout/malformed still
                            # fall through to the static fallback below)
                            llm_out2, raw2 = prod.produce(evidence_packet, _discovered=discovered)
                            if isinstance(raw2, dict) and raw2.get("mock"):
                                raise ValueError("mock revision rejected in daily path")
                            numeric_bad2 = numeric_guard(llm_out2, evidence_packet, ctx="revision")
                            script2 = build_script_from_llm(topic, pol, llm_out2, generation_mode="groq")
                            citation_bad2 = citation_guard(script2.get("sources", []), evidence_packet,
                                                            ctx="revision")
                            gate2 = gate_report(script2, pol, "revision", topic=topic, packet=evidence_packet)
                            review_out2, review_raw2 = rev.review(llm_out2, evidence_packet,
                                                                  _discovered=discovered,
                                                                  producer_model=prod.model)
                            if isinstance(review_raw2, dict) and review_raw2.get("mock"):
                                raise ValueError("mock reviewer output rejected in daily path")
                            reviewer2_blocks = bool(review_out2.get("blocking_errors")
                                                     or review_out2.get("unsupported_claims"))
                            review_ok2 = (review_out2.get("approved") and review_out2.get("score", 0) >= 85
                                          and review_out2.get("technology_relevance")
                                          and review_out2.get("metacognition_relevance")
                                          and not reviewer2_blocks)
                            if (review_ok2 and not numeric_bad2 and not citation_bad2
                                    and not gate2["issues"]):
                                script = script2
                                generation_mode = "groq"
                                reviewer_report = {"model": rev.model, "raw": review_raw2,
                                                   "output": review_out2, "revision": True}
                            else:
                                # Second rejection, surviving numbers or invented
                                # citations, or the one allowed Revision still
                                # failing the deterministic pre-render gate → the
                                # validated Static English Fallback decides.
                                reasons = []
                                if not review_ok2:
                                    reasons.append("Reviewer rejected after revision "
                                                   f"(approved={bool(review_out2.get('approved'))}, "
                                                   f"score={review_out2.get('score')}, "
                                                   f"blocking={review_out2.get('blocking_errors') or []}, "
                                                   f"unsupported={review_out2.get('unsupported_claims') or []})")
                                if numeric_bad2:
                                    reasons.append(f"numeric claims survived the revision: {numeric_bad2}")
                                if citation_bad2:
                                    reasons.append("the revision invented citation URLs the evidence packet "
                                                   f"does not provide: {[str(u)[:60] for u in citation_bad2]}"
                                                   " — invented evidence is rejected")
                                if gate2["issues"]:
                                    reasons.append("script still fails the pre-render gate after the one "
                                                   f"allowed revision: {gate2['issues']}")
                                raise ValueError("; ".join(reasons) or "revision rejected")
                        except Exception as e_rev:
                            print(f"[producer] reviewer second rejection / revision unusable, falling back: "
                                  f"{common.scrub_secrets(str(e_rev))}")
                            script = None
                    else:
                        script = None

        except Exception as e:
            # Every provider error is classified by build/groq_http.py and
            # scrubbed here before it reaches stdout, producer_report.json,
            # script metadata or an issue body. The API key can never appear.
            safe_error = common.scrub_secrets(str(e))
            category = getattr(e, "category", "") or ""
            print(f"[producer] Groq producer failed: {safe_error} — trying fallback")
            if category:
                print(f"[producer] groq error category: {category}")
            script = None
            producer_report["error"] = safe_error
            producer_report["error_category"] = category
            producer_report["mode"] = "groq-failed"

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

    # CTA diversity: rolling editorial memory avoids repeating the most recent
    # CTA type (cta_policy.avoid_same_type_consecutive). The label is derived
    # deterministically from the ending text, so it works identically for groq
    # and static-fallback scripts; with no memory it falls back to the plain
    # classification (deterministic).
    ending_lines = [l["t"] for ch in script["chunks"] if ch.get("beat") == "ending" for l in ch.get("en", [])]
    script["meta"]["cta_type"] = choose_cta_type(" ".join(ending_lines), memory, pol)

    # Honesty gate: a report must never claim "groq" when the script that was
    # actually built came from the static English fallback.
    if generation_mode != "groq" and producer_report.get("mode") == "groq":
        producer_report["mode"] = "groq-rejected"
        producer_report["note"] = ("provider responded but the result was rejected "
                                   "or unusable — static English fallback was built")

    # Final deterministic PRE-RENDER gate on whatever would ship (Revision output,
    # first-pass output, or the Static English Fallback). The fallback is
    # VALIDATED, not trusted: if even it fails the gate, this stage skips before
    # rendering — no script.json is written, so TTS/subtitles/render never see a
    # known-bad script, and no padding is applied to dodge the gate.
    gate_final = gate_report(script, pol, f"final ({generation_mode})",
                             topic=topic, packet=evidence_packet)
    producer_report["gate"] = {k: gate_final[k] for k in
                               ("words", "estimated_seconds", "required_words",
                                "target_words", "target_ok", "issues", "text_blocking")}
    os.makedirs(a.out, exist_ok=True)
    if gate_final["issues"]:
        print(f"[producer] PRE-RENDER GATE: skipping before render — {'; '.join(gate_final['issues'])} "
              f"(mode={generation_mode}; bounded policy exhausted: Producer → Reviewer → one Revision "
              f"→ final Reviewer → validated fallback). No script.json, no padding, no TTS, no render, "
              f"no Buffer.", flush=True)
        common.save_json(os.path.join(a.out, "producer_report.json"), producer_report)
        raise SystemExit(3)

    common.save_json(os.path.join(a.out, "script.json"), script)
    # Also save producer/reviewer reports for QA and final report
    common.save_json(os.path.join(a.out, "producer_report.json"), producer_report)
    if reviewer_report:
        common.save_json(os.path.join(a.out, "reviewer_report.json"), reviewer_report)

    print(f"[producer] script → {a.out}/script.json  playbook={script['meta']['playbook']} "
          f"chunks={len(script['chunks'])} words={gate_final['words']} est={gate_final['estimated_seconds']:.1f}s "
          f"gate=ok mode={generation_mode} lang=en")

if __name__ == "__main__":
    main()
