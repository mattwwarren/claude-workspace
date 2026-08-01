"""Shared test helpers for the ``cw.codex_review`` per-submodule test suite.

Cross-module doubles, payload builders, and git helpers used by two or more of
the split ``test_codex_review_*.py`` files (and by ``test_codex_fix_loop.py``).
This module has no ``test_`` prefix, so pytest does not collect it (same
convention as ``tests/conftest.py``); it is imported explicitly by the test
modules that use each helper.
"""

from __future__ import annotations

import json
import os
import subprocess
from typing import TYPE_CHECKING

from cw.codex_runner import CodexRunResult
from cw.models import Stage, TicketTask
from tests.conftest import _make_ticket_task

if TYPE_CHECKING:
    from pathlib import Path


def _doc_json(*, role: str = "Code Quality Reviewer", status: str = "ok") -> str:
    return json.dumps(
        {
            "reviewer_role": role,
            "status": status,
            "detail": "reviewed; no issues found.",
            "findings": [],
        }
    )


def _ok_result(role: str = "Code Quality Reviewer") -> CodexRunResult:
    return CodexRunResult(
        returncode=0, stdout="", stderr="", output_file_content=_doc_json(role=role)
    )


class _SequencedRunner:
    """CodexRunner double returning queued results, recording each call."""

    def __init__(self, results: list[CodexRunResult]) -> None:
        self._results = results
        self.calls: list[dict[str, object]] = []

    def run(
        self,
        worktree: Path,
        argv: list[str],
        timeout_seconds: int | None,
        *,
        stdin: str | None = None,
    ) -> CodexRunResult:
        self.calls.append(
            {"argv": list(argv), "timeout": timeout_seconds, "stdin": stdin}
        )
        return self._results[len(self.calls) - 1]


class _Clock:
    """Deterministic monotonic() stand-in stepping through *values*."""

    def __init__(self, values: list[float]) -> None:
        self._values = values
        self._i = 0

    def __call__(self) -> float:
        value = self._values[self._i]
        if self._i < len(self._values) - 1:
            self._i += 1
        return value


def _git(repo: Path, *args: str) -> None:
    clean_env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        check=True,
        env=clean_env,
    )


def _write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def _task() -> TicketTask:
    return _make_ticket_task(ticket_id="T-1", client="test", stage=Stage.REVIEW)
