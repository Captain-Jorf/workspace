"""Retention for public draft branches (drafts/<date>) — bounded, Buffer-aware, never fails a run.

  python3 build/retention.py            # dry run: print what would be deleted
  python3 build/retention.py --apply    # delete eligible remote branches

A drafts/<date> branch hosts the public MP4 that Buffer fetches at publish time, so it must stay
until Buffer has *actually* published the post. Rules (policy: content/editorial_policy.json → retention):

  * never delete a branch younger than `draft_branch_days`
  * never delete while editorial memory says the post is still waiting (`keep_if_status`, e.g.
    queued-in-buffer / scheduled / unknown) — Buffer may not have fetched the file yet
  * a branch is deleted only when its memory status is terminal (sent, error, qa-failed, *-error,
    approved-dry-run, duplicate-prevented, queue-full) or when it is older than 4× the retention window
  * legacy branches (2026-09-13/14/15, old workflow) are left alone unless older than 4× window
  * memory entries are never removed — only the big public files go away
"""
import argparse
import datetime
import os
import re
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import common  # noqa: E402

TERMINAL = {"sent", "error", "qa-failed", "approved-dry-run", "duplicate-prevented", "queue-full",
            "automation-error", "trend-error", "source-error", "script-error", "translation-error",
            "tts-error", "render-error", "buffer-error", "legacy-not-published"}


def remote_draft_branches():
    out = subprocess.run(["git", "ls-remote", "--heads", "origin", "refs/heads/drafts/*"],
                         capture_output=True, text=True, timeout=60)
    if out.returncode != 0:
        raise RuntimeError(out.stderr.strip()[:200])
    tags = []
    for line in out.stdout.splitlines():
        m = re.search(r"refs/heads/drafts/(\d{4}-\d{2}-\d{2})$", line)
        if m:
            tags.append(m.group(1))
    return sorted(tags)


def decide(tag, mem, pol, today):
    ret = pol.get("retention", {})
    days = int(ret.get("draft_branch_days", 14))
    keep_status = set(ret.get("keep_if_status", ["queued", "scheduled", "unknown"])) | {"queued-in-buffer", "sending"}
    try:
        age = (today - datetime.date.fromisoformat(tag)).days
    except ValueError:
        return False, "unparseable tag"
    if age < days:
        return False, f"younger than {days} days"
    entry = next((e for e in mem.get("entries", []) if e.get("content_id") == f"reel-{tag}"), None)
    status = (entry or {}).get("status", "unknown")
    if age >= 4 * days:
        return True, f"older than {4 * days} days (hard cap), status={status}"
    if status in keep_status:
        return False, f"status={status} — Buffer may still need the file"
    if status in TERMINAL:
        return True, f"terminal status {status}, age {age}d"
    return False, f"status={status} not terminal"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--today", default=None)
    a = ap.parse_args()
    try:
        pol = common.policy()
        mem = common.load_memory()
        today = datetime.date.fromisoformat(a.today) if a.today else datetime.datetime.now(datetime.timezone.utc).date()
        branches = remote_draft_branches()
    except Exception as e:  # noqa: BLE001
        print(f"[retention] skipped: {str(e)[:160]}")
        return 0
    deleted = 0
    for tag in branches:
        ok, why = decide(tag, mem, pol, today)
        verb = "DELETE" if ok else "keep  "
        print(f"[retention] {verb} drafts/{tag}: {why}")
        if ok and a.apply:
            r = subprocess.run(["git", "push", "-q", "origin", "--delete", f"refs/heads/drafts/{tag}"],
                               capture_output=True, text=True, timeout=60)
            if r.returncode == 0:
                deleted += 1
            else:
                print(f"[retention] could not delete drafts/{tag}: {r.stderr.strip()[:120]}")
    print(f"[retention] {len(branches)} draft branch(es), {deleted} deleted{'' if a.apply else ' (dry run)'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
