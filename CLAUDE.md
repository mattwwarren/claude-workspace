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

## Architecture Decisions

- **Keystroke injection**: `cw bg` injects `/session-done` into Zellij panes. Fragile but zero-coupling to Claude Code internals.
- **Flat JSON state**: Simple, human-readable. Single-user tool.
- **Jinja2 layouts**: KDL templates rendered per-client with workspace paths.
