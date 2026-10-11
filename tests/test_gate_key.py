"""Tests for .claude/scripts/gate_key.py (#2436).

The script derives the Step 2.5 gate key and owns the gate path's owner claim.
It is stdlib-only and runs under a client host's bare ``python3``, so it is
loaded by path and driven in-process (``main(argv, probe=...)`` or its public
functions), which is what lets the ``.claude/scripts`` coverage floor see it.
Process facts come from the ``Probe`` seam: a fake ``/proc`` tree, a stub
``ps`` and a fake ``kill`` keep the claim matrix hermetic, and a small
real-kernel group sits on top.
"""

from __future__ import annotations

import ast
import errno
import fcntl
import hashlib
import json
import os
import re
import runpy
import socket
import subprocess
import sys
import threading
import time
from dataclasses import replace
from pathlib import Path
from typing import TYPE_CHECKING, Any, NamedTuple

import pytest

from tests.conftest import (
    GATE_KEY_SCRIPT,
    GATE_KEY_TEST_PIDNS,
    _write_bin_stub,
    git_in,
    load_guard_script_module,
    make_claimant,
    run_guard_script_cli,
)

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator, Mapping

pytestmark = pytest.mark.usefixtures("clean_gate_env")

_mod = load_guard_script_module(GATE_KEY_SCRIPT, "gate_key")

KEY = "0123456789abcdef"
BRANCH = "feature"
_BOOT_ID = "0123abcd-4567-89ab-cdef-0123456789ab"
_PS_TIME = "Sat Oct 11 12:34:56 2026"

# The claimant's own chain in the fake tree: OWN (the script) -> SHELL (the
# fence's bash) -> WORKER (the claude process) -> 1.
WORKER, SHELL, OWN = 4100, 4200, 4300
OTHER_WORKER, OTHER_SHELL = 5100, 5200
DEAD_WORKER, DEAD_SHELL = 6100, 6200
_DEAD_TOKEN = f"proc:{_BOOT_ID}:77"


class FakeProc(NamedTuple):
    """One process in a fake ``/proc`` tree."""

    start: int | str
    ppid: int = 1
    state: str = "S"
    comm: str = "bash"


def fake_proc_tree(
    tmp_path: Path,
    procs: Mapping[int, FakeProc],
    *,
    pidns: str | None = GATE_KEY_TEST_PIDNS,
    name: str = "proc",
    boot_id: str = _BOOT_ID,
    shell_pid: int | None = SHELL,
    own_pid: int | None = OWN,
) -> Any:
    """Write a Linux-format ``/proc`` tree and return a ``Probe`` over it.

    The probe's ``kill`` raises ``ProcessLookupError`` for any pid absent from
    *procs*; ``pidns=None`` leaves out the ``self/ns/pid`` link.
    """
    root = tmp_path / name
    random_dir = root / "sys" / "kernel" / "random"
    random_dir.mkdir(parents=True, exist_ok=True)
    (random_dir / "boot_id").write_text(f"{boot_id}\n", encoding="utf-8")
    for pid, proc in procs.items():
        proc_dir = root / str(pid)
        proc_dir.mkdir(exist_ok=True)
        rest = [proc.state, str(proc.ppid), *["0"] * 17, str(proc.start), *["0"] * 10]
        line = f"{pid} ({proc.comm}) " + " ".join(rest) + "\n"
        (proc_dir / "stat").write_text(line, encoding="utf-8")
    if pidns is not None:
        ns_dir = root / "self" / "ns"
        ns_dir.mkdir(parents=True, exist_ok=True)
        (ns_dir / "pid").symlink_to(pidns)
    live = set(procs)

    def _kill(pid: int, _sig: int) -> None:
        if pid not in live:
            raise ProcessLookupError(pid)

    return _mod.Probe(proc_root=root, kill=_kill, own_pid=own_pid, shell_pid=shell_pid)


def _base_procs(**extra: FakeProc) -> dict[int, FakeProc]:
    procs = {
        WORKER: FakeProc(11, 1),
        SHELL: FakeProc(12, WORKER),
        OWN: FakeProc(13, SHELL),
        OTHER_WORKER: FakeProc(21, 1),
        OTHER_SHELL: FakeProc(22, OTHER_WORKER),
    }
    procs.update({int(pid): proc for pid, proc in extra.items()})
    return procs


def _tree(tmp_path: Path, **kwargs: Any) -> Any:
    return fake_proc_tree(tmp_path, _base_procs(), **kwargs)


def tok(probe: Any, pid: int) -> str:
    token = _mod.process_start_token(pid, probe)
    assert token is not None, f"no token for {pid}"
    return str(token)


def _claimant(probe: Any, worker: int = WORKER, shell: int = SHELL, **kw: Any) -> Any:
    return make_claimant(
        _mod, (worker, tok(probe, worker)), (shell, tok(probe, shell)), **kw
    )


def _record(claimant: Any, **overrides: object) -> dict[str, object]:
    record: dict[str, object] = _mod.build_record(KEY, BRANCH, claimant)
    record.update(overrides)
    return record


def _dead_record(**overrides: object) -> dict[str, object]:
    record: dict[str, object] = {
        "v": 1,
        "pid": DEAD_WORKER,
        "start": _DEAD_TOKEN,
        "shell_pid": DEAD_SHELL,
        "shell_start": _DEAD_TOKEN,
        "host": socket.gethostname(),
        "pidns": GATE_KEY_TEST_PIDNS,
        "key": KEY,
        "branch": BRANCH,
        "claimed_at": "2026-10-11T00:00:00Z",
        "source": "CLAUDE_PID",
    }
    record.update(overrides)
    return record


def _write_owner(gate_root: Path, record: object) -> Path:
    gate_root.mkdir(parents=True, exist_ok=True)
    owner = Path(_mod.owner_path(gate_root, KEY))
    text = record if isinstance(record, str) else json.dumps(record)
    owner.write_text(text, encoding="utf-8")
    return owner


def _owner_json(gate_root: Path) -> dict[str, Any]:
    data: dict[str, Any] = json.loads(
        Path(_mod.owner_path(gate_root, KEY)).read_text(encoding="utf-8")
    )
    return data


def _stub_ps(tmp_path: Path, body: str) -> str:
    """A ``ps`` stub; *body* is shell run with ``$2`` = ``<field>=``, ``$4`` = pid."""
    tmp_path.mkdir(parents=True, exist_ok=True)
    return str(_write_bin_stub(tmp_path, "ps", f"#!/bin/sh\n{body}\n") / "ps")


def _ps_probe(tmp_path: Path, ps_cmd: str, **kwargs: Any) -> Any:
    def _kill(_pid: int, _sig: int) -> None:
        return None

    defaults: dict[str, Any] = {"shell_pid": SHELL, "own_pid": OWN, "kill": _kill}
    defaults.update(kwargs)
    return _mod.Probe(proc_root=tmp_path / "no-proc", ps_cmd=ps_cmd, **defaults)


def _claim_main(gate_root: Path, probe: Any, *extra: str) -> int:
    code: int = _mod.main(
        [
            "--branch",
            BRANCH,
            "--claim",
            "--key",
            KEY,
            "--gate-root",
            str(gate_root),
            *extra,
        ],
        probe=probe,
    )
    return code


def _release_main(gate_root: Path, probe: Any) -> int:
    code: int = _mod.main(
        ["--release", "--key", KEY, "--gate-root", str(gate_root)], probe=probe
    )
    return code


# --- Key ---------------------------------------------------------------------


def test_key_is_sha256_prefix_of_common_dir_and_branch(
    make_git_repo: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    repo = make_git_repo("clone")
    monkeypatch.chdir(repo)
    common = os.path.realpath(repo / ".git")
    expected = hashlib.sha256(f"{common}\n{BRANCH}\n".encode()).hexdigest()[:16]
    assert _mod.main(["--branch", BRANCH]) == 0
    captured = capsys.readouterr()
    assert captured.out == f"{expected}\n"
    assert captured.err == ""
    assert re.fullmatch(r"[0-9a-f]{16}", expected)


def _key_from(cwd: Path, branch: str, monkeypatch: pytest.MonkeyPatch) -> str:
    monkeypatch.chdir(cwd)
    return str(_mod.compute_key(_mod.git_common_dir(), branch))


def test_key_identical_from_main_checkout_linked_worktree_and_subdirectory(
    make_git_repo: Callable[..., Path], monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    repo = make_git_repo("clone")
    linked = tmp_path / "linked"
    git_in(repo, "worktree", "add", "--detach", str(linked))
    subdir = repo / "a" / "b"
    subdir.mkdir(parents=True)
    keys = {_key_from(cwd, BRANCH, monkeypatch) for cwd in (repo, linked, subdir)}
    assert len(keys) == 1


def test_key_differs_per_clone_and_per_branch(
    make_git_repo: Callable[..., Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    clone_a = make_git_repo("clone-a")
    clone_b = make_git_repo("clone-b")
    a_feature = _key_from(clone_a, "feature", monkeypatch)
    a_other = _key_from(clone_a, "other", monkeypatch)
    b_feature = _key_from(clone_b, "feature", monkeypatch)
    assert len({a_feature, a_other, b_feature}) == 3


def test_key_ignores_cw_session(
    make_git_repo: Callable[..., Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = make_git_repo("clone")
    monkeypatch.setenv("CW_SESSION", "9141a90d")
    first = _key_from(repo, BRANCH, monkeypatch)
    monkeypatch.setenv("CW_SESSION", "7d5c53c2")
    assert _key_from(repo, BRANCH, monkeypatch) == first


# --- Key failure paths -------------------------------------------------------


def _assert_key_failure(
    capsys: pytest.CaptureFixture[str], argv: list[str], message: str
) -> None:
    assert _mod.main(argv) == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err.startswith("gate_key: ")
    assert message in captured.err


def test_outside_a_git_repo_exits_1(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    plain = tmp_path / "plain"
    plain.mkdir()
    monkeypatch.chdir(plain)
    monkeypatch.setenv("GIT_CEILING_DIRECTORIES", str(tmp_path))
    _assert_key_failure(
        capsys, ["--branch", BRANCH], "git rev-parse --git-common-dir exited 128"
    )


def test_git_failure_exits_1(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    bin_dir = _write_bin_stub(tmp_path, "git", "#!/bin/sh\necho boom >&2\nexit 128\n")
    monkeypatch.setenv("PATH", f"{bin_dir}:{os.environ['PATH']}")
    _assert_key_failure(capsys, ["--branch", BRANCH], "exited 128: boom")


def test_empty_git_common_dir_exits_1(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    bin_dir = _write_bin_stub(tmp_path, "git", "#!/bin/sh\nexit 0\n")
    monkeypatch.setenv("PATH", f"{bin_dir}:{os.environ['PATH']}")
    _assert_key_failure(capsys, ["--branch", BRANCH], "printed no git common dir")


def test_git_not_runnable_exits_1(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    empty = tmp_path / "empty-bin"
    empty.mkdir()
    monkeypatch.setenv("PATH", str(empty))
    _assert_key_failure(capsys, ["--branch", BRANCH], "could not run")


def test_git_timeout_exits_1(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    bin_dir = _write_bin_stub(tmp_path, "git", "#!/bin/sh\nsleep 5\n")
    monkeypatch.setenv("PATH", f"{bin_dir}:{os.environ['PATH']}")
    monkeypatch.setattr(_mod, "GIT_TIMEOUT_S", 0.2)
    _assert_key_failure(capsys, ["--branch", BRANCH], "could not run")


def test_empty_branch_exits_1(
    make_git_repo: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.chdir(make_git_repo("clone"))
    _assert_key_failure(capsys, ["--branch", ""], "empty branch")


def test_compute_key_rejects_an_empty_common_dir() -> None:
    with pytest.raises(_mod.GateKeyError, match="empty git common dir"):
        _mod.compute_key("", BRANCH)


def test_missing_branch_is_usage_error_2() -> None:
    with pytest.raises(SystemExit) as exc_info:
        _mod.main([])
    assert exc_info.value.code == 2


@pytest.mark.parametrize("value", ["", "xyz", "0123456789ABCDEF", "0123456789abcde"])
def test_require_hex_key_rejects_empty_and_non_hex(value: str) -> None:
    with pytest.raises(_mod.GateKeyError, match="not a 16-hex gate key"):
        _mod.require_hex_key(value)


def test_require_hex_key_accepts_16_lowercase_hex() -> None:
    assert _mod.require_hex_key(KEY) == KEY


# --- Usage and validation ----------------------------------------------------


def _assert_usage_error(argv: list[str]) -> None:
    with pytest.raises(SystemExit) as exc_info:
        _mod.main(argv)
    assert exc_info.value.code == 2


def test_claim_without_gate_root_is_usage_error_even_with_cw_gate_root_set(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CW_GATE_ROOT", str(tmp_path / "gate-root"))
    argv = ["--branch", BRANCH, "--claim", "--key", KEY]
    result = run_guard_script_cli(GATE_KEY_SCRIPT, argv)
    assert result.returncode == 2, result.stderr
    assert "--gate-root" in result.stderr
    _assert_usage_error(argv)
    assert not (tmp_path / "gate-root").exists()


@pytest.mark.parametrize(
    "argv",
    [
        ["--release", "--gate-root", "g"],
        ["--branch", BRANCH, "--claim", "--gate-root", "g"],
        ["--claim", "--key", KEY, "--gate-root", "g"],
        ["--branch", BRANCH, "--claim", "--release", "--key", KEY, "--gate-root", "g"],
        ["--branch", BRANCH, "--key", KEY],
        ["--key", KEY],
        ["--branch", BRANCH, "--gate-root", "g"],
        ["--release", "--key", KEY],
    ],
    ids=[
        "release_without_key",
        "claim_without_key",
        "claim_without_branch",
        "claim_and_release",
        "key_in_key_mode",
        "key_alone",
        "gate_root_in_key_mode",
        "release_without_gate_root",
    ],
)
def test_usage_errors_exit_2(argv: list[str]) -> None:
    _assert_usage_error(argv)


def test_release_with_a_malformed_key_exits_1(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    argv = ["--release", "--key", "nothex", "--gate-root", str(tmp_path)]
    assert _mod.main(argv) == 1
    assert "not a 16-hex gate key" in capsys.readouterr().err


def test_claim_with_a_malformed_key_exits_1_and_writes_nothing(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    gate_root = tmp_path / "gate-root"
    argv = ["--branch", BRANCH, "--claim", "--key", "nothex"]
    assert _mod.main([*argv, "--gate-root", str(gate_root)]) == 1
    assert "not a 16-hex gate key" in capsys.readouterr().err
    assert not gate_root.exists()


def test_claim_with_a_gate_root_that_is_a_file_exits_1(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    gate_root = tmp_path / "gate-root"
    gate_root.write_text("not a directory\n", encoding="utf-8")
    probe = _tree(tmp_path)
    assert _claim_main(gate_root, probe) == 1
    assert "cannot use gate root" in capsys.readouterr().err


# --- Process facts -----------------------------------------------------------


def test_proc_start_token_parses_stat_with_parens_in_comm(tmp_path: Path) -> None:
    probe = fake_proc_tree(tmp_path, {77: FakeProc(987654, 1, comm="(a b) c)")})
    assert _mod.process_start_token(77, probe) == f"proc:{_BOOT_ID}:987654"
    assert _mod.parent_pid(77, probe) == 1
    assert _mod.process_state(77, probe) == "S"


def test_proc_start_token_none_for_missing_short_or_boot_id_less_proc(
    tmp_path: Path,
) -> None:
    probe = fake_proc_tree(tmp_path, {77: FakeProc(5, 1)})
    assert _mod.process_start_token(78, probe) is None
    short = probe.proc_root / "79"
    short.mkdir()
    (short / "stat").write_text("79 (x) S 1 0\n", encoding="utf-8")
    assert _mod.process_start_token(79, probe) is None
    no_paren = probe.proc_root / "80"
    no_paren.mkdir()
    (no_paren / "stat").write_text("garbage\n", encoding="utf-8")
    assert _mod.process_start_token(80, probe) is None
    assert _mod.parent_pid(80, probe) is None
    assert _mod.process_state(80, probe) is None
    (probe.proc_root / "sys" / "kernel" / "random" / "boot_id").unlink()
    assert _mod.process_start_token(77, probe) is None


def test_ps_fallback_when_proc_root_absent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    printing = _ps_probe(tmp_path / "a", _stub_ps(tmp_path / "a", f'echo "{_PS_TIME}"'))
    assert _mod.process_start_token(42, printing) == f"ps:{_PS_TIME}"
    silent = _ps_probe(tmp_path / "b", _stub_ps(tmp_path / "b", "exit 0"))
    assert _mod.process_start_token(42, silent) is None
    failing = _ps_probe(tmp_path / "c", _stub_ps(tmp_path / "c", "exit 1"))
    assert _mod.process_start_token(42, failing) is None
    missing = _ps_probe(tmp_path / "d", str(tmp_path / "d" / "no-such-ps"))
    assert _mod.process_start_token(42, missing) is None
    slow = _ps_probe(tmp_path / "e", _stub_ps(tmp_path / "e", "sleep 5"))
    monkeypatch.setattr(_mod, "PS_TIMEOUT_S", 0.2)
    assert _mod.process_start_token(42, slow) is None


def test_parent_pid_via_proc_and_ps(tmp_path: Path) -> None:
    probe = _tree(tmp_path)
    assert _mod.parent_pid(SHELL, probe) == WORKER
    assert _mod.parent_pid(9999, probe) is None
    ps_probe = _ps_probe(tmp_path, _stub_ps(tmp_path, 'echo "  42"'))
    assert _mod.parent_pid(7, ps_probe) == 42
    bad = _ps_probe(tmp_path / "b", _stub_ps(tmp_path / "b", "echo nope"))
    assert _mod.parent_pid(7, bad) is None
    none = _ps_probe(tmp_path / "c", _stub_ps(tmp_path / "c", "exit 1"))
    assert _mod.parent_pid(7, none) is None


def test_is_ancestor_chain_true_false_unreadable_link_cycle_and_pid_1(
    tmp_path: Path,
) -> None:
    procs = _base_procs()
    procs.update(
        {7001: FakeProc(1, 7002), 7002: FakeProc(1, 7001), 7003: FakeProc(1, 0)}
    )
    probe = fake_proc_tree(tmp_path, procs)
    assert _mod.is_ancestor(WORKER, OWN, probe)
    assert _mod.is_ancestor(SHELL, OWN, probe)
    assert not _mod.is_ancestor(OTHER_WORKER, OWN, probe)
    assert not _mod.is_ancestor(WORKER, 9999, probe)
    assert not _mod.is_ancestor(WORKER, 7001, probe)
    assert not _mod.is_ancestor(WORKER, 7003, probe)
    assert _mod.is_ancestor(1, OWN, probe)


def _probe_with_kill(probe: Any, error: BaseException) -> Any:
    def _kill(_pid: int, _sig: int) -> None:
        raise error

    return replace(probe, kill=_kill)


def test_proc_alive(tmp_path: Path) -> None:
    probe = _tree(tmp_path)
    live = _mod.Proc(WORKER, tok(probe, WORKER))
    assert _mod.proc_alive(live, probe)
    assert not _mod.proc_alive(_mod.Proc(9999, _DEAD_TOKEN), probe)
    assert _mod.proc_alive(live, _probe_with_kill(probe, PermissionError()))
    assert _mod.proc_alive(live, _probe_with_kill(probe, OSError(errno.EIO, "x")))
    assert not _mod.proc_alive(_mod.Proc(WORKER, _DEAD_TOKEN), probe)
    (probe.proc_root / str(WORKER) / "stat").unlink()
    assert _mod.proc_alive(_mod.Proc(WORKER, _DEAD_TOKEN), probe)


# --- Token shape and the ps environment (R18, R25) ---------------------------


def test_ps_runs_with_pinned_locale_and_timezone(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    body = (
        'if [ "$TZ" = UTC ] && [ "$LC_ALL" = C ]; then\n'
        f'  echo "{_PS_TIME}"\n'
        "else\n"
        '  echo "Sa Okt 11 08:34:56 2026"\n'
        "fi"
    )
    probe = _ps_probe(tmp_path, _stub_ps(tmp_path, body))
    monkeypatch.setenv("TZ", "America/New_York")
    monkeypatch.setenv("LC_ALL", "de_DE.UTF-8")
    first = _mod.process_start_token(42, probe)
    monkeypatch.setenv("TZ", "Asia/Tokyo")
    assert first == f"ps:{_PS_TIME}"
    assert _mod.process_start_token(42, probe) == first


def test_ps_token_normalises_a_padded_day(tmp_path: Path) -> None:
    probe = _ps_probe(tmp_path, _stub_ps(tmp_path, 'echo "Sat Oct  3 08:01:02 2026"'))
    assert _mod.process_start_token(42, probe) == "ps:Sat Oct 3 08:01:02 2026"
    assert _mod.foreign_start_token(42, probe) == "ps:Sat Oct 3 08:01:02 2026"


def _malformed_owner_claim(
    tmp_path: Path, probe: Any, claimant: Any
) -> tuple[int, str]:
    gate_root = tmp_path / "gate-root"
    _write_owner(
        gate_root,
        _dead_record(
            pid=OTHER_WORKER,
            shell_pid=OTHER_SHELL,
            start=f"ps:{_PS_TIME}",
            shell_start=f"ps:{_PS_TIME}",
            pidns=claimant.pidns,
        ),
    )
    code, text = _mod.claim(gate_root, KEY, BRANCH, claimant, probe)
    return int(code), str(text)


@pytest.mark.parametrize(
    "output", ["garbage", "Sat Oct 11 12:34 2026", "Sat Oct 11 12:34:56 26"]
)
def test_foreign_malformed_ps_tokens_are_unreadable_not_dead(
    tmp_path: Path, output: str
) -> None:
    probe = _ps_probe(tmp_path, _stub_ps(tmp_path, f'echo "{output}"'))
    assert _mod.process_start_token(42, probe) == f"ps:{output}"
    assert _mod.foreign_start_token(42, probe) is None
    assert _mod.proc_alive(_mod.Proc(42, f"ps:{_PS_TIME}"), probe)
    claimant = make_claimant(
        _mod, (WORKER, "ps:a"), (SHELL, "ps:b"), pidns=None, source="CLAUDE_PID"
    )
    code, text = _malformed_owner_claim(tmp_path, probe, claimant)
    assert code == 3
    assert f"gate path in use by a live run, pid {OTHER_WORKER}" in text
    assert "If no run owns the path, remove" in text


@pytest.mark.parametrize(
    ("boot_id", "start", "short"),
    [
        ("not-a-uuid", 11, False),
        (_BOOT_ID, "abc", False),
        (_BOOT_ID, 11, True),
    ],
    ids=["boot_id_not_uuid", "start_not_digits", "short_stat"],
)
def test_malformed_proc_tokens_are_unreadable_not_dead(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    boot_id: str,
    start: int | str,
    short: bool,
) -> None:
    procs = {pid: FakeProc(start, 1) for pid in (OTHER_WORKER, OTHER_SHELL, SHELL)}
    probe = fake_proc_tree(tmp_path, procs, boot_id=boot_id)
    if short:
        for pid in procs:
            (probe.proc_root / str(pid) / "stat").write_text(
                f"{pid} (x) S 1 0 0\n", encoding="utf-8"
            )
    assert _mod.process_start_token(OTHER_WORKER, probe) is None
    assert _mod.proc_alive(_mod.Proc(OTHER_WORKER, _DEAD_TOKEN), probe)
    claimant = make_claimant(_mod, (WORKER, _DEAD_TOKEN), (SHELL, _DEAD_TOKEN))
    gate_root = tmp_path / "gate-root"
    _write_owner(gate_root, _dead_record(pid=OTHER_WORKER, shell_pid=OTHER_SHELL))
    code, text = _mod.claim(gate_root, KEY, BRANCH, claimant, probe)
    assert code == 3
    assert f"gate path in use by a live run, pid {OTHER_WORKER}" in text
    assert _claim_main(tmp_path / "other-root", probe) == 1
    assert "cannot record an owner" in capsys.readouterr().err


def _verbatim_ps_body() -> str:
    return (
        'case "$2" in\n'
        "  lstart=) echo garbage ;;\n"
        f'  ppid=) case "$4" in {OWN}) echo {SHELL} ;; {SHELL}) echo {WORKER} ;;'
        " *) echo 1 ;; esac ;;\n"
        "  stat=) echo S ;;\n"
        "esac"
    )


def test_claimants_own_ps_token_is_recorded_verbatim(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    probe = _ps_probe(tmp_path, _stub_ps(tmp_path, _verbatim_ps_body()))
    monkeypatch.setenv("CW_GATE_OWNER_PID", str(WORKER))
    gate_root = tmp_path / "gate-root"
    assert _claim_main(gate_root, probe) == 0
    record = _owner_json(gate_root)
    assert record["shell_start"] == "ps:garbage"
    assert record["start"] == "ps:garbage"
    assert record["pid"] == WORKER
    assert "cannot record an owner" not in capsys.readouterr().err
    assert _release_main(gate_root, probe) == 0
    assert not Path(_mod.owner_path(gate_root, KEY)).exists()

    assert _claim_main(gate_root, probe) == 0
    assert _claim_main(gate_root, probe) == 3
    err = capsys.readouterr().err
    assert "(bad token in shell_start)" in err


def test_claimants_empty_ps_shell_token_exits_1_with_m8(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    probe = _ps_probe(tmp_path, _stub_ps(tmp_path, "exit 0"))
    gate_root = tmp_path / "gate-root"
    assert _claim_main(gate_root, probe) == 1
    assert "cannot record an owner" in capsys.readouterr().err
    assert not Path(_mod.owner_path(gate_root, KEY)).exists()


def test_recorded_token_failing_the_shape_check_is_a_malformed_record(
    tmp_path: Path,
) -> None:
    probe = _tree(tmp_path)
    gate_root = tmp_path / "gate-root"
    owner = _write_owner(gate_root, _dead_record(start="not-a-token"))
    code, text = _mod.claim(gate_root, KEY, BRANCH, _claimant(probe), probe)
    assert code == 3
    assert text == _mod.refuse_malformed(owner, "bad token in start")


# --- Pid namespace probe (R17) -----------------------------------------------


def test_read_pidns_reads_the_self_ns_pid_link(tmp_path: Path) -> None:
    assert _mod.read_pidns(_tree(tmp_path / "a")) == GATE_KEY_TEST_PIDNS
    assert _mod.read_pidns(_tree(tmp_path / "b", pidns=None)) is None
    no_self = _tree(tmp_path / "c")
    (no_self.proc_root / "self" / "ns" / "pid").unlink()
    (no_self.proc_root / "self" / "ns").rmdir()
    assert _mod.read_pidns(no_self) is None
    not_a_dir = tmp_path / "proc-file"
    not_a_dir.write_text("x", encoding="utf-8")
    assert _mod.read_pidns(_mod.Probe(proc_root=not_a_dir)) is None
    real_link = Path("/proc/self/ns/pid")
    if real_link.is_symlink():
        assert _mod.read_pidns(_mod.Probe()) == str(real_link.readlink())


# --- Durability and edge cases (R22) -----------------------------------------


def _spy_fsync_and_replace(
    monkeypatch: pytest.MonkeyPatch, *, fsync_error: OSError | None = None
) -> list[str]:
    events: list[str] = []
    real_fsync = os.fsync
    real_replace = Path.replace

    def _fsync(fd: int) -> None:
        events.append("fsync")
        if fsync_error is not None:
            raise fsync_error
        real_fsync(fd)

    def _replace(self: Path, target: str | os.PathLike[str]) -> Path:
        events.append("replace")
        assert json.loads(self.read_text(encoding="utf-8"))["v"] == 1
        return real_replace(self, target)

    monkeypatch.setattr(os, "fsync", _fsync)
    monkeypatch.setattr(Path, "replace", _replace)
    return events


def test_publish_fsyncs_the_temp_file_before_replace(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    probe = _tree(tmp_path)
    gate_root = tmp_path / "gate-root"
    events = _spy_fsync_and_replace(monkeypatch)
    code, _ = _mod.claim(gate_root, KEY, BRANCH, _claimant(probe), probe)
    assert code == 0
    assert events == ["fsync", "replace"]


def test_publish_fsync_failure_exits_1_and_keeps_the_old_record(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    probe = _tree(tmp_path)
    gate_root = tmp_path / "gate-root"
    owner = _write_owner(gate_root, _dead_record())
    before = owner.read_text(encoding="utf-8")
    events = _spy_fsync_and_replace(monkeypatch, fsync_error=OSError(errno.EIO, "io"))
    code, text = _mod.claim(gate_root, KEY, BRANCH, _claimant(probe), probe)
    assert code == 1
    assert "could not publish the owner record" in text
    assert events == ["fsync"]
    assert owner.read_text(encoding="utf-8") == before
    assert not owner.with_name(owner.name + ".tmp").exists()


def test_zombie_state_reads_as_dead_on_proc(tmp_path: Path) -> None:
    procs = _base_procs()
    procs[DEAD_WORKER] = FakeProc(77, 1, state="Z")
    procs[DEAD_SHELL] = FakeProc(77, 1, state="Z")
    probe = fake_proc_tree(tmp_path, procs)
    assert not _mod.proc_alive(_mod.Proc(DEAD_WORKER, _DEAD_TOKEN), probe)
    assert _mod.proc_alive(_mod.Proc(WORKER, tok(probe, WORKER)), probe)
    gate_root = tmp_path / "gate-root"
    _write_owner(gate_root, _dead_record())
    code, text = _mod.claim(gate_root, KEY, BRANCH, _claimant(probe), probe)
    assert code == 0
    assert "previous owner is gone" in text


@pytest.mark.parametrize(("stat", "alive"), [("Z+", False), ("S", True)])
def test_zombie_state_reads_as_dead_on_ps(
    tmp_path: Path, stat: str, alive: bool
) -> None:
    body = f'case "$2" in lstart=) echo "{_PS_TIME}" ;; stat=) echo "{stat}" ;; esac'
    probe = _ps_probe(tmp_path, _stub_ps(tmp_path, body))
    assert _mod.process_state(42, probe) == stat[0]
    assert _mod.proc_alive(_mod.Proc(42, f"ps:{_PS_TIME}"), probe) is alive


def test_overflowing_pid_reads_as_alive_without_a_traceback(
    tmp_path: Path, sleeper: Callable[[], subprocess.Popen[bytes]]
) -> None:
    probe = _mod.Probe()
    huge = 2**70
    with pytest.raises(OverflowError):
        os.kill(huge, 0)
    assert _mod.proc_alive(_mod.Proc(huge, _DEAD_TOKEN), probe)
    child = sleeper()
    pidns = _mod.read_pidns(probe)
    claimant = make_claimant(_mod, child.pid, child.pid, pidns=pidns)
    gate_root = tmp_path / "gate-root"
    _write_owner(gate_root, _dead_record(pid=huge, shell_pid=huge, pidns=pidns))
    code, text = _mod.claim(gate_root, KEY, BRANCH, claimant, probe)
    assert code == 3
    assert f"gate path in use by a live run, pid {huge}" in text


# --- Interpreter and syntax floor (R16, R24) ---------------------------------


def _script_tree() -> ast.Module:
    return ast.parse(GATE_KEY_SCRIPT.read_text(encoding="utf-8"))


def _call_name(node: ast.Call) -> str:
    func = node.func
    if isinstance(func, ast.Name):
        return func.id
    if isinstance(func, ast.Attribute):
        return func.attr
    return ""


def test_script_obeys_the_python_39_floor() -> None:
    source = GATE_KEY_SCRIPT.read_text(encoding="utf-8")
    ast.parse(source, feature_version=(3, 9))
    tree = _script_tree()
    first = tree.body[1]
    assert isinstance(first, ast.ImportFrom)
    assert first.module == "__future__"
    assert [alias.name for alias in first.names] == ["annotations"]
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            assert all(alias.name != "datetime" for alias in node.names)
        if isinstance(node, ast.ImportFrom):
            assert node.module != "datetime"
            assert all(alias.name != "UTC" for alias in node.names)
        if isinstance(node, ast.Attribute):
            assert node.attr != "UTC"
        if isinstance(node, ast.Call):
            name = _call_name(node)
            keywords = {kw.arg for kw in node.keywords}
            assert name != "zip"
            if name == "dataclass":
                assert not keywords & {"slots", "kw_only"}
            if name == "isinstance":
                assert not isinstance(node.args[1], ast.BinOp)


def test_script_passes_the_ruff_syntax_check_at_the_python_39_floor(
    tmp_path: Path,
) -> None:
    command = [
        sys.executable,
        "-m",
        "ruff",
        "check",
        "--isolated",
        "--target-version",
        "py39",
        "--select",
        "E9",
        "--quiet",
    ]
    result = subprocess.run(
        [*command, str(GATE_KEY_SCRIPT)], capture_output=True, text=True, check=False
    )
    assert result.returncode == 0, result.stdout
    assert result.stdout == ""
    assert result.stderr == ""
    control = tmp_path / "nested_quote.py"
    control.write_text('d = {"k": 1}\nx = f"{d["k"]}"\n', encoding="utf-8")
    bad = subprocess.run(
        [*command, str(control)], capture_output=True, text=True, check=False
    )
    assert bad.returncode != 0
    assert "Cannot reuse outer quote character in f-strings" in bad.stdout


def test_claimed_at_is_a_utc_strftime_stamp(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    epoch = 1_760_000_000.0
    claimant = _claimant(_tree(tmp_path))
    with monkeypatch.context() as patch:
        patch.setattr(_mod.time, "time", lambda: epoch)
        record = _mod.build_record(KEY, BRANCH, claimant)
    expected = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(epoch))
    assert record["claimed_at"] == expected
    assert re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z", expected)


# --- Claimant resolution (R3 amendment 1, R7, R13) ---------------------------


def _resolve(probe: Any, environ: dict[str, str]) -> Any:
    return _mod.resolve_claimant(environ, probe)


def test_cw_gate_owner_pid_beats_claude_pid(tmp_path: Path) -> None:
    probe = _tree(tmp_path)
    claimant = _resolve(
        probe, {"CW_GATE_OWNER_PID": str(WORKER), "CLAUDE_PID": str(SHELL)}
    )
    assert claimant.source == "CW_GATE_OWNER_PID"
    assert claimant.worker.pid == WORKER
    assert claimant.shell.pid == SHELL
    assert claimant.note == ""
    assert claimant.pidns == GATE_KEY_TEST_PIDNS


def test_claude_pid_used_when_only_it_is_set_and_is_an_ancestor(tmp_path: Path) -> None:
    probe = _tree(tmp_path)
    claimant = _resolve(probe, {"CLAUDE_PID": str(WORKER)})
    assert claimant.source == "CLAUDE_PID"
    assert claimant.worker == _mod.Proc(WORKER, tok(probe, WORKER))


@pytest.mark.parametrize(
    ("environ", "note"),
    [
        ({}, "neither CW_GATE_OWNER_PID nor CLAUDE_PID is set"),
        ({"CLAUDE_PID": "abc"}, "CLAUDE_PID is not a positive integer (got 'abc')"),
        ({"CLAUDE_PID": "0"}, "CLAUDE_PID is not a positive integer (got '0')"),
        ({"CLAUDE_PID": "-5"}, "CLAUDE_PID is not a positive integer (got '-5')"),
        ({"CLAUDE_PID": ""}, "CLAUDE_PID is not a positive integer (got '')"),
        ({"CLAUDE_PID": "9999"}, "CLAUDE_PID=9999 has no readable start token"),
        (
            {"CLAUDE_PID": str(OTHER_WORKER)},
            f"CLAUDE_PID={OTHER_WORKER} is not an ancestor of this process",
        ),
        (
            {"CW_GATE_OWNER_PID": "x", "CLAUDE_PID": str(WORKER)},
            "CW_GATE_OWNER_PID is not a positive integer (got 'x'); CLAUDE_PID is"
            " set but not consulted",
        ),
        (
            {"CW_GATE_OWNER_PID": str(OTHER_WORKER), "CLAUDE_PID": str(WORKER)},
            f"CW_GATE_OWNER_PID={OTHER_WORKER} is not an ancestor of this process;"
            " CLAUDE_PID is set but not consulted",
        ),
        (
            {"CW_GATE_OWNER_PID": "y" * 60},
            "CW_GATE_OWNER_PID is not a positive integer (got '" + "y" * 40 + "')",
        ),
    ],
    ids=[
        "neither",
        "non_numeric",
        "zero",
        "negative",
        "empty",
        "unreadable_token",
        "not_ancestor",
        "invalid_override_does_not_fall_through",
        "non_ancestor_override_does_not_fall_through",
        "value_truncated",
    ],
)
def test_unidentified_claimant_falls_back_to_the_shell_pid(
    tmp_path: Path, environ: dict[str, str], note: str
) -> None:
    probe = _tree(tmp_path)
    claimant = _resolve(probe, environ)
    assert claimant.source == "shell-pid"
    assert claimant.worker == claimant.shell
    assert claimant.shell == _mod.Proc(SHELL, tok(probe, SHELL))
    assert claimant.note == note


def test_unreadable_shell_token_resolves_to_none(tmp_path: Path) -> None:
    probe = _tree(tmp_path)
    (probe.proc_root / str(SHELL) / "stat").unlink()
    assert _resolve(probe, {"CLAUDE_PID": str(WORKER)}) is None


# --- Claim / judge matrix ----------------------------------------------------


def _claim(tmp_path: Path, probe: Any, claimant: Any, seed: object | None) -> Any:
    gate_root = tmp_path / "gate-root"
    owner = Path(_mod.owner_path(gate_root, KEY))
    before = None
    if seed is not None:
        _write_owner(gate_root, seed)
        before = owner.read_bytes()
    code, text = _mod.claim(gate_root, KEY, BRANCH, claimant, probe)
    return code, text, owner, before


def test_absent_file_claims(tmp_path: Path) -> None:
    probe = _tree(tmp_path)
    claimant = _claimant(probe)
    code, text, owner, _ = _claim(tmp_path, probe, claimant, None)
    assert (code, text) == (0, "")
    record = json.loads(owner.read_text(encoding="utf-8"))
    assert record == {
        "v": 1,
        "pid": WORKER,
        "start": tok(probe, WORKER),
        "shell_pid": SHELL,
        "shell_start": tok(probe, SHELL),
        "host": socket.gethostname(),
        "pidns": GATE_KEY_TEST_PIDNS,
        "key": KEY,
        "branch": BRANCH,
        "claimed_at": record["claimed_at"],
        "source": "CLAUDE_PID",
    }
    assert all(value is not None for value in record.values())


def test_absent_file_claim_records_a_null_pidns_when_unreadable(tmp_path: Path) -> None:
    probe = _tree(tmp_path, pidns=None)
    claimant = _claimant(probe, pidns=None)
    code, _, owner, _ = _claim(tmp_path, probe, claimant, None)
    assert code == 0
    assert json.loads(owner.read_text(encoding="utf-8"))["pidns"] is None


def test_live_foreign_worker_refuses(tmp_path: Path) -> None:
    probe = _tree(tmp_path)
    seed = _record(_claimant(probe, OTHER_WORKER, OTHER_SHELL))
    code, text, owner, before = _claim(tmp_path, probe, _claimant(probe), seed)
    assert code == 3
    assert text.startswith(
        f"gate_key: gate path in use by a live run, pid {OTHER_WORKER}"
    )
    assert str(owner) in text
    assert text.endswith(f"If no run owns the path, remove {owner} and re-run")
    assert owner.read_bytes() == before


def test_dead_worker_and_dead_shell_is_taken_over(tmp_path: Path) -> None:
    probe = _tree(tmp_path)
    code, text, owner, _ = _claim(tmp_path, probe, _claimant(probe), _dead_record())
    assert code == 0
    assert text == _mod.note_both_dead(DEAD_WORKER, DEAD_SHELL, owner)
    assert json.loads(owner.read_text(encoding="utf-8"))["pid"] == WORKER


def test_recycled_pid_reads_dead(tmp_path: Path) -> None:
    probe = _tree(tmp_path)
    seed = _dead_record(pid=OTHER_WORKER, shell_pid=OTHER_SHELL)
    code, text, _, _ = _claim(tmp_path, probe, _claimant(probe), seed)
    assert code == 0
    assert "previous owner is gone" in text


def test_worker_gone_but_recorded_shell_alive_refuses(tmp_path: Path) -> None:
    probe = _tree(tmp_path)
    seed = _dead_record(shell_pid=OTHER_SHELL, shell_start=tok(probe, OTHER_SHELL))
    code, text, owner, before = _claim(tmp_path, probe, _claimant(probe), seed)
    assert code == 3
    assert text == _mod.refuse_live_shell_dead_worker(OTHER_SHELL, DEAD_WORKER, owner)
    assert owner.read_bytes() == before


def test_same_worker_with_a_dead_recorded_shell_is_a_takeover(tmp_path: Path) -> None:
    probe = _tree(tmp_path)
    seed = _record(_claimant(probe), shell_pid=DEAD_SHELL, shell_start=_DEAD_TOKEN)
    code, text, owner, _ = _claim(tmp_path, probe, _claimant(probe), seed)
    assert code == 0
    assert text == _mod.note_same_worker(DEAD_SHELL, WORKER, owner)


def test_same_worker_with_a_live_different_shell_refuses(tmp_path: Path) -> None:
    probe = _tree(tmp_path)
    seed = _record(_claimant(probe, WORKER, OTHER_SHELL))
    code, text, owner, before = _claim(tmp_path, probe, _claimant(probe), seed)
    assert code == 3
    assert text == _mod.refuse_live_shell_same_worker(OTHER_SHELL, WORKER, owner)
    assert owner.read_bytes() == before


@pytest.mark.parametrize("worker", [WORKER, OTHER_WORKER])
def test_same_shell_reentry_takes_over(tmp_path: Path, worker: int) -> None:
    probe = _tree(tmp_path)
    seed = _record(_claimant(probe, worker, SHELL))
    code, text, owner, _ = _claim(tmp_path, probe, _claimant(probe), seed)
    assert code == 0
    assert text == _mod.note_reentry(SHELL, owner)
    assert "re-entry from the same shell" in text


def test_equal_shell_pid_with_a_different_token_is_not_a_reentry(
    tmp_path: Path,
) -> None:
    probe = _tree(tmp_path)
    same_worker = _record(_claimant(probe), shell_start=_DEAD_TOKEN)
    code, text, _, _ = _claim(tmp_path / "a", probe, _claimant(probe), same_worker)
    assert code == 0
    assert "previous gate shell" in text
    live_other = _record(_claimant(probe, OTHER_WORKER, SHELL), shell_start=_DEAD_TOKEN)
    code, text, _, _ = _claim(tmp_path / "b", probe, _claimant(probe), live_other)
    assert code == 3
    assert f"gate path in use by a live run, pid {OTHER_WORKER}" in text


@pytest.mark.parametrize(
    ("recorded", "own"),
    [
        ("pid:[1]", GATE_KEY_TEST_PIDNS),
        (None, GATE_KEY_TEST_PIDNS),
        (GATE_KEY_TEST_PIDNS, None),
    ],
    ids=["different", "recorded_null", "own_unreadable"],
)
def test_different_pid_namespace_refuses(
    tmp_path: Path, recorded: str | None, own: str | None
) -> None:
    probe = _tree(tmp_path, pidns=own)
    seed = _dead_record(pidns=recorded)
    code, text, owner, before = _claim(
        tmp_path, probe, _claimant(probe, pidns=own), seed
    )
    assert code == 3
    assert "liveness cannot be checked across pid namespaces" in text
    assert str(owner) in text
    assert "remove" in text
    assert text == _mod.refuse_foreign_pidns(owner, recorded, DEAD_WORKER, own)
    assert owner.read_bytes() == before


def test_both_pid_namespaces_unreadable_is_the_same_namespace(tmp_path: Path) -> None:
    probe = _tree(tmp_path, pidns=None)
    code, text, _, _ = _claim(
        tmp_path, probe, _claimant(probe, pidns=None), _dead_record(pidns=None)
    )
    assert code == 0
    assert "previous owner is gone" in text


def test_equal_pid_namespace_proceeds_to_the_ordinary_rules(tmp_path: Path) -> None:
    probe = _tree(tmp_path)
    seed = _record(_claimant(probe, OTHER_WORKER, OTHER_SHELL))
    code, text, _, _ = _claim(tmp_path, probe, _claimant(probe), seed)
    assert code == 3
    assert "gate path in use by a live run" in text


def test_foreign_host_refuses_before_the_pidns_rule(tmp_path: Path) -> None:
    probe = _tree(tmp_path)
    seed = _dead_record(host="elsewhere", pidns="pid:[1]")
    code, text, owner, before = _claim(tmp_path, probe, _claimant(probe), seed)
    assert code == 3
    assert "elsewhere" in text
    assert socket.gethostname() in text
    assert str(owner) in text
    assert "remove" in text
    assert "live run" not in text
    assert owner.read_bytes() == before


def test_unsupported_record_version_refuses(tmp_path: Path) -> None:
    probe = _tree(tmp_path)
    code, text, owner, before = _claim(
        tmp_path, probe, _claimant(probe), _dead_record(v=2)
    )
    assert code == 3
    assert text == _mod.refuse_unsupported_version(owner, 2)
    assert owner.read_bytes() == before


@pytest.mark.parametrize(
    ("seed", "why"),
    [
        ("{not json", "invalid JSON"),
        ("[1, 2]", "not a JSON object"),
        (_dead_record(v="1"), "missing or malformed field v"),
        (_dead_record(v=True), "missing or malformed field v"),
        (
            {k: v for k, v in _dead_record().items() if k != "v"},
            "missing or malformed field v",
        ),
        (
            {k: v for k, v in _dead_record().items() if k != "pid"},
            "missing or malformed field pid",
        ),
        (_dead_record(pid=0), "missing or malformed field pid"),
        (_dead_record(shell_pid="7"), "missing or malformed field shell_pid"),
        (_dead_record(shell_start=7), "bad token in shell_start"),
        (_dead_record(start="ps:garbage"), "bad token in start"),
        (_dead_record(host=""), "missing or malformed field host"),
        (
            {k: v for k, v in _dead_record().items() if k != "pidns"},
            "missing or malformed field pidns",
        ),
        (_dead_record(pidns=""), "missing or malformed field pidns"),
        (_dead_record(pidns=5), "missing or malformed field pidns"),
    ],
)
def test_malformed_record_refuses(tmp_path: Path, seed: object, why: str) -> None:
    probe = _tree(tmp_path)
    code, text, owner, before = _claim(tmp_path, probe, _claimant(probe), seed)
    assert code == 3
    assert text == _mod.refuse_malformed(owner, why)
    assert owner.read_bytes() == before


def test_unreadable_owner_file_refuses_as_invalid_json(tmp_path: Path) -> None:
    probe = _tree(tmp_path)
    gate_root = tmp_path / "gate-root"
    owner = _write_owner(gate_root, "x")
    owner.write_bytes(b"\xff\xfe\x00")
    code, text = _mod.claim(gate_root, KEY, BRANCH, _claimant(probe), probe)
    assert code == 3
    assert text == _mod.refuse_malformed(owner, "invalid JSON")


@pytest.mark.parametrize(
    ("overrides", "rendered"),
    [
        ({"source": 7}, "claimed unknown via unknown"),
        (
            {"claimed_at": ["x"], "source": "CLAUDE_PID"},
            "claimed unknown via CLAUDE_PID",
        ),
    ],
)
def test_claimed_at_and_source_are_display_only(
    tmp_path: Path, overrides: dict[str, object], rendered: str
) -> None:
    probe = _tree(tmp_path)
    seed = _record(_claimant(probe, OTHER_WORKER, OTHER_SHELL))
    seed.pop("claimed_at")
    seed.update(overrides)
    code, text, owner, before = _claim(tmp_path, probe, _claimant(probe), seed)
    assert code == 3
    assert f"({rendered}; owner file {owner})" in text
    assert "malformed" not in text
    assert owner.read_bytes() == before


# --- Message wording (R13, R21) ----------------------------------------------

_OWNER = Path("/g/cw-gate-owner-k.json")
_EXIT_CLAUSE = f"If no run owns the path, remove {_OWNER} and re-run"

_WORDING: list[tuple[str, Callable[[], str], str]] = [
    (
        "M1",
        lambda: _mod.refuse_live_worker(
            11, "2026-10-11T00:00:00Z", "CLAUDE_PID", _OWNER
        ),
        "gate_key: gate path in use by a live run, pid 11 (claimed"
        f" 2026-10-11T00:00:00Z via CLAUDE_PID; owner file {_OWNER}); refusing"
        f" destructive cleanup. {_EXIT_CLAUSE}",
    ),
    (
        "M2",
        lambda: _mod.refuse_live_shell_same_worker(22, 11, _OWNER),
        "gate_key: gate path in use by a live run, pid 22 (a still-running gate"
        f" shell of this same worker, pid 11; owner file {_OWNER}); refusing"
        f" destructive cleanup. {_EXIT_CLAUSE}",
    ),
    (
        "M3",
        lambda: _mod.refuse_live_shell_dead_worker(22, 11, _OWNER),
        "gate_key: gate path in use by a live run, pid 22 (its worker pid 11 is"
        f" gone; owner file {_OWNER}); refusing destructive cleanup. {_EXIT_CLAUSE}",
    ),
    (
        "M4",
        lambda: _mod.refuse_foreign_host("far", "near", 11, _OWNER),
        "gate_key: gate path claimed on host far, not this host (near): pid 11;"
        " liveness cannot be checked across hosts, refusing destructive cleanup."
        f" If that run is gone, remove {_OWNER} and re-run",
    ),
    (
        "M5",
        lambda: _mod.refuse_unsupported_version(_OWNER, 2),
        f"gate_key: owner file {_OWNER} has unsupported record version 2; refusing"
        " destructive cleanup. If its run is gone, remove the file and re-run",
    ),
    *[
        (
            f"M6-{why}",
            (lambda why=why: _mod.refuse_malformed(_OWNER, why)),
            f"gate_key: owner file {_OWNER} is unreadable or malformed ({why});"
            " refusing destructive cleanup. If no run owns the path, remove the"
            " file and re-run",
        )
        for why in (
            "invalid JSON",
            "not a JSON object",
            "missing or malformed field pid",
            "bad token in start",
        )
    ],
    (
        "M7",
        lambda: _mod.refuse_foreign_pidns(_OWNER, None, 11, "pid:[2]"),
        f"gate_key: owner file {_OWNER} was written from pid namespace unreadable"
        " (pid 11), this process runs in pid:[2]; liveness cannot be checked"
        f" across pid namespaces, refusing destructive cleanup. {_EXIT_CLAUSE}",
    ),
    (
        "M8",
        lambda: _mod.unreadable_shell_token(33),
        "gate_key: cannot record an owner: the start token of this fence's shell"
        " (pid 33) could not be read from /proc or from ps; a claim is never"
        " written without an owner, so the gate path was left untouched. There"
        " is no owner file to remove: run ps -o lstart= -p 33 from this shell"
        " and, if it prints nothing, report that host's /proc and ps output",
    ),
    (
        "rule-5a",
        lambda: _mod.note_reentry(33, _OWNER),
        "gate_key: re-entry from the same shell (pid 33) that holds the owner"
        f" record; taking over (owner file {_OWNER})",
    ),
    (
        "rule-5",
        lambda: _mod.note_same_worker(22, 11, _OWNER),
        "gate_key: previous gate shell (pid 22) of this same worker (pid 11) is"
        f" gone; taking over (owner file {_OWNER})",
    ),
    (
        "rule-8",
        lambda: _mod.note_both_dead(11, 22, _OWNER),
        "gate_key: previous owner is gone (worker pid 11, shell pid 22); taking"
        f" over (owner file {_OWNER})",
    ),
]


@pytest.mark.parametrize(
    ("label", "build", "expected"), _WORDING, ids=[case[0] for case in _WORDING]
)
def test_refusal_messages_match_the_quoted_wording(
    label: str, build: Callable[[], str], expected: str
) -> None:
    assert build() == expected
    if label in {"M1", "M2", "M3", "M7"}:
        assert expected.endswith(_EXIT_CLAUSE)


# --- Claim key (R28c) --------------------------------------------------------


def test_claim_records_the_supplied_key_without_consulting_git(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    plain = tmp_path / "plain"
    plain.mkdir()
    monkeypatch.chdir(plain)
    monkeypatch.setenv("GIT_CEILING_DIRECTORIES", str(tmp_path))
    bin_dir = _write_bin_stub(tmp_path, "git", "#!/bin/sh\nexit 128\n")
    monkeypatch.setenv("PATH", f"{bin_dir}:{os.environ['PATH']}")
    supplied = "fedcba9876543210"
    gate_root = tmp_path / "gate-root"
    code = _mod.main(
        ["--branch", "b", "--claim", "--key", supplied, "--gate-root", str(gate_root)],
        probe=_tree(tmp_path),
    )
    assert code == 0
    record = json.loads(
        (gate_root / f"cw-gate-owner-{supplied}.json").read_text(encoding="utf-8")
    )
    assert record["key"] == supplied
    assert record["branch"] == "b"


# --- Release -----------------------------------------------------------------


def test_release_removes_only_a_record_naming_this_shell(tmp_path: Path) -> None:
    probe = _tree(tmp_path)
    gate_root = tmp_path / "gate-root"
    owner = _write_owner(gate_root, _record(_claimant(probe)))
    assert _mod.release(gate_root, KEY, probe) == 0
    assert not owner.exists()
    foreign = _write_owner(
        gate_root, _record(_claimant(probe, OTHER_WORKER, OTHER_SHELL))
    )
    assert _mod.release(gate_root, KEY, probe) == 0
    assert foreign.exists()
    other_ns = _write_owner(gate_root, _record(_claimant(probe), pidns="pid:[1]"))
    assert _mod.release(gate_root, KEY, probe) == 0
    assert other_ns.exists()


@pytest.mark.parametrize("content", [None, "{not json", "[1]"])
def test_release_is_exit_0_when_absent_or_unreadable(
    tmp_path: Path, content: str | None
) -> None:
    probe = _tree(tmp_path)
    gate_root = tmp_path / "gate-root"
    if content is not None:
        owner = _write_owner(gate_root, content)
    assert _mod.release(gate_root, KEY, probe) == 0
    if content is not None:
        assert owner.exists()


def test_release_leaves_the_record_when_the_shell_token_is_unreadable(
    tmp_path: Path,
) -> None:
    probe = _tree(tmp_path)
    gate_root = tmp_path / "gate-root"
    owner = _write_owner(gate_root, _record(_claimant(probe)))
    (probe.proc_root / str(SHELL) / "stat").unlink()
    assert _mod.release(gate_root, KEY, probe) == 0
    assert owner.exists()


def test_release_needs_no_git_and_works_from_a_deleted_cwd(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    probe = _tree(tmp_path)
    gate_root = tmp_path / "gate-root"
    owner = _write_owner(gate_root, _record(_claimant(probe)))
    doomed = tmp_path / "doomed"
    doomed.mkdir()
    monkeypatch.chdir(doomed)
    doomed.rmdir()
    empty = tmp_path / "empty-bin"
    empty.mkdir()
    monkeypatch.setenv("PATH", str(empty))
    assert _release_main(gate_root, probe) == 0
    assert not owner.exists()


# --- Stdout and claim() contract (R27) ---------------------------------------


def test_claim_and_release_write_nothing_to_stdout(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    probe = _tree(tmp_path)
    gate_root = tmp_path / "gate-root"
    assert _claim_main(gate_root, probe) == 0
    assert capsys.readouterr().out == ""
    _write_owner(gate_root, _dead_record())
    assert _claim_main(gate_root, probe) == 0
    assert capsys.readouterr().out == ""
    _write_owner(gate_root, _record(_claimant(probe, OTHER_WORKER, OTHER_SHELL)))
    assert _claim_main(gate_root, probe) == 3
    assert capsys.readouterr().out == ""
    assert _release_main(gate_root, probe) == 0
    assert capsys.readouterr().out == ""


def test_claim_returns_exit_code_and_stderr_text(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    probe = _tree(tmp_path)
    gate_root = tmp_path / "gate-root"
    owner = _write_owner(gate_root, _dead_record())
    code, text = _mod.claim(gate_root, KEY, BRANCH, _claimant(probe), probe)
    assert (code, text) == (0, _mod.note_both_dead(DEAD_WORKER, DEAD_SHELL, owner))
    seed = _record(_claimant(probe, OTHER_WORKER, OTHER_SHELL))
    _write_owner(gate_root, seed)
    code, text = _mod.claim(gate_root, KEY, BRANCH, _claimant(probe), probe)
    assert code == 3
    assert text.startswith(
        f"gate_key: gate path in use by a live run, pid {OTHER_WORKER}"
    )
    assert capsys.readouterr().err == ""
    _write_owner(gate_root, seed)
    assert _claim_main(gate_root, probe) == 3
    lines = capsys.readouterr().err.splitlines()
    assert lines[0].startswith("gate_key: WARN claimant identified by shell-pid")
    assert lines[1] == text


# --- main()-level pins (R7, R12, R19) ----------------------------------------


def _warn_line(shell: int, reason: str) -> str:
    return (
        f"gate_key: WARN claimant identified by shell-pid {shell} ({reason});"
        " overlap is still refused while this shell lives"
    )


def test_no_owner_env_records_the_shell_pid_and_does_not_fail_open(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    probe = _tree(tmp_path)
    gate_root = tmp_path / "gate-root"
    assert _claim_main(gate_root, probe) == 0
    record = _owner_json(gate_root)
    assert record["pid"] == record["shell_pid"] == SHELL
    assert record["source"] == "shell-pid"
    assert capsys.readouterr().err == (
        _warn_line(SHELL, "neither CW_GATE_OWNER_PID nor CLAUDE_PID is set") + "\n"
    )
    other_shell = replace(probe, shell_pid=OTHER_SHELL)
    assert _claim_main(gate_root, other_shell) == 3
    without_first = fake_proc_tree(
        tmp_path,
        {pid: proc for pid, proc in _base_procs().items() if pid != SHELL},
        name="proc2",
        shell_pid=OTHER_SHELL,
    )
    assert _claim_main(gate_root, without_first) == 0


def test_non_ancestor_claude_pid_falls_back_to_the_shell_pid(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    probe = _tree(tmp_path)
    monkeypatch.setenv("CLAUDE_PID", str(OTHER_WORKER))
    gate_root = tmp_path / "gate-root"
    assert _claim_main(gate_root, probe) == 0
    assert _owner_json(gate_root)["pid"] == SHELL
    reason = f"CLAUDE_PID={OTHER_WORKER} is not an ancestor of this process"
    assert capsys.readouterr().err == _warn_line(SHELL, reason) + "\n"


def test_ancestor_claude_pid_identifies_the_worker(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    probe = _tree(tmp_path)
    monkeypatch.setenv("CLAUDE_PID", str(WORKER))
    gate_root = tmp_path / "gate-root"
    assert _claim_main(gate_root, probe) == 0
    record = _owner_json(gate_root)
    assert record["pid"] == WORKER
    assert record["shell_pid"] == SHELL
    err = capsys.readouterr().err
    assert err == f"gate_key: claimant identified by CLAUDE_PID (worker pid {WORKER})\n"
    assert "WARN" not in err


def test_cw_gate_owner_pid_takes_precedence(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    probe = _tree(tmp_path)
    monkeypatch.setenv("CLAUDE_PID", str(SHELL))
    monkeypatch.setenv("CW_GATE_OWNER_PID", str(WORKER))
    gate_root = tmp_path / "gate-root"
    assert _claim_main(gate_root, probe) == 0
    assert _owner_json(gate_root)["source"] == "CW_GATE_OWNER_PID"
    assert capsys.readouterr().err == (
        f"gate_key: claimant identified by CW_GATE_OWNER_PID (worker pid {WORKER})\n"
    )


def test_same_shell_reentry_is_a_takeover_with_a_note_not_a_refusal(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    probe = _tree(tmp_path)
    gate_root = tmp_path / "gate-root"
    assert _claim_main(gate_root, probe) == 0
    capsys.readouterr()
    assert _claim_main(gate_root, probe) == 0
    owner = _mod.owner_path(gate_root, KEY)
    note = (
        f"gate_key: re-entry from the same shell (pid {SHELL}) that holds the"
        f" owner record; taking over (owner file {owner})"
    )
    assert note in capsys.readouterr().err.splitlines()


def test_unreadable_shell_token_exits_1_with_m8(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    probe = _tree(tmp_path)
    (probe.proc_root / str(SHELL) / "stat").unlink()
    gate_root = tmp_path / "gate-root"
    assert _claim_main(gate_root, probe) == 1
    assert capsys.readouterr().err == _mod.unreadable_shell_token(SHELL) + "\n"
    assert not Path(_mod.owner_path(gate_root, KEY)).exists()


@pytest.mark.parametrize(
    ("environ", "line"),
    [
        (
            {"CW_GATE_OWNER_PID": str(WORKER)},
            f"gate_key: claimant identified by CW_GATE_OWNER_PID (worker pid {WORKER})",
        ),
        (
            {"CLAUDE_PID": str(WORKER)},
            f"gate_key: claimant identified by CLAUDE_PID (worker pid {WORKER})",
        ),
        ({}, _warn_line(SHELL, "neither CW_GATE_OWNER_PID nor CLAUDE_PID is set")),
    ],
    ids=["override", "claude_pid", "shell_pid"],
)
def test_claim_prints_one_source_line_for_each_source(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    environ: dict[str, str],
    line: str,
) -> None:
    for name, value in environ.items():
        monkeypatch.setenv(name, value)
    assert _claim_main(tmp_path / "gate-root", _tree(tmp_path)) == 0
    assert capsys.readouterr().err.splitlines() == [line]


def test_release_via_main_uses_key_not_git(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    probe = _tree(tmp_path)
    gate_root = tmp_path / "gate-root"
    owner = _write_owner(gate_root, _record(_claimant(probe)))
    bin_dir = _write_bin_stub(tmp_path, "git", "#!/bin/sh\nexit 128\n")
    monkeypatch.setenv("PATH", f"{bin_dir}:{os.environ['PATH']}")
    assert _release_main(gate_root, probe) == 0
    assert not owner.exists()


_AMBIENT_GATE_VARS = ("CLAUDE_PID", "CW_GATE_OWNER_PID", "CW_GATE_ROOT")


@pytest.fixture(scope="class")
def _ambient_gate_env() -> Iterator[None]:
    """Export the gate variables for a whole class, as a worker shell does.

    Class scope sets them up before the function-scoped ``clean_gate_env``
    that ``pytestmark`` applies, so the test below sees what that fixture left.
    """
    with pytest.MonkeyPatch.context() as patch:
        for name in _AMBIENT_GATE_VARS:
            patch.setenv(name, "4242")
        yield


@pytest.mark.usefixtures("_ambient_gate_env")
class TestCleanGateEnv:
    def test_clean_gate_env_fixture_clears_ambient_variables(self) -> None:
        assert all(name not in os.environ for name in _AMBIENT_GATE_VARS)


def test_main_as_script_exits_with_main_status(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    gate_root = tmp_path / "gate-root"
    argv = ["gate_key.py", "--release", "--key", KEY, "--gate-root", str(gate_root)]
    monkeypatch.setattr(sys, "argv", argv)
    with pytest.raises(SystemExit) as exc_info:
        runpy.run_path(str(GATE_KEY_SCRIPT), run_name="__main__")
    assert exc_info.value.code == 0


@pytest.mark.skipif(
    not Path("/proc/self/ns/pid").exists(), reason="needs a Linux /proc"
)
def test_default_probe_real_kernel_sanity(
    sleeper: Callable[[], subprocess.Popen[bytes]],
) -> None:
    probe = _mod.Probe()
    claimant = _mod.resolve_claimant({"CLAUDE_PID": str(os.getppid())}, probe)
    assert claimant is not None
    assert claimant.source == "CLAUDE_PID"
    assert claimant.worker.pid == os.getppid()
    child = sleeper()
    live = _mod.Proc(child.pid, tok(probe, child.pid))
    assert _mod.proc_alive(live, probe)
    child.terminate()
    child.wait(timeout=10)
    assert not _mod.proc_alive(live, probe)


# --- R9 pins -----------------------------------------------------------------


def test_claim_works_with_tmpdir_pointing_at_a_nonexistent_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("TMPDIR", str(tmp_path / "missing-tmpdir"))
    touched: list[Path] = []
    real_open = Path.open
    real_replace = Path.replace

    def _open(self: Path, *args: Any, **kwargs: Any) -> Any:
        if args and str(args[0])[:1] in {"w", "a"}:
            touched.append(self)
        return real_open(self, *args, **kwargs)

    def _replace(self: Path, target: str | os.PathLike[str]) -> Path:
        touched.append(Path(target))
        return real_replace(self, target)

    monkeypatch.setattr(Path, "open", _open)
    monkeypatch.setattr(Path, "replace", _replace)
    gate_root = tmp_path / "gate-root"
    assert _claim_main(gate_root, _tree(tmp_path)) == 0
    assert touched
    assert all(path.parent == gate_root for path in touched)


def test_script_does_not_import_tempfile() -> None:
    assert "tempfile" not in GATE_KEY_SCRIPT.read_text(encoding="utf-8")


def test_takeover_leaves_no_stale_or_tmp_files(tmp_path: Path) -> None:
    probe = _tree(tmp_path)
    gate_root = tmp_path / "gate-root"
    _write_owner(gate_root, _dead_record())
    code, _ = _mod.claim(gate_root, KEY, BRANCH, _claimant(probe), probe)
    assert code == 0
    assert sorted(path.name for path in gate_root.iterdir()) == [
        f"cw-gate-owner-{KEY}.json",
        f"cw-gate-owner-{KEY}.lock",
    ]


def test_leftover_tmp_from_a_crashed_claim_is_overwritten_not_accumulated(
    tmp_path: Path,
) -> None:
    probe = _tree(tmp_path)
    gate_root = tmp_path / "gate-root"
    gate_root.mkdir()
    leftover = gate_root / f"cw-gate-owner-{KEY}.json.tmp"
    leftover.write_text("half a record", encoding="utf-8")
    code, _ = _mod.claim(gate_root, KEY, BRANCH, _claimant(probe), probe)
    assert code == 0
    assert not leftover.exists()
    assert _owner_json(gate_root)["pid"] == WORKER


def _hold_lock(gate_root: Path) -> Any:
    gate_root.mkdir(parents=True, exist_ok=True)
    handle = (gate_root / f"cw-gate-owner-{KEY}.lock").open("a")
    fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
    return handle


def test_claim_waits_on_the_claim_lock(tmp_path: Path) -> None:
    probe = _tree(tmp_path)
    gate_root = tmp_path / "gate-root"
    handle = _hold_lock(gate_root)
    results: list[tuple[int, str]] = []
    worker = threading.Thread(
        target=lambda: results.append(
            _mod.claim(gate_root, KEY, BRANCH, _claimant(probe), probe)
        )
    )
    worker.start()
    worker.join(timeout=0.3)
    assert worker.is_alive()
    assert results == []
    handle.close()
    worker.join(timeout=10)
    assert results == [(0, "")]


def test_claim_lock_timeout_exits_1(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    probe = _tree(tmp_path)
    gate_root = tmp_path / "gate-root"
    handle = _hold_lock(gate_root)
    monkeypatch.setattr(_mod, "LOCK_TIMEOUT_S", 0.2)
    try:
        code, text = _mod.claim(gate_root, KEY, BRANCH, _claimant(probe), probe)
    finally:
        handle.close()
    assert code == 1
    assert text.startswith("gate_key: timed out")
    assert not Path(_mod.owner_path(gate_root, KEY)).exists()


def test_publish_failure_exits_1_and_keeps_the_old_record(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    probe = _tree(tmp_path)
    gate_root = tmp_path / "gate-root"
    owner = _write_owner(gate_root, _dead_record())
    before = owner.read_text(encoding="utf-8")

    def _replace(self: Path, target: str | os.PathLike[str]) -> Path:
        raise OSError(errno.EXDEV, "cross-device")

    monkeypatch.setattr(Path, "replace", _replace)
    code, text = _mod.claim(gate_root, KEY, BRANCH, _claimant(probe), probe)
    assert code == 1
    assert "could not publish the owner record" in text
    assert owner.read_text(encoding="utf-8") == before
    assert not owner.with_name(owner.name + ".tmp").exists()


# --- Concurrency -------------------------------------------------------------

_RACERS = 8


def _race(tmp_path: Path, seed: object | None) -> tuple[list[tuple[int, str]], Path]:
    procs = _base_procs()
    for index in range(_RACERS):
        procs[8000 + index] = FakeProc(30 + index, 1)
        procs[9000 + index] = FakeProc(50 + index, 8000 + index)
    probe = fake_proc_tree(tmp_path, procs)
    gate_root = tmp_path / "gate-root"
    if seed is not None:
        _write_owner(gate_root, seed)
    barrier = threading.Barrier(_RACERS)
    results: list[tuple[int, str]] = []
    lock = threading.Lock()

    def _run(index: int) -> None:
        claimant = _claimant(probe, 8000 + index, 9000 + index)
        barrier.wait()
        outcome = _mod.claim(gate_root, KEY, BRANCH, claimant, probe)
        with lock:
            results.append(outcome)

    threads = [threading.Thread(target=_run, args=(i,)) for i in range(_RACERS)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)
    return results, gate_root


def _assert_one_winner(results: list[tuple[int, str]], gate_root: Path) -> None:
    assert len(results) == _RACERS
    winners = [text for code, text in results if code == 0]
    losers = [text for code, text in results if code == 3]
    assert len(winners) == 1
    assert len(losers) == _RACERS - 1
    winner_pid = _owner_json(gate_root)["pid"]
    for text in losers:
        assert f"gate path in use by a live run, pid {winner_pid}" in text


def test_concurrent_claims_seeded_from_a_dead_owner_have_exactly_one_winner(
    tmp_path: Path,
) -> None:
    results, gate_root = _race(tmp_path, _dead_record())
    _assert_one_winner(results, gate_root)
    winner = next(text for code, text in results if code == 0)
    assert winner.startswith("gate_key: previous owner is gone")


def test_concurrent_claims_from_an_absent_file_have_exactly_one_winner(
    tmp_path: Path,
) -> None:
    results, gate_root = _race(tmp_path, None)
    _assert_one_winner(results, gate_root)
