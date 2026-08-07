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
    _build_generic_codex_argv,
    _capture_diff,
    _prepare_review_pass,
    _run_codex_role,
    _slug,
    run_codex_roles,
)
from cw.codex_runner import CodexRunResult, RealCodexRunner
from cw.review_findings import ReviewerFindingsDocument, consolidate_verdict
from tests._codex_review_helpers import _task
from tests.conftest import _clean_git_env
from tests.test_codex_contract_secrets import _assert_no_secrets_leaked

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator

pytestmark = pytest.mark.integration

_log = logging.getLogger(__name__)

_CODEX_LIVE = os.environ.get("INTEGRATION_CODEX_LIVE", "").strip() not in ("", "0")

_LIVE_SESSION_ID = "live-contract-suite"
_ROLE = "Code Quality Reviewer"


def _git(repo: Path, *args: str) -> None:
    clean_env = _clean_git_env()
    subprocess.run(
        ["git", "-C", str(repo), *args], capture_output=True, check=True, env=clean_env
    )


class _RecordingCodexRunner(RealCodexRunner):
    """RealCodexRunner that stashes every result so secrets can be scanned."""

    def __init__(self) -> None:
        self.results: list[CodexRunResult] = []

    def run(
        self,
        worktree: Path,
        argv: list[str],
        timeout_seconds: int | None,
        *,
        stdin: str | None = None,
    ) -> CodexRunResult:
        result = super().run(worktree, argv, timeout_seconds, stdin=stdin)
        self.results.append(result)
        return result

    def assert_clean(self) -> None:
        """Scan every captured subprocess output for leaked secrets."""
        for result in self.results:
            _assert_no_secrets_leaked(
                result.stdout, result.stderr, result.output_file_content or ""
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


def _install_agent_specs(repo: Path) -> None:
    """Copy the real .claude/agents/ tree into *repo* verbatim (R6).

    Wholesale copytree (not a hand-picked subset) so the fixture cannot
    drift from _REVIEWER_ROLE_AGENT_FILES. The tree is git-tracked, so a
    fresh worker worktree has it; same off-checkout read location as
    tests/test_orchestrator_skill.py:5.

    Module-level (not conftest.py): only this class consumes it today;
    promote to conftest.py if a second consumer appears.
    """
    source = Path(__file__).parent.parent / ".claude" / "agents"
    shutil.copytree(source, repo / ".claude" / "agents")


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
        doc, failure, _metrics = _run_codex_role(
            runner=runner,
            worktree=repo,
            role=_ROLE,
            prompt=_reviewer_prompt(_ROLE, diff.text),
            model=None,
            timeout_seconds=120,
            scratch_dir=_scratch(base),
            session_id=_LIVE_SESSION_ID,
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
        doc, failure, _metrics = _run_codex_role(
            runner=runner,
            worktree=repo,
            role=_ROLE,
            prompt=_reviewer_prompt(_ROLE, diff.text),
            model=None,
            timeout_seconds=120,
            scratch_dir=_scratch(base),
            session_id=_LIVE_SESSION_ID,
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
        doc, failure, _metrics = _run_codex_role(
            runner=runner,
            worktree=repo,
            role=_ROLE,
            prompt=prompt,
            model=None,
            timeout_seconds=120,
            scratch_dir=_scratch(base),
            session_id=_LIVE_SESSION_ID,
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
        doc, failure, _metrics = _run_codex_role(
            runner=_RecordingCodexRunner(),
            worktree=repo,
            role=_ROLE,
            prompt=_reviewer_prompt(_ROLE, diff.text),
            model=None,
            timeout_seconds=120,
            scratch_dir=scratch,
            session_id=_LIVE_SESSION_ID,
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
        doc, failure, _metrics = _run_codex_role(
            runner=runner,
            worktree=repo,
            role=_ROLE,
            prompt=_reviewer_prompt(_ROLE, diff.text),
            model="definitely-not-a-real-model-xyz",
            timeout_seconds=120,
            scratch_dir=_scratch(base),
            session_id=_LIVE_SESSION_ID,
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
        doc, failure, _metrics = _run_codex_role(
            runner=runner,
            worktree=repo,
            role=_ROLE,
            prompt=prompt,
            model=None,
            timeout_seconds=2,
            scratch_dir=_scratch(base),
            session_id=_LIVE_SESSION_ID,
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
            doc, failure, _metrics = _run_codex_role(
                runner=runner,
                worktree=repo,
                role=_ROLE,
                prompt=_reviewer_prompt(_ROLE, diff.text),
                model=None,
                timeout_seconds=120,
                scratch_dir=_scratch(base),
                session_id=_LIVE_SESSION_ID,
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
        # A nightly run installs a real codex CLI right before this test, so
        # it has a live opportunity to catch a capability-probe regression
        # (e.g. a version-string parsing bug misdiagnosing a capable install)
        # — assert on it rather than only logging, so a misdiagnosis turns
        # the nightly run red instead of passing silently (#1238 review).
        assert probe.diagnosis is None, probe.detail
        runner.assert_clean()


_CANARY_SESSION_ID = "live-contract-canary"


@pytest.mark.skipif(not _CODEX_LIVE, reason="INTEGRATION_CODEX_LIVE not set")
class TestCodexContractProductionPromptCanary:
    """Seeded defect must survive consolidate_verdict under the REAL
    production prompt chain (#1546) -- _prepare_review_pass ->
    run_codex_roles -- not this file's self-authored _reviewer_prompt().

    Sequencing (ticket body): meaningful only after #1543's
    _OUTPUT_INSTRUCTIONS precedence fix landed (377a576f, ancestor of the
    base this ticket branches from) -- against the raw pre-#1543 prompt
    this canary would fail for the already-diagnosed reason and prove
    nothing new.
    """

    def test_seeded_defect_survives_production_prompt_chain(
        self,
        make_git_repo: Callable[..., Path],
        live_base: Callable[[], Path],
    ) -> None:
        base = live_base()
        defect = "def ratio(n: int) -> float:\n    return n / 0\n"
        repo = _seed_repo(
            make_git_repo,
            base,
            "production-prompt-canary",
            filename="math_util.py",
            content=defect,
        )
        _install_agent_specs(repo)

        prepared = _prepare_review_pass(_task(), repo, "main")
        # R2: a seeded .py defect under small-scope (_task()'s default
        # scope_hint=None) selects Code Quality + SysAdmin (mandatory)
        # plus Data Safety (categories.python forces
        # mutates_persisted_state) -- three live codex exec calls, the
        # intended consequence of routing through real selection logic.
        assert prepared.roles == [
            "Code Quality Reviewer",
            "SysAdmin Reviewer",
            "Data Safety Reviewer",
        ]

        runner = _RecordingCodexRunner()
        documents, failures, _metrics_by_role = run_codex_roles(
            runner=runner,
            worktree=repo,
            roles=prepared.roles,
            prompts_by_role=prepared.prompts_by_role,
            model=None,
            wall_clock_budget_seconds=None,
            session_id=_CANARY_SESSION_ID,
        )
        runner.assert_clean()

        verdict = consolidate_verdict(
            documents,
            prepared.diff,
            prepared.reviewed_sha,
            failed_reviewers=failures,
        )
        assert len(verdict.accepted) >= 1, (
            "no finding survived consolidate_verdict under the production "
            f"prompt chain; documents={documents!r} failures={failures!r}"
        )


@pytest.mark.skipif(not _CODEX_LIVE, reason="INTEGRATION_CODEX_LIVE not set")
class TestCodexContractAuditEvents:
    """#1710: the real CLI emits a parseable JSONL audit stream and persists
    no session file when ``--json``/``--ephemeral`` are set.

    Re-proves R0 (``--ephemeral`` writes nothing under ``~/.codex/sessions``)
    on every nightly run rather than only at plan time.
    """

    def test_argv_always_carries_the_audit_flags(
        self, live_base: Callable[[], Path]
    ) -> None:
        base = live_base()
        argv = _build_generic_codex_argv(
            model=None,
            schema_path=_scratch(base) / "s.json",
            output_path=_scratch(base) / "o.json",
        )
        assert "--json" in argv
        assert "--ephemeral" in argv

    def test_live_run_populates_audit_metrics_and_persists_no_session(
        self, make_git_repo: Callable[..., Path], live_base: Callable[[], Path]
    ) -> None:
        base = live_base()
        repo = _seed_repo(
            make_git_repo,
            base,
            "audit-events",
            filename="greeting.py",
            content='def greet(name: str) -> str:\n    return f"Hello, {name}!"\n',
        )
        diff, _sha, _files = _capture_diff(repo, "main")

        sessions_dir = Path.home() / ".codex" / "sessions"
        before: int | None = (
            sum(1 for _ in sessions_dir.rglob("*") if _.is_file())
            if sessions_dir.is_dir()
            else None
        )

        runner = _RecordingCodexRunner()
        doc, failure, metrics = _run_codex_role(
            runner=runner,
            worktree=repo,
            role=_ROLE,
            prompt=_reviewer_prompt(_ROLE, diff.text),
            model=None,
            timeout_seconds=120,
            scratch_dir=_scratch(base),
            session_id=_LIVE_SESSION_ID,
        )
        assert failure is None
        assert doc is not None
        runner.assert_clean()

        thread_id = metrics["thread_id"]
        assert isinstance(thread_id, str)
        assert thread_id
        assert metrics["terminal_event"] == "turn.completed"
        assert metrics["tool_call_counts"]
        token_counts = [
            metrics["input_tokens"],
            metrics["cached_input_tokens"],
            metrics["output_tokens"],
            metrics["reasoning_tokens"],
        ]
        for value in token_counts:
            assert isinstance(value, int)
            assert value >= 0

        if before is None:
            pytest.skip("~/.codex/sessions absent in this environment")
        after = sum(1 for _ in sessions_dir.rglob("*") if _.is_file())
        assert after == before, (
            "--ephemeral must not persist a session file; "
            f"~/.codex/sessions file count went {before} -> {after}"
        )
