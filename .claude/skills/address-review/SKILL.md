---
name: address-review
description: Address review comments on a GitHub PR and post replies
---

# /address-review

Address review feedback on a GitHub PR. Invoked by the cw orchestrator daemon when pr.review_received is detected.

## Inputs

The PR number is passed as the first argument: `/address-review <pr-number>`

## Steps

1. **Fetch review comments**
   ```bash
   gh pr view <pr-number> --json reviews,reviewRequests
   gh api repos/{owner}/{repo}/pulls/<pr-number>/comments
   ```

2. **Checkout the PR branch**
   ```bash
   gh pr checkout <pr-number>
   ```

3. **Address each comment**
   - For each unresolved thread: read the comment, understand the concern
   - Make the appropriate code change (or reply explaining why no change is needed)
   - For substantive code changes: commit them

4. **Reply to comments**
   For each addressed thread, post a reply:
   ```bash
   gh api repos/{owner}/{repo}/pulls/<pr-number>/comments/<comment-id>/replies \
     -f body="<response>"
   ```

5. **Push**
   ```bash
   git push
   ```

## Termination

Exit after addressing all unresolved review threads. If a thread is unclear or requires a large architectural change, reply asking for clarification and mark it as deferred.

## Output

Print a summary: `Review addressed: <pr-number> — <N> comments resolved, <M> deferred`
