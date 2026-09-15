"""Free neural TTS via Microsoft Edge read-aloud endpoint (no API key).
usage: python3 build/tts_edge.py <epdir> [voice]   → <epdir>/cNN.mp3

Speaks chunk["tts_text"] when present (pronunciation overrides such as
"@metacognition.hq" → "at metacognition H Q"); subtitles keep the original
text. Retries each chunk a few times; a chunk that stays empty is a tts-error.
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
    await edge_tts.Communicate(text, voice, rate=rate).save(path)


def main():
    ep = os.path.abspath(sys.argv[1])
    pt = policy_tts()
    voice = sys.argv[2] if len(sys.argv) > 2 else os.environ.get("TTS_VOICE", pt.get("voice", VOICE))
    rate = os.environ.get("TTS_RATE", pt.get("rate", RATE))
    sc = json.load(open(f"{ep}/script.json", encoding="utf-8"))
    for i, ch in enumerate(sc["chunks"], 1):
        txt = ch.get("tts_text") or " ".join(l["t"] for l in ch["en"])
        out = f"{ep}/c{i:02d}.mp3"
        last = None
        for attempt in range(4):
            try:
                asyncio.run(synth(txt, out, voice, rate))
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
