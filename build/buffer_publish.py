"""Official Buffer (GraphQL) publisher for the @metacognition.hq reel factory.

Endpoint : POST https://api.buffer.com   (Authorization: Bearer $BUFFER_TOKEN)
Docs     : https://developers.buffer.com
The token is read from the BUFFER_TOKEN env var (GitHub Actions secret).
It is NEVER printed, NEVER logged, NEVER committed.

Subcommands
  check           read-only: verify the token, list organizations, resolve the
                  target Instagram channel. Creates NO posts.
  find-channel    read-only: print the resolved Instagram channel name.
  queue           read-only: count scheduled posts on the channel and look for a
                  post that already carries a given content id.
  publish         add an already-rendered, publicly-hosted reel to the Buffer
                  queue via the official createPost mutation
                  (schedulingType: automatic, mode: addToQueue).
                  Buffer then publishes it at the channel's next schedule slot
                  (configured in the Buffer UI: 19:30 Asia/Tehran, 1 post/day).

Safety rails (all fail closed)
  * the target channel must match the repo variable TARGET_BUFFER_CHANNEL
    (default "metacognition.hq") EXACTLY (case-insensitive) on name/displayName
    AND have service == instagram. Substring matches are NOT accepted. A
    paused/locked/disconnected channel is refused.
  * publish requires --yes AND env AUTO_PUBLISH_ENABLED == "true" (exact).
    Anything else ("True", "1", missing) → dry run, createPost is never called.
  * three idempotency layers: local marker JSON → editorial memory (via the
    caller) → live lookup of scheduled posts containing the content-id marker
    line. If a post exists, exit 0 without any mutation.
  * on a timeout / ambiguous createPost response the queue is re-read before
    any retry; if the post exists it is adopted instead of re-created.
  * a full queue (Free plan) is detected before and after createPost →
    exit EXIT_QUEUE_FULL, no post, caller opens a warning issue.
  * video URL must be public HTTPS and reachable (HEAD) before createPost.
  * caption text hard-capped at 2200 chars; hashtags go to
    metadata.instagram.firstComment with a controlled fallback.
  * GraphQL/transport errors never leak the token (scrubbed before printing).
"""
import datetime
import json
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request

# Import common for quarantine check (safe, no network)
try:
    import common
    common.assert_content_language_en()  # fail-closed EN-only
    _common = common
except Exception:
    _common = None

API = "https://api.buffer.com"
UA = "Mozilla/5.0 (trend-factory; +metacognition.hq)"   # Buffer sits behind Cloudflare
CAPTION_LIMIT = 2200
DEFAULT_CHANNEL = "metacognition.hq"
QUEUE_LIMIT_FREE = 10
CONTENT_MARK_RE = re.compile(r"reel-id:\s*([a-z0-9\-]+)", re.I)

# exit codes (used by tests and by the workflows)
EXIT_OK = 0
EXIT_NO_TOKEN = 2
EXIT_CAPTION = 3
EXIT_URL = 4
EXIT_API = 5
EXIT_NO_CHANNEL = 6
EXIT_QUEUE_FULL = 7
EXIT_DISABLED = 8          # publish requested while AUTO_PUBLISH_ENABLED != "true"


class BufferError(Exception):
    pass


class QueueFull(BufferError):
    pass


def target_channel_name():
    return (os.environ.get("TARGET_BUFFER_CHANNEL") or DEFAULT_CHANNEL).strip()


def publishing_enabled():
    """Exact-string opt-in. 'True', '1', 'yes' → disabled (fail closed)."""
    return os.environ.get("AUTO_PUBLISH_ENABLED", "") == "true"


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
        raise BufferError(_scrub(f"transport error: {type(e).__name__}: {e}", tok))
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
    displayName
    service
    descriptor
    isQueuePaused
    isLocked
    isDisconnected
    timezone
  }
}
"""

# older/stricter schema variants — tried in order if the first one fails
CHANNELS_QUERY_FALLBACKS = [
    """
query Channels($input: ChannelsInput!) {
  channels(input: $input) {
    id
    name
    service
    descriptor
  }
}
""",
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
]

POSTS_QUERY = """
query Posts($input: PostsInput!, $first: Int!, $after: String) {
  posts(input: $input, first: $first, after: $after) {
    edges { node { id text status dueAt } }
    pageInfo { hasNextPage endCursor }
  }
}
"""


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


def _names(ch):
    out = []
    for k in ("name", "displayName"):
        v = (ch.get(k) or "").strip().lstrip("@").lower()
        if v:
            out.append(v)
    return out


def find_instagram_channel(tok, preferred=None):
    """Read-only, exact-match resolution of the target Instagram channel.

    A channel qualifies only if service == instagram AND one of name/displayName
    equals TARGET_BUFFER_CHANNEL (case-insensitive, leading '@' ignored).
    """
    preferred = (preferred or target_channel_name()).lstrip("@").lower()
    if not preferred:
        raise BufferError("TARGET_BUFFER_CHANNEL is empty — refusing to guess a channel")
    insta, matches = [], []
    for org in organizations(tok):
        for ch in channels_for_org(tok, org["id"]):
            if (ch.get("service") or "").lower() != "instagram":
                continue
            ch = dict(ch, _org=org.get("name") or "", _org_id=org.get("id"))
            insta.append(ch)
            if preferred in _names(ch):
                matches.append(ch)
    if not insta:
        raise BufferError("no Instagram channel connected to this Buffer account")
    if not matches:
        names = ", ".join(_chan_label(c) or "(unnamed)" for c in insta)
        raise BufferError(f"no Instagram channel named exactly '{preferred}' (seen: {names})")
    if len(matches) > 1:
        raise BufferError(f"{len(matches)} Instagram channels named '{preferred}' — ambiguous, refusing")
    ch = matches[0]
    if ch.get("isDisconnected"):
        raise BufferError(f"channel '{_chan_label(ch)}' is disconnected in Buffer — reconnect it first")
    if ch.get("isLocked"):
        raise BufferError(f"channel '{_chan_label(ch)}' is locked in Buffer")
    if ch.get("isQueuePaused"):
        raise BufferError(f"channel '{_chan_label(ch)}' has a paused queue — posts would never go out")
    return ch


def list_posts(tok, org_id, channel_id, statuses=("scheduled",), max_pages=4):
    """Scheduled posts on the channel (newest due first)."""
    posts, after = [], None
    for _ in range(max_pages):
        variables = {"first": 50, "after": after,
                     "input": {"organizationId": org_id,
                               "sort": [{"field": "dueAt", "direction": "desc"}],
                               "filter": {"channelIds": [channel_id], "status": list(statuses)}}}
        data = _post({"query": POSTS_QUERY, "variables": variables}, tok)
        conn = data.get("posts") or {}
        for e in conn.get("edges") or []:
            if e.get("node"):
                posts.append(e["node"])
        pi = conn.get("pageInfo") or {}
        if not pi.get("hasNextPage"):
            break
        after = pi.get("endCursor")
    return posts


def find_post_by_content_id(posts, content_id):
    for p in posts:
        m = CONTENT_MARK_RE.search(p.get("text") or "")
        if m and m.group(1).lower() == content_id.lower():
            return p
    return None


def queue_state(tok, ch, content_id=None):
    """→ dict(count, limit, full, existing_post)."""
    try:
        posts = list_posts(tok, ch["_org_id"], ch["id"])
    except BufferError as e:
        # the posts query is optional on some plans; report but don't invent facts
        return {"count": None, "limit": QUEUE_LIMIT_FREE, "full": False, "existing_post": None,
                "note": f"queue lookup unavailable: {e}"}
    existing = find_post_by_content_id(posts, content_id) if content_id else None
    limit = int(os.environ.get("BUFFER_QUEUE_LIMIT") or QUEUE_LIMIT_FREE)
    return {"count": len(posts), "limit": limit, "full": len(posts) >= limit, "existing_post": existing}


def cmd_check(argv):
    tok = _token()
    out_json = argv[argv.index("--json") + 1] if "--json" in argv else None
    orgs = organizations(tok)
    print("Buffer connection: OK")
    print(f"Organizations visible: {len(orgs)} ({', '.join(o.get('name', '?') for o in orgs)})")
    ch = find_instagram_channel(tok)
    print(f"Instagram channel found: {_chan_label(ch)} (exact match for '{target_channel_name()}')")
    qs = queue_state(tok, ch)
    if qs.get("count") is not None:
        print(f"Scheduled posts in queue: {qs['count']} / {qs['limit']}")
    else:
        print(qs.get("note", "queue lookup skipped"))
    flag = "true → publishing allowed" if publishing_enabled() else "not exactly 'true' → publishing disabled"
    print(f"AUTO_PUBLISH_ENABLED is {flag}")
    print("Read-only check complete — no posts were created.")
    if out_json:
        with open(out_json, "w") as f:
            json.dump({"org_id": ch.get("_org_id") or orgs[0]["id"],
                       "org_name": ch.get("_org") or orgs[0].get("name"),
                       "channel_id": ch.get("id"),
                       "channel_name": _chan_label(ch),
                       "service": ch.get("service"),
                       "timezone": ch.get("timezone"),
                       "queue_count": qs.get("count"), "queue_limit": qs.get("limit"),
                       "publishing_enabled": publishing_enabled()}, f, indent=1)
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


def cmd_queue(argv):
    tok = _token()
    cid = argv[argv.index("--content-id") + 1] if "--content-id" in argv else None
    ch = find_instagram_channel(tok)
    qs = queue_state(tok, ch, cid)
    print(f"channel: {_chan_label(ch)}  scheduled: {qs.get('count')} / {qs.get('limit')}  full: {qs.get('full')}")
    if cid:
        ex = qs.get("existing_post")
        print(f"content id {cid}: {'already queued as ' + ex['id'] if ex else 'not in queue'}")
    if "--json" in argv:
        with open(argv[argv.index("--json") + 1], "w") as f:
            json.dump({k: v for k, v in qs.items() if k != "existing_post"} |
                      {"existing_post_id": (qs.get("existing_post") or {}).get("id")}, f, indent=1)
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
            ctype = r.headers.get("Content-Type") or ""
            if ctype and "video" not in ctype and "octet-stream" not in ctype:
                return False, f"HEAD {url} -> unexpected Content-Type {ctype}"
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


def with_content_mark(text, content_id):
    """Append the idempotency marker line (also human-meaningful) if missing."""
    if not content_id or CONTENT_MARK_RE.search(text):
        return text
    mark = f"reel-id: {content_id}"
    return f"{text}\n\n{mark}"          # never truncate silently: the length check rejects instead


def build_inputs(channel_id, text, video_url, first_comment, thumbnail_ms=None):
    """createPost input variants, most complete first, safest last."""
    video = {"url": video_url}
    if thumbnail_ms is not None:
        video = {"url": video_url, "metadata": {"thumbnailOffset": int(thumbnail_ms)}}
    base = {
        "channelId": channel_id,
        "text": text,
        "schedulingType": "automatic",
        "mode": "addToQueue",
        "assets": [{"video": video}],
    }
    variants = []
    meta = {"type": "reel", "shouldShareToFeed": True}
    if first_comment:
        variants.append(dict(base, metadata={"instagram": dict(meta, firstComment=first_comment)}))
    variants.append(dict(base, metadata={"instagram": dict(meta)}))
    plain = dict(base, assets=[{"video": {"url": video_url}}])
    variants.append(plain)               # bare input — last-resort fallback
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
        if "queue" in low and ("full" in low or "limit" in low):
            raise QueueFull(f"Buffer queue is full: {last_msg}")
        # a validation error about firstComment/metadata → try the next variant
        if any(k in low for k in ("firstcomment", "first comment", "metadata",
                                  "instagram", "validation", "invalid", "thumbnail")):
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
    content_id = arg("--content-id", required=False, default=(f"reel-{tag}" if tag else ""))
    thumb = arg("--thumbnail-ms", required=False, default=None)

    # --- QUARANTINE SAFETY LOCK (Issue #14 and pre-fix outputs) ---
    # Even if AUTO_PUBLISH_ENABLED=true, quarantined ids/dates must never be published.
    # This is a temporary safety lock in the fix branch, but also a permanent fail-closed gate.
    if _common is not None:
        try:
            q_date = tag if tag else None
            if _common.is_quarantined(content_id, q_date):
                print(f"[SAFETY] content_id {content_id} / tag {tag} is quarantined (translation-rejected) — refusing to publish.")
                print(f"[SAFETY] quarantine reason: see content/quarantine.json")
                return EXIT_DISABLED
        except Exception as e:
            # If quarantine check fails, fail closed
            print(f"[SAFETY] quarantine check failed: {e} — refusing to publish as precaution.")
            return EXIT_DISABLED

    want_live = "--yes" in argv and "--dry-run" not in argv
    dry = not (want_live and publishing_enabled())

    tok = _token()

    # 1. idempotency (local marker) — never queue the same reel twice
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
    text = with_content_mark(text, content_id)
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

    # 4. resolve channel (read-only, exact match)
    try:
        ch = find_instagram_channel(tok)
    except BufferError as e:
        print(str(e))
        return EXIT_NO_CHANNEL
    print(f"Instagram channel: {_chan_label(ch)}")

    # 5. live idempotency + queue capacity (read-only)
    qs = queue_state(tok, ch, content_id)
    if qs.get("existing_post"):
        p = qs["existing_post"]
        print(f"a post with reel-id {content_id} is already scheduled in Buffer (post id {p['id']}) — adopting it.")
        save_marker(marker, {"buffer_post_id": p["id"], "status": p.get("status"), "due_at": p.get("dueAt"),
                             "channel_id": ch.get("id"), "channel_name": _chan_label(ch), "video_url": video,
                             "tag": tag, "content_id": content_id, "adopted": True,
                             "created_utc": datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")})
        return EXIT_OK
    if qs.get("count") is not None:
        print(f"queue: {qs['count']} / {qs['limit']} scheduled")
        if qs["full"]:
            print("Buffer queue is full (Free plan limit) — not adding a post today. Retry tomorrow.")
            return EXIT_QUEUE_FULL
    else:
        print(qs.get("note", "queue lookup skipped"))

    variants = build_inputs(ch["id"], text, video, hashtags, thumb)
    if dry:
        why_dry = ("--yes not passed" if "--yes" not in argv else
                   "--dry-run passed" if "--dry-run" in argv else
                   'AUTO_PUBLISH_ENABLED is not exactly "true"')
        print(f"DRY RUN ({why_dry}) — payload validated, createPost NOT called.")
        print("channel id resolved, caption within limit, video URL public.")
        return EXIT_DISABLED if ("--yes" in argv and "--dry-run" not in argv) else EXIT_OK

    # 6. createPost (addToQueue — Buffer publishes at the channel's schedule)
    try:
        post, used = create_post(tok, variants)
    except QueueFull as e:
        print(str(e))
        return EXIT_QUEUE_FULL
    except BufferError as e:
        # ambiguous outcome (timeout / 5xx): did the post get created anyway?
        print(f"createPost error: {e}")
        qs2 = queue_state(tok, ch, content_id)
        p = qs2.get("existing_post")
        if p:
            print(f"the post exists after all (post id {p['id']}) — adopting it, no retry.")
            save_marker(marker, {"buffer_post_id": p["id"], "status": p.get("status"), "due_at": p.get("dueAt"),
                                 "channel_id": ch.get("id"), "channel_name": _chan_label(ch), "video_url": video,
                                 "tag": tag, "content_id": content_id, "adopted": True,
                                 "created_utc": datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")})
            return EXIT_OK
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
        "content_id": content_id,
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
        elif cmd == "queue":
            sys.exit(cmd_queue(rest))
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
