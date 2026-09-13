"""Official Instagram Graph API publisher for the reel factory.

Setup (once):  see content/insta_setup.md  →  secrets/insta.env
Usage:
  python3 build/insta_publish.py token-info
  python3 build/insta_publish.py publish --video URL --caption output/ep02_caption.txt [--at "2026-09-14 19:30"]
  python3 build/insta_publish.py run-due          # cron: * * * * *  (or a background worker)
  python3 build/insta_publish.py refresh-token    # extend the ~60-day page token
Nothing is ever uploaded through unofficial endpoints. Videos must be public URLs.
"""
import json, os, sys, time, urllib.parse, urllib.request, datetime

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ENV = f"{ROOT}/secrets/insta.env"
QUEUE = f"{ROOT}/content/queue.json"
API = "https://graph.facebook.com/v21.0"


def cfg():
    if not os.path.exists(ENV):
        sys.exit(f"missing {ENV} — follow content/insta_setup.md first")
    c = {}
    for line in open(ENV):
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            c[k.strip()] = v.strip()
    for k in ("IG_USER_ID", "PAGE_TOKEN"):
        if k not in c:
            sys.exit(f"{ENV} missing key {k}")
    return c


def call(path, data=None, method="POST"):
    url = f"{API}/{path}"
    body = urllib.parse.urlencode(data).encode() if data else None
    req = urllib.request.Request(url, data=body, method=method)
    with urllib.request.urlopen(req, timeout=120) as r:
        return json.load(r)


def split_caption(path):
    txt = open(path).read().strip()
    lines = txt.splitlines()
    cap, tags = [], []
    for ln in lines:
        (tags if ln.strip().startswith("#") else cap).append(ln)
    while cap and not cap[-1].strip():
        cap.pop()
    return "\n".join(cap).strip(), " ".join(t.strip() for t in tags if t.strip())


def create_container(c, video_url, caption):
    return call(f"{c['IG_USER_ID']}/media",
                {"media_type": "REELS", "video_url": video_url, "caption": caption,
                 "access_token": c["PAGE_TOKEN"]})["id"]


def wait_container(c, cid, timeout=600):
    t0 = time.time()
    while time.time() - t0 < timeout:
        st = call(f"{cid}?fields=status,status_code,error_message",
                  {"access_token": c["PAGE_TOKEN"]}, "GET")
        if st.get("status") == "FINISHED":
            return st
        if st.get("status") in ("ERROR", "EXPIRED"):
            sys.exit(f"container error: {st.get('error_message')}")
        time.sleep(5)
    sys.exit("container processing timed out")


def publish(c, cid):
    return call(f"{c['IG_USER_ID']}/media_publish",
                {"creation_id": cid, "access_token": c["PAGE_TOKEN"]})["id"]


def first_comment(c, media_id, text):
    return call(f"{media_id}/comments",
                {"message": text, "access_token": c["PAGE_TOKEN"]})


def do_publish(video, caption_file, comment=True):
    c = cfg()
    cap, tags = split_caption(caption_file)
    cid = create_container(c, video, cap)
    print("container:", cid, "→ processing…")
    wait_container(c, cid)
    mid = publish(c, cid)
    print("PUBLISHED media id:", mid)
    if comment and tags:
        first_comment(c, mid, tags)
        print("first comment (hashtags) posted")
    return mid


def load_queue():
    return json.load(open(QUEUE)) if os.path.exists(QUEUE) else []


def save_queue(q):
    json.dump(q, open(QUEUE, "w"), indent=1, ensure_ascii=False)


def parse_at(s):
    for fmt in ("%Y-%m-%d %H:%M", "%Y-%m-%dT%H:%M", "%Y-%m-%d %H:%M:%S"):
        try:
            return datetime.datetime.strptime(s, fmt).timestamp()
        except ValueError:
            pass
    sys.exit(f"bad --at format: {s}  (use 'YYYY-MM-DD HH:MM')")


def main():
    a = sys.argv[1:]
    if not a:
        print(__doc__)
        return
    cmd = a[0]
    if cmd == "token-info":
        c = cfg()
        me = call(f"me?fields=id,name,username&access_token={c['PAGE_TOKEN']}", None, "GET")
        print("token OK for:", me)
    elif cmd == "publish":
        d = dict(zip(a[1::2], a[2::2]))
        video, capf = d.get("--video"), d.get("--caption")
        if not video or not capf:
            sys.exit("need --video URL and --caption FILE")
        at = d.get("--at")
        if at:
            q = load_queue()
            q.append({"at": parse_at(at), "video": video, "caption": capf, "done": False})
            save_queue(q)
            print(f"queued for {at}  (run `run-due` via cron/worker)")
        else:
            do_publish(video, capf)
    elif cmd == "run-due":
        c = cfg()
        q = load_queue()
        now = time.time()
        for item in q:
            if not item.get("done") and item["at"] <= now:
                mid = do_publish(item["video"], item["caption"])
                item["done"] = True
                item["media_id"] = mid
                save_queue(q)
    elif cmd == "refresh-token":
        c = cfg()
        r = call(f"oauth/access_token?grant_type=fb_exchange_token&client_id={c.get('APP_ID','')}"
                 f"&client_secret={c.get('APP_SECRET','')}&fb_exchange_token={c['PAGE_TOKEN']}", None, "GET")
        print("new token expires in ~", r.get("expires_in", "?"), "s — update secrets/insta.env")
        print(r.get("access_token", "")[:24] + "…")
    else:
        print(__doc__)


if __name__ == "__main__":
    main()
