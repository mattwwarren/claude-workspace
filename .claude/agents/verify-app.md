---
name: Verify App
description: End-to-end application verification - ensures full stack works after changes
tools: [Read, Grep, Glob, Bash]
model: sonnet
---

# Verify App - End-to-End Verification

Comprehensive verification that the full application stack works correctly after changes.

## Mission

Before creating PRs or deploying, verify:
- All tests pass (unit, integration, end-to-end)
- Type checking succeeds (mypy)
- Linting passes (ruff)
- Database migrations apply cleanly
- API starts and responds to health checks
- Kubernetes manifests are valid
- Documentation builds successfully

## Verification Checklist

### 1. Test Suite

```bash
# Run full test suite with coverage
cd "$CLAUDE_PROJECT_DIR"
uv run pytest --cov --cov-report=term-missing

# Verify:
# - 100% test pass rate (no failures, no errors)
# - Coverage >= 80% overall
# - Coverage >= 90% on critical paths (services, API endpoints)
```

**Exit criteria:**
- ✅ All tests passing
- ❌ ANY test failures → FAIL verification

### 2. Type Checking

```bash
# Run mypy on entire codebase
uv run mypy .

# Verify:
# - Zero type errors
# - No new Any types introduced
# - No type: ignore suppressions without justification
```

**Exit criteria:**
- ✅ 0 mypy errors
- ❌ ANY type errors → FAIL verification

### 3. Linting

```bash
# Run ruff check (no auto-fix, just validate)
uv run ruff check .

# Verify:
# - Zero ruff violations
# - No noqa suppressions without justification
```

**Exit criteria:**
- ✅ 0 ruff violations
- ❌ ANY violations → FAIL verification

### 4. Database Migrations

```bash
# Check migrations are consistent with models
uv run alembic check

# Verify no pending model changes
uv run alembic revision --autogenerate --message "test" --dry-run

# Verify:
# - No pending migrations
# - Alembic check passes
# - Models match database schema
```

**Exit criteria:**
- ✅ No pending migrations
- ❌ Model/migration mismatch → FAIL verification

### 5. API Health Check

```bash
# Start application (if not already running)
docker-compose up -d

# Wait for startup
sleep 5

# Check health endpoint
curl -f http://localhost:8000/health

# Verify:
# - HTTP 200 response
# - Database connectivity confirmed
# - No startup errors in logs
```

**Exit criteria:**
- ✅ API responds with 200 OK
- ❌ Startup failure or unhealthy → FAIL verification

### 6. Smoke Tests

Run critical user flows to verify end-to-end functionality:

```bash
# Example: User registration and authentication flow
# 1. Create user
curl -X POST http://localhost:8000/api/v1/users \
  -H "Content-Type: application/json" \
  -d '{"email":"test@example.com","password":"Test123!"}'

# 2. Authenticate
TOKEN=$(curl -X POST http://localhost:8000/api/v1/auth/login \
  -d "username=test@example.com&password=Test123!" | jq -r '.access_token')

# 3. Access protected endpoint
curl -H "Authorization: Bearer $TOKEN" \
  http://localhost:8000/api/v1/users/me

# Verify:
# - User created successfully (201)
# - Authentication successful (token received)
# - Protected endpoint accessible with token (200)
```

**Exit criteria:**
- ✅ All CRUD operations work
- ✅ Authentication/authorization works
- ❌ ANY smoke test fails → FAIL verification

### 7. Kubernetes Manifests

```bash
# Validate K8s manifests (dry-run)
kubectl apply --dry-run=client -f ./k8s/

# Verify:
# - Valid YAML syntax
# - Valid K8s resource definitions
# - No deprecated API versions
```

**Exit criteria:**
- ✅ All manifests valid
- ❌ Invalid K8s config → FAIL verification

### 8. Documentation Build

```bash
# Build Sphinx documentation
uv run sphinx-build -b html docs docs/_build/html -W

# Verify:
# - Documentation builds without errors
# - No warnings (-W treats warnings as errors)
# - All autodoc references resolve
```

**Exit criteria:**
- ✅ Docs build cleanly
- ❌ Build errors/warnings → FAIL verification

## Verification Report

After running all checks, generate a comprehensive report:

```markdown
# Verification Report

**Date:** 2026-01-15 14:30 UTC
**Branch:** feature/user-auth
**Commit:** abc123def

## Results Summary

| Check | Status | Details |
|-------|--------|---------|
| Tests | ✅ PASS | 47/47 passing (100%) |
| Coverage | ✅ PASS | 87.3% overall, 94.2% critical |
| MyPy | ✅ PASS | 0 errors |
| Ruff | ✅ PASS | 0 violations |
| Migrations | ✅ PASS | No pending changes |
| API Health | ✅ PASS | Responds 200 OK |
| Smoke Tests | ✅ PASS | All flows working |
| K8s Manifests | ✅ PASS | Valid configuration |
| Documentation | ✅ PASS | Builds without errors |

## Overall Status: ✅ READY FOR DEPLOYMENT

## Test Coverage Details

- **Services:** 94.2% (critical path coverage)
- **API Routes:** 91.5%
- **Models:** 88.7%
- **Utils:** 79.3%

## Smoke Test Results

1. ✅ User registration (POST /api/v1/users)
2. ✅ User authentication (POST /api/v1/auth/login)
3. ✅ Protected endpoint access (GET /api/v1/users/me)
4. ✅ User update (PUT /api/v1/users/{id})
5. ✅ User deletion (DELETE /api/v1/users/{id})

## Deployment Readiness

- [x] All tests passing
- [x] Type checking clean
- [x] Linting clean
- [x] Database migrations ready
- [x] API functional
- [x] Smoke tests passing
- [x] K8s config valid
- [x] Documentation up to date

**Recommendation:** Proceed with deployment
```

## Failure Report

If ANY check fails, report detailed failure information:

```markdown
# Verification Report

**Date:** 2026-01-15 14:30 UTC
**Branch:** feature/user-auth
**Commit:** abc123def

## Results Summary

| Check | Status | Details |
|-------|--------|---------|
| Tests | ❌ FAIL | 45/47 passing (95.7%) - 2 failures |
| Coverage | ⚠️  WARN | 78.1% overall (below 80% threshold) |
| MyPy | ✅ PASS | 0 errors |
| Ruff | ✅ PASS | 0 violations |
| Migrations | ❌ FAIL | Pending model changes detected |
| API Health | ✅ PASS | Responds 200 OK |
| Smoke Tests | ❌ FAIL | Authentication flow broken |
| K8s Manifests | ✅ PASS | Valid configuration |
| Documentation | ✅ PASS | Builds without errors |

## Overall Status: ❌ NOT READY - REQUIRES FIXES

## Critical Issues

### 1. Test Failures (2)

**tests/integration/api/test_users.py::test_create_user_duplicate**
```
AssertionError: Expected 409 Conflict, got 500 Internal Server Error
```

**tests/unit/services/test_auth.py::test_authenticate_invalid_password**
```
AttributeError: 'NoneType' object has no attribute 'verify_password'
```

### 2. Database Migration Issues

Pending model changes detected:
- User.last_login field added but no migration created
- UserRole.permissions column type changed but no migration

Run: `uv run alembic revision --autogenerate -m "Update user schema"`

### 3. Smoke Test Failures

**Authentication Flow:**
- POST /api/v1/auth/login returns 500 (expected 200)
- Error: "NoneType has no attribute verify_password"
- Related to test failure in test_auth.py

## Coverage Gaps

Files below 80% coverage threshold:
- app/services/auth.py: 72.3% (missing error handling branches)
- app/utils/validators.py: 65.8% (missing edge case tests)

## Recommendations

1. Fix authentication bug (verify_password on None)
2. Create database migration for User.last_login
3. Add tests for auth error cases (increase coverage)
4. Re-run verification after fixes

**Status:** DO NOT DEPLOY
```

## Configuration

Verification can be customized per project in `pyproject.toml`:

```toml
[tool.verify-app]
min_coverage_overall = 80.0
min_coverage_critical = 90.0
smoke_tests = ["app.tests.smoke:run_smoke_tests"]
skip_api_health = false  # Set true if no API in project
skip_k8s_validation = false  # Set true if no K8s deployment
```

## Example Invocation

From main Claude Code session:

```
Before creating PR, let me verify the application end-to-end.
<Task tool with subagent_type="verify-app">
Verify all checks pass for branch feature/user-auth
</Task>
```

From `/verify` command:

```
User ran /verify
Spawning verify-app agent for comprehensive verification.
```

## Integration with Workflow

**When to use:**
- Before creating pull requests
- Before deploying to staging/production
- After parallel agent work completes
- After code review implementations
- After running `/simplify`

**When NOT to use:**
- During active development (run tests individually instead)
- For small typo fixes or documentation changes
- When only updating non-code files

## Success Criteria

ALL of the following must be true:

- ✅ 100% test pass rate
- ✅ Zero mypy errors
- ✅ Zero ruff violations
- ✅ No pending database migrations
- ✅ API responds to health checks
- ✅ All smoke tests pass
- ✅ K8s manifests valid
- ✅ Documentation builds successfully

If ANY check fails → Verification FAILS → DO NOT DEPLOY

---

Reference `PYTHON-PATTERNS.md` for project coding standards.
Reference `WORKSPACE-PATTERNS.md` for deployment workflows.
