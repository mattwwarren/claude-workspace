"""Step 2.5 gate-key helpers (#2436): the gate_key.py loader and installer,
the claimant doubles, the gate fence locators, and the ``clean_gate_env``
and ``sleeper`` fixtures. A registered plugin. Moved from
``tests/conftest.py`` (#2668).
"""

from __future__ import annotations

import functools
import re
import subprocess
import sys
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest

from tests._shared.guard_fences import load_guard_script_module
from tests._shared.workflow_text import _REPO_ROOT, _bash_fences, _cmd

if TYPE_CHECKING:
    import types
    from collections.abc import Callable, Iterator


# --- Step 2.5 gate key (#2436) ----------------------------------------------
# Shared by tests/test_gate_key.py, tests/test_gate_key_fences.py and the gate-2
# runner in tests/test_scope_conformance_gate_docs.py, so the fence locators,
# the key helper and the claim doubles are spelled once.

GATE_KEY_SCRIPT = _REPO_ROOT / ".claude" / "scripts" / "gate_key.py"


# The pid-namespace link text a fake /proc tree and a built claimant share by
# default (the value observed on the Linux dispatch host).
GATE_KEY_TEST_PIDNS = "pid:[4026531836]"


# The per-claim generation a built claimant carries by default (#2676).
GATE_KEY_TEST_GENERATION = "a1b2c3d4e5f60718"


# Step 2.5 gate 2's numbered bullet and gate 3's, which bound gate 2's section.
_GATE2_START = "2. **File set is within the plan's enumeration**"


_GATE2_END = "3. **Test command exit code is 0:**"


_GATE3_END = "4. **Mypy/ruff clean"


# The line a gate fence prints when gate_key.py resolves nowhere (R15): the one
# place the regex is spelled, so text, executed and gate-2 checks cannot drift.
_GATE_KEY_HELPER_MISSING = re.compile(
    r'GATE_KEY_HELPER_MISSING:[^"\n]*run scripts/install-skills\.sh'
)


@functools.cache
def _gate_key_module() -> types.ModuleType:
    """The stdlib-only gate_key.py, loaded lazily so conftest imports without it."""
    return load_guard_script_module(GATE_KEY_SCRIPT, "gate_key")


def gate_key_for(repo: Path, branch: str) -> str:
    """The gate key the fences derive for *branch* in *repo*'s clone.

    Uses the script's own ``compute_key`` so every fence test shares one
    formula; ``tests/test_gate_key.py`` pins that formula independently.
    """
    key: str = _gate_key_module().compute_key(str((repo / ".git").resolve()), branch)
    return key


def link_real_python3(bin_dir: Path) -> Path:
    """Put ``python3 -> sys.executable`` in *bin_dir*; return *bin_dir*.

    Executed fences call ``python3``; linking the test interpreter keeps them
    off a host ``/usr/bin/python3`` the venv never vetted.
    """
    bin_dir.mkdir(parents=True, exist_ok=True)
    link = bin_dir / "python3"
    link.unlink(missing_ok=True)
    link.symlink_to(sys.executable)
    return bin_dir


def install_gate_key(home: Path) -> Path:
    """Copy the real gate_key.py to ``<home>/.claude/scripts``; return the copy."""
    scripts = home / ".claude" / "scripts"
    scripts.mkdir(parents=True, exist_ok=True)
    target = scripts / GATE_KEY_SCRIPT.name
    target.write_text(GATE_KEY_SCRIPT.read_text(encoding="utf-8"), encoding="utf-8")
    return target


@pytest.fixture
def clean_gate_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Clear the ambient gate variables a dispatched worker exports (R12).

    A worker shell carries ``CLAUDE_PID``; CI does not. Clearing it (and the
    override and gate root) gives every gate-key test the same environment.
    """
    for name in ("CLAUDE_PID", "CW_GATE_OWNER_PID", "CW_GATE_ROOT"):
        monkeypatch.delenv(name, raising=False)


@pytest.fixture
def sleeper() -> Iterator[Callable[[], subprocess.Popen[bytes]]]:
    """Factory for live, killable child processes; teardown reaps every one."""
    children: list[subprocess.Popen[bytes]] = []

    def _start() -> subprocess.Popen[bytes]:
        child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
        children.append(child)
        return child

    yield _start
    for child in children:
        if child.poll() is None:
            child.terminate()
        child.wait(timeout=10)


def make_claimant(
    gate_key_module: types.ModuleType,
    worker: int | tuple[int, str],
    shell: int | tuple[int, str],
    *,
    source: str = "CLAUDE_PID",
    note: str = "",
    pidns: str | None = GATE_KEY_TEST_PIDNS,
    generation: str = GATE_KEY_TEST_GENERATION,
) -> Any:
    """Build *gate_key_module*'s ``Claimant`` from two process specs.

    Each spec is a ``(pid, token)`` pair, or a bare pid whose real start token
    is read through the module's default ``Probe`` (so a ``sleeper`` pid can be
    passed directly).
    """

    def _proc(spec: int | tuple[int, str]) -> Any:
        if isinstance(spec, int):
            token = gate_key_module.process_start_token(spec, gate_key_module.Probe())
            assert token is not None, f"no start token for live pid {spec}"
            return gate_key_module.Proc(spec, token)
        return gate_key_module.Proc(spec[0], spec[1])

    return gate_key_module.Claimant(
        _proc(worker), _proc(shell), source, note, pidns, generation
    )


def assert_gate_key_helper_missing(text: str) -> None:
    """Assert *text* carries the missing-helper line ending in the remedy."""
    assert _GATE_KEY_HELPER_MISSING.search(text), text


def _single_bash_fence(text: str, where: str) -> str:
    fences = _bash_fences(text)
    assert len(fences) == 1, f"{where}: expected one bash fence, got {len(fences)}"
    return fences[0]


def _fence_containing(doc: str, marker: str) -> str:
    fences = [fence for fence in _bash_fences(_cmd(doc)) if marker in fence]
    assert len(fences) == 1, f"{doc}: expected one fence with {marker!r}"
    return fences[0]


def gate2_section() -> str:
    """Step 2.5 gate 2, from its numbered bullet to gate 3's (#2141)."""
    content = _cmd("auto-dev-impl.md")
    start = content.index(_GATE2_START)
    end = content.index(_GATE2_END, start)
    return content[start:end]


def gate2_fence() -> str:
    """Step 2.5 gate 2's own bash fence in ``auto-dev-impl.md``."""
    return _single_bash_fence(gate2_section(), "gate 2")


def impl_gate3_fence() -> str:
    """Step 2.5 gate 3's own bash fence in ``auto-dev-impl.md``."""
    content = _cmd("auto-dev-impl.md")
    start = content.index(_GATE2_END)
    end = content.index(_GATE3_END, start)
    return _single_bash_fence(content[start:end], "gate 3")


def impl_gate_setup_fence() -> str:
    """Step 2.5's gate-setup fence: the one that arms the cleanup trap."""
    return _fence_containing("auto-dev-impl.md", "trap gate_wt_cleanup EXIT")


def monolith_gate_fence() -> str:
    """``auto-dev.md``'s Mitigation 1 fence: the one that arms the cleanup trap."""
    return _fence_containing("auto-dev.md", "trap gate_wt_cleanup EXIT")
