"""Content Producer Agent — topic.json → episode script.json (+ caption spec).

Structure (fixed, policy-driven): hook → problem → explain → example →
technique → ending. One main message per reel, conversational English,
no invented numbers, evidence-tier-aware claim wording, Persian subtitles
via free translators (Google → MyMemory) with hard validation.

Scripts are built from curated "playbooks" per calendar topic / pillar. The
calendar entries in content/calendar.json carry `beats` and `sources`; this
module turns them into spoken lines using honest templates only — it never
fabricates statistics, citations or URLs. Trend topics (tier C discovery) are
produced in "limited-claims" mode: the trend is the hook, the technique is
one of the evergreen metacognition techniques, and claims stay hedged.

usage:
  python3 build/content_producer.py --topic <topic.json> --out <epdir> [--variant 0|1] [--no-translate]
"""
import argparse
import json
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import common  # noqa: E402

HANDLE = "@metacognition.hq"

# --------------------------------------------------------------- playbooks
# Each playbook: one main message. Lines are short and speakable. Claim words
# ("study", "research") appear only where the calendar entry has a tier-A/B source.
PLAYBOOKS = {
    "dunning-kruger": {
        "pillar": "BIAS", "tags": ["dunning-kruger", "overconfidence", "calibration"],
        "hook": "Why do the least skilled people often feel the most confident?",
        "problem": ["You just learned something new, and suddenly it feels simple.",
                    "That feeling is the trap. Not the skill. The feeling."],
        "explain": ["Here's the catch: the skill you need to do a task well",
                    "is the same skill you need to judge how well you did it.",
                    "Beginners lack both, so their guess about themselves runs high."],
        "example": ["Think of your first week driving. Smooth, easy, nailed it.",
                    "Then a truck merges, it rains, and you realise how much you didn't see."],
        "technique": ["So try this: before you rate yourself, find one hard test of the skill.",
                      "A real question, a real task, a real person who does it better.",
                      "Then compare your guess with the result, and update the guess."],
        "ending": "Where did you last feel sure, and turn out wrong? Be honest.",
        "cta_type": "share-experience",
        "web": ["SKILL", "SELF-VIEW", "THE GAP", "TEST", "COMPARE", "UPDATE"],
        "claim_note": "The classic finding is Kruger & Dunning (1999); later work debates how big the effect is, so the reel avoids numbers.",
    },
    "forgetting-curve": {
        "pillar": "MEMORY", "tags": ["forgetting-curve", "spacing"],
        "hook": "You studied for three hours. So why is it gone by Friday?",
        "problem": ["Nothing feels more productive than a long cram session.",
                    "But most of it quietly leaks out over the next few days."],
        "explain": ["Memories fade fastest right after you learn them.",
                    "Each time you pull one back before it's gone, the fade slows down.",
                    "That's the whole idea behind spacing: review later, not longer."],
        "example": ["Say you learn ten words tonight.",
                    "Reviewing them once tomorrow beats reviewing them five more times tonight."],
        "technique": ["Try this: after any study session, set three reminders.",
                      "Tomorrow, in three days, and in a week.",
                      "Each time, recall first, then check. Don't just reread."],
        "ending": "What's one thing you'd love to still remember next month? Space it.",
        "cta_type": "try-it",
        "web": ["LEARN", "DAY 1", "DAY 3", "DAY 7", "RECALL", "KEEP"],
    },
    "testing-effect": {
        "pillar": "LEARN", "tags": ["testing-effect", "retrieval-practice"],
        "hook": "What if the test was the best study tool you own?",
        "problem": ["Most of us treat tests as the judgement at the end.",
                    "So we spend our time reading, and dread the quiz."],
        "explain": ["But pulling an answer out of your head changes the memory itself.",
                    "Every successful recall makes the next one easier.",
                    "Reading feels smoother, yet the effort of recall is what sticks."],
        "example": ["Close the book and explain the chapter to an empty chair.",
                    "The parts you stumble on are exactly the parts you hadn't learned yet."],
        "technique": ["Try this tonight: turn every heading in your notes into a question.",
                      "Answer from memory, out loud, then check.",
                      "Mark what you missed, and quiz that again tomorrow."],
        "ending": "Try it once this week and tell me what surprised you.",
        "cta_type": "try-it",
        "web": ["READ", "RECALL", "CHECK", "GAPS", "AGAIN", "STICK"],
    },
    "explanatory-depth": {
        "pillar": "SELF", "tags": ["illusion-of-explanatory-depth", "explain"],
        "hook": "Do you actually know how a zipper works? Explain it. Right now.",
        "problem": ["Most people feel they understand everyday things quite well.",
                    "Until someone asks them to explain step by step."],
        "explain": ["Familiarity feels like understanding.",
                    "You've seen the thing a thousand times, so the brain files it under 'known'.",
                    "The gap only shows up when you have to produce the explanation yourself."],
        "example": ["Try a toilet, a bicycle, or the app you use every day.",
                    "Start explaining, and notice the exact sentence where the words run out."],
        "technique": ["Before a decision or an exam, do the explain test.",
                      "Say the mechanism out loud, step by step, as if to a curious kid.",
                      "Wherever you get vague, that's your next thing to learn."],
        "ending": "Which everyday object would you fail to explain? Try it.",
        "cta_type": "question",
        "web": ["FAMILIAR", "FEELS KNOWN", "EXPLAIN", "GAP", "LEARN", "KNOWN"],
    },
    "desirable-difficulties": {
        "pillar": "LEARN", "tags": ["desirable-difficulties", "effort"],
        "hook": "The study session that felt easy probably taught you the least.",
        "problem": ["We pick the methods that feel good: rereading, highlighting, clean notes.",
                    "They feel smooth, and smooth feels like learning."],
        "explain": ["But some difficulty during practice helps memory later.",
                    "Struggling to recall, mixing topics, waiting before you review.",
                    "The effort is not a bug. It's the signal that something is being built."],
        "example": ["Two students, same chapter. One rereads it three times.",
                    "The other closes it and tries to rebuild the chapter from memory, and fails a bit.",
                    "Next week, guess who explains it better."],
        "technique": ["Try this: add one deliberate difficulty to your next session.",
                      "Recall before you reread, or mix two topics, or wait a day.",
                      "If it feels harder, you're probably doing it right."],
        "ending": "What's the hardest study method you keep avoiding?",
        "cta_type": "question",
        "web": ["EASY", "FEELS GOOD", "EFFORT", "RECALL", "MIX", "LASTS"],
    },
    "planning-fallacy": {
        "pillar": "DECIDE", "tags": ["planning-fallacy", "estimation"],
        "hook": "Why does everything take twice as long as you promised yourself?",
        "problem": ["You plan the week, and it looks doable. Even relaxed.",
                    "Then Thursday arrives and half the list is untouched."],
        "explain": ["When we plan, we imagine the best-case version of the task.",
                    "We picture ourselves focused, healthy, and uninterrupted.",
                    "We forget the interruptions, because they are not part of the story we tell."],
        "example": ["Think about the last essay, tax form, or apartment move.",
                    "Your guess before, and the real time after. Notice the gap."],
        "technique": ["Try this: before you estimate, ask how long similar tasks took last time.",
                      "Use that number, not the hopeful one.",
                      "Then add the interruptions you already know will come."],
        "ending": "What did you finish on time this month? Anything?",
        "cta_type": "question",
        "web": ["PLAN", "BEST CASE", "REALITY", "LAST TIME", "BUFFER", "DONE"],
    },
    "interleaving": {
        "pillar": "LEARN", "tags": ["interleaving", "practice"],
        "hook": "Studying one topic all day feels great, and it teaches you less.",
        "problem": ["Blocked practice, one skill at a time, feels organised and fluent.",
                    "The fluency is real. The learning is smaller than it feels."],
        "explain": ["When topics are mixed, you have to choose the method each time.",
                    "That choice is exactly what exams and real life demand.",
                    "Blocked practice hands you the method for free, so you never practise choosing."],
        "example": ["A math set with only fractions is easy: every answer uses fractions.",
                    "Mix fractions, ratios and percentages, and now you must notice which is which."],
        "technique": ["Try this: split your session into three topics instead of one.",
                      "Rotate every fifteen minutes, and finish with a mixed quiz.",
                      "Expect it to feel messier. That's the point."],
        "ending": "Which two subjects would you mix first?",
        "cta_type": "try-it",
        "web": ["BLOCK", "FLUENT", "MIX", "CHOOSE", "NOTICE", "TRANSFER"],
    },
    "tip-of-tongue": {
        "pillar": "MEMORY", "tags": ["feeling-of-knowing", "tip-of-the-tongue"],
        "hook": "How can you feel a word you can't say?",
        "problem": ["It's right there. You know the first letter, the shape, the rhythm.",
                    "And still it won't come."],
        "explain": ["That feeling of knowing is your brain's own progress bar.",
                    "It monitors memory even when retrieval fails.",
                    "That's metacognition: a judgement about your own mind, from the inside."],
        "example": ["You blank on an actor's name, walk away, and it pops up in the shower.",
                    "The search kept running quietly after you stopped forcing it."],
        "technique": ["Try this: when a word is stuck, say the first letter and the number of syllables.",
                      "Then stop trying for a minute.",
                      "Give the background search room instead of blocking it."],
        "ending": "What's the last word that got stuck for you?",
        "cta_type": "question",
        "web": ["CUE", "FEELING", "SEARCH", "PAUSE", "POP", "RECALL"],
    },
    "illusion-of-transparency": {
        "pillar": "SELF", "tags": ["illusion-of-transparency", "communication"],
        "hook": "You're sure they understood you. They didn't.",
        "problem": ["You explained the plan clearly. In your head it was obvious.",
                    "Then the work comes back, and it's not what you meant."],
        "explain": ["We overestimate how much of our inner state leaks out.",
                    "You know the context, so your words feel complete.",
                    "The listener only has the words."],
        "example": ["Send a text with a joke and no emoji. Watch how often it lands wrong.",
                    "Your tone was in your head, not in the message."],
        "technique": ["Try this: after you explain something important, ask for it back.",
                      "Not 'did you get it?' but 'what will you do first?'",
                      "The answer shows you what actually arrived."],
        "ending": "When did a message of yours land completely wrong?",
        "cta_type": "share-experience",
        "web": ["YOU", "CONTEXT", "WORDS", "THEM", "ASK BACK", "MATCH"],
    },
    "highlighting": {
        "pillar": "LEARN", "tags": ["highlighting", "rereading"],
        "hook": "Your highlighter remembers more than you do.",
        "problem": ["A page full of yellow feels like work done.",
                    "But marking a sentence is not the same as learning it."],
        "explain": ["Highlighting is passive: your eyes move, your memory doesn't.",
                    "It also fools you, because the marked text feels familiar next time.",
                    "Familiar is not the same as retrievable."],
        "example": ["Open an old textbook. Cover the highlights. Try to say what was under them.",
                    "That silence is the real test result."],
        "technique": ["Try this instead: read a section, close it, write two lines from memory.",
                      "Then compare and fix.",
                      "Slower per page. Faster per thing actually learned."],
        "ending": "Save this for your next study session and try the two-line rule.",
        "cta_type": "save",
        "web": ["MARK", "FAMILIAR", "COVER", "RECALL", "WRITE", "LEARNED"],
    },
    "overconfidence": {
        "pillar": "BIAS", "tags": ["overconfidence", "calibration"],
        "hook": "Being ninety percent sure, and right sixty percent of the time. What does that cost you?",
        "problem": ["Confidence feels like information. Often it's just a mood.",
                    "And the mood is usually higher than the evidence."],
        "explain": ["Calibration means your confidence matches your hit rate.",
                    "Most of us are miscalibrated in one direction: too sure.",
                    "The fix is not less confidence. It's confidence that is tested."],
        "example": ["Before you send the estimate, the answer, or the hot take,",
                    "write a number: how sure am I? Then check later. Keep score."],
        "technique": ["Try this for a week: add a confidence number to ten small predictions.",
                      "Weather, arrival times, quiz answers.",
                      "Then compare. The gap is where your judgement needs work."],
        "ending": "How sure are you that you're well calibrated? Write the number.",
        "cta_type": "try-it",
        "web": ["SURE", "RIGHT", "GAP", "PREDICT", "SCORE", "CALIBRATE"],
    },
    "pre-mortem": {
        "pillar": "DECIDE", "tags": ["pre-mortem", "planning"],
        "hook": "What if you planned your project's funeral before it started?",
        "problem": ["Most plans get reviewed in a mood of optimism.",
                    "Everyone nods, and the doubts stay quiet."],
        "explain": ["A pre-mortem flips the frame: imagine it is a year later and the plan failed.",
                    "Now write down why.",
                    "Imagined failure makes hidden risks easier to say out loud."],
        "example": ["Before a launch, the team spends ten minutes writing failure stories.",
                    "Suddenly the missing budget line and the vague deadline show up."],
        "technique": ["Try this on any decision: set a timer for five minutes.",
                      "Write 'it failed because...' and list every reason you can.",
                      "Then fix the two you can actually control."],
        "ending": "Try a five-minute pre-mortem on your next plan and tell me what showed up.",
        "cta_type": "try-it",
        "web": ["PLAN", "FUTURE", "FAILED", "WHY", "FIX", "SHIP"],
    },
    "self-explanation": {
        "pillar": "LEARN", "tags": ["self-explanation", "teaching"],
        "hook": "Explain it out loud, and you'll find the hole in your own understanding.",
        "problem": ["Reading nods along with the author. Everything seems to follow.",
                    "Then you try to say it yourself and the chain breaks."],
        "explain": ["Self-explanation forces you to connect each step to the one before.",
                    "The connections are what understanding is made of.",
                    "Silent reading lets you skip them without noticing."],
        "example": ["Read one paragraph. Then say: 'this matters because... and that leads to...'",
                    "Where the sentence stalls is where the learning starts."],
        "technique": ["Try this: after each section, teach it to your phone's voice recorder.",
                      "Listen back once.",
                      "Every 'um' marks a place to reread."],
        "ending": "Which topic would you struggle to teach right now?",
        "cta_type": "question",
        "web": ["READ", "WHY", "LINK", "STALL", "TEACH", "UNDERSTAND"],
    },
    "flavell-1979": {
        "pillar": "SELF", "tags": ["flavell", "metacognition-basics"],
        "hook": "There's a watcher in your head, and someone gave it a name in 1979.",
        "problem": ["You plan, you study, you decide, and something quietly rates how it's going.",
                    "Most of us never train that part."],
        "explain": ["Flavell called it metacognition: knowing about your own thinking, and steering it.",
                    "Two halves: what you know about how your mind works,",
                    "and what you do with that knowledge in the moment."],
        "example": ["You feel a chapter is 'done'. The watcher asks: could I explain it?",
                    "That one question is the whole skill in miniature."],
        "technique": ["Try this loop today: before a task, say what 'done' will look like.",
                      "During it, pause once and ask how it's actually going.",
                      "After it, write one line about what you'd change."],
        "ending": "Plan, monitor, evaluate. Which step do you usually skip?",
        "cta_type": "question",
        "web": ["THINKING", "WATCHER", "KNOW", "STEER", "PLAN", "EVALUATE"],
    },
    "checklists": {
        "pillar": "DECIDE", "tags": ["checklists", "experts"],
        "hook": "Why do expert pilots still read a checklist they know by heart?",
        "problem": ["Expertise makes steps automatic. Automatic steps are the easiest to skip.",
                    "And skipping feels exactly like doing."],
        "explain": ["A checklist is memory you don't have to trust.",
                    "It moves the watcher outside your head, onto paper,",
                    "where fatigue and confidence can't edit it."],
        "example": ["Before sending a big email: names right, attachment in, tone read once.",
                    "Three lines. They catch the mistakes you make when you're sure."],
        "technique": ["Try this: pick one task you do weekly and have messed up before.",
                      "Write the five steps you tend to skip.",
                      "Read it before the task, every time, especially when you feel confident."],
        "ending": "What's the task that deserves your first checklist?",
        "cta_type": "try-it",
        "web": ["EXPERT", "AUTO", "SKIP", "LIST", "OUTSIDE", "SAFE"],
    },
    "listening": {
        "pillar": "ATTENTION", "tags": ["listening", "attention"],
        "hook": "When did you last notice you had stopped listening?",
        "problem": ["Someone talks, you nod, and your mind is already drafting a reply.",
                    "You hear the words and miss the point."],
        "explain": ["Attention drifts on its own. That's normal.",
                    "Metacognition is noticing the drift, early, and coming back.",
                    "The skill isn't perfect focus. It's fast return."],
        "example": ["In a meeting, try to summarise the last sentence in your head.",
                    "If you can't, you had already left."],
        "technique": ["Try this: in your next conversation, set one silent check-in.",
                      "Halfway through, ask yourself: what did they just say?",
                      "If you can't answer, ask them. Nobody minds."],
        "ending": "Try one check-in today and notice where your mind went.",
        "cta_type": "try-it",
        "web": ["WORDS", "DRIFT", "NOTICE", "RETURN", "CHECK", "HEARD"],
    },
    "sleep-memory": {
        "pillar": "MEMORY", "tags": ["sleep", "consolidation"],
        "hook": "Why does yesterday's impossible problem look easy this morning?",
        "problem": ["You fought with it all evening and got nowhere.",
                    "Then you slept, and the answer was just sitting there."],
        "explain": ["While you sleep, the brain replays and reorganises what you learned.",
                    "Weak links get pruned, useful ones get stronger.",
                    "So the work continues, without you pushing."],
        "example": ["Cramming until three in the morning trades the replay for a few extra pages.",
                    "Often a bad trade."],
        "technique": ["Try this: study, then do a quick recall test right before bed.",
                      "Sleep on it.",
                      "Test again in the morning and compare."],
        "ending": "What problem would you like to hand to your sleeping brain tonight?",
        "cta_type": "question",
        "web": ["LEARN", "NIGHT", "REPLAY", "PRUNE", "MORNING", "SOLVED"],
    },
    "learning-styles": {
        "pillar": "THINK", "tags": ["learning-styles", "myths"],
        "hook": "Visual learner, auditory learner. Sure? The evidence is thinner than you think.",
        "problem": ["Most of us were sorted into a learning style at school.",
                    "It feels true, because we all have preferences."],
        "explain": ["Preference is real. But matching teaching to a style hasn't shown clear benefits in careful tests.",
                    "What matters more is matching the method to the material.",
                    "Maps for geography. Sound for language. Practice for everything."],
        "example": ["You may love diagrams, but you still learn pronunciation by listening and speaking.",
                    "The task picks the channel, not the label."],
        "technique": ["Try this: for your next topic, ask what form the knowledge naturally takes.",
                      "Then study it in that form, and test yourself in the form the exam uses."],
        "ending": "Were you labelled a learning style at school? Which one?",
        "cta_type": "question",
        "web": ["LABEL", "PREFER", "MATERIAL", "METHOD", "TEST", "LEARN"],
    },
    "superforecasters": {
        "pillar": "PROB", "tags": ["forecasting", "calibration"],
        "hook": "Some ordinary people predict world events better than experts. What's their trick?",
        "problem": ["Most predictions are vague: 'probably', 'likely', 'I think so'.",
                    "Vague predictions can never be wrong, so they never teach you anything."],
        "explain": ["Good forecasters put numbers on their beliefs, then keep score.",
                    "They start from base rates, how often things like this usually happen,",
                    "and update in small steps when new facts arrive."],
        "example": ["Will the project ship on time? Instead of 'probably', say seventy percent.",
                    "Then track ten such calls. Your real hit rate will surprise you."],
        "technique": ["Try this: keep a tiny forecast log for two weeks.",
                      "Question, probability, outcome.",
                      "Nothing improves judgement faster than seeing your own numbers."],
        "ending": "What's one prediction you'd put a number on today?",
        "cta_type": "try-it",
        "web": ["VAGUE", "NUMBER", "BASE RATE", "UPDATE", "SCORE", "BETTER"],
    },
    "kids-metacognition": {
        "pillar": "SELF", "tags": ["development", "children"],
        "hook": "At what age do you first notice your own thinking?",
        "problem": ["Young children often believe they will remember everything.",
                    "Ask a five-year-old how many pictures they can recall, and the answer is 'all of them'."],
        "explain": ["Knowing the limits of your own memory develops slowly.",
                    "It grows through feedback: trying, missing, and noticing the miss.",
                    "Adults are not finished either. We just get better at hiding the misses."],
        "example": ["A child who says 'I need to write this down' has just used metacognition.",
                    "That sentence is a small monitoring miracle."],
        "technique": ["Try this with a kid, or with yourself: before a task, guess the result.",
                      "After it, compare, gently.",
                      "The comparison, not the score, is what builds the watcher."],
        "ending": "What's the first time you remember catching your own mistake?",
        "cta_type": "share-experience",
        "web": ["CHILD", "ALL OF IT", "TRY", "MISS", "NOTICE", "GROW"],
    },
    "diagnosis-bias": {
        "pillar": "THINK", "tags": ["anchoring", "confirmation", "experts"],
        "hook": "Even experts fall for the first answer that fits. Here's the habit that catches it.",
        "problem": ["The first explanation that fits feels like the answer.",
                    "After that, new facts get sorted into 'supports it' or 'ignore it'."],
        "explain": ["That's anchoring plus confirmation: the first idea sets the frame,",
                    "and the frame decides what you notice.",
                    "The fix isn't more knowledge. It's a pause that asks what else this could be."],
        "example": ["A mechanic hears a rattle, blames the exhaust, and stops looking.",
                    "The loose heat shield goes unnoticed for weeks."],
        "technique": ["Try this before you commit to any explanation: name two alternatives out loud.",
                      "Then ask what evidence would separate them.",
                      "One minute. It catches the errors that confidence hides."],
        "ending": "Nothing here is medical advice, just a thinking habit. What was your last first-answer mistake?",
        "cta_type": "question",
        "web": ["FIRST FIT", "FRAME", "IGNORE", "PAUSE", "ALTERNATIVES", "EVIDENCE"],
    },
    "calibration-quiz": {
        "pillar": "PROB", "tags": ["calibration", "quiz"],
        "hook": "How sure are you, really? Let's measure it in one minute.",
        "problem": ["We say 'I'm sure' all day. Almost nobody checks the hit rate.",
                    "So the word 'sure' never learns anything."],
        "explain": ["Calibration is simple: when you say ninety percent, you should be right about nine times in ten.",
                    "Most people are right far less often than their confidence suggests.",
                    "You can't fix a number you never measure."],
        "example": ["Guess the year the first email was sent. Now say how confident you are.",
                    "Do that for ten questions, and a pattern appears fast."],
        "technique": ["Try this: write five factual questions with a friend.",
                      "Answer each with a confidence number, then check together.",
                      "Whoever's numbers match their hits best is the better calibrated, not the smarter one."],
        "ending": "Save this and run the five-question game tonight.",
        "cta_type": "save",
        "web": ["SURE", "GUESS", "NUMBER", "CHECK", "PATTERN", "ADJUST"],
    },
}


# Bridge (end of the problem beat: why it matters to *you*) and recap (start of the
# memorable ending, before the CTA). Kept separate so every playbook lands in the
# 150-260 word / 60-120 s window with a proper "so what" and a proper wrap-up.
EXTRA = {
    "dunning-kruger": {
        "bridge": "And the less you know about something, the louder that feeling of 'simple' gets.",
        "recap": ["So: confidence is not a measurement. It's a guess your brain makes before the test.",
                  "Find the test first. Then let the confidence follow."]},
    "forgetting-curve": {
        "bridge": "It's not that you're bad at remembering. You're just reviewing at the wrong time.",
        "recap": ["So the rule is simple: don't study longer. Come back sooner, then later, then later again.",
                  "Every return is a deposit."]},
    "testing-effect": {
        "bridge": "That's backwards, because the quiz isn't the measurement. The quiz is the workout.",
        "recap": ["So flip it: test first, read second.",
                  "The stumble you feel when you recall is the memory getting stronger, not weaker."]},
    "explanatory-depth": {
        "bridge": "That gap between feeling and knowing has a name: the illusion of explanatory depth.",
        "recap": ["So the next time something feels obvious, treat that as a question, not an answer.",
                  "Explain it. If the words run out, you've just found where to look."]},
    "desirable-difficulties": {
        "bridge": "So the method that feels best is often the one teaching you least.",
        "recap": ["Remember: smooth practice feels like progress. Rough practice usually is progress.",
                  "Pick a little difficulty on purpose, and let the struggle be your signal."]},
    "planning-fallacy": {
        "bridge": "It's not laziness. It's a bias so reliable it has a name: the planning fallacy.",
        "recap": ["So: your gut plans the movie version. Your history knows the real one.",
                  "Estimate from history, not from hope, and pad for the interruptions you already know about."]},
    "interleaving": {
        "bridge": "The problem is that fluency during practice and skill during the test are two different things.",
        "recap": ["So mix it up. Rotate topics, expect a bit of mess, and finish with a mixed quiz.",
                  "Harder now, easier when it counts."]},
    "tip-of-tongue": {
        "bridge": "That stuck feeling is annoying, but it's also a window into how your mind watches itself.",
        "recap": ["So the feeling of knowing is real information, even when the word isn't there yet.",
                  "Give it a cue, then give it room."]},
    "illusion-of-transparency": {
        "bridge": "It's not that they weren't listening. It's that you heard more than you actually said.",
        "recap": ["So: your message is not what you meant. It's what arrived.",
                  "Ask for it back, and let their answer be the real test."]},
    "highlighting": {
        "bridge": "Here's why it fails: your memory only grows when it has to do the work.",
        "recap": ["So put the highlighter down. Read, close, write two lines, then check.",
                  "Learning is what survives when the page is out of sight."]},
    "overconfidence": {
        "bridge": "And the cost isn't feeling wrong. It's all the decisions you made while feeling right.",
        "recap": ["So: don't aim for less confidence. Aim for confidence you can check.",
                  "Ten small predictions this week will tell you more than any pep talk."]},
    "pre-mortem": {
        "bridge": "So the risks stay hidden right up to the moment they can't be fixed.",
        "recap": ["So: imagine the failure first, on paper, while it's still cheap.",
                  "Five minutes of pretend hindsight can save you months of the real thing."]},
    "self-explanation": {
        "bridge": "It's not that you weren't paying attention. It's that reading lets you borrow the author's thinking.",
        "recap": ["So make yourself say it: this matters because, and that leads to.",
                  "Where your own words stall, the understanding hasn't been built yet."]},
    "flavell-1979": {
        "bridge": "That quiet rater is why two people can study the same hour and learn very different amounts.",
        "recap": ["So: plan, monitor, evaluate. Three small questions, once each.",
                  "Train the watcher, and everything else you do gets a little smarter."]},
    "checklists": {
        "bridge": "That's the trap of expertise: the better you get, the less you check.",
        "recap": ["So don't trust your memory when you feel most confident. That's exactly when it edits.",
                  "Put the steps on paper, and let the paper be the watcher."]},
    "listening": {
        "bridge": "It's not rudeness. Attention wanders by default, and nobody taught us to catch it.",
        "recap": ["So the goal isn't never drifting. It's noticing sooner and coming back faster.",
                  "One silent check-in per conversation is enough to start."]},
    "sleep-memory": {
        "bridge": "That's not luck. Your brain kept working on it while you were off duty.",
        "recap": ["So: learn, test, sleep, test again.",
                  "The night is part of the study session, not a break from it."]},
    "learning-styles": {
        "bridge": "But a preference for how you like to study isn't the same as a rule for how you learn best.",
        "recap": ["So drop the label and look at the material.",
                  "Match the method to what you're learning, and test yourself the way you'll be tested."]},
    "superforecasters": {
        "bridge": "It's not that they know more. It's that they can be wrong in a way they can measure.",
        "recap": ["So: put a number on it, start from how often it usually happens, then update slowly.",
                  "Vague beliefs never learn. Numbers do."]},
    "kids-metacognition": {
        "bridge": "That confidence isn't a flaw. It's the starting point every one of us began from.",
        "recap": ["So: guess first, check after, and compare gently.",
                  "The watcher grows from noticing the gap, at five years old or fifty."]},
    "diagnosis-bias": {
        "bridge": "It happens to experienced people precisely because their first guess is usually good.",
        "recap": ["So: the first fit is a hypothesis, not a verdict.",
                  "Name two alternatives, ask what would separate them, and give the truth a chance to show up."]},
    "calibration-quiz": {
        "bridge": "That's a problem, because a confidence you never test can't improve.",
        "recap": ["So: a number, a check, a pattern. That's all calibration is.",
                  "Do it once and 'I'm sure' starts to mean something."]},
}
for _k, _e in EXTRA.items():
    PLAYBOOKS[_k]["bridge"] = _e["bridge"]
    PLAYBOOKS[_k]["recap"] = _e["recap"]


# one more concrete beat for the shorter playbooks (keeps every reel safely above 60 s)
EXAMPLE_PLUS = {
    "listening": "Or a podcast: you look up and realise you have no idea what the last two minutes were about.",
    "sleep-memory": "Same with a piano piece or a new language: the messy evening practice often sounds cleaner the next day.",
    "highlighting": "Most people manage a word or two, and then a long pause.",
    "self-explanation": "Try it with this very reel: why would saying it out loud change anything?",
    "forgetting-curve": "Same number of reviews, different timing, and the memory lasts weeks instead of days.",
    "illusion-of-transparency": "The same thing happens at work: you said 'soon', they heard 'next month'.",
    "tip-of-tongue": "You weren't trying anymore, and that's exactly why it worked.",
    "pre-mortem": "Nobody had lied. The optimism had simply kept those sentences from being spoken.",
    "checklists": "Surgeons and pilots do the same thing, and not because they forgot how.",
    "superforecasters": "Ten calls is small, but it's enough to see whether your seventy percent is really a fifty.",
    "testing-effect": "That stumble feels bad, but it's the most useful feedback of the whole evening.",
    "kids-metacognition": "So is the adult who says 'let me check that before I answer'.",
    "calibration-quiz": "If you were sure five times and right twice, that's the whole lesson right there.",
    "planning-fallacy": "The gap is rarely small, and it almost always points the same way.",
    "overconfidence": "Not to punish yourself. Just to see what your 'sure' is actually worth.",
}
for _k, _line in EXAMPLE_PLUS.items():
    PLAYBOOKS[_k]["example"] = list(PLAYBOOKS[_k]["example"]) + [_line]
TECHNIQUE_PLUS = {
    "sleep-memory": "Keep the test short. Five questions is plenty.",
    "highlighting": "Two lines, not a summary. Just what you remember.",
}
for _k, _line in TECHNIQUE_PLUS.items():
    PLAYBOOKS[_k]["technique"] = list(PLAYBOOKS[_k]["technique"]) + [_line]

# calendar id → playbook key (content/calendar.json ids)
CALENDAR_MAP = {
    2: "dunning-kruger", 3: "forgetting-curve", 4: "testing-effect", 6: "explanatory-depth",
    7: "desirable-difficulties", 9: "planning-fallacy", 10: "calibration-quiz", 11: "interleaving",
    12: "tip-of-tongue", 14: "illusion-of-transparency", 15: "highlighting", 16: "overconfidence",
    18: "pre-mortem", 19: "self-explanation", 20: "flavell-1979", 21: "checklists", 23: "listening",
    24: "sleep-memory", 25: "learning-styles", 26: "superforecasters",
    29: "calibration-quiz", 30: "kids-metacognition", 31: "diagnosis-bias",
}

# Trend topics (limited-claims mode): the trend is only the hook; the body is an
# evergreen metacognitive "lens" chosen by pillar. No numbers, no research claims —
# the supervisor blocks claim words in this mode anyway.
TREND_LENSES = {
    "BIAS": {
        "hook": "{short} is everywhere right now. But how sure should anyone actually be?",
        "problem": ["When a topic gets loud, confidence goes up faster than knowledge does.",
                    "You hear strong opinions all day and start to feel you have one too."],
        "explain": ["Your brain reads familiarity as truth. Hear a claim often enough and it feels obvious.",
                    "That feeling is fluency, not knowledge.",
                    "So the loudest week is exactly when your judgement is least reliable."],
        "example": ["Take {short}. Ask yourself: what would I have said about this a month ago?",
                    "If the answer is nothing, your new certainty came from repetition, not from checking."],
        "technique": ["Try this before you repeat any hot take: give it a confidence number out loud.",
                      "Then name one thing that would change your mind.",
                      "If you can't name it, you don't have an opinion yet. You have a headline."],
        "bridge": "The topic isn't the problem. The speed of your certainty is.",
        "recap": ["So: repetition makes things feel true, and a loud week is when that feeling peaks.",
                  "Ask how sure you are before you repeat anything."],
        "ending": "What's the last strong opinion you picked up without checking? Be honest.",
        "cta_type": "share-experience",
        "web": ["HEADLINE", "REPEAT", "FEELS TRUE", "HOW SURE?", "WHAT CHANGES IT?", "OPINION"],
        "tags": ["trend", "fluency-check"],
    },
    "LEARN": {
        "hook": "Everyone's suddenly learning about {short}. Most of them will forget it by Friday.",
        "problem": ["Reading about a hot topic feels like learning it.",
                    "But recognising an idea and being able to use it are two different skills."],
        "explain": ["When you read, the words are in front of you, so everything feels clear.",
                    "Close the tab and that clarity disappears, because you never had to produce it.",
                    "Understanding appears when you explain, not when you nod."],
        "example": ["Try it with {short}. Close this video and explain it to an imaginary friend in three sentences.",
                    "Notice exactly where you stall. That gap was invisible one minute ago."],
        "technique": ["Try this whenever a topic is trending: read, close, explain out loud, then reopen.",
                      "Fix only the part you got wrong.",
                      "One round of that beats ten more articles."],
        "bridge": "That's not a memory problem. It's a testing problem: you never checked.",
        "recap": ["So: read, close, explain, fix. One round.",
                  "The gap you find is worth more than the next article."],
        "ending": "Which trending topic could you actually explain right now? Try it before you answer.",
        "cta_type": "try-it",
        "web": ["READ", "FEELS CLEAR", "CLOSE", "EXPLAIN", "GAP", "FIX"],
        "tags": ["trend", "explain-test"],
    },
    "ATTENTION": {
        "hook": "{short} just ate an hour of your attention. Did you decide to give it?",
        "problem": ["Trending topics don't ask for your attention. They take it, one small tap at a time.",
                    "And each tap feels tiny, so you never notice the total."],
        "explain": ["Your attention runs on cues, not on plans.",
                    "A notification, a headline, a thumbnail: each one restarts your focus from zero.",
                    "The cost isn't the look. It's the minutes it takes to get back."],
        "example": ["Say {short} pops up while you're working.",
                    "You check it for a minute, and twenty minutes later you're still reading about it."],
        "technique": ["Try this: when a topic pulls at you, write it on a note instead of opening it.",
                      "Give it a time: I'll read this at six.",
                      "Half the time, by six you won't care. That's your attention choosing on purpose."],
        "bridge": "That's how a small tap becomes a lost hour, without a single decision being made.",
        "recap": ["So: write it down, give it a time, and let your attention choose later, on purpose.",
                  "Most of what pulls at you can wait."],
        "ending": "What pulled your focus today that you never actually chose? Name it.",
        "cta_type": "question",
        "web": ["CUE", "TAP", "RESTART", "NOTE IT", "LATER", "CHOOSE"],
        "tags": ["trend", "attention-cue"],
    },
    "DECIDE": {
        "hook": "Everyone has a take on {short}. Almost nobody has written down what they expect to happen.",
        "problem": ["A take is cheap. A prediction you can be wrong about is expensive.",
                    "So we make takes, and later we remember them as predictions."],
        "explain": ["Your memory quietly edits your past opinions to match what actually happened.",
                    "That's why everyone feels like they saw it coming.",
                    "The only defence is a record you can't edit."],
        "example": ["Take {short}. Write one sentence about where you think it ends up, and a date.",
                    "Put it somewhere you'll see again. That's it."],
        "technique": ["Try this for a month: every time you have a strong take, write the prediction and the date.",
                      "When the date arrives, score it honestly.",
                      "The scoreboard teaches you more than any article."],
        "bridge": "So the confidence keeps growing, and the judgement behind it never gets tested.",
        "recap": ["So: write the prediction, add the date, and score it later.",
                  "A record you can't edit is the only honest mirror for your judgement."],
        "ending": "What's your prediction on this one? Write it down before you scroll on.",
        "cta_type": "try-it",
        "web": ["TAKE", "PREDICT", "DATE", "EDIT", "RECORD", "SCORE"],
        "tags": ["trend", "prediction-log"],
    },
}
GENERIC_LENS = {
    "hook": "Everyone's talking about {short}. But how do you know what's actually true?",
    "problem": ["A story spreads because it's interesting, not because it's checked.",
                "By the time you've formed an opinion, you rarely remember where it came from."],
    "explain": ["Your brain treats a familiar claim as a true claim.",
                "Hear it three times and it starts to feel obvious.",
                "That feeling is fluency, not knowledge."],
    "example": ["Take {short}. Ask who measured it, who benefits from you believing it, and what would change their mind.",
                "Usually at least one of those answers is missing, and that's the part worth noticing."],
    "technique": ["Try this before you share anything: pause for ten seconds.",
                  "Name the source out loud. Then name one reason it could be wrong.",
                  "If you can't do either, you don't have an opinion yet. You have a headline."],
    "bridge": "That's how a headline quietly becomes your opinion.",
    "recap": ["So: pause, name the source, name one way it could be wrong.",
              "If you can't, you're holding a headline, not a view."],
    "ending": "What's the last thing you shared without checking? Be honest.",
    "cta_type": "share-experience",
    "web": ["HEADLINE", "REPEAT", "FEELS TRUE", "SOURCE", "DOUBT", "OPINION"],
    "tags": ["trend", "source-check"],
}


STOP_LEAD = re.compile(r"^(show hn|ask hn|tell hn|launch hn|new study|a new study|study|research|paper|opinion|analysis|"
                       r"breaking|i built|we built|i made|we made|i wrote|introducing|"
                       r"why|how|what|when|where|the|a|an|is|are|do|does|did|can|should|could|would|will)\b[:\s-]*", re.I)
CUT_AT = re.compile(r"\s+(?:is|are|was|were|and why|and how|and the|because|but|that|which|who|when|while|so that|"
                    r"explained|—|–|-|:|;|,|\(|\[)\s+", re.I)
CUT_LATE = re.compile(r"\s+(?:to|for|in|on|at|with|without|from|by|of|and)\s+", re.I)   # only when still too long
TAIL_JUNK = re.compile(r"\s+(and|or|of|in|for|to|the|a|an|is|are|was|were|with|by|on|at|that|your|my|our|their|"
                       r"app|tool|website|extension|plugin|startup|library|framework|api|sdk|cli|bot|game)$", re.I)
KNOWN_NAMES = {"Bayes", "Kahneman", "Tversky", "Ebbinghaus", "Flavell", "Tetlock", "Dunning", "Kruger", "Bjork",
               "Roediger", "Karpicke", "Gawande", "Klein", "Gilovich", "Pashler", "Dunlosky", "Feynman", "Occam",
               "Hanlon", "Pareto", "Parkinson", "Peter", "Goodhart", "Simpson", "Zeigarnik", "Stroop", "Piaget"}
COMMON_CAPS = {"why", "how", "what", "when", "the", "a", "an", "new", "show", "ask", "cognitive", "brain", "smart",
               "sleep", "memory", "study", "overconfidence", "metacognitive", "attention", "focus", "decision", "people",
               "most", "your", "you", "we", "our", "i", "it", "this", "that", "these", "those", "one", "two", "three"}


def short_title(title, limit=44):
    """Turn a headline into a short noun phrase that can sit inside a spoken sentence.

    'The Dunning-Kruger effect is autocorrelation'  -> 'the Dunning-Kruger effect'
    'Cognitive load theory and why your onboarding docs fail' -> 'cognitive load theory'
    'Why LLMs hallucinate: a study of confidence calibration…' -> 'LLM hallucination' is out of reach; we
    keep 'LLMs hallucinate' -> readable and short.
    Proper nouns and acronyms keep their case; everything else is lower-cased for mid-sentence use."""
    t = re.sub(r"\s+", " ", title or "").strip().rstrip(".?!")
    t = re.sub(r"^[\"'“”‘’]+|[\"'“”‘’]+$", "", t)
    t = STOP_LEAD.sub("", t)
    for _ in range(3):
        t2 = STOP_LEAD.sub("", t)
        if t2 == t:
            break
        t = t2
    # cut at the first clause boundary / punctuation once the phrase has ≥2 words
    m = CUT_AT.search(t)
    while m and len(t[:m.start()].split()) < 2:
        m = CUT_AT.search(t, m.end())
    if m:
        t = t[:m.start()]
    t = re.split(r"[:;(\[]", t)[0].strip(" -–—,")
    if len(t.split()) > 5:                                   # still long → cut at a late preposition after ≥3 words
        m = CUT_LATE.search(t)
        while m and len(t[:m.start()].split()) < 3:
            m = CUT_LATE.search(t, m.end())
        if m:
            t = t[:m.start()]
    if len(t) > limit:
        t = t[:limit].rsplit(" ", 1)[0]
        m = list(CUT_LATE.finditer(t))
        if m and len(t[:m[-1].start()].split()) >= 2:
            t = t[:m[-1].start()]                            # never end on a dangling prepositional fragment
    for _ in range(3):
        t2 = TAIL_JUNK.sub("", t).strip()
        if t2 == t:
            break
        t = t2
    words = t.split()
    if not words:
        words = (title or "this topic").split()[:4]
    # keep acronyms and proper names; lower-case ordinary words for mid-sentence use.
    # A capitalised FIRST word of the headline is ambiguous (sentence case) → treated as proper only if it
    # is in the known-names list or looks like a surname followed by a name-marker.
    out = []
    first_headline_word = (title or "").split()[:1]
    for w in words:
        base = w.strip(",.:;()")
        if base.isupper() or base[:2].isupper() or base.lower().startswith("llm"):
            out.append(w)                                   # LLM, AI, GPT-5, LLMs
        elif "-" in base and base[:1].isupper():
            out.append(w)                                   # Dunning-Kruger, Tversky-Kahneman
        elif base[:1].isupper() and base.lower() not in COMMON_CAPS and (
                [w] != first_headline_word or re.sub(r"('s|')$", "", base) in KNOWN_NAMES):
            out.append(w)                                   # Kahneman, Ebbinghaus, Bayes'
        else:
            out.append(w.lower())
    phrase = " ".join(out)
    # 'the cognitive load theory' is wrong, 'the Dunning-Kruger effect' is right: only add 'the' after a name
    if re.match(r"^[A-Z][\w-]+ (effect|theory|bias|fallacy|paradox|principle|method|rule|law|curve|theorem)$", phrase) \
            and not re.match(r"^(the|a|an)\b", phrase, re.I):
        phrase = "the " + phrase                                # the Dunning-Kruger effect (but: Bayes' theorem)
    return phrase


def cap_first(s):
    return s[:1].upper() + s[1:] if s else s


SINGLE_WORD_OK = {"overconfidence", "procrastination", "multitasking", "metacognition", "calibration", "interleaving",
                  "forgetting", "mnemonics", "flashcards", "journaling", "mindfulness", "focus", "attention", "memory",
                  "hindsight", "anchoring", "priming", "heuristics", "burnout", "boredom", "curiosity", "intuition"}
VERB_LIKE = re.compile(r"\b(is|are|was|were|be|been|being|do|does|did|has|have|had|will|would|can|could|should|"
                       r"built|made|wrote|launch|launched|release|released|announce|announced|explained|impairs|"
                       r"beats|kills|loves|hates|believe|believes|hallucinate|stop|start|get|got|make|makes|"
                       r"you|your|we|our|i|my|me|us|them|they|he|she|it)\b", re.I)


def speakable_topic(title):
    """(ok, short, why): can this headline be spoken as a noun phrase inside the trend-lens sentences?
    Trend lenses say things like 'Take {short}.' / 'Try it with {short}.' — that only works for a
    noun phrase of 1–6 words without verbs/pronouns. Anything else → the scout skips the candidate."""
    short = short_title(title)
    n = len(short.split())
    if n < 1 or n > 6:
        return False, short, f"{n} words"
    if VERB_LIKE.search(short):
        return False, short, "contains a verb/pronoun (not a noun phrase)"
    if re.search(r"[?!\"“”]", short) or len(short) < 4:
        return False, short, "punctuation/too short"
    if n < 2 and short.lower() not in SINGLE_WORD_OK:
        return False, short, "single word is too vague for a hook"
    return True, short, ""


def trend_playbook(topic_title, pillar):
    lens = TREND_LENSES.get(pillar, GENERIC_LENS)
    short = short_title(topic_title)
    pb = {"pillar": pillar, "claim_note": "trend topic: no research claims; evergreen technique through a pillar lens."}
    for k, v in lens.items():
        if isinstance(v, str):
            pb[k] = cap_first(v.format(short=short))
        elif k in ("web", "tags", "cta_type"):
            pb[k] = list(v) if isinstance(v, list) else v
        else:                                   # problem/explain/example/technique/recap
            pb[k] = [cap_first(x.format(short=short)) for x in v]
    return pb


# --------------------------------------------------------------- helpers
SCENE_FOR_BEAT = {"hook": "hook", "problem": "problem", "explain": "explain",
                  "example": "example", "technique": "technique", "ending": "ending"}


def lines_for(pb, beat):
    v = pb[beat]
    return [v] if isinstance(v, str) else list(v)


def chunk_plan(pb, variant=0):
    """Chunks = TTS units. Lines within a chunk share one mp3 (natural flow)."""
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
    if variant == 1:
        # retry variant: slightly shorter, more spoken cadence (drop nothing essential)
        plan = [(b, [re.sub(r"\s+", " ", l).strip() for l in ls]) for b, ls in plan]
    return plan


def spoken_form(text, overrides):
    """Pronunciation overrides for TTS only (subtitles keep the original)."""
    s = text
    for k, v in sorted(overrides.items(), key=lambda kv: -len(kv[0])):
        s = s.replace(k, v)
    s = re.sub(r"https?://\S+", "the link", s)
    return s


# --------------------------------------------------------------- translation
# Machine translators render English idioms literally ("Sleep on it" → "روی آن بخواب"). The narration
# keeps its idioms (they are the conversational voice), but the TRANSLATOR receives a plain-meaning
# rewrite of the same line. Longest match first; case-insensitive; word-bounded.
TRANSLATION_GLOSS = {
    "sleep on it": "wait until the next morning before deciding",
    "hot take": "quick opinion",
    "hot topic": "popular topic",
    "nailed it": "did it perfectly",
    "your gut": "your intuition",
    "a window into": "a way to observe",
    "off duty": "not actively working",
    "the movie version": "the ideal version",
    "pep talk": "motivational speech",
    "keep score": "record the results",
    "keeps score": "records the results",
    "hit rate": "success rate",
    "by heart": "from memory",
    "empty chair": "imaginary listener",
    "runs high": "is too high",
    "pops up": "suddenly appears",
    "pop up": "suddenly appear",
    "pulls at you": "attracts your attention",
    "pull one back": "recall one",
    "lands wrong": "is misunderstood",
    "when did a message of yours land completely wrong": "when was a message of yours completely misunderstood",
    "what sticks": "what stays in memory",
    "the catch": "the hidden problem",
    "here's why it fails": "this is why it does not work",
    "you blank on": "you cannot recall",
    "fall for": "are fooled by",
    "falls for": "is fooled by",
    "a bad trade": "a bad exchange",
    "on paper": "in writing",
    "out loud": "aloud",
    "for free": "without effort",
    "cheap": "low-cost",
    "expensive": "costly",
    "loud week": "week of intense discussion",
    "gets loud": "becomes widely discussed",
    "the loudest week": "the week of most intense discussion",
    "by friday": "within a few days",
    "gone by friday": "forgotten within a few days",
    "cram session": "last-minute study session",
    "cramming": "last-minute studying",
    "in the shower": "later, while doing something else",
    "clean notes": "neat notes",
    "a take": "an opinion",
    "make takes": "form opinions",
    "strong take": "strong opinion",
    "scroll on": "continue browsing",
    "two-line rule": "rule of writing two lines",
    "the explain test": "the explanation test",
    "the whole skill in miniature": "the entire skill in small form",
    "the watcher": "the inner observer",
    "monitoring miracle": "impressive act of self-monitoring",
    "rate yourself": "evaluate yourself",
    "pretend hindsight": "imagined hindsight",
    "the real thing": "the real failure",
    "messier": "less tidy",
    "mess": "disorder",
    "that stumble": "that hesitation",
    "the stumble": "the hesitation",
    "stumble on": "have difficulty with",
    "if the words run out": "if you cannot continue explaining",
    "where the words run out": "where you can no longer explain",
    "quiz that again": "test that again",
    "progress bar": "progress indicator",
    "the shape, the rhythm": "its form, its rhythm",
    "it won't come": "you cannot recall it",
    "dumb things": "false things",
    "smart people": "intelligent people",
}
_GLOSS_RX = [(re.compile(rf"(?<![\w'])({re.escape(k)})(?![\w'])", re.I), v)
             for k, v in sorted(TRANSLATION_GLOSS.items(), key=lambda kv: -len(kv[0]))]


def translation_source(text):
    """Plain-meaning English used as translator input (never shown, never spoken).
    Keeps the capitalisation of the matched span ('The watcher' → 'The inner observer')."""
    s = text
    for rx, v in _GLOSS_RX:
        s = rx.sub(lambda m, v=v: cap_first(v) if m.group(1)[:1].isupper() else v, s)
    return s


PERSIAN_FIXES = [
    (re.compile(r"ي"), "ی"), (re.compile(r"ك"), "ک"),  # Arabic → Persian letterforms FIRST (for ZWNJ rules)
    (re.compile(r"\s+([،؛:؟!.])"), r"\1"),           # no space before punctuation
    (re.compile(r"([،؛])(?=\S)"), r"\1 "),              # space after comma/semicolon
    (re.compile(r"\bمی (?=\S)"), "می\u200c"),           # می + ZWNJ
    (re.compile(r"\bنمی (?=\S)"), "نمی\u200c"),
    (re.compile(r" ها\b"), "\u200cها"),
    (re.compile(r" های\b"), "\u200cهای"),
    (re.compile(r" تر\b"), "\u200cتر"),
    (re.compile(r" ترین\b"), "\u200cترین"),
    (re.compile(r"\?"), "؟"), (re.compile(r","), "،"), (re.compile(r";"), "؛"),
    (re.compile(r"\s{2,}"), " "),
]


def polish_fa(s):
    s = (s or "").strip()
    for rx, rep in PERSIAN_FIXES:
        s = rx.sub(rep, s)
    return s.strip()


def translation_valid(en, fa, pol):
    """Hard checks used both here and by the QA supervisor."""
    fa = fa or ""
    if not fa.strip():
        return False, "empty"
    if common.persian_ratio(fa) < 0.7:
        return False, f"persian ratio {common.persian_ratio(fa):.2f} < 0.7"
    if common.PLACEHOLDER_RE.search(fa):
        return False, "placeholder"
    if fa.strip() == en.strip():
        return False, "identical to English"
    latin = [w for w in common.latin_words(fa) if w.lower() not in ("metacognition", "hq", "metacognition.hq")]
    if len(latin) > 2:
        return False, f"untranslated words {latin[:4]}"
    if len(fa) > pol["length"]["max_chars_per_line_fa"] * 2:
        return False, f"too long ({len(fa)} chars)"
    src = translation_source(en)                     # what the translator actually received
    ratio = len(fa) / max(1, len(src))
    # Relaxed for curated short mobile subtitles (idioms like "Sleep on it." -> short FA)
    # Original 0.35-2.6 was too strict for natural Persian
    if ratio < 0.25 or ratio > 3.0:
        return False, f"length ratio {ratio:.2f} suspicious"
    return True, ""


FIXTURE_FA = [
    "این جمله فقط برای آزمایش محلی است و در انتشار واقعی استفاده نمی‌شود.",
    "ذهن ما آشنایی را با درستی اشتباه می‌گیرد و همین نقطهٔ شروع خطاست.",
    "پیش از آنکه چیزی را تکرار کنی، یک لحظه بایست و از خودت بپرس چقدر مطمئنی.",
    "این احساسِ روشن‌بودن، شواهد نیست؛ فقط تکرار است.",
    "یک سؤال ساده می‌تواند فرق بین دانستن و حدس‌زدن را نشان بدهد.",
]


def fixture_translator(lines):
    """Offline stand-in used ONLY when TRANSLATE_FIXTURE=1 (local tests). Produces valid,
    clearly-labelled Persian so render/QA code paths can run without network."""
    out = []
    for i, ln in enumerate(lines):
        base = FIXTURE_FA[i % len(FIXTURE_FA)]
        ln = translation_source(ln)                  # same length reference as the validator
        # keep the length ratio plausible for the validator, but never cut a sentence mid-word
        while len(base) < 0.5 * len(ln):
            base += " " + FIXTURE_FA[(i + 1) % len(FIXTURE_FA)]
        limit = int(2.2 * len(ln)) + 5
        if len(base) > limit:
            cut = base.rfind(" ", 0, limit)
            base = base[:cut if cut > 20 else limit]
        out.append(base.rstrip(" ،"))
    return out, "fixture", []


def load_curated_catalog():
    cat = common.load_fa_catalog()
    if not cat:
        return {}
    # translations dict: hash -> {en, fa}
    return cat.get("translations", {})

def curated_translator(lines):
    """Use version-controlled FA catalog. Returns (fa_lines, engine, failures)"""
    catalog = load_curated_catalog()
    if not catalog:
        return None, None, ["curated catalog missing"]
    out = []
    fails = []
    for ln in lines:
        h = common.en_hash(ln)
        entry = catalog.get(h)
        if not entry or not entry.get("fa"):
            fails.append(f"curated: missing translation for hash {h} en='{ln[:60]}'")
            continue
        # also check that stored en matches (hash collision guard) - compare normalized
        stored_en = entry.get("en", "")
        if common.en_hash(stored_en) != h:
            fails.append(f"curated: hash mismatch for '{ln[:40]}'")
            continue
        fa = polish_fa(entry["fa"])
        out.append(fa)
    if fails:
        return None, None, fails
    return out, "curated", []

def load_trend_fa_templates():
    data = common.load_fa_trend_templates()
    if not data:
        return {}
    return data.get("templates", {})

def translate_short_via_glossary(short_en):
    """Translate dynamic short topic via glossary if possible, else None"""
    gloss = common.load_fa_glossary()
    if not gloss:
        return None
    terms = gloss.get("terms", {})
    # exact lower match
    key = short_en.lower().strip()
    if key in terms:
        return terms[key]
    # try without 'the '
    if key.startswith("the "):
        k2 = key[4:]
        if k2 in terms:
            return terms[k2]
    # try contains known term
    for en_term, fa_term in terms.items():
        if en_term.lower() in key:
            # replace only whole words, not in-word
            # For safety, only return if short_en is exactly that term or contains it as phrase
            # We will do simple containment check for now
            if en_term.lower() == key:
                return fa_term
    return None

def trend_curated_translator(lines, topic_title, pillar):
    """For trend lenses: use curated FA templates. Returns (fa_lines, engine, fails) or None if not possible"""
    templates = load_trend_fa_templates()
    if not templates:
        return None, None, ["trend FA templates missing"]
    lens = templates.get(pillar) or templates.get("GENERIC")
    if not lens:
        return None, None, [f"no FA template for pillar {pillar}"]
    short_en = short_title(topic_title)
    short_fa = translate_short_via_glossary(short_en)
    if short_fa is None:
        return None, None, [f"trend short '{short_en}' not in glossary - cannot guarantee FA quality"]
    fa_templates = lens.get("fa", {})
    pb_fa = {}
    for k, v in fa_templates.items():
        if isinstance(v, str):
            pb_fa[k] = v.format(short=short_fa)
        elif k in ("web", "tags", "cta_type"):
            pb_fa[k] = v
        else:
            pb_fa[k] = [x.format(short=short_fa) for x in v]

    def _lines_for(pb, beat):
        vv = pb.get(beat)
        if not vv:
            return []
        return [vv] if isinstance(vv, str) else list(vv)

    fa_all = []
    fa_all += _lines_for(pb_fa, "hook")
    fa_all += _lines_for(pb_fa, "problem")
    if pb_fa.get("bridge"):
        fa_all.append(pb_fa["bridge"])
    ex = _lines_for(pb_fa, "explain")
    if len(ex) >= 3:
        fa_all += ex[:2]
        fa_all += ex[2:]
    else:
        fa_all += ex
    fa_all += _lines_for(pb_fa, "example")
    te = _lines_for(pb_fa, "technique")
    if len(te) >= 3:
        fa_all += te[:2]
        fa_all += te[2:]
    else:
        fa_all += te
    if pb_fa.get("recap"):
        fa_all += list(pb_fa["recap"])
    fa_all += _lines_for(pb_fa, "ending")

    if len(fa_all) != len(lines):
        return None, None, [f"trend FA template length mismatch {len(fa_all)} vs {len(lines)}"]
    return fa_all, "curated-trend", []

def translate_lines(lines, pol, engines=None):
    """Google → MyMemory; each line validated; returns (fa_lines, engine, failures)."""
    if os.environ.get("TRANSLATE_FIXTURE") == "1":
        return fixture_translator(lines)
    # In production auto-publish mode, MyMemory-only is blocking error
    # This function is still used for draft proposals, but QA will block mymemory-only for auto-publish
    fails = []
    engines = engines or ("google", "mymemory")
    for eng in engines:
        try:
            if eng == "google":
                from deep_translator import GoogleTranslator
                tr = GoogleTranslator(source="en", target="fa")
            else:
                from deep_translator import MyMemoryTranslator
                tr = MyMemoryTranslator(source="en-US", target="fa-IR")
            out = []
            bad = []
            for ln in lines:
                fa = polish_fa(tr.translate(translation_source(ln)))
                ok, why = translation_valid(ln, fa, pol)
                if not ok:
                    bad.append(f"{eng}: '{ln[:40]}' → {why}")
                out.append(fa)
            if not bad:
                return out, eng, fails
            fails += bad
        except Exception as e:                                # noqa: BLE001
            fails.append(f"{eng}: {type(e).__name__}: {str(e)[:120]}")
    return None, None, fails


# --------------------------------------------------------------- script
def build_script(topic, pol, variant=0, translate=True, translator=None):
    cal = topic.get("calendar") or {}
    key = CALENDAR_MAP.get(cal.get("id")) if cal else None
    is_calendar = bool(key) or topic.get("evidence_mode") == "calendar"
    if key:
        pb = PLAYBOOKS[key]
        playbook_key = key
    elif topic.get("evidence_mode") == "calendar":
        raise SystemExit(f"[producer] calendar id {cal.get('id')} has no playbook — script-error")
    else:
        pb = trend_playbook(topic["title"], topic.get("pillar", "THINK"))
        playbook_key = "trend"

    plan = chunk_plan(pb, variant)
    overrides = pol.get("tts", {}).get("pronunciation_overrides", {})
    chunks = []
    all_en = []
    for i, (beat, lines) in enumerate(plan, 1):
        en = [{"t": l, "scene": SCENE_FOR_BEAT[beat], "beat": beat} for l in lines]
        chunks.append({"id": f"c{i}", "beat": beat, "en": en, "fa": [],
                       "tts_text": " ".join(spoken_form(l, overrides) for l in lines)})
        all_en += lines

    engine, fails = None, []
    if translate:
        if translator is not None:
            fa_all, engine, fails = translator(all_en)
        else:
            # Calendar content must use curated catalog (100% coverage, no external dep)
            if is_calendar:
                fa_all, engine, fails = curated_translator(all_en)
                if fa_all is None:
                    # Fail closed: missing curated translation blocks publish
                    raise SystemExit("[producer] curated translation missing — translation-error\n  " + "\n  ".join(fails[:12]))
            else:
                # Trend: try curated trend templates (fail-closed if short not in glossary)
                fa_all, engine, fails = trend_curated_translator(all_en, topic["title"], topic.get("pillar", "THINK"))
                if fa_all is None:
                    # If trend cannot be translated via curated, raise error that will cause fallback to calendar in pipeline
                    raise SystemExit("[producer] trend curated translation not available — translation-error\n  " + "\n  ".join(fails[:8]))
        if fa_all is None:
            raise SystemExit("[producer] translation failed validation on all engines — translation-error\n  "
                             + "\n  ".join(fails[:8]))
        # Validate each FA via translation_valid (also checks persian ratio, etc.)
        # For curated, we still validate but allow curated to pass even if length ratio slightly off? We keep validation
        for en_l, fa_l in zip(all_en, fa_all):
            ok, why = translation_valid(en_l, fa_l, pol)
            if not ok:
                raise SystemExit(f"[producer] curated translation failed validation — translation-error\n  en='{en_l[:60]}' fa='{fa_l[:60]}' reason={why}")
        k = 0
        for ch in chunks:
            n = len(ch["en"])
            ch["fa"] = fa_all[k:k + n]
            k += n
    else:
        for ch in chunks:
            ch["fa"] = ["" for _ in ch["en"]]

    tag = topic["content_date"]
    sources = []
    if cal:
        for s in cal.get("sources", [])[:3]:
            sources.append({"label": s, "url": "", "tier": "A" if any(y in s for y in ("(19", "(20")) else "B",
                            "role": "evidence"})
    disc = topic.get("discovery_source") or {}
    if disc.get("url"):
        sources.append({"label": disc.get("name", "discovery"), "url": disc["url"],
                        "tier": disc.get("tier", "C"), "role": "discovery"})

    hashtag_pool = {
        "LEARN": ["#learningscience", "#studytips", "#howtolearn"],
        "MEMORY": ["#memory", "#learningscience", "#spacedrepetition"],
        "ATTENTION": ["#attention", "#focus", "#deepwork"],
        "BIAS": ["#cognitivebias", "#overconfidence", "#criticalthinking"],
        "DECIDE": ["#decisionmaking", "#mentalmodels", "#planning"],
        "THINK": ["#criticalthinking", "#reasoning", "#mentalmodels"],
        "SOLVE": ["#problemsolving", "#creativity", "#mentalmodels"],
        "SELF": ["#selfawareness", "#metacognition", "#reflection"],
        "PROB": ["#probability", "#forecasting", "#calibration"],
        "MODELS": ["#mentalmodels", "#decisionmaking", "#criticalthinking"],
    }
    tags = list(dict.fromkeys(pol["hashtag_policy"]["always"] + hashtag_pool.get(pb["pillar"], [])[:3]
                              + ["#cognitivescience"]))[: pol["hashtag_policy"]["max"]]

    explain_lines = lines_for(pb, "explain")
    tech_lines = lines_for(pb, "technique")
    caption = {
        "hook": pb["hook"],
        "intro": " ".join(lines_for(pb, "problem")),
        "sections": [
            {"icon": "🧠", "title": "WHAT'S GOING ON", "lines": explain_lines},
            {"icon": "✦", "title": "TRY THIS", "lines": tech_lines},
        ],
        "sources": [s["label"] + (f" — {s['url']}" if s["url"] else "") for s in sources],
        "ctas": [pb["ending"]],
        "hashtags": tags,
    }

    script = {
        "meta": {"title": f"Auto reel {tag} — {topic['title']}", "topic": topic["title"],
                 "content_id": topic["content_id"], "content_date": tag, "pillar": pb["pillar"],
                 "playbook": playbook_key, "variant": variant, "tags": pb.get("tags", []),
                 "cta_type": pb.get("cta_type", "question"), "evidence_mode": topic.get("evidence_mode"),
                 "claim_note": pb.get("claim_note", ""), "translation_engine": engine,
                 "note": topic.get("fallback_reason", ""), "handle": HANDLE,
                 "fps": 30, "w": 1080, "h": 1920, "gap": 0.34, "lead": 0.55, "tail": 1.2,
                 "logo": "stand-in"},
        "scene_tags": {"hook": "01 · THE QUESTION", "problem": "02 · THE PROBLEM",
                       "explain": "03 · WHAT'S GOING ON", "example": "04 · IN REAL LIFE",
                       "technique": "05 · TRY THIS", "ending": "06 · YOUR TURN"},
        "web": pb["web"],
        "sources": sources,
        "chunks": chunks,
        "caption": caption,
    }
    return script


def install_http_timeout(seconds=25):
    """deep_translator calls requests without a timeout; a stalled free endpoint must not hang the run
    (the pipeline also has a subprocess timeout as the outer backstop)."""
    import socket
    socket.setdefaulttimeout(seconds)
    try:
        import requests.adapters as ra
    except Exception:                                         # noqa: BLE001
        return
    if getattr(ra.HTTPAdapter.send, "_mc_timeout", False):
        return
    orig = ra.HTTPAdapter.send

    def send(self, request, *args, **kw):
        if kw.get("timeout") is None:
            kw["timeout"] = seconds
        return orig(self, request, *args, **kw)
    send._mc_timeout = True
    ra.HTTPAdapter.send = send


def main():
    install_http_timeout(25)
    ap = argparse.ArgumentParser()
    ap.add_argument("--topic", required=True)
    ap.add_argument("--out", required=True, help="episode dir")
    ap.add_argument("--variant", type=int, default=0)
    ap.add_argument("--no-translate", action="store_true")
    ap.add_argument("--policy", default=None)
    a = ap.parse_args()
    pol = common.policy(a.policy)
    topic = common.load_json(a.topic)
    if not topic:
        raise SystemExit("[producer] topic.json missing — trend-error")
    script = build_script(topic, pol, variant=a.variant, translate=not a.no_translate)
    os.makedirs(a.out, exist_ok=True)
    common.save_json(os.path.join(a.out, "script.json"), script)
    words = sum(common.word_count(l["t"]) for ch in script["chunks"] for l in ch["en"])
    print(f"[producer] script → {a.out}/script.json  playbook={script['meta']['playbook']} "
          f"chunks={len(script['chunks'])} words={words} translator={script['meta']['translation_engine']}")


if __name__ == "__main__":
    main()
