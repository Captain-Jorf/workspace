"""Offline stand-in for tts_edge.py — LOCAL TESTS ONLY (never used by the workflow).

Generates speech-like noise bursts (one burst per word, ~2.6 words/s) so that
timing/render/QA can be exercised without network access. The audio is clearly
not speech; the supervisor's real run uses Edge TTS.

usage: python3 build/tts_synthetic.py <epdir>
"""
import json
import os
import subprocess
import sys

import imageio_ffmpeg
import numpy as np

FF = imageio_ffmpeg.get_ffmpeg_exe()
SR = 24000


def burst(n, rng, f0):
    t = np.arange(n) / SR
    env = np.sin(np.pi * np.arange(n) / n) ** 0.6
    sig = 0.35 * np.sin(2 * np.pi * f0 * t) + 0.15 * np.sin(2 * np.pi * 2 * f0 * t) + 0.08 * rng.standard_normal(n)
    return (sig * env).astype(np.float32)


def main():
    ep = os.path.abspath(sys.argv[1])
    sc = json.load(open(f"{ep}/script.json", encoding="utf-8"))
    rng = np.random.default_rng(7)
    for i, ch in enumerate(sc["chunks"], 1):
        words = (ch.get("tts_text") or " ".join(l["t"] for l in ch["en"])).split()
        pieces = [np.zeros(int(SR * 0.12), np.float32)]
        for w in words:
            n = int(SR * (0.16 + 0.05 * min(len(w), 8)))
            pieces.append(burst(n, rng, 110 + rng.integers(0, 60)))
            pieces.append(np.zeros(int(SR * 0.06), np.float32))
            if w.endswith((".", "?", "!")):
                pieces.append(np.zeros(int(SR * 0.25), np.float32))
        pieces.append(np.zeros(int(SR * 0.2), np.float32))
        audio = np.concatenate(pieces)
        pcm = (np.clip(audio, -1, 1) * 32767).astype(np.int16).tobytes()
        out = f"{ep}/c{i:02d}.mp3"
        subprocess.run([FF, "-y", "-hide_banner", "-loglevel", "error", "-f", "s16le", "-ar", str(SR), "-ac", "1",
                        "-i", "-", "-c:a", "libmp3lame", "-b:a", "64k", out], input=pcm, check=True)
        print("synthetic", os.path.basename(out), f"{len(audio) / SR:.1f}s")


if __name__ == "__main__":
    main()
