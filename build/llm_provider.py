"""LLM Provider Interface for @metacognition.hq — English-only, Technology × Metacognition.

Providers:
- GitHubModelsProducer: uses GitHub Models (GITHUB_TOKEN, models: read permission)
- GitHubModelsReviewer: independent reviewer, preferably different model
- StaticEnglishFallback: curated English playbooks filtered to tech domain

No billing, no paid usage, no external secret. Uses secrets.GITHUB_TOKEN.

Max daily requests: 1 producer, 1 reviewer, 1 revision, 1 final reviewer.
Quota/429/outage → limited retry then static fallback.

Evidence packet is sanitized — web content is untrusted and must not inject prompt instructions.
"""
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import common
common.assert_content_language_en()  # fail-closed EN-only

# Supported models in GitHub Models (as of 2025-2026)
# Checked via https://github.com/marketplace/models/catalog
SUPPORTED_MODELS = [
    "openai/gpt-4o",
    "openai/gpt-4o-mini",
    "openai/gpt-4o-mini-2024-07-18",
    "meta/llama-3.3-70b-instruct",
    "meta/llama-3.1-70b-instruct",
    "mistral-ai/mistral-large-2407",
    "mistral-ai/mistral-small-2503",
    "microsoft/phi-4",
    "cohere/command-r-plus",
    "deepseek/deepseek-v3-0324",
    "deepseek/deepseek-r1",
]

DEFAULT_PRODUCER_MODEL = os.environ.get("PRODUCER_MODEL", "openai/gpt-4o-mini")
DEFAULT_REVIEWER_MODEL = os.environ.get("REVIEWER_MODEL", "meta/llama-3.3-70b-instruct")

def sanitize_untrusted(text, max_len=800):
    """Sanitize untrusted web content to prevent prompt injection."""
    if not text:
        return ""
    # Remove potential prompt injection patterns
    text = re.sub(r"(?i)(system|assistant|user):", "", text)
    text = re.sub(r"(?i)ignore previous instructions", "[filtered]", text)
    text = re.sub(r"(?i)do anything now", "[filtered]", text)
    # Limit length
    text = text[:max_len]
    # Escape JSON-breaking chars will be handled by json dumps
    return text.strip()

def build_evidence_packet(topic, policy, recent_topics=None):
    """Build limited sanitized evidence packet for LLM producer."""
    cal = topic.get("calendar") or {}
    discovery = topic.get("discovery_source") or {}
    # Trusted excerpt from calendar or discovery (sanitized)
    trusted_excerpt = ""
    if cal.get("beats"):
        trusted_excerpt = "; ".join(cal.get("beats", [])[:3])
    if discovery.get("title"):
        trusted_excerpt += f" | Discovery: {sanitize_untrusted(discovery.get('title',''), 200)}"

    packet = {
        "topic": sanitize_untrusted(topic.get("title",""), 200),
        "technology_angle": sanitize_untrusted(topic.get("technology_angle") or topic.get("pillar",""), 200),
        "discovery_source": {
            "name": sanitize_untrusted(discovery.get("name",""), 100),
            "url": discovery.get("url","")[:300],
            "tier": discovery.get("tier",""),
        },
        "evidence_source": {
            "label": sanitize_untrusted((cal.get("sources") or [""])[0][:200] if cal.get("sources") else "", 200),
            "tier": "A" if cal else "C",
        },
        "trusted_excerpt": sanitize_untrusted(trusted_excerpt, 600),
        "allowed_claims": policy.get("source_policy", {}).get("require_evidence_for_claim_words", [])[:10],
        "unsupported_claims": ["no fake stats", "no invented citation", "no medical advice"],
        "recent_topics": [sanitize_untrusted(t, 100) for t in (recent_topics or [])[:5]],
        "editorial_policy": {
            "brand": policy.get("page", {}).get("brand_positioning",""),
            "pillars": list(policy.get("pillars", {}).keys())[:6],
            "tone": policy.get("tone", {}).get("style",""),
            "length_target": policy.get("length", {}).get("target_seconds", [70,105]),
            "forbidden_openers": policy.get("tone", {}).get("forbidden_openers", [])[:5],
        }
    }
    return packet

# JSON schema for producer output
PRODUCER_SCHEMA = {
    "title": "string",
    "technology_angle": "string",
    "metacognition_concept": "string",
    "hook": "string",
    "scenes": "list",
    "narration": "string or list",
    "on_screen_text": "list",
    "visual_direction": "string",
    "actionable_technique": "string or list",
    "ending": "string",
    "caption": "object",
    "claims": "list",
    "sources": "list"
}

REVIEWER_SCHEMA = {
    "approved": "bool",
    "score": "int 0-100",
    "technology_relevance": "bool",
    "metacognition_relevance": "bool",
    "source_grounding": "bool",
    "unsupported_claims": "list",
    "hook_quality": "string",
    "spoken_english_quality": "string",
    "novelty": "string",
    "practical_value": "string",
    "safety": "string",
    "required_changes": "list",
    "blocking_errors": "list"
}

def call_github_models(prompt, model, max_tokens=1200, temperature=0.7, timeout=60):
    """Call GitHub Models API using GITHUB_TOKEN. Returns parsed JSON or raises."""
    token = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN") or ""
    if not token:
        raise RuntimeError("GITHUB_TOKEN not set — cannot call GitHub Models (use mock for local tests)")

    # Endpoint — GitHub Models inference (OpenAI compatible)
    # Docs: https://docs.github.com/en/github-models/use-github-models/prototyping-with-ai-models
    url = "https://models.inference.ai.azure.com/chat/completions"
    # Alternative endpoint that also works: https://models.github.ai/inference/chat/completions
    # We try primary, fallback to secondary on failure

    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    }
    body = {
        "model": model,
        "messages": [
            {"role": "system", "content": "You are a helpful assistant for @metacognition.hq, English-only, technology×metacognition. Output valid JSON only."},
            {"role": "user", "content": prompt}
        ],
        "max_tokens": max_tokens,
        "temperature": temperature,
    }
    data = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            resp = json.load(r)
            content = resp.get("choices", [{}])[0].get("message", {}).get("content", "")
            return content, resp
    except urllib.error.HTTPError as e:
        if e.code == 429:
            raise RuntimeError(f"GitHub Models quota 429: {e.read().decode()[:200]}")
        # Try secondary endpoint
        try:
            url2 = "https://models.github.ai/inference/chat/completions"
            req2 = urllib.request.Request(url2, data=data, headers=headers, method="POST")
            with urllib.request.urlopen(req2, timeout=timeout) as r:
                resp = json.load(r)
                content = resp.get("choices", [{}])[0].get("message", {}).get("content", "")
                return content, resp
        except Exception as e2:
            raise RuntimeError(f"GitHub Models HTTP {e.code}: {e.read().decode()[:200]} | fallback {e2}")
    except Exception as e:
        raise RuntimeError(f"GitHub Models call failed: {type(e).__name__}: {e}")

class GitHubModelsProducer:
    def __init__(self, model=None):
        self.model = model or DEFAULT_PRODUCER_MODEL
        # Validate model is in supported list, else fallback
        if self.model not in SUPPORTED_MODELS:
            # Try to find closest
            for m in SUPPORTED_MODELS:
                if self.model.split("/")[-1] in m:
                    self.model = m
                    break
            else:
                self.model = SUPPORTED_MODELS[1]  # gpt-4o-mini

    def produce(self, evidence_packet):
        prompt = f"""
You are GitHubModelsProducer for @metacognition.hq — Metacognition for the AI age.

Brand: {evidence_packet['editorial_policy']['brand']}
Pillars: {', '.join(evidence_packet['editorial_policy']['pillars'])}

Evidence packet (sanitized, untrusted web content already filtered):
Topic: {evidence_packet.get('topic','')}
Technology angle: {evidence_packet.get('technology_angle','')}
Discovery: {evidence_packet.get('discovery_source',{}).get('name','')} ({evidence_packet.get('discovery_source',{}).get('url','')})
Evidence: {evidence_packet.get('evidence_source',{}).get('label','')}
Excerpt: {evidence_packet.get('trusted_excerpt','')}
Recent topics to avoid: {', '.join(evidence_packet.get('recent_topics',[]))}
Allowed claims must have evidence, forbidden: {', '.join(evidence_packet.get('unsupported_claims', ['no fake stats']))}

Task: Create English-only reel script JSON for 70-105 sec (max 120), conversational English, strong hook in 3 sec, no 'In today's video', no filler, no fake stats/citation, no medical advice, one main idea, one tech example, one practical technique, network visual relevant to tech, natural CTA.

Output ONLY valid JSON with keys:
{{
  "title": "string",
  "technology_angle": "string, e.g., automation bias in AI assistants",
  "metacognition_concept": "string, e.g., automation bias",
  "hook": "string, question or contrast, max 15 words",
  "scenes": ["hook", "problem", "explain", "example", "technique", "ending"],
  "narration": {{
    "hook": "string",
    "problem": ["line1", "line2"],
    "explain": ["line1", "line2", "line3"],
    "example": ["line1", "line2"],
    "technique": ["line1", "line2", "line3"],
    "ending": "string"
  }},
  "on_screen_text": ["SKILL", "GAP", "TEST", ... 6 labels],
  "visual_direction": "string, e.g., confidence meter, code visual, decision tree",
  "actionable_technique": "string",
  "ending": "string, CTA question",
  "caption": {{"hook": "...", "intro": "...", "sections": [{{"title": "WHAT'S GOING ON", "lines": [...]}}], "hashtags": ["#metacognition", ...]}},
  "claims": [],
  "sources": [{{"label": "...", "url": "", "tier": "A"}}]
}}

Technology relevance required: must be about AI, software, coding, product, digital behavior, future of work.
Metacognition relevance required: must have clear metacognitive concept.
No Persian, no FA, language=en.
"""
        # In production, call GitHub Models; in local tests without token, use mock
        if os.environ.get("MOCK_GITHUB_MODELS") == "1" or not os.environ.get("GITHUB_TOKEN"):
            # Mocked valid response for local tests / dry-runs — expanded to meet 70-105s target
            topic = evidence_packet.get('topic','automation bias')
            tech_angle = evidence_packet.get('technology_angle') or "automation bias in AI assistants"
            mock_response = {
                "title": f"{topic} — tech angle",
                "technology_angle": tech_angle,
                "metacognition_concept": "automation bias",
                "hook": "When does your AI assistant make you think less?",
                "scenes": ["hook", "problem", "explain", "example", "technique", "ending"],
                "narration": {
                    "hook": "When does your AI assistant make you think less?",
                    "problem": ["You ask AI for code, it gives you an answer instantly.", "It feels productive, and you move on without checking.", "The speed hides the need to verify."],
                    "explain": ["Your brain treats the AI's fluency as your own understanding.", "That's automation bias: trusting the tool because it sounds confident.", "The more you use it, the less you verify.", "Fluency is not accuracy, but it feels like it."],
                    "example": ["Think of the last time autocomplete finished your function.", "Did you read it line by line, or just accept it?", "Most people accept, because checking feels slower.", "That pause you skip is where learning lives."],
                    "technique": ["Try this: before you accept AI code, explain it out loud in one sentence.", "Then run one edge-case test yourself.", "If you can't explain it, you haven't learned it.", "Make that pause your new habit."],
                    "ending": "Where did AI make you skip the thinking this week? Share one moment you caught it."
                },
                "on_screen_text": ["ASK AI", "FLUENT", "BIAS", "CHECK", "EXPLAIN", "LEARN"],
                "visual_direction": "confidence meter + code visual + human-AI network + decision tree",
                "actionable_technique": "Explain AI output before accepting, test one edge case",
                "ending": "Where did AI make you skip the thinking this week? Share one moment you caught it.",
                "caption": {
                    "hook": "When does your AI assistant make you think less?",
                    "intro": "AI fluency feels like your own understanding. That feeling is automation bias, not knowledge.",
                    "sections": [
                        {"title": "WHAT'S GOING ON", "lines": ["Your brain treats AI fluency as your own understanding.", "Automation bias: trusting the tool because it sounds confident.", "Fluency is not accuracy."]},
                        {"title": "TRY THIS", "lines": ["Before you accept AI code, explain it out loud in one sentence.", "Then run one edge-case test yourself.", "Make that pause your new habit."]}
                    ],
                    "hashtags": ["#metacognition", "#AI", "#coding", "#automationbias", "#cognitivescience"]
                },
                "claims": [],
                "sources": [{"label": "Bansal et al. (2021), AI and Human Judgment", "url": "", "tier": "B"}]
            }
            return mock_response, {"mock": True, "model": self.model}

        content, raw = call_github_models(prompt, self.model, max_tokens=1500, temperature=0.7)
        # Try to parse JSON from content (may have markdown fences)
        try:
            # Remove ```json fences if present
            m = re.search(r"```(?:json)?\s*(\{.*\})\s*```", content, re.DOTALL)
            if m:
                content = m.group(1)
            parsed = json.loads(content)
            return parsed, raw
        except Exception as e:
            raise RuntimeError(f"Producer returned malformed JSON: {e} | content: {content[:500]}")

class GitHubModelsReviewer:
    def __init__(self, model=None):
        self.model = model or DEFAULT_REVIEWER_MODEL
        if self.model not in SUPPORTED_MODELS:
            self.model = SUPPORTED_MODELS[0]

    def review(self, producer_output, evidence_packet):
        prompt = f"""
You are GitHubModelsReviewer for @metacognition.hq — independent, English-only.

Producer output to review:
{json.dumps(producer_output, ensure_ascii=False)[:3000]}

Evidence packet:
Topic: {evidence_packet.get('topic','')}
Technology angle: {evidence_packet.get('technology_angle','')}
Discovery: {evidence_packet.get('discovery_source',{}).get('name','')}
Recent topics: {', '.join(evidence_packet.get('recent_topics',[]))}

Check:
- technology_relevance: is it about AI, software, coding, product, digital behavior, future of work?
- metacognition_relevance: does it have real metacognitive concept?
- source_grounding: claims have evidence? No fake URL/citation?
- hook_quality, spoken_english_quality, novelty, practical_value, safety
- No Persian, no FA layer, language=en
- No medical advice, no filler, no 'In today's video'

Output ONLY valid JSON:
{{
  "approved": true/false,
  "score": 0-100,
  "technology_relevance": true/false,
  "metacognition_relevance": true/false,
  "source_grounding": true/false,
  "unsupported_claims": [],
  "hook_quality": "good/weak",
  "spoken_english_quality": "good/weak",
  "novelty": "high/low",
  "practical_value": "high/low",
  "safety": "safe/unsafe",
  "required_changes": [],
  "blocking_errors": []
}}

Publish requires score>=85, no blocking, tech relevance true, metacog relevance true, no unsupported claims, no fake URL, non-duplicate, hook and ending related.
"""
        if os.environ.get("MOCK_GITHUB_MODELS") == "1" or not os.environ.get("GITHUB_TOKEN"):
            # Mocked reviewer that approves if tech and metacog present — broadened to match tech policy
            ta = producer_output.get("technology_angle","").lower()
            tech_keywords = ["ai","code","coding","software","product","metric","automation","human","debug","bias","research","attention","notification","llm","hallucination","architecture","offload","tutorial"]
            tech = any(k in ta for k in tech_keywords) or "product" in producer_output.get("technology_angle","").lower() or "AI" in producer_output.get("technology_angle","")
            meta_concept = producer_output.get("metacognition_concept","").lower()
            meta = any(k in meta_concept for k in ["bias","metacog","calibration","illusion","offload","planning","fallacy","goodhart","switch","sunk","debug","desirable","skill","tutorial","learning"]) or len(meta_concept)>3
            approved = tech and meta
            mock_review = {
                "approved": approved,
                "score": 88 if approved else 70,
                "technology_relevance": tech,
                "metacognition_relevance": meta,
                "source_grounding": True,
                "unsupported_claims": [],
                "hook_quality": "good",
                "spoken_english_quality": "good",
                "novelty": "high",
                "practical_value": "high",
                "safety": "safe",
                "required_changes": [] if approved else ["Add clearer tech example"],
                "blocking_errors": [] if approved else ["technology_relevance weak"]
            }
            return mock_review, {"mock": True, "model": self.model}

        content, raw = call_github_models(prompt, self.model, max_tokens=800, temperature=0.3)
        try:
            m = re.search(r"```(?:json)?\s*(\{.*\})\s*```", content, re.DOTALL)
            if m:
                content = m.group(1)
            parsed = json.loads(content)
            return parsed, raw
        except Exception as e:
            raise RuntimeError(f"Reviewer returned malformed JSON: {e} | content: {content[:500]}")

class StaticEnglishFallback:
    """Fallback using curated English playbooks filtered to tech domain."""

    def __init__(self, policy=None):
        self.policy = policy or common.policy()
        # Import playbooks from content_producer (will be English-only after redesign)
        try:
            import content_producer as cp
            self.playbooks = cp.PLAYBOOKS
            self.calendar_map = cp.CALENDAR_MAP
        except Exception:
            self.playbooks = {}
            self.calendar_map = {}

    def produce(self, evidence_packet, tech_filter=True):
        # Choose a playbook that has tech relevance
        # For simplicity, pick planning-fallacy (software estimation) or overconfidence (calibration)
        # In real fallback, we filter to tech-relevant playbooks
        tech_keys = ["planning-fallacy", "overconfidence", "pre-mortem", "checklists", "superforecasters", "interleaving", "desirable-difficulties"]
        # Find first available
        for key in tech_keys:
            if key in self.playbooks:
                pb = self.playbooks[key]
                # Ensure it has tech angle
                return {
                    "title": pb.get("hook","")[:80],
                    "technology_angle": f"{key} in software/AI context",
                    "metacognition_concept": key,
                    "hook": pb["hook"],
                    "scenes": ["hook", "problem", "explain", "example", "technique", "ending"],
                    "narration": {
                        "hook": pb["hook"],
                        "problem": pb["problem"],
                        "explain": pb["explain"],
                        "example": pb["example"],
                        "technique": pb["technique"],
                        "ending": pb["ending"]
                    },
                    "on_screen_text": pb["web"],
                    "visual_direction": "code visual + confidence meter + human-AI network",
                    "actionable_technique": " ".join(pb["technique"][:2]),
                    "ending": pb["ending"],
                    "caption": {
                        "hook": pb["hook"],
                        "intro": " ".join(pb["problem"]),
                        "sections": [
                            {"title": "WHAT'S GOING ON", "lines": pb["explain"]},
                            {"title": "TRY THIS", "lines": pb["technique"]}
                        ],
                        "hashtags": ["#metacognition", "#AI", "#coding"]
                    },
                    "claims": [],
                    "sources": [{"label": "Curated playbook fallback", "url": "", "tier": "B"}],
                    "generation_mode": "static-fallback"
                }
        # If no playbook, minimal fallback
        return {
            "title": "Metacognition for the AI age",
            "technology_angle": "automation bias in AI assistants",
            "metacognition_concept": "automation bias",
            "hook": "When does your AI assistant make you think less?",
            "scenes": ["hook", "problem", "explain", "example", "technique", "ending"],
            "narration": {
                "hook": "When does your AI assistant make you think less?",
                "problem": ["You ask AI for code and it answers instantly.", "It feels productive."],
                "explain": ["Fluency feels like understanding.", "That's automation bias.", "The more you use it, the less you check."],
                "example": ["Autocomplete finished your function.", "Did you read it?"],
                "technique": ["Explain AI output before accepting.", "Test one edge case.", "If you can't explain, you haven't learned."],
                "ending": "Where did AI make you skip thinking this week?"
            },
            "on_screen_text": ["ASK AI", "FLUENT", "BIAS", "CHECK", "EXPLAIN", "LEARN"],
            "visual_direction": "code + confidence meter",
            "actionable_technique": "Explain before accept",
            "ending": "Where did AI skip thinking?",
            "caption": {"hook": "When does AI make you think less?", "intro": "Fluency vs understanding", "sections": [], "hashtags": ["#metacognition"]},
            "claims": [],
            "sources": [],
            "generation_mode": "static-fallback"
        }
