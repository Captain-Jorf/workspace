"""Daily reel factory — orchestrator with controlled retries.

  python3 build/pipeline.py produce --tag 2026-09-16 [--fixture fixtures/trends_sample.json]
         [--calendar-only] [--synthetic-tts] [--fixture-translation] [--skip-network]
      → topic → script (+FA) → TTS → timing → render → posters → caption → QA (pre-publish)
        with the mandated retries: script/translation rejected → regenerate ONCE;
        render rejected → ONE safer re-render; second failure → qa-failed.
      Writes output/auto-<tag>_state.json (consumed by report_issue.py) and exits 0 when
      the day has an APPROVED candidate, 10 otherwise (the workflow still files an issue).

  python3 build/pipeline.py verify --tag T --public-url URL [--skip-network]
      → re-runs the supervisor with the public URL (MIME/size) → final verdict.

  python3 build/pipeline.py record --tag T --status queued-in-buffer|approved-dry-run|qa-failed|...
      → manifest + editorial memory (no secrets), so reruns and the scout stay idempotent.

Every stage failure maps to a label: trend-error, source-error, script-error,
translation-error, tts-error, render-error, qa-failed, automation-error.
"""
import argparse
import json
import os
import re
import subprocess
import sys
import traceback

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import common  # noqa: E402

PY = sys.executable
B = os.path.dirname(os.path.abspath(__file__))
STAGE_LABEL = {"trend": "trend-error", "source": "source-error", "script": "script-error",
               "translate": "translation-error", "tts": "tts-error", "timing": "tts-error",
               "render": "render-error", "poster": "render-error", "caption": "script-error",
               "qa": "qa-failed"}


class Stage(Exception):
    def __init__(self, stage, msg):
        super().__init__(msg)
        self.stage = stage


def state_path(tag):
    return os.path.join(common.ROOT, "output", f"auto-{tag}_state.json")


def load_state(tag):
    return common.load_json(state_path(tag), {}) or {}


def save_state(tag, st):
    st["updated_utc"] = common.utc_now()
    common.save_json(state_path(tag), st)


def run(cmd, stage, env=None, timeout=1800):
    e = dict(os.environ)
    e.update(env or {})
    print(f"[pipeline:{stage}] $ {' '.join(os.path.relpath(c, common.ROOT) if c.startswith('/') else c for c in cmd)}", flush=True)
    r = subprocess.run(cmd, env=e, cwd=common.ROOT, text=True, capture_output=True, timeout=timeout)
    tail = (r.stdout[-3000:] + "\n" + r.stderr[-3000:]).strip()
    print(tail, flush=True)
    if r.returncode != 0:
        # last meaningful line as the human-readable reason
        lines = [l for l in tail.splitlines() if l.strip()]
        raise Stage(stage, lines[-1] if lines else f"exit {r.returncode}")
    return r.stdout


def qa_cmd(tag, ep, paths, topic_path, public_url="", skip_network=False, no_frames=False):
    cmd = [PY, os.path.join(B, "qa_supervisor.py"), "--ep", ep, "--video", paths["mp4"],
           "--caption", paths["caption"], "--poster", paths["poster"], "--poster45", paths["poster_4x5"],
           "--topic", topic_path, "--out-json", paths["qa_json"], "--out-md", paths["qa_md"]]
    if public_url:
        cmd += ["--public-url", public_url]
    if skip_network:
        cmd += ["--skip-network"]
    if no_frames:
        cmd += ["--no-frames"]
    return cmd


def run_qa(cmd, stage="qa"):
    r = subprocess.run(cmd, cwd=common.ROOT, text=True, capture_output=True)
    print((r.stdout[-4000:] + r.stderr[-1500:]).strip(), flush=True)
    if r.returncode not in (0, 20):
        raise Stage("qa", f"supervisor could not evaluate (exit {r.returncode}) — failing closed")
    return r.returncode == 0


def script_summary(script):
    beats = {}
    for ch in script["chunks"]:
        beats.setdefault(ch["beat"], []).extend(l["t"] for l in ch["en"])
    parts = []
    for b in ("hook", "problem", "explain", "technique"):
        if beats.get(b):
            parts.append(" ".join(beats[b][:2]))
    return " ".join(parts)[:700]


# ----------------------------------------------------------------- produce
def produce(a):
    tag = a.tag
    ep = common.episode_dir(tag)
    paths = common.output_paths(tag)
    os.makedirs(ep, exist_ok=True)
    os.makedirs(os.path.dirname(paths["mp4"]), exist_ok=True)
    st = {"tag": tag, "content_id": common.content_id(tag), "status": "running", "stage": "start",
          "retries": {"script": 0, "render": 0}, "branch": f"drafts/{tag}",
          "run_id": os.environ.get("GITHUB_RUN_ID", "local"), "dry_run": a.dry_run}
    save_state(tag, st)
    topic_path = os.path.join(ep, "topic.json")
    try:
        # 1. trend scout (+ source validation + dedupe inside)
        st["stage"] = "trend"
        cmd = [PY, os.path.join(B, "trend_scout.py"), "--date", tag, "--out", topic_path]
        if a.fixture:
            cmd += ["--fixture", a.fixture]
        if a.calendar_only:
            cmd += ["--calendar-only"]
        run(cmd, "trend")
        topic = common.load_json(topic_path)
        if not topic:
            raise Stage("trend", "scout produced no topic")
        st["topic"] = {k: topic.get(k) for k in ("title", "pillar", "evidence_mode", "discovery_source",
                                                 "normalized_topic", "fallback_reason")}
        save_state(tag, st)

        approved = False
        variant = 0
        while True:
            # 2. script + translation
            st["stage"] = "script"
            env = {"TRANSLATE_FIXTURE": "1"} if a.fixture_translation else {}
            cmd = [PY, os.path.join(B, "content_producer.py"), "--topic", topic_path, "--out", ep,
                   "--variant", str(variant)]
            try:
                run(cmd, "script", env=env)
            except Stage as e:
                if "translation" in str(e).lower():
                    e.stage = "translate"
                raise
            script = common.load_json(os.path.join(ep, "script.json"))
            st["script"] = {"summary": script_summary(script), "sources": script.get("sources", []),
                            "playbook": script["meta"].get("playbook"), "cta_type": script["meta"].get("cta_type"),
                            "translation_engine": script["meta"].get("translation_engine"),
                            "hash": common.script_hash(script)}
            save_state(tag, st)

            # 3. tts + timing
            st["stage"] = "tts"
            for f in os.listdir(ep):
                if re.match(r"c\d\d\.(mp3|words\.json|meta\.jsonl)$", f):
                    os.remove(os.path.join(ep, f))
            tts = "tts_synthetic.py" if a.synthetic_tts else "tts_edge.py"
            run([PY, os.path.join(B, tts), ep], "tts", timeout=900)
            st["stage"] = "timing"
            run([PY, os.path.join(B, "timing.py")], "timing", env={"EP_DIR": ep})

            # 4. caption (needed by QA)
            st["stage"] = "caption"
            run([PY, os.path.join(B, "caption.py"), ep, paths["caption"]], "caption")

            # 5. render (+ one safer retry) + posters + QA
            safe = False
            while True:
                st["stage"] = "render"
                cmd = [PY, os.path.join(B, "render_auto.py"), "--ep", ep, "--out", paths["mp4"]]
                if safe:
                    cmd.append("--safe")
                run(cmd, "render", timeout=2400)
                st["stage"] = "poster"
                run([PY, os.path.join(B, "poster_auto.py"), "--ep", ep, "--out-dir", os.path.dirname(paths["mp4"])], "poster")
                st["stage"] = "qa"
                # local test runs may whitelist the fixture translator for the supervisor; a drill can force
                # the strict path with QA_STRICT_FIXTURE=1 to prove the factory refuses non-real Persian.
                if a.fixture_translation and os.environ.get("QA_STRICT_FIXTURE") != "1":
                    os.environ["QA_ALLOW_FIXTURE"] = "1"
                approved = run_qa(qa_cmd(tag, ep, paths, topic_path, skip_network=a.skip_network))
                qa = common.load_json(paths["qa_json"], {})
                st["qa"] = qa
                save_state(tag, st)
                if approved:
                    break
                blocking = " ".join(qa.get("blocking_errors", [])).lower()
                render_related = any(k in blocking for k in ("video_quality", "audio_quality", "subtitle_layout"))
                content_related = any(k in blocking for k in ("script_quality", "english_quality", "persian_quality",
                                                              "topic_relevance", "source_quality", "caption_quality",
                                                              "duplicate_check"))
                if render_related and not content_related and not safe and st["retries"]["render"] == 0:
                    print("[pipeline] render/audio/layout rejected → ONE safer re-render", flush=True)
                    st["retries"]["render"] = 1
                    safe = True
                    continue
                break
            if approved:
                break
            if variant == 0 and st["retries"]["script"] == 0:
                blocking = " ".join(qa.get("blocking_errors", [])).lower()
                if any(k in blocking for k in ("script_quality", "english_quality", "persian_quality", "caption_quality")):
                    print("[pipeline] script/translation rejected → regenerate ONCE (variant 1)", flush=True)
                    st["retries"]["script"] = 1
                    variant = 1
                    continue
            break

        if not approved:
            st["status"] = "qa-failed"
            st["stage"] = "qa"
            st["error"] = "; ".join((st.get("qa") or {}).get("blocking_errors", [])[:6]) or \
                f"score {(st.get('qa') or {}).get('score')} below minimum"
            save_state(tag, st)
            print("[pipeline] REJECTED — no post today", flush=True)
            return 10
        st["status"] = "approved-local"
        st["caption"] = open(paths["caption"], encoding="utf-8").read()
        save_state(tag, st)
        print("[pipeline] approved locally — awaiting public URL verification", flush=True)
        return 0
    except Stage as e:
        st["status"] = STAGE_LABEL.get(e.stage, "automation-error")
        st["stage"] = e.stage
        st["error"] = common.scrub_secrets(str(e))
        save_state(tag, st)
        print(f"[pipeline] FAILED at {e.stage}: {st['error']}", flush=True)
        return 10
    except Exception as e:                                    # noqa: BLE001
        st["status"] = "automation-error"
        st["error"] = common.scrub_secrets(f"{type(e).__name__}: {e}")
        save_state(tag, st)
        traceback.print_exc()
        return 10


# ----------------------------------------------------------------- verify
def verify(a):
    tag = a.tag
    ep = common.episode_dir(tag)
    paths = common.output_paths(tag)
    st = load_state(tag)
    topic_path = os.path.join(ep, "topic.json")
    st["public_url"] = a.public_url
    try:
        ok = run_qa(qa_cmd(tag, ep, paths, topic_path, public_url=a.public_url, skip_network=a.skip_network,
                           no_frames=True))
    except Stage as e:
        st.update(status="qa-failed", stage="qa", error=str(e))
        save_state(tag, st)
        return 10
    st["qa"] = common.load_json(paths["qa_json"], {})
    if not ok:
        st.update(status="qa-failed", stage="verify", error="; ".join(st["qa"].get("blocking_errors", [])[:6]))
        save_state(tag, st)
        return 10
    st["status"] = "approved"
    st["stage"] = "verified"
    save_state(tag, st)
    return 0


# ----------------------------------------------------------------- record
def record(a):
    tag = a.tag
    st = load_state(tag)
    paths = common.output_paths(tag)
    st["status"] = a.status
    marker = common.load_json(paths["marker"], {}) or {}
    if marker:
        st["buffer"] = {k: marker.get(k) for k in ("buffer_post_id", "status", "due_at", "channel_name",
                                                   "first_comment_used", "adopted")}
    if a.error:
        st["error"] = common.scrub_secrets(a.error)
    save_state(tag, st)
    qa = st.get("qa") or {}
    script = st.get("script") or {}
    topic = st.get("topic") or {}
    ep = common.episode_dir(tag)
    sc = common.load_json(os.path.join(ep, "script.json"), {}) or {}
    manifest = {
        "content_id": st.get("content_id") or common.content_id(tag),
        "content_date": tag,
        "topic": topic.get("title"),
        "normalized_topic": topic.get("normalized_topic") or common.normalize_title(topic.get("title") or ""),
        "pillar": topic.get("pillar"),
        "tags": (sc.get("meta") or {}).get("tags", []),
        "cta_type": script.get("cta_type"),
        "playbook": script.get("playbook"),
        "source_url": next((s.get("url") for s in script.get("sources", []) if s.get("url")), None),
        "sources": script.get("sources", []),
        "script_hash": script.get("hash"),
        "video_hash": qa.get("video_hash") or (common.sha256_file(paths["mp4"]) if os.path.exists(paths["mp4"]) else None),
        "qa_score": qa.get("score"),
        "qa_approved": qa.get("approved"),
        "buffer_post_id": (st.get("buffer") or {}).get("buffer_post_id"),
        "buffer_due_at": (st.get("buffer") or {}).get("due_at"),
        "status": a.status,
        "public_url": st.get("public_url"),
        "branch": st.get("branch"),
        "run_id": st.get("run_id"),
        "error": st.get("error"),
        "timestamp_utc": common.utc_now(),
    }
    common.save_json(paths["manifest"], manifest)
    mem = common.load_memory()
    entry = {k: manifest[k] for k in ("content_id", "content_date", "topic", "normalized_topic", "pillar", "tags",
                                      "cta_type", "playbook", "source_url", "script_hash", "video_hash", "qa_score",
                                      "buffer_post_id", "status", "run_id")}
    entry["reason"] = (st.get("error") or "")[:300] if a.status not in ("queued-in-buffer", "approved-dry-run") else ""
    entry["hook_type"] = "question" if "?" in ((sc.get("caption") or {}).get("hook") or "") else "statement"
    entry["updated_utc"] = common.utc_now()
    common.upsert_memory(mem, entry)
    common.save_memory(mem)
    print(f"[pipeline] recorded {a.status} for {tag} → manifest + editorial memory")
    return 0


def note(a):
    """Append a human-readable note to the state file (shown in the daily issue for any status)."""
    st = load_state(a.tag)
    st.setdefault("notes", []).append(common.scrub_secrets(a.text)[:500])
    save_state(a.tag, st)
    return 0


def main():
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("produce")
    p.add_argument("--tag", required=True)
    p.add_argument("--fixture")
    p.add_argument("--calendar-only", action="store_true")
    p.add_argument("--synthetic-tts", action="store_true", help="LOCAL TESTS ONLY")
    p.add_argument("--fixture-translation", action="store_true", help="LOCAL TESTS ONLY")
    p.add_argument("--skip-network", action="store_true")
    p.add_argument("--dry-run", action="store_true")
    p = sub.add_parser("verify")
    p.add_argument("--tag", required=True)
    p.add_argument("--public-url", required=True)
    p.add_argument("--skip-network", action="store_true")
    p = sub.add_parser("note")
    p.add_argument("--tag", required=True)
    p.add_argument("--text", required=True)
    p = sub.add_parser("record")
    p.add_argument("--tag", required=True)
    p.add_argument("--status", required=True)
    p.add_argument("--error", default="")
    a = ap.parse_args()
    if a.cmd == "produce":
        if (a.synthetic_tts or a.fixture_translation) and os.environ.get("GITHUB_ACTIONS") == "true" \
                and os.environ.get("ALLOW_TEST_MODES") != "1":
            raise SystemExit("test-only modes are not allowed in CI production runs")
        sys.exit(produce(a))
    if a.cmd == "verify":
        sys.exit(verify(a))
    if a.cmd == "record":
        sys.exit(record(a))
    if a.cmd == "note":
        sys.exit(note(a))


if __name__ == "__main__":
    main()
