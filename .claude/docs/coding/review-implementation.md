# Implementing Review Feedback

Guidelines for implementing code review findings efficiently and correctly.

## Before Starting

1. **Read the ENTIRE review** - Don't start implementing piecemeal
2. **Count all items** - Know how many findings need addressing
3. **Identify blockers** - Critical issues first
4. **Ask clarifying questions** - If anything is unclear

## Implementation Protocol

### 1. Complete Reading Required

- Read full review document
- Look for CRITICAL/BLOCKER markers
- Count total findings (explicitly state the count)
- Group by category (security, performance, tests, etc.)

### 2. Clarifying Questions First

**Ask the user:**
- Scope confirmation (implement all or subset?)
- Resource constraints (database access, API keys, etc.)
- Dependencies on other work
- Priority if time-limited

**Example:**
> "I found 12 review items:
> - 3 critical (security, type errors)
> - 5 major (performance, tests)
> - 4 low (style, naming)
>
> Should I implement all 12, or focus on critical/major?"

### 3. Implementation Standards

**Complete ALL review items** unless user explicitly approves skipping some.

**Prioritize:**
1. CRITICAL/BLOCKER items first
2. Major concerns second
3. Low priority last

**Never declare "ready to commit" unless:**
- 100% test pass rate (not 24/25, not 95%)
- Zero linting violations
- Zero type errors
- All CRITICAL items resolved
- All requested items completed

### 4. Verification Steps

After implementation:

```bash
# 1. Run linting (must be zero violations)
ruff check .

# 2. Run type checking (must be zero errors)
mypy .

# 3. Run full test suite (must be 100% passing)
pytest

# 4. Check coverage (if review mentioned it)
pytest --cov=app
```

**If ANY check fails:** Not ready. Fix and re-verify.

### 5. Communication Standards

**Be brutally honest:**

✅ **Good:**
- "24 of 25 tests passing - fixing last failure"
- "Ruff still shows 2 violations in module X"
- "Skipped item #7 per your approval"

❌ **Bad:**
- "Tests mostly passing" (how many?)
- "Just a few lint issues" (zero is required)
- "Ready to commit" (when 1 test fails)
- "Good enough" (not a valid status)

### 6. Tracking Progress

Use clear status markers:

```markdown
## Review Item Status

### Item 1: Add type hints to user_service.py
Status: ✅ Complete
Files: user_service.py:45-120
Tests: Added test_user_service_type_safety.py

### Item 2: Fix N+1 query in get_orders
Status: 🔄 In Progress
Issue: Need to confirm eager loading strategy

### Item 3: Missing error handling in payment flow
Status: ⏳ Pending
Blocked: Waiting for Stripe API key
```

## Common Mistakes to Avoid

### ❌ Incomplete Implementation

**Wrong:**
> "Fixed most of the review items. There are still 2 type errors but they're minor."

**Right:**
> "Implemented 10 of 12 items. Items #5 and #8 still need work:
> - #5: Type error in async function - working on fix
> - #8: Missing test - writing now
>
> Current status: 23/25 tests passing, 2 type errors remaining."

### ❌ Minimizing Failures

**Wrong:**
> "Just one small test failure, otherwise ready to go!"

**Right:**
> "1 test failing: test_create_user_with_duplicate_email. Debugging now. Not ready to commit."

### ❌ Skipping Verification

**Wrong:**
> "Made all the changes, should be good now."

**Right:**
> "Completed all 12 review items. Running verification:
> - Ruff: ✅ 0 violations
> - MyPy: ✅ 0 errors
> - Tests: ✅ 25/25 passing
> - Coverage: ✅ 87%
>
> Ready to commit."

## Test Failure Response

If tests fail after implementing review feedback:

1. **Don't declare success** - Not ready if tests fail
2. **Identify root cause** - Why did implementation break tests?
3. **Fix the issue** - Update implementation or tests (correctly)
4. **Re-run full suite** - Verify fix doesn't break other tests
5. **Report honestly** - "Fixed, now X/X passing"

## Integration After Parallel Work

If review findings were addressed by parallel agents:

1. **Wait for ALL agents to complete**
2. **Run integration tests immediately**
3. **Run mypy on integrated code** (agents may have conflicts)
4. **Check for API contract breaks** (one agent's change → another's failure)
5. **Verify no duplicate/conflicting fixes**

**Lesson:** Agent completion ≠ task completion. Integration verification is mandatory.

## Definition of "Done"

A review item is "done" when:

- ✅ Code change implemented as specified
- ✅ Tests added/updated as needed
- ✅ All tests passing (100%)
- ✅ No new linting violations
- ✅ No new type errors
- ✅ Changes committed with clear message

**Not done if:**
- ❌ Tests still failing
- ❌ Linting violations remain
- ❌ Type errors present
- ❌ Implementation incomplete
- ❌ Can't explain what was changed

---

**Golden Rule:** All review items get implemented. All tests pass. Zero violations. No exceptions.
