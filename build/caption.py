"""Caption assembler: <epdir>/script.json["caption"] → output/<name>_caption.txt (<=2200 chars).
usage: python3 build/caption.py <epdir> [out.txt]

File format (consumed by buffer_publish.split_caption): caption body, blank
line, then ONE hashtag line. Hashtags go to the Instagram first comment.
"""
import json
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import common
common.assert_content_language_en()
LIMIT = 2200


def build(sc):
    c = sc.get("caption", {})
    parts = [c.get("hook", "").strip(), "", c.get("intro", "").strip()]
    for sec in c.get("sections", []):
        parts += ["", f"{sec.get('icon', '✦')} {sec['title']}"]
        parts += [f"→ {ln}" for ln in sec.get("lines", [])]
    src = c.get("sources", [])
    if src:
        parts += ["", "📚 SOURCES"]
        parts += [f"{i}. {s}" for i, s in enumerate(src, 1)]
    ctas = [x for x in c.get("ctas", []) if x]
    if ctas:
        parts += [""] + ctas
    if sc.get("meta", {}).get("logo") == "stand-in":
        pass                                     # never claim an official logo in captions
    cap = "\n".join(parts).strip()
    cap = re.sub(r"\n{3,}", "\n\n", cap)
    tag = " ".join(dict.fromkeys(c.get("hashtags", [])))
    return cap, tag


def fit(cap, tag):
    if len(cap) <= LIMIT:
        return cap
    cap2 = re.sub(r"\((\d{4})[^)]*\)", r"(\1)", cap)          # shrink citations
    if len(cap2) <= LIMIT:
        return cap2
    # drop the sources block last-resort
    cap3 = re.sub(r"\n\n📚 SOURCES(?:\n[^\n]*)+", "", cap2)
    if len(cap3) <= LIMIT:
        return cap3
    return cap3[:LIMIT - 1].rstrip() + "…"


def main():
    epdir = os.path.abspath(sys.argv[1])
    name = os.path.basename(epdir)
    with open(f"{epdir}/script.json", encoding="utf-8") as fh:
        sc = json.load(fh)
    cap, tag = build(sc)
    cap = fit(cap, tag)
    out = sys.argv[2] if len(sys.argv) > 2 else f"{ROOT}/output/{name}_caption.txt"
    os.makedirs(os.path.dirname(os.path.abspath(out)), exist_ok=True)
    with open(out, "w", encoding="utf-8") as f:
        f.write(cap + "\n\n" + tag + "\n")
    print(f"caption: {len(cap)} chars (limit {LIMIT}) → {out}")
    print("hashtags (first comment):", tag)


if __name__ == "__main__":
    main()
