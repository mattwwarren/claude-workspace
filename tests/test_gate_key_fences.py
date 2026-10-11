"""Doc and executed tests for the Step 2.5 gate fences' use of gate_key.py (#2436).

Four fences name the gate paths: the impl setup fence, gate 2, gate 3 and the
``auto-dev.md`` monolith. Each must resolve ``gate_key.py``, stop with the
install remedy when it is absent, and build ``cw-gate-wt-``/``cw-gate-tmp-``
from the key it prints, never from ``$CW_SESSION``. The setup and monolith
fences must also claim the path before their destructive self-heal and arm
their cleanup only after the claim succeeds.

The fence comments are worded so that none of the forbidden tokens appears in
them, and the token assertions below stay strict over the whole fence text,
comments included (R8): they are not scoped to non-comment lines.

Executed tests run the doc fences with real ``bash``, real ``git`` and the test
interpreter linked as ``python3``. Stream contract: ``gate_key.py``'s refusal
and takeover notes go to stderr; the fence's own ``IMPL_FAILED:``,
``GATE_KEY_HELPER_MISSING:`` and could-not-compute lines are echoed to stdout;
each assertion reads the one stream it names.
"""

from __future__ import annotations

import fcntl
import json
import re
import shutil
import subprocess
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from tests.conftest import (
    GATE_KEY_SCRIPT,
    _bash_fences,
    _clean_git_env,
    _cmd,
    _write_bin_stub,
    assert_gate_key_helper_missing,
    gate2_fence,
    gate_key_for,
    impl_gate3_fence,
    impl_gate_setup_fence,
    install_gate_key,
    link_real_python3,
    load_guard_script_module,
    make_claimant,
    monolith_gate_fence,
    substitute_fence_placeholders,
)

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping

pytestmark = pytest.mark.usefixtures("clean_gate_env")

_mod = load_guard_script_module(GATE_KEY_SCRIPT, "gate_key")

_REPO_ROOT = Path(__file__).resolve().parents[1]
_BRANCH = "feature"
_STALE_SESSION = "9141a90d"

_FENCES: dict[str, Callable[[], str]] = {
    "setup": impl_gate_setup_fence,
    "gate2": gate2_fence,
    "gate3": impl_gate3_fence,
    "monolith": monolith_gate_fence,
}
_CLAIMING = ("setup", "monolith")

_KEY_CALL = "python3 \"$KEY_SCRIPT\" --branch '<branch-name>'"
_FORBIDDEN = (
    "CW_SESSION",
    "sha256",
    "shasum",
    "md5sum",
    "cksum",
    "git-common-dir",
    "printf '%s\\n%s\\n'",
)
_GATE2_FORBIDDEN = ("rev-parse", "GUARD_ROOT", "$PWD", "CTX_WORKTREE", "SESSION_ROOT")
_CLAIM_CALL = "--branch '<branch-name>' --claim --key \"$GATE_KEY\" --gate-root"
_RELEASE_CALL = '--release --key "$GATE_KEY"'
_CLEANUP_GUARD = '[ -z "${GATE_WT_CLEANED:-}" ] || return 0; GATE_WT_CLEANED=1'
_EXIT_TRAP = "trap gate_wt_cleanup EXIT"
_SIGNAL_TRAP = "trap 'gate_wt_cleanup; exit 143' INT TERM"
_DEFAULT_ROOT = "${CW_GATE_ROOT:-/var/tmp}"
_COULD_NOT_COMPUTE = re.compile(
    r"could not compute the gate key \(.*; Python 3\.\d+\.\d+.*\)"
)
_CW_SESSION_TMP_PATH = re.compile(
    r"(?:/tmp|/var/tmp|CW_GATE_ROOT)[^\s\"'`]*\$\{?CW_SESSION"
)


def _forbidden_tokens(fence: str, extra: tuple[str, ...] = ()) -> list[str]:
    return [token for token in (*_FORBIDDEN, *extra) if token in fence]


# --- Text, over the four fences ----------------------------------------------


@pytest.mark.parametrize("name", list(_FENCES))
def test_fence_calls_gate_key_and_builds_paths_from_it(name: str) -> None:
    fence = _FENCES[name]()
    assert _KEY_CALL in fence
    anchor = "$SESSION_WT" if name == "gate2" else "$GUARD_ROOT"
    assert f'"{anchor}/.claude/scripts/gate_key.py"' in fence
    assert '"$HOME/.claude/scripts/gate_key.py"' in fence
    assert_gate_key_helper_missing(fence[: fence.index(_KEY_CALL)])
    assert f'"{_DEFAULT_ROOT}/cw-gate-wt-$GATE_KEY"' in fence
    assert f'"{_DEFAULT_ROOT}/cw-gate-tmp-$GATE_KEY"' in fence
    assert '[[ "$GATE_KEY" =~ ^[0-9a-f]{16}$ ]]' in fence
    assert (
        "could not compute the gate key ($KEY_SCRIPT; $(python3 --version 2>&1))"
        in (fence)
    )
    extra = _GATE2_FORBIDDEN if name == "gate2" else ()
    assert _forbidden_tokens(fence, extra) == []


def test_forbidden_token_check_bites_on_a_synthetic_fence() -> None:
    synthetic = (
        'TMPWT="${CW_GATE_ROOT:-/var/tmp}/cw-gate-wt-$CW_SESSION"\n'
        "KEY=$(printf '%s\\n%s\\n' \"$(git rev-parse --git-common-dir)\" b"
        " | sha256sum)\n"
    )
    assert set(_forbidden_tokens(synthetic)) == {
        "CW_SESSION",
        "sha256",
        "git-common-dir",
        "printf '%s\\n%s\\n'",
    }


@pytest.mark.parametrize("name", _CLAIMING)
def test_claim_precedes_the_destructive_commands(name: str) -> None:
    fence = _FENCES[name]()
    claim = fence.index(_CLAIM_CALL)
    assert claim < fence.index("git worktree remove --force")
    assert claim < fence.index("rm -rf")
    assert claim < fence.index(_EXIT_TRAP)
    cleanup = _cleanup_text(fence)
    assert _RELEASE_CALL in cleanup


@pytest.mark.parametrize("name", list(_FENCES))
def test_every_cw_gate_root_occurrence_uses_the_default_spelling(name: str) -> None:
    fence = _FENCES[name]()
    assert fence.count("CW_GATE_ROOT") == fence.count(_DEFAULT_ROOT)
    assert fence.count(_DEFAULT_ROOT) >= 2


def test_cw_gate_root_spelling_check_bites_on_a_synthetic_fence() -> None:
    synthetic = 'A="${CW_GATE_ROOT:-/var/tmp}/x"\nB="${CW_GATE_ROOT:-/tmp}/y"\n'
    assert synthetic.count("CW_GATE_ROOT") != synthetic.count(_DEFAULT_ROOT)


def test_impl_doc_states_the_per_call_gate_contract() -> None:
    content = _cmd("auto-dev-impl.md")
    start = content.index("that cwd confusion is the bug this step fixes.")
    end = content.index("FORK_POINT is recomputed", start)
    sentence = content[start:end]
    assert "cover the setup call only" in sentence
    assert "re-derive" in sentence
    assert "gate 1" not in sentence.lower()
    assert "gate 5" not in sentence.lower()


def _cleanup_text(fence: str) -> str:
    lines = fence.splitlines()
    start = next(i for i, line in enumerate(lines) if "gate_wt_cleanup() {" in line)
    end = next(i for i in range(start + 1, len(lines)) if lines[i] == "}")
    return "\n".join(lines[start : end + 1])


def _assert_armed_after_claim(fence: str) -> None:
    lines = fence.splitlines()
    entry = lines.index("trap - EXIT INT TERM")
    assert lines[entry + 1] == "GATE_WT_CLEANED=1"
    for assignment in ("FORK_POINT=", "GATE_KEY=", "TMPWT=", "export TMPDIR="):
        first = next(i for i, line in enumerate(lines) if line.startswith(assignment))
        assert entry < first, assignment
    assert '--claim --key "$GATE_KEY"' in fence
    reset = re.search(r"^GATE_WT_CLEANED=$", fence, re.MULTILINE)
    assert reset is not None
    assert fence.index(_CLAIM_CALL) < reset.start()
    assert fence.index("gate_wt_cleanup() {") < reset.start()
    reset_line = lines.index("GATE_WT_CLEANED=")
    assert lines[reset_line + 1] == _EXIT_TRAP


@pytest.mark.parametrize("name", _CLAIMING)
def test_cleanup_is_armed_only_after_the_claim(name: str) -> None:
    fence = _FENCES[name]()
    _assert_armed_after_claim(fence)
    assert _EXIT_TRAP in fence
    assert _SIGNAL_TRAP in fence


def test_cleanup_arming_check_bites_on_a_synthetic_fence() -> None:
    fence = impl_gate_setup_fence()
    moved = fence.replace("\nGATE_WT_CLEANED=\n", "\n").replace(
        "python3 \"$KEY_SCRIPT\" --branch '<branch-name>' --claim",
        "GATE_WT_CLEANED=\npython3 \"$KEY_SCRIPT\" --branch '<branch-name>' --claim",
    )
    assert moved != fence
    with pytest.raises(AssertionError):
        _assert_armed_after_claim(moved)


@pytest.mark.parametrize("name", _CLAIMING)
def test_gate_wt_cleanup_is_guarded_run_once(name: str) -> None:
    fence = _FENCES[name]()
    cleanup = _cleanup_text(fence)
    body = cleanup[cleanup.index("{") + 1 : cleanup.rindex("}")].strip()
    assert body.startswith(_CLEANUP_GUARD)
    assert body.index(_CLEANUP_GUARD) < body.index("git worktree remove")
    assert body.index(_CLEANUP_GUARD) < body.index("rm -rf")
    last = body.rstrip(";").split(";")[-1]
    assert _RELEASE_CALL in last
    assert _EXIT_TRAP in fence
    assert _SIGNAL_TRAP in fence


# --- Repo-wide scan (R2) ------------------------------------------------------


def _scanned_files() -> list[Path]:
    claude = _REPO_ROOT / ".claude"
    return sorted(
        [
            *claude.glob("commands/*.md"),
            *claude.glob("skills/**/SKILL.md"),
            *claude.glob("scripts/*.py"),
        ]
    )


@pytest.mark.parametrize(
    "path", _scanned_files(), ids=lambda path: str(path.relative_to(_REPO_ROOT))
)
def test_no_tmp_path_is_keyed_on_cw_session(path: Path) -> None:
    hits = _CW_SESSION_TMP_PATH.findall(path.read_text(encoding="utf-8"))
    assert hits == [], f"{path}: {hits}"


@pytest.mark.parametrize(
    ("line", "flagged"),
    [
        ("sort > /tmp/x-$CW_SESSION", True),
        ('TMPWT="${CW_GATE_ROOT:-/var/tmp}/cw-gate-wt-$CW_SESSION"', True),
        ('"$(git rev-parse --show-toplevel)/.cw/conflicted-files-$CW_SESSION"', False),
        (
            "Never write this cache to a fixed `/tmp/...` path or any"
            " `$CW_SESSION`-keyed path",
            False,
        ),
    ],
)
def test_cw_session_tmp_path_regex_flags_only_keyed_paths(
    line: str, flagged: bool
) -> None:
    assert (_CW_SESSION_TMP_PATH.search(line) is not None) is flagged


def test_exactly_the_four_fences_name_gate_paths() -> None:
    claude = _REPO_ROOT / ".claude"
    docs = [*claude.glob("commands/*.md"), *claude.glob("skills/**/SKILL.md")]
    naming = [
        fence
        for doc in sorted(docs)
        for fence in _bash_fences(doc.read_text(encoding="utf-8"))
        if "cw-gate-wt-" in fence or "cw-gate-tmp-" in fence
    ]
    expected = [build() for build in _FENCES.values()]
    assert sorted(naming) == sorted(expected)
    for fence in naming:
        assert 'python3 "$KEY_SCRIPT"' in fence
        assert _forbidden_tokens(fence) == []


# --- Executed ------------------------------------------------------------------


def _run_fence_body(
    body: str,
    *,
    cwd: Path,
    home: Path,
    gate_root: Path,
    bin_dir: Path,
    extra_env: Mapping[str, str] | None = None,
    timeout: float = 120.0,
) -> subprocess.CompletedProcess[str]:
    """Run a filled fence body (or several) in one ``bash -c``."""
    env = _clean_git_env()
    env.update(
        {
            "HOME": str(home),
            "PATH": f"{bin_dir}:{env.get('PATH', '')}",
            "CW_GATE_ROOT": str(gate_root),
            "CW_SESSION": _STALE_SESSION,
        }
    )
    env.update(extra_env or {})
    return subprocess.run(
        ["bash", "-c", body],
        cwd=cwd,
        env=env,
        capture_output=True,
        text=True,
        check=False,
        timeout=timeout,
    )


class _Bed:
    """One executed-fence test bed: home with the script, python3, gate root."""

    def __init__(self, tmp_path: Path, *, install: bool = True) -> None:
        self.tmp_path = tmp_path
        self.home = tmp_path / "home"
        self.home.mkdir(parents=True, exist_ok=True)
        self.script = install_gate_key(self.home) if install else None
        self.gate_root = tmp_path / "gate-root"
        self.gate_root.mkdir(parents=True, exist_ok=True)
        self.bin_dir = link_real_python3(tmp_path / "bin")

    def run(
        self,
        body: str,
        cwd: Path,
        extra_env: Mapping[str, str] | None = None,
    ) -> subprocess.CompletedProcess[str]:
        return _run_fence_body(
            body,
            cwd=cwd,
            home=self.home,
            gate_root=self.gate_root,
            bin_dir=self.bin_dir,
            extra_env=extra_env,
        )

    def paths(self, repo: Path) -> tuple[Path, Path, Path]:
        key = gate_key_for(repo, _BRANCH)
        return (
            self.gate_root / f"cw-gate-wt-{key}",
            self.gate_root / f"cw-gate-tmp-{key}",
            self.gate_root / f"cw-gate-owner-{key}.json",
        )


def _fill(fence: str, test_command: str = "true") -> str:
    return substitute_fence_placeholders(
        fence, {"branch-name": _BRANCH, "test_command": test_command}
    )


def _setup_body(echo_paths: bool = True) -> str:
    body = _fill(impl_gate_setup_fence())
    return body + ('\necho "PATHS=$TMPWT|$TMPDIR"\n' if echo_paths else "\n")


def _monolith_preamble() -> str:
    lines = monolith_gate_fence().splitlines()
    end = next(
        i
        for i, line in enumerate(lines)
        if line.startswith("git worktree add --detach")
    )
    return "\n".join(lines[: end + 1])


def _echoed_paths(result: subprocess.CompletedProcess[str]) -> str:
    lines = [line for line in result.stdout.splitlines() if line.startswith("PATHS=")]
    assert len(lines) == 1, result.stdout + result.stderr
    return lines[0].removeprefix("PATHS=")


def _real_git() -> str:
    git = shutil.which("git")
    assert git, "git must be on PATH"
    return git


def _stub_git(root: Path) -> Path:
    """A ``git`` that acts on ``worktree add`` per ``$GATE_STUB_MODE``, else delegates.

    ``kill``: SIGKILL the fence's shell. ``term``: SIGTERM it, then run the real
    command. ``hold``: touch ``$GATE_STUB_MARKER`` and wait for
    ``$GATE_STUB_RELEASE``, then fail. Every call is logged to
    ``$GATE_STUB_LOG`` when set. Returns the stub's bin directory.
    """
    body = (
        "#!/bin/sh\n"
        '[ -n "${GATE_STUB_LOG:-}" ] && echo "$*" >> "$GATE_STUB_LOG"\n'
        'if [ "$1" = worktree ] && [ "$2" = add ]; then\n'
        '  case "${GATE_STUB_MODE:-}" in\n'
        '    kill) kill -9 "$PPID"; exit 1 ;;\n'
        '    term) kill -TERM "$PPID" ;;\n'
        '    hold) : > "$GATE_STUB_MARKER"\n'
        '          while [ ! -f "$GATE_STUB_RELEASE" ]; do sleep 0.05; done\n'
        "          exit 1 ;;\n"
        "  esac\n"
        "fi\n"
        f'exec "{_real_git()}" "$@"\n'
    )
    root.mkdir(parents=True, exist_ok=True)
    return _write_bin_stub(root, "git", body)


def _publish_owner(
    bed: _Bed, repo: Path, pid_owner: subprocess.Popen[bytes], *, alive: bool
) -> Path:
    """Publish an owner record for *repo* naming *pid_owner* as worker and shell."""
    probe = _mod.Probe()
    claimant = make_claimant(
        _mod, pid_owner.pid, pid_owner.pid, pidns=_mod.read_pidns(probe)
    )
    if not alive:
        pid_owner.terminate()
        pid_owner.wait(timeout=10)
    key = gate_key_for(repo, _BRANCH)
    code, _ = _mod.claim(bed.gate_root, key, _BRANCH, claimant, probe)
    assert code == 0
    return Path(_mod.owner_path(bed.gate_root, key))


def _plant(directory: Path, name: str = "sentinel", text: str = "keep\n") -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / name
    path.write_text(text, encoding="utf-8")
    return path


def test_two_clones_same_cw_session_get_distinct_gate_paths(
    tmp_path: Path, make_git_repo_with_origin: Callable[..., tuple[Path, Path]]
) -> None:
    bed = _Bed(tmp_path)
    clone_a, _ = make_git_repo_with_origin("clone-a")
    clone_b, _ = make_git_repo_with_origin("clone-b")
    result_a = bed.run(_setup_body(), clone_a)
    result_b = bed.run(_setup_body(), clone_b)
    assert result_a.returncode == 0, result_a.stdout + result_a.stderr
    assert result_b.returncode == 0, result_b.stdout + result_b.stderr
    paths_a, paths_b = _echoed_paths(result_a), _echoed_paths(result_b)
    assert paths_a != paths_b
    assert _STALE_SESSION not in paths_a
    assert _STALE_SESSION not in paths_b


def test_other_clones_gate_worktree_survives_self_heal(
    tmp_path: Path, make_git_repo_with_origin: Callable[..., tuple[Path, Path]]
) -> None:
    bed = _Bed(tmp_path)
    clone_a, _ = make_git_repo_with_origin("clone-a")
    clone_b, _ = make_git_repo_with_origin("clone-b")
    sentinel = _plant(bed.paths(clone_a)[0])
    result = bed.run(_setup_body(), clone_b)
    assert result.returncode == 0, result.stdout + result.stderr
    assert sentinel.read_text(encoding="utf-8") == "keep\n"


def test_live_foreign_owner_refuses_and_leaves_the_path(
    tmp_path: Path,
    make_git_repo_with_origin: Callable[..., tuple[Path, Path]],
    sleeper: Callable[[], subprocess.Popen[bytes]],
) -> None:
    bed = _Bed(tmp_path)
    repo, _ = make_git_repo_with_origin("clone")
    child = sleeper()
    owner = _publish_owner(bed, repo, child, alive=True)
    tmpwt = bed.paths(repo)[0]
    sentinel = _plant(tmpwt)
    before = sorted(path.name for path in bed.gate_root.iterdir())
    result = bed.run(_setup_body(), repo)
    assert result.returncode != 0
    live = result.stderr.index(f"gate path in use by a live run, pid {child.pid}")
    assert result.stderr.index("If no run owns the path, remove", live) > live
    assert str(owner) in result.stderr
    assert f"IMPL_FAILED: gate path {tmpwt} not claimed" in result.stdout
    assert sentinel.read_text(encoding="utf-8") == "keep\n"
    assert sorted(path.name for path in bed.gate_root.iterdir()) == before


def test_dead_owner_allows_and_self_heal_runs(
    tmp_path: Path,
    make_git_repo_with_origin: Callable[..., tuple[Path, Path]],
    sleeper: Callable[[], subprocess.Popen[bytes]],
) -> None:
    bed = _Bed(tmp_path)
    repo, _ = make_git_repo_with_origin("clone")
    _publish_owner(bed, repo, sleeper(), alive=False)
    tmpwt, _, owner = bed.paths(repo)
    sentinel = _plant(tmpwt)
    result = bed.run(_setup_body(), repo)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "previous owner is gone" in result.stderr
    assert not sentinel.exists()
    assert not owner.exists()


def test_absent_owner_file_allows_and_a_normal_exit_releases(
    tmp_path: Path, make_git_repo_with_origin: Callable[..., tuple[Path, Path]]
) -> None:
    bed = _Bed(tmp_path)
    repo, _ = make_git_repo_with_origin("clone")
    tmpwt, tmpdir, owner = bed.paths(repo)
    result = bed.run(_setup_body() + f'cat "{owner}"\n', repo)
    assert result.returncode == 0, result.stdout + result.stderr
    assert '"shell_pid"' in result.stdout
    assert not owner.exists()
    assert not tmpwt.exists()
    assert not tmpdir.exists()


def test_sigkilled_run_leaves_the_record_and_the_next_run_takes_over(
    tmp_path: Path, make_git_repo_with_origin: Callable[..., tuple[Path, Path]]
) -> None:
    bed = _Bed(tmp_path)
    repo, _ = make_git_repo_with_origin("clone")
    owner = bed.paths(repo)[2]
    stub_dir = _stub_git(tmp_path / "stub")
    killed = bed.run(
        _setup_body(),
        repo,
        {
            "PATH": f"{stub_dir}:{bed.bin_dir}:{_clean_git_env()['PATH']}",
            "GATE_STUB_MODE": "kill",
        },
    )
    assert killed.returncode == -9
    assert owner.exists()
    result = bed.run(_setup_body(), repo)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "previous owner is gone" in result.stderr
    assert not owner.exists()


def _driver(repo: Path, owner: Path, tmpwt: Path) -> str:
    """Two child fences under one worker (``CW_GATE_OWNER_PID`` = this shell)."""
    return (
        "export CW_GATE_OWNER_PID=$$\n"
        f'cd "{repo}"\n'
        'GATE_STUB_MODE=kill PATH="$STUB_DIR:$PATH" bash -c "$FENCE"\n'
        'echo "FIRST=$?"\n'
        f'[ -f "{owner}" ] && echo RECORD_LEFT\n'
        'bash -c "$FENCE"\n'
        'echo "SECOND=$?"\n'
        f'[ -d "{tmpwt}" ] || echo TMPWT_RELEASED\n'
    )


def test_same_worker_retry_after_a_killed_gate_shell_takes_over(
    tmp_path: Path, make_git_repo_with_origin: Callable[..., tuple[Path, Path]]
) -> None:
    bed = _Bed(tmp_path)
    repo, _ = make_git_repo_with_origin("clone")
    tmpwt, _, owner = bed.paths(repo)
    stub_dir = _stub_git(tmp_path / "stub")
    env = {"FENCE": _setup_body(echo_paths=False), "STUB_DIR": str(stub_dir)}
    result = bed.run(_driver(repo, owner, tmpwt), tmp_path, env)
    assert "FIRST=137" in result.stdout, result.stdout + result.stderr
    assert "RECORD_LEFT" in result.stdout
    assert "SECOND=0" in result.stdout, result.stdout + result.stderr
    assert "previous gate shell" in result.stderr
    assert not owner.exists()


def test_same_worker_overlap_with_a_live_gate_shell_refuses(
    tmp_path: Path, make_git_repo_with_origin: Callable[..., tuple[Path, Path]]
) -> None:
    bed = _Bed(tmp_path)
    repo, _ = make_git_repo_with_origin("clone")
    tmpwt, _, owner = bed.paths(repo)
    stub_dir = _stub_git(tmp_path / "stub")
    marker, release = tmp_path / "held", tmp_path / "release"
    driver = (
        "export CW_GATE_OWNER_PID=$$\n"
        f'cd "{repo}"\n'
        'GATE_STUB_MODE=hold PATH="$STUB_DIR:$PATH" bash -c "$FENCE" &\n'
        "HELD=$!\n"
        f'for _ in $(seq 400); do [ -f "{marker}" ] && break; sleep 0.05; done\n'
        f'mkdir -p "{tmpwt}" && echo keep > "{tmpwt}/sentinel"\n'
        'bash -c "$FENCE"\n'
        'echo "SECOND=$?"\n'
        f'[ -f "{tmpwt}/sentinel" ] && echo SENTINEL_KEPT\n'
        f': > "{release}"\n'
        'wait "$HELD"\n'
    )
    env = {
        "FENCE": _setup_body(echo_paths=False),
        "STUB_DIR": str(stub_dir),
        "GATE_STUB_MARKER": str(marker),
        "GATE_STUB_RELEASE": str(release),
    }
    result = bed.run(driver, tmp_path, env)
    assert marker.exists(), result.stdout + result.stderr
    assert "SECOND=1" in result.stdout, result.stdout + result.stderr
    assert "gate path in use by a live run" in result.stderr
    assert "SENTINEL_KEPT" in result.stdout
    assert owner.exists()
    assert "gate cleanup failed; owner record retained" in result.stderr


def test_setup_fence_twice_in_one_shell_is_a_takeover_not_a_refusal(
    tmp_path: Path, make_git_repo_with_origin: Callable[..., tuple[Path, Path]]
) -> None:
    bed = _Bed(tmp_path)
    repo, _ = make_git_repo_with_origin("clone")
    owner = bed.paths(repo)[2]
    show = 'cat "$CW_GATE_ROOT/cw-gate-owner-$GATE_KEY.json"; echo\n'
    body = _setup_body(echo_paths=False) + 'echo "SHELL=$$"\n' + show
    body += _setup_body(echo_paths=False) + show
    result = bed.run(body, repo)
    assert result.returncode == 0, result.stdout + result.stderr
    shell = next(
        line.removeprefix("SHELL=")
        for line in result.stdout.splitlines()
        if line.startswith("SHELL=")
    )
    records = [
        json.loads(line) for line in result.stdout.splitlines() if line.startswith("{")
    ]
    assert len(records) == 2
    assert all(str(record["shell_pid"]) == shell for record in records)
    assert result.stderr.count("re-entry from the same shell") == 1
    assert not owner.exists()


def test_setup_fence_runs_without_cw_session_env(
    tmp_path: Path, make_git_repo_with_origin: Callable[..., tuple[Path, Path]]
) -> None:
    bed = _Bed(tmp_path)
    repo, _ = make_git_repo_with_origin("clone")
    result = bed.run("unset CW_SESSION\n" + _setup_body(), repo)
    assert result.returncode == 0, result.stdout + result.stderr


_EARLIER_FENCES: dict[str, Callable[[], str]] = {
    "setup": lambda: _fill(impl_gate_setup_fence()),
    "monolith_preamble": lambda: _fill(_monolith_preamble()),
}


def _shorten_lock_timeout(script: Path) -> None:
    text = script.read_text(encoding="utf-8")
    rewritten = text.replace("LOCK_TIMEOUT_S = 10.0\n", "LOCK_TIMEOUT_S = 0.3\n")
    assert rewritten != text, "LOCK_TIMEOUT_S = 10.0 not found in gate_key.py"
    script.write_text(rewritten, encoding="utf-8")


@pytest.mark.parametrize("fence_name", list(_EARLIER_FENCES))
@pytest.mark.parametrize("claim_outcome", ["refused", "lock_timeout"])
def test_refused_or_failed_claim_in_a_shell_that_ran_an_earlier_fence_deletes_nothing(
    tmp_path: Path,
    make_git_repo_with_origin: Callable[..., tuple[Path, Path]],
    sleeper: Callable[[], subprocess.Popen[bytes]],
    fence_name: str,
    claim_outcome: str,
) -> None:
    bed = _Bed(tmp_path)
    clone_a, _ = make_git_repo_with_origin("clone-a")
    clone_b, _ = make_git_repo_with_origin("clone-b")
    wt_a, _, owner_a = bed.paths(clone_a)
    wt_b, tmp_b, _ = bed.paths(clone_b)
    sentinels = [_plant(wt_b, text="wt-b\n"), _plant(tmp_b, text="tmp-b\n")]
    held = None
    child = sleeper()
    if claim_outcome == "refused":
        _publish_owner(bed, clone_b, child, alive=True)
    else:
        assert bed.script is not None
        _shorten_lock_timeout(bed.script)
        key_b = gate_key_for(clone_b, _BRANCH)
        held = (bed.gate_root / f"cw-gate-owner-{key_b}.lock").open("a")
        fcntl.flock(held.fileno(), fcntl.LOCK_EX)
    fence = _EARLIER_FENCES[fence_name]()
    body = f'cd "{clone_a}"\n{fence}\ncd "{clone_b}"\n{fence}\n'
    try:
        result = bed.run(body, tmp_path)
    finally:
        if held is not None:
            held.close()
    assert result.returncode == 1, result.stdout + result.stderr
    assert f"IMPL_FAILED: gate path {wt_b} not claimed" in result.stdout
    if claim_outcome == "refused":
        assert f"gate path in use by a live run, pid {child.pid}" in result.stderr
        assert "If no run owns the path, remove" in result.stderr
    else:
        timeout_lines = [
            line
            for line in result.stderr.splitlines()
            if line.startswith("gate_key:") and "timed out" in line
        ]
        assert timeout_lines
        assert all(
            "gate path in use by a live run" not in line for line in timeout_lines
        )
    assert [path.read_text(encoding="utf-8") for path in sentinels] == [
        "wt-b\n",
        "tmp-b\n",
    ]
    assert wt_a.exists()
    assert owner_a.exists()


def test_term_during_the_gate_runs_the_removal_once(
    tmp_path: Path, make_git_repo_with_origin: Callable[..., tuple[Path, Path]]
) -> None:
    bed = _Bed(tmp_path)
    repo, _ = make_git_repo_with_origin("clone")
    tmpwt, tmpdir, owner = bed.paths(repo)
    stub_dir = _stub_git(tmp_path / "stub")
    log = tmp_path / "git.log"
    env = {
        "PATH": f"{stub_dir}:{bed.bin_dir}:{_clean_git_env()['PATH']}",
        "GATE_STUB_MODE": "term",
        "GATE_STUB_LOG": str(log),
    }
    result = bed.run(_setup_body(echo_paths=False), repo, env)
    assert result.returncode == 143, result.stdout + result.stderr
    entries = log.read_text(encoding="utf-8").splitlines()
    add = next(i for i, entry in enumerate(entries) if entry.startswith("worktree add"))
    after = [
        entry for entry in entries[add + 1 :] if entry.startswith("worktree remove")
    ]
    assert len(after) == 1, entries
    assert not owner.exists()
    assert not tmpwt.exists()
    assert not tmpdir.exists()


def test_fences_agree_on_gate_paths(
    tmp_path: Path, make_git_repo_with_origin: Callable[..., tuple[Path, Path]]
) -> None:
    bed = _Bed(tmp_path)
    repo, _ = make_git_repo_with_origin("clone")
    tmpwt, tmpdir, _ = bed.paths(repo)
    expected = f"{tmpwt}|{tmpdir}"
    setup = bed.run(_setup_body(), repo)
    assert setup.returncode == 0, setup.stdout + setup.stderr
    assert _echoed_paths(setup) == expected
    monolith = bed.run(
        _fill(_monolith_preamble()) + '\necho "PATHS=$TMPWT|$TMPDIR"\n', repo
    )
    assert monolith.returncode == 0, monolith.stdout + monolith.stderr
    assert _echoed_paths(monolith) == expected
    tmpwt.mkdir()
    gate3 = bed.run(
        _fill(
            impl_gate3_fence(),
            test_command='printf \'PATHS=%s|%s\\n\' "$PWD" "$TMPDIR"',
        ),
        repo,
    )
    assert gate3.returncode == 0, gate3.stdout + gate3.stderr
    assert _echoed_paths(gate3) == expected


@pytest.mark.parametrize(
    ("name", "code"), [("setup", 1), ("gate3", 3), ("monolith", 1)]
)
def test_missing_helper_stops_without_creating_any_gate_path(
    tmp_path: Path,
    make_git_repo_with_origin: Callable[..., tuple[Path, Path]],
    name: str,
    code: int,
) -> None:
    bed = _Bed(tmp_path, install=False)
    repo, _ = make_git_repo_with_origin("clone")
    result = bed.run(_fill(_FENCES[name]()), repo)
    assert result.returncode == code, result.stdout + result.stderr
    assert_gate_key_helper_missing(result.stdout)
    assert not [path for path in bed.gate_root.iterdir() if "cw-gate-" in path.name]


@pytest.mark.parametrize("name", ["setup", "gate3", "monolith"])
def test_a_non_hex_key_is_rejected_with_the_interpreter_version(
    tmp_path: Path,
    make_git_repo_with_origin: Callable[..., tuple[Path, Path]],
    name: str,
) -> None:
    bed = _Bed(tmp_path)
    assert bed.script is not None
    bed.script.write_text('print("nothex")\n', encoding="utf-8")
    repo, _ = make_git_repo_with_origin("clone")
    result = bed.run(_fill(_FENCES[name]()), repo)
    assert result.returncode != 0
    assert _COULD_NOT_COMPUTE.search(result.stdout), result.stdout
    assert not [path for path in bed.gate_root.iterdir() if "cw-gate-" in path.name]


def test_setup_fence_from_a_non_git_cwd_exits_non_zero(tmp_path: Path) -> None:
    bed = _Bed(tmp_path)
    plain = tmp_path / "plain"
    plain.mkdir()
    result = bed.run(_setup_body(), plain, {"GIT_CEILING_DIRECTORIES": str(tmp_path)})
    assert result.returncode != 0
    assert not [path for path in bed.gate_root.iterdir() if "cw-gate-" in path.name]
