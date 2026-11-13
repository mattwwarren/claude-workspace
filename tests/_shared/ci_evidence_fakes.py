"""In-process stand-ins for ``prep_pr_state``'s two GitHub/git readers (#3618).

``gate_ci_evidence`` reads the committed config (``_base_config_text``), the
local HEAD (``_run_git``) and, through ``_pr_state`` / ``_check_entries``, the
PR and its checks. These helpers replace those internal seams so the decision
logic can be driven without a ``gh`` binary; they are not ``gh`` CLI fakes (the
subprocess-level shim is ``tests/_tool_fakes.py::stub_gh_ci``).

Promoted from ``tests/test_prep_pr_gate_ci_evidence.py`` so the ``ci-deferral``
and ``ci-reuse-plan`` tests share them. No fixtures live here, so the module is
imported directly rather than registered in conftest's ``pytest_plugins``.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Callable

HEAD = "a" * 40
OTHER_HEAD = "b" * 40
GATE = "unit-tests"
CHECK = "ci-status"

CONFIG = f"""\
quality_gates:
  ci_evidence:
    {GATE}: {CHECK}
"""


def git_returning(value: str) -> Callable[..., str]:
    """A ``_run_git`` stand-in that answers *value* to every call."""

    def fake_git(args: list[str], default: str = "") -> str:
        del args, default
        return value

    return fake_git


def config_returning(text: str) -> Callable[[str], str]:
    """A ``_base_config_text`` stand-in that answers *text* for any base."""

    def fake_config(base: str) -> str:
        del base
        return text

    return fake_config


def const_gh(value: object) -> Callable[[list[str]], object]:
    """A ``_gh_json`` stand-in that answers *value* for any arguments."""

    def fake_gh(args: list[str]) -> object:
        del args
        return value

    return fake_gh


def check_run(
    name: str,
    *,
    status: str = "completed",
    conclusion: str = "success",
    html_url: str | None = None,
) -> dict[str, Any]:
    """A REST check run (lower-case enums, as ``gh api`` returns them)."""
    run: dict[str, Any] = {"name": name, "status": status, "conclusion": conclusion}
    if html_url is not None:
        run["html_url"] = html_url
    return run


def commit_status(
    name: str, state: str, *, target_url: str | None = None
) -> dict[str, Any]:
    """A REST commit status."""
    entry: dict[str, Any] = {"context": name, "state": state}
    if target_url is not None:
        entry["target_url"] = target_url
    return entry


def pr(*, head: str = HEAD, base: str = "main", state: str = "OPEN") -> dict[str, Any]:
    """``gh pr view --json headRefOid,baseRefName,state`` (GraphQL, upper-case)."""
    return {"headRefOid": head, "baseRefName": base, "state": state}


class Github:
    """Stands in for the two GitHub reads and records how they were called."""

    def __init__(
        self,
        pr: dict[str, Any] | None,
        entries: dict[str, list[dict[str, Any]] | None] | None = None,
    ) -> None:
        self.pr = pr
        self.entries = entries or {}
        self.pr_calls: list[tuple[int, str | None]] = []
        self.entry_calls: list[tuple[str | None, str, str]] = []

    def pr_state(self, pr: int, repo: str | None) -> dict[str, Any] | None:
        self.pr_calls.append((pr, repo))
        return self.pr

    def check_entries(
        self, repo: str | None, sha: str, check: str
    ) -> list[dict[str, Any]] | None:
        self.entry_calls.append((repo, sha, check))
        return self.entries.get(check, [])
