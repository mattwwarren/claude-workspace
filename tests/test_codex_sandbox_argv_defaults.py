"""Argv pins, launch-site inventory and static guards for codex sandbox modes (#1767).

A deliberate cross-module static guard: it departs from the one-test-file-per-
source-module mapping because the property it pins (every cw-launched
``codex exec`` carries ``--sandbox <mode>`` plus ``-c approval_policy=never``
from ONE seam, :func:`cw.codex_review._const._exec_policy_argv`) spans three
builders in two packages.

- The default argv of each role is hard-coded as a literal: with no override,
  it is the pre-#1767 argv plus exactly one ``-c approval_policy=never`` pair
  directly after the ``--sandbox <mode>`` pair (AC2 as revised by the operator
  Decision).
- Every launch site, driven through its production code path against a
  recording fake runner, carries the override.
- An AST inventory pins which modules hold a ``codex exec`` argv literal, and
  that each of them calls the seam.
"""

from __future__ import annotations

import ast
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

import cw
from cw.codex_finalize._argv import build_finalize_argv
from cw.codex_fix_loop.commit import _build_fix_codex_argv
from cw.codex_impl._argv import build_impl_argv
from cw.codex_plan._argv import build_plan_argv
from cw.codex_review._capability import (
    _PROBE_ARGV,
    _PROBE_SENTINEL,
    _build_probe_argv,
    _probe_filesystem_capability,
)
from cw.codex_review._roles import _build_generic_codex_argv, run_codex_roles
from cw.codex_runner import FakeCodexRunner
from cw.models import SandboxMode, StageExecutorConfig
from tests._codex_review_helpers import _doc_json, _is_fix_argv
from tests.test_codex_fix_loop import _MF_DOC, _run_loop, _worktree

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator

_SRC = Path(cw.__file__).parent
_SEAM = "_exec_policy_argv"
_SEAM_MODULE = "codex_review/_const.py"
_LAUNCH_SITES = frozenset(
    {
        "codex_review/_roles.py",
        "codex_review/_capability.py",
        "codex_fix_loop/commit.py",
        "tracker_io/_argv.py",
        "codex_plan/_argv.py",
        "codex_impl/_argv.py",
        "codex_finalize/_argv.py",
    }
)
_MODE_LITERALS = frozenset({"read-only", "workspace-write", "danger-full-access"})
_MODE_SCOPED_PACKAGES = (
    "codex_review",
    "codex_fix_loop",
    "tracker_io",
    "codex_plan",
    "codex_impl",
    "codex_finalize",
)
_OVERRIDE = ["-c", "approval_policy=never"]
_LEGAL_MODES = (SandboxMode.READ_ONLY, SandboxMode.WORKSPACE_WRITE)

_S = Path("/scratch/schema.json")
_O = Path("/scratch/output.json")


def _reviewer_argv(mode: SandboxMode | None = None) -> list[str]:
    if mode is None:
        return _build_generic_codex_argv(
            model="m", reasoning_effort="high", schema_path=_S, output_path=_O
        )
    return _build_generic_codex_argv(
        model="m",
        reasoning_effort="high",
        schema_path=_S,
        output_path=_O,
        sandbox_mode=mode,
    )


def _fix_argv(mode: SandboxMode | None = None) -> list[str]:
    if mode is None:
        return _build_fix_codex_argv(model="m", reasoning_effort="high")
    return _build_fix_codex_argv(model="m", reasoning_effort="high", sandbox_mode=mode)


_REVIEWER_LITERAL = [
    "codex",
    "exec",
    "--sandbox",
    "read-only",
    "-c",
    "approval_policy=never",
    "--json",
    "--ephemeral",
    "-c",
    "model_reasoning_effort=high",
    "--output-schema",
    str(_S),
    "-o",
    str(_O),
    "-m",
    "m",
]
_REVIEWER_UNPINNED_LITERAL = [
    "codex",
    "exec",
    "--sandbox",
    "read-only",
    "-c",
    "approval_policy=never",
    "--json",
    "--ephemeral",
    "--output-schema",
    str(_S),
    "-o",
    str(_O),
]
_FIX_LITERAL = [
    "codex",
    "exec",
    "--sandbox",
    "workspace-write",
    "-c",
    "approval_policy=never",
    "--json",
    "-c",
    "model_reasoning_effort=high",
    "-m",
    "m",
]
_PLAN_LITERAL = [
    "codex",
    "exec",
    "--sandbox",
    "read-only",
    "-c",
    "approval_policy=never",
    "--json",
    "--ephemeral",
    "-c",
    "model_reasoning_effort=high",
    "--output-schema",
    str(_S),
    "-o",
    str(_O),
    "-m",
    "m",
]
_IMPL_LITERAL = [
    "codex",
    "exec",
    "--sandbox",
    "workspace-write",
    "-c",
    "approval_policy=never",
    "--json",
    "--ephemeral",
    "-c",
    "model_reasoning_effort=high",
    "--output-schema",
    str(_S),
    "-o",
    str(_O),
    "-m",
    "m",
]
#: The FINALIZE turn's argv: fixed read-only, like PLAN (#3062).
_FINALIZE_LITERAL = _PLAN_LITERAL
_PROBE_LITERAL = [
    "codex",
    "exec",
    "--sandbox",
    "read-only",
    "-c",
    "approval_policy=never",
    "--skip-git-repo-check",
]


def _impl_argv(mode: SandboxMode) -> list[str]:
    return build_impl_argv(
        schema_path=_S,
        output_path=_O,
        model="m",
        reasoning_effort="high",
        sandbox_mode=mode,
        other_servers=(),
    )


def _assert_override_after_sandbox(argv: list[str], mode: str) -> None:
    idx = argv.index("--sandbox")
    assert argv[idx : idx + 4] == ["--sandbox", mode, *_OVERRIDE], argv
    assert argv.count("--sandbox") == 1, argv
    assert argv.count("approval_policy=never") == 1, argv


def _argvs(runner: FakeCodexRunner) -> list[list[str]]:
    argvs: list[list[str]] = []
    for call in runner.calls:
        argv = call["argv"]
        assert isinstance(argv, list)
        argvs.append([str(a) for a in argv])
    return argvs


# ---------------------------------------------------------------------------
# Default argv pins (AC2 as revised)
# ---------------------------------------------------------------------------


class TestDefaultArgvLiterals:
    def test_reviewer_default_argv(self) -> None:
        assert _reviewer_argv() == _REVIEWER_LITERAL

    def test_reviewer_default_argv_unpinned(self) -> None:
        argv = _build_generic_codex_argv(
            model=None, reasoning_effort=None, schema_path=_S, output_path=_O
        )
        assert argv == _REVIEWER_UNPINNED_LITERAL

    def test_fix_default_argv(self) -> None:
        assert _fix_argv() == _FIX_LITERAL

    def test_probe_default_argv(self) -> None:
        assert list(_PROBE_ARGV) == _PROBE_LITERAL

    def test_plan_default_argv(self) -> None:
        argv = build_plan_argv(
            schema_path=_S,
            output_path=_O,
            model="m",
            reasoning_effort="high",
            mcp_server=None,
            tracker=None,
            other_servers=(),
        )
        assert argv == _PLAN_LITERAL

    def test_finalize_default_argv(self) -> None:
        argv = build_finalize_argv(
            schema_path=_S,
            output_path=_O,
            model="m",
            reasoning_effort="high",
            other_servers=(),
        )
        assert argv == _FINALIZE_LITERAL

    def test_impl_default_argv(self) -> None:
        assert _impl_argv(StageExecutorConfig().fix_sandbox_mode) == _IMPL_LITERAL

    def test_config_default_modes_build_the_same_literals(self) -> None:
        cfg = StageExecutorConfig()
        assert _impl_argv(cfg.fix_sandbox_mode) == _IMPL_LITERAL
        assert _reviewer_argv(cfg.reviewer_sandbox_mode) == _REVIEWER_LITERAL
        assert _fix_argv(cfg.fix_sandbox_mode) == _FIX_LITERAL
        assert _build_probe_argv(cfg.reviewer_sandbox_mode) == _PROBE_LITERAL


# ---------------------------------------------------------------------------
# The fix/review discriminator is tied to the real builders
# ---------------------------------------------------------------------------


class TestFixDiscriminatorPin:
    """``_is_fix_argv`` must classify the real builders' output, in every mode.

    The fix-loop doubles route calls by it, so a flag change in any builder
    fails here instead of silently misbooking calls in those doubles.
    """

    @pytest.mark.parametrize("mode", _LEGAL_MODES, ids=str)
    def test_only_the_fix_argv_is_a_fix(self, mode: SandboxMode) -> None:
        assert _is_fix_argv(_fix_argv(mode)) is True
        assert _is_fix_argv(_reviewer_argv(mode)) is False
        assert _is_fix_argv(_build_probe_argv(mode)) is False


# ---------------------------------------------------------------------------
# Every launch site carries the override (Decision regression test)
# ---------------------------------------------------------------------------


def _sandbox_of(argv: list[str]) -> str:
    return argv[argv.index("--sandbox") + 1]


class TestEveryLaunchSiteCarriesTheOverride:
    """Each site's argv, recorded by a fake runner where production builds it."""

    @pytest.mark.parametrize("mode", _LEGAL_MODES, ids=str)
    def test_reviewer_launch(self, mode: SandboxMode, tmp_path: Path) -> None:
        runner = FakeCodexRunner(output_file_content=_doc_json())
        run_codex_roles(
            runner=runner,
            worktree=tmp_path,
            roles=["Code Quality Reviewer"],
            prompts_by_role={"Code Quality Reviewer": "p"},
            model=None,
            reasoning_effort=None,
            wall_clock_budget_seconds=None,
            session_id="s-site-reviewer",
            reviewer_sandbox_mode=mode,
        )
        (argv,) = _argvs(runner)
        _assert_override_after_sandbox(argv, mode.value)

    @pytest.mark.parametrize("mode", _LEGAL_MODES, ids=str)
    def test_probe_launch(self, mode: SandboxMode) -> None:
        runner = FakeCodexRunner(stdout=f"{_PROBE_SENTINEL}\n")
        _probe_filesystem_capability(
            runner=runner, session_id="s-site-probe", sandbox_mode=mode
        )
        (argv,) = _argvs(runner)
        _assert_override_after_sandbox(argv, mode.value)

    def test_plan_launch(self) -> None:
        # The PLAN turn has no sandbox knob: read-only is the only mode.
        argv = build_plan_argv(
            schema_path=_S,
            output_path=_O,
            model=None,
            reasoning_effort=None,
            mcp_server=None,
            tracker=None,
            other_servers=(),
        )
        _assert_override_after_sandbox(argv, "read-only")

    def test_finalize_launch(self) -> None:
        # The FINALIZE turn has no sandbox knob: read-only is the only mode.
        argv = build_finalize_argv(
            schema_path=_S,
            output_path=_O,
            model=None,
            reasoning_effort=None,
            other_servers=("linear",),
        )
        _assert_override_after_sandbox(argv, "read-only")

    @pytest.mark.parametrize("mode", _LEGAL_MODES, ids=str)
    def test_impl_launch(self, mode: SandboxMode) -> None:
        # The IMPL turn takes the lane's resolved fix_sandbox_mode from the seam.
        _assert_override_after_sandbox(_impl_argv(mode), mode.value)

    def test_fix_launch(self, make_git_repo: Callable[..., Path]) -> None:
        # fix_sandbox_mode has one legal config value, so only it is driven.
        worktree = _worktree(make_git_repo, "wt-site-fix")
        runner = FakeCodexRunner(output_file_content=_MF_DOC)
        _run_loop(runner, worktree, session_id="s-site-fix")
        argvs = _argvs(runner)
        fix_argvs = [a for a in argvs if _is_fix_argv(a)]
        assert fix_argvs
        for argv in argvs:
            _assert_override_after_sandbox(argv, _sandbox_of(argv))
        for argv in fix_argvs:
            _assert_override_after_sandbox(argv, "workspace-write")


# ---------------------------------------------------------------------------
# Static guards over src/cw
# ---------------------------------------------------------------------------


def _modules() -> Iterator[tuple[str, ast.Module]]:
    for path in sorted(_SRC.rglob("*.py")):
        rel = path.relative_to(_SRC).as_posix()
        yield rel, ast.parse(path.read_text(encoding="utf-8"), filename=str(path))


def _docstring_ids(tree: ast.Module) -> set[int]:
    owners = (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)
    ids: set[int] = set()
    for node in ast.walk(tree):
        if not isinstance(node, owners) or not node.body:
            continue
        first = node.body[0]
        if (
            isinstance(first, ast.Expr)
            and isinstance(first.value, ast.Constant)
            and isinstance(first.value.value, str)
        ):
            ids.add(id(first.value))
    return ids


def _str_constants(tree: ast.Module) -> Iterator[ast.Constant]:
    skip = _docstring_ids(tree)
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Constant)
            and isinstance(node.value, str)
            and id(node) not in skip
        ):
            yield node


def _is_const(node: ast.expr, value: str) -> bool:
    return isinstance(node, ast.Constant) and node.value == value


def _codex_exec_literals(tree: ast.Module) -> list[int]:
    """Line numbers of ``[..., "codex", "exec", ...]`` list/tuple literals."""
    lines: list[int] = []
    for node in ast.walk(tree):
        if not isinstance(node, (ast.List, ast.Tuple)):
            continue
        elts = node.elts
        if any(
            _is_const(elts[i], "codex") and _is_const(elts[i + 1], "exec")
            for i in range(len(elts) - 1)
        ):
            lines.append(node.lineno)
    return lines


def _calls_seam(tree: ast.Module) -> bool:
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if (isinstance(func, ast.Name) and func.id == _SEAM) or (
            isinstance(func, ast.Attribute) and func.attr == _SEAM
        ):
            return True
    return False


class TestLaunchSiteInventory:
    """Every ``codex exec`` argv literal in src/cw is a known, seam-calling site.

    Documented limitation: the rule is syntactic. A builder that constructs
    its argv via ``shlex.split("codex exec ...")``, ``" ".join``,
    ``["codex"] + ["exec", ...]``, ``["codex", *rest]`` or list
    ``.append``/``.extend`` is NOT detected. The backstops are
    :class:`TestNoSandboxLiteralOutsideTheSeam` (which catches any builder
    that spells ``--sandbox`` or ``approval_policy=never`` itself) and the
    argv-builder grep pasted in the PR body. This inventory is a tripwire for
    the common case, not a proof.
    """

    def test_codex_exec_literals_live_only_in_known_launch_sites(self) -> None:
        found: dict[str, list[int]] = {}
        for rel, tree in _modules():
            lines = _codex_exec_literals(tree)
            if lines:
                found[rel] = lines
        strays = [
            f"codex exec argv literal at {rel}:{line} is not a known launch site; "
            "build its --sandbox and `-c approval_policy=never` pair with "
            "cw.codex_review._const._exec_policy_argv and add the module to "
            "this inventory"
            for rel, lines in sorted(found.items())
            if rel not in _LAUNCH_SITES
            for line in lines
        ]
        assert not strays, "\n".join(strays)
        assert set(found) == _LAUNCH_SITES

    def test_every_launch_site_calls_the_seam(self) -> None:
        trees = dict(_modules())
        missing = [
            f"codex exec argv literal in {rel} does not call {_SEAM}; build its "
            "--sandbox and `-c approval_policy=never` pair with "
            "cw.codex_review._const._exec_policy_argv"
            for rel in sorted(_LAUNCH_SITES)
            if not _calls_seam(trees[rel])
        ]
        assert not missing, "\n".join(missing)


class TestNoSandboxLiteralOutsideTheSeam:
    def test_no_mode_literal_in_the_codex_packages(self) -> None:
        strays = [
            f"{rel}:{node.lineno} spells sandbox mode {node.value!r}; use "
            "cw.models.enums.SandboxMode / its DEFAULT_* constants"
            for rel, tree in _modules()
            if rel.split("/", 1)[0] in _MODE_SCOPED_PACKAGES
            for node in _str_constants(tree)
            if node.value in _MODE_LITERALS
        ]
        assert not strays, "\n".join(strays)

    @pytest.mark.parametrize("literal", ["--sandbox", "approval_policy=never"])
    def test_flag_literal_lives_only_in_the_seam_module(self, literal: str) -> None:
        holders = {
            rel
            for rel, tree in _modules()
            for node in _str_constants(tree)
            if node.value == literal
        }
        assert holders == {_SEAM_MODULE}
