"""Caption assembler: <epdir>/script.json["caption"] → output/<name>_caption.txt (<=2200 chars)."""
import json, os, sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def build(sc):
    c = sc.get("caption", {})
    parts = [c.get("hook", ""), "", c.get("intro", "")]
    for sec in c.get("sections", []):
        parts += ["", f"{sec.get('icon', '✦')} {sec['title']}"]
        parts += [f"→ {ln}" for ln in sec.get("lines", [])]
    src = c.get("sources", [])
    if src:
        parts += ["", "📚 SOURCES"]
        parts += [f"{i}. {s}" for i, s in enumerate(src, 1)]
    parts += [""] + c.get("ctas", [])
    cap = "\n".join(p for p in parts)
    tag = " ".join(c.get("hashtags", []))
    return cap, tag


def fit(cap, tag):
    if len(cap) <= 2200:
        return cap
    # shrink: drop source details to first author + year
    import re
    cap2 = re.sub(r"\((\d{4})[^)]*\)", r"(\1)", cap)
    if len(cap2) <= 2200:
        return cap2
    return cap2[:2197] + "…"


def main():
    epdir = os.path.abspath(sys.argv[1])
    name = os.path.basename(epdir)
    sc = json.load(open(f"{epdir}/script.json"))
    cap, tag = build(sc)
    cap = fit(cap, tag)
    out = f"{ROOT}/output/{name}_caption.txt"
    with open(out, "w") as f:
        f.write(cap + "\n\n" + tag + "\n")
    print(f"caption: {len(cap)} chars (limit 2200) → {out}")
    print("hashtags go in the first comment:")
    print(tag)


if __name__ == "__main__":
    main()
