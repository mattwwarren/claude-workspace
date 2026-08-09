"""Shared test fixtures for cw test suite."""

from __future__ import annotations

import os
import shutil
import subprocess
import warnings
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any
from unittest.mock import MagicMock

import pytest
import yaml

from cw.config import save_state
from cw.models import (
    ClientConfig,
    CwState,
    OrchestratorEventType,
    Session,
    SessionOrigin,
    SessionPurpose,
    SessionStatus,
    TicketTask,
)
from cw.native_daemon import FakeNativeDaemonClient
from cw.review_findings import (
    CapturedDiff,
    EscalationMetadata,
    Finding,
    ReviewerFindingsDocument,
)

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator

# A captured record_event invocation: (event_type, payload, correlation_id).
CapturedEvent = tuple[OrchestratorEventType, dict[str, Any], str | None]

# Repo-root-relative path constants + src/ discovery, hoisted from
# test_review_approval_guard.py's pre-existing private copy (#1240).
# Shared by test_review_approval_guard.py (which keeps its own private
# copy, unmodified, deliberately left as-is) and test_ticket_boundary_guard.py
# (which imports these). Pure, generic path/discovery helpers with no
# scan-semantics coupling, so they are the ones hoisted; each test file's
# scan-specific `_run_scan` driver stays file-local.
_REPO_ROOT = Path(__file__).resolve().parents[1]
_SRC_ROOT = _REPO_ROOT / "src"

# Optional external CLIs whose presence a test must not silently assume
# (#1753). git is intentionally excluded — universally present, and much
# of the suite shells out to it directly.
_OPTIONAL_BINARY_DENYLIST: frozenset[str] = frozenset({"codex", "opencode"})


def _iter_src_files() -> list[Path]:
    """Return every ``*.py`` file under ``src/``, sorted for determinism."""
    return sorted(_SRC_ROOT.rglob("*.py"))


def _load_workflow(path: Path) -> dict[Any, Any]:
    """Parse a GitHub Actions workflow YAML file at *path* (#1612).

    Parameterized hoist of the byte-identical private ``_workflow()`` helpers
    in test_changelog_advisory_workflow.py and test_pr_events_workflow.py,
    which differ only in the module-level ``WORKFLOW_PATH`` each closes over.
    Those two private copies are deliberately left unmodified; this is the
    canonical version a new workflow-guard test should import rather than
    adding a third copy. Each file's step/script accessors stay file-local —
    they are coupled to a specific job and step id, not generic.
    """
    workflow: dict[Any, Any] = yaml.safe_load(path.read_text())
    return workflow


def _on_block(workflow: dict[Any, Any]) -> dict[str, Any]:
    """Return the trigger block of a parsed *workflow* (#1612).

    PyYAML's SafeLoader follows YAML 1.1, which parses the bare ``on`` scalar
    key as the boolean ``True`` rather than the string "on" -- a well-known
    GitHub Actions YAML gotcha. Callers must not index ``workflow["on"]``.
    """
    on_block: dict[str, Any] = workflow[True]
    return on_block


def _seed_daemon_session(
    tmp_path: Path,
    tmp_config_dir: Path,
    session_id: str = "test1234",
    client: str = "test-client",
    name: str | None = None,
    surface_ref: str | None = "fake-pane-99",
    status: SessionStatus = SessionStatus.ACTIVE,
    **overrides: object,
) -> Session:
    """Create and save a daemon session in state.

    Extra ``**overrides`` are merged after the named params, so a caller can
    set any additional ``Session`` field (e.g. ``purpose``, ``origin``,
    ``worktree_path``) without a parallel seed helper (#1308).
    """
    workspace = tmp_path / "workspace" / client
    workspace.mkdir(parents=True, exist_ok=True)
    kwargs: dict[str, object] = {
        "id": session_id,
        "name": name or f"{client}/auto-dev/GEN-42",
        "client": client,
        "purpose": SessionPurpose.IMPL,
        "origin": SessionOrigin.DAEMON,
        "status": status,
        "workspace_path": workspace,
        "surface_ref": surface_ref,
    }
    kwargs.update(overrides)
    sess = Session.model_validate(kwargs)
    state = CwState(sessions=[sess])
    save_state(state)
    return sess


def _write_idle_transcript(
    home: Path,
    worktree: Path,
    filename: str = "fake-short-id-sess.jsonl",
) -> Path:
    """Write a minimal transcript .jsonl under the project dir for *worktree*.

    Default filename starts with ``fake-short-id`` so that
    ``_locate_session_transcript``'s surface_ref-prefix glob finds it when the
    session has ``surface_ref="fake-short-id"`` (the default in
    ``_mk_headless_daemon_session``).
    """
    encoded = str(worktree).replace("/", "-").replace(".", "-")
    project_dir = home / ".claude" / "projects" / encoded
    project_dir.mkdir(parents=True, exist_ok=True)
    path = project_dir / filename
    path.parent.mkdir(parents=True, exist_ok=True)
    record = '{"type": "assistant", "message": {"role": "assistant", "content": []}}\n'
    path.write_text(record)
    return path


def _make_daemon_session(**overrides: object) -> Session:
    """Canonical non-persisting DAEMON ``Session`` builder (#1308).

    Builds a fixed baseline daemon session; any ``**overrides`` are merged
    after the defaults so every local ``Session(...)`` construction across the
    test suite can delegate here as ``_make_daemon_session(field=value, ...)``.
    """
    kwargs: dict[str, object] = {
        "id": "sess-1",
        "name": "client-a/auto-dev/T-1",
        "client": "client-a",
        "purpose": SessionPurpose.IMPL,
        "origin": SessionOrigin.DAEMON,
        "status": SessionStatus.ACTIVE,
        "workspace_path": Path("/tmp/ws"),
        "worktree_path": Path("/tmp/wt"),
        "surface_ref": "live-ref",
        "claude_session_id": None,
        "started_at": datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC),
    }
    kwargs.update(overrides)
    return Session.model_validate(kwargs)


def find_completed_session(state: CwState) -> Session:
    """Return the sole session carrying a terminal last_result.

    Shared by test_executor.py and test_codex_executor.py's completion-path
    tests so the `next((s for s in state.sessions if s.last_result is not
    None), None)` idiom isn't duplicated at every call site (GitHub #1458).
    Asserts exactly one such session exists.
    """
    session = next((s for s in state.sessions if s.last_result is not None), None)
    assert session is not None
    return session


def _make_ticket_task(**overrides: object) -> TicketTask:
    """Minimal-but-valid ``TicketTask`` with keyword overrides (#1308).

    Only ``ticket_id`` and ``client`` are required by the model; both are
    defaulted here so ``_make_ticket_task()`` yields a valid PENDING task.
    Follows the same dict-merge + ``model_validate`` idiom as
    ``_make_escalation`` / ``_make_finding``.
    """
    kwargs: dict[str, object] = {
        "ticket_id": "T-1",
        "client": "test-client",
    }
    kwargs.update(overrides)
    return TicketTask.model_validate(kwargs)


def _patch_cw_dist_not_found(monkeypatch: pytest.MonkeyPatch) -> None:
    """Patch importlib.metadata.distribution() to raise PackageNotFoundError,
    simulating a registry/unknown install. Shared helper (#1514).
    """
    import importlib.metadata

    def _raise(_pkg: str) -> object:
        raise importlib.metadata.PackageNotFoundError(_pkg)

    monkeypatch.setattr(importlib.metadata, "distribution", _raise)


def _write_project_config_yaml(root: Path, content: str) -> None:
    """Write .claude/project-config.yaml under *root*.

    Shared by test_tracker.py's and test_review_strategy.py's own private
    `_write_config` copies in shape (write a project-config.yaml under a tmp
    root); this is the canonical version new tests should import instead of
    adding a fourth copy. The two existing private copies in test_tracker.py
    and test_review_strategy.py are left as-is — pre-existing duplication,
    out of scope for this ticket.
    """
    config_dir = root / ".claude"
    config_dir.mkdir(parents=True, exist_ok=True)
    (config_dir / "project-config.yaml").write_text(content, encoding="utf-8")


def plan_body(*, spec: bool = True, soundness: bool = True) -> str:
    """Build a plan-of-record body with optional signoff markers.

    Markers match the verbatim shape auto-dev-plan.md appends:
    ``<!-- plan-spec-reviewed: YYYY-MM-DD vN -->`` /
    ``<!-- plan-soundness-reviewed: YYYY-MM-DD vN -->``. Shared by
    test_reconcile_gate_recipes.py and test_dev_queue.py (#968) — both
    modules independently need a plan-of-record body shaped for the
    tracker-first/`.cw/plan.md`-fallback two-marker "plan reviewed" check.
    """
    lines = ["# Plan — some ticket", ""]
    if spec:
        lines.append("<!-- plan-spec-reviewed: 2026-07-08 v2 -->")
    if soundness:
        lines.append("<!-- plan-soundness-reviewed: 2026-07-08 v1 -->")
    lines.extend(["", "body text"])
    return "\n".join(lines)


def stub_fetch_plan(
    monkeypatch: pytest.MonkeyPatch,
    body: str | None,
    *,
    target: str = "cw.reconcile.gate_recipes.fetch_approved_plan_comment",
) -> None:
    """Patch ``fetch_approved_plan_comment`` at *target* to return *body*.

    Default target matches ``gate_recipes``' module-level import binding;
    pass ``target="cw.dev_queue.lifecycle.fetch_approved_plan_comment"`` to stub the
    binding ``_plan_is_reviewed`` reads instead (#968).
    """
    monkeypatch.setattr(target, lambda _ticket_id, **_k: body)


def _make_escalation(**overrides: object) -> EscalationMetadata:
    """Minimal-but-valid EscalationMetadata with keyword overrides (#1237)."""
    kwargs: dict[str, object] = {
        "target_reviewer": "Perf Reviewer",
        "evidence_quote": "def broken():",
    }
    kwargs.update(overrides)
    return EscalationMetadata.model_validate(kwargs)


def _finding_kwargs(**overrides: object) -> dict[str, object]:
    """Full kwargs for a valid Finding (#1237).

    Shared by :func:`_make_finding` and by tests that need the raw dict
    (e.g. ``Finding.model_construct(**_finding_kwargs(...))`` to bypass
    Pydantic validation) — a single source of truth so the two never drift.
    Defaults line up with ``_make_diff``: ``evidence`` appears in the diff
    text, ``file`` is a changed file, and the line range is a changed line.
    """
    kwargs: dict[str, object] = {
        "severity": "MUST_FIX",
        "file": "src/cw/foo.py",
        "line_start": 10,
        "line_end": 10,
        "summary": "Bug here",
        "consequence": "It breaks",
        "suggested_fix": "Fix it",
        "evidence": "def broken():",
        "confidence": "HIGH",
        "escalation": None,
    }
    kwargs.update(overrides)
    return kwargs


def _make_finding(**overrides: object) -> Finding:
    """Minimal-but-valid Finding with keyword overrides (#1237)."""
    return Finding.model_validate(_finding_kwargs(**overrides))


def _make_reviewer_doc(
    *findings: Finding, **overrides: object
) -> ReviewerFindingsDocument:
    """Minimal-but-valid ReviewerFindingsDocument wrapping *findings* (#1237)."""
    kwargs: dict[str, object] = {
        "reviewer_role": "Test Reviewer",
        "status": "ok",
        "detail": "reviewed; no issues found.",
        "findings": list(findings),
    }
    kwargs.update(overrides)
    return ReviewerFindingsDocument.model_validate(kwargs)


def _make_diff(*added_lines: str, **overrides: object) -> CapturedDiff:
    """Minimal-but-valid CapturedDiff (#1237, restructured #1236).

    Positional args are added ("+"-prefixed) content lines. ``files`` maps a
    changed file path to its list of changed line numbers; ``extra_text`` is
    appended verbatim so context/removed lines can be exercised.

    Populates ``file_diffs`` (per-file hunk text, for prompt inlining and the
    file-level evidence fallback) and ``file_line_text`` (per-file
    ``{line_number: content}`` for the added lines) alongside the flat ``text``,
    so every call site keeps passing under the per-file/per-line
    ``_classify_finding``. Line numbers are paired with ``added_lines`` via a
    GLOBAL position counter shared across every file (not reset per file), so
    each file genuinely gets distinct content when multiple files are passed
    in ``files`` — MUST_FIX 3 (#1236): the previous per-file-reset ``enumerate``
    gave every file's first claimed line the same ``lines[0]`` text, so a
    "stolen from another file" R6 regression test could never actually prove
    file-scoping (the stolen evidence wasn't genuinely present in ANY file's
    structured map). The last content repeats if the combined line count
    across all files exceeds ``len(added_lines)``, keeping ``files[f] ==
    sorted(file_line_text[f])`` an invariant.

    ``file_window_text`` (#1738) is set equal to ``file_line_text`` — this
    helper never generates context lines (every body line is ``+``-prefixed),
    so it has no distinct content to contribute to the hunk-context superset;
    tests that need genuine context-line content use the real
    ``_parse_unified_diff`` parser against a real diff instead (see
    ``tests/test_review_findings.py``'s ``_pr1729_captured_diff``).
    """
    lines = added_lines or ("def broken():",)
    files = overrides.get("files", {"src/cw/foo.py": [10]})
    extra_text = str(overrides.get("extra_text", ""))
    assert isinstance(files, dict)
    header = "\n".join(f"+++ b/{path}" for path in files)
    body = "\n".join(f"+{line}" for line in lines)
    text = f"{header}\n{body}\n{extra_text}"
    file_diffs: dict[str, str] = {}
    file_line_text: dict[str, dict[int, str]] = {}
    pos = 0
    for path, line_nums in files.items():
        per_file: dict[int, str] = {}
        for ln in line_nums:
            per_file[ln] = lines[pos] if pos < len(lines) else lines[-1]
            pos += 1
        file_line_text[path] = per_file
        file_body = "\n".join(f"+{per_file[ln]}" for ln in line_nums)
        file_diffs[path] = f"+++ b/{path}\n{file_body}\n{extra_text}"
    return CapturedDiff(
        text=text,
        files=files,
        file_diffs=file_diffs,
        file_line_text=file_line_text,
        file_window_text=dict(file_line_text),
    )


@pytest.fixture(autouse=True)
def tmp_config_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Redirect every cw state/config path to ``tmp_path``.

    Autouse so no test can accidentally touch ``~/.local/share/cw`` or
    ``~/.config/cw``. Consumers read paths via ``cw.config`` accessor
    functions, so patching the module-level constants here reaches every
    caller — individual test files should not need to patch module-local
    bindings. Attribute names must match exactly; any drift fails loudly
    rather than being swallowed.
    """
    config_dir = tmp_path / ".config" / "cw"
    state_dir = tmp_path / ".local" / "share" / "cw"
    config_dir.mkdir(parents=True)
    state_dir.mkdir(parents=True)

    monkeypatch.setattr("cw.config.CONFIG_DIR", config_dir)
    monkeypatch.setattr("cw.config.STATE_DIR", state_dir)
    monkeypatch.setattr("cw.config.CLIENTS_FILE", config_dir / "clients.yaml")
    monkeypatch.setattr("cw.config.STATE_FILE", state_dir / "sessions.json")
    monkeypatch.setattr("cw.config.EVENTS_DIR", state_dir / "events")
    monkeypatch.setattr("cw.config.HISTORY_DIR", state_dir / "history")
    monkeypatch.setattr("cw.config.PR_WATCHER_DIR", state_dir / "pr_watcher")
    monkeypatch.setattr("cw.config.REVIEW_MONITOR_DIR", tmp_path / "review-monitor")
    monkeypatch.setattr(
        "cw.config.ORCHESTRATOR_CONFIG_DIR", tmp_path / ".claude-workspace"
    )
    monkeypatch.setattr(
        "cw.config.ORCHESTRATOR_CONFIG_FILE",
        tmp_path / ".claude-workspace" / "orchestrator.yaml",
    )
    monkeypatch.setattr("cw.config.DEV_QUEUE_FILE", state_dir / "dev_queue.json")
    monkeypatch.setattr("cw.config.DEV_QUEUE_LOCK", state_dir / ".dev_queue.lock")
    monkeypatch.setattr("cw.config.DEV_PLAN_FILE", state_dir / "dev_plan.json")
    monkeypatch.setattr("cw.config.DEV_PLAN_LOCK", state_dir / ".dev_plan.lock")
    monkeypatch.setattr("cw.config.DEV_PLAN_OUTPUT_DIR", state_dir / "plan_output")
    monkeypatch.setattr("cw.config.SESSIONS_LOCK", state_dir / ".sessions.lock")
    monkeypatch.setattr("cw.config.CLIENTS_LOCK", config_dir / ".clients.yaml.lock")
    monkeypatch.setattr(
        "cw.config.DISPATCH_LOOP_LOCK", state_dir / ".dispatch_loop.lock"
    )
    monkeypatch.setattr(
        "cw.dispatch_state.DISPATCH_STATE_FILE", state_dir / "dispatch_state.json"
    )
    monkeypatch.setattr(
        "cw.dispatch_state.DISPATCH_STATE_LOCK", state_dir / ".dispatch_state.lock"
    )
    monkeypatch.setattr(
        "cw.config.CONCURRENCY_OVERRIDE_FILE",
        state_dir / "concurrency_overrides.json",
    )
    monkeypatch.setattr(
        "cw.config.CONCURRENCY_OVERRIDE_LOCK",
        state_dir / ".concurrency_overrides.lock",
    )
    monkeypatch.setattr("cw.config.FOCUS_FILE", state_dir / "focus.json")
    monkeypatch.setattr("cw.config.FOCUS_LOCK", state_dir / ".focus.lock")

    # Redirect the native-daemon roster path so tests don't read the
    # user's real ~/.claude/daemon/roster.json. RealNativeDaemonClient
    # tolerates a missing file (returns empty set), so this isolates the
    # native side of reconcile for any test that doesn't explicitly
    # inject a fake daemon client.
    monkeypatch.setattr(
        "cw.native_daemon._ROSTER_PATH",
        tmp_path / ".claude" / "daemon" / "roster.json",
    )

    # Stub _claude_agents_json so tests don't invoke the real ``claude``
    # binary. Tests that want specific liveness behaviour override this with
    # their own monkeypatch.setattr call; pytest patches stack and the
    # test-level patch wins.
    monkeypatch.setattr(
        "cw.reconcile.core._claude_agents_json",
        list,
    )

    return tmp_path


@pytest.fixture(autouse=True)
def _mock_push_notification(monkeypatch: pytest.MonkeyPatch) -> None:
    """Stop tests from firing real desktop notifications.

    ``cw.notify.fire_push_notification`` spawns a daemon thread that shells
    out to ``notify-send`` and ``peon.sh``. On a machine with a window manager
    that means every reconcile attention-path under test floods the desktop
    with real notifications (and can wedge the WM). Every production call site
    (``reconcile.idle``/``tasks``/``salvage``) reaches the helper through the
    re-export at ``cw.reconcile._deps.fire_push_notification``, so patching that
    one seam autouse guarantees no test fires for real — even ones that forget
    to mock it themselves.

    Tests that assert on the call (``test_reconcile.py``) re-patch the same name
    inside the test; pytest patches stack and the test-level patch wins.
    ``test_notify.py`` exercises the real helper via ``cw.notify`` directly and
    is unaffected. Attribute name must match exactly; drift fails loudly.
    """
    monkeypatch.setattr(
        "cw.reconcile._deps.fire_push_notification",
        MagicMock(name="fire_push_notification"),
    )


@pytest.fixture(autouse=True)
def _mock_gh_availability(monkeypatch: pytest.MonkeyPatch) -> None:
    """Default the fleet-wide gh-availability probe to 'available' (RFC 0011 A5).

    Sibling of ``_mock_push_notification``: ``dispatch_tick``'s per-client
    availability gate calls ``check_gh_availability``, which shells out to a
    real ``gh auth status`` subprocess. Without a default, every existing
    dispatch test would depend on the host machine's live gh auth state (and
    pay a real subprocess per tick). Patching the ``cw.dispatch`` binding
    autouse guarantees no dispatch test probes for real; the fleet reads as
    available unless a test overrides this seam. ``TestAvailabilityPreflightGate``
    re-patches the same name via ``_force_gh_unavailable`` and pytest's patch
    stacking lets the test-level patch win. ``test_gh.py`` exercises the real
    helper via ``cw.gh`` directly and is unaffected.
    """
    monkeypatch.setattr("cw.dispatch.gating.check_gh_availability", lambda **_kw: True)


@pytest.fixture(autouse=True)
def _mock_ssh_key_available(monkeypatch: pytest.MonkeyPatch) -> None:
    """Default the SSH-agent-key preflight probe to 'available' (#927).

    Sibling of ``_mock_gh_availability``: ``dispatch_tick``'s SSH-key gate
    calls ``check_ssh_key_available``, which shells out to a real ``ssh-add
    -l`` subprocess. Without a default, every existing dispatch test would
    depend on the host machine's live ssh-agent state. Patching the
    ``cw.dispatch.gating`` binding autouse guarantees no dispatch test probes
    for real; the key reads as available unless a test overrides this seam.
    ``TestSshKeyPreflightGate`` re-patches the same name via
    ``_force_ssh_key_unavailable`` and pytest's patch stacking lets the
    test-level patch win. ``test_ssh.py`` exercises the real helper via
    ``cw.ssh`` directly and is unaffected.
    """
    monkeypatch.setattr(
        "cw.dispatch.gating.check_ssh_key_available", lambda **_kw: True
    )


@pytest.fixture(autouse=True)
def _mock_codex_capability_probe(monkeypatch: pytest.MonkeyPatch) -> None:
    """Default the codex filesystem-capability fingerprint probe (#1709).

    Sibling of ``_mock_gh_availability``: without this, every test that reaches
    ``_prepare_review_pass`` for the first time in a process would shell out to
    the *host machine's* real ``codex --version``, making the runtime
    fingerprint — and therefore cache-hit/miss behavior — depend on whatever
    happens to be installed. Patching both seams autouse makes the fingerprint
    deterministic; the probe itself still runs for real against the mocked
    boundary, which is the point of the idiom.

    Note the patch targets are ``_capability``'s own module-level seam
    functions, NOT ``cw.codex_review._capability.subprocess.run`` /
    ``.shutil.which``: those attribute paths resolve to the *global*
    ``subprocess``/``shutil`` module objects, so patching them autouse would
    replace ``subprocess.run`` process-wide and break every git helper in this
    suite. See ``_capability._run_codex_version``'s docstring.

    ``tests/test_codex_capability.py`` re-patches the same two names directly
    for its binary-absent/timeout/non-zero-exit/unparseable cases; pytest's
    patch stack lets the test-level patch win.
    """
    monkeypatch.setattr(
        "cw.codex_review._capability._which_codex", lambda: "/usr/bin/codex"
    )
    monkeypatch.setattr(
        "cw.codex_review._capability._run_codex_version",
        lambda _timeout_seconds: subprocess.CompletedProcess(
            args=[], returncode=0, stdout="codex-cli 0.147.0\n", stderr=""
        ),
    )


@pytest.fixture(autouse=True)
def _hide_optional_binaries(
    request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Default optional external CLIs to ABSENT so tests reproduce CI (#1753).

    Sibling of ``_mock_codex_capability_probe``, but at a different layer:
    ``CodexExecutor.spawn()``'s pre-flight (``cw.executor.shutil.which`` at
    ``src/cw/executor.py:884``) and ``codex_capability_diagnosis()``
    (``executor.py:141``) call the bare ``shutil.which("codex")`` directly —
    there is no bespoke seam function to patch the way the fixtures above
    patch ``_which_codex``. ``opencode_runner.opencode_available()``
    (``src/cw/opencode_runner.py:96``) does the same for ``"opencode"``.
    Since ``import shutil`` binds every call site to the one process-wide
    ``shutil`` module object, patching ``shutil.which`` itself — filtered by
    ``_OPTIONAL_BINARY_DENYLIST`` — covers every unseamed call site at once,
    without a real ``codex``/``opencode`` on the dev machine's ``PATH``
    silently masking the exact CODEX_NOT_FOUND branch that shipped red in CI
    (#1727/#1752). This fixture subsumes and replaces the local
    ``cw.executor.shutil.which`` monkeypatch that used to live in
    ``tests/test_dispatch.py``'s ``TestCodexSpawnDoesNotBlockDispatch``.

    Escape hatch: ``@pytest.mark.binary_on_path("codex")`` makes a
    denylisted binary look present (a deterministic ``/usr/bin/<name>``),
    not merely un-hidden — a test that just stops hiding it would still
    depend on the *real* binary being installed, reintroducing the original
    bug for any runner that opts in without one.

    ``@pytest.mark.integration``-marked tests are exempt entirely: those
    tests intentionally shell out to real external tools (tmux, cmux,
    ``claude --bg``), so this fixture no-ops for them.
    """
    if request.node.get_closest_marker("integration") is not None:
        return
    marker = request.node.get_closest_marker("binary_on_path")
    forced_present = set(marker.args) if marker is not None else set()
    real_which = shutil.which

    def _guarded_which(
        cmd: str, mode: int = os.F_OK | os.X_OK, path: str | None = None
    ) -> str | None:
        if cmd in _OPTIONAL_BINARY_DENYLIST:
            return f"/usr/bin/{cmd}" if cmd in forced_present else None
        return real_which(cmd, mode, path)

    monkeypatch.setattr("shutil.which", _guarded_which)


@pytest.fixture(scope="session", autouse=True)
def _guard_no_real_claude_projects_writes() -> Iterator[None]:
    """Fail the suite if a test leaked a directory into the REAL projects dir.

    ``cw._util.claude_project_dir()`` resolves via ``Path.home()`` directly,
    not through ``queue_peek.CLAUDE_PROJECTS`` / ``queue_peek.CW_STATE`` — so a
    test fixture that redirects only those two module constants (as
    ``patched_peek`` did before this guard existed) leaves that call path
    writing into the real ``~/.claude/projects/`` (GH #1736).

    This fixture is a safety net, not the fix: setup/teardown here run outside
    any individual test's ``monkeypatch`` context, so ``Path.home()`` is still
    the real, unpatched home. It snapshots the real directory's entries at
    session start and again at session end, then splits any new entries by
    whether their name matches the ``tmp-pytest``/``pytest-of`` signature this
    bug class produces (``tmp_path``-rooted worktrees run through the
    unpatched ``claude_project_dir``):

    - Entries matching the signature fail the suite — this is the regression
      guard for #1736.
    - Entries not matching it only warn, since concurrent Claude Code sessions
      routinely write into this same real directory while this suite runs,
      and failing on that would make suite exit status depend on unrelated
      activity outside this repo.

    Nothing is deleted here (out of scope). A structurally stronger fix —
    redirecting ``HOME`` for the entire suite by construction, so this class
    of leak becomes impossible rather than caught after the fact — is tracked
    as a follow-up: GH #1756.
    """
    real_projects = Path.home() / ".claude" / "projects"
    before = (
        {p.name for p in real_projects.iterdir()} if real_projects.exists() else set()
    )
    yield
    after = (
        {p.name for p in real_projects.iterdir()} if real_projects.exists() else set()
    )
    leaked = after - before
    suspect = {name for name in leaked if "tmp-pytest" in name or "pytest-of" in name}
    other = leaked - suspect
    if other:
        warnings.warn(
            f"New entries appeared under the real {real_projects} during the "
            f"test session that don't match the known GH #1736 leak "
            f"signature: {sorted(other)}. Not failing the suite on these since "
            "concurrent Claude Code activity routinely writes here.",
            stacklevel=2,
        )
    assert not suspect, (
        f"Test suite leaked directories into the REAL {real_projects} "
        f"(GH #1736): {sorted(suspect)}. A test resolved "
        "cw._util.claude_project_dir() without redirecting Path.home() via "
        "the HOME env var -- see the patched_peek fixture in "
        "tests/test_queue_peek.py for the pattern."
    )


@pytest.fixture
def tmp_state_dir(tmp_config_dir: Path) -> Path:
    """Return the state directory within tmp_config_dir."""
    return tmp_config_dir / ".local" / "share" / "cw"


@pytest.fixture
def tmp_events_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Redirect cw.config.EVENTS_DIR to tmp_path."""
    events_dir = tmp_path / ".local" / "share" / "cw" / "events"
    events_dir.mkdir(parents=True)
    monkeypatch.setattr("cw.config.EVENTS_DIR", events_dir)
    return events_dir


@pytest.fixture
def sample_client(tmp_path: Path) -> ClientConfig:
    """A ClientConfig pointing at tmp_path."""
    workspace = tmp_path / "workspace" / "test-project"
    workspace.mkdir(parents=True)
    return ClientConfig(
        name="test-client",
        workspace_path=workspace,
        default_branch="main",
    )


@pytest.fixture
def sample_session(sample_client: ClientConfig) -> Session:
    """A Session with known values."""
    return Session(
        id="abcd1234",
        name="test-client/impl",
        client="test-client",
        purpose=SessionPurpose.IMPL,
        status=SessionStatus.ACTIVE,
        workspace_path=sample_client.workspace_path,
        surface_ref="impl",
        started_at=datetime(2025, 1, 15, 10, 0, 0, tzinfo=UTC),
    )


@pytest.fixture
def sample_state(sample_client: ClientConfig) -> CwState:
    """A CwState with a mix of active/backgrounded/completed sessions."""
    return CwState(
        sessions=[
            Session(
                id="sess0001",
                name="test-client/impl",
                client="test-client",
                purpose=SessionPurpose.IMPL,
                status=SessionStatus.ACTIVE,
                workspace_path=sample_client.workspace_path,
                started_at=datetime(2025, 1, 15, 10, 0, 0, tzinfo=UTC),
            ),
            Session(
                id="sess0002",
                name="test-client/idea",
                client="test-client",
                purpose=SessionPurpose.IDEA,
                status=SessionStatus.BACKGROUNDED,
                workspace_path=sample_client.workspace_path,
                started_at=datetime(2025, 1, 15, 9, 0, 0, tzinfo=UTC),
                backgrounded_at=datetime(2025, 1, 15, 11, 0, 0, tzinfo=UTC),
            ),
            Session(
                id="sess0003",
                name="other-client/impl",
                client="other-client",
                purpose=SessionPurpose.IMPL,
                status=SessionStatus.COMPLETED,
                workspace_path=sample_client.workspace_path,
                started_at=datetime(2025, 1, 14, 8, 0, 0, tzinfo=UTC),
            ),
        ]
    )


@pytest.fixture
def mock_native_daemon() -> FakeNativeDaemonClient:
    """A FakeNativeDaemonClient for testing daemon-origin spawn and reconcile."""
    return FakeNativeDaemonClient()


@pytest.fixture
def capture_events(
    monkeypatch: pytest.MonkeyPatch,
) -> Callable[..., list[CapturedEvent]]:
    """Patch ``record_event`` on an arbitrary module and capture its calls.

    ``monkeypatch.setattr`` patches a name by the *calling* module's binding,
    so a test that needs to observe events emitted from ``cw.dev_queue`` must
    patch ``cw.dev_queue.record_event`` — the ``capture_event`` closures in
    ``test_dispatch.py`` that patch ``cw.dispatch.routing.record_event`` will
    NOT see events emitted from ``cw.dev_queue``. This factory patches
    ``<module_path>.record_event`` and returns a list that accumulates
    ``(event_type, payload, correlation_id)`` tuples for each emit, optionally
    filtered to a single ``event_type``.

    Call it once per module you want to observe; a test that spans two producer
    modules (e.g. the dispatch finalize-regress path, which emits from both
    ``cw.dispatch.routing`` and ``cw.dev_queue``) calls it twice with distinct
    lists.
    """

    def _factory(
        module_path: str,
        event_type: OrchestratorEventType | None = None,
    ) -> list[CapturedEvent]:
        captured: list[CapturedEvent] = []

        def _capture(
            etype: OrchestratorEventType,
            payload: dict[str, Any] | None = None,
            *,
            correlation_id: str | None = None,
        ) -> None:
            if event_type is None or etype == event_type:
                captured.append((etype, payload or {}, correlation_id))

        monkeypatch.setattr(f"{module_path}.record_event", _capture)
        return captured

    return _factory


def _clean_git_env() -> dict[str, str]:
    """``os.environ`` with ``GIT_*`` vars stripped.

    Shared by ``make_git_repo`` and the live codex contract suite's own
    ``git`` helper (``tests/test_codex_contract_live.py``) so a nested git
    invocation never inherits a wrapping git call's env (e.g. ``GIT_DIR``).
    """
    return {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}


@pytest.fixture
def make_git_repo(tmp_path: Path) -> Callable[..., Path]:
    """Factory fixture to create git repos in tmp_path.

    Initialises with a single empty commit on ``main`` so callers that
    invoke ``git worktree add`` (notably dispatch / pr_responder tests)
    have a real commit to branch from. Sets per-repo user.name/email so
    the commit succeeds without a global git config (CI runners often
    lack one).

    The keyword-only ``base`` overrides the parent directory (default
    ``tmp_path``). The live codex contract suite (#1238) passes a home-tree
    base because snap-confined codex cannot reach ``/tmp``; every pre-existing
    positional caller keeps its exact ``tmp_path``-relative behavior.
    """

    def _make(name: str, *, base: Path | None = None) -> Path:
        repo = (base if base is not None else tmp_path) / name
        repo.mkdir(parents=True, exist_ok=True)
        clean_env = _clean_git_env()

        def _git(*args: str) -> None:
            subprocess.run(
                ["git", "-C", str(repo), *args],
                capture_output=True,
                check=True,
                env=clean_env,
            )

        _git("init", "-b", "main")
        _git("config", "user.email", "test@example.com")
        _git("config", "user.name", "cw test")
        _git("commit", "--allow-empty", "-m", "initial")
        return repo

    return _make
