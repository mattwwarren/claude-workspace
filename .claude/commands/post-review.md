---
description: "Post PR review with inline comments to GitHub via the API"
argument-hint: "<PR#> [--event APPROVE|REQUEST_CHANGES|COMMENT] [--body 'summary']"
allowed-tools: ["Bash", "Glob", "Grep", "Read", "Write"]
---

# Post Review to GitHub

Post a code review with inline file comments to a GitHub PR. Replaces the fragile pattern of building JSON heredocs in bash.

**Arguments:** "$ARGUMENTS"

---

## Step 1: Parse Arguments

Extract from `$ARGUMENTS`:

| Param | Default | Notes |
|-------|---------|-------|
| PR number | _(required)_ | Bare number or `#N` |
| `--event` | `COMMENT` | `APPROVE`, `REQUEST_CHANGES`, or `COMMENT` |
| `--body` | _(auto-generated)_ | Top-level review summary |
| `--commit` | _(none)_ | Pin to specific SHA |
| `--dry-run` | false | Print payload without posting |

If no PR number is provided, abort: "Usage: /post-review <PR#>"

## Step 2: Collect Review Findings

Look in the **current conversation context** for review findings. These typically come from a prior `/review` invocation or manual analysis.

### Finding Sources

Scan backward through the conversation for the most recent review output. Findings may appear as:

1. **Structured review output** (from `/review` skill):
   ```
   ### Must Fix
   **file/path.py**
   - **[Agent]** L42: Description. → Fix.

   ### Should Fix
   ...
   ```

2. **Inline notes** you produced during manual review:
   ```
   - file.py:42 — description of issue
   ```

3. **If no findings are in context**, ask the user: "No review findings in the current conversation. Provide them, or run /review first?"

### Convert to Comment JSON

For each finding, build a JSON object:

```json
{
  "path": "src/api/handler.py",
  "line": 42,
  "body": "**[Code Quality]** Description of issue and fix suggestion."
}
```

Rules:
- `path` must be relative to repo root (no leading `/`)
- `line` is the line number in the **new version** of the file (right side of diff). It **must** appear in the PR diff — if unsure, verify with `gh pr diff <PR#> | grep -n`
- `body` supports full GitHub markdown (newlines as `\n` in JSON are fine — the script handles escaping)
- Prefix the body with `**[Agent Name]**` if the finding came from a named reviewer agent
- For multi-line comments, add `"start_line": N` for the first line of the range
- Omit `line` entirely to create a file-level comment (when the exact line is uncertain)

### Severity Mapping to Event

If `--event` was not explicitly provided, infer it:
- Any **MUST_FIX** findings → `REQUEST_CHANGES`
- Only **SHOULD_FIX** findings → `COMMENT`
- No findings → `APPROVE`

## Step 3: Build the Review Body

The body is a terse top-level summary — nothing more. Findings live in inline
comments (Step 2); never restate them in the body.

**Do not itemize the author's work back to them.** When approving a PR where the
author addressed earlier review comments, the body is a short sign-off — NOT a
per-comment changelog of what they changed. The author knows what they did;
recounting it is noise, and reads as performative accounting ("look how
thoroughly I checked"). One line is enough. The verification you did to satisfy
yourself the work is done is *yours* — keep it in your reply to the user, not in
the public review body. This holds even when you got something wrong earlier in
the session: the review body is not where you make amends or show your work.

If `--body` was not provided, auto-generate a summary:

- **Findings present:** `Found N issues (X must-fix, Y should-fix). See inline comments.`
- **Approval, no prior findings:** `No issues found. LGTM.`
- **Approval after the author addressed an earlier review:**
  `Earlier review comments addressed, CI green. LGTM.` — one line, no per-item recap.

Append the attribution footer to whatever body is used:
```
🤖 Generated with [Claude Code](https://claude.ai/code)
```

## Step 4: Write Comments to Temp File

Use the **Write tool** to create `/tmp/pr_review_comments.json` with the comments JSON array. Do NOT use bash heredocs — the Write tool handles JSON content (quotes, newlines, markdown) without shell escaping issues.

## Step 5: Post the Review

```bash
~/.claude/scripts/post_review.py <PR#> \
  --event <EVENT> \
  --body "<summary>" \
  --comments-file /tmp/pr_review_comments.json
```

Add `--commit <SHA>` if pinning to a specific commit.
Add `--dry-run` to preview the payload first.

### Interpreting Results

**Success:**
```json
{
  "success": true,
  "review_id": 12345,
  "url": "https://github.com/owner/repo/pull/123#pullrequestreview-12345",
  "comments_posted": 5,
  "comments_failed": 0
}
```

**Partial success (fallback activated):**
```json
{
  "success": true,
  "comments_posted": 4,
  "comments_failed": 1,
  "failed": [{"path": "foo.py", "line": 999, "_error": "line not in diff"}],
  "note": "Batch post failed; comments posted individually"
}
```

When comments fail (usually because a line number isn't in the diff):
1. Report which comments failed and why
2. Offer to retry failed comments as file-level comments (omit `line`)
3. Or offer to look up the correct line numbers from the diff

**Full failure:**
```json
{
  "error": true,
  "message": "..."
}
```

Common causes: bad auth, wrong repo, PR doesn't exist. Report the error to the user.

## Step 6: Clean Up and Report

```bash
rm -f /tmp/pr_review_comments.json
```

Report to the user:
- PR number and link
- Event type (approved / commented / requested changes)
- Comments posted vs failed
- Link to the review on GitHub

## Step 7: Register for Monitoring

After posting the review, register the PR so `/review-monitor` tracks it through to merge.

1. Get repo info and the **review baseline SHA** — the commit the review was
   posted against, NOT live HEAD:
   ```bash
   REPO=$(gh repo view --json nameWithOwner --jq .nameWithOwner)
   REPO_PATH=$(git rev-parse --show-toplevel)
   # commit_id of the review object we just created — authoritative.
   REVIEW_SHA=$(gh api "repos/$REPO/pulls/<PR#>/reviews/<review_id>" --jq .commit_id)
   ```

   > **Why not `gh pr view --json headRefOid`?** The monitor delta-reviews
   > `git diff <registered sha>..HEAD`. By the time you register — and *especially*
   > on any later re-registration — the author may already have pushed fix
   > commits. If you anchor to live HEAD, those fixes land *before* the baseline
   > and become invisible: the monitor reports "nothing addressed" while the
   > author addressed everything. Always anchor to the review's own `commit_id`.

2. Get our thread IDs from the review we just posted:
   ```bash
   gh review view <PR#> --json
   ```
   Filter threads where the first comment author is us and the thread was created in the last few minutes. Extract each thread's `id`, `path`, and `line`.

3. Register — `--threads` is **space-separated**, not comma-separated:
   ```bash
   ~/.claude/scripts/review_monitor.py register <PR#> \
     --role reviewer \
     --repo "$REPO" \
     --repo-path "$REPO_PATH" \
     --sha "$REVIEW_SHA" \
     --review-id <review_id> \
     --threads <id1> <id2> <id3> ... \
     --thread-details '<JSON array of {id, file, line}>'
   ```

   `register` **merges** threads (it never removes) and overwrites the baseline
   SHA with `--sha`. Do **not** `drop` + re-register to "fix" an entry — `drop`
   discards the baseline and the fresh `register` re-anchors to whatever `--sha`
   you pass. If an entry needs correcting, read its current `last_seen_sha` from
   `status --all --json` and pass that exact value back.

4. **Verify before reporting.** `register` is silent on success — do not assume
   it worked. Confirm the entry landed and is anchored correctly:
   ```bash
   ~/.claude/scripts/review_monitor.py status --all --json | python3 -c "
   import sys, json
   e = json.load(sys.stdin)['monitored']['$REPO#<PR#>']
   print('sha:', e['delta_base_sha'], '| threads:', len(e['our_threads']))"
   ```
   Confirm `sha` (the delta-review baseline) equals `REVIEW_SHA` and `threads`
   equals the number you posted.
   Only then report: "Registered PR #N for review monitoring (N threads)."

---

## Quick Reference (gh review CLI)

The `gh review` CLI extension is installed and provides a simpler alternative to the Python script. Use it as the **preferred method** for posting reviews.

```bash
# Add inline comments (each creates/appends to a pending review)
gh review comment <PR#> --file src/foo.py --line 42 --body "**[Code Quality]** Issue description"
gh review comment <PR#> --file src/bar.py --line 10 --end-line 15 --body "**[Architecture]** Multi-line issue"

# Add a code suggestion
gh review comment <PR#> --file src/foo.py --line 42 --suggestion "corrected_code_here"

# Check what's queued before submitting
gh review pending <PR#>

# Submit the review
gh review submit <PR#> --approve --body "Summary"
gh review submit <PR#> --request-changes --body "See inline comments"
gh review submit <PR#> --comment --body "Summary"

# Discard a pending review if something went wrong
gh review pending <PR#> --discard
```

### Posting a review with gh review

For each finding from Step 2, run:
```bash
gh review comment <PR#> --file <path> --line <line> --body "<body>"
```

Then submit:
```bash
gh review submit <PR#> --event-flag --body "<summary>"
```

This replaces Steps 4-5 (temp file + Python script). Comments that fail are reported immediately per-command rather than requiring batch fallback.

### Other useful commands

```bash
# View existing review threads on a PR (useful before reviewing)
gh review view <PR#> --unresolved
gh review view <PR#> --json

# Reply to an existing thread
gh review reply <thread-id> --body "Fixed in latest push"

# Resolve/unresolve threads
gh review resolve <thread-id>
gh review unresolve <thread-id>
```

---

## Quick Reference (Python script — legacy fallback)

Use the Python script when you need batch posting (all comments in a single API call) or commit pinning:

```bash
# Dry run — see what would be posted
~/.claude/scripts/post_review.py 123 --event COMMENT --body "Summary" --comments-file /tmp/comments.json --dry-run

# Post a comment review
~/.claude/scripts/post_review.py 123 --event COMMENT --body "Summary" --comments-file /tmp/comments.json

# Post approval with no inline comments
echo '[]' | ~/.claude/scripts/post_review.py 123 --event APPROVE --body "LGTM"

# Request changes
~/.claude/scripts/post_review.py 123 --event REQUEST_CHANGES --body "See inline comments" --comments-file /tmp/comments.json

# Pin to a specific commit
~/.claude/scripts/post_review.py 123 --event COMMENT --body "Summary" --comments-file /tmp/comments.json --commit abc123

# Skip fallback (fail fast)
~/.claude/scripts/post_review.py 123 --event COMMENT --body "Summary" --comments-file /tmp/comments.json --no-fallback
```
