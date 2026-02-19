# claude-workspace (cw)

Multi-session workspace orchestrator for Claude Code.

## Project Structure

- `src/cw/` - Main package
  - `cli.py` - Click CLI dispatcher
  - `config.py` - Client config loading (~/.config/cw/clients.yaml)
  - `models.py` - Pydantic models (Session, Client, State)
  - `session.py` - Session lifecycle (start, bg, resume)
  - `zellij.py` - Zellij CLI wrapper
  - `handoff.py` - Handoff document parsing
- `layouts/` - Jinja2 templates for Zellij layouts (.kdl.j2)
- `config/` - Example configuration files
- `tests/` - Test suite

## Development

```bash
uv run cw --help                    # Run CLI
uv run pytest tests/ -v             # Run tests
uv run ruff check src/ tests/       # Lint
uv run mypy src/                    # Type check
uv run pytest tests/ --cov=cw      # Coverage report
```

## Quality Gates

Before committing, run all three checks:

```bash
uv run ruff check src/ tests/ && uv run mypy src/ && uv run pytest tests/ -v
```

Pre-commit hooks enforce this automatically (`uv run pre-commit install`).

## Testing

118 tests across 7 test files covering all modules (92% line coverage).

| File | Tests | Covers |
|------|-------|--------|
| `test_models.py` | Models, enums, state queries | `models.py` |
| `test_session_helpers.py` | `_relative_time` with freezegun | `session.py` helpers |
| `test_config.py` | Config load/save, client lookup | `config.py` |
| `test_handoff.py` | Handoff parsing, mtime filtering | `handoff.py` |
| `test_zellij.py` | Zellij wrapper, layout generation | `zellij.py` |
| `test_session.py` | Session lifecycle (start/bg/resume) | `session.py` |
| `test_cli.py` | CLI dispatch, Click integration | `cli.py` |

**Patterns:**
- Monkeypatch `CONFIG_DIR`/`STATE_DIR` to `tmp_path` (see `conftest.py`)
- Mock `cw.zellij.*` via `mock_zellij` fixture for session tests
- Use `freezegun` for time-dependent assertions
- Use Click's `CliRunner` for CLI tests

## Key Patterns

- State stored at `~/.local/share/cw/sessions.json`
- Client config at `~/.config/cw/clients.yaml`
- Generated layouts at `~/.config/zellij/layouts/cw-<client>.kdl`
- Integrates with existing handoff pipeline at `~/.claude/scripts/generate_handoff.py`

## Shell Completion

Enable tab completion for `cw` commands:

```bash
# Bash (~/.bashrc)
eval "$(_CW_COMPLETE=bash_source cw)"

# Zsh (~/.zshrc)
eval "$(_CW_COMPLETE=zsh_source cw)"

# Fish (~/.config/fish/config.fish)
_CW_COMPLETE=fish_source cw | source
```

Run `cw completion <shell>` to see the activation snippet.

Completions provide:
- Client names for `start`, `switch`
- Session names for `resume` (filters out completed sessions)
- Purpose choices for `hand` (via Click.Choice, automatic)

## Common Workflows

### Full session lifecycle

```bash
# Start a new session (launches Zellij with impl/review/debt panes)
cw start my-client

# Background when done (triggers /session-done, waits for handoff)
cw bg

# Resume later with handoff context injected
cw resume my-client/impl

# Check what's running
cw status
```

### Handing off between sessions

```bash
# Send a task from impl to debt session
cw hand debt "Fix the ruff violations in session.py" --from impl

# Messages are persisted to .cw/messages/ for audit
```

### Multi-client workflow

```bash
# Start sessions for different projects
cw start client-a
cw start client-b

# Switch between client tabs in Zellij
cw switch client-a
cw switch client-b

# List all active sessions
cw list
```

## Architecture Decisions

- **Keystroke injection**: `cw bg` injects `/session-done` into Zellij panes. Fragile but zero-coupling to Claude Code internals.
- **Flat JSON state**: Simple, human-readable. Single-user tool.
- **Jinja2 layouts**: KDL templates rendered per-client with workspace paths.
- **On-demand health checks**: `cw status` and `cw start` detect crashed Claude panes via Zellij's `dump-layout` output. No background daemon needed.
