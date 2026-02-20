# Python Development Patterns

Universal best practices for writing clean, maintainable Python code in CLI tools.

---

# Code Quality Principles

## Critical Rules - NEVER Do These

### 1. NEVER Use `noqa` or `type: ignore` Suppressions

**Always fix the root cause of linter/type warnings.** Suppressions hide problems.

**Only exception:** Security test fixtures with intentional violations need justification:
```python
test_paths = ["/etc/passwd"]  # noqa: S108 - intentional for security test
```

### 2. NEVER Create Custom Constants for Stdlib/Dependency Values

**Always check stdlib and existing dependencies BEFORE creating custom constants.**

```python
# Bad
HTTP_TOO_MANY_REQUESTS = 429
CACHE_TTL_SECONDS = 86400

# Good
from http import HTTPStatus
from datetime import timedelta

# Use HTTPStatus.TOO_MANY_REQUESTS (value is 429)
# Use timedelta(hours=24).total_seconds() for TTL
```

**Check these sources first:**
1. **Stdlib modules:** `http.HTTPStatus`, `datetime.timedelta`, `os.*`, `signal.*`
2. **Project dependencies:** Check what's already imported
3. **Only then:** Create custom constant if truly novel

### 3. NEVER Add Dependencies Without Exhausting Existing Solutions

**Before adding a new dependency, verify:**
1. Can stdlib handle this? (`json`, `datetime`, `pathlib`, `hashlib`, etc.)
2. Is functionality already in project dependencies? (`pydantic`, `click`, etc.)
3. Can existing dependency be used differently?

**New dependencies require:**
- Strong justification (stdlib/existing can't solve it)
- Maintenance status check
- License compatibility

## Keep It Simple (KISS)

**Default to the simplest solution that works:**
- Solve the immediate problem, not hypothetical future ones
- Three similar lines of code > premature abstraction
- Simple duplication > complex DRY extraction
- Local variables > state management for simple cases
- Direct code > configuration-driven for one-off uses

**Red flags for over-engineering:**
- Adding configuration for things that rarely/never change
- Creating abstractions before you have 3+ use cases
- Using design patterns without clear, demonstrable benefit
- Adding features "for later" that aren't currently requested
- Building frameworks when libraries exist

## DRY Guidelines - When to Extract Duplicate Patterns

**Pattern extraction thresholds:**
- **2 occurrences:** Consider extraction if complex (>10 lines)
- **3 occurrences:** MUST extract (no exceptions)
- **Simple patterns (<5 lines):** Can wait until 3-4 occurrences

## Prefer Built-ins and Libraries

**Default to using existing solutions:**
- Use standard library functions over custom implementations
- Use well-maintained libraries for common problems
- Don't suffer from NIH (Not Invented Here) syndrome

**When custom implementations ARE justified:**
- No suitable library exists for the specific use case
- Library is unmaintained or has known security vulnerabilities
- Library adds significant dependency weight for a trivial use case

---

# Python & Pydantic Conventions

**Strict ruff configuration patterns:**

## Type Annotations

- **All functions need return types**: Including test methods (`-> None`)
- **Avoid `Any` in annotations**: Use `object` for generic catch-all types (ANN401)
- **List variance:** `list[SubType]` cannot be assigned to `list[Union[...]]`. Use explicit annotation
- **Pydantic ValidationInfo**: Import directly from `pydantic`, not submodules
  ```python
  from pydantic import Field, ValidationInfo, field_validator

  @field_validator("field_name")
  @classmethod
  def validate_field(cls, value: str, info: ValidationInfo) -> str:
      ...
  ```

## Enums

- **Use `StrEnum` for string enums** (UP037):
  ```python
  from enum import StrEnum

  class MyEnum(StrEnum):  # Correct
      VALUE = "value"

  class MyEnum(str, Enum):  # Outdated pattern
      VALUE = "value"
  ```

## Error Handling

- **Store error messages in variables** (EM101):
  ```python
  # Good
  msg = "Invalid input"
  raise ValueError(msg)

  # Bad
  raise ValueError("Invalid input")
  ```

- **Chain exceptions properly** (TRY003):
  ```python
  try:
      UUID(value)
  except ValueError as err:
      msg = "Must be a valid UUID"
      raise ValidationError(msg) from err  # Preserve traceback
  ```

## Constants Over Magic Values

- **Extract magic numbers/strings to constants** (PLR2004):
  ```python
  # Good
  MAX_NAME_LENGTH = 80
  if len(value) > MAX_NAME_LENGTH:
      msg = f"Name must be {MAX_NAME_LENGTH} characters or less"

  # Bad
  if len(value) > 80:
      raise ValueError("Name must be 80 characters or less")
  ```

## Test Patterns

- **Combine nested context managers** (SIM117):
  ```python
  # Good - Single parenthesized with
  with (
      patch.object(service, "method1"),
      patch.object(service, "method2"),
      pytest.raises(ValueError),
  ):
      service.do_something()

  # Bad - Nested with statements
  with patch.object(service, "method1"):
      with patch.object(service, "method2"):
          with pytest.raises(ValueError):
              service.do_something()
  ```

- **Return type annotations on all test methods**:
  ```python
  def test_something(self) -> None:  # Add -> None
      ...
  ```

- **Use `object` for mock side effects** (ANN401):
  ```python
  def side_effect(*_args: object, **_kwargs: object) -> object:  # Correct
      return mock_value

  def side_effect(*_args: Any, **_kwargs: Any) -> Any:  # Wrong
      return mock_value
  ```

## Security Test Patterns

When testing security validations, use `noqa` comments for intentional violations:

```python
absolute_paths = [
    "/etc/passwd",
    "/tmp/malicious.ova",  # noqa: S108 (hardcoded /tmp path)
]

test_password = "password"  # noqa: S105 (test fixture)
```

---

# Test Architecture Principles

## Separation of Concerns

**Global fixtures should be minimal and self-contained:**
- Base fixtures provide only essential setup
- Domain-specific fixtures extend base fixtures with additional configuration
- Never add features to global fixtures if only needed by a subset of tests
- Test files can override inherited fixtures to compose the setup they need

**Example Structure:**
```python
# Global fixture - minimal setup
@pytest.fixture
def tmp_config(tmp_path):
    # Only essential setup
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    yield config_dir

# Domain-specific fixture - extends base
@pytest.fixture
def config_with_clients(tmp_config):
    # Adds domain-specific configuration
    clients_file = tmp_config / "clients.yaml"
    clients_file.write_text("client-a:\n  workspace: /tmp/a\n")
    yield tmp_config

# Test file overrides to compose requirements
@pytest.fixture
def tmp_config(config_with_clients):
    """All tests in this file need client setup."""
    return config_with_clients
```

## Fixture Isolation Problems

**Common pitfall:** Adding setup to global fixtures breaks unrelated tests.

**Symptom:** Tests that previously passed now fail because they see unexpected data.

**Solution:** Move specific setup to domain-specific fixture, use fixture override pattern.

## Public vs Private Methods

**Tests should ONLY mock/reference public APIs.**

```python
# Wrong
mocker.patch("cw.session._internal_helper")

# Correct
mocker.patch("cw.session.public_method")
```

**Why:**
- Private methods (prefix `_`) are implementation details
- Tests coupling to private methods break when refactoring internals
- Public methods are the contract - tests should validate the contract
- If you need to mock a private method, it should probably be public or refactored

---

# Test Mocking Best Practices

## Mocking Instance Methods

```python
# Wrong - Requires type: ignore suppression
obj._method = mock_func  # type: ignore[method-assign]

# Correct - No suppression needed
mocker.patch.object(obj, "_method", mock_func)
```

**Why:** pytest-mock's `patch.object()` properly handles the mock assignment while respecting the type system.

## Unused Test Parameters

```python
# Wrong
def helper(param: str) -> None:  # noqa: ARG002

# Correct
def helper(_param: str) -> None:  # Underscore prefix signals intentionally unused
```

## Exception Handler Testing

**NEVER mock inside exception handlers. Mock the SOURCE of the exception, test the handler.**

```python
# Wrong - Mocks the fallback inside except block
mocker.patch.object(client, "fetch", side_effect=Error())
mocker.patch.object(cache, "get_stale", return_value=data)  # Don't mock this!

# Correct - Only mock external service, test actual handler
def test_fallback(tmp_path):
    # Put real data in state file
    state_file = tmp_path / "state.json"
    state_file.write_text('{"key": "cached"}')

    # Mock only external operation that fails
    with patch.object(client, "fetch", side_effect=Error()):
        result = service.operation()  # Handler runs for real
    assert result == "cached"  # Verify fallback worked
```

## When Bare Exception Catches Are Acceptable

**Valid Pattern:** Non-critical operations that must not interrupt critical paths

```python
try:
    self._save_checkpoint(...)  # Best-effort, non-critical
except Exception:  # noqa: BLE001 - Checkpoint saves are non-critical, operation must continue
    LOGGER.warning("Failed to save checkpoint", exc_info=True)
```

**Requirements for bare Exception catches:**
1. **Clear justification** in comment explaining WHY bare catch is necessary
2. **Tests verify resilience** - operation continues despite failure
3. **Logged with full context** using `exc_info=True` for debugging
4. **Non-critical operation** - failure doesn't compromise core functionality

---

# Shared Constants Location

**Constants that multiple modules need:**

```python
# Correct - Shared location
# src/cw/constants.py (or within the relevant module)
MAX_SESSION_NAME_LENGTH = 64
DEFAULT_POLL_INTERVAL = 30
STATE_FILE_VERSION = 2

# Both CLI and session modules import from here
from cw.constants import MAX_SESSION_NAME_LENGTH
```

**Guideline:**
- Configuration constants - Constants module or domain-specific module
- Business constants - Module where the logic lives
- CLI constants - `cli.py` (command names, help text)

---

# Suppression Strategy

## Default: Fix Root Cause, Never Suppress

**The Rule:** Suppressions hide problems. Always fix the root cause first.

When you encounter a linter/type checker warning:
1. **First:** Understand what the tool is warning about
2. **Second:** Fix the code to eliminate the warning
3. **Last Resort:** If truly impossible to fix, add suppression with justification

## Valid Suppression Scenarios

Only add suppressions when ALL these conditions are met:
- Intentional design decision (not a workaround)
- Clear justification comment explaining WHY
- Tests verify the behavior being suppressed
- No way to fix the root cause

---

This is free and unencumbered software released into the public domain.

For more information, please refer to <http://unlicense.org/>
