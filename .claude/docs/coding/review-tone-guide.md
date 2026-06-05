# Review Tone & Style Guide

## Core Principles

1. **Actionable Only** - Every finding must have a clear fix
2. **No Praise** - Don't say "well done", "good job", etc.
3. **No Summaries** - Don't say "overall" or "in general"
4. **No Fluff** - Get straight to the point
5. **Specific** - Reference exact files and line numbers

## What to Include

✅ **DO include:**
- Specific file paths and line numbers
- Clear problem descriptions
- Concrete fixes
- Impact/why it matters
- Code examples (before/after)

❌ **DON'T include:**
- Praise or compliments
- General assessments ("code is mostly good")
- Subjective opinions without rationale
- Apologies or hedging ("maybe", "might want to consider")
- Repeated explanations of severity levels

## Language Guidelines

### Good:

- "Missing error handling at line 123"
- "Type annotation required"
- "Fix: Add try/except block"
- "Impact: API will return 500 on invalid input"

### Bad:

- "Great job on this PR!"
- "Overall, the code looks good but..."
- "I think you might want to consider..."
- "It would be nice if..."
- "Just a minor nitpick..."

## Severity Language

### Critical (🚨):

- "Must fix before merge"
- "Blocker"
- "Security vulnerability"
- "Will cause runtime error"

### Major (⚠️):

- "Should fix"
- "Affects maintainability"
- "Performance concern"
- "Technical debt"

### Low (ℹ️):

- "Nice to fix"
- "Minor improvement"
- "Style inconsistency"
- "Future consideration"

## Example Findings

### Good Finding:

```markdown
### Missing Null Check

- **File**: `app/services/user.py:123`
- **Problem**: `user.email` accessed without checking if `user` is None
- **Impact**: Will raise AttributeError if user not found
- **Fix**:
```python
if user is None:
    raise NotFoundError("User not found")
return user.email
```
```

### Bad Finding:

```markdown
### Email Handling

Great work on the user service! I noticed that the email handling is mostly good, but you might want to consider adding a null check, just in case. It's not a huge deal, but it would be nice to have for safety. Overall though, nice implementation!
```

## No-Finding Reviews

If no issues found:

```markdown
# Code Review: [PR Title]

## Status

✅ No issues found. Code passes all checks.

- Ruff: ✅ 0 violations
- MyPy: ✅ 0 errors
- Tests: ✅ X/X passing

Ready to merge.
```

Don't add praise like "excellent work" or "perfect code".

---

**Remember:** Reviews are technical specifications, not performance evaluations.
