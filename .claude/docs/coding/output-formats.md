# Standard Review Output Formats

## Quick Review Format

For small PRs (<5 files, <200 lines):

### If no issues:

```markdown
# Code Review: [PR Title]

## Status
✅ No issues found. Ready to merge.

- Ruff: ✅ 0 violations
- MyPy: ✅ 0 errors
- Tests: ✅ X/X passing
```

### If issues found:

```markdown
# Code Review: [PR Title]

## Findings

### [Issue Title]
- **File**: `path/to/file.py:123`
- **Problem**: [What is wrong]
- **Fix**: [Specific action]

## Status
- ❌ Ruff: X violations
- ✅ MyPy: 0 errors
```

## Standard Review Format

For medium PRs (5-20 files):

```markdown
# Code Review: [PR Title]

## Findings by Severity

### 🚨 Critical Issues (Must Fix)

#### [Issue Title]
- **File**: `path/to/file.py:123`
- **Problem**: [Description]
- **Impact**: [Why it matters]
- **Fix**: [Specific action]

### ⚠️ Major Concerns (Should Fix)

#### [Issue Title]
- **File**: `path/to/file.py:456`
- **Problem**: [Description]
- **Impact**: [Why it matters]
- **Fix**: [Action required]

### ℹ️ Low Priority (Nice to Fix)

- **[Area]**: [Recommendation]

## Summary

- **Files reviewed**: N
- **Findings**: X critical, Y major, Z low
- **Tests**: X/Y passing
- **Status**: 🚨 BLOCKED / ⚠️ NEEDS CHANGES / ✅ READY
```

## Thorough Review Format

For complex PRs (20+ files):

```markdown
# Code Review: [Feature/PR Title]

## Review Scope

- **Files**: N files, ~X lines changed
- **Focus**: [security, architecture, performance]
- **Risk**: [low, medium, high]

## Architecture Review

### Design Decisions
- [Key changes]

### Concerns
- [Issues]

## Findings by Severity

[Same structure as Standard Review]

## Summary

| Aspect | Status | Details |
|--------|--------|---------|
| **Linting** | ✅/❌ | X violations |
| **Types** | ✅/❌ | X errors |
| **Tests** | ✅/❌ | X/Y passing |
| **Coverage** | ✅/⚠️ | X% |

**Status**: 🚨 BLOCKED / ⚠️ CHANGES NEEDED / ✅ READY
```

## Severity Levels

- **🚨 Critical**: Must fix (security, correctness, test failures)
- **⚠️ Major**: Should fix (performance, maintainability)
- **ℹ️ Low**: Nice to fix (style, minor improvements)

---

**Key Principle:** Only report actionable problems. No praise, summaries, or fluff.
