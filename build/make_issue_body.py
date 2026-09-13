"""Build the markdown body of the daily draft review issue.

usage:
  python3 build/make_issue_body.py --tag 2026-09-14 --repo owner/name \
      --raw-url https://raw.githubusercontent.com/owner/name/drafts/2026-09-14/output/auto-2026-09-14.mp4 \
      [--video-ok yes|no] > /tmp/body.md

Reads only local files (script.json, trends.json, caption txt). No network,
no secrets. Keeps the workflow YAML simple and the body reproducible.
"""
import argparse
import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def load(path, default=None):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception:                                        # noqa: BLE001
        return default


def build(tag, repo, raw_url, video_ok="yes"):
    ep = f"{ROOT}/content/episodes/auto-{tag}"
    sc = load(f"{ep}/script.json", {}) or {}
    meta, cap = sc.get("meta", {}), sc.get("caption", {})
    topic = meta.get("title", "").split("—", 1)[-1].strip() or "(unknown topic)"
    note = meta.get("note", "")

    trends = (load(f"{ROOT}/content/trends.json", {}) or {}).get("trends") or []
    if trends:
        t0 = trends[0]
        radar = f"{t0.get('source', '?')} · score {t0.get('score', '?')} · pillar {t0.get('pillar', '?')}"
    else:
        radar = "content calendar fallback"

    sources = cap.get("sources", [])[:3]
    hashtags = " ".join(cap.get("hashtags", []))

    caption_file = f"{ROOT}/output/auto-{tag}_caption.txt"
    try:
        caption = open(caption_file, encoding="utf-8").read().strip()
    except Exception:                                        # noqa: BLE001
        caption = "(caption missing)"
    if len(caption) > 1600:
        caption = caption[:1600] + " …"

    # preview stills: whatever preview_stills.py produced (names depend on duration)
    import glob
    import re
    prevs = sorted(glob.glob(f"{ROOT}/output/drafts/preview_*.jpg"),
                   key=lambda p: int(re.search(r"preview_(\d+)", p).group(1)))
    prev_links = " · ".join(
        f"[t={os.path.basename(p).split('_')[1].split('.')[0]} s]"
        f"(https://github.com/{repo}/blob/drafts/{tag}/output/drafts/{os.path.basename(p)})"
        for p in prevs) or "(no previews)"

    blob = f"https://github.com/{repo}/blob/drafts/{tag}"
    warn = "" if video_ok == "yes" else (
        "\n> ⚠️ The public MP4 URL was not reachable when this issue was created. "
        "Re-check the link before commenting `/publish`.\n")

    L = []
    L.append(f"## Daily trend reel draft — {tag}")
    L.append("")
    L.append(f"**Topic:** {topic}")
    L.append(f"**Radar source:** {radar}")
    if note:
        L.append(f"**Note:** {note}")
    if sources:
        L.append("**Caption sources:** " + "; ".join(sources))
    if hashtags:
        L.append(f"**Hashtags (Buffer first comment):** {hashtags}")
    if warn:
        L.append(warn)
    L.append("")
    L.append("### Review")
    L.append(f"- 🎬 **MP4 (public URL Buffer will fetch):** {raw_url}")
    L.append(f"- 🖥 Watch in browser: {blob}/output/auto-{tag}.mp4")
    L.append(f"- 📝 Caption: {blob}/output/auto-{tag}_caption.txt")
    L.append(f"- 🖼 Previews: {prev_links}")
    L.append(f"- 🧾 Script: {blob}/content/episodes/auto-{tag}/script.json")
    L.append("")
    L.append("### Caption preview")
    L.append("```")
    L.append(caption)
    L.append("```")
    L.append("")
    L.append("### How to publish")
    L.append("Comment **exactly** `/publish` on this issue (repo owner / collaborators only).")
    L.append("")
    L.append("The reel is then added **once** to the official **Buffer** queue of "
             "`metacognition.hq` (GraphQL `createPost`, `mode: addToQueue`), and Buffer "
             "publishes it at the next **19:30 Asia/Tehran** slot from the channel schedule.")
    L.append("")
    L.append("**Nothing is sent to Buffer or Instagram before that comment.** "
             "Anonymous `/publish` comments are ignored.")
    return "\n".join(L) + "\n"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--tag", required=True)
    ap.add_argument("--repo", required=True)
    ap.add_argument("--raw-url", required=True)
    ap.add_argument("--video-ok", default="yes")
    a = ap.parse_args()
    sys.stdout.write(build(a.tag, a.repo, a.raw_url, a.video_ok))


if __name__ == "__main__":
    main()
