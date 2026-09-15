"""Master audio track + word-level timing map.

  EP_DIR=content/episodes/auto-<tag> python3 build/timing.py
    → <ep>/full.wav (44.1 kHz stereo, loudness-normalised)
    → <ep>/timing.json  {total, chunks[{id, beat, start, dur, lines[{text, scene, beat,
                          start, end, words[{w,start,end}]}], fa[{text,start,end}]}]}

Per chunk: mp3 → wav, TTS lead/trail silence trimmed (so subtitles never wait on
silence), duration measured on the trimmed wav. Word timing comes from the TTS
service's own word boundaries (<ep>/cNN.words.json, written by tts_edge.py) when
they align with the script tokens; otherwise it falls back to a length-proportional
estimate inside each display line. Persian lines are proportional inside the
chunk. The whole track is loudness-normalised (EBU R128, -16 LUFS) so every reel
sits at the same level.
"""
import json
import os
import re
import subprocess
import sys
import tempfile

import imageio_ffmpeg

FF = imageio_ffmpeg.get_ffmpeg_exe()
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def dur(path):
    r = subprocess.run([FF, "-i", path], capture_output=True, text=True)
    m = re.search(r"Duration: (\d+):(\d+):(\d+\.\d+)", r.stderr)
    if not m:
        raise SystemExit(f"[timing] cannot read duration of {path} — tts-error")
    h, mi, s = m.groups()
    return int(h) * 3600 + int(mi) * 60 + float(s)


def run(args):
    subprocess.run([FF, "-y", "-hide_banner", "-loglevel", "error"] + args, check=True)


def word_weights(text):
    return [len(w) + 2.2 for w in text.split()]


def _norm(tok):
    return re.sub(r"[^a-z0-9]+", "", tok.lower().replace("’", "'"))


def align_boundaries(tokens, boundaries):
    """Map script tokens (with punctuation) onto the TTS service's word boundaries.

    Greedy, in-order, on alphanumeric content only: a script token may span several service
    words ("HQ" ← "H", "Q"; "check-in" ← "check", "in"). Returns [(start, end)] per token, or
    None when the alignment is not trustworthy (different wording, spelled-out numbers,
    pronunciation overrides, missing metadata) — the caller then falls back to estimates."""
    bs = [(b, _norm(b.get("text", ""))) for b in (boundaries or [])]
    bs = [(b, n) for b, n in bs if n and "start" in b and "end" in b]
    if not bs or not tokens:
        return None
    bi, out = 0, []
    for tok in tokens:
        want = _norm(tok)
        if not want:                                        # punctuation-only token ("—")
            prev_end = out[-1][1] if out else bs[0][0]["start"]
            out.append((prev_end, prev_end))
            continue
        got, st, en = "", None, None
        while len(got) < len(want):
            if bi >= len(bs):
                return None
            b, piece = bs[bi]
            if st is None:
                st = b["start"]
            got += piece
            en = b["end"]
            bi += 1
            if not want.startswith(got):
                return None
        out.append((st, en))
    if bi != len(bs):                                       # the service spoke more words than the script
        return None
    return out


def load_boundaries(mp3):
    path = mp3[:-4] + ".words.json"
    if not os.path.exists(path):
        return None
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return None
    return data if isinstance(data, list) and data else None


def main():
    adir = os.environ.get("EP_DIR")
    sc_path = f"{adir}/script.json" if adir else f"{ROOT}/content/script.json"
    adir = adir or f"{ROOT}/audio"
    with open(sc_path, encoding="utf-8") as fh:
        sc = json.load(fh)
    meta = sc["meta"]
    gap, lead, tail = meta["gap"], meta["lead"], meta["tail"]
    tmp = tempfile.mkdtemp(prefix="timing_")
    parts, t = [], lead
    timeline = {"total": 0.0, "lead": lead, "tail": tail, "gap": gap, "chunks": []}
    trim = ("silenceremove=start_periods=1:start_threshold=-45dB:start_silence=0.08,"
            "areverse,silenceremove=start_periods=1:start_threshold=-45dB:start_silence=0.12,areverse,"
            "apad=pad_dur=0.08")
    for ch in sc["chunks"]:
        mp3 = f"{adir}/c{int(ch['id'][1:]):02d}.mp3"
        if not os.path.exists(mp3) or os.path.getsize(mp3) < 1000:
            raise SystemExit(f"[timing] missing/empty audio {mp3} — tts-error")
        wav = os.path.join(tmp, f"{ch['id']}.wav")
        run(["-i", mp3, "-af", trim, "-ar", "44100", "-ac", "2", "-c:a", "pcm_s16le", wav])
        d = dur(wav)
        if d < 0.4:
            raise SystemExit(f"[timing] chunk {ch['id']} is only {d:.2f}s — tts-error")
        lines = []
        # measured word boundaries (Edge TTS) → exact karaoke; only when the spoken text is the
        # display text (no pronunciation overrides) and the tokens align
        measured = None
        spoken = ch.get("tts_text")
        display = " ".join(l["t"] for l in ch["en"])
        bounds = load_boundaries(mp3)
        if bounds and (not spoken or spoken == display):
            pairs = align_boundaries(display.split(), bounds)
            if pairs:
                lead_cut = max(0.0, bounds[0]["start"] - 0.08)      # silenceremove keeps 80 ms of lead
                scale = 1.0
                last_end = pairs[-1][1] - lead_cut
                if last_end > d:                                    # tail was trimmed harder than expected
                    scale = d / last_end
                measured = [(max(0.0, (a - lead_cut) * scale), min(d, max(0.0, (b - lead_cut) * scale)))
                            for a, b in pairs]
        if measured:
            k = 0
            for l in ch["en"]:
                toks = l["t"].split()
                seg = measured[k:k + len(toks)]
                k += len(toks)
                words = [{"w": tok, "start": round(t + a, 3), "end": round(t + max(b, a + 0.05), 3)}
                         for tok, (a, b) in zip(toks, seg)]
                lines.append({"text": l["t"], "scene": l["scene"], "beat": l.get("beat", l["scene"]),
                              "start": round(t + seg[0][0], 3), "end": round(t + seg[-1][1], 3),
                              "words": words, "timing": "measured"})
            # display lines must tile the chunk: extend each line to the next line's start
            for a, b in zip(lines, lines[1:]):
                a["end"] = b["start"]
            lines[0]["start"] = round(t, 3)
            lines[-1]["end"] = round(t + d, 3)
        else:
            lw = [sum(word_weights(l["t"])) for l in ch["en"]]
            tot = sum(lw) or 1
            lt = t
            for l, w in zip(ch["en"], lw):
                ld = d * w / tot
                words, wt = [], lt
                ww = word_weights(l["t"])
                wsum = sum(ww) or 1
                for tok, x in zip(l["t"].split(), ww):
                    wd = ld * x / wsum
                    words.append({"w": tok, "start": round(wt, 3), "end": round(wt + wd, 3)})
                    wt += wd
                lines.append({"text": l["t"], "scene": l["scene"], "beat": l.get("beat", l["scene"]),
                              "start": round(lt, 3), "end": round(lt + ld, 3), "words": words,
                              "timing": "estimated"})
                lt += ld
        fa, fw = [], [len(x) + 4 for x in ch["fa"]]
        ft = t
        for txt, w in zip(ch["fa"], fw):
            fd = d * w / (sum(fw) or 1)
            fa.append({"text": txt, "start": round(ft, 3), "end": round(ft + fd, 3)})
            ft += fd
        timeline["chunks"].append({"id": ch["id"], "beat": ch.get("beat"), "start": round(t, 3),
                                   "dur": round(d, 3), "lines": lines, "fa": fa})
        parts.append(wav)
        t += d + gap
    timeline["total"] = round(t - gap + tail, 3)

    def silence(name, secs):
        p = os.path.join(tmp, name)
        run(["-f", "lavfi", "-i", "anullsrc=r=44100:cl=stereo", "-t", f"{secs:.3f}", "-c:a", "pcm_s16le", p])
        return p

    gapwav, leadwav, tailwav = silence("gap.wav", gap), silence("lead.wav", lead), silence("tail.wav", tail)
    lst = os.path.join(tmp, "concat.txt")
    seq = [leadwav]
    for i, wav in enumerate(parts):
        seq += [wav, gapwav if i < len(parts) - 1 else tailwav]
    with open(lst, "w") as f:
        for p in seq:
            f.write(f"file '{p}'\n")
    raw = os.path.join(tmp, "full_raw.wav")
    run(["-f", "concat", "-safe", "0", "-i", lst, "-c:a", "pcm_s16le", raw])
    # loudness normalisation → consistent level across episodes, no clipping (TP -1.5 dB)
    run(["-i", raw, "-af", "loudnorm=I=-16:TP=-1.5:LRA=11", "-ar", "44100", "-ac", "2",
         "-c:a", "pcm_s16le", f"{adir}/full.wav"])
    real = dur(f"{adir}/full.wav")
    if abs(real - timeline["total"]) > 0.25:
        # loudnorm keeps duration; a mismatch means a broken concat — trust the file, warn loudly
        print(f"[timing] WARNING: computed {timeline['total']}s vs file {real:.3f}s")
        timeline["total"] = round(real, 3)
    measured_n = sum(1 for c in timeline["chunks"] for l in c["lines"] if l.get("timing") == "measured")
    total_n = sum(len(c["lines"]) for c in timeline["chunks"])
    timeline["word_timing"] = {"measured_lines": measured_n, "lines": total_n}
    with open(f"{adir}/timing.json", "w", encoding="utf-8") as fh:
        json.dump(timeline, fh, ensure_ascii=False, indent=1)
    print(f"word timing: {measured_n}/{total_n} lines from TTS word boundaries")
    print("total seconds:", timeline["total"])
    for c in timeline["chunks"]:
        print(c["id"], c["beat"], c["start"], c["dur"])


if __name__ == "__main__":
    main()
