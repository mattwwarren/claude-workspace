"""Shared builders for the scope-drift grant tests (#3772).

Plain functions, no fixtures, so this module is not registered in
``tests/conftest.py`` (precedent: ``stage_preflight_builders``). Every grant
builder agrees on one identity (client ``acme``, ticket ``T-1``, one row
``created_at``), so a grant and the row built by :func:`grant_task` bind by
default and a test overrides exactly the dimension it exercises.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, NamedTuple

from cw.models.enums import Stage
from cw.models.scope_grant import ScopeGrant
from tests._shared.git_repos import git_in
from tests._shared.session_seeding import _make_ticket_task

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence

    from cw.models.tasks.ticket_task import TicketTask

GRANT_REMOTE_URL = "https://github.com/acme/widgets.git"
GRANT_SLUG = "acme/widgets"
GRANT_CLIENT = "acme"
GRANT_TICKET = "T-1"
GRANT_ROW_CREATED = datetime(2026, 11, 2, tzinfo=UTC)
GRANT_RECORDED_AT = datetime(2026, 11, 3, tzinfo=UTC)
GRANT_POLICY = "policy-a"

_REAL_SCRIPT = (
    Path(__file__).resolve().parents[2]
    / ".claude"
    / "scripts"
    / "check_plan_scope_conformance.py"
)
_PYPROJECT = "[tool.cw.scope_conformance]\nabs_floor = 0\n"


class GrantRepo(NamedTuple):
    """A worktree whose history is H1 (a, b) then H2 (adds c)."""

    worktree: Path
    h1: str
    h2: str


def _write(repo: Path, rel: str, body: str) -> None:
    target = repo / rel
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(body, encoding="utf-8")


def _commit(repo: Path, message: str) -> str:
    git_in(repo, "add", "-A")
    git_in(repo, "commit", "-q", "-m", message)
    return git_in(repo, "rev-parse", "HEAD").strip()


def build_grant_repo(make_git_repo: Callable[..., Path]) -> GrantRepo:
    """A GitHub-origin repo: H1 has plan, a, b and ``abs_floor = 0``; H2 adds c.

    ``abs_floor = 0`` makes a single extra path trip the scope-conformance gate
    (``check_plan_scope_conformance.py`` allows ``max(abs_floor, ...)`` extras).
    """
    repo = make_git_repo("grant-repo")
    git_in(repo, "remote", "add", "origin", GRANT_REMOTE_URL)
    _write(repo, "pyproject.toml", _PYPROJECT)
    _write(repo, "src/plan.py", "plan = 1\n")
    _write(repo, "src/a.py", "a = 1\n")
    _write(repo, "src/b.py", "b = 1\n")
    h1 = _commit(repo, "H1")
    _write(repo, "src/c.py", "c = 1\n")
    h2 = _commit(repo, "H2")
    return GrantRepo(worktree=repo, h1=h1, h2=h2)


def make_grant(**overrides: object) -> ScopeGrant:
    """A grant that binds to :func:`grant_task`'s row, with *overrides* applied."""
    kwargs: dict[str, object] = {
        "client": GRANT_CLIENT,
        "ticket_id": GRANT_TICKET,
        "row_created_at": GRANT_ROW_CREATED,
        "repo": GRANT_SLUG,
        "pr_number": None,
        "head": "1" * 40,
        "policy": GRANT_POLICY,
        "plan_fingerprint": None,
        "paths": ["src/a.py", "src/b.py"],
        "actor": "op",
        "recorded_at": GRANT_RECORDED_AT,
    }
    kwargs.update(overrides)
    return ScopeGrant.model_validate(kwargs)


def grant_task(grants: Sequence[ScopeGrant], **overrides: object) -> TicketTask:
    """An IMPL row with the grants' identity (client, ticket, ``created_at``)."""
    kwargs: dict[str, object] = {
        "ticket_id": GRANT_TICKET,
        "client": GRANT_CLIENT,
        "stage": Stage.IMPL,
        "created_at": GRANT_ROW_CREATED,
        "scope_grants": list(grants),
    }
    kwargs.update(overrides)
    return _make_ticket_task(**kwargs)


def install_real_scope_script() -> Path:
    """Copy the repo's real scope script under the per-test ``~/.claude/scripts``."""
    target_dir = Path.home() / ".claude" / "scripts"
    target_dir.mkdir(parents=True, exist_ok=True)
    target = target_dir / _REAL_SCRIPT.name
    target.write_bytes(_REAL_SCRIPT.read_bytes())
    return target


def drift_details(paths: Sequence[str]) -> str:
    """The ``extra_files:`` list text a ``plan_scope_drift`` park carries."""
    return "extra_files:\n" + "".join(f"- {path}\n" for path in paths)
