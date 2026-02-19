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
uv run cw --help          # Run CLI
uv run pytest             # Run tests
uv run ruff check src/    # Lint
uv run mypy src/          # Type check
```

## Key Patterns

- State stored at `~/.local/share/cw/sessions.json`
- Client config at `~/.config/cw/clients.yaml`
- Generated layouts at `~/.config/zellij/layouts/cw-<client>.kdl`
- Integrates with existing handoff pipeline at `~/.claude/scripts/generate_handoff.py`

## Architecture Decisions

- **Keystroke injection**: `cw bg` injects `/session-done` into Zellij panes. Fragile but zero-coupling to Claude Code internals.
- **Flat JSON state**: Simple, human-readable. Single-user tool.
- **Jinja2 layouts**: KDL templates rendered per-client with workspace paths.
