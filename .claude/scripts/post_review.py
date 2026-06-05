#!/usr/bin/env python3
"""Post a PR review with inline comments to GitHub via gh api.

Replaces the fragile pattern of building JSON heredocs in bash. Handles:
- Proper JSON escaping (newlines, quotes, markdown in comment bodies)
- Validation of comment structure before posting
- Fallback: if batch post fails (usually bad line numbers), retries
  comments individually and reports which ones failed
- Dry-run mode for debugging payloads

Usage:
  # Post from stdin
  echo '[{"path":"foo.py","line":42,"body":"issue here"}]' | python3 post_review.py 123 --event COMMENT --body "Summary"

  # Post from file
  python3 post_review.py 123 --event REQUEST_CHANGES --body "Found issues" --comments-file /tmp/comments.json

  # Dry run (print payload, don't post)
  python3 post_review.py 123 --event COMMENT --body "Summary" --comments-file /tmp/comments.json --dry-run

Comment JSON format (array of objects):
  - path (required): file path relative to repo root
  - body (required): comment text (markdown OK, newlines OK)
  - line (recommended): line number in the NEW version of the file
  - side: LEFT or RIGHT (default RIGHT)
  - start_line: for multi-line comments, first line of range
  - start_side: side for start_line (default matches side)
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path
from typing import Any


def get_repo_nwo() -> str:
    """Get owner/repo from gh CLI."""
    result = subprocess.run(
        ["gh", "repo", "view", "--json", "nameWithOwner", "-q", ".nameWithOwner"],
        capture_output=True,
        text=True,
        check=True,
    )
    return result.stdout.strip()


def gh_api_post(endpoint: str, payload: dict[str, Any]) -> dict[str, Any]:
    """POST JSON to a GitHub API endpoint via gh api."""
    result = subprocess.run(
        ["gh", "api", endpoint, "--method", "POST", "--input", "-"],
        input=json.dumps(payload),
        capture_output=True,
        text=True,
    )

    if result.returncode != 0:
        # Try to parse structured error
        error_body = result.stdout or result.stderr
        try:
            err = json.loads(error_body)
            return {"error": True, "status": result.returncode, "message": err.get("message", ""), "details": err}
        except json.JSONDecodeError:
            return {"error": True, "status": result.returncode, "message": error_body.strip()}

    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError:
        return {"success": True, "raw": result.stdout}


def validate_comment(c: dict[str, Any]) -> tuple[dict[str, Any] | None, str | None]:
    """Validate and normalize a single comment. Returns (normalized, error)."""
    if "path" not in c:
        return None, "missing 'path'"
    if "body" not in c:
        return None, "missing 'body'"

    comment: dict[str, Any] = {
        "path": c["path"],
        "body": c["body"],
    }

    if "line" in c:
        comment["line"] = int(c["line"])
        comment["side"] = c.get("side", "RIGHT")

        if "start_line" in c:
            comment["start_line"] = int(c["start_line"])
            comment["start_side"] = c.get("start_side", comment["side"])
    else:
        # No line number → file-level comment
        comment["subject_type"] = "file"

    return comment, None


def validate_comments(raw: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Validate all comments. Returns (valid, invalid)."""
    valid = []
    invalid = []

    for c in raw:
        normalized, error = validate_comment(c)
        if normalized:
            valid.append(normalized)
        else:
            invalid.append({**c, "_error": error})

    return valid, invalid


def post_review(
    repo: str,
    pr_number: int,
    event: str,
    body: str,
    comments: list[dict[str, Any]],
    commit_id: str | None = None,
) -> dict[str, Any]:
    """Post a review. Returns the API response."""
    payload: dict[str, Any] = {
        "event": event,
        "body": body,
    }

    if comments:
        payload["comments"] = comments

    if commit_id:
        payload["commit_id"] = commit_id

    return gh_api_post(f"repos/{repo}/pulls/{pr_number}/reviews", payload)


def post_with_fallback(
    repo: str,
    pr_number: int,
    event: str,
    body: str,
    comments: list[dict[str, Any]],
    commit_id: str | None = None,
) -> dict[str, Any]:
    """Post review with fallback for bad comments.

    Strategy:
    1. Try posting the full review with all comments
    2. If that fails (usually a bad line number), post body-only review
       then add each comment individually as a standalone review comment
    3. Report which comments posted and which failed
    """
    # Attempt 1: full review
    result = post_review(repo, pr_number, event, body, comments, commit_id)
    if not result.get("error"):
        return {
            "success": True,
            "review_id": result.get("id"),
            "url": result.get("html_url", ""),
            "comments_posted": len(comments),
            "comments_failed": 0,
            "failed": [],
        }

    if not comments:
        # No comments to fall back on — the review itself failed
        return result

    batch_error = result.get("message", "")
    print(f"Batch post failed: {batch_error}", file=sys.stderr)
    print("Falling back to individual comment posting...", file=sys.stderr)

    # Attempt 2: post body-only review
    body_result = post_review(repo, pr_number, event, body, [], commit_id)
    if body_result.get("error"):
        return {
            "error": True,
            "message": f"Even body-only review failed: {body_result.get('message', '')}",
            "batch_error": batch_error,
        }

    review_url = body_result.get("html_url", "")

    # Attempt 3: add comments individually as standalone review comments
    posted = []
    failed = []

    for comment in comments:
        payload: dict[str, Any] = {
            "body": comment["body"],
            "path": comment["path"],
        }

        if "line" in comment:
            payload["line"] = comment["line"]
            payload["side"] = comment.get("side", "RIGHT")
            if "start_line" in comment:
                payload["start_line"] = comment["start_line"]
                payload["start_side"] = comment.get("start_side", payload["side"])
        else:
            payload["subject_type"] = "file"

        if commit_id:
            payload["commit_id"] = commit_id

        cr = gh_api_post(f"repos/{repo}/pulls/{pr_number}/comments", payload)
        if cr.get("error"):
            failed.append({**comment, "_error": cr.get("message", "unknown")})
            print(f"  FAILED: {comment['path']}:{comment.get('line', 'file')} — {cr.get('message', '')}", file=sys.stderr)
        else:
            posted.append(comment)
            print(f"  OK: {comment['path']}:{comment.get('line', 'file')}", file=sys.stderr)

    return {
        "success": True,
        "review_id": body_result.get("id"),
        "url": review_url,
        "comments_posted": len(posted),
        "comments_failed": len(failed),
        "failed": failed,
        "note": "Batch post failed; comments posted individually (not grouped in review)",
    }


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Post a PR review with inline comments to GitHub",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("pr_number", type=int, help="PR number")
    parser.add_argument("--event", choices=["APPROVE", "REQUEST_CHANGES", "COMMENT"],
                        default="COMMENT", help="Review event type (default: COMMENT)")
    parser.add_argument("--body", default="", help="Top-level review body text")
    parser.add_argument("--body-file", type=Path, help="Read review body from file instead of --body")
    parser.add_argument("--comments-file", type=Path, help="Read comments JSON from file (default: stdin)")
    parser.add_argument("--commit", help="Pin review to a specific commit SHA")
    parser.add_argument("--repo", help="owner/repo (default: auto-detect from current directory)")
    parser.add_argument("--dry-run", action="store_true", help="Print payload as JSON without posting")
    parser.add_argument("--no-fallback", action="store_true",
                        help="Don't retry individual comments on batch failure")

    args = parser.parse_args()

    # Resolve body
    body = args.body
    if args.body_file:
        body = args.body_file.read_text().strip()

    # Read comments
    if args.comments_file:
        raw_json = args.comments_file.read_text()
    elif not sys.stdin.isatty():
        raw_json = sys.stdin.read()
    else:
        raw_json = "[]"

    try:
        comments_input = json.loads(raw_json)
    except json.JSONDecodeError as e:
        print(json.dumps({"error": True, "message": f"Invalid JSON: {e}"}))
        sys.exit(1)

    # Accept {"comments": [...]} or bare [...]
    if isinstance(comments_input, dict):
        comments_input = comments_input.get("comments", [])

    valid, invalid = validate_comments(comments_input)

    if invalid:
        print(f"Skipped {len(invalid)} invalid comments:", file=sys.stderr)
        for inv in invalid:
            print(f"  {inv.get('path', '?')}:{inv.get('line', '?')} — {inv.get('_error', '?')}", file=sys.stderr)

    # Resolve repo
    repo = args.repo or get_repo_nwo()

    if args.dry_run:
        payload: dict[str, Any] = {"event": args.event, "body": body}
        if valid:
            payload["comments"] = valid
        if args.commit:
            payload["commit_id"] = args.commit
        print(json.dumps({"repo": repo, "pr": args.pr_number, "payload": payload}, indent=2))
        if invalid:
            print(f"\n# {len(invalid)} comments would be skipped", file=sys.stderr)
        return

    # Post
    if args.no_fallback:
        result = post_review(repo, args.pr_number, args.event, body, valid, args.commit)
        if not result.get("error"):
            result = {
                "success": True,
                "review_id": result.get("id"),
                "url": result.get("html_url", ""),
                "comments_posted": len(valid),
                "comments_failed": len(invalid),
            }
    else:
        result = post_with_fallback(repo, args.pr_number, args.event, body, valid, args.commit)

    print(json.dumps(result, indent=2))

    if result.get("error"):
        sys.exit(1)


if __name__ == "__main__":
    main()
