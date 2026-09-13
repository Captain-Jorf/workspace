"""Official Buffer (GraphQL) publisher for the @metacognition.hq reel factory.

Endpoint : POST https://api.buffer.com   (Authorization: Bearer $BUFFER_TOKEN)
Docs     : https://developers.buffer.com
The token is read from the BUFFER_TOKEN env var (GitHub Actions secret).
It is NEVER printed, NEVER logged, NEVER committed.

Subcommands
  check           read-only: verify the token, list organizations, find the
                  Instagram channel (metacognition.hq). Creates NO posts.
  find-channel    read-only: print the resolved Instagram channel name.
  publish         add an already-rendered, publicly-hosted reel to the Buffer
                  queue via the official createPost mutation
                  (schedulingType: automatic, mode: addToQueue).
                  Buffer then publishes it at the channel's next schedule slot
                  (configured in the Buffer UI: 19:30 Asia/Tehran, 1 post/day).

Safety rails
  * publish refuses to run unless --yes is passed explicitly.
  * publish is idempotent: a marker JSON file records the Buffer post id; if
    the marker exists the script exits 0 without any API mutation.
  * the video URL must be public HTTPS and reachable (HEAD check) before any
    createPost call — Buffer fetches the video from that URL itself.
  * caption text is hard-capped at 2200 chars (Instagram limit).
  * hashtags go into metadata.instagram.firstComment; if the API rejects the
    first comment (or the metadata block) we retry with a safe fallback.
  * GraphQL/transport errors never leak the token (it is scrubbed from every
    message before printing).

Usage
  python3 build/buffer_publish.py check [--json out.json]
  python3 build/buffer_publish.py find-channel [--json out.json]
  python3 build/buffer_publish.py publish --tag 2026-09-13 \
      --video https://raw.githubusercontent.com/OWNER/REPO/drafts/TAG/output/auto-TAG.mp4 \
      --caption output/auto-TAG_caption.txt \
      --marker output/auto-TAG.buffer.json --yes
"""
import datetime
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request

API = "https://api.buffer.com"
UA = "Mozilla/5.0 (trend-factory; +metacognition.hq)"   # Buffer sits behind Cloudflare
CAPTION_LIMIT = 2200
PREFERRED_CHANNEL = "metacognition.hq"

# exit codes (used by tests and by the workflows)
EXIT_OK = 0
EXIT_NO_TOKEN = 2
EXIT_CAPTION = 3
EXIT_URL = 4
EXIT_API = 5
EXIT_NO_CHANNEL = 6


class BufferError(Exception):
    pass


def _token():
    tok = os.environ.get("BUFFER_TOKEN", "").strip()
    if not tok:
        print("BUFFER_TOKEN is not set — refusing to touch the Buffer API.")
        sys.exit(EXIT_NO_TOKEN)
    return tok


def _scrub(msg, tok):
    """Remove any accidental occurrence of the token from a message."""
    msg = str(msg)
    if tok:
        msg = msg.replace(tok, "***")
    return msg


def _post(payload, tok, timeout=60):
    """One GraphQL request. Returns the parsed 'data' dict; raises BufferError."""
    req = urllib.request.Request(
        API,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json",
                 "Authorization": f"Bearer {tok}",
                 "User-Agent": UA},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            body = json.load(r)
    except urllib.error.HTTPError as e:
        detail = ""
        try:
            detail = e.read().decode("utf-8", "replace")[:400]
        except Exception:
            pass
        raise BufferError(_scrub(f"HTTP {e.code} from api.buffer.com: {detail}", tok))
    except Exception as e:                                   # noqa: BLE001
        raise BufferError(_scrub(f"transport error: {e}", tok))
    if body.get("errors"):
        msgs = "; ".join(_scrub(x.get("message", "?"), tok) for x in body["errors"])
        raise BufferError(f"GraphQL error: {msgs}")
    return body.get("data") or {}


# ---------------------------------------------------------------- read-only

ACCOUNT_QUERY = """
query Account {
  account {
    id
    organizations { id name }
  }
}
"""

CHANNELS_QUERY = """
query Channels($input: ChannelsInput!) {
  channels(input: $input) {
    id
    name
    service
    descriptor
  }
}
"""

# older/stricter schema variants — tried in order if the first one fails
CHANNELS_QUERY_FALLBACKS = [
    """
query Channels($input: ChannelsInput!) {
  channels(input: $input) {
    id
    displayName
    service
    descriptor
  }
}
""",
    """
query Channels($input: ChannelsInput!) {
  channels(input: $input) {
    id
    service
    descriptor
  }
}
""",
]


def organizations(tok):
    data = _post({"query": ACCOUNT_QUERY}, tok)
    orgs = (data.get("account") or {}).get("organizations") or []
    if not orgs:
        raise BufferError("no organizations visible to this Buffer token")
    return orgs


def channels_for_org(tok, org_id):
    variables = {"input": {"organizationId": org_id}}
    last = None
    for q in [CHANNELS_QUERY] + CHANNELS_QUERY_FALLBACKS:
        try:
            data = _post({"query": q, "variables": variables}, tok)
            return data.get("channels") or []
        except BufferError as e:
            last = e
    raise BufferError(f"channels query failed: {last}")


def _chan_label(ch):
    return ch.get("name") or ch.get("displayName") or ch.get("descriptor") or ""


def find_instagram_channel(tok, preferred=PREFERRED_CHANNEL):
    """Read-only search across all orgs for the Instagram channel."""
    found = []
    orgs = organizations(tok)
    for org in orgs:
        for ch in channels_for_org(tok, org["id"]):
            svc = (ch.get("service") or "").lower()
            if svc == "instagram":
                ch = dict(ch, _org=org.get("name") or "", _org_id=org.get("id"))
                found.append(ch)
    if not found:
        raise BufferError("no Instagram channel connected to this Buffer account")
    pref = preferred.lower()
    for ch in found:
        hay = " ".join(str(ch.get(k) or "") for k in
                       ("name", "displayName", "descriptor", "externalLink")).lower()
        if pref in hay:
            return ch
    if len(found) == 1:
        return found[0]
    names = ", ".join(_chan_label(c) for c in found)
    raise BufferError(f"several Instagram channels found ({names}); "
                      f"none matches '{preferred}'")


def cmd_check(argv):
    tok = _token()
    out_json = argv[argv.index("--json") + 1] if "--json" in argv else None
    orgs = organizations(tok)
    print("Buffer connection: OK")
    print(f"Organizations visible: {len(orgs)} ({', '.join(o.get('name', '?') for o in orgs)})")
    ch = find_instagram_channel(tok)
    print(f"Instagram channel found: {_chan_label(ch)}")
    print("Read-only check complete — no posts were created.")
    if out_json:
        with open(out_json, "w") as f:
            json.dump({"org_id": ch.get("_org_id") or orgs[0]["id"],
                       "org_name": ch.get("_org") or orgs[0].get("name"),
                       "channel_id": ch.get("id"),
                       "channel_name": _chan_label(ch),
                       "service": ch.get("service")}, f, indent=1)
        print(f"channel metadata written to {out_json}")
    return EXIT_OK


def cmd_find_channel(argv):
    tok = _token()
    ch = find_instagram_channel(tok)
    print(f"Instagram channel found: {_chan_label(ch)}")
    if "--json" in argv:
        with open(argv[argv.index("--json") + 1], "w") as f:
            json.dump({"channel_id": ch.get("id"),
                       "channel_name": _chan_label(ch),
                       "service": ch.get("service")}, f, indent=1)
    return EXIT_OK


# ---------------------------------------------------------------- publishing

CREATE_POST_QUERY = """
mutation CreatePost($input: CreatePostInput!) {
  createPost(input: $input) {
    ... on PostActionSuccess {
      post { id text status dueAt }
    }
    ... on MutationError { message }
  }
}
"""


def split_caption(path):
    """Caption file = body text + trailing hashtag line(s) starting with '#'."""
    with open(path, encoding="utf-8") as f:
        txt = f.read().strip()
    cap, tags = [], []
    for ln in txt.splitlines():
        (tags if ln.strip().startswith("#") else cap).append(ln)
    while cap and not cap[-1].strip():
        cap.pop()
    return "\n".join(cap).strip(), " ".join(t.strip() for t in tags if t.strip())


def url_is_public(url, timeout=30):
    """HEAD-check a candidate video URL: must be public HTTPS and reachable."""
    if not url.startswith("https://"):
        return False, "video URL must be https://"
    host = (urllib.parse.urlparse(url).hostname or "").lower()
    for bad in ("localhost", "127.0.0.1", "0.0.0.0", "::1", ".local", ".internal"):
        if host == bad or host.endswith(bad):
            return False, f"video URL host '{host}' is not public"
    req = urllib.request.Request(url, method="HEAD", headers={"User-Agent": UA})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            if r.status != 200:
                return False, f"HEAD {url} -> HTTP {r.status}"
            length = r.headers.get("Content-Length")
            return True, f"HTTP 200, {length or '?'} bytes"
    except urllib.error.HTTPError as e:
        return False, f"HEAD {url} -> HTTP {e.code} (not publicly reachable)"
    except Exception as e:                                   # noqa: BLE001
        return False, f"HEAD {url} -> {e}"


def load_marker(path):
    if path and os.path.exists(path):
        try:
            with open(path, encoding="utf-8") as f:
                return json.load(f)
        except Exception:                                    # noqa: BLE001
            return None
    return None


def save_marker(path, data):
    if not path:
        return
    os.makedirs(os.path.dirname(os.path.abspath(path)) or ".", exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=1, ensure_ascii=False)


def build_inputs(channel_id, text, video_url, first_comment):
    """createPost input variants, most complete first, safest last."""
    base = {
        "channelId": channel_id,
        "text": text,
        "schedulingType": "automatic",
        "mode": "addToQueue",
        "assets": [{"video": {"url": video_url}}],
    }
    variants = []
    meta = {"type": "reel", "shouldShareToFeed": True}
    if first_comment:
        variants.append(dict(base, metadata={"instagram": dict(meta, firstComment=first_comment)}))
    variants.append(dict(base, metadata={"instagram": dict(meta)}))
    variants.append(dict(base))          # bare input — last-resort fallback
    return variants


def create_post(tok, variants):
    """Try createPost with each input variant; returns (post, variant_index)."""
    last_msg = "unknown error"
    for i, inp in enumerate(variants):
        data = _post({"query": CREATE_POST_QUERY,
                      "variables": {"input": inp}}, tok)
        res = data.get("createPost") or {}
        if res.get("post"):
            return res["post"], i
        last_msg = res.get("message") or json.dumps(res)[:300]
        low = last_msg.lower()
        # a validation error about firstComment/metadata → try the next variant
        if any(k in low for k in ("firstcomment", "first comment", "metadata",
                                  "instagram", "validation", "invalid")):
            continue
        raise BufferError(f"createPost failed: {last_msg}")
    raise BufferError(f"createPost failed with all input variants: {last_msg}")


def cmd_publish(argv):
    def arg(name, required=True, default=None):
        if name in argv:
            return argv[argv.index(name) + 1]
        if required:
            raise SystemExit(f"publish: missing {name}")
        return default

    tag = arg("--tag", required=False, default="")
    video = arg("--video")
    caption_file = arg("--caption")
    marker = arg("--marker", required=False, default=None)
    dry = "--dry-run" in argv or "--yes" not in argv

    tok = _token()

    # 1. idempotency — never queue the same reel twice
    existing = load_marker(marker)
    if existing and existing.get("buffer_post_id"):
        print(f"already queued in Buffer (post id {existing['buffer_post_id']}, "
              f"due {existing.get('due_at', '?')}) — nothing to do.")
        return EXIT_OK

    # 2. caption
    if not os.path.exists(caption_file):
        print(f"caption file not found: {caption_file}")
        return EXIT_CAPTION
    text, hashtags = split_caption(caption_file)
    if not text:
        print("caption text is empty")
        return EXIT_CAPTION
    if len(text) > CAPTION_LIMIT:
        print(f"caption is {len(text)} chars > Instagram limit {CAPTION_LIMIT}")
        return EXIT_CAPTION
    print(f"caption: {len(text)} chars (limit {CAPTION_LIMIT}); "
          f"hashtags: {len(hashtags.split()) if hashtags else 0}")

    # 3. public video URL
    ok, why = url_is_public(video)
    if not ok:
        print(f"video URL check failed: {why}")
        return EXIT_URL
    print(f"video URL is public: {why}")

    # 4. resolve channel (read-only)
    try:
        ch = find_instagram_channel(tok)
    except BufferError as e:
        print(str(e))
        return EXIT_NO_CHANNEL
    print(f"Instagram channel: {_chan_label(ch)}")

    variants = build_inputs(ch["id"], text, video, hashtags)
    if dry:
        print("DRY RUN — payload validated, createPost NOT called.")
        print("channel id resolved, caption within limit, video URL public.")
        return EXIT_OK

    # 5. createPost (addToQueue — Buffer publishes at the channel's schedule)
    try:
        post, used = create_post(tok, variants)
    except BufferError as e:
        print(str(e))
        return EXIT_API

    first_comment_used = bool(hashtags) and used == 0
    if hashtags and not first_comment_used:
        print("note: first comment (hashtags) was rejected by the API — "
              "post was queued without it (safe fallback).")
    marker_data = {
        "buffer_post_id": post.get("id"),
        "status": post.get("status"),
        "due_at": post.get("dueAt"),
        "channel_id": ch.get("id"),
        "channel_name": _chan_label(ch),
        "video_url": video,
        "tag": tag,
        "first_comment_used": first_comment_used,
        "input_variant": used,
        "created_utc": datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds"),
    }
    save_marker(marker, marker_data)
    print(f"Queued in Buffer: post id {post.get('id')} "
          f"(status {post.get('status')}, due {post.get('dueAt') or 'next schedule slot'})")
    print("Buffer will publish it to Instagram at the channel's next 19:30 "
          "Asia/Tehran queue slot.")
    if marker:
        print(f"idempotency marker written: {marker}")
    return EXIT_OK


def main():
    a = sys.argv[1:]
    cmd = a[0] if a else ""
    rest = a[1:]
    try:
        if cmd == "check":
            sys.exit(cmd_check(rest))
        elif cmd == "find-channel":
            sys.exit(cmd_find_channel(rest))
        elif cmd == "publish":
            sys.exit(cmd_publish(rest))
        else:
            print(__doc__)
            sys.exit(EXIT_OK if cmd in ("-h", "--help") else 1)
    except BufferError as e:
        print(str(e))
        sys.exit(EXIT_API)
    except SystemExit as e:
        if isinstance(e.code, int):
            raise
        print(e.code)
        sys.exit(EXIT_API)


if __name__ == "__main__":
    main()
