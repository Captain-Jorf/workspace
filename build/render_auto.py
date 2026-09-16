"""Render an auto episode: MP4 (9:16, H.264/AAC), previews, QA contact sheet,
and layout.json (subtitle boxes for the independent QA supervisor).

usage:
  python3 build/render_auto.py --ep content/episodes/auto-2026-09-16 --out output/auto-2026-09-16.mp4
      [--safe]           # controlled-retry mode: no flashes/punch, CRF 23, preset slow-ish
      [--stills-only]    # previews + contact sheet + layout.json, no MP4
      [--frames N]       # render only N frames (smoke test)
"""
import argparse
import json
import os
import subprocess
import sys

import imageio_ffmpeg
from PIL import Image, ImageDraw

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import common
common.assert_content_language_en()  # fail-closed EN-only  # noqa: E402
from reel_engine import Reel, W, H, FPS, font  # noqa: E402


def stills(reel, out_dir, tag):
    """3 previews (start / middle / end) + per-beat start/mid/end frames for QA + contact sheet."""
    os.makedirs(out_dir, exist_ok=True)
    prev = []
    for name, t in (("start", 1.6), ("middle", reel.total * 0.5), ("end", reel.total - 2.0)):
        p = os.path.join(out_dir, f"preview_{name}.jpg")
        reel.frame(t).save(p, quality=86)
        prev.append(p)
    # QA sample frames: start / middle / end of every beat
    qa_frames = []
    for i, (t0, beat) in enumerate(reel.cuts):
        t1 = reel.cut_t[i + 1] if i + 1 < len(reel.cut_t) else reel.total
        for pos, t in (("s", t0 + 0.6), ("m", (t0 + t1) / 2), ("e", max(t0 + 0.6, t1 - 0.5))):
            qa_frames.append((beat, pos, min(t, reel.total - 0.05)))
    thumbs = []
    qa_dir = os.path.join(out_dir, "qa_frames")
    os.makedirs(qa_dir, exist_ok=True)
    for k, (beat, pos, t) in enumerate(qa_frames):
        im = reel.frame(t)
        im.save(os.path.join(qa_dir, f"{k:02d}_{beat}_{pos}.jpg"), quality=80)
        thumbs.append((im.resize((270, 480), Image.BILINEAR), f"{beat} {pos} {t:.1f}s"))
    cols = 6
    rows = (len(thumbs) + cols - 1) // cols
    sheet = Image.new("RGB", (cols * 280 + 10, rows * 512 + 10), (14, 12, 10))
    d = ImageDraw.Draw(sheet)
    for i, (im, lab) in enumerate(thumbs):
        x, y = 10 + (i % cols) * 280, 10 + (i // cols) * 512
        sheet.paste(im, (x, y))
        d.text((x + 4, y + 484), lab, fill=(233, 180, 74), font=font("en", 600, 16))
    sheet_path = os.path.join(out_dir, "qa_contact_sheet.jpg")
    sheet.save(sheet_path, quality=70)
    return prev, sheet_path, qa_frames


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--ep", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--safe", action="store_true")
    ap.add_argument("--stills-only", action="store_true")
    ap.add_argument("--frames", type=int, default=0)
    ap.add_argument("--policy", default=None)
    a = ap.parse_args()

    pol = common.policy(a.policy)
    reel = Reel(a.ep, pol, safe=a.safe)
    tag = os.path.basename(os.path.abspath(a.ep)).replace("auto-", "")
    out_dir = os.path.join(os.path.dirname(os.path.abspath(a.out)), "drafts", f"auto-{tag}")
    prev, sheet, qa_frames = stills(reel, out_dir, tag)
    layout = dict(reel.layout)
    layout.update({"total": reel.total, "fps": FPS, "width": W, "height": H, "safe_mode": a.safe,
                   "beats": [{"beat": b, "start": t} for t, b in reel.cuts],
                   "qa_frames": [{"beat": b, "pos": p, "t": t} for b, p, t in qa_frames],
                   "previews": prev, "contact_sheet": sheet})
    # Render manifest for the final rendered-visual QA: which scenes rendered
    # code visuals (the plan's justified set) — cross-checked by the supervisor
    # against the rendered frames.
    if getattr(reel, "plan", None):
        layout["visual_plan"] = {
            "pillar": reel.plan.get("pillar"),
            "photo_policy": reel.plan.get("photo_policy"),
            "scenes": [{k: sc.get(k) for k in ("scene_id", "beat", "visual_category",
                                               "visual_purpose", "code_justified",
                                               "cursor_justified", "photo_designated")}
                       | {"asset_kind": (sc.get("asset") or {}).get("kind"),
                          "asset_id": (sc.get("asset") or {}).get("id")}
                       for sc in reel.plan["scenes"]],
            "code_scenes": reel.code_scenes_rendered,
        }
    common.save_json(os.path.join(a.ep, "layout.json"), layout)
    print(f"[render] previews → {out_dir}  (contact sheet is a QA artifact, not a deliverable)")
    if a.stills_only:
        return

    FF = imageio_ffmpeg.get_ffmpeg_exe()
    n = int(reel.total * FPS) if not a.frames else min(a.frames, int(reel.total * FPS))
    crf = "23" if a.safe else "21"
    preset = "medium"
    cmd = [FF, "-y", "-hide_banner", "-loglevel", "error",
           "-f", "rawvideo", "-pix_fmt", "rgb24", "-s", f"{W}x{H}", "-r", str(FPS), "-i", "-",
           "-i", reel.audio_full,
           "-c:v", "libx264", "-preset", preset, "-crf", crf, "-pix_fmt", "yuv420p", "-profile:v", "high",
           "-g", str(FPS * 2), "-c:a", "aac", "-b:a", "160k", "-ar", "44100", "-movflags", "+faststart",
           "-shortest", a.out]
    os.makedirs(os.path.dirname(os.path.abspath(a.out)), exist_ok=True)
    proc = subprocess.Popen(cmd, stdin=subprocess.PIPE)
    for f in range(n):
        proc.stdin.write(reel.frame(f / FPS).tobytes())
        if f % 300 == 0:
            print(f"[render] frame {f}/{n}", flush=True)
    proc.stdin.close()
    proc.wait()
    if proc.returncode != 0:
        raise SystemExit(f"[render] ffmpeg failed with code {proc.returncode} — render-error")
    print(f"[render] done → {a.out} ({os.path.getsize(a.out) / 1e6:.1f} MB, {n / FPS:.1f}s)")


if __name__ == "__main__":
    main()
