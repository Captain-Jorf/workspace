"""Main frame loop: bg ken-burns + concept web + scene fg + captions + chrome -> ffmpeg."""
import math, os, subprocess, sys, bisect
import numpy as np
from PIL import Image, ImageDraw
import imageio_ffmpeg
from engine import (ROOT, W, H, FPS, GOLD, GOLD_HI, WARM, DIM, font, clamp,
                    ease, ease_out, lerp, text_img, gold_text, glow_disc, res)
from scenes import SCENES, BGKIND

R = res()
BW, BH = 1296, 2304
CUT_T = [t for t, _ in R.cuts]


def scene_at(t):
    i = bisect.bisect_right(CUT_T, t) - 1
    return max(0, i)


def bg_crop(kind, t, t0, sd, punch=0.0):
    big = {"base": R.bg_base, "brain": R.bg_brain, "desk": R.bg_desk}[kind]
    pr = clamp((t - t0) / sd)
    scale = (1.06 + 0.10 * pr) * (1 + punch)
    dx = lerp(-34, 34, pr)
    dy = lerp(-20, 26, pr)
    cw, ch = W * 1.2 / scale, H * 1.2 / scale
    cx, cy = BW / 2 + dx, BH / 2 + dy
    box = (int(cx - cw / 2), int(cy - ch / 2), int(cx + cw / 2), int(cy + ch / 2))
    return big.crop(box).resize((W, H), Image.BILINEAR)


def draw_web(fr, t, cur_scene):
    d = ImageDraw.Draw(fr)
    ss = R.scene_start
    for a, b in R.edges:
        na, nb = R.nodes[a], R.nodes[b]
        t0 = max(ss[na["sc"]], ss[nb["sc"]])
        p = ease((t - t0) / 0.9)
        if p <= 0:
            continue
        hot = na["sc"] == cur_scene or nb["sc"] == cur_scene
        al = int(150 if hot else 62)
        x0, y0, x1, y1 = na["x"], na["y"], nb["x"], nb["y"]
        mx, my = (x0 + x1) / 2, (y0 + y1) / 2
        dx, dy = x1 - x0, y1 - y0
        L = math.hypot(dx, dy) or 1
        cx, cy = mx - dy / L * L * 0.16, my + dx / L * L * 0.16
        pts = []
        n = 22
        for i in range(int(n * p) + 1):
            u = i / n
            px = (1 - u) ** 2 * x0 + 2 * (1 - u) * u * cx + u ** 2 * x1
            py = (1 - u) ** 2 * y0 + 2 * (1 - u) * u * cy + u ** 2 * y1
            pts.append((px, py))
        for i in range(len(pts) - 1):
            d.line([pts[i], pts[i + 1]], fill=(233, 180, 74, al), width=2)
    for k, nd in R.nodes.items():
        t0 = ss[nd["sc"]]
        if t < t0:
            continue
        p = ease((t - t0) / 0.5)
        hot = nd["sc"] == cur_scene
        r = (9 if hot else 6) * p
        x, y = nd["x"], nd["y"]
        if hot:
            g = glow_disc(90, (255, 200, 110, 150), 26, 14)
            fr.alpha_composite(g, (int(x) - 45, int(y) - 45))
        d.ellipse([x - r, y - r, x + r, y + r], fill=GOLD if hot else (120, 102, 70))
        if cur_scene == "loop" and k in ("plan", "monitor", "evaluate"):
            continue
        lab = nd["lab_on"] if hot else nd["lab"]
        li = lab.copy()
        li.putalpha(li.getchannel("A").point(lambda v: int(v * p * (1.0 if hot else 0.75))))
        fr.alpha_composite(li, (int(x - lab.width / 2), int(y + 14)))


def draw_captions(fr, t):
    lines = R.cap_lines
    i = bisect.bisect_right([l["start"] for l in lines], t) - 1
    if i < 0:
        return
    ln = lines[i]
    a = ease((t - ln["start"]) / 0.16)
    y = 176
    for row in ln["rows"]:
        _draw_row(fr, row, y, t, a)
        y += 80


def _draw_row(fr, row, y, t, a):
    rw = row[-1][2] + row[-1][1][0].width
    x = (W - rw) // 2
    for wd, (wi, gi), off in row:
        wx = x + off
        if t >= wd["end"]:
            im = wi
            al = 232 * a
        elif t >= wd["start"]:
            pop = 1 + 0.22 * (1 - ease(min(1, (t - wd["start"]) / 0.13)))
            im = gi.resize((int(gi.width * pop), int(gi.height * pop)), Image.BILINEAR)
            al = 255 * a
        else:
            continue
        ii = im.copy()
        ii.putalpha(ii.getchannel("A").point(lambda v: int(v * al / 255)))
        fr.alpha_composite(ii, (int(wx), int(y + (wi.height - im.height) // 2)))


def _draw_caret(fr, ln, y, t, a):
    if t < ln["end"] + 0.1 and int(t * 2.7) % 2 == 0:
        act = ln["rows"][0]
        for row in ln["rows"]:
            if t >= row[0][0]["start"]:
                act = row
        rw = act[-1][2] + act[-1][1][0].width
        x = (W - rw) // 2
        cx = x
        for wd, (wi, gi), off in act:
            if t >= wd["start"]:
                cx = x + off + wi.width + 10
        hgt = act[0][1][0].height
        d = ImageDraw.Draw(fr)
        d.rectangle([cx, y + 8, cx + 7, y + 8 + int(hgt * 0.72)], fill=(255, 228, 158, int(230 * a)))


def _caret_wrap(fr, t):
    lines = R.cap_lines
    i = bisect.bisect_right([l["start"] for l in lines], t) - 1
    if i < 0:
        return
    ln = lines[i]
    a = ease((t - ln["start"]) / 0.16)
    idx = 0
    for r_i, row in enumerate(ln["rows"]):
        if t >= row[0][0]["start"]:
            idx = r_i
    _draw_caret(fr, ln, 176 + idx * 80, t, a)


def draw_fa(fr, t):
    lines = R.fa_lines
    i = bisect.bisect_right([l["start"] for l in lines], t) - 1
    if i < 0 or t > lines[i]["end"] + 0.12:
        return
    ln = lines[i]
    a = ease((t - ln["start"]) / 0.22)
    img = ln["img"].copy()
    img.putalpha(img.getchannel("A").point(lambda v: int(v * a)))
    y = 1648 + int((1 - a) * 26)
    fr.alpha_composite(img, ((W - img.width) // 2, y))


def draw_chrome(fr, t, cur_scene, scene_t0):
    d = ImageDraw.Draw(fr)
    p = clamp(t / R.total)
    d.rectangle([0, 0, int(W * p), 7], fill=GOLD)
    d.ellipse([int(W * p) - 8, -5, int(W * p) + 8, 11], fill=GOLD_HI)
    wa = R.wmark.copy()
    wa.putalpha(wa.getchannel("A").point(lambda v: int(v * 0.92)))
    fr.alpha_composite(wa, (W - 132, 52))
    hi = R.handle_img
    fr.alpha_composite(hi, ((W - hi.width) // 2, 1842))
    tag = R.tag_imgs[cur_scene]
    a = ease((t - scene_t0) / 0.3)
    ti = tag.copy()
    ti.putalpha(ti.getchannel("A").point(lambda v: int(v * a)))
    fr.alpha_composite(ti, (64 - int((1 - a) * 30), 108))
    d.line([64, 152, 64 + int(tag.width * a), 152], fill=(233, 180, 74, 160), width=2)


def frame(t):
    si = scene_at(t)
    cur_scene, t0 = R.cuts[si][1], CUT_T[si]
    sd = (CUT_T[si + 1] if si + 1 < len(CUT_T) else R.total) - t0
    ts = t - t0
    # transition punch
    punch = 0.0
    flash = 0.0
    for ct in CUT_T[1:]:
        dt = abs(t - ct)
        if dt < 0.30:
            k = 1 - dt / 0.30
            punch = max(punch, 0.025 * k)
            flash = max(flash, k)
    bgkind = BGKIND[cur_scene]
    bg = bg_crop(bgkind, t, t0, sd, punch)
    fr = bg.convert("RGBA")
    # cross-fade previous bg kind
    if si > 0:
        prev_kind = BGKIND[R.cuts[si - 1][1]]
        if prev_kind != bgkind and ts < 0.35:
            pb = bg_crop(prev_kind, t, t0, sd, punch).convert("RGBA")
            fr = Image.blend(pb, fr, ease(ts / 0.35))
    draw_web(fr, t, cur_scene)
    ctx = dict(R=R, fr=fr, t=t, ts=ts, sd=sd, t0=t0)
    SCENES[cur_scene](ctx)
    draw_captions(fr, t)
    _caret_wrap(fr, t)
    draw_fa(fr, t)
    draw_chrome(fr, t, cur_scene, t0)
    if flash > 0:
        fl = R.flash.copy()
        fl.putalpha(fl.getchannel("A").point(lambda v: int(v * 0.34 * flash)))
        fr.alpha_composite(fl)
    return fr.convert("RGB")


def main():
    a = dict(x.split("=") for x in sys.argv[1:])
    f0 = int(a.get("start", 0))
    f1 = int(a.get("end", int(R.total * FPS)))
    out = a.get("out", f"{ROOT}/output/reel_metacognition_hq.mp4")
    still = int(a.get("still", 0))
    if still:
        os.makedirs(f"{ROOT}/output/stills", exist_ok=True)
        for f in range(f0, f1, still):
            frame(f / FPS).save(f"{ROOT}/output/stills/f{f:05d}.jpg", quality=88)
        print("stills done")
        return
    FF = imageio_ffmpeg.get_ffmpeg_exe()
    cmd = [FF, "-y", "-hide_banner", "-loglevel", "error",
           "-f", "rawvideo", "-pix_fmt", "rgb24", "-s", f"{W}x{H}", "-r", str(FPS), "-i", "-",
           "-i", f"{ROOT}/audio/full.wav",
           "-c:v", "libx264", "-preset", "medium", "-crf", "19", "-pix_fmt", "yuv420p",
           "-c:a", "aac", "-b:a", "192k", "-movflags", "+faststart", "-shortest", out]
    proc = subprocess.Popen(cmd, stdin=subprocess.PIPE)
    for f in range(f0, f1):
        im = frame(f / FPS)
        proc.stdin.write(im.tobytes())
        if f % 300 == 0:
            print(f"frame {f}/{f1}", flush=True)
    proc.stdin.close()
    proc.wait()
    print("done", out)


if __name__ == "__main__":
    main()
