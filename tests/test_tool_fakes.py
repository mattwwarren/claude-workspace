"""Tests for ``tests/_tool_fakes.py`` -- the shared external-tool fakes (#3078).

Every shell shim is run for real from ``tmp_path/bin``, which sits inside the
test root, so the tool launch guard lets it through. The in-process fakes are
driven directly. The "no private duplicate" scan walks every ``tests/*.py``
with ``ast`` so a re-grown private copy of a shared fake fails here.
"""

from __future__ import annotations

import ast
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

import cw.executor.codex
import cw.executor.core
import cw.native_daemon
from tests import _changelog_repair_helpers, _tool_fakes
from tests._tool_fakes import (
    ARM_VIEW_ARMED,
    ARM_VIEW_OPEN,
    GH_UNAUTHENTICATED,
    AbsentToolCli,
    FakeGh,
    FakeGhCli,
    FakeGhComment,
    FakePostCommentGh,
    GhReply,
    ReadyGh,
    RecordingShim,
    assert_gh_calls,
    fake_claude_cli,
    fake_opencode_cli,
    fake_pre_commit_cli,
    fake_uv_cli,
    gh_calls,
    restore_real_run,
    shim_env,
    stub_gh,
    stub_gh_arm,
    stub_gh_issue_create,
    stub_gh_issue_list,
    stub_gh_recording,
    write_bin_stub,
)

_TESTS_DIR = Path(__file__).resolve().parent
_REPO_ROOT = _TESTS_DIR.parent
_HOME = "tests/_tool_fakes.py"

# Every shared fake or shim helper whose private copy must not reappear: the
# moved names plus the private spellings they used to carry.
_SHARED_NAMES = frozenset(
    {
        "FakeGh",
        "_FakeGh",
        "_ReadyGh",
        "ReadyGh",
        "write_bin_stub",
        "stub_gh",
        "stub_gh_recording",
        "stub_gh_arm",
        "gh_calls",
        "shim_env",
        "assert_gh_calls",
        "_stub_gh",
        "_stub_gh_recording",
        "_stub_gh_arm",
        "_stub_gh_create",
        "_stub_gh_issue_list",
        "_write_bin_stub",
        "_shim_env",
        "_gh_calls",
        "_assert_gh_calls",
    }
)


def _run(argv: list[str], env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(argv, env=env, capture_output=True, text=True, check=False)


class TestShellShims:
    def test_write_bin_stub_writes_an_executable_in_bin(self, tmp_path: Path) -> None:
        bin_dir = write_bin_stub(tmp_path, "tool", "#!/bin/sh\necho hi\n")

        assert bin_dir == tmp_path / "bin"
        assert os.access(bin_dir / "tool", os.X_OK)
        assert _run(["tool"], shim_env(bin_dir)).stdout == "hi\n"

    def test_stub_gh_prints_stdout_and_exits(self, tmp_path: Path) -> None:
        bin_dir = stub_gh(tmp_path, exit_code=3, stdout="$NOT_EXPANDED\n")

        result = _run(["gh", "issue", "list"], shim_env(bin_dir))

        assert result.returncode == 3
        assert result.stdout == "$NOT_EXPANDED\n"

    def test_stub_gh_recording_records_argv_and_scripts_exits(
        self, tmp_path: Path
    ) -> None:
        bin_dir = stub_gh_recording(tmp_path, view_exit=1, create_exit=4)
        env = shim_env(bin_dir)

        assert _run(["gh", "release", "view", "v1 x"], env).returncode == 1
        assert _run(["gh", "release", "create", "v1"], env).returncode == 4
        assert _run(["gh", "pr", "list"], env).returncode == 0

        assert gh_calls(bin_dir) == [
            ["release", "view", "v1 x"],
            ["release", "create", "v1"],
            ["pr", "list"],
        ]
        assert_gh_calls(bin_dir, gh_calls(bin_dir))

    def test_gh_calls_is_empty_before_any_launch(self, tmp_path: Path) -> None:
        assert gh_calls(stub_gh_recording(tmp_path)) == []

    def test_stub_gh_arm_replays_steps_and_updates_the_view(
        self, tmp_path: Path
    ) -> None:
        bin_dir = stub_gh_arm(tmp_path, [(1, "boom", None), (0, "", ARM_VIEW_ARMED)])
        env = shim_env(bin_dir)

        assert _run(["gh", "pr", "view"], env).stdout == ARM_VIEW_OPEN
        first = _run(["gh", "pr", "merge"], env)
        assert (first.returncode, first.stderr) == (1, "boom")
        assert _run(["gh", "pr", "merge"], env).returncode == 0
        assert _run(["gh", "pr", "view"], env).stdout == ARM_VIEW_ARMED
        assert len(gh_calls(bin_dir)) == 4

    def test_stub_gh_issue_create_captures_the_body(self, tmp_path: Path) -> None:
        bin_dir = stub_gh_issue_create(tmp_path)
        env = shim_env(bin_dir)

        ok = _run(["gh", "issue", "create", "--title", "t", "--body", "B"], env)
        assert ok.returncode == 0
        assert (tmp_path / "created-body.txt").read_text() == "B"
        assert _run(["gh", "issue", "create"], env).returncode == 2
        assert _run(["gh", "pr", "list"], env).returncode == 2

    @pytest.mark.parametrize(("other_calls", "rc"), [("reject", 2), ("log", 0)])
    def test_stub_gh_issue_list_filters_and_handles_other_calls(
        self, tmp_path: Path, other_calls: str, rc: int
    ) -> None:
        issues = [
            {"number": 1, "author": {"login": "app/github-actions"}},
            {"number": 2, "author": {"login": "human"}},
        ]
        bin_dir = stub_gh_issue_list(
            tmp_path, list_json=json.dumps(issues), other_calls=other_calls
        )
        env = shim_env(bin_dir)
        argv = ["gh", "issue", "list", "--app", "github-actions"]

        listed = _run([*argv, "--jq", ".[].number"], env)
        assert listed.stdout.split() == ["1"]
        assert _run(argv, env).returncode == 2
        assert _run(["gh", "issue", "close", "1"], env).returncode == rc

    def test_stub_gh_issue_list_rejects_an_unknown_mode(self, tmp_path: Path) -> None:
        with pytest.raises(ValueError, match="other_calls"):
            stub_gh_issue_list(tmp_path, list_json="[]", other_calls="maybe")

    def test_stub_gh_issue_list_honours_the_author(self, tmp_path: Path) -> None:
        issues = [{"number": 7, "author": {"login": "bot"}}]
        bin_dir = stub_gh_issue_list(
            tmp_path, list_json=json.dumps(issues), author="bot"
        )

        listed = _run(
            ["gh", "issue", "list", "--app", "github-actions", "--jq", ".[].number"],
            shim_env(bin_dir),
        )

        assert listed.stdout.split() == ["7"]

    def test_stub_gh_records_its_calls(self, tmp_path: Path) -> None:
        bin_dir = stub_gh(tmp_path, exit_code=3, stdout="out\n")
        env = shim_env(bin_dir)

        result = _run(["gh", "pr", "view", "a b"], env)
        _run(["gh", "issue", "list"], env)

        assert (result.returncode, result.stdout) == (3, "out\n")
        assert gh_calls(bin_dir) == [["pr", "view", "a b"], ["issue", "list"]]

    def test_stub_gh_issue_create_records_its_calls(self, tmp_path: Path) -> None:
        bin_dir = stub_gh_issue_create(tmp_path)
        env = shim_env(bin_dir)
        create = ["gh", "issue", "create", "--title", "t u", "--body", "B"]

        assert _run(create, env).returncode == 0
        assert _run(["gh", "pr", "list"], env).returncode == 2

        assert gh_calls(bin_dir) == [create[1:], ["pr", "list"]]

    @pytest.mark.parametrize("other_calls", ["reject", "log"])
    def test_stub_gh_issue_list_records_its_calls(
        self, tmp_path: Path, other_calls: str
    ) -> None:
        bin_dir = stub_gh_issue_list(tmp_path, list_json="[]", other_calls=other_calls)
        env = shim_env(bin_dir)
        listing = ["gh", "issue", "list", "--app", "github-actions", "--jq", ".[]"]
        other = ["gh", "issue", "close", "1"]

        _run(listing, env)
        _run(other, env)

        assert gh_calls(bin_dir) == [listing[1:], other[1:]]

    def test_every_shell_shim_writer_records_its_calls(self) -> None:
        """A new stub cannot skip recording without failing here.

        Each function that writes a shim through ``write_bin_stub`` (the helper
        itself excepted) must build its script from the shared recording lines.
        """
        source = (_TESTS_DIR / "_tool_fakes.py").read_text(encoding="utf-8")
        recorders = {"_RECORD_ARGV_LINES", "_recording_prelude"}
        writers: dict[str, bool] = {}
        for node in ast.walk(ast.parse(source)):
            if not isinstance(node, ast.FunctionDef) or node.name == "write_bin_stub":
                continue
            names = {n.id for n in ast.walk(node) if isinstance(n, ast.Name)}
            if "write_bin_stub" in names:
                writers[node.name] = bool(recorders & names)

        assert {
            "stub_gh",
            "stub_gh_recording",
            "stub_gh_arm",
            "stub_gh_issue_create",
            "stub_gh_issue_list",
            "__init__",
        } <= set(writers)
        assert [name for name, records in writers.items() if not records] == []


class TestRecordingShim:
    def test_records_argv_and_honours_scripted_exits(self, tmp_path: Path) -> None:
        shim = RecordingShim(
            tmp_path,
            "tool",
            responses={
                "run": GhReply(stdout="run\n"),
                "run --help": GhReply(returncode=5, stdout="help\n", stderr="e"),
            },
            default=GhReply(returncode=9),
        )
        env = shim.env()

        helped = _run(["tool", "run", "--help"], env)
        assert (helped.returncode, helped.stdout, helped.stderr) == (5, "help\n", "e")
        assert _run(["tool", "run", "x"], env).stdout == "run\n"
        assert _run(["tool", "other"], env).returncode == 9
        assert shim.calls == [["run", "--help"], ["run", "x"], ["other"]]

    def test_resolves_through_a_path_under_tmp_path(self, tmp_path: Path) -> None:
        shim = RecordingShim(tmp_path, "tool")
        resolved = _run(["sh", "-c", "command -v tool"], shim.env())

        assert Path(resolved.stdout.strip()) == shim.path
        assert shim.path.is_relative_to(tmp_path)

    def test_env_keeps_a_base_mapping(self, tmp_path: Path) -> None:
        shim = RecordingShim(tmp_path, "tool")

        env = shim.env({"KEEP": "1", "PATH": "/nope"}, EXTRA="2")

        assert env["KEEP"] == "1"
        assert env["EXTRA"] == "2"
        assert env["PATH"].startswith(str(shim.bin_dir))

    def test_rejects_a_key_with_shell_metacharacters(self, tmp_path: Path) -> None:
        with pytest.raises(ValueError, match="metacharacter"):
            RecordingShim(tmp_path, "tool", responses={'a"b': GhReply()})

    def test_claude_shim_answers_its_one_shot_calls(self, tmp_path: Path) -> None:
        shim = fake_claude_cli(tmp_path, agents_json='[{"id": "a1"}]')
        env = shim.env()

        assert json.loads(_run(["claude", "agents", "--json"], env).stdout) == [
            {"id": "a1"}
        ]
        assert "Claude Code" in _run(["claude", "--version"], env).stdout
        for verb in ("stop", "attach", "respawn"):
            assert _run(["claude", verb, "a1"], env).returncode == 0
        assert shim.calls[0] == ["agents", "--json"]

    def test_opencode_shim_answers_help_and_streams_events(
        self, tmp_path: Path
    ) -> None:
        shim = fake_opencode_cli(
            tmp_path, help_text="--pure --dir\n", events=['{"type": "done"}']
        )
        env = shim.env()

        opencode = str(shim.path)
        assert _run([opencode, "run", "--help"], env).stdout == "--pure --dir\n"
        streamed = _run([opencode, "run", "--format", "json", "hi"], env)
        assert streamed.stdout == '{"type": "done"}\n'
        assert shim.calls[-1] == ["run", "--format", "json", "hi"]

    def test_uv_and_pre_commit_shims_record(self, tmp_path: Path) -> None:
        uv = fake_uv_cli(tmp_path)
        hooks = fake_pre_commit_cli(tmp_path, default=GhReply(returncode=1))

        assert _run(["uv", "lock", "--check"], uv.env()).returncode == 0
        assert _run(["pre-commit", "run"], hooks.env()).returncode == 1
        assert uv.calls == [["lock", "--check"]]
        assert hooks.calls == [["run"]]


class TestInProcessGhFakes:
    def test_fake_gh_records_the_sprint_surface(self) -> None:
        gh = FakeGh(existing={"a": 5}, milestone_exists=True)

        assert gh.find_milestone("M") == (1, True)
        assert gh.milestone_issue_titles(1) == ({"a": 5}, True)
        number = gh.create_issue("b", "body", labels=["x"], milestone=1).number
        assert gh.update_issue_body(number or 0, "new") is True
        assert gh.calls == [
            "find_milestone:M",
            "milestone_issue_titles:1",
            "create_issue:b",
            "update_issue_body:100",
        ]
        assert gh.bodies[100] == "new"

    def test_post_comment_fake_threads_posts(self) -> None:
        gh = FakePostCommentGh("https://x/issues/1#c-")

        posted = gh.post("1", "hello", cwd=None)

        assert posted is not None
        assert posted.stdout == b"https://x/issues/1#c-1\n"
        assert gh.fetch("1", 5, cwd=None)[0]["url"] == "https://x/issues/1#c-1"
        assert gh.fetch_visibility(timeout=1, cwd=None) == "PUBLIC"
        assert gh.visibility_calls == 1

    def test_ready_gh_scripts_gh_and_delegates_the_rest(self, tmp_path: Path) -> None:
        delegated: list[list[str]] = []

        def _real(
            cmd: list[str], check: bool, capture: bool, *, cwd: Path | None
        ) -> subprocess.CompletedProcess[str]:
            delegated.append(cmd)
            return subprocess.CompletedProcess(cmd, 0, "ok", "")

        gh = ReadyGh([True, None], ready_rc=1, ready_stderr="no", real_run=_real)

        assert json.loads(gh(["gh", "pr", "view"]).stdout) == {"isDraft": True}
        assert gh(["gh", "pr", "ready"], cwd=tmp_path).returncode == 1
        assert gh(["gh", "pr", "view"]).stderr == "HTTP 502"
        assert gh(["git", "status"]).stdout == "ok"
        assert delegated == [["git", "status"]]
        assert gh.cwds == [None, tmp_path, None]

    def test_ready_gh_defaults_to_the_scripts_real_run(self) -> None:
        gh = ReadyGh([])

        result = gh([sys.executable, "-c", "print('real')"])

        assert result.stdout.strip() == "real"

    def test_gh_comment_records_argv_and_stdin(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import cw.gh

        fake = FakeGhComment().install(monkeypatch)

        cw.gh._sp.run(["gh", "issue", "comment"], input=b"body")
        cw.gh._sp.run(["gh", "issue", "comment"])

        assert fake.calls == [["gh", "issue", "comment"]] * 2
        assert fake.bodies == ["body", ""]

    def test_gh_cli_scripts_by_longest_prefix_and_passes_through(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        fake = (
            FakeGhCli()
            .reply("pr", stdout="any")
            .reply("pr", "view", stdout="view", returncode=0)
            .install(monkeypatch)
        )

        text = subprocess.run(["gh", "pr", "view"], text=True, check=False)
        raw = subprocess.run(["gh", "pr", "list"], check=False)
        unscripted = subprocess.run(["gh", "api"], text=True, check=False)
        passthrough = subprocess.run(
            [sys.executable, "-c", "print(1)"], capture_output=True, check=False
        )

        assert text.stdout == "view"
        assert raw.stdout == b"any"
        assert unscripted.returncode == 1
        assert passthrough.stdout.strip() == b"1"
        assert fake.calls == [["gh", "pr", "view"], ["gh", "pr", "list"], ["gh", "api"]]


class TestSuiteDefaults:
    """The ``subprocess.run`` defaults conftest installs for every unit test."""

    def test_unmocked_gh_answers_as_unauthenticated(self) -> None:
        result = subprocess.run(
            ["gh", "pr", "list"], capture_output=True, text=True, check=False
        )

        assert result.returncode == GH_UNAUTHENTICATED.returncode
        assert "gh auth login" in result.stderr

    def test_a_gh_stub_in_the_test_root_still_runs(self, tmp_path: Path) -> None:
        bin_dir = stub_gh(tmp_path, exit_code=0, stdout="from-stub\n")

        result = _run(["gh", "pr", "list"], shim_env(bin_dir))

        assert result.stdout == "from-stub\n"

    @pytest.mark.parametrize("tool", ["claude", "codex", "opencode"])
    def test_ci_absent_tools_raise_enoent(self, tool: str) -> None:
        with pytest.raises(FileNotFoundError) as excinfo:
            subprocess.run([tool, "--version"], check=False)

        assert excinfo.value.filename == tool

    def test_absent_tool_stub_in_the_root_runs(self, tmp_path: Path) -> None:
        shim = fake_claude_cli(tmp_path)

        assert _run(["claude", "--version"], shim.env()).returncode == 0

    def test_absent_tool_cli_chains_to_the_previous_run(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        seen: list[object] = []

        def _previous(
            argv: list[str] | str, *args: object, **kwargs: object
        ) -> subprocess.CompletedProcess[str]:
            seen.append(argv)
            return subprocess.CompletedProcess(argv, 0, "", "")

        monkeypatch.setattr(subprocess, "run", _previous)
        fake = AbsentToolCli({"alpha"}).install(monkeypatch)

        subprocess.run(["beta"], check=False)
        subprocess.run("alpha", check=False)
        with pytest.raises(FileNotFoundError):
            subprocess.run(["alpha", "x"], check=False)

        assert seen == [["beta"], "alpha"]
        assert fake.calls == [["alpha", "x"]]

    def test_restore_real_run_undoes_the_defaults(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        restore_real_run(monkeypatch)

        assert subprocess.run is _tool_fakes._REAL_RUN


class TestReExports:
    def test_fake_gh_head_is_the_lazily_reexported_original(self) -> None:
        assert _tool_fakes.FakeGhHead is _changelog_repair_helpers.FakeGhHead

    def test_unknown_attribute_still_raises(self) -> None:
        with pytest.raises(AttributeError, match="no attribute"):
            _tool_fakes.__getattr__("NoSuchFake")

    def test_runner_and_daemon_fakes_are_the_src_classes(self) -> None:
        core = cw.executor.core
        assert _tool_fakes.FakeFireAndForgetRunner is core.FakeFireAndForgetRunner
        native = cw.native_daemon
        assert _tool_fakes.FakeNativeDaemonClient is native.FakeNativeDaemonClient

    def test_module_imports_without_tests_conftest(self) -> None:
        probe = "import sys, tests._tool_fakes; print('tests.conftest' in sys.modules)"
        result = subprocess.run(
            [sys.executable, "-c", probe],
            cwd=_REPO_ROOT,
            capture_output=True,
            text=True,
            check=True,
        )

        assert result.stdout.strip() == "False"

    def test_codex_job_runner_fixture_swaps_the_launcher(
        self, fake_codex_job_runner: cw.executor.core.FakeFireAndForgetRunner
    ) -> None:
        launcher = vars(cw.executor.codex)["RealCodexJobRunner"]

        assert launcher() is fake_codex_job_runner


def _defined_shared_names(path: Path) -> list[tuple[str, int]]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    found: list[tuple[str, int]] = []
    # Module level only: a method that happens to share a name (a fixture
    # object's ``gh_calls()`` reader) is not a private copy of the helper.
    for node in tree.body:
        if not isinstance(node, (ast.ClassDef, ast.FunctionDef)):
            continue
        if isinstance(node, ast.ClassDef) and node.name.startswith("Test"):
            continue
        if node.name in _SHARED_NAMES:
            found.append((node.name, node.lineno))
    return found


class TestNoPrivateDuplicate:
    def test_shared_fakes_are_defined_only_in_the_shared_module(self) -> None:
        strays = [
            f"{path.relative_to(_REPO_ROOT)}:{line} defines {name}"
            for path in sorted(_TESTS_DIR.rglob("*.py"))
            if path.relative_to(_REPO_ROOT).as_posix() != _HOME
            for name, line in _defined_shared_names(path)
        ]

        assert not strays, "use tests/_tool_fakes.py instead:\n" + "\n".join(strays)

    def test_the_shared_module_defines_the_public_names(self) -> None:
        defined = {name for name, _line in _defined_shared_names(_REPO_ROOT / _HOME)}

        assert {"FakeGh", "ReadyGh", "write_bin_stub", "stub_gh_arm"} <= defined

    def test_test_classes_are_skipped(self, tmp_path: Path) -> None:
        probe = tmp_path / "probe.py"
        probe.write_text(
            "class TestGhChecks:\n    pass\n\n"
            "class FakeGh:\n    def gh_calls(self):\n        pass\n"
        )

        assert _defined_shared_names(probe) == [("FakeGh", 4)]
