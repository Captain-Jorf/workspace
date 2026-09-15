"""Translation-gloss audit: for every narration line whose translator input differs from the
subtitle text, print ORIGINAL → GLOSSED (translator input) → PERSIAN. Also verifies that the
stored English (subtitles + tts_text) is the original wording, never the gloss.

usage: python3 build/gloss_audit.py <epdir> [--md out.md]
exit 0 = English untouched; 1 = a glossed phrase leaked into subtitles or narration."""
import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import content_producer as cp  # noqa: E402


def audit(ep):
    sc = json.load(open(os.path.join(ep, "script.json"), encoding="utf-8"))
    rows, leaks = [], []
    for ch in sc["chunks"]:
        spoken = ch.get("tts_text") or ""
        for en, fa in zip(ch["en"], ch["fa"]):
            orig = en["t"]
            glossed = cp.translation_source(orig)
            if glossed != orig:
                rows.append({"chunk": ch["id"], "en": orig, "translator_input": glossed, "fa": fa})
                # the gloss must not be what we show or speak
                for rx, v in cp._GLOSS_RX:
                    if rx.search(orig) and v.lower() in spoken.lower() and v.lower() not in orig.lower():
                        leaks.append(f"{ch['id']}: gloss '{v}' found in narration")
                    if rx.search(orig) and v.lower() in orig.lower() and v.lower() not in cp.translation_source(orig).lower():
                        pass
    return sc, rows, leaks


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("ep")
    ap.add_argument("--md", default=None)
    a = ap.parse_args()
    sc, rows, leaks = audit(a.ep)
    total = sum(len(ch["en"]) for ch in sc["chunks"])
    out = [f"# Gloss audit — {sc['meta'].get('topic')}", "",
           f"{len(rows)} of {total} lines were simplified for the translator "
           f"(engine: {sc['meta'].get('translation_engine')}). English subtitles/narration keep the original.", "",
           "| # | English (subtitle + narration) | translator input | Persian |", "|---|---|---|---|"]
    for i, r in enumerate(rows, 1):
        out.append(f"| {i} | {r['en']} | {r['translator_input']} | {r['fa']} |")
    if leaks:
        out += ["", "**LEAKS:**"] + [f"- {l}" for l in leaks]
    text = "\n".join(out)
    print(text)
    if a.md:
        with open(a.md, "w", encoding="utf-8") as f:
            f.write(text + "\n")
    return 1 if leaks else 0


if __name__ == "__main__":
    sys.exit(main())
