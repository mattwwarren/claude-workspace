# Testing Philosophy & Best Practices

Comprehensive testing guidelines for both backend (Python/pytest) and frontend (TypeScript/Vitest) testing. Reference this when reviewing test quality.

## Core Testing Principles

### 1. Test Behavior, Not Implementation

**Good - Testing behavior:**
```python
def test_user_can_login_with_valid_credentials():
    # Test what the user experiences
    response = await client.post("/login", json={"email": "user@example.com", "password": "secret"})
    assert response.status_code == 200
    assert "access_token" in response.json()
```

**Bad - Testing implementation:**
```python
def test_login_calls_authenticate_method():
    # Testing internal implementation details
    with patch.object(AuthService, 'authenticate') as mock_auth:
        await login_endpoint(credentials)
        mock_auth.assert_called_once()  # ❌ Testing how it works, not what it does
```

**Why:** Implementation can change while behavior stays the same. Tests should survive refactoring.

### 2. Write Tests That Could Actually Fail

**Good - Can fail if code breaks:**
```python
def test_cannot_delete_user_with_active_sessions():
    user = create_user_with_sessions()

    with pytest.raises(ConflictError):
        await user_service.delete_user(user.id)

    # Verify user still exists
    assert await user_service.get_user(user.id) is not None
```

**Bad - Always passes:**
```python
def test_delete_user():
    with patch.object(UserService, 'delete_user', return_value=True):
        result = await user_service.delete_user(1)  # ❌ Mocking what we're testing!
        assert result is True  # ❌ Always passes
```

**Why:** Tests that can't fail provide false confidence.

### 3. One Logical Concept Per Test

**Good - Single concept:**
```python
def test_user_creation_generates_unique_id():
    user = await user_service.create(UserCreate(email="test@example.com"))
    assert user.id is not None
    assert isinstance(user.id, int)

def test_user_creation_hashes_password():
    plain_password = "secret123"
    user = await user_service.create(UserCreate(email="test@example.com", password=plain_password))
    assert user.password != plain_password
    assert user.password.startswith("$2b$")  # bcrypt prefix
```

**Bad - Multiple concepts:**
```python
def test_create_user():
    user = await user_service.create(UserCreate(email="test@example.com", password="secret"))
    # ❌ Testing ID generation
    assert user.id is not None
    # ❌ Testing password hashing
    assert user.password != "secret"
    # ❌ Testing email storage
    assert user.email == "test@example.com"
    # ❌ Testing database persistence
    db_user = await db.get(User, user.id)
    assert db_user is not None
    # Too many things - hard to tell what failed
```

**Why:** When test fails, you want to know exactly what broke.

### 4. Arrange-Act-Assert (AAA) Pattern

**Always structure tests clearly:**

```python
async def test_user_cannot_access_other_users_data():
    # Arrange - Set up test data and preconditions
    user_a = await create_user(email="alice@example.com")
    user_b = await create_user(email="bob@example.com")
    user_b_data = await create_user_data(user_b.id, content="private")

    # Act - Perform the action being tested
    with pytest.raises(ForbiddenError):
        await data_service.get_data(user_b_data.id, requesting_user=user_a)

    # Assert - Verify the expected outcome
    # (implicit in the raises check, but could add more)
```

**Use blank lines** to separate sections. Add comments for complex setup.

### 5. Test Independence

**Good - Independent tests:**
```python
class TestUserService:
    @pytest.fixture
    async def user(self):
        # Fresh user for each test
        return await create_user()

    async def test_update_name(self, user):
        updated = await user_service.update(user.id, name="New Name")
        assert updated.name == "New Name"

    async def test_update_email(self, user):
        # Separate user, no dependency on previous test
        updated = await user_service.update(user.id, email="new@example.com")
        assert updated.email == "new@example.com"
```

**Bad - Dependent tests:**
```python
class TestUserService:
    user = None  # ❌ Shared state

    def test_create_user(self):
        self.user = create_user()  # ❌ Test 1 creates
        assert self.user.id is not None

    def test_update_user(self):
        # ❌ Depends on test_create_user running first
        updated = update_user(self.user.id, name="New")
        assert updated.name == "New"
```

**Why:** Tests should be runnable in any order, in parallel, or individually.

## Test Types and Strategy

### Test Pyramid

```
        /\
       /E2E\        Few - Slow - Full system
      /------\
     /  Inte  \     Some - Medium - With database/external services
    /----------\
   / Unit Tests \   Many - Fast - Isolated components
  /--------------\
```

### Backend Test Strategy

**Unit Tests (80% of tests):**
- Services, utilities, business logic
- All dependencies mocked
- Fast (<10ms each)
- Test edge cases thoroughly

**Integration Tests (15% of tests):**
- API endpoints with real database
- Provider resources with mocked external APIs
- Test transactions, migrations
- Medium speed (~100ms each)

**E2E Tests (5% of tests):**
- Critical user flows
- Real database, real services (or staging)
- Slow (seconds each)
- Smoke tests for deployment validation

### Frontend Test Strategy

**Component Unit Tests (70% of tests):**
- Components in isolation
- Mocked props, callbacks
- Test rendering, user interactions
- Fast

**Hook Tests (15% of tests):**
- Custom hooks with `renderHook`
- Test state changes, side effects
- Isolated from components

**Integration Tests (10% of tests):**
- Multiple components together
- With React Query, routing
- Test data flow

**E2E Tests (5% of tests):**
- Critical flows with Playwright/Cypress
- Real API calls (or mocked API)
- Slow

## Backend Testing Patterns (Python/pytest)

### Fixtures Best Practices

```python
# conftest.py - Shared fixtures
@pytest.fixture
async def db_session() -> AsyncGenerator[AsyncSession, None]:
    """Provide a clean database session for each test."""
    async with async_session_maker() as session:
        yield session
        await session.rollback()  # Clean up

@pytest.fixture
async def authenticated_client(client, db_session) -> AsyncClient:
    """Provide an authenticated API client."""
    user = await create_test_user(db_session)
    token = create_access_token(user.id)
    client.headers["Authorization"] = f"Bearer {token}"
    return client

# test_users.py - Use fixtures
async def test_get_user_profile(authenticated_client, db_session):
    response = await authenticated_client.get("/users/me")
    assert response.status_code == 200
```

**Fixture Scope:**
- `scope="function"` (default): New fixture per test
- `scope="class"`: Shared across test class
- `scope="module"`: Shared across file
- `scope="session"`: Shared across all tests

**Use function scope** unless there's a performance reason not to.

### Async Testing

```python
# Mark async tests
@pytest.mark.asyncio
async def test_async_operation():
    result = await async_function()
    assert result is not None

# Async fixtures
@pytest.fixture
async def async_resource():
    resource = await setup_resource()
    yield resource
    await cleanup_resource(resource)
```

### Mocking External Services

```python
# Good - Mock at the boundary
async def test_fetch_vm_from_azure(mocker):
    # Mock the external API client
    mock_client = mocker.patch("sigma.providers.azure.compute_client.ComputeManagementClient")
    mock_client.return_value.virtual_machines.get.return_value = Mock(
        id="vm-123",
        name="test-vm",
        location="eastus"
    )

    # Test our code
    vm = await azure_provider.get_virtual_machine("vm-123")
    assert vm.name == "test-vm"

# Use pytest-vcr for HTTP recording/replay
@pytest.mark.vcr()
async def test_real_api_call():
    # First run: makes real request and records
    # Subsequent runs: replays from cassette
    result = await external_api.call()
    assert result.status == "success"
```

### Exception Testing

```python
# Test that specific exception is raised
async def test_user_not_found_raises_404():
    with pytest.raises(NotFoundError) as exc_info:
        await user_service.get_user(999999)

    assert "User not found" in str(exc_info.value)
    assert exc_info.value.status_code == 404

# Test that exception is NOT raised
async def test_valid_operation_succeeds():
    # No exception should be raised
    result = await user_service.create_user(valid_data)
    assert result.id is not None
```

### Parametrized Tests

```python
# Test multiple inputs efficiently
@pytest.mark.parametrize("email,expected_valid", [
    ("user@example.com", True),
    ("user@subdomain.example.com", True),
    ("user", False),
    ("@example.com", False),
    ("user@", False),
    ("", False),
])
def test_email_validation(email, expected_valid):
    assert validate_email(email) == expected_valid

# Parametrize with complex objects
@pytest.mark.parametrize("status,expected_next_states", [
    (Status.PENDING, [Status.APPROVED, Status.REJECTED]),
    (Status.APPROVED, [Status.ACTIVE]),
    (Status.REJECTED, []),
])
def test_valid_state_transitions(status, expected_next_states):
    assert get_valid_transitions(status) == expected_next_states
```

## Frontend Testing Patterns (TypeScript/Vitest)

### Component Testing

```typescript
import { render, screen, fireEvent, waitFor } from '@testing-library/react'
import { describe, it, expect, vi } from 'vitest'
import UserProfile from './UserProfile'

describe('UserProfile', () => {
  it('displays user information', () => {
    // Arrange
    const user = { id: 1, name: 'John Doe', email: 'john@example.com' }

    // Act
    render(<UserProfile user={user} />)

    // Assert
    expect(screen.getByText('John Doe')).toBeInTheDocument()
    expect(screen.getByText('john@example.com')).toBeInTheDocument()
  })

  it('calls onEdit when edit button clicked', async () => {
    // Arrange
    const user = { id: 1, name: 'John Doe', email: 'john@example.com' }
    const onEdit = vi.fn()
    render(<UserProfile user={user} onEdit={onEdit} />)

    // Act
    const editButton = screen.getByRole('button', { name: /edit/i })
    await fireEvent.click(editButton)

    // Assert
    expect(onEdit).toHaveBeenCalledWith(user)
  })

  it('shows loading state while fetching', () => {
    render(<UserProfile userId={1} loading={true} />)

    expect(screen.getByText(/loading/i)).toBeInTheDocument()
    expect(screen.queryByText('John Doe')).not.toBeInTheDocument()
  })

  it('shows error message on fetch failure', () => {
    const error = 'Failed to load user'
    render(<UserProfile userId={1} error={error} />)

    expect(screen.getByText(error)).toBeInTheDocument()
  })
})
```

### Hook Testing

```typescript
import { renderHook, waitFor } from '@testing-library/react'
import { describe, it, expect } from 'vitest'
import { useUserData } from './useUserData'

describe('useUserData', () => {
  it('fetches user data on mount', async () => {
    const { result } = renderHook(() => useUserData(1))

    // Initially loading
    expect(result.current.isLoading).toBe(true)

    // Wait for data
    await waitFor(() => {
      expect(result.current.isLoading).toBe(false)
    })

    expect(result.current.data).toEqual({ id: 1, name: 'John' })
  })

  it('refetches data when id changes', async () => {
    const { result, rerender } = renderHook(
      ({ id }) => useUserData(id),
      { initialProps: { id: 1 } }
    )

    await waitFor(() => expect(result.current.data?.id).toBe(1))

    // Change id
    rerender({ id: 2 })

    await waitFor(() => expect(result.current.data?.id).toBe(2))
  })
})
```

### Mocking Modules

```typescript
// Mock entire module
vi.mock('./api/users', () => ({
  fetchUser: vi.fn(() => Promise.resolve({ id: 1, name: 'John' })),
  updateUser: vi.fn(),
}))

// Mock specific functions
import * as userApi from './api/users'
const mockFetchUser = vi.spyOn(userApi, 'fetchUser')
mockFetchUser.mockResolvedValue({ id: 1, name: 'John' })

// Mock React Query
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
const queryClient = new QueryClient({
  defaultOptions: { queries: { retry: false } }
})

const wrapper = ({ children }) => (
  <QueryClientProvider client={queryClient}>
    {children}
  </QueryClientProvider>
)

renderHook(() => useUserQuery(1), { wrapper })
```

## Common Testing Anti-Patterns

### ❌ Anti-Pattern 1: Testing Framework Code

```python
# Bad
def test_pytest_raises_works():
    with pytest.raises(ValueError):
        raise ValueError("test")
    # ❌ Testing pytest, not our code

# Good
def test_validation_raises_error_for_negative_amount():
    with pytest.raises(ValidationError):
        validate_amount(-100)
    # ✅ Testing our validation logic
```

### ❌ Anti-Pattern 2: Brittle Tests

```typescript
// Bad
it('renders user list', () => {
  render(<UserList users={users} />)
  // ❌ Breaks if we add a new user or change order
  expect(screen.getAllByRole('listitem')).toHaveLength(3)
  // ❌ Breaks if we change exact text
  expect(screen.getByText('User: John Doe (john@example.com)')).toBeInTheDocument()
})

// Good
it('renders user list', () => {
  render(<UserList users={users} />)
  // ✅ Tests actual data is displayed
  users.forEach(user => {
    expect(screen.getByText(user.name)).toBeInTheDocument()
  })
})
```

### ❌ Anti-Pattern 3: Testing Too Many Things

```python
# Bad - God test
async def test_user_workflow():
    # Create
    user = await create_user()
    assert user.id

    # Update
    updated = await update_user(user.id, name="New")
    assert updated.name == "New"

    # Login
    token = await login(user.email, user.password)
    assert token

    # Make request
    data = await get_data(token)
    assert data

    # Delete
    await delete_user(user.id)
    assert not await get_user(user.id)
    # ❌ When this fails, which part broke?

# Good - Separate tests
async def test_can_create_user(): ...
async def test_can_update_user_name(): ...
async def test_can_login_with_valid_credentials(): ...
async def test_can_fetch_data_with_valid_token(): ...
async def test_can_delete_user(): ...
```

### ❌ Anti-Pattern 4: Flaky Tests

```python
# Bad - Time dependent
async def test_cache_expires():
    cache.set("key", "value", ttl=1)
    await asyncio.sleep(1)  # ❌ Flaky timing
    assert cache.get("key") is None

# Good - Control time
async def test_cache_expires(freezer):
    cache.set("key", "value", ttl=60)
    freezer.tick(61)  # ✅ Deterministic time advancement
    assert cache.get("key") is None

# Bad - Random data
def test_process_transaction():
    amount = random.randint(1, 1000)  # ❌ Non-deterministic
    result = process(amount)
    assert result > 0

# Good - Fixed data
@pytest.mark.parametrize("amount", [1, 100, 1000, 9999])
def test_process_transaction(amount):
    result = process(amount)
    assert result == amount * 1.1  # ✅ Predictable
```

### ❌ Anti-Pattern 5: Unclear Test Names

```python
# Bad names
def test_1(): ...
def test_user(): ...
def test_it_works(): ...
def test_edge_case(): ...

# Good names
def test_create_user_with_valid_email_returns_user(): ...
def test_create_user_with_invalid_email_raises_validation_error(): ...
def test_create_user_with_duplicate_email_raises_conflict_error(): ...
def test_update_user_name_succeeds_when_authenticated(): ...
```

## Test Coverage Guidelines

### What to Aim For

- **Overall backend**: 90%+ (enforced by `--cov-fail-under=90`)
- **Critical paths**: 100% (auth, payments, data mutations)
- **Utilities/helpers**: 100% (pure functions, easy to test)
- **API endpoints**: 90%+ (integration tests)
- **Business logic**: 95%+ (unit tests with edge cases)
- **UI components**: 70%+ (focus on logic, not markup)

### What Coverage Doesn't Mean

- **High coverage ≠ Good tests**: Can have 100% coverage with weak tests
- **Low coverage ≠ Bad code**: Some code is hard to test (not worth forcing)
- **Line coverage ≠ Branch coverage**: May miss untested conditional branches

### Focus on Quality Over Quantity

```python
# 100% coverage, but weak test
def test_divide():
    assert divide(10, 2) == 5  # Only happy path

# Better: Lower coverage percentage, but tests edge cases
def test_divide_returns_correct_result():
    assert divide(10, 2) == 5

def test_divide_by_zero_raises_error():
    with pytest.raises(ZeroDivisionError):
        divide(10, 0)

def test_divide_handles_float_precision():
    assert abs(divide(1, 3) - 0.333333) < 0.0001
```

## Testing Checklist for Reviews

When reviewing tests, check:

- [ ] **AAA Pattern**: Clear Arrange-Act-Assert structure
- [ ] **Independence**: Tests don't depend on each other
- [ ] **Naming**: Test names describe what and why
- [ ] **Single Concept**: One logical assertion per test
- [ ] **Can Fail**: Test would fail if code is broken
- [ ] **Edge Cases**: Null, empty, boundary values tested
- [ ] **Error Cases**: Exceptions and failures tested
- [ ] **Mocking**: External dependencies mocked appropriately
- [ ] **Not Over-Mocked**: Not mocking system under test
- [ ] **Async Handled**: Proper await in async tests
- [ ] **Fast**: Unit tests run in milliseconds
- [ ] **Deterministic**: No random data, no timing issues
- [ ] **Clean Up**: Resources freed, database rolled back

## Further Reading

- [Python Testing with pytest](https://pragprog.com/titles/bopytest/python-testing-with-pytest/) - Brian Okken
- [Testing JavaScript Applications](https://www.manning.com/books/testing-javascript-applications) - Lucas da Costa
- [Growing Object-Oriented Software, Guided by Tests](http://www.growing-object-oriented-software.com/) - Steve Freeman & Nat Pryce
- [Testing Trophy](https://kentcdodds.com/blog/the-testing-trophy-and-testing-classifications) - Kent C. Dodds
