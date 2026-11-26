"""Tests for cw.stage_verification.impl_type_gate: codex IMPL gate 4 pieces (#3531).

The command matchers, the baseline states, the default-branch ``tests.prepare``
pin, the snapshot commit, the touched files, the no-growth check, the gate
worktree, the mypy attribution and the baselined-debt record. Repos are real,
sandboxed git repos; mypy output comes from real captures under
``tests/fixtures`` (paths redacted only). Nothing here launches ``uv``.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from cw.review_adjudication import (
    DEFERRED_FINDINGS_RELATIVE_PATH,
    Adjudication,
    read_deferred_findings_md,
    render_deferred_findings_md,
)
from cw.review_adjudication._models import DEFERRED_OUTCOME
from cw.stage_verification import impl_type_gate as gate
from cw.stage_verification.impl_type_gate import (
    DEFERRED_UNPARSEABLE_FRICTION,
    NO_BASELINE_RULE,
    MypyBaseline,
    MypyError,
    VerdictKind,
)
from tests._shared.git_repos import add_bare_origin, commit_tracked_file, git_in

if TYPE_CHECKING:
    from collections.abc import Callable

_FIXTURES = Path(__file__).resolve().parent / "fixtures"
_REPO_ROOT = Path(__file__).resolve().parents[1]
_ERRORS = (_FIXTURES / "mypy_error_output_real.txt").read_text(encoding="utf-8")
_ERRORS_COLUMNS = (_FIXTURES / "mypy_error_output_columns_real.txt").read_text(
    encoding="utf-8"
)
_USAGE = (_FIXTURES / "mypy_usage_error_real.txt").read_text(encoding="utf-8")
_CONFIG = ".claude/project-config.yaml"


@pytest.fixture
def repo(make_git_repo: Callable[..., Path]) -> Path:
    """A repo on ``main`` with a bare origin, then a ``dev/x`` branch."""
    path = make_git_repo("type-gate")
    commit_tracked_file(path, "pkg/base.py", "A = 1\n")
    add_bare_origin(path)
    git_in(path, "checkout", "-b", "dev/x")
    return path


def _push_main(repo: Path, relpath: str, content: str) -> None:
    git_in(repo, "checkout", "main")
    commit_tracked_file(repo, relpath, content)
    git_in(repo, "push", "origin", "main")
    git_in(repo, "checkout", "dev/x")
    git_in(repo, "fetch", "origin")


def _snap(repo: Path) -> str:
    git_in(repo, "add", "-A")
    tree = git_in(repo, "write-tree")
    return gate.snapshot_commit(repo, tree)


# -- declaration and launcher --------------------------------------------------


@pytest.mark.parametrize(
    ("command", "tool", "declared", "launch"),
    [
        ("uv run ruff format --check src/", "ruff", True, "uv"),
        ("uv run mypy src/ | mypy-baseline filter", "mypy", True, "uv"),
        ("uv run mypy-baseline sync", "mypy", False, None),
        ("uv run mypy-baseline filter", "mypy-baseline", True, "uv"),
        (".venv/bin/mypy src/", "mypy", True, "other"),
        ("make lint", "ruff", False, None),
        ("make mypy", "mypy", False, None),
        ("uv run pre-commit run mypy --all-files", "mypy", False, None),
        ("uvx mypy src/", "mypy", True, "other"),
        ("mypy src/", "mypy", True, "other"),
        ("uv run --extra mcp mypy --strict src/", "mypy", True, "uv"),
        ("uv run python -m mypy src/", "mypy", True, "uv"),
        ("ruff check . && uv run mypy src/", "mypy", True, "uv"),
        ("ruff check . && uv run mypy src/", "ruff", True, "other"),
        ("uv run pytest", "mypy", False, None),
        ("uv run mypy --baseline-file .mypy/baseline.json src/", "mypy", True, "uv"),
        ("cat .mypy/notes", "mypy", False, None),
        ("poetry run mypy src/", "mypy", True, "other"),
    ],
)
def test_declares_tool_and_tool_launch(
    command: str, tool: str, declared: bool, launch: str | None
) -> None:
    assert gate.declares_tool(command, tool) is declared
    assert gate.tool_launch(command, tool) == launch


def test_launch_over_prefers_uv_then_other() -> None:
    assert gate.launch_over(["mypy src/", "uv run mypy src/"], "mypy") == "uv"
    assert gate.launch_over(["mypy src/", "true"], "mypy") == "other"
    assert gate.launch_over(["true"], "mypy") is None


def test_consumer_launch_judges_the_mypy_segment_else_the_consumer() -> None:
    piped = "uv run mypy src/ | mypy-baseline filter"
    assert gate.consumer_launch([piped]) == "uv"
    assert gate.consumer_launch(["mypy src/ | uv run mypy-baseline filter"]) == "other"
    assert gate.consumer_launch(["cat out | uv run mypy-baseline filter"]) == "uv"
    assert gate.consumer_launch(["uv run mypy src/"]) is None


# -- baseline states -----------------------------------------------------------


def test_detected_baseline_needs_a_tracked_file_and_a_consumer(repo: Path) -> None:
    _push_main(repo, "mypy-baseline.txt", "a.py:1: error: x\n")
    fork = git_in(repo, "rev-parse", "origin/main")
    commands = ["uv run mypy src/ | mypy-baseline filter", "uv run ruff check ."]

    found = gate.detect_mypy_baseline(repo, fork, commands)

    assert found == MypyBaseline(files=("mypy-baseline.txt",), commands=(commands[0],))


def test_a_consumer_without_a_tracked_file_is_no_baseline(repo: Path) -> None:
    fork = git_in(repo, "rev-parse", "HEAD")
    commands = ["uv run mypy src/ | mypy-baseline filter"]
    assert gate.detect_mypy_baseline(repo, fork, commands) is None


def test_a_tracked_file_without_a_consumer_is_consumer_unknown(repo: Path) -> None:
    _push_main(repo, ".mypy/baseline.json", "{}\n")
    fork = git_in(repo, "rev-parse", "origin/main")

    found = gate.detect_mypy_baseline(repo, fork, ["uv run mypy src/"])

    assert found == MypyBaseline(files=(".mypy/baseline.json",), commands=())


def test_a_baseline_added_only_on_the_branch_is_not_detected(repo: Path) -> None:
    fork = git_in(repo, "rev-parse", "HEAD")
    commit_tracked_file(repo, "mypy-baseline.txt", "x\n")
    commands = ["uv run mypy src/ | mypy-baseline filter"]
    assert gate.detect_mypy_baseline(repo, fork, commands) is None


def test_a_baseline_file_argument_is_a_candidate(repo: Path) -> None:
    _push_main(repo, "conf/mypy-base.json", "{}\n")
    fork = git_in(repo, "rev-parse", "origin/main")
    command = "uv run mypy --baseline-file=./conf/mypy-base.json src/"

    found = gate.detect_mypy_baseline(repo, fork, [command])

    assert found == MypyBaseline(files=("conf/mypy-base.json",), commands=(command,))
    assert gate.baseline_candidates(["x --baseline-path 'b.txt'"])[-1] == "b.txt"


# -- tests.prepare -------------------------------------------------------------


def test_pin_prepare_absent_file_is_unset(repo: Path) -> None:
    assert gate.pin_prepare(repo, "main") == gate.PreparePin()


def test_pin_prepare_reads_the_default_branch_value(repo: Path) -> None:
    _push_main(repo, _CONFIG, "tests:\n  prepare: '  make deps  '\n")
    assert gate.pin_prepare(repo, "main") == gate.PreparePin(command="make deps")


def test_pin_prepare_non_string_is_an_error(repo: Path) -> None:
    _push_main(repo, _CONFIG, "tests:\n  prepare: [a, b]\n")
    pin = gate.pin_prepare(repo, "main")
    assert pin.command is None
    assert pin.error is not None
    assert "single shell command string" in pin.error


def test_pin_prepare_non_utf8_is_an_error(repo: Path) -> None:
    git_in(repo, "checkout", "main")
    path = repo / _CONFIG
    path.parent.mkdir(parents=True)
    path.write_bytes(b"tests:\n  prepare: \xff\xfe\n")
    git_in(repo, "add", _CONFIG)
    git_in(repo, "commit", "-m", "bad config")
    git_in(repo, "push", "origin", "main")
    git_in(repo, "checkout", "dev/x")
    git_in(repo, "fetch", "origin")

    pin = gate.pin_prepare(repo, "main")

    assert pin.command is None
    assert pin.error is not None
    assert "UTF-8" in pin.error


def test_pin_prepare_symlink_entry_is_an_error(repo: Path) -> None:
    git_in(repo, "checkout", "main")
    (repo / ".claude").mkdir()
    (repo / _CONFIG).symlink_to("/etc/hostname")
    git_in(repo, "add", _CONFIG)
    git_in(repo, "commit", "-m", "link")
    git_in(repo, "push", "origin", "main")
    git_in(repo, "checkout", "dev/x")
    git_in(repo, "fetch", "origin")

    pin = gate.pin_prepare(repo, "main")

    assert pin.error is not None
    assert "not a regular file" in pin.error


def test_pin_prepare_missing_ref_is_an_error(repo: Path) -> None:
    # A recent git's fetch records origin/HEAD, which would be preferred.
    git_in(repo, "symbolic-ref", "--delete", "refs/remotes/origin/HEAD")
    pin = gate.pin_prepare(repo, "no-such-branch")
    assert pin.command is None
    assert pin.error is not None
    assert "origin/no-such-branch" in pin.error


def test_pin_prepare_prefers_origin_head(repo: Path) -> None:
    git_in(repo, "checkout", "-b", "trunk", "main")
    commit_tracked_file(repo, _CONFIG, "tests:\n  prepare: from-trunk\n")
    git_in(repo, "push", "origin", "trunk")
    git_in(repo, "checkout", "dev/x")
    git_in(repo, "fetch", "origin")
    git_in(
        repo, "symbolic-ref", "refs/remotes/origin/HEAD", "refs/remotes/origin/trunk"
    )

    assert gate.pin_prepare(repo, "main") == gate.PreparePin(command="from-trunk")


def test_pin_prepare_ignores_the_worktree_and_the_branch(repo: Path) -> None:
    _push_main(repo, _CONFIG, "tests:\n  prepare: trusted\n")
    commit_tracked_file(repo, _CONFIG, "tests:\n  prepare: branch-commit\n")
    (repo / _CONFIG).write_text("tests:\n  prepare: worktree-edit\n", encoding="utf-8")

    assert gate.pin_prepare(repo, "main") == gate.PreparePin(command="trusted")


# -- snapshot, fork, touched files, growth -------------------------------------


def test_snapshot_commit_leaves_head_and_refs_alone(repo: Path) -> None:
    head = git_in(repo, "rev-parse", "HEAD")
    refs = git_in(repo, "for-each-ref")
    (repo / "pkg" / "new.py").write_text("B = 2\n", encoding="utf-8")

    snap = _snap(repo)

    assert git_in(repo, "rev-parse", "HEAD") == head
    assert git_in(repo, "for-each-ref") == refs
    assert git_in(repo, "rev-parse", f"{snap}^") == head
    assert git_in(repo, "show", f"{snap}:pkg/new.py") == "B = 2"


def test_merge_head_is_a_second_parent_and_its_files_are_not_touched(
    repo: Path,
) -> None:
    _push_main(repo, "pkg/merged.py", "M = 1\n")
    commit_tracked_file(repo, "pkg/mine.py", "X = 1\n")
    git_in(repo, "merge", "--no-commit", "--no-ff", "origin/main")
    merge_head = git_in(repo, "rev-parse", "MERGE_HEAD")

    snap = _snap(repo)
    fork = gate.gate_fork_point(repo, "main", snap)

    assert git_in(repo, "rev-parse", f"{snap}^2") == merge_head
    assert fork == merge_head
    assert gate.touched_python_files(repo, fork, snap) == ("pkg/mine.py",)


def test_touched_files_skip_deletions_and_non_python(repo: Path) -> None:
    fork = git_in(repo, "rev-parse", "HEAD")
    (repo / "pkg" / "base.py").unlink()
    (repo / "notes.md").write_text("n\n", encoding="utf-8")
    (repo / "-dash.py").write_text("D = 1\n", encoding="utf-8")
    (repo / "types.pyi").write_text("T: int\n", encoding="utf-8")

    files = gate.touched_python_files(repo, fork, _snap(repo))

    assert files == ("-dash.py", "types.pyi")
    assert gate.path_args(files) == ["./-dash.py", "types.pyi"]


def test_baseline_growth_reports_added_lines_only(repo: Path) -> None:
    commit_tracked_file(repo, "mypy-baseline.txt", "keep\ndrop\n")
    fork = git_in(repo, "rev-parse", "HEAD")
    (repo / "mypy-baseline.txt").write_text("keep\n", encoding="utf-8")
    assert gate.baseline_growth(repo, fork, _snap(repo), "mypy-baseline.txt") == []

    (repo / "mypy-baseline.txt").write_text("keep\nnew entry\n", encoding="utf-8")
    added = gate.baseline_growth(repo, fork, _snap(repo), "mypy-baseline.txt")

    assert added == ["new entry"]


# -- the gate worktree ---------------------------------------------------------


def test_gate_worktree_is_removed_on_normal_exit(repo: Path, tmp_path: Path) -> None:
    root = tmp_path / "root"
    root.mkdir()
    snap = _snap(repo)
    with gate.gate_worktree(repo, snap, root=str(root), ticket="T-1") as path:
        assert path.name.startswith("cw-gate-wt-impl-")
        assert git_in(path, "rev-parse", "HEAD") == snap
        assert git_in(path, "branch", "--show-current") == ""
    assert not path.exists()
    assert str(path) not in git_in(repo, "worktree", "list")


def test_gate_worktree_is_removed_on_exception(repo: Path, tmp_path: Path) -> None:
    root = tmp_path / "root"
    root.mkdir()
    snap = _snap(repo)
    seen: list[Path] = []

    def _fail() -> None:
        with gate.gate_worktree(repo, snap, root=str(root), ticket="T-1") as path:
            seen.append(path)
            msg = "boom"
            raise RuntimeError(msg)

    with pytest.raises(RuntimeError, match="boom"):
        _fail()
    assert not seen[0].exists()


def test_gate_worktree_add_failure_raises_and_leaves_nothing(
    repo: Path, tmp_path: Path
) -> None:
    root = tmp_path / "root"
    root.mkdir()
    with (
        pytest.raises(gate.subprocess.CalledProcessError),
        gate.gate_worktree(repo, "0" * 40, root=str(root), ticket="T-1"),
    ):
        pass
    assert list(root.iterdir()) == []


def test_a_leftover_directory_logs_a_warning(
    repo: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    root = tmp_path / "root"
    root.mkdir()
    monkeypatch.setattr(gate.shutil, "rmtree", lambda *_a, **_k: None)
    caplog.set_level(logging.WARNING, logger=gate.__name__)
    with gate.gate_output_dir(str(root), "T-9") as out:
        pass
    assert out.exists()
    assert f"impl_gate4_worktree_leftover ticket=T-9 path={out}" in caplog.text


def test_gate_root_defaults_off_tmpfs(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("CW_GATE_ROOT", raising=False)
    assert gate.gate_root() == "/var/tmp"
    monkeypatch.setenv("CW_GATE_ROOT", "/x/y")
    assert gate.gate_root() == "/x/y"


# -- mypy attribution ----------------------------------------------------------


def test_parse_mypy_errors_on_the_real_capture() -> None:
    errors = gate.parse_mypy_errors(_ERRORS)
    assert [(e.path, e.line, e.code) for e in errors] == [
        ("pkg/helper.py", 2, "return-value"),
        ("pkg/helper.py", 5, "assignment"),
        ("main.py", 5, "assignment"),
        ("main.py", 6, "attr-defined"),
    ]
    assert errors[0].text.startswith("pkg/helper.py:2: error: Incompatible return")


def test_parse_mypy_errors_with_columns_matches_the_plain_capture() -> None:
    plain = [(e.path, e.line, e.code) for e in gate.parse_mypy_errors(_ERRORS)]
    cols = [(e.path, e.line, e.code) for e in gate.parse_mypy_errors(_ERRORS_COLUMNS)]
    assert cols == plain


def test_parse_mypy_errors_normalizes_paths(tmp_path: Path) -> None:
    text = _ERRORS.replace("pkg/helper.py", f"{tmp_path}/pkg/helper.py").replace(
        "main.py:", "./main.py:"
    )
    paths = {e.path for e in gate.parse_mypy_errors(text, tmp_path)}
    assert paths == {"pkg/helper.py", "main.py"}


def test_a_usage_error_is_not_a_diagnostic() -> None:
    assert gate.parse_mypy_errors(_USAGE) == []


def _err(path: str) -> MypyError:
    return MypyError(path=path, line=1, code="misc", text=f"{path}:1: error: x  [misc]")


@pytest.mark.parametrize(
    ("exit_code", "errors", "kind"),
    [
        (0, [], VerdictKind.PASSED),
        (1, [_err("main.py"), _err("other.py")], VerdictKind.TOUCHED),
        (1, [_err("other.py")], VerdictKind.UNTOUCHED_ONLY),
        (2, [_err("other.py")], VerdictKind.UNPARSED),
        (1, [], VerdictKind.UNPARSED),
        (2, [], VerdictKind.UNPARSED),
    ],
)
def test_mypy_verdict(
    exit_code: int, errors: list[MypyError], kind: VerdictKind
) -> None:
    verdict = gate.mypy_verdict(exit_code, errors, {"main.py"})
    assert verdict.kind is kind
    if kind is VerdictKind.TOUCHED:
        assert [e.path for e in verdict.touched_errors] == ["main.py"]
    if kind is VerdictKind.UNTOUCHED_ONLY:
        assert verdict.untouched_files == ("other.py",)


def test_no_baseline_rule_is_quoted_from_the_stage_doc() -> None:
    doc = (_REPO_ROOT / ".claude" / "commands" / "auto-dev-impl.md").read_text(
        encoding="utf-8"
    )
    assert NO_BASELINE_RULE in doc


def test_mypy_touched_block_caps_the_listed_errors() -> None:
    errors = [_err("main.py")] * 25
    block = gate.mypy_touched_block("uv run mypy main.py", 1, errors, ["main.py"])
    assert block.reason == "impl_failed"
    assert block.details.startswith("Step 2.5 gate 4 failed: ")
    assert block.details.count("main.py:1: error: x") == 20
    assert "... and 5 more" in block.details
    assert NO_BASELINE_RULE in block.details


def test_growth_block_withholds_secret_looking_lines() -> None:
    block = gate.growth_block("mypy-baseline.txt", ["a.py:1: error", "password = 1"])
    assert "a suppression that needs explicit approval" in block.details
    assert "password = 1" not in block.details


# -- the baselined-debt record ----------------------------------------------


def _artifact(worktree: Path) -> Path:
    return worktree / DEFERRED_FINDINGS_RELATIVE_PATH


def test_record_writes_one_entry_per_touched_file(tmp_path: Path) -> None:
    errors = gate.parse_mypy_errors(_ERRORS)

    friction = gate.record_baselined_errors(_artifact(tmp_path), errors, {"main.py"})

    assert friction == []
    entries = read_deferred_findings_md(_artifact(tmp_path))
    assert len(entries) == 1
    entry = entries[0]
    assert (entry.file, entry.severity, entry.outcome) == (
        "main.py",
        "SHOULD_FIX",
        DEFERRED_OUTCOME,
    )
    assert entry.summary == "pre-existing baselined mypy errors in main.py"
    assert entry.rationale == (
        "baselined debt in a file this change touched; not introduced by it: "
        "main.py:5 [assignment], main.py:6 [attr-defined]"
    )


def test_record_skips_a_file_with_a_prior_entry(tmp_path: Path) -> None:
    prior = Adjudication(
        severity="SHOULD_FIX",
        file="main.py",
        summary="something else entirely",
        outcome=DEFERRED_OUTCOME,
        rationale="an older reason",
    )
    path = _artifact(tmp_path)
    path.parent.mkdir(parents=True)
    path.write_text(render_deferred_findings_md([prior]), encoding="utf-8")
    before = path.read_text(encoding="utf-8")
    errors = gate.parse_mypy_errors(_ERRORS)

    assert gate.record_baselined_errors(path, errors, {"main.py"}) == []
    assert path.read_text(encoding="utf-8") == before


def test_record_leaves_a_malformed_artifact_untouched(tmp_path: Path) -> None:
    path = _artifact(tmp_path)
    path.parent.mkdir(parents=True)
    path.write_text("not the artifact\n", encoding="utf-8")
    errors = gate.parse_mypy_errors(_ERRORS)

    friction = gate.record_baselined_errors(path, errors, {"main.py"})

    assert friction == [DEFERRED_UNPARSEABLE_FRICTION]
    assert path.read_text(encoding="utf-8") == "not the artifact\n"


def test_record_caps_the_error_list(tmp_path: Path) -> None:
    errors = [
        MypyError(path="main.py", line=n, code="misc", text="x") for n in range(25)
    ]
    gate.record_baselined_errors(_artifact(tmp_path), errors, {"main.py"})
    rationale = read_deferred_findings_md(_artifact(tmp_path))[0].rationale
    assert "main.py:19 [misc]" in rationale
    assert "main.py:20 [misc]" not in rationale
    assert rationale.endswith("and 5 more")


def test_record_with_no_touched_errors_writes_nothing(tmp_path: Path) -> None:
    errors = gate.parse_mypy_errors(_ERRORS)
    assert gate.record_baselined_errors(_artifact(tmp_path), errors, {"x.py"}) == []
    assert not _artifact(tmp_path).exists()
