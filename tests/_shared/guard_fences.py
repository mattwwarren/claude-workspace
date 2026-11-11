"""Guard-script fence harness: the ``cw-script-version`` marker cases, the
stub interpreters a fence shells out to, and the runners that execute a
doc fence or a guard script's CLI. Moved from ``tests/conftest.py`` (#2668).
"""

from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import TYPE_CHECKING

from tests import _git_sandbox

if TYPE_CHECKING:
    import types
    from collections.abc import Mapping


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


def write_guard_stub_bin(
    tmp_path: Path, names: tuple[str, ...] = ("uv", "python3")
) -> Path:
    """Create a ``PATH`` directory of sentinel interpreters for a fence (#2141).

    Replaces the runner's former rewrite of the fence *text* (``uv run python``
    → ``echo INVOKED``), which silently mutated the very line under test and
    would have kept passing had the doc's invocation changed shape. The fence
    now executes exactly as the doc spells it; only the binaries it calls are
    fixtures. Both spellings in these docs are covered — ``uv run python
    "$RESOLVED"`` and the ``python3 "$RESOLVED"`` the stdlib-only pre-mutation
    guard uses (#2436) — and each stub echoes the sentinel followed by its own
    argument vector, so a caller can assert on the arguments the doc passes. A
    bare ``python`` is deliberately not stubbed: it is absent from the impl
    environment, so a fence that regressed to it would run the host interpreter
    (or fail with command-not-found) and miss the INVOKED sentinel.

    *names* narrows the stub set; Step 2.5 gate 2 passes ``("uv",)`` so its
    ``python3`` stays the real interpreter that computes the gate key.
    """
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(parents=True, exist_ok=True)
    for name in names:
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
    create_base_commit: bool = False,
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
    invocation is neutralised by putting sentinel ``uv``/``python3`` stubs first
    on ``PATH`` instead. The per-site capture variables are echoed afterwards
    because four of the five fences assign the invocation into a command
    substitution rather than letting it print.

    This runner serves the four ``$GUARD_ROOT`` resolver sites. Step 2.5 gate 2
    derives its anchor from ``git worktree list`` instead and has its own
    real-worktree runner in ``tests/test_scope_conformance_gate_docs.py``.
    """
    repo = tmp_path / "repo"
    repo.mkdir(parents=True, exist_ok=True)
    _git_sandbox.run("init", "-b", "main", cwd=str(repo), text=False)
    if create_base_commit:
        _git_sandbox.run(
            "-c",
            "user.name=Codex Fixture",
            "-c",
            "user.email=codex-fixture@example.invalid",
            "commit",
            "--allow-empty",
            "-m",
            "fixture base",
            cwd=str(repo),
            text=False,
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
        + '\necho "${VERDICT-}${RESOLVE_OUTPUT-}${SCOPE_CONFORMANCE_OUTPUT-}'
        '${MUST_FIX_OVERRIDE_OUTPUT-}"\n'
    )
    session = tmp_path.name
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


def load_guard_script_module(script_path: Path, module_name: str) -> types.ModuleType:
    """Import a stdlib-only ``.claude/scripts/`` guard script as a module.

    The scripts live outside ``src/`` and must not import ``cw``, so their unit
    tests load them by path. Shared by every per-script test file rather than
    each carrying a private copy.
    """
    spec = importlib.util.spec_from_file_location(module_name, script_path)
    assert spec is not None
    assert spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules.setdefault(module_name, mod)
    spec.loader.exec_module(mod)
    return mod


def run_guard_script_cli(
    script_path: Path, args: list[str]
) -> subprocess.CompletedProcess[str]:
    """Run a guard script as a subprocess, capturing its exit code and output."""
    return subprocess.run(
        [sys.executable, str(script_path), *args],
        capture_output=True,
        text=True,
        check=False,
    )
