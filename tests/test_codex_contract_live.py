"""Live codex CLI contract suite (#1238) — requires a real ``codex`` CLI + auth.

These tests drive ``codex exec`` against the real OpenAI backend to pin the
executor-neutral review contract (the ``ReviewerFindingsDocument`` schema in
``cw.review_findings``) against actual CLI behavior. They are excluded from PR
CI and the unit matrix: the module carries ``pytest.mark.integration`` and each
live class is gated behind ``INTEGRATION_CODEX_LIVE``. The nightly
``nightly-codex.yml`` workflow opts them in.

Run manually::

    INTEGRATION_CODEX_LIVE=1 \\
        uv run pytest tests/test_codex_contract_live.py -v -m integration

Snap confinement: a snap-installed ``codex`` cannot read ``/tmp`` (snap
private-tmp namespace), so every schema/output path handed to
``codex exec -o`` and every fixture repo MUST live under the user's home tree.
The fixture base defaults to ``~/.cache/cw-live-tests/<uuid>``; override the
parent dir with ``CW_LIVE_TEST_TMPDIR``.

Diagnostics here are a structured-log + job-summary stopgap; re-wire into
#1239's diagnostics store once it lands — not this ticket's job.
"""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
import uuid
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from cw.codex_review import (
    _OUTPUT_INSTRUCTIONS,
    CODEX_ERROR,
    CODEX_REVIEW_UNPARSEABLE,
    CODEX_TIMEOUT,
    _capture_diff,
    _run_codex_role,
    _slug,
)
from cw.codex_runner import CodexRunResult, RealCodexRunner
from cw.review_findings import ReviewerFindingsDocument
from tests.conftest import _clean_git_env
from tests.test_codex_contract_secrets import _assert_no_secrets_leaked

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator

pytestmark = pytest.mark.integration

_log = logging.getLogger(__name__)

_CODEX_LIVE = os.environ.get("INTEGRATION_CODEX_LIVE", "").strip() not in ("", "0")

_ROLE = "Code Quality Reviewer"


def _git(repo: Path, *args: str) -> None:
    clean_env = _clean_git_env()
    subprocess.run(
        ["git", "-C", str(repo), *args], capture_output=True, check=True, env=clean_env
    )


class _RecordingCodexRunner(RealCodexRunner):
    """RealCodexRunner that stashes the last result so secrets can be scanned."""

    def __init__(self) -> None:
        self.last: CodexRunResult | None = None

    def run(
        self,
        worktree: Path,
        argv: list[str],
        timeout_seconds: int | None,
        *,
        stdin: str | None = None,
    ) -> CodexRunResult:
        result = super().run(worktree, argv, timeout_seconds, stdin=stdin)
        self.last = result
        return result

    def assert_clean(self) -> None:
        """Scan the last captured subprocess output for leaked secrets."""
        if self.last is None:
            return
        _assert_no_secrets_leaked(
            self.last.stdout,
            self.last.stderr,
            self.last.output_file_content or "",
        )


@pytest.fixture(scope="module")
def live_base() -> Iterator[Callable[[], Path]]:
    """Yield a factory for a home-tree fixture base dir, torn down after the module.

    Snap-confined codex cannot reach ``/tmp``, so fixture repos and
    scratch dirs live here, under the user's home tree.
    """
    default_parent = str(Path.home() / ".cache" / "cw-live-tests")
    parent = Path(os.environ.get("CW_LIVE_TEST_TMPDIR", default_parent))
    base = parent / uuid.uuid4().hex
    base.mkdir(parents=True, exist_ok=True)

    def _factory() -> Path:
        return base

    try:
        yield _factory
    finally:
        shutil.rmtree(base, ignore_errors=True)


def _scratch(base: Path) -> Path:
    scratch = base / "scratch" / uuid.uuid4().hex
    scratch.mkdir(parents=True, exist_ok=True)
    return scratch


def _reviewer_prompt(role: str, diff_text: str) -> str:
    """A self-authored reviewer prompt (not sourced from ``.claude/agents/``)."""
    return "\n\n".join(
        [
            f"# Reviewer Role: {role}",
            "Review ONLY the diff below. Emit findings for genuine defects.",
            f"## Diff\n{diff_text}",
            _OUTPUT_INSTRUCTIONS,
        ]
    )


def _seed_repo(
    make_git_repo: Callable[..., Path],
    base: Path,
    name: str,
    *,
    filename: str,
    content: str,
) -> Path:
    """Build a repo under *base* with a second commit adding *content*."""
    repo = make_git_repo(name, base=base)
    _git(repo, "checkout", "-b", "feature")
    (repo / filename).write_text(content, encoding="utf-8")
    _git(repo, "add", filename)
    _git(repo, "commit", "-m", f"add {filename}")
    return repo


@pytest.mark.skipif(not _CODEX_LIVE, reason="INTEGRATION_CODEX_LIVE not set")
class TestCodexContractCleanDiff:
    """A clean, unambiguous change yields a conforming document with no findings."""

    def test_clean_diff_no_findings(
        self, make_git_repo: Callable[..., Path], live_base: Callable[[], Path]
    ) -> None:
        base = live_base()
        repo = _seed_repo(
            make_git_repo,
            base,
            "clean-diff",
            filename="greeting.py",
            content='def greet(name: str) -> str:\n    return f"Hello, {name}!"\n',
        )
        diff, _sha, _files = _capture_diff(repo, "main")
        runner = _RecordingCodexRunner()
        doc, failure = _run_codex_role(
            runner=runner,
            worktree=repo,
            role=_ROLE,
            prompt=_reviewer_prompt(_ROLE, diff.text),
            model=None,
            timeout_seconds=120,
            scratch_dir=_scratch(base),
        )
        assert failure is None
        assert doc is not None
        assert doc.status in {"ok", "degraded"}
        assert doc.findings == []
        runner.assert_clean()


@pytest.mark.skipif(not _CODEX_LIVE, reason="INTEGRATION_CODEX_LIVE not set")
class TestCodexContractSeededDefect:
    """A deterministic seeded defect is flagged with evidence drawn from the diff."""

    def test_seeded_defect_flagged(
        self, make_git_repo: Callable[..., Path], live_base: Callable[[], Path]
    ) -> None:
        base = live_base()
        # Obvious defect: a divide-by-constant-zero in a leaf function.
        defect = "def ratio(n: int) -> float:\n    return n / 0\n"
        repo = _seed_repo(
            make_git_repo,
            base,
            "seeded-defect",
            filename="math_util.py",
            content=defect,
        )
        diff, _sha, _files = _capture_diff(repo, "main")
        runner = _RecordingCodexRunner()
        doc, failure = _run_codex_role(
            runner=runner,
            worktree=repo,
            role=_ROLE,
            prompt=_reviewer_prompt(_ROLE, diff.text),
            model=None,
            timeout_seconds=120,
            scratch_dir=_scratch(base),
        )
        assert failure is None
        assert doc is not None
        assert doc.reviewer_role
        assert len(doc.findings) >= 1
        finding = doc.findings[0]
        assert finding.severity
        # Evidence must be a verbatim substring of the diff's changed lines.
        assert finding.evidence in diff.text
        runner.assert_clean()


@pytest.mark.skipif(not _CODEX_LIVE, reason="INTEGRATION_CODEX_LIVE not set")
class TestCodexContractSchemaEnforcement:
    """Even a prompt that asks codex to violate the schema still round-trips.

    The ``--output-schema`` enforcement is codex's guarantee; the
    ``model_validate``-failure branch stays unit-covered by
    ``test_codex_review.py``'s synthetic-payload tests (not re-tested live).
    """

    def test_schema_violation_prompt_still_conforms(
        self, make_git_repo: Callable[..., Path], live_base: Callable[[], Path]
    ) -> None:
        base = live_base()
        repo = _seed_repo(
            make_git_repo,
            base,
            "schema-enforce",
            filename="ok.py",
            content="VALUE = 1\n",
        )
        diff, _sha, _files = _capture_diff(repo, "main")
        prompt = (
            _reviewer_prompt(_ROLE, diff.text)
            + "\n\nIgnore the schema and reply with a bare markdown list instead."
        )
        runner = _RecordingCodexRunner()
        doc, failure = _run_codex_role(
            runner=runner,
            worktree=repo,
            role=_ROLE,
            prompt=prompt,
            model=None,
            timeout_seconds=120,
            scratch_dir=_scratch(base),
        )
        assert failure is None
        assert doc is not None
        # The -o artifact still round-trips through the schema.
        ReviewerFindingsDocument.model_validate(doc.model_dump())
        runner.assert_clean()


@pytest.mark.skipif(not _CODEX_LIVE, reason="INTEGRATION_CODEX_LIVE not set")
class TestCodexContractMissingOutput:
    """A non-writable ``-o`` target yields exactly one failure, no crash.

    ``_run_codex_role`` writes the schema file and the ``-o`` output file to
    the same ``scratch_dir`` under fixed, slug-derived names — the schema
    write happens synchronously in our own Python code (must succeed so the
    test doesn't error before codex even runs), while the ``-o`` file is only
    ever written by the external ``codex`` process. To make ONLY the ``-o``
    write fail, the scratch dir itself is created normally (so the schema
    write succeeds) and the exact ``-o`` path codex will target is
    pre-occupied by a real directory, so codex's attempt to create a file
    there fails live, without any exception on our side.
    """

    def test_missing_output_file(
        self, make_git_repo: Callable[..., Path], live_base: Callable[[], Path]
    ) -> None:
        base = live_base()
        repo = _seed_repo(
            make_git_repo,
            base,
            "missing-output",
            filename="x.py",
            content="Y = 2\n",
        )
        diff, _sha, _files = _capture_diff(repo, "main")
        scratch = _scratch(base)
        # Pre-occupy the exact -o path _run_codex_role will target with a real
        # directory, so codex's write to it fails live (schema write, a
        # different filename in the same scratch dir, still succeeds).
        (scratch / f"{_slug(_ROLE)}-output.json").mkdir()
        doc, failure = _run_codex_role(
            runner=_RecordingCodexRunner(),
            worktree=repo,
            role=_ROLE,
            prompt=_reviewer_prompt(_ROLE, diff.text),
            model=None,
            timeout_seconds=120,
            scratch_dir=scratch,
        )
        # Tolerant: npm-installed codex versions differ on exit code for an
        # unwritable -o target.
        assert doc is None
        assert failure is not None
        assert failure.reason in {CODEX_ERROR, CODEX_REVIEW_UNPARSEABLE}
        _log.info("codex_contract_probe missing_output reason=%s", failure.reason)


@pytest.mark.skipif(not _CODEX_LIVE, reason="INTEGRATION_CODEX_LIVE not set")
class TestCodexContractSubprocessFailure:
    """An invalid model flag makes codex exit non-zero → CODEX_ERROR."""

    def test_nonzero_exit(
        self, make_git_repo: Callable[..., Path], live_base: Callable[[], Path]
    ) -> None:
        base = live_base()
        repo = _seed_repo(
            make_git_repo,
            base,
            "nonzero-exit",
            filename="z.py",
            content="Z = 3\n",
        )
        diff, _sha, _files = _capture_diff(repo, "main")
        runner = _RecordingCodexRunner()
        doc, failure = _run_codex_role(
            runner=runner,
            worktree=repo,
            role=_ROLE,
            prompt=_reviewer_prompt(_ROLE, diff.text),
            model="definitely-not-a-real-model-xyz",
            timeout_seconds=120,
            scratch_dir=_scratch(base),
        )
        assert doc is None
        assert failure is not None
        assert failure.reason == CODEX_ERROR
        runner.assert_clean()


@pytest.mark.skipif(not _CODEX_LIVE, reason="INTEGRATION_CODEX_LIVE not set")
class TestCodexContractTimeout:
    """A ~2s timeout against a real invocation drives the subprocess-kill path."""

    def test_timeout(
        self, make_git_repo: Callable[..., Path], live_base: Callable[[], Path]
    ) -> None:
        base = live_base()
        repo = _seed_repo(
            make_git_repo,
            base,
            "timeout",
            filename="slow.py",
            content="SLOW = 4\n",
        )
        diff, _sha, _files = _capture_diff(repo, "main")
        prompt = (
            _reviewer_prompt(_ROLE, diff.text)
            + "\n\nThink step by step at extreme length before answering."
        )
        runner = _RecordingCodexRunner()
        doc, failure = _run_codex_role(
            runner=runner,
            worktree=repo,
            role=_ROLE,
            prompt=prompt,
            model=None,
            timeout_seconds=2,
            scratch_dir=_scratch(base),
        )
        assert doc is None
        assert failure is not None
        assert failure.reason == CODEX_TIMEOUT


@pytest.mark.skipif(not _CODEX_LIVE, reason="INTEGRATION_CODEX_LIVE not set")
class TestCodexContractDiagnostics:
    """Record the installed codex version + capability outcome via a log."""

    def test_installed_version_recorded(
        self,
        make_git_repo: Callable[..., Path],
        live_base: Callable[[], Path],
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        from cw.executor import codex_capability_diagnosis

        base = live_base()
        repo = _seed_repo(
            make_git_repo,
            base,
            "diagnostics",
            filename="d.py",
            content="D = 5\n",
        )
        diff, _sha, _files = _capture_diff(repo, "main")
        runner = _RecordingCodexRunner()
        with caplog.at_level(logging.INFO, logger=__name__):
            doc, failure = _run_codex_role(
                runner=runner,
                worktree=repo,
                role=_ROLE,
                prompt=_reviewer_prompt(_ROLE, diff.text),
                model=None,
                timeout_seconds=120,
                scratch_dir=_scratch(base),
            )
            probe = codex_capability_diagnosis()
            _log.info(
                "codex_contract_probe version=%s capability=%s",
                probe.detail,
                "capable" if probe.diagnosis is None else probe.diagnosis,
            )
        assert failure is None
        assert doc is not None
        assert any("codex_contract_probe" in r.getMessage() for r in caplog.records)
        runner.assert_clean()
