"""Free neural TTS via Microsoft Edge read-aloud endpoint (no API key).
usage: python3 build/tts_edge.py <epdir> [voice]   → <epdir>/cNN.mp3

Speaks chunk["tts_text"] when present (pronunciation overrides such as
"@metacognition.hq" → "at metacognition H Q"); subtitles keep the original
text. Retries each chunk a few times; a chunk that stays empty is a tts-error.

Also stores the service's real word boundaries as <epdir>/cNN.words.json
([{"text", "start", "end"} in seconds, relative to the mp3]) so timing.py can
drive the karaoke highlight from measured timings instead of estimates.
"""
import asyncio
import json
import os
import sys
import time

import edge_tts

VOICE = "en-US-ChristopherNeural"   # casual, clear; swap freely (edge-tts --list-voices)
RATE = "+5%"


def policy_tts():
    try:
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        pol = json.load(open(os.path.join(root, "content", "editorial_policy.json"), encoding="utf-8"))
        return pol.get("tts", {})
    except Exception:                                         # noqa: BLE001
        return {}


async def synth(text, path, voice, rate):
    meta = path[:-4] + ".meta.jsonl"
    try:
        com = edge_tts.Communicate(text, voice, rate=rate, boundary="WordBoundary")   # edge-tts >= 7
    except TypeError:
        com = edge_tts.Communicate(text, voice, rate=rate)                            # edge-tts 6.x: words by default
    await com.save(path, meta)
    words = []
    try:
        with open(meta, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                m = json.loads(line)
                if m.get("type") != "WordBoundary":
                    continue
                st = m["offset"] / 1e7
                words.append({"text": m.get("text", ""), "start": round(st, 3),
                              "end": round(st + m.get("duration", 0) / 1e7, 3)})
    except (OSError, ValueError, KeyError):
        words = []
    finally:
        try:
            os.remove(meta)
        except OSError:
            pass
    with open(path[:-4] + ".words.json", "w", encoding="utf-8") as fh:
        json.dump(words, fh, ensure_ascii=False)


def main():
    ep = os.path.abspath(sys.argv[1])
    pt = policy_tts()
    voice = sys.argv[2] if len(sys.argv) > 2 else os.environ.get("TTS_VOICE", pt.get("voice", VOICE))
    rate = os.environ.get("TTS_RATE", pt.get("rate", RATE))
    with open(f"{ep}/script.json", encoding="utf-8") as fh:
        sc = json.load(fh)
    for i, ch in enumerate(sc["chunks"], 1):
        txt = ch.get("tts_text") or " ".join(l["t"] for l in ch["en"])
        out = f"{ep}/c{i:02d}.mp3"
        last = None
        for attempt in range(4):
            try:
                asyncio.run(asyncio.wait_for(synth(txt, out, voice, rate), timeout=120))   # a stalled socket ≠ a hung run
                if os.path.getsize(out) > 2000:
                    break
                last = "empty audio"
            except Exception as e:                            # noqa: BLE001
                last = f"{type(e).__name__}: {e}"
            time.sleep(2 + attempt * 3)
        else:
            raise SystemExit(f"[tts] chunk {i} failed after retries ({last}) — tts-error")
        print("tts", os.path.basename(out), f"{os.path.getsize(out) // 1024} KB")


if __name__ == "__main__":
    main()
