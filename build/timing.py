"""Build the master audio track + word-level timing map (proportional allocation)."""
import json, os, re, subprocess, sys
import imageio_ffmpeg

FF = imageio_ffmpeg.get_ffmpeg_exe()
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def dur(path):
    r = subprocess.run([FF, "-i", path], capture_output=True, text=True)
    m = re.search(r"Duration: (\d+):(\d+):(\d+\.\d+)", r.stderr)
    h, mi, s = m.groups()
    return int(h) * 3600 + int(mi) * 60 + float(s)


def run(args):
    subprocess.run([FF, "-y", "-hide_banner", "-loglevel", "error"] + args, check=True)


def main():
    adir = os.environ.get("EP_DIR") or f"{ROOT}/audio"
    sc = json.load(open(f"{adir}/script.json" if os.environ.get("EP_DIR") else f"{ROOT}/content/script.json"))
    meta = sc["meta"]
    gap, lead, tail = meta["gap"], meta["lead"], meta["tail"]
    parts, t = [], lead
    timeline = {"total": 0.0, "chunks": []}
    for ch in sc["chunks"]:
        mp3 = f"{adir}/c{int(ch['id'][1:]):02d}.mp3"
        d = dur(mp3)
        wav = f"/tmp/{ch['id']}.wav"
        run(["-i", mp3, "-ar", "44100", "-ac", "2", "-c:a", "pcm_s16le", wav])
        # word timing: proportional inside each display line
        lines = []
        lw = [sum(len(w) + 2.2 for w in l["t"].split()) for l in ch["en"]]
        tot = sum(lw)
        lt = t
        for l, w in zip(ch["en"], lw):
            ld = d * w / tot
            words, wt = [], lt
            wsum = sum(len(x) + 2.2 for x in l["t"].split())
            for tok in l["t"].split():
                wd = ld * (len(tok) + 2.2) / wsum
                words.append({"w": tok, "start": wt, "end": wt + wd})
                wt += wd
            lines.append({"text": l["t"], "scene": l["scene"], "start": lt, "end": lt + ld,
                          "words": words})
            lt += ld
        # persian lines proportional across chunk
        fa, fw = [], [len(x) + 4 for x in ch["fa"]]
        ft = t
        for txt, w in zip(ch["fa"], fw):
            fd = d * w / sum(fw)
            fa.append({"text": txt, "start": ft, "end": ft + fd})
            ft += fd
        timeline["chunks"].append({"id": ch["id"], "start": t, "dur": d, "lines": lines, "fa": fa})
        parts.append((wav, d))
        t += d + gap
    timeline["total"] = t - gap + tail
    # ---- concat audio with gaps
    gapwav = "/tmp/gap.wav"
    run(["-f", "lavfi", "-i", f"anullsrc=r=44100:cl=stereo", "-t", str(gap), "-c:a", "pcm_s16le", gapwav])
    leadwav = "/tmp/lead.wav"
    run(["-f", "lavfi", "-i", f"anullsrc=r=44100:cl=stereo", "-t", str(lead), "-c:a", "pcm_s16le", leadwav])
    tailwav = "/tmp/tail.wav"
    run(["-f", "lavfi", "-i", f"anullsrc=r=44100:cl=stereo", "-t", str(tail), "-c:a", "pcm_s16le", tailwav])
    lst = "/tmp/concat.txt"
    seq = [leadwav]
    for i, (wav, _) in enumerate(parts):
        seq.append(wav)
        seq.append(gapwav if i < len(parts) - 1 else tailwav)
    with open(lst, "w") as f:
        for p in seq:
            f.write(f"file '{p}'\n")
    run(["-f", "concat", "-safe", "0", "-i", lst, "-c:a", "pcm_s16le", f"{adir}/full.wav"])
    json.dump(timeline, open(f"{adir}/timing.json", "w"), ensure_ascii=False, indent=1)
    print("total seconds:", round(timeline["total"], 2))
    for c in timeline["chunks"]:
        print(c["id"], round(c["start"], 2), round(c["dur"], 2))


if __name__ == "__main__":
    main()
