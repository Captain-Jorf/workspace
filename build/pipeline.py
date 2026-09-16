"""Daily reel factory — English-only, Technology × Metacognition, Groq + Static Fallback.

  python3 build/pipeline.py produce --tag 2026-09-16 [--fixture fixtures/trends_sample.json]
         [--calendar-only] [--synthetic-tts] [--skip-network]
      → topic → script (LLM + fallback) → TTS → timing → render → posters → caption → QA (pre-publish)
        with mandated retries: script rejected → regenerate ONCE;
        render rejected → ONE safer re-render (temp file + validation + atomic replace); second failure → qa-failed.
      Writes output/auto-<tag>_state.json and exits 0 when APPROVED, 10 otherwise.

  python3 build/pipeline.py verify --tag T --public-url URL [--skip-network]
      → re-runs supervisor with public URL

  python3 build/pipeline.py record --tag T --status queued-in-buffer|approved-dry-run|qa-failed|...

English-only: CONTENT_LANGUAGE=en fail-closed, no translator calls, no Persian fixture, no FA layer.
Max daily: 1 Producer, 1 Reviewer, if rejected max 1 Revision + final Reviewer.
On quota 429/outage limited retry then static fallback, no cost, no pipeline stop if fallback valid.
"""
import argparse
import json
import os
import re
import subprocess
import sys
import time
import traceback

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import common

PY = sys.executable
B = os.path.dirname(os.path.abspath(__file__))
STAGE_LABEL = {"trend": "trend-error", "source": "source-error", "script": "script-error",
               "tts": "tts-error", "timing": "tts-error", "render": "render-error",
               "poster": "render-error", "caption": "script-error", "qa": "qa-failed"}

class Stage(Exception):
    def __init__(self, stage, msg):
        super().__init__(msg)
        self.stage = stage

def assert_no_mock_in_ci():
    """Mock LLM fixtures are forbidden in CI/production (cron or dispatch).

    Raises Stage('script', ...) when GITHUB_ACTIONS=true and MOCK_GROQ=1.
    Local runs (no GITHUB_ACTIONS) may use the explicit mock for tests.
    """
    if os.environ.get("GITHUB_ACTIONS") == "true" and os.environ.get("MOCK_GROQ") == "1":
        raise Stage("script", "MOCK_GROQ=1 is forbidden in CI/production — refusing mock content")

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
    try:
        r = subprocess.run(cmd, env=e, cwd=common.ROOT, text=True, capture_output=True, timeout=timeout)
    except subprocess.TimeoutExpired as ex:
        out = ex.stdout or ""
        if isinstance(out, bytes):
            out = out.decode("utf-8", "replace")
        print(common.scrub_secrets(out[-1500:]), flush=True)
        raise Stage(stage, f"timed out after {timeout} s")
    tail = (r.stdout[-3000:] + "\n" + r.stderr[-3000:]).strip()
    print(tail, flush=True)
    if r.returncode != 0:
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

def validate_mp4(path):
    """Validate MP4 for safe retry: video+audio, 1080x1920, 60-120s, decode few frames."""
    if not os.path.exists(path) or os.path.getsize(path) < 1024:
        return False, "missing or too small"
    try:
        import shutil, json, subprocess
        try:
            import imageio_ffmpeg
            ffmpeg = imageio_ffmpeg.get_ffmpeg_exe()
        except Exception:
            ffmpeg = shutil.which("ffmpeg") or "ffmpeg"
        ffprobe = shutil.which("ffprobe")
        info = None
        if ffprobe:
            r = subprocess.run([ffprobe, "-v", "error", "-print_format", "json", "-show_format", "-show_streams", path],
                               capture_output=True, text=True, timeout=20)
            if r.returncode == 0:
                info = json.loads(r.stdout)
        if not info:
            r = subprocess.run([ffmpeg, "-hide_banner", "-i", path], capture_output=True, text=True, timeout=20)
            txt = r.stderr
            if "Video:" not in txt:
                return False, "no video stream in probe"
            if "Audio:" not in txt:
                return False, "no audio stream in probe"
            # fallback minimal
            return True, "fallback probe ok"
        vs = [s for s in info.get("streams", []) if s.get("codec_type") == "video"]
        au = [s for s in info.get("streams", []) if s.get("codec_type") == "audio"]
        if not vs:
            return False, "no video stream"
        if not au:
            return False, "no audio stream"
        v = vs[0]
        w, h = int(v.get("width", 0)), int(v.get("height", 0))
        if (w, h) != (1080, 1920):
            return False, f"resolution {w}x{h} not 1080x1920"
        dur = float(info.get("format", {}).get("duration") or v.get("duration") or 0)
        if not (60 <= dur <= 120):
            return False, f"duration {dur:.1f}s outside 60-120"
        # decode few frames at start, middle, end
        for ss in ("0", f"{dur*0.5:.2f}", f"{max(0, dur-1):.2f}"):
            r = subprocess.run([ffmpeg, "-v", "error", "-xerror", "-ss", ss, "-i", path, "-vframes", "1", "-f", "null", "-"],
                               capture_output=True, text=True, timeout=20)
            if r.returncode != 0:
                return False, f"decode error at {ss}s: {r.stderr[-200:]}"
        return True, f"valid {w}x{h} {dur:.1f}s"
    except Exception as e:
        return False, f"validation exception {type(e).__name__}: {e}"

# produce
def produce(a):
    common.assert_content_language_en()
    assert_no_mock_in_ci()
    tag = a.tag
    ep = common.episode_dir(tag)
    paths = common.output_paths(tag)
    os.makedirs(ep, exist_ok=True)
    os.makedirs(os.path.dirname(paths["mp4"]), exist_ok=True)
    st = {"tag": tag, "content_id": common.content_id(tag), "status": "running", "stage": "start",
          "retries": {"script": 0, "render": 0}, "branch": f"drafts/{tag}",
          "run_id": os.environ.get("GITHUB_RUN_ID", "local"), "dry_run": a.dry_run,
          "content_language": "en", "generation_mode": "unknown"}
    save_state(tag, st)
    topic_path = os.path.join(ep, "topic.json")
    lock_path = os.path.join(os.path.dirname(paths["mp4"]), f"auto-{tag}.render.lock")
    tmp_retry_path = os.path.join(os.path.dirname(paths["mp4"]), f"auto-{tag}.safe.tmp.mp4")
    try:
        # Prevent two renders simultaneous for same tag
        if os.path.exists(lock_path):
            try:
                age = os.path.getmtime(lock_path)
                if time.time() - age < 600:
                    raise Stage("render", f"another render for {tag} is running (lock {lock_path}) — refusing concurrent render")
                else:
                    os.remove(lock_path)
            except FileNotFoundError:
                pass
        with open(lock_path, "w") as lf:
            lf.write(f"{os.getpid()} {common.utc_now()}\n")

        # 1. trend scout
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
        st["topic"] = {k: topic.get(k) for k in ("title", "pillar", "technology_angle", "evidence_mode", "discovery_source", "normalized_topic", "fallback_reason")}
        save_state(tag, st)

        approved = False
        variant = 0
        while True:
            # 2. script via GitHub Models + static fallback
            st["stage"] = "script"
            env = {}
            if os.environ.get("MOCK_GROQ"):
                env["MOCK_GROQ"] = "1"
            cmd = [PY, os.path.join(B, "content_producer.py"), "--topic", topic_path, "--out", ep, "--variant", str(variant)]
            try:
                run(cmd, "script", env=env, timeout=600)
            except Stage as e:
                if st["retries"]["script"] == 0:
                    print(f"[pipeline] script failed ({e}) → retry variant 1", flush=True)
                    st["retries"]["script"] = 1
                    variant = 1
                    continue
                raise
            script = common.load_json(os.path.join(ep, "script.json"))
            if not script:
                raise Stage("script", "script.json missing")
            if script.get("meta", {}).get("language") != "en":
                raise Stage("script", f"language {script.get('meta',{}).get('language')} != en")
            st["script"] = {"summary": script_summary(script), "sources": script.get("sources", []),
                            "playbook": script.get("meta", {}).get("playbook"),
                            "technology_angle": script.get("meta", {}).get("technology_angle"),
                            "metacognition_concept": script.get("meta", {}).get("metacognition_concept"),
                            "generation_mode": script.get("meta", {}).get("generation_mode"),
                            "language": script.get("meta", {}).get("language"),
                            "cta_type": script.get("meta", {}).get("cta_type"),
                            "hash": common.script_hash(script)}
            st["generation_mode"] = script.get("meta", {}).get("generation_mode", "unknown")
            save_state(tag, st)

            # 2b. Deterministic PRE-RENDER script gate — enforced again here,
            # BEFORE TTS, subtitle generation and video rendering, on whatever
            # script.json holds. Uses the QA single-sourced word count
            # (common.spoken_word_count: actual spoken words only, never a
            # model-reported count) and the duration preflight from the
            # configured narration rate. A known-short script must not spend
            # TTS/render resources: on failure the existing ONE allowed script
            # retry runs, then the run is skipped before media stages (no
            # padding with filler, repeated CTA or silence is ever attempted).
            gate_issues = common.pre_render_issues(script)
            if gate_issues:
                if st["retries"]["script"] == 0:
                    print("[pipeline] pre-render script gate: " + "; ".join(gate_issues)
                          + " → retry producer ONCE (variant 1) before any TTS/render", flush=True)
                    st["retries"]["script"] = 1
                    st["gate"] = {"stage": "script", "issues": gate_issues,
                                  "words": common.spoken_word_count(script)}
                    variant = 1
                    continue
                raise Stage("script", "pre-render script gate: " + "; ".join(gate_issues)
                            + " — skipped before TTS/render (no media, no Buffer)")
            st["gate"] = {"stage": "script", "ok": True,
                          "words": common.spoken_word_count(script),
                          "estimated_seconds": common.estimate_spoken_seconds(script)}
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

            # 4. caption
            st["stage"] = "caption"
            run([PY, os.path.join(B, "caption.py"), ep, paths["caption"]], "caption")

            # 5. render + posters + QA with one safer retry using temp file + validation
            safe = False
            original_mp4_valid = False
            while True:
                st["stage"] = "render"
                if not safe:
                    render_out = paths["mp4"]
                    cmd = [PY, os.path.join(B, "render_auto.py"), "--ep", ep, "--out", render_out]
                else:
                    render_out = tmp_retry_path
                    cmd = [PY, os.path.join(B, "render_auto.py"), "--ep", ep, "--out", render_out, "--safe"]
                try:
                    run(cmd, "render", timeout=2400)
                except Stage as e_render:
                    if safe:
                        # Clean temp, keep original
                        try:
                            if os.path.exists(render_out):
                                os.remove(render_out)
                        except Exception:
                            pass
                        raise Stage("render", f"safe re-render failed/timed out: {e_render} — keeping original valid file, fail-closed, no Buffer createPost")
                    else:
                        raise
                if not safe:
                    valid, why = validate_mp4(paths["mp4"])
                    original_mp4_valid = valid
                    print(f"[pipeline] first render {'valid' if valid else 'invalid'}: {why}", flush=True)
                else:
                    # Validate temp before atomic replace
                    valid, why = validate_mp4(render_out)
                    if not valid:
                        print(f"[pipeline] safe retry produced invalid file: {why} — keeping original {paths['mp4']}", flush=True)
                        try:
                            if os.path.exists(render_out):
                                os.remove(render_out)
                        except Exception:
                            pass
                        st["stage"] = "render"
                        st["error"] = f"safe re-render invalid: {why} — original preserved, no publish"
                        save_state(tag, st)
                        raise Stage("render", f"safe re-render invalid ({why}) — original preserved, fail-closed")
                    try:
                        os.replace(render_out, paths["mp4"])
                        print(f"[pipeline] safe retry valid ({why}) → atomic replace {paths['mp4']}", flush=True)
                    except Exception as e_replace:
                        raise Stage("render", f"atomic replace failed: {e_replace}")
                    finally:
                        try:
                            if os.path.exists(render_out):
                                os.remove(render_out)
                        except Exception:
                            pass

                st["stage"] = "poster"
                run([PY, os.path.join(B, "poster_auto.py"), "--ep", ep, "--out-dir", os.path.dirname(paths["mp4"])], "poster")
                st["stage"] = "qa"
                approved = run_qa(qa_cmd(tag, ep, paths, topic_path, skip_network=a.skip_network))
                qa = common.load_json(paths["qa_json"], {})
                # Test hook: force render reject for integration test of safe retry path
                if os.environ.get("FORCE_RENDER_REJECT") == "1" and not safe and st["retries"]["render"] == 0:
                    print("[pipeline] FORCE_RENDER_REJECT=1 → forcing render-related QA rejection to test safe retry path", flush=True)
                    approved = False
                    qa["blocking_errors"] = qa.get("blocking_errors", []) + ["[video_quality] forced render reject for safe retry integration test"]
                    qa["approved"] = False
                    common.save_json(paths["qa_json"], qa)
                st["qa"] = qa
                save_state(tag, st)
                if approved:
                    break
                blocking = " ".join(qa.get("blocking_errors", [])).lower()
                render_related = any(k in blocking for k in ("video_quality", "audio_quality", "subtitle_layout"))
                content_related = any(k in blocking for k in ("script_quality", "english_quality", "technology_relevance", "metacognition_relevance", "topic_relevance", "source_quality", "caption_quality", "duplicate_check", "reviewer_check", "english_only", "content_language"))
                if render_related and not content_related and not safe and st["retries"]["render"] == 0:
                    print("[pipeline] render/audio/layout rejected → ONE safer re-render (temp file + validation)", flush=True)
                    st["retries"]["render"] = 1
                    safe = True
                    continue
                break
            if approved:
                break
            if variant == 0 and st["retries"]["script"] == 0:
                blocking = " ".join(qa.get("blocking_errors", [])).lower()
                if any(k in blocking for k in ("script_quality", "english_quality", "technology_relevance", "metacognition_relevance", "caption_quality", "reviewer_check")):
                    print("[pipeline] script rejected → regenerate ONCE (variant 1)", flush=True)
                    st["retries"]["script"] = 1
                    variant = 1
                    continue
            break

        if not approved:
            st["status"] = "qa-failed"
            st["stage"] = "qa"
            st["error"] = "; ".join((st.get("qa") or {}).get("blocking_errors", [])[:6]) or f"score {(st.get('qa') or {}).get('score')} below minimum"
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
    except Exception as e:
        st["status"] = "automation-error"
        st["error"] = common.scrub_secrets(f"{type(e).__name__}: {e}")
        save_state(tag, st)
        traceback.print_exc()
        return 10
    finally:
        # Clean lock and temp file
        try:
            if os.path.exists(lock_path):
                os.remove(lock_path)
        except Exception:
            pass
        try:
            if os.path.exists(tmp_retry_path):
                os.remove(tmp_retry_path)
        except Exception:
            pass

def verify(a):
    common.assert_content_language_en()
    tag = a.tag
    ep = common.episode_dir(tag)
    paths = common.output_paths(tag)
    st = load_state(tag)
    topic_path = os.path.join(ep, "topic.json")
    st["public_url"] = a.public_url
    try:
        ok = run_qa(qa_cmd(tag, ep, paths, topic_path, public_url=a.public_url, skip_network=a.skip_network, no_frames=True))
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

# Failure states written by produce()/verify() — the authoritative outcome when
# the pipeline itself stopped. A QA rejection ("qa-failed") or a stage error must
# stay a QA/failure state through manifest, memory, issue and the final workflow
# annotation: it can never be relabeled as a Buffer outcome merely because no
# Buffer post was attempted. buffer-error stays reserved for ACTUAL Buffer
# read-only/mutation failures (which always run after a verified public URL).
FAILURE_STATES = {"qa-failed", "trend-error", "source-error", "script-error",
                  "translation-error", "tts-error", "render-error", "automation-error"}
BUFFER_OUTCOME_STATES = {"buffer-error", "queue-full", "queued-in-buffer",
                         "approved-dry-run", "auto-published", "scheduled", "queued", "sent"}

def record(a):
    tag = a.tag
    st = load_state(tag)
    paths = common.output_paths(tag)
    marker = common.load_json(paths["marker"], {}) or {}
    if (not marker) and st.get("status") in FAILURE_STATES and a.status in BUFFER_OUTCOME_STATES:
        print(f"[pipeline] record: keeping failure state '{st['status']}' for {tag} — refusing to "
              f"relabel as '{a.status}' (no Buffer post was attempted; buffer-* is reserved for "
              f"actual Buffer failures)", flush=True)
        a.status = st["status"]
    st["status"] = a.status
    if marker:
        st["buffer"] = {k: marker.get(k) for k in ("buffer_post_id", "status", "due_at", "channel_name", "first_comment_used", "adopted")}
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
        "technology_angle": script.get("technology_angle") or (sc.get("meta") or {}).get("technology_angle"),
        "metacognition_concept": script.get("metacognition_concept") or (sc.get("meta") or {}).get("metacognition_concept"),
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
        "language": "en",
        "content_language": "en",
        "generation_mode": st.get("generation_mode") or (sc.get("meta") or {}).get("generation_mode"),
    }
    common.save_json(paths["manifest"], manifest)
    mem = common.load_memory()
    entry = {k: manifest[k] for k in ("content_id", "content_date", "topic", "normalized_topic", "pillar", "tags", "cta_type", "playbook", "source_url", "script_hash", "video_hash", "qa_score", "buffer_post_id", "status", "run_id")}
    entry["technology_angle"] = manifest.get("technology_angle")
    entry["metacognition_concept"] = manifest.get("metacognition_concept")
    entry["reason"] = (st.get("error") or "")[:300] if a.status not in ("queued-in-buffer", "approved-dry-run") else ""
    entry["hook_type"] = "question" if "?" in ((sc.get("caption") or {}).get("hook") or "") else "statement"
    entry["updated_utc"] = common.utc_now()
    common.upsert_memory(mem, entry)
    common.save_memory(mem)
    print(f"[pipeline] recorded {a.status} for {tag} → manifest + editorial memory lang=en")
    return 0

def note(a):
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
        if a.synthetic_tts and os.environ.get("GITHUB_ACTIONS") == "true" and os.environ.get("ALLOW_TEST_MODES") != "1":
            raise SystemExit("test-only modes not allowed in CI")
        sys.exit(produce(a))
    if a.cmd == "verify":
        sys.exit(verify(a))
    if a.cmd == "record":
        sys.exit(record(a))
    if a.cmd == "note":
        sys.exit(note(a))

if __name__ == "__main__":
    main()
