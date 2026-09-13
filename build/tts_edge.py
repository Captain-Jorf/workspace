"""Free neural TTS via Microsoft Edge read-aloud endpoint (no API key).
usage: python3 build/tts_edge.py <epdir> [voice]   → <epdir>/cNN.mp3
"""
import asyncio, json, os, sys
import edge_tts

VOICE = "en-US-ChristopherNeural"   # casual, clear; swap freely (edge-tts --list-voices)


async def synth(text, path, voice):
    await edge_tts.Communicate(text, voice).save(path)


def main():
    ep = os.path.abspath(sys.argv[1])
    voice = sys.argv[2] if len(sys.argv) > 2 else VOICE
    sc = json.load(open(f"{ep}/script.json"))
    for i, ch in enumerate(sc["chunks"], 1):
        txt = " ".join(l["t"] for l in ch["en"])
        out = f"{ep}/c{i:02d}.mp3"
        asyncio.run(synth(txt, out, voice))
        print("tts", os.path.basename(out))


if __name__ == "__main__":
    main()
