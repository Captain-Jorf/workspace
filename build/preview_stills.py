"""3 preview stills for a draft episode (uses EP_DIR)."""
import os, sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import render

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.makedirs(f"{ROOT}/output/drafts", exist_ok=True)
tot = render.R.total
for t in (2.0, tot * 0.45, tot - 2.0):
    p = f"{ROOT}/output/drafts/preview_{t:.0f}.jpg"
    render.frame(t).save(p, quality=85)
    print("preview", p)
