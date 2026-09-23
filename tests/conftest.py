"""Shared test fixtures for cw test suite."""

from __future__ import annotations

import contextlib
import fcntl
import json
import os
import shutil
import subprocess
import warnings
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, TypedDict, cast
from unittest.mock import MagicMock

import pytest
import yaml

from cw.config import load_state, save_state
from cw.disk import DiskUsage
from cw.models import (
    AGENT_SPAWN_STAMP_KEY,
    HOOK_CONTEXT_RELATIVE_PATH,
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
from cw.orchestrate import TickSummary
from cw.review_findings import (
    CapturedDiff,
    Confidence,
    DebtRecord,
    EscalationMetadata,
    Finding,
    ReviewerFindingsDocument,
    Severity,
)

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator, Mapping, Sequence

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

# Root of the repo-tracked slash-command prose that the doc-guard test files
# assert against, backing the shared ``_cmd`` reader below (#1787).
_COMMANDS_ROOT = _REPO_ROOT / ".claude" / "commands"

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


def _cmd(name: str) -> str:
    """Return the text of ``.claude/commands/<name>`` (#1787).

    Canonical reader for the doc-guard test files that assert against
    slash-command prose. Consolidates 20 byte-identical (modulo an explicit
    ``encoding="utf-8"``) private per-file copies, the same "hoist a duplicated
    private test helper into conftest.py" pattern as ``_load_workflow`` above;
    a new command-prose guard test should import this rather than adding a
    twenty-first copy. Imported by::

        test_ambiguity_scan_adopted_assumptions.py
        test_auto_dev_finalize_automerge_verification.py
        test_auto_dev_finalize_early_push.py
        test_auto_dev_finalize_semantic_resolve.py
        test_auto_dev_gate_worktree_leak.py
        test_auto_dev_intake_context_schema.py
        test_auto_dev_model_pins.py
        test_auto_dev_preflight_resolutions.py
        test_blocking_findings_comment.py
        test_bodyfile_write_tool_conformance.py
        test_completion_artifacts_per_gate.py
        test_consolidated_park.py
        test_impl_guard_staleness_docs.py
        test_impl_plan_recovery_tracker_aware.py
        test_operator_actionable_findings_comment.py
        test_plan_format_only_findings.py
        test_plan_persistence.py
        test_plan_stage_settlement.py
        test_scope_conformance_gate_docs.py
        test_sentinel_emission_discipline.py
        test_unavailability.py

    ``test_auto_dev_intake_origin_sync_retry.py`` keeps its own copy: its
    signature is genuinely divergent (zero-argument, hardcoded filename), so it
    is not a duplicate of this helper. Each file's sibling ``_agent``/``_doc``/
    ``_skill`` readers stay file-local — out of scope for #1787.
    """
    return (_COMMANDS_ROOT / name).read_text(encoding="utf-8")


def _appendix(stage: str) -> str:
    """Return the text of ``.claude/commands/auto-dev-<stage>-appendix.md`` (#1879).

    Sibling of ``_cmd`` for the core+appendix split. #1879 moved each stage
    doc's genuinely rare-path procedures (fetch-failure handling, divergence
    handling, the fix loop, CI-wait polling, ...) out of the core file the
    worker loads on every run and into a companion appendix the worker reads
    only when the named trigger condition fires. Guard tests whose pinned
    literals moved with their content assert against this reader instead of
    ``_cmd``; no assertion was dropped in the move.
    """
    return (_COMMANDS_ROOT / f"auto-dev-{stage}-appendix.md").read_text(
        encoding="utf-8"
    )


def _bash_fences(content: str) -> list[str]:
    """Return the body of every ```bash fenced block in *content* (#2141).

    Sibling of ``_cmd``/``_appendix`` above, and hoisted for the same reason
    (#1787's precedent): the guard-script doc tests in
    ``test_scope_conformance_gate_docs.py`` and
    ``test_auto_dev_finalize_semantic_resolve.py`` each carried a
    byte-identical private copy, so a fix to the (deliberately crude,
    no-markdown-parser) fence scanner could land in one and not the other.
    A new doc-guard test that needs to execute or inspect a fenced snippet
    should import this rather than adding a third copy.
    """
    fences: list[str] = []
    lines = content.splitlines()
    index = 0
    while index < len(lines):
        if lines[index].strip().startswith("```bash"):
            body: list[str] = []
            index += 1
            while index < len(lines) and not lines[index].strip().startswith("```"):
                body.append(lines[index])
                index += 1
            fences.append("\n".join(body))
        index += 1
    return fences


# Sentinel standing in for a guard script's real invocation inside an executed
# doc fence, so a test observes *whether* the fence reached the script rather
# than running it (#2141).
GUARD_FENCE_INVOKED = "INVOKED"

# The stub body planted for every interpreter a guard fence shells out to.
# `$*` rather than `$@` so the whole argument vector lands on one line, which is
# what a caller asserting "the stub received this absolute --plan path" reads.
_GUARD_STUB_BODY = f'#!/bin/sh\nprintf "%s %s\\n" "{GUARD_FENCE_INVOKED}" "$*"\n'

# The one ``cw-script-version`` marker value that may reach a guard-script
# invocation: a clean integer at the version table's minimum.
GUARD_MARKER_CURRENT = "# cw-script-version: 1\n"

# The below-minimum marker, named separately because the precedence tests plant
# it as "the *other* candidate is stale" scenery rather than as a parametrized
# case of GUARD_MARKER_BAD_CASES below.
GUARD_MARKER_STALE = "# cw-script-version: 0\n"

# A current marker on line 3, under a shebang and a docstring. The real guard
# scripts carry theirs on line 2, but the rule is "within the first 5 lines",
# and a parse anchored to one exact line number would be a different contract
# than the one the docs state (#2141 round 8).
GUARD_MARKER_CURRENT_LINE_3 = (
    '#!/usr/bin/env python3\n"""Guard script."""\n# cw-script-version: 1\n'
)

# Every marker state that MUST reach the invocation, as (id, file body) pairs.
# Companion to GUARD_MARKER_BAD_CASES: a parse tightened enough to reject
# ``marker_outside_header`` below can just as easily reject a legitimate header,
# and a bad-cases-only matrix cannot tell the two apart.
GUARD_MARKER_GOOD_CASES: tuple[tuple[str, str], ...] = (
    ("marker_first_line", GUARD_MARKER_CURRENT),
    ("marker_third_line", GUARD_MARKER_CURRENT_LINE_3),
)

# Every marker state that must NOT reach an invocation, as (id, file body)
# pairs. Hoisted here (#2141 round 6) as the union of the two private lists
# ``test_scope_conformance_gate_docs.py`` and
# ``test_auto_dev_finalize_semantic_resolve.py`` had each grown: the shorter
# list was missing ``malformed_negative``/``malformed_suffix``, so a fence whose
# marker check regressed on those was red in one module and green in the other.
#
# Anything that is not ``^[0-9]{1,6}$`` is stale by construction. A
# `grep -oE '[0-9]+'` extraction turned `1.5` into two lines, which made
# `[ ... -lt ... ]` error out and the condition evaluate false, so the script ran
# anyway (#2141 round 2). ``malformed_oversized`` is the same failure one width
# up (#2141 round 5): an unbounded ``^[0-9]+$`` accepts a 20-digit value, which
# overflows ``[ -lt ]`` ("integer expression expected"), evaluates false, and
# falls through to the invocation — so the digit count itself has to be bounded.
GUARD_MARKER_BAD_CASES: tuple[tuple[str, str], ...] = (
    ("below_minimum", GUARD_MARKER_STALE),
    ("no_marker", "import sys\n"),
    ("malformed_float", "# cw-script-version: 1.5\n"),
    ("malformed_alpha", "# cw-script-version: abc\n"),
    ("malformed_negative", "# cw-script-version: -1\n"),
    ("malformed_suffix", "# cw-script-version: 2x\n"),
    ("malformed_empty", "# cw-script-version:\n"),
    ("malformed_oversized", "# cw-script-version: 99999999999999999999\n"),
    (
        "marker_outside_header",
        # A script whose header carries no marker, but whose body mentions one
        # further down — in a docstring, a help string, or a comment about the
        # convention. ``grep -m1`` matched it anywhere in the file, so a genuinely
        # unmarked script passed on an incidental later mention (#2141 round 8).
        "#!/usr/bin/env python3\n" + "# filler\n" * 8 + "# cw-script-version: 5\n",
    ),
)


def write_guard_stub_bin(tmp_path: Path) -> Path:
    """Create a ``PATH`` directory of sentinel interpreters for a fence (#2141).

    Replaces the runner's former rewrite of the fence *text* (``uv run python``
    → ``echo INVOKED``), which silently mutated the very line under test and
    would have kept passing had the doc's invocation changed shape. The fence
    now executes exactly as the doc spells it; only the binaries it calls are
    fixtures. Both spellings in these docs are covered — ``uv run python
    "$RESOLVED"`` and the bare ``python "$RESOLVED"`` the stdlib-only
    pre-mutation guard uses — and each stub echoes the sentinel followed by its
    own argument vector, so a caller can assert on the arguments the doc passes.
    """
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(parents=True, exist_ok=True)
    for name in ("uv", "python"):
        stub = bin_dir / name
        stub.write_text(_GUARD_STUB_BODY, encoding="utf-8")
        stub.chmod(0o755)
    return bin_dir


def _placement(location: str, body: str) -> dict[str, str | None]:
    """Plant *body* at the repo-local or the global candidate location (#2141).

    The ``**kwargs`` pair a ``run_guard_fence`` caller passes to exercise one
    branch of the resolver's candidate list. Hoisted here next to
    ``write_guard_stub_bin`` (round 5): ``test_scope_conformance_gate_docs.py``
    and ``test_auto_dev_finalize_semantic_resolve.py`` each carried a
    byte-identical private copy, so a change to the runner's keyword names
    could land in one and not the other.
    """
    if location == "repo_local":
        return {"repo_local": body, "global_copy": None}
    return {"repo_local": None, "global_copy": body}


# The ``run_guard_fence`` fixture root each candidate location is planted under.
_GUARD_CANDIDATE_ROOTS = {
    "repo_local": "repo",
    "global_only": "home",
    "worktree_override": "context-worktree",
}


def guard_candidate_path(tmp_path: Path, location: str, script: str) -> str:
    """The absolute path a correct resolver must land on for *location* (#2141).

    Companion to ``_placement``: a caller that plants a body at one candidate
    asserts the stub echoed *this* path, which is what separates "the fence
    reached a script" from "the fence reached the right script". Without it a
    resolver that always picked the global copy passed the repo-local case.
    """
    return str(
        tmp_path / _GUARD_CANDIDATE_ROOTS[location] / ".claude" / "scripts" / script
    )


def substitute_fence_placeholders(fence: str, placeholders: Mapping[str, str]) -> str:
    """Replace ``<name>`` doc placeholders in *fence*, and nothing else (#2141).

    The docs spell call-site-specific values as angle-bracket placeholders
    (``<branch-name>``, ``<script>``, ``MIN_VERSION=<N>``). An executable fence
    test has to fill those in, but it must not otherwise rewrite the fence —
    every other byte is the artifact under test.
    """
    for name, value in placeholders.items():
        fence = fence.replace(f"<{name}>", value)
    return fence


def run_guard_fence(
    tmp_path: Path,
    fence: str,
    script: str,
    *,
    repo_local: str | None = None,
    global_copy: str | None = None,
    worktree_path_override: str | None = None,
    placeholders: Mapping[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    """Execute a guard-script resolver *fence* against fixture copies (#2141).

    Sibling of ``_bash_fences``, and hoisted for the same reason: the
    ``test_scope_conformance_gate_docs.py`` and
    ``test_auto_dev_finalize_semantic_resolve.py`` runners had diverged into
    two near-copies of the same substitute-and-execute technique.

    *repo_local* and *global_copy* are the file bodies to plant at
    ``<repo>/.claude/scripts/<script>`` and ``$HOME/.claude/scripts/<script>``;
    ``None`` leaves that location empty, so a caller can exercise the
    repo-local, global-only, both, and absent branches from one helper.

    *worktree_path_override*, when given, is the script body planted at a
    directory **other than** ``<repo>``, with a ``<repo>/.claude/cw-context.json``
    written to point ``worktree_path`` at it — exercising the resolver's
    context-provided-anchor branch, which no ``repo_local``/``global_copy``
    fixture reaches (both of those only ever probe paths derived from
    ``git rev-parse --show-toplevel`` or ``$HOME``, never a `cw-context.json`
    override to a third location).

    The fence is run from a **nested subdirectory** of the repo, not its root:
    the resolver must anchor its repo-local candidate to an absolute root
    (``worktree_path``/``git rev-parse --show-toplevel``) rather than probing a
    bare relative ``.claude/scripts/...``, and a runner that always executed
    from the root could not tell the two apart. The fence text itself is left
    alone apart from *placeholders* (see ``substitute_fence_placeholders``): the
    invocation is neutralised by putting sentinel ``uv``/``python`` stubs first
    on ``PATH`` instead. The per-site capture variables are echoed afterwards
    because three of the four fences assign the invocation into a command
    substitution rather than letting it print.

    This runner serves the three ``$GUARD_ROOT`` resolver sites. Step 2.5 gate 2
    derives its anchor from ``git worktree list`` instead and has its own
    real-worktree runner in ``tests/test_scope_conformance_gate_docs.py``.
    """
    repo = tmp_path / "repo"
    repo.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        ["git", "-C", str(repo), "init", "-b", "main"],
        capture_output=True,
        check=True,
        env=_clean_git_env(),
    )
    if repo_local is not None:
        scripts = repo / ".claude" / "scripts"
        scripts.mkdir(parents=True, exist_ok=True)
        (scripts / script).write_text(repo_local, encoding="utf-8")

    home = tmp_path / "home"
    home.mkdir(parents=True, exist_ok=True)
    if global_copy is not None:
        global_scripts = home / ".claude" / "scripts"
        global_scripts.mkdir(parents=True, exist_ok=True)
        (global_scripts / script).write_text(global_copy, encoding="utf-8")

    if worktree_path_override is not None:
        override_root = tmp_path / "context-worktree"
        override_scripts = override_root / ".claude" / "scripts"
        override_scripts.mkdir(parents=True, exist_ok=True)
        (override_scripts / script).write_text(worktree_path_override, encoding="utf-8")
        context_dir = repo / ".claude"
        context_dir.mkdir(parents=True, exist_ok=True)
        (context_dir / "cw-context.json").write_text(
            json.dumps({"worktree_path": str(override_root)}), encoding="utf-8"
        )

    nested = repo / "nested" / "deep"
    nested.mkdir(parents=True, exist_ok=True)

    bin_dir = write_guard_stub_bin(tmp_path)
    body = (
        substitute_fence_placeholders(fence, placeholders or {})
        + '\necho "${VERDICT-}${RESOLVE_OUTPUT-}${SCOPE_CONFORMANCE_OUTPUT-}"\n'
    )
    # Scoped to this tmp_path so the gate-2 fence's hard-coded
    # `/tmp/touched_files-$CW_SESSION` scratch write cannot collide with a
    # concurrent run; the path is literal in the doc, so it is cleaned up here
    # rather than redirected.
    session = tmp_path.name
    try:
        return subprocess.run(
            ["bash", "-c", body],
            cwd=nested,
            env={
                "HOME": str(home),
                "PATH": f"{bin_dir}:{os.environ.get('PATH', '')}",
                "CW_SESSION": session,
                "TMPWT": str(repo),
                "FORK_POINT": "HEAD",
            },
            capture_output=True,
            text=True,
            check=False,
        )
    finally:
        Path(f"/tmp/touched_files-{session}").unlink(missing_ok=True)


def _write_bin_stub(tmp_path: Path, name: str, body: str) -> Path:
    """Write ``body`` as an executable ``name`` in ``tmp_path/bin``; return the dir.

    The shared mechanics behind every fake-external-CLI helper here
    (``_stub_gh``, ``_stub_cw``) so a new one differs only in its script body.
    """
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir(exist_ok=True)
    stub = fake_bin / name
    stub.write_text(body)
    stub.chmod(0o755)
    return fake_bin


def _stub_gh(tmp_path: Path, *, exit_code: int, stdout: str = "") -> Path:
    """Write an executable ``gh`` stub into a fresh bin dir and return it (#1799).

    Hoist of the byte-identical private copy in
    test_changelog_gate_workflow.py, which is deliberately left unmodified
    (same convention as ``_load_workflow`` above); this is the canonical
    version a new workflow-guard test should import rather than adding a
    third copy. Imported today by test_release_tag_workflow.py's dry-run
    summary tests, whose script shells out to ``gh issue list``.
    """
    # Quoted heredoc ('GH_STDOUT_EOF') -- no shell interpolation of `stdout`'s
    # contents, matching how a real `gh` payload is opaque data.
    return _write_bin_stub(
        tmp_path,
        "gh",
        f"#!/bin/sh\ncat <<'GH_STDOUT_EOF'\n{stdout}GH_STDOUT_EOF\nexit {exit_code}\n",
    )


def _stub_cw(
    tmp_path: Path,
    *,
    events: Sequence[str] = (),
    delay_s: float = 0.0,
    exit_code: int = 0,
    block: bool = False,
) -> Path:
    """Write an executable fake ``cw`` into a fresh bin dir and return it (#2250).

    Stands in for ``cw event tail --follow ... --json``: ignores its flags,
    prints each pre-built JSON line in ``events`` to stdout (sleeping
    ``delay_s`` between lines), then exits ``exit_code`` -- or, with
    ``block=True``, ``exec``s a long ``sleep`` so only the caller's own timer or
    an explicit terminate ends it. ``exec`` keeps the stub's PID, so
    terminating that PID really stops it rather than orphaning a ``sleep``.

    Side files next to the stub: ``cw.args`` (one invocation arg per line) and
    ``cw.pid`` (the stub's PID), for tests asserting what was invoked and
    whether it was terminated.
    """
    lines = [
        "#!/bin/sh",
        'DIR=$(dirname "$0")',
        'printf \'%s\\n\' "$@" > "$DIR/cw.args"',
        'echo $$ > "$DIR/cw.pid"',
    ]
    for i, event in enumerate(events):
        if i and delay_s:
            lines.append(f"sleep {delay_s}")
        # Quoted heredoc -- the event JSON is opaque data, never interpolated.
        lines.extend([f"cat <<'CW_EVENT_EOF'\n{event}", "CW_EVENT_EOF"])
    lines.append("exec sleep 3600" if block else f"exit {exit_code}")
    return _write_bin_stub(tmp_path, "cw", "\n".join(lines) + "\n")


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


def _write_stop_hook_transcript(
    home: Path,
    worktree: Path,
    claude_session_id: str,
    assistant_text: str,
) -> Path:
    """Write ``<claude_session_id>.jsonl`` under *worktree*'s project dir.

    Promoted from ``TestSignalStop._write_transcript`` (``tests/test_cli.py``,
    #1692), whose 19 in-class call sites now import and call this helper
    directly. Keyed on an exact ``claude_session_id``, matching how the
    Stop-hook path actually resolves a sentinel: ``_parse_headless_sentinel``
    -> ``_parse_sentinel_from_transcript(cwd_value, csid)``
    (``cw.cli.stop_hook``) looks the transcript up by exact
    ``claude_session_id``, not by a surface_ref-prefix glob -- the mechanism
    :func:`_write_idle_transcript` above targets instead. Param order/return
    type follow that sibling's ``(home, worktree, ...) -> Path`` convention.
    """
    encoded = str(worktree).replace("/", "-").replace(".", "-")
    project_dir = home / ".claude" / "projects" / encoded
    project_dir.mkdir(parents=True, exist_ok=True)
    record = {
        "type": "assistant",
        "message": {
            "role": "assistant",
            "content": [{"type": "text", "text": assistant_text}],
        },
    }
    path = project_dir / f"{claude_session_id}.jsonl"
    path.write_text(json.dumps(record) + "\n")
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


def _seed_completed_session(
    tmp_path: Path,
    tmp_config_dir: Path,
    ticket_id: str,
    client: str = "test-client",
    status: SessionStatus = SessionStatus.TIMED_OUT,
    last_result: dict[str, object] | None = None,
    completed_at: datetime | None = None,
) -> Session:
    """Seed a TIMED_OUT or COMPLETED session for a given ticket in state.

    Hoisted from ``tests/test_spawn.py`` (#2280) so ``tests/test_codex_
    executor.py`` can seed a prior codex-review park for the same
    ``prior_attempts_summary`` machinery without duplicating the builder.
    """
    workspace = tmp_path / "workspace" / client
    workspace.mkdir(parents=True, exist_ok=True)
    sess = Session(
        name=f"{client}/auto-dev/{ticket_id}",
        client=client,
        purpose=SessionPurpose.IMPL,
        origin=SessionOrigin.DAEMON,
        status=status,
        workspace_path=workspace,
        last_result=last_result,
        completed_at=completed_at or datetime.now(UTC),
    )
    state = load_state()
    state.sessions.append(sess)
    save_state(state)
    return sess


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


def _make_tick_summary(**overrides: object) -> TickSummary:
    """Canonical ``TickSummary`` builder with keyword overrides (#1875).

    Follows the same dict-merge + ``model_validate`` idiom as
    ``_make_daemon_session`` / ``_make_ticket_task``. Shared so the doctor's
    on-demand loop-liveness check and the dispatch loop's proactive staleness
    watchdog -- which exercise the *same* stale+pending predicate
    (``cw.dispatch._stale_pending_clients``) -- never drift onto two
    independently-maintained builders for it.
    """
    kwargs: dict[str, object] = {
        "claimed": 0,
        "pending": 0,
        "running": 0,
        "cap": 3,
        "skip_reason": "none",
        "tick_at": datetime.now(UTC),
    }
    kwargs.update(overrides)
    return TickSummary.model_validate(kwargs)


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


# ``_write_hook_context_file``'s ``stamp`` sentinels (#2229): keep the real
# writer's seeded ``{0, None}`` stamp, or delete the key entirely (a legacy
# pre-#1646 context). Any other value replaces the stamp verbatim.
_STAMP_UNCHANGED: object = object()
_STAMP_ABSENT: object = object()


def _write_hook_context_file(
    worktree: Path,
    workspace_path: Path | None = None,
    lane: str | None = None,
    stamp: object = _STAMP_UNCHANGED,
    headless: bool = False,
) -> None:
    """Materialize ``<worktree>/.claude/cw-context.json`` via the real writer.

    Hoisted from ``test_cli_guard.py``'s private ``_write_context`` (#1646) so
    the guard hook tests and the agent-spawn-stamp hook tests share one
    materializer. Using the real ``_write_hook_context`` (rather than a
    hand-written JSON literal) is deliberate: it keeps every hook test reading
    the exact context shape production writes, including new schema fields.

    *lane* (#1946) forwards to the real writer's new ``lane`` parameter so
    ``cw guard-busy-wait``'s per-lane config tests read the same ``"lane"``
    key production stamps — an ad hoc parallel JSON writer in the test file
    is exactly the fixture drift this helper's hoist exists to prevent.

    *stamp* (#2229) overrides the seeded ``agent_spawn_stamp`` after the real
    writer runs: ``_STAMP_ABSENT`` deletes the key, any other non-default
    value replaces it, so the Stop hook's stamp-shape edge cases are seeded
    through the same file the production writer produced.

    *headless* (#2211) forwards to the real writer's ``headless`` parameter so
    ``cw agent-spawn-pre``'s spawn-shape policy — which applies only to
    headless dispatch workers — reads the same ``"headless"`` key production
    stamps, for the same anti-drift reason as *lane* above.
    """
    from cw.spawn import _write_hook_context

    _write_hook_context(
        worktree,
        session_id="sess940g",
        session_name="client-a/impl",
        client="client-a",
        purpose="impl",
        ticket_id="940",
        origin=SessionOrigin.DAEMON,
        headless=headless,
        workspace_path=workspace_path,
        lane=lane,
    )
    if stamp is _STAMP_UNCHANGED:
        return
    context_path = worktree / HOOK_CONTEXT_RELATIVE_PATH
    context = json.loads(context_path.read_text(encoding="utf-8"))
    if stamp is _STAMP_ABSENT:
        del context[AGENT_SPAWN_STAMP_KEY]
    else:
        context[AGENT_SPAWN_STAMP_KEY] = stamp
    context_path.write_text(json.dumps(context, indent=2) + "\n", encoding="utf-8")


def _headless_worktree(tmp_path: Path, name: str = "wt") -> Path:
    """A worktree whose context marks it a headless dispatch worker (#2211).

    The precondition for every ``cw agent-spawn-pre`` spawn-shape test, since
    the policy applies to headless workers and nowhere else. Lives here rather
    than in either test file because both ``test_cli_agent_spawn_stamp.py``
    and ``test_cli_subagent_policy.py`` need it, and they had grown identical
    private copies.
    """
    worktree = tmp_path / name
    worktree.mkdir()
    _write_hook_context_file(worktree, headless=True)
    return worktree


@contextlib.contextmanager
def _hold_context_lock(worktree: Path) -> Iterator[None]:
    """Hold ``<worktree>/.claude/cw-context.json.lock`` exclusively (#1946).

    Reproduces the contended-lock condition every hook write path must fail
    open on. Hoisted from ``test_cli_agent_spawn_stamp.py``'s inline
    ``fcntl.flock`` setup so the three consumers of
    ``cw.cli._hook_io._write_cw_context_locked`` share one technique instead
    of each re-deriving it — the same "don't duplicate the discipline"
    reasoning that promoted the write primitive itself into ``_hook_io``.

    Callers must also shorten ``cw.cli._hook_io._LOCK_TIMEOUT_SECS_DEFAULT``
    (patched where it is *defined*, never on a re-exporting module) so the
    bounded retry budget expires quickly.
    """
    lock_path = worktree / ".claude" / "cw-context.json.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("w") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


def _invoke_hook_command(command: str, payload: dict[str, object]) -> Any:
    """``CliRunner``-invoke hook subcommand *command* with *payload* on stdin.

    Returns the click ``Result``. Hoisted from ``test_cli_guard.py``'s private
    ``_invoke`` (#1646) and generalized over the command name, since every cw
    hook handler shares the same "JSON object on stdin, exit code is the
    contract" surface.
    """
    from click.testing import CliRunner

    from cw.cli import main

    runner = CliRunner()
    return runner.invoke(main, [command], input=json.dumps(payload))


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


def _plan_text(paths: list[str]) -> str:
    """Build a realistic plan document with a ``## Files Modified`` section.

    Shared by test_check_plan_scope_conformance.py and test_plan_files.py —
    both exercise the same ``## Files Modified`` parsing contract, one via
    the standalone .claude/scripts mirror, the other via src/cw.plan_files.
    """
    bullets = "\n".join(f"- {p} (~40 lines)" for p in paths)
    return (
        "# Implementation Plan: Something (#9999)\n\n"
        "## Patterns Found\n\n"
        "- Proposed: a thing.\n\n"
        "## Files Modified\n\n"
        f"{bullets}\n\n"
        "**Scope tier:** small\n\n"
        "## Ambiguities\n\n"
        "NO_AMBIGUITIES\n"
    )


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


class FindingKwargs(TypedDict):
    """Precisely-typed kwargs for a genuinely-valid Finding literal.

    Mirrors Finding's 10 non-defaulted-in-practice fields exactly (#1922) --
    the shape a real captured fixture needs to splat directly into
    Finding(**kwargs) and type-check under --strict. NOT for
    intentionally-invalid payloads; see _RawFindingKwargs for that.
    """

    severity: Severity
    file: str
    line_start: int | None
    line_end: int | None
    summary: str
    consequence: str
    suggested_fix: str
    evidence: str
    confidence: Confidence
    escalation: EscalationMetadata | None


class _RawFindingKwargs(TypedDict, total=False):
    """Loosely-typed kwargs bag for a possibly-invalid Finding payload (#1922).

    Every value is `object`, not the real field type: this shape exists
    solely to give Finding.model_construct(**...) splats a closed key set
    (excluding BaseModel.model_construct's `_fields_set` parameter) so mypy
    stops conservatively checking the splat against every keyword-reachable
    parameter. It intentionally does NOT constrain values -- callers
    deliberately construct invalid Findings here (bad severities, blank
    evidence) to bypass Pydantic validation and exercise defensive checks
    downstream.
    """

    severity: object
    file: object
    line_start: object
    line_end: object
    summary: object
    consequence: object
    suggested_fix: object
    evidence: object
    confidence: object
    escalation: object
    no_diff_anchor: object
    transitive_impact_evidence: object
    release_critical_exception: object
    contests_adjudication: object


def _finding_kwargs(**overrides: object) -> _RawFindingKwargs:
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
    return cast("_RawFindingKwargs", kwargs)


def _without_evidence(payload: dict[str, Any]) -> dict[str, Any]:
    """Strip the required ``evidence`` field to build a schema-invalid payload
    (#2029).

    Omission rather than a blank value: the required-field failure is the
    shape a real reviewer produces when it forgets the field entirely, and it
    is what the ticket's own acceptance criteria name. Shared by
    ``test_review_findings.py``, ``test_codex_review_roles.py``, and
    ``test_cli_review_consolidate.py``, which each built a near-identical helper
    doing this same delete against their own base-payload builder — one
    source of truth so the mechanism can't silently drift between them.
    """
    payload = dict(payload)
    del payload["evidence"]
    return payload


def _make_finding(**overrides: object) -> Finding:
    """Minimal-but-valid Finding with keyword overrides (#1237)."""
    return Finding.model_validate(_finding_kwargs(**overrides))


def _make_debt_record(**overrides: object) -> DebtRecord:
    """Minimal-but-valid DebtRecord with keyword overrides (#1837).

    Lives here alongside :func:`_make_finding` so ``test_review_debt.py`` and
    ``test_codex_fix_loop_convergence.py`` share one builder instead of each
    keeping a local copy. Defaults line up with ``_finding_kwargs``.
    """
    kwargs: dict[str, object] = {
        "fingerprint": ("src/cw/foo.py", "bug here"),
        "file": "src/cw/foo.py",
        "evidence": "def broken():",
        "summary": "Bug here",
        "suggested_follow_up": "Fix it",
        "discovery_sha": "deadbee",
        "reviewer_role": "Code Quality Reviewer",
    }
    kwargs.update(overrides)
    return DebtRecord.model_validate(kwargs)


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


def _doc_payload(*findings: dict[str, Any], **overrides: object) -> dict[str, Any]:
    """A raw ``ReviewerFindingsDocument`` dict (bypasses Pydantic construction
    so invalid payloads — e.g. a bogus severity — can be sent through the CLI).
    """
    payload: dict[str, Any] = {
        "reviewer_role": "Code Quality Reviewer",
        "status": "ok",
        "detail": "",
        "findings": list(findings),
    }
    payload.update(overrides)
    return payload


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

    # Redirect the #2226 user-level Stop-hook scan away from the operator's
    # real ~/.claude, so `cw doctor` tests see a clean host regardless of what
    # the machine running them has installed. (The sibling
    # doctor.versions._CLAUDE_SETTINGS_PATH and doctor.skills_drift._CLAUDE_HOME
    # seams are NOT patched here — a pre-existing gap, out of scope for #2226.)
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
    monkeypatch.setattr("cw.dispatch.gating.push_remote_scheme", lambda _path: "ssh")


@pytest.fixture(autouse=True)
def _mock_disk_usage(monkeypatch: pytest.MonkeyPatch) -> None:
    """Default the claim-time disk-pressure probe to 'abundant space' (#1887).

    Sibling of ``_mock_gh_availability`` / ``_mock_ssh_key_available``:
    ``dispatch_tick``'s disk-pressure gate calls ``check_disk_usage``, which
    reads the *host machine's* real free space via ``shutil.disk_usage``.
    Without a default, every existing dispatch test would pass or fail
    depending on how full the CI runner's disk happens to be. Patching the
    ``cw.dispatch.gating`` binding autouse guarantees no dispatch test probes
    the real filesystem; the mount reads as roomy unless a test overrides this
    seam. ``TestDiskPressurePreflightGate`` re-patches the same name via
    ``_force_disk_pressure_gated`` and pytest's patch stacking lets the
    test-level patch win. ``test_disk.py`` exercises the real helper via
    ``cw.disk`` directly and is unaffected.
    """
    monkeypatch.setattr(
        "cw.dispatch.gating.check_disk_usage",
        lambda _path: DiskUsage(total_gb=500.0, free_gb=250.0),
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
            args=[], returncode=0, stdout="codex-cli 0.144.5\n", stderr=""
        ),
    )


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

    Shared by ``make_git_repo``, ``git_in``, and any test that needs a
    ``GIT_*``-stripped env for a raw ``subprocess`` call, so a nested git
    invocation never inherits a wrapping git call's env (e.g. ``GIT_DIR``).
    """
    return {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}


def git_in(repo: Path, *args: str) -> str:
    """Run git in *repo* with a ``GIT_*``-stripped env, returning stripped stdout.

    Canonical runner for tests that drive a real ``make_git_repo`` repo through
    raw git commands. Consolidates four byte-identical private copies
    (``test_branch_ahead.py``, ``test_dispatch_branch_freshness.py``,
    ``test_worktree.py``, and ``test_dispatch.py``'s ``_git_in_repo``) — the
    same "hoist a duplicated private test helper into conftest.py" pattern as
    ``_cmd`` and ``commit_tracked_file``, and the further private copies
    consolidated by #2195. The env strip matters because pytest
    may itself be running inside a git hook, whose ``GIT_DIR``/``GIT_INDEX_FILE``
    would otherwise redirect the nested invocation away from *repo*.
    """
    result = subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        text=True,
        check=True,
        env=_clean_git_env(),
    )
    return result.stdout.strip()


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


def init_repo_with_remote(path: Path, remote_url: str | None) -> Path:
    """Build a minimal git repo at *path*, optionally with an ``origin`` remote.

    Raw subprocess (not ``make_git_repo``): no existing fixture sets a
    remote, and extending the widely-shared factory to add one would be a
    broader-blast-radius change than the callers need (#1198). Shared by
    ``test_pr_hydrate.py`` and ``test_preflight.py`` (#2158) so the same
    ~10-line builder isn't maintained as two independent copies.
    """
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "-C", str(path), "init"], capture_output=True, check=True)
    if remote_url is not None:
        subprocess.run(
            ["git", "-C", str(path), "remote", "add", "origin", remote_url],
            capture_output=True,
            check=True,
        )
    return path


def commit_tracked_file(worktree: Path, relpath: str, content: str = "x = 1\n") -> None:
    """Write *relpath* under *worktree* and commit it as a real tracked file.

    Shared by tests that need a ``make_git_repo`` worktree to carry tracked
    files beyond its base empty commit — cw #1915's ``build_aiderignore``
    exercises ``git ls-files`` against a real tracked-file set, and its tests
    (plus the corresponding executor spawn test) all need this same
    mkdir/write/add/commit sequence. Reuses ``_clean_git_env()`` so the nested
    git invocation doesn't inherit a wrapping git call's env.
    """
    path = worktree / relpath
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    clean_env = _clean_git_env()
    subprocess.run(
        ["git", "-C", str(worktree), "add", relpath],
        capture_output=True,
        check=True,
        env=clean_env,
    )
    subprocess.run(
        ["git", "-C", str(worktree), "commit", "-m", f"add {relpath}"],
        capture_output=True,
        check=True,
        env=clean_env,
    )


def push_commit_to_origin(
    origin: Path,
    branch: str,
    work_dir: Path,
    filename: str,
    content: str = "out-of-band\n",
) -> str:
    """Push one new commit to *branch* on the bare *origin* from a side clone.

    Simulates an out-of-band push (another machine, a sibling session, a fix
    agent's isolation worktree) that advances ``origin/<branch>`` without
    touching the workspace under test. *branch* must already exist on *origin*.

    The clone at *work_dir* is created only when *work_dir* does not yet exist;
    a second call in the same test reuses it, re-syncing to the current remote
    tip first so the new commit lands on top of whatever was pushed since.
    Writes *content* to *filename* so a test can make upstream set a value that
    differs from a local commit. Returns the pushed commit's SHA.
    """
    if not work_dir.exists():
        subprocess.run(
            ["git", "clone", str(origin), str(work_dir)],
            capture_output=True,
            text=True,
            check=True,
            env=_clean_git_env(),
        )
    git_in(work_dir, "fetch", "origin")
    git_in(work_dir, "checkout", "-B", branch, f"origin/{branch}")
    (work_dir / filename).write_text(content, encoding="utf-8")
    git_in(work_dir, "add", filename)
    git_in(
        work_dir,
        "-c",
        "user.email=test@example.com",
        "-c",
        "user.name=cw test",
        "commit",
        "-m",
        f"out-of-band {filename}",
    )
    git_in(work_dir, "push", "origin", branch)
    return git_in(work_dir, "rev-parse", "HEAD")


def tree_fingerprint(worktree: Path) -> tuple[str, str, str]:
    """``(HEAD sha, porcelain status, digest of every working-tree file)``.

    Byte-level: two equal fingerprints mean nothing moved HEAD, the index or a
    single file's content in the worktree. Hoisted (#2213 round 5) from
    ``test_reconcile_review_recipes.py`` once the dispatch claim tests needed
    the same "nothing touched the occupied worktree" proof.
    """
    import hashlib

    digest = hashlib.sha256()
    for path in sorted(worktree.rglob("*")):
        rel = path.relative_to(worktree)
        if rel.parts[0] == ".git" or not path.is_file():
            continue
        digest.update(str(rel).encode())
        digest.update(path.read_bytes())
    return (
        git_in(worktree, "rev-parse", "HEAD"),
        git_in(worktree, "status", "--porcelain=v1", "--untracked-files=all"),
        digest.hexdigest(),
    )


def occupy_worktree(
    client: ClientConfig,
    worktree: Path,
    source: str,
    *,
    daemon: FakeNativeDaemonClient | None = None,
) -> None:
    """Make a live occupant appear for *worktree* through the named source.

    ``state`` (a non-terminal cw session homed there) and ``roster`` (a live
    daemon worker with that ``cwd``) are positive matches; ``unreadable-roster``
    is the fail-closed case (occupancy cannot be ruled out). Shared by the
    fix-agent and dispatch-claim refusal tests (#2213 round 5).

    *daemon* (#2213 round 7): pass the SAME :class:`FakeNativeDaemonClient`
    instance the caller is about to inject into ``create_worktree`` /
    ``dispatch_tick`` / ``_spawn_claimed_task``. Occupancy checks now consult
    the caller's own resolved daemon rather than defaulting to the real one, so
    a ``roster``/``unreadable-roster`` occupant written to the real (tmp-
    isolated) roster file would go unseen by an injected fake -- writing to the
    fake directly is what makes those sources observable again. Omit it (the
    ``state`` source is unaffected either way) only when the caller relies on
    ``create_worktree``'s own default (no injected daemon).
    """
    from cw import native_daemon
    from cw.config import load_state

    if source == "state":
        state = load_state()
        state.sessions.append(
            Session(
                name=f"{client.name}/impl/occupant",
                client=client.name,
                purpose=SessionPurpose.IMPL,
                origin=SessionOrigin.USER,
                workspace_path=client.workspace_path,
                worktree_path=worktree,
                status=SessionStatus.ACTIVE,
            )
        )
        save_state(state)
        return
    if daemon is not None:
        if source == "roster":
            daemon.seed_live_worker(worktree)
        else:
            daemon.roster_unreadable = True
        return
    roster = native_daemon._ROSTER_PATH
    roster.parent.mkdir(parents=True, exist_ok=True)
    if source == "roster":
        payload = {"workers": {"aaaa1111": {"pid": 1, "cwd": str(worktree)}}}
        roster.write_text(json.dumps(payload), encoding="utf-8")
    else:
        roster.write_text("{not json", encoding="utf-8")
