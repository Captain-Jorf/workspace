"""One-command episode producer.
usage: python3 build/make_reel.py content/episodes/ep02 [--no-render]
Requires: <epdir>/script.json + <epdir>/cNN.mp3 TTS clips (generated in Arena).
"""
import json, os, subprocess, sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def run(cmd, env):
    subprocess.run(cmd, check=True, env=env)


def main():
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    norun = "--no-render" in sys.argv
    epdir = os.path.abspath(args[0])
    name = os.path.basename(epdir)
    sc = json.load(open(f"{epdir}/script.json"))
    n = len(sc["chunks"])
    missing = [i for i in range(1, n + 1) if not os.path.exists(f"{epdir}/c{i:02d}.mp3")]
    env = dict(os.environ, EP_DIR=epdir, PYTHONPATH=f"{ROOT}/build")
    if missing:
        print(f"[{name}] missing TTS clips: " + ", ".join(f"c{i:02d}" for i in missing))
        print("In Arena, generate speech (page voice) for each line below, saved into the episode folder:")
        for i in missing:
            txt = " ".join(l["t"] for l in sc["chunks"][i - 1]["en"])
            print(f"  c{i:02d}.mp3 ← \"{txt}\"")
        sys.exit(1)
    print(f"[{name}] 1/3 timing…")
    run([sys.executable, f"{ROOT}/build/timing.py"], env)
    if not norun:
        print(f"[{name}] 2/3 render…")
        run([sys.executable, f"{ROOT}/build/render.py",
             f"out={ROOT}/output/{name}_reel.mp4"], env)
    print(f"[{name}] 3/3 caption…")
    run([sys.executable, f"{ROOT}/build/caption.py", epdir], env)
    print(f"[{name}] DONE → output/{name}_reel.mp4 + output/{name}_caption.txt")


if __name__ == "__main__":
    main()
