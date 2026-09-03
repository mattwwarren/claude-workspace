"""Guard tests for `/prep-pr`'s `prep_pr_state.py` resolution (#2090).

`/prep-pr` used to invoke `~/.claude/scripts/prep_pr_state.py` directly. That
path is a separate `global-claude` checkout nothing syncs with this repo, so a
stale copy there silently lacked the `gate-timeout` / `gate-elapsed`
subcommands Step 7 depends on (#1432) — the two calls failed as "invalid
choice" and the agent fell back to improvising gate timeouts, the exact
judgment #1432 removed.

Group A pins every prose copy (following #1634's all-copies rule): no bare
installed-path invocation may remain, and the resolver fence in
`auto-dev-finalize.md` must stay byte-identical to `/prep-pr` Step 2's.
Group B *executes* the extracted resolver against real temp trees, following
`test_prep_pr_ship_it_layouts.py`: prose can confirm the order was written
down, but only running it falsifies "the repo copy wins and a stale install
stops the step".
"""

from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).parent.parent
PREP_PR_PATH = ROOT / ".claude" / "commands" / "prep-pr.md"
FINALIZE_PATH = ROOT / ".claude" / "commands" / "auto-dev-finalize.md"
FENCE = "```bash"
RESOLVER_ANCHOR = 'PREP_PR_STATE=""'
INSTALLED_PATH = "~/.claude/scripts/prep_pr_state.py"
SUBCOMMANDS = (
    "detect-gates",
    "snapshot",
    "check-scope",
    "gate-timeout",
    "gate-elapsed",
    "clean",
)

FRESH_SCRIPT = "#!/usr/bin/env python3\n# subcommands: detect-gates gate-timeout\n"
STALE_SCRIPT = "#!/usr/bin/env python3\n# subcommands: detect-gates snapshot\n"


def _fence_after(path: Path, anchor: str) -> str:
    """Extract the bash fence that contains `anchor` in `path`."""
    text = path.read_text(encoding="utf-8")
    at = text.index(anchor)
    start = text.rindex(FENCE, 0, at) + len(FENCE)
    return text[start : text.index("```", start)]


def _dedent_fence(fence: str) -> str:
    """Strip the list-item indentation `auto-dev-finalize.md` nests fences under."""
    lines = fence.splitlines()
    indents = [len(ln) - len(ln.lstrip()) for ln in lines if ln.strip()]
    indent = min(indents) if indents else 0
    return "\n".join(ln[indent:] if ln.strip() else "" for ln in lines).strip()


# ---------------------------------------------------------------------------
# Group A — prose pins
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("path", [PREP_PR_PATH, FINALIZE_PATH])
def test_no_bare_installed_path_invocation_remains(path: Path) -> None:
    """Every `prep_pr_state.py` call goes through the resolved variable."""
    text = path.read_text(encoding="utf-8")
    bare = [ln for ln in text.splitlines() if ln.strip().startswith(INSTALLED_PATH)]
    assert bare == []
    assert '"$PREP_PR_STATE" detect-gates' in text


def test_prep_pr_calls_every_subcommand_through_resolved_path() -> None:
    """The Step 7 ladder subcommands are invoked via the resolver's variable."""
    text = PREP_PR_PATH.read_text(encoding="utf-8")
    for sub in SUBCOMMANDS:
        assert re.search(rf'"\$PREP_PR_STATE" {sub}\b', text), sub


def test_resolver_fence_is_identical_across_copies() -> None:
    """#1634's all-copies rule: the two resolver fences cannot drift."""
    prep = _dedent_fence(_fence_after(PREP_PR_PATH, RESOLVER_ANCHOR))
    finalize = _dedent_fence(_fence_after(FINALIZE_PATH, RESOLVER_ANCHOR))
    assert prep == finalize


def test_resolver_probes_repo_layouts_before_installed_path() -> None:
    fence = _fence_after(PREP_PR_PATH, RESOLVER_ANCHOR)
    order = [
        fence.index(".claude/scripts/prep_pr_state.py"),
        fence.index(" scripts/prep_pr_state.py "),
        fence.index('"$HOME/.claude/scripts/prep_pr_state.py"'),
    ]
    assert order == sorted(order)
    assert "gate-timeout" in fence
    assert "STALE" in fence


def test_step_two_explains_non_persistent_shell_state() -> None:
    """The variable is per-call; the prose must say so, or later steps regress."""
    text = PREP_PR_PATH.read_text(encoding="utf-8")
    assert "Shell state does not persist between `Bash` tool calls" in text
    assert "Never fall back to a bare `~/.claude/scripts/prep_pr_state.py`" in text


# ---------------------------------------------------------------------------
# Group B — execute the resolver against real trees
# ---------------------------------------------------------------------------


def _run_resolver(cwd: Path, home: Path) -> subprocess.CompletedProcess[str]:
    script = _dedent_fence(_fence_after(PREP_PR_PATH, RESOLVER_ANCHOR))
    env = {**os.environ, "HOME": str(home)}
    return subprocess.run(
        ["bash", "-c", script],
        cwd=cwd,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )


def _write(path: Path, body: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")


@pytest.fixture
def tree(tmp_path: Path) -> tuple[Path, Path]:
    repo = tmp_path / "repo"
    home = tmp_path / "home"
    repo.mkdir()
    home.mkdir()
    return repo, home


def test_repo_dot_claude_copy_resolves(tree: tuple[Path, Path]) -> None:
    repo, home = tree
    _write(repo / ".claude" / "scripts" / "prep_pr_state.py", FRESH_SCRIPT)

    result = _run_resolver(repo, home)

    assert result.returncode == 0, result.stdout + result.stderr
    assert "PREP_PR_STATE=.claude/scripts/prep_pr_state.py" in result.stdout


def test_repo_scripts_copy_resolves(tree: tuple[Path, Path]) -> None:
    repo, home = tree
    _write(repo / "scripts" / "prep_pr_state.py", FRESH_SCRIPT)

    result = _run_resolver(repo, home)

    assert result.returncode == 0, result.stdout + result.stderr
    assert "PREP_PR_STATE=scripts/prep_pr_state.py" in result.stdout


def test_installed_copy_is_the_fallback(tree: tuple[Path, Path]) -> None:
    repo, home = tree
    _write(home / ".claude" / "scripts" / "prep_pr_state.py", FRESH_SCRIPT)

    result = _run_resolver(repo, home)

    assert result.returncode == 0, result.stdout + result.stderr
    assert f"PREP_PR_STATE={home}/.claude/scripts/prep_pr_state.py" in result.stdout


def test_repo_copy_wins_over_installed_copy(tree: tuple[Path, Path]) -> None:
    """The #2090 shape: a fresh repo copy beside a stale install must win."""
    repo, home = tree
    _write(repo / ".claude" / "scripts" / "prep_pr_state.py", FRESH_SCRIPT)
    _write(home / ".claude" / "scripts" / "prep_pr_state.py", STALE_SCRIPT)

    result = _run_resolver(repo, home)

    assert result.returncode == 0, result.stdout + result.stderr
    assert "PREP_PR_STATE=.claude/scripts/prep_pr_state.py" in result.stdout


def test_stale_installed_copy_stops_loudly(tree: tuple[Path, Path]) -> None:
    """A pre-#1432 copy fails the step instead of letting timeouts be improvised."""
    repo, home = tree
    _write(home / ".claude" / "scripts" / "prep_pr_state.py", STALE_SCRIPT)

    result = _run_resolver(repo, home)

    assert result.returncode != 0
    assert "STALE" in result.stdout
    assert "gate-timeout" in result.stdout


def test_no_copy_anywhere_stops_loudly(tree: tuple[Path, Path]) -> None:
    repo, home = tree

    result = _run_resolver(repo, home)

    assert result.returncode != 0
    assert "not found" in result.stdout


def test_real_repo_copy_passes_the_staleness_check(tmp_path: Path) -> None:
    """This repo's own tracked copy must satisfy the resolver it ships."""
    result = _run_resolver(ROOT, tmp_path)

    assert result.returncode == 0, result.stdout + result.stderr
    assert "PREP_PR_STATE=.claude/scripts/prep_pr_state.py" in result.stdout
