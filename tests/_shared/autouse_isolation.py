"""Every function-scope autouse fixture of the suite: config/HOME isolation,
tool-launch and raw-git guards, host-probe defaults and latch resets.
Moved from ``tests/conftest.py`` (#2668).

Same-holder autouse fixtures run in alphabetical order; do not add
function-scope autouse fixtures elsewhere (pinned by
tests/test_shared_layer.py).
"""

from __future__ import annotations

import os
import shutil
import tempfile
from pathlib import Path
from typing import TYPE_CHECKING
from unittest.mock import MagicMock

import pytest

import tests._session_home
from cw import _lock_hold as lock_hold_module
from cw.disk import DiskUsage, InodeUsage
from cw.opencode_runner import _supports_v1_flags
from tests import _git_ceiling, _git_sandbox, _host_probe_guard, _tool_fakes
from tests import _lock_invariants as lock_invariants
from tests import _real_cw_guard as real_cw_guard
from tests._shared.workflow_text import _REPO_ROOT

if TYPE_CHECKING:
    from collections.abc import Iterator

    from tests._host_probe_guard import LaunchRecorder
    from tests._review_debt_helpers import CodexDeferralCall


# Optional external CLIs whose presence a test must not silently assume
# (#1753). git is intentionally excluded — universally present, and much
# of the suite shells out to it directly.
_OPTIONAL_BINARY_DENYLIST: frozenset[str] = frozenset({"codex", "opencode"})


# External CLIs the unit tier must never launch for real (#3078). Wider than
# the ``shutil.which`` masking set above (gh and uv are present on CI, so they
# still resolve), but launching any of these outside the test root raises
# ``ToolLaunchBlockedError``. Use the fakes in ``tests/_tool_fakes.py``.
_GUARDED_TOOLS: frozenset[str] = _host_probe_guard.GUARDED_TOOLS


# Whether the raw-git guard also refuses a ``src/cw`` git launch whose cwd is
# outside the basetemp (#3078). Off: the audit run with it on tripped 89
# distinct tests (mostly ``src`` probes run from the process cwd), above the
# plan's enforce-at-most-10 threshold. The env check for ``src`` launches
# stays on either way.
_ENFORCE_SRC_GIT_CONTAINMENT = False


@pytest.fixture(autouse=True)
def _lock_hold_isolation(monkeypatch: pytest.MonkeyPatch) -> None:
    """Give each test fresh lock-hold recorder state with recording on (#2637).

    The pending notes and the state-I/O accumulator are thread-local module
    state, so a test that leaves either behind would leak into the next test's
    first record. ``RECORD_ENABLED`` is read once at import from
    ``CW_LOCK_HOLD_RECORD``; pinning it keeps a developer's shell setting out of
    the suite.
    """
    monkeypatch.setattr("cw._lock_hold._STATE", lock_hold_module._ThreadState())
    monkeypatch.setattr("cw._lock_hold.RECORD_ENABLED", True)


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
    monkeypatch.setattr(
        "cw.config.CODEX_LEGACY_RECOVERY_FILE", state_dir / "codex_legacy_recovery.json"
    )
    monkeypatch.setattr(
        "cw.config.CODEX_LEGACY_RECOVERY_LOCK",
        state_dir / ".codex_legacy_recovery.lock",
    )

    # Redirect the native-daemon roster path so tests don't read the
    # user's real ~/.claude/daemon/roster.json. RealNativeDaemonClient
    # tolerates a missing file (returns empty set), so this isolates the
    # native side of reconcile for any test that doesn't explicitly
    # inject a fake daemon client.
    monkeypatch.setattr(
        "cw.native_daemon._ROSTER_PATH",
        tmp_path / ".claude" / "daemon" / "roster.json",
    )

    # Redirect the #2226 user-level Stop-hook scan away from the operator's
    # real ~/.claude, so `cw doctor` tests see a clean host regardless of what
    # the machine running them has installed. (The sibling
    # doctor.versions._CLAUDE_SETTINGS_PATH and doctor.skills_drift._CLAUDE_HOME
    # seams are not patched here: they bind at import time under the session
    # HOME set by tests/_session_home.py, so they miss the real ~/.claude by
    # construction (#1756).)
    monkeypatch.setattr("cw.doctor.user_level_hooks._CLAUDE_HOME", tmp_path / ".claude")

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
def _stub_codex_deferral_filing(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep codex loop and confirm rounds off the real review-debt filer (#3137).

    A codex round that records a SHOULD_FIX defer would otherwise reach the
    real ``run_debt_filing`` (and ``gh``) from every test. Tests that want the
    recorded calls take the ``codex_deferral_calls`` fixture.
    """
    from tests._review_debt_helpers import stub_file_codex_deferrals

    stub_file_codex_deferrals(monkeypatch)


@pytest.fixture
def codex_deferral_calls(
    monkeypatch: pytest.MonkeyPatch,
) -> list[CodexDeferralCall]:
    """The codex defer-filing calls recorded by the autouse stub (#3137)."""
    from tests._review_debt_helpers import stub_file_codex_deferrals

    return stub_file_codex_deferrals(monkeypatch)


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
def _clear_park_config_cache() -> Iterator[None]:
    """Reset the abandoned-exit park gate's per-process config memo (#2135).

    ``cw.reconcile.abandoned_exit.park_gate_open`` memoizes the resolved
    config for the life of the (short-lived) Stop-hook process. A test process
    is long-lived and swaps ``tmp_config_dir`` per test, so a memo carried
    across tests would answer for a config that no longer exists.
    """
    from cw.reconcile.abandoned_exit import clear_park_config_cache

    clear_park_config_cache()
    yield
    clear_park_config_cache()


@pytest.fixture(autouse=True)
def _reset_repair_latches() -> Iterator[None]:
    """Reset the conflict-repair recipes' in-process attempt latches (#2771, #2917).

    Each latch is module-level process state, so an entry one test leaves
    behind would block (or count against) another test's repair of the same
    ``(client, ticket_id, pr_url)``, including tests that reach a recipe only
    through ``reconcile(dispatch_review_jobs=True)``.
    """
    from cw.reconcile.review_recipes.changelog_repair import (
        _LATCH as _CHANGELOG_LATCH,
    )
    from cw.reconcile.review_recipes.mechanical_repair import (
        _LATCH as _MECHANICAL_LATCH,
    )

    latches = (_CHANGELOG_LATCH, _MECHANICAL_LATCH)
    for latch in latches:
        latch.clear()
    yield
    for latch in latches:
        latch.clear()


@pytest.fixture(autouse=True)
def _reset_dirty_check_outage_state() -> Iterator[None]:
    """Reset the dirty-check pre-pass's in-process outage state (#2662).

    ``_ANNOUNCED_OUTAGES`` (the edge latch of the ``worktree_probe_unavailable``
    signal) and ``_DEFERRED_LAST_TICK`` (the sessions ordered last by the next
    capture pass) are module-level process state, so one test's outage would
    silence another test's first signal or reorder its capture pass.
    """
    from cw.reconcile import dirty_checks

    dirty_checks._ANNOUNCED_OUTAGES.clear()
    dirty_checks._DEFERRED_LAST_TICK.clear()
    yield
    dirty_checks._ANNOUNCED_OUTAGES.clear()
    dirty_checks._DEFERRED_LAST_TICK.clear()


@pytest.fixture(autouse=True)
def _reset_stop_carryover() -> Iterator[None]:
    """Reset the post-lock stop carry-over (#2551).

    The carry-over is module-level process state, so a stop one test leaves
    carried would run first in (and count against the cap of) another test's
    drain, and would make ``is_surface_stop_queued`` answer for a ref the
    other test never queued.
    """
    from cw.reconcile.deferred import _CARRYOVER

    _CARRYOVER.clear()
    yield
    _CARRYOVER.clear()


@pytest.fixture(autouse=True)
def _reset_worker_tmp_last_attempt() -> Iterator[None]:
    """Reset the fallback-tmp retention sweep's in-process last-attempt stamp (#2682).

    The stamp is module-level process state, so a sweep one test ran would make
    the next test's real-clock ``auto_sweep_fallback_tmp`` call read as not due.
    """
    from cw.worker_tmp_retention import _LAST_ATTEMPT

    _LAST_ATTEMPT.clear()
    yield
    _LAST_ATTEMPT.clear()


@pytest.fixture(autouse=True)
def _mock_gh_availability(monkeypatch: pytest.MonkeyPatch) -> None:
    """Default the fleet-wide gh-availability probe to 'available' (RFC 0011 A5).

    Sibling of ``_mock_push_notification``: ``dispatch_tick``'s per-client
    availability gate calls ``check_gh_availability``, which shells out to a
    real ``gh auth status`` subprocess. Without a default, every existing
    dispatch test would depend on the host machine's live gh auth state (and
    pay a real subprocess per tick). Patching the
    ``cw.dispatch.gating.availability`` binding autouse guarantees no dispatch
    test probes for real; the fleet reads as available unless a test overrides
    this seam. ``TestAvailabilityPreflightGate``
    re-patches the same name via ``_force_gh_unavailable`` and pytest's patch
    stacking lets the test-level patch win. ``test_gh.py`` exercises the real
    helper via ``cw.gh`` directly and is unaffected.
    """
    monkeypatch.setattr(
        "cw.dispatch.gating.availability.check_gh_availability", lambda **_kw: True
    )


@pytest.fixture(autouse=True)
def _mock_ssh_key_available(monkeypatch: pytest.MonkeyPatch) -> None:
    """Default the SSH-agent-key preflight probe to 'available' (#927).

    Sibling of ``_mock_gh_availability``: ``dispatch_tick``'s SSH-key gate
    calls ``check_ssh_key_available``, which shells out to a real ``ssh-add
    -l`` subprocess. Without a default, every existing dispatch test would
    depend on the host machine's live ssh-agent state. Patching the
    ``cw.dispatch.gating.ssh_key`` binding autouse guarantees no dispatch test
    probes for real; the key reads as available unless a test overrides this
    seam.
    ``TestSshKeyPreflightGate`` re-patches the same name via
    ``_force_ssh_key_unavailable`` and pytest's patch stacking lets the
    test-level patch win. ``test_ssh.py`` exercises the real helper via
    ``cw.ssh`` directly and is unaffected.
    """
    monkeypatch.setattr(
        "cw.dispatch.gating.ssh_key.check_ssh_key_available", lambda **_kw: True
    )


@pytest.fixture(autouse=True)
def _mock_push_remote_scheme(monkeypatch: pytest.MonkeyPatch) -> None:
    """Default every client's push remote to ``ssh`` so the probe is engaged (#1495).

    Sibling of ``_mock_ssh_key_available``: ``_apply_ssh_key_gate`` now
    resolves the client's push-remote scheme via ``push_remote_scheme``
    (a real ``git remote get-url --push origin`` subprocess) before deciding
    whether the SSH-key probe applies. The dispatch fixtures' fake client
    repos have no ``origin`` at all, which would resolve to ``unknown`` --
    also probe-engaging, so behaviour would match, but every tick would pay
    a subprocess for it. Pinning ``ssh`` keeps the pre-#1495 gate contract
    (``TestSshKeyPreflightGate``) exercised verbatim; ``test_ssh.py``
    exercises the real helper via ``cw.ssh`` directly and is unaffected.
    """
    monkeypatch.setattr(
        "cw.dispatch.gating.ssh_key.push_remote_scheme", lambda _path: "ssh"
    )


@pytest.fixture(autouse=True)
def _mock_disk_usage(monkeypatch: pytest.MonkeyPatch) -> None:
    """Default the claim-time disk-pressure probe to 'abundant space' (#1887).

    Sibling of ``_mock_gh_availability`` / ``_mock_ssh_key_available``:
    ``dispatch_tick``'s disk-pressure gate calls ``check_disk_usage``, which
    reads the *host machine's* real free space via ``shutil.disk_usage``.
    Without a default, every existing dispatch test would pass or fail
    depending on how full the CI runner's disk happens to be. Patching the
    ``cw.dispatch.gating.disk_pressure`` binding autouse guarantees no dispatch
    test probes the real filesystem; the mount reads as roomy unless a test
    overrides this seam. ``TestDiskPressurePreflightGate`` re-patches the same name via
    ``_force_disk_pressure_gated`` and pytest's patch stacking lets the
    test-level patch win. ``test_disk.py`` exercises the real helper via
    ``cw.disk`` directly and is unaffected.

    #2470 extends the same default to the inode probe (``check_inode_usage``,
    which reads the host's real ``os.statvfs``) at both of its consumer
    bindings -- the dispatch gate and the ``cw doctor`` worker-tmp check --
    plus the doctor check's own ``check_disk_usage`` binding. The inode
    default sits comfortably above both the absolute floor and the fraction
    floor. ``TestHostTmpInodePressureGate`` and the doctor below-threshold
    test re-patch their own seams per test.
    """
    roomy_disk = DiskUsage(total_gb=500.0, free_gb=250.0)
    roomy_inodes = InodeUsage(total_inodes=1_000_000, free_inodes=900_000)
    monkeypatch.setattr(
        "cw.dispatch.gating.disk_pressure.check_disk_usage", lambda _path: roomy_disk
    )
    monkeypatch.setattr(
        "cw.dispatch.gating.disk_pressure.check_inode_usage", lambda _path: roomy_inodes
    )
    monkeypatch.setattr(
        "cw.doctor.config_checks.check_disk_usage", lambda _path: roomy_disk
    )
    monkeypatch.setattr(
        "cw.doctor.config_checks.check_inode_usage", lambda _path: roomy_inodes
    )
    # #2682: the fallback-mount doctor check probes ~/.cw/tmp's mount.
    monkeypatch.setattr(
        "cw.doctor.worker_tmp_fallback.check_disk_usage", lambda _path: roomy_disk
    )
    monkeypatch.setattr(
        "cw.doctor.worker_tmp_fallback.check_inode_usage", lambda _path: roomy_inodes
    )


# Moved to ``tests/_tool_fakes.py`` (#3078). Bound here so pytest registers
# them from this plugin's namespace: the codex capability probe stays autouse
# suite-wide and ``fake_codex_job_runner`` stays requestable by name.
_mock_codex_capability_probe = _tool_fakes._mock_codex_capability_probe


fake_codex_job_runner = _tool_fakes.fake_codex_job_runner


@pytest.fixture(autouse=True)
def _isolate_global_agents_dir(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Point the agent-spec global fallback at an empty directory (#1773).

    Sibling of ``_mock_codex_capability_probe``: ``_resolve_agent_spec`` falls
    back to ``~/.claude/agents/<role>.md`` when the worktree has no usable
    repo-local copy, and a developer machine's real ``~/.claude/agents/`` is
    populated. Without this, every test that reaches ``_prepare_review_pass``
    on a tmp worktree with no ``.claude/agents/`` directory would silently read
    the *host's* specs and become host-dependent — green here, different in CI.

    Tests that need a populated global directory re-patch the same name
    themselves; pytest's patch stacking lets the test-level patch win.
    """
    global_agents = tmp_path / "isolated-global-agents"
    global_agents.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(
        "cw.codex_review._context._agent_spec._GLOBAL_AGENTS_DIR", global_agents
    )


@pytest.fixture(autouse=True)
def _isolate_home(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Give every test its own ``HOME`` at ``tmp_path / "_home"`` (#1756).

    ``tests/_session_home.py`` already moved ``HOME`` off the operator's real
    home at import time; this narrows it per test, so call-time
    ``Path.home()`` readers (``claude_project_dir``, the ``~/.cw/wt/<hash>``
    worktree base behind #2460) never see another test's files. The
    directory is ``_home``, not ``home``: many tests build their own
    ``tmp_path / "home"`` with a bare ``mkdir()``.

    No-op when the session opted out of the redirect (``CW_TEST_REAL_HOME`` or
    an ``INTEGRATION_*`` live gate), since live runs need the real home.
    """
    if tests._session_home.SESSION_HOME is None:
        return
    home = tmp_path / "_home"
    home.mkdir()
    tests._session_home.write_minimal_gitconfig(home)
    monkeypatch.setenv("HOME", str(home))


@pytest.fixture(autouse=True)
def launch_recorder(request: pytest.FixtureRequest) -> Iterator[LaunchRecorder]:
    """Fail a test whose code launched a guarded tool or raw git (#3078).

    The tool launch guard (``_hide_optional_binaries``), the raw-git guard and
    the real-``.cw/`` write guard (``_real_cw_guard``, #3113) record every
    launch or write they refuse here, and teardown fails the test on any
    record its markers do not expect -- so a block swallowed by a broad
    ``except OSError`` in ``src/cw`` still fails. Opt out a deliberate launch
    with ``@pytest.mark.tool_launch_expected(...)`` or ``raw_git_allowed``.
    Logic: ``tests/_host_probe_guard.py``.
    """
    recorder = _host_probe_guard.LaunchRecorder()
    yield recorder
    _host_probe_guard.finish(recorder, request.node)


@pytest.fixture(autouse=True)
def _hide_optional_binaries(
    request: pytest.FixtureRequest,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path_factory: pytest.TempPathFactory,
    launch_recorder: LaunchRecorder,
) -> None:
    """Default optional external CLIs to ABSENT so tests reproduce CI (#1753).

    Two layers. ``shutil.which`` masking: ``CodexExecutor.spawn()``'s
    pre-flight (``cw.executor.codex.shutil.which``) and
    ``codex_capability_diagnosis()`` call the bare ``shutil.which("codex")``
    directly, and ``opencode_runner.opencode_available()`` does the same for
    ``"opencode"``. Since ``import shutil`` binds every call site to the one
    process-wide ``shutil`` module object, patching ``shutil.which`` itself --
    filtered by ``_OPTIONAL_BINARY_DENYLIST`` -- covers every unseamed call
    site at once, without a real ``codex``/``opencode`` on the dev machine's
    ``PATH`` silently masking the CODEX_NOT_FOUND branch that shipped red in
    CI (#1727/#1752). The masking set stays ``{codex, opencode}``: ``gh`` and
    ``uv`` are present on CI, so masking them would flip the "available"
    branch of cw's own probes and make local runs differ from CI.

    Launch guard: ``shutil.which`` masking never reaches a probe that hands a
    name straight to ``subprocess`` (``opencode run --help``), so the fixture
    also refuses a launch of any ``_GUARDED_TOOLS`` binary outside the pytest
    basetemp -- by bare name, absolute path, ``shell=True``, ``executable=``,
    ``os.exec*``/``os.spawn*``/``os.system`` -- with ``ToolLaunchBlockedError``
    (``FileNotFoundError``), as a CI host without the binary does (#2664,
    #3078). A stub from ``tests/_tool_fakes.py`` under ``tmp_path`` runs. Logic
    and limits: ``tests/_host_probe_guard.py``.

    Escape hatch: ``@pytest.mark.binary_on_path("codex")`` makes a
    denylisted binary look present to ``shutil.which`` (a deterministic
    ``/usr/bin/<name>``), not merely un-hidden. It does not let a launch
    through: a forced-present tool must still be mocked at its seam.

    ``@pytest.mark.integration``-marked tests are exempt entirely: those
    tests intentionally shell out to real external tools (``claude --bg``,
    ``codex``, ``opencode``), so this fixture no-ops for them.
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
    _host_probe_guard.install_binary_launch_guard(
        monkeypatch,
        _GUARDED_TOOLS,
        forced_present,
        root=tmp_path_factory.getbasetemp(),
        recorder=launch_recorder,
    )


@pytest.fixture(autouse=True)
def _gh_unauthenticated_by_default(
    request: pytest.FixtureRequest,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path_factory: pytest.TempPathFactory,
) -> None:
    """Answer every unmocked ``gh`` as an installed, logged-out gh does (#3078).

    That is the CI runner's condition: ``gh`` is preinstalled there and no
    token reaches the test step, so ``cw.gh``'s readers (``pr_exists_for_branch``
    behind every dispatch tick, the merged-PR refs, comment posting) get a
    non-zero exit and fail open. Without this default the same calls reached
    the real GitHub API from an authenticated dev machine. Installed on
    ``subprocess.run`` (``cw.gh._sp`` is the ``subprocess`` module), so a test's
    own ``cw.gh._sp.run`` / ``subprocess.run`` patch still wins, and a test's
    ``stub_gh`` shim inside the basetemp still runs. Sibling of
    ``_mock_gh_availability``; ``integration`` tests are exempt.
    """
    if request.node.get_closest_marker("integration") is not None:
        return
    _tool_fakes.FakeGhCli(
        default=_tool_fakes.GH_UNAUTHENTICATED,
        stub_root=tmp_path_factory.getbasetemp(),
    ).install(monkeypatch)


@pytest.fixture(autouse=True)
def _absent_tools_by_default(
    request: pytest.FixtureRequest,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path_factory: pytest.TempPathFactory,
    _gh_unauthenticated_by_default: None,
) -> None:
    """Make ``subprocess.run`` of claude, codex and opencode raise ENOENT (#3078).

    None of the three is installed on the CI runner, so an unmocked one-shot
    probe there (``cw doctor``'s ``claude --version``, ``claude stop``,
    ``opencode run --help``) takes its binary-absent branch. This default gives
    every local run the same answer instead of the host binary's, without
    failing each incidental probe at teardown the way the launch guard's
    recorder would. ``Popen``/``exec`` forms and absolute host paths still
    reach the guard; a stub under the basetemp still runs. Chains after
    ``_gh_unauthenticated_by_default``; ``integration`` tests are exempt.
    """
    if request.node.get_closest_marker("integration") is not None:
        return
    _tool_fakes.AbsentToolCli(
        _GUARDED_TOOLS - {"gh", "uv", "pre-commit"},
        stub_root=tmp_path_factory.getbasetemp(),
    ).install(monkeypatch)


@pytest.fixture(autouse=True)
def _isolate_process_table(
    request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Default ``psutil.process_iter`` to an empty host process table (#2664).

    ``cw.reconcile.codex_boot._codex_processes_in`` scans the whole table and
    fails closed -- ``None``, "inconclusive", the boot pass parks -- whenever a
    process is unreadable (``AccessDenied`` reports ``None``): one foreign or
    zombie ``codex`` makes every unpatched scan on the host inconclusive, so
    the result depended on the machine, not the test.

    Sibling of ``_mock_codex_capability_probe``. Patched at ``psutil`` rather
    than at ``_codex_processes_in`` so tests that fake the table with their
    own ``psutil.process_iter`` patch keep working (a later patch wins). A
    test that needs the real reader scopes its results to pids it owns with
    ``tests/_host_probe_guard.py::scoped_process_iter``.

    ``@pytest.mark.integration``-marked tests are exempt, as for
    ``_hide_optional_binaries``.
    """
    if request.node.get_closest_marker("integration") is not None:
        return
    monkeypatch.setattr("psutil.process_iter", _host_probe_guard.empty_process_table)


def _in_initial_pid_namespace(*, proc_root: Path | None = None) -> bool:
    del proc_root
    return True


@pytest.fixture(autouse=True)
def _assume_initial_pid_namespace(monkeypatch: pytest.MonkeyPatch) -> None:
    """Default ``cw.launch_marker.in_initial_pid_namespace`` to True (#3328).

    A suite run inside a container or sandbox pid namespace would otherwise
    stamp no launch marker (``current_launch_marker()`` is None) and demote
    every dead marker to ``IN_FLIGHT``, so results would depend on where the
    suite runs. Tests of the real function import it at module top, before
    this patch, and a test that needs the demotion patches it back to False.
    """
    monkeypatch.setattr(
        "cw.launch_marker.in_initial_pid_namespace", _in_initial_pid_namespace
    )


@pytest.fixture(autouse=True)
def _codex_fix_gates_off(monkeypatch: pytest.MonkeyPatch) -> None:
    """Default the codex fix loop's post-fix quality gates to an empty plan (#3056).

    Without it every committing fix cycle in the existing suite would park
    ``codex_fix_gate_failed``: the test HOME carries no ``prep_pr_state.py``,
    and no unrelated test should spawn the detector. The plan is the zero-gate
    one with no script, which runs nothing and surfaces no friction token.
    ``tests/test_codex_fix_loop_gates.py`` restores the real functions.
    """
    from cw.codex_fix_loop.gates import GatePlan

    monkeypatch.setattr(
        "cw.codex_fix_loop.commit.plan_cycle_gates",
        lambda *_, **__: GatePlan(script=None, gates=(), why_unavailable=None),
    )
    monkeypatch.setattr(
        "cw.codex_fix_loop.commit.run_cycle_gates", lambda *_, **__: None
    )


@pytest.fixture(autouse=True)
def _reset_opencode_flag_probe() -> Iterator[None]:
    """Clear the process-sticky ``opencode run --help`` probe cache (#2664).

    ``_supports_v1_flags`` is an ``lru_cache(maxsize=1)``: whichever test
    reaches it first fixes the answer for the rest of the process. Clearing
    around every test keeps a stale ``True`` -- seeded by an ``integration``
    test that ran the real binary, or by a probe test -- from leaking.
    """
    _supports_v1_flags.cache_clear()
    yield
    _supports_v1_flags.cache_clear()


@pytest.fixture(autouse=True)
def _lock_invariants(
    request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch
) -> Iterator[None]:
    """Fail a test that breaks the ADR-0019 lock discipline (#1233).

    Records every guarded lock event and every real subprocess launched under
    ``sessions_lock``; at teardown fails on a re-entry, an order violation, an
    unallowlisted in-lock subprocess, or a leaked lock. Opt a deliberate
    violation out with ``@pytest.mark.lock_violations_expected(...)``. Logic
    and known limits: ``tests/_lock_invariants.py``.
    """
    trace = lock_invariants.install(monkeypatch)
    yield
    lock_invariants.finish(trace, request.node)


@pytest.fixture(autouse=True)
def _git_sandbox_policy(request: pytest.FixtureRequest) -> Iterator[None]:
    """Switch the runner's path containment off for ``integration`` tests.

    The live codex suite builds its repos under a home-tree base, outside the
    basetemp, and live runs keep the real ``HOME``.
    """
    live = request.node.get_closest_marker("integration") is not None
    with _git_sandbox.containment(enforced=not live):
        yield


@pytest.fixture(autouse=True)
def _confine_git_discovery(
    tmp_path_factory: pytest.TempPathFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Stop git ascending out of the pytest basetemp into an enclosing checkout.

    A dispatched worker's ``TMPDIR`` lives inside its own worktree, so a
    ``tmp_path`` meant as "not a repository" would otherwise resolve to that
    worktree (#2598). Injected at the ``Popen`` layer because every git seam
    strips ``GIT_*`` from its env. Logic: ``tests/_git_ceiling.py``.
    """
    _git_ceiling.install(monkeypatch, tmp_path_factory.getbasetemp())


@pytest.fixture(autouse=True)
def _raw_git_guard(
    request: pytest.FixtureRequest,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path_factory: pytest.TempPathFactory,
    launch_recorder: LaunchRecorder,
) -> None:
    """Refuse a git launch from test code that bypasses the runner (#3078).

    Test code launches git through ``tests/_git_sandbox.run`` (``git_in``,
    ``make_git_repo``); a launch is otherwise allowed only from ``src/cw`` or
    ``.claude/scripts`` code, and then only without ``GIT_DIR``,
    ``GIT_WORK_TREE`` or ``GIT_INDEX_FILE`` in its env. Refusals raise
    ``RawGitLaunchError`` and are recorded in ``launch_recorder``, so a broad
    ``except`` in ``src/cw`` cannot swallow one. Exempt: ``integration`` tests
    and ``@pytest.mark.raw_git_allowed``. Logic: ``tests/_git_sandbox.py``.
    """
    node = request.node
    if node.get_closest_marker("integration") or node.get_closest_marker(
        "raw_git_allowed"
    ):
        return
    _git_sandbox.install_raw_git_guard(
        monkeypatch,
        launch_recorder,
        root=tmp_path_factory.getbasetemp(),
        enforce_containment=_ENFORCE_SRC_GIT_CONTAINMENT,
    )


@pytest.fixture(autouse=True)
def _real_cw_guard(
    request: pytest.FixtureRequest,
    tmp_path_factory: pytest.TempPathFactory,
    launch_recorder: LaunchRecorder,
) -> Iterator[None]:
    """Refuse a write under this checkout's real ``.cw/`` (#3113).

    A test whose cwd is the repo root once wrote fixture deferrals into a
    dispatch worktree's ``.cw/deferred-findings.md``. Writes under the pytest
    basetemp and ``tempfile.gettempdir()`` stay allowed, because a dispatched
    worker's ``TMPDIR`` is ``<worktree>/.cw/tmp``. A refusal raises
    ``RealCwWriteError`` and is recorded in ``launch_recorder``. Exempt:
    ``integration`` tests and ``@pytest.mark.real_cw_write_allowed``. Logic
    and limits: ``tests/_real_cw_guard.py``.
    """
    node = request.node
    if node.get_closest_marker("integration") or node.get_closest_marker(
        "real_cw_write_allowed"
    ):
        yield
        return
    allowed = (
        tmp_path_factory.getbasetemp().resolve(),
        Path(tempfile.gettempdir()).resolve(),
    )
    real_cw_guard.arm(_REPO_ROOT / ".cw", allowed, launch_recorder)
    yield
    real_cw_guard.disarm()
