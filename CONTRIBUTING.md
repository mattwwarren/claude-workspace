# Contributing to claude-workspace (cw)

Thanks for considering a contribution. This is a small, single-maintainer
project; the workflow below keeps the bar high without much ceremony.

## Setup

```bash
git clone https://github.com/mattwwarren/claude-workspace.git
cd claude-workspace
uv sync
uv run pre-commit install
```

You need Python 3.13+ and [`uv`](https://docs.astral.sh/uv/).

## Quality gates

Before opening a PR, all three of these must pass with zero output:

```bash
uv run ruff check src/ tests/
uv run mypy src/
uv run pytest tests/ -v
```

Or, as a single chain:

```bash
uv run ruff check src/ tests/ && uv run mypy src/ && uv run pytest tests/ -v
```

The pre-commit hook runs the same checks. No suppressions (`# noqa`,
`# type: ignore`) without explicit justification in the PR description.

## Coding standards

- Read [PYTHON-PATTERNS.md](PYTHON-PATTERNS.md) before writing new modules
  or validators. Conservative defaults: prefer extracting a helper or
  constant over a magic value; full type annotations everywhere; `object`
  over `Any`.
- Read [CLAUDE.md](CLAUDE.md) for project-specific architecture decisions
  and the agent-spawning / cost-control conventions.
- Test files map one-to-one onto source modules
  (`test_cli.py` ↔ `cli.py`, `test_cmux.py` ↔ `cmux.py`, etc.).
- Use `Grep` / `Glob` / `Read` tools rather than shell `grep` / `find` /
  `cat` when the work is in an agent context (faster, no permission
  prompts).

## Pull request flow

1. Branch from `main`. Prefer focused PRs — one logical change per PR.
2. Run the quality gates locally before pushing.
3. Open the PR with a clear summary and a test plan checklist.
4. The author (Matt) reviews and merges. There's no review SLA; please
   ping if a PR has been quiet for more than a few days.

## Reporting bugs

Open a GitHub issue with:

- What you ran (full `cw` command + relevant env)
- What you expected
- What happened (paste the error output verbatim)
- `cw --version` and `claude --version` output
- For session-state bugs: a redacted copy of the relevant
  `~/.local/share/cw/sessions.json` entry, if you can share it

## Security issues

Do **not** open a public issue. See [SECURITY.md](SECURITY.md) for the
private disclosure path.

## License

This project is released into the public domain via the
[Unlicense](LICENSE). By contributing, you agree your contribution is
released under the same terms.
