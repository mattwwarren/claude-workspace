"""Tests for cw.stage_verification.impl_type_gate: codex IMPL gate 4 pieces (#3531).

The command matchers, the baseline states, the default-branch ``tests.prepare``
pin, the snapshot commit, the touched files, the no-growth check, the gate
worktree, the mypy attribution and the baselined-debt record. Repos are real,
sandboxed git repos; mypy output comes from real captures under
``tests/fixtures`` (paths redacted only). Nothing here launches ``uv``.
"""

from __future__ import annotations

import json
import logging
import shutil
import subprocess
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
    RECORD_FAILED_FRICTION,
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
_BASELINE_TXT = (_FIXTURES / "mypy_baseline_real.txt").read_text(encoding="utf-8")
_BASELINE_JSON = (_FIXTURES / "basedmypy_baseline_real.json").read_text(
    encoding="utf-8"
)
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

_UV = gate.UvRun(options=())
_MCP = gate.UvRun(options=("--extra", "mcp"))
_UNREPRODUCIBLE = gate.UvRun(options=None)


@pytest.mark.parametrize(
    ("command", "tool", "declared", "launch"),
    [
        ("uv run ruff format --check src/", "ruff", True, _UV),
        ("uv run mypy src/ | mypy-baseline filter", "mypy", True, _UV),
        ("uv run mypy-baseline sync", "mypy", False, None),
        ("uv run mypy-baseline filter", "mypy-baseline", True, _UV),
        (".venv/bin/mypy src/", "mypy", True, "other"),
        ("make lint", "ruff", False, None),
        ("make mypy", "mypy", False, None),
        ("uv run pre-commit run mypy --all-files", "mypy", False, None),
        ("uvx mypy src/", "mypy", True, "other"),
        ("mypy src/", "mypy", True, "other"),
        ("uv run --extra mcp mypy --strict src/", "mypy", True, _MCP),
        ("uv run python -m mypy src/", "mypy", True, _UV),
        ("ruff check . && uv run mypy src/", "mypy", True, _UV),
        ("ruff check . && uv run mypy src/", "ruff", True, "other"),
        ("uv run pytest", "mypy", False, None),
        ("uv run mypy --baseline-file .mypy/baseline.json src/", "mypy", True, _UV),
        ("cat .mypy/notes", "mypy", False, None),
        # Named in the text, but no shell word is the tool.
        ('uv run mypy""x', "mypy", True, "other"),
        ("poetry run mypy src/", "mypy", True, "other"),
    ],
)
def test_declares_tool_and_tool_launch(
    command: str, tool: str, declared: bool, launch: gate.Launch | None
) -> None:
    assert gate.declares_tool(command, tool) is declared
    assert gate.tool_launch(command, tool) == launch


@pytest.mark.parametrize(
    ("command", "options"),
    [
        ("uv run --extra dev mypy .", ("--extra", "dev")),
        ("uv run --extra=dev mypy .", ("--extra=dev",)),
        ("uv run --all-extras --all-groups mypy .", ("--all-extras", "--all-groups")),
        ("uv run --frozen mypy .", ("--frozen",)),
        ("uv run --locked mypy .", ("--locked",)),
        ("uv run -q mypy .", ("-q",)),
        ("uv run --quiet mypy .", ("--quiet",)),
        ("uv run --no-progress mypy .", ("--no-progress",)),
        (
            "uv run --group lint --with=types-x --python 3.12 mypy .",
            ("--group", "lint", "--with=types-x", "--python", "3.12"),
        ),
        ("uv run --with mypy mypy .", ("--with", "mypy")),
        ("uv run --with 'types-x>=1.0' mypy .", ("--with", "types-x>=1.0")),
        ("uv run --python /opt/py/python mypy .", ("--python", "/opt/py/python")),
        ("uv run --with=/srv/tools/x mypy .", ("--with=/srv/tools/x",)),
        ("uv run mypy --extra not-uv .", ()),
        ("echo ok && uv run mypy .", ()),
        ("FOO=1 true && uv run mypy .", ()),
        ("(\nuv run mypy .\n)", ()),
    ],
)
def test_tool_launch_carries_the_environment_options(
    command: str, options: tuple[str, ...]
) -> None:
    assert gate.tool_launch(command, "mypy") == gate.UvRun(options=options)


@pytest.mark.parametrize(
    "command",
    [
        "cd sub && uv run mypy .",
        "(cd sub && uv run mypy .)",
        "pushd sub; uv run mypy .",
        "export UV_PROJECT=sub && uv run mypy .",
        "UV_PROJECT=sub; uv run mypy .",
        "UV_PROJECT=sub uv run mypy .",
        "env MYPYPATH=stubs uv run mypy .",
        "uv run --directory backend mypy .",
        "uv run --directory=backend mypy .",
        "uv run --project backend mypy .",
        "uv run --package api mypy .",
        "uv run --no-sync mypy .",
        "uv run --all-extras=yes mypy .",
        "uv run --python $PYTHON mypy .",
        # An untracked file is absent from the gate worktree.
        "uv run --env-file .env mypy .",
        "uv run --env-file=.env mypy .",
        "uv run --with ./tools/x mypy .",
        "uv run --with=./tools/x mypy .",
        "uv run --python .venv/bin/python mypy .",
        "uv run --python=.venv/bin/python mypy .",
        "uv run --extra= mypy .",
        # ``--with`` is the last token: there is no value to repeat.
        "uv run --extra mypy --with",
    ],
)
def test_a_command_cw_cannot_reproduce_has_no_options(command: str) -> None:
    assert gate.tool_launch(command, "mypy") == _UNREPRODUCIBLE


def test_launch_over_prefers_a_reproducible_uv_run_then_any_then_other() -> None:
    moved = "cd sub && uv run mypy ."
    assert gate.launch_over(["mypy src/", "uv run mypy src/"], "mypy") == _UV
    assert gate.launch_over([moved, "uv run --extra mcp mypy ."], "mypy") == _MCP
    assert gate.launch_over(["mypy src/", moved], "mypy") == _UNREPRODUCIBLE
    assert gate.launch_over(["mypy src/", "true"], "mypy") == "other"
    assert gate.launch_over(["true"], "mypy") is None


def test_consumer_launch_judges_the_mypy_segment_else_the_consumer() -> None:
    piped = "uv run mypy src/ | mypy-baseline filter"
    assert gate.consumer_launch([piped]) == _UV
    assert gate.consumer_launch(["mypy src/ | uv run mypy-baseline filter"]) == "other"
    assert gate.consumer_launch(["cat out | uv run mypy-baseline filter"]) == _UV
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
    assert gate.baseline_candidates(["mypy --baseline-path 'b.txt'"])[-1] == "b.txt"


_DEFAULTS = ("mypy-baseline.txt", ".mypy/baseline.json")


@pytest.mark.parametrize(
    "command",
    [
        "gitleaks detect --baseline-path .gitleaks-baseline.json",
        "uv run pytest --baseline-file perf.json",
        "gitleaks detect --baseline-path x && uv run mypy src/",
        "uv run pytest --baseline-file=perf.json && uv run mypy src/",
    ],
)
def test_another_tools_baseline_argument_is_not_mypys(command: str) -> None:
    assert gate.baseline_candidates([command]) == _DEFAULTS
    assert gate.is_baseline_consumer(command) is False


@pytest.mark.parametrize(
    "command",
    [
        "uv run mypy src/ | uv run mypy-baseline filter",
        "uv run mypy --baseline-file conf/b.json src/",
        "gitleaks detect && uv run mypy --baseline-file conf/b.json src/",
        'uv run mypy --exclude "build|dist" --baseline-file conf/b.json src/',
        "uv run mypy --exclude 'build|dist' --baseline-file conf/b.json src/",
        'uv run mypy --exclude "a;b&&c||d" --baseline-file conf/b.json src/',
        'uv run mypy --exclude "a\\"|b" --baseline-file conf/b.json src/',
    ],
)
def test_a_mypy_segment_is_still_a_baseline_consumer(command: str) -> None:
    assert gate.is_baseline_consumer(command) is True
    if "--baseline-file" in command:
        assert "conf/b.json" in gate.baseline_candidates([command])


@pytest.mark.parametrize(
    "command",
    [
        'gitleaks -r "a|b" | uv run mypy src/',
        "gitleaks -r 'a;b' && uv run mypy src/",
        # An unbalanced quote splits its line at every separator.
        "echo don't && uv run mypy src/",
        # A newline always splits, inside quotes too.
        'echo "a\nuv run mypy src/"',
        # An escaped quote opens no span.
        'echo \\"; uv run mypy src/ \\"',
    ],
)
def test_a_separator_outside_a_quoted_span_still_splits(command: str) -> None:
    with_argument = f"gitleaks --baseline-path x.json; {command}"

    assert gate.declares_tool(command, "mypy") is True
    assert gate.baseline_candidates([with_argument]) == _DEFAULTS
    assert gate.is_baseline_consumer(with_argument) is False


def test_another_tools_tracked_baseline_is_no_mypy_baseline(repo: Path) -> None:
    _push_main(repo, ".gitleaks-baseline.json", "[]\n")
    fork = git_in(repo, "rev-parse", "origin/main")
    commands = [
        "gitleaks detect --baseline-path .gitleaks-baseline.json",
        "uv run mypy src/",
    ]

    assert gate.detect_mypy_baseline(repo, fork, commands) is None


@pytest.mark.parametrize(
    ("name", "content"),
    [
        ("mypy-baseline.txt", _BASELINE_TXT),
        (".mypy/baseline.json", _BASELINE_JSON),
    ],
    ids=["text", "json"],
)
def test_baselined_paths_reads_both_formats_at_the_fork(
    repo: Path, name: str, content: str
) -> None:
    commit_tracked_file(repo, name, content)
    fork = git_in(repo, "rev-parse", "HEAD")
    (repo / name).write_text("", encoding="utf-8")

    found = gate.baselined_paths(repo, fork, [name])

    assert found == {"pkg/old.py", "top.py"}
    # Exact repo-relative paths only, never part of a longer one.
    assert "old.py" not in found


def test_baselined_paths_ignores_empty_and_unknown_json(repo: Path) -> None:
    emptied = json.loads(_BASELINE_JSON)
    emptied["files"]["top.py"] = []
    commit_tracked_file(repo, "a.json", json.dumps(emptied))
    commit_tracked_file(repo, "b.json", "[]\n")
    fork = git_in(repo, "rev-parse", "HEAD")

    assert gate.baselined_paths(repo, fork, ["a.json", "b.json"]) == {"pkg/old.py"}


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


def _hide_attribute(repo: Path) -> None:
    commit_tracked_file(repo, ".gitattributes", "mypy-baseline.txt -diff\n")


@pytest.mark.parametrize(
    "configure",
    [
        lambda repo: git_in(repo, "config", "color.ui", "always"),
        lambda repo: git_in(repo, "config", "diff.external", "true"),
        _hide_attribute,
    ],
    ids=["color-always", "diff-external", "diff-attribute"],
)
def test_baseline_growth_ignores_diff_settings(
    repo: Path, configure: Callable[[Path], object]
) -> None:
    commit_tracked_file(repo, "mypy-baseline.txt", "keep\n")
    configure(repo)
    fork = git_in(repo, "rev-parse", "HEAD")
    (repo / "mypy-baseline.txt").write_text("keep\nnew entry\n", encoding="utf-8")

    added = gate.baseline_growth(repo, fork, _snap(repo), "mypy-baseline.txt")

    assert added == ["new entry"]


def test_baseline_growth_reads_a_non_utf8_line(repo: Path) -> None:
    commit_tracked_file(repo, "mypy-baseline.txt", "keep\n")
    fork = git_in(repo, "rev-parse", "HEAD")
    (repo / "mypy-baseline.txt").write_bytes(b"keep\nbad \xff entry\n")

    added = gate.baseline_growth(repo, fork, _snap(repo), "mypy-baseline.txt")

    assert added == ["bad � entry"]


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


def test_gate_worktree_cleanup_keeps_other_worktrees_registered(
    repo: Path, tmp_path: Path
) -> None:
    root = tmp_path / "root"
    root.mkdir()
    sibling = tmp_path / "sibling"
    git_in(repo, "worktree", "add", "--detach", str(sibling), "HEAD")
    # A sibling session's worktree whose directory is missing right now.
    sibling.rename(tmp_path / "sibling-moved")

    with gate.gate_worktree(repo, _snap(repo), root=str(root), ticket="T-1") as path:
        pass

    listed = git_in(repo, "worktree", "list")
    assert str(sibling) in listed
    assert str(path) not in listed


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
        pytest.raises(subprocess.CalledProcessError),
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
    monkeypatch.setattr(shutil, "rmtree", lambda *_a, **_k: None)
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


def test_record_with_an_unreadable_artifact_is_record_failed(
    tmp_path: Path,
) -> None:
    path = _artifact(tmp_path)
    path.mkdir(parents=True)
    errors = gate.parse_mypy_errors(_ERRORS)

    friction = gate.record_baselined_errors(path, errors, {"main.py"})

    assert friction == [RECORD_FAILED_FRICTION]


def test_record_with_a_failing_write_is_record_failed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def _fail(_path: Path, _text: str) -> None:
        msg = "disk full"
        raise OSError(msg)

    monkeypatch.setattr(gate, "atomic_write_text", _fail)
    errors = gate.parse_mypy_errors(_ERRORS)

    friction = gate.record_baselined_errors(_artifact(tmp_path), errors, {"main.py"})

    assert friction == [RECORD_FAILED_FRICTION]
    assert not _artifact(tmp_path).exists()


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
