"""Authorization + validity gate for `/publish` issue comments.

Called by .github/workflows/publish-approved-draft.yml BEFORE anything touches
Buffer. All input comes from environment variables (set by the workflow from
the issue_comment event payload). Nothing sensitive is printed.

Env inputs
  GATE_AUTHOR_ASSOC   github.event.comment.author_association
  GATE_AUTHOR         github.event.comment.user.login   (logged only)
  GATE_BODY           github.event.comment.body
  GATE_TITLE          github.event.issue.title
  GATE_LABELS         comma-separated issue label names
  GATE_IS_PR          "1" when the issue is actually a pull request

Decision (printed as GATE: <reason>, reflected in the exit code)
   0  proceed          — valid draft issue, exact /publish, authorized author
  10  unauthorized      — author is not OWNER/MEMBER/COLLABORATOR
  11  not-a-draft       — title/label mismatch, or comment is on a PR
  12  bad-command       — comment body is not exactly /publish
  13  already-queued    — issue already carries the queued-in-buffer label
"""
import os
import re
import sys

ALLOWED_ASSOCIATIONS = {"OWNER", "MEMBER", "COLLABORATOR"}
DRAFT_TITLE_RE = re.compile(r"^trend reel draft \d{4}-\d{2}-\d{2}$")
DRAFT_LABEL = "auto-draft"
QUEUED_LABEL = "queued-in-buffer"
COMMAND = "/publish"

EXIT_PROCEED = 0
EXIT_UNAUTHORIZED = 10
EXIT_NOT_DRAFT = 11
EXIT_BAD_COMMAND = 12
EXIT_ALREADY_QUEUED = 13


def normalize_body(body):
    """/publish must be the whole comment (surrounding whitespace allowed)."""
    return " ".join((body or "").replace("\r", " ").split())


def decide(author_assoc=None, body=None, title=None, labels=None, is_pr=None):
    # read env at call time (not import time) so the workflow and tests can set it
    if author_assoc is None:
        author_assoc = os.environ.get("GATE_AUTHOR_ASSOC", "")
    if body is None:
        body = os.environ.get("GATE_BODY", "")
    if title is None:
        title = os.environ.get("GATE_TITLE", "")
    if labels is None:
        labels = os.environ.get("GATE_LABELS", "")
    if is_pr is None:
        is_pr = os.environ.get("GATE_IS_PR", "") == "1"
    label_set = {x.strip() for x in labels.split(",") if x.strip()}

    if is_pr:
        return EXIT_NOT_DRAFT, "comment is on a pull request, not a draft issue"

    if not (DRAFT_TITLE_RE.match(title or "") or DRAFT_LABEL in label_set):
        return EXIT_NOT_DRAFT, "issue is not a daily draft issue (title/label mismatch)"

    if normalize_body(body) != COMMAND:
        return EXIT_BAD_COMMAND, f"comment body is not exactly '{COMMAND}'"

    # exact match — GitHub always emits canonical UPPER-CASE associations
    if (author_assoc or "") not in ALLOWED_ASSOCIATIONS:
        # checked after the cheap structural checks, but always before any action
        return EXIT_UNAUTHORIZED, (
            f"author_association '{author_assoc}' is not one of "
            + "/".join(sorted(ALLOWED_ASSOCIATIONS)))

    if QUEUED_LABEL in label_set:
        return EXIT_ALREADY_QUEUED, f"issue already has the '{QUEUED_LABEL}' label"

    return EXIT_PROCEED, "valid /publish from an authorized author"


def main():
    code, reason = decide()
    author = os.environ.get("GATE_AUTHOR", "?")
    print(f"GATE: {reason} (comment by @{author}, association "
          f"{os.environ.get('GATE_AUTHOR_ASSOC', '?')})")
    sys.exit(code)


if __name__ == "__main__":
    main()
