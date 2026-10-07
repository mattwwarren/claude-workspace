"""Seam assertions for the ``cw.review_finding_dispositions`` package split (#2498).

``review_finding_dispositions`` was a single 1410-line flat module. The split
must keep five things true that the behavioral suite cannot see on its own:

1. The historic names survive: every top-level name of the flat module is
   bound in the submodule the owner table names, and the 15-name import
   surface (14 public names plus the private ``_disposition_key`` six test
   files import) still resolves through ``cw.review_finding_dispositions``.
2. Every logging module keeps emitting on the ``cw.review_finding_dispositions``
   logger, so ``caplog`` filters and operator log routing see the same records.
3. The import discipline holds: no package file imports anything from ``cw`` at
   module scope beyond ``cw.review_markers`` and its own sibling submodules by
   direct path. ``cw.models.tasks`` imports ``FindingDisposition``, so any other
   module-scope ``cw`` import can close an import cycle (#1838).
4. The two patched names, ``cw.events.record_event`` and ``cw._git.run_git``,
   stay resolved at call time by a function-local import, so a patch on the
   source module keeps reaching the reader.
5. The ``cw review dispositions`` entrypoint still runs end to end through the
   package's re-exports, including the drift (STALE) path.
"""

from __future__ import annotations

import ast
import importlib
import inspect
import json
import logging
import subprocess
import sys
import textwrap
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
from click.testing import CliRunner

import cw
import cw.review_finding_dispositions
from cw.auto_dev_result import Review
from cw.cli import main
from cw.dev_queue import add_ticket
from cw.models import Stage, TicketTask
from cw.review_finding_dispositions import (
    FindingDisposition,
    _disposition_key,
    disposition_drifted,
    log_refused_dispositions,
    parse_finding_disposition_block,
    suppress_adjudicated_findings,
)
from cw.review_findings import AcceptedFinding, Finding, ReviewVerdict
from cw.review_markers import DISPOSITION_SENTINEL, RefusedDisposition

from .conftest import _make_finding, commit_tracked_file, git_in

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator
    from types import ModuleType

_PKG = "cw.review_finding_dispositions"
_CONSTANTS_MODULE = f"{_PKG}._constants"
_MODEL = f"{_PKG}.model"
_DRIFT = f"{_PKG}.drift"
_PROVENANCE = f"{_PKG}.provenance"
_MATCH = f"{_PKG}.match"
_LOGGER = "cw.review_finding_dispositions"
_TICKET = "T-2498"

_SRC = Path(cw.__file__).parent
_FLAT_FILE = _SRC / "review_finding_dispositions.py"
_PACKAGE_DIR = _SRC / "review_finding_dispositions"

# Owning module for every historic top-level name of the flat
# ``review_finding_dispositions.py`` (``_log`` aside: every logging submodule
# defines its own). Each extraction commit of the split edits only the entries
# it moves.
_OWNER: dict[str, str] = {
    "Outcome": _MODEL,
    "_REJECTED": _MODEL,
    "REVERSED": _MODEL,
    "_MUST_FIX": _CONSTANTS_MODULE,
    "_FIXED": _CONSTANTS_MODULE,
    "_MATCH_EXACT": _CONSTANTS_MODULE,
    "_MATCH_CLAIM": _CONSTANTS_MODULE,
    "_KEY_SEPARATOR": _MODEL,
    "_DIGEST_SUFFIX_RE": _MODEL,
    "_DISPOSITION_MD_TITLE": _PKG,
    "_DISPOSITION_SCHEMA_VERSION": _PKG,
    "_DISPOSITION_BLOCK_RE": _PKG,
    "_SUPPRESSION_SIGNAL": _PKG,
    "FindingDisposition": _MODEL,
    "_summary_digest": _MODEL,
    "_disposition_key": _MODEL,
    "split_disposition_key": _MODEL,
    "render_finding_disposition_block": _PKG,
    "_parse_one_disposition_block": _PKG,
    "parse_finding_disposition_block": _PKG,
    "merge_finding_dispositions": _PROVENANCE,
    "_is_utc_timestamp": _PROVENANCE,
    "_identity_is_bound": _PROVENANCE,
    "_provenance_gaps": _PROVENANCE,
    "partition_enforceable_dispositions": _PROVENANCE,
    "log_refused_dispositions": _PROVENANCE,
    "_render_suppression_signal": _PKG,
    "_MIN_TOKEN_LEN": _MATCH,
    "_ANCHORED_MIN_SHARED": _MATCH,
    "_ANCHORED_MIN_DICE": _MATCH,
    "_PROSE_MIN_SHARED": _MATCH,
    "_PROSE_MIN_DICE": _MATCH,
    "_STOPWORDS": _MATCH,
    "_TOKEN_RE": _MATCH,
    "_BACKTICK_RE": _MATCH,
    "_IDENTIFIER_SPAN_RE": _MATCH,
    "_claim_tokens": _MATCH,
    "_claim_symbols": _MATCH,
    "_claim_similarity": _MATCH,
    "_LedgerMatch": _MATCH,
    "_best_claim_match": _MATCH,
    "_match_ledger": _MATCH,
    "_ledger_matches": _MATCH,
    "_CLAIM_NOTE": _PKG,
    "_stamp_suppressed": _PKG,
    "_emit_suppression": _PKG,
    "_emit_shadow": _PKG,
    "_GIT_DIFF_UNCHANGED": _DRIFT,
    "_GIT_DIFF_CHANGED": _DRIFT,
    "disposition_drifted": _DRIFT,
    "disposition_event_type": _PKG,
    "disposition_event_payload": _PKG,
    "_emit_stale": _PKG,
    "suppress_adjudicated_findings": _PKG,
    "build_finding_disposition_ledger": _PKG,
}

# Everything in ``_OWNER`` that is not a function or a class. These carry no
# ``__module__`` of their own (or carry their type's), so their ownership is
# checked by membership in the owner's namespace instead.
_CONSTANTS = frozenset(
    {
        "Outcome",
        "_REJECTED",
        "REVERSED",
        "_MUST_FIX",
        "_FIXED",
        "_MATCH_EXACT",
        "_MATCH_CLAIM",
        "_KEY_SEPARATOR",
        "_DIGEST_SUFFIX_RE",
        "_DISPOSITION_MD_TITLE",
        "_DISPOSITION_SCHEMA_VERSION",
        "_DISPOSITION_BLOCK_RE",
        "_SUPPRESSION_SIGNAL",
        "_MIN_TOKEN_LEN",
        "_ANCHORED_MIN_SHARED",
        "_ANCHORED_MIN_DICE",
        "_PROSE_MIN_SHARED",
        "_PROSE_MIN_DICE",
        "_STOPWORDS",
        "_TOKEN_RE",
        "_BACKTICK_RE",
        "_IDENTIFIER_SPAN_RE",
        "_CLAIM_NOTE",
        "_GIT_DIFF_UNCHANGED",
        "_GIT_DIFF_CHANGED",
    }
)

# The package's import surface: the 14 public names plus ``_disposition_key``,
# which six test files outside this module's own tests import.
_SURFACE = (
    "FindingDisposition",
    "Outcome",
    "REVERSED",
    "_disposition_key",
    "build_finding_disposition_ledger",
    "disposition_drifted",
    "disposition_event_payload",
    "disposition_event_type",
    "log_refused_dispositions",
    "merge_finding_dispositions",
    "parse_finding_disposition_block",
    "partition_enforceable_dispositions",
    "render_finding_disposition_block",
    "split_disposition_key",
    "suppress_adjudicated_findings",
)

# Every module the split produces (the package itself included), derived from
# the owner table so each extraction commit only has to edit ``_OWNER``.
_MODULES = tuple(sorted(set(_OWNER.values())))

# The first module a cold interpreter imports in the import-order smoke test:
# the original #1838 list, ``cw.models.tasks`` (the module that imports
# ``FindingDisposition``), and every package submodule that exists.
_FIRST_IMPORTS = tuple(
    dict.fromkeys(
        (
            _PKG,
            "cw.review_findings",
            "cw.review_debt",
            "cw.models",
            "cw.events",
            "cw.models.tasks",
            *_MODULES,
        )
    )
)

# (reader, source module, patched name). Tests patch the name on its SOURCE
# module, which only reaches the reader because the reader imports it inside
# its own body, at call time.
_PATCHED_READERS = (
    ("_emit_suppression", "cw.events", "record_event"),
    ("_emit_shadow", "cw.events", "record_event"),
    ("_emit_stale", "cw.events", "record_event"),
    ("disposition_drifted", "cw._git", "run_git"),
)
_PATCHED_NAMES = frozenset(name for _, _, name in _PATCHED_READERS)

#: What ``render_finding_disposition_block`` puts in front of the sentinel.
_MARKER_TITLE = "## Review Finding Dispositions\n\n"


def _owner(name: str) -> ModuleType:
    return importlib.import_module(_OWNER[name])


def _owned(name: str) -> object:
    """The object *name* is bound to in its owning module's namespace."""
    return vars(_owner(name))[name]


def _package_files() -> list[Path]:
    """The source files of the module: the flat file before the split."""
    if _PACKAGE_DIR.is_dir():
        return sorted(_PACKAGE_DIR.rglob("*.py"))
    return [_FLAT_FILE]


def _module_name(path: Path) -> str:
    if path == _FLAT_FILE or path.name == "__init__.py":
        return _PKG
    return f"{_PKG}.{path.stem}"


def _settled(file: str, summary: str) -> dict[str, FindingDisposition]:
    """A one-entry ledger carrying the full provenance set."""
    key = _disposition_key(file, summary)
    assert key is not None
    entry = FindingDisposition.model_validate(
        {
            "outcome": "REJECTED",
            "rationale": "settled in an earlier round",
            "recorded_at": "2026-08-16T00:00:00Z",
            "actor": "operator",
            "reviewed_sha": "abc1234",
            "summary": summary,
        }
    )
    return {key: entry}


def _accepted_finding(finding: Finding) -> AcceptedFinding:
    return AcceptedFinding.model_validate(
        {"finding": finding, "reviewers": ["Test Reviewer"]}
    )


def _blocking_verdict(finding: Finding) -> ReviewVerdict:
    return ReviewVerdict.model_validate(
        {
            "blocking": True,
            "must_fix": [finding],
            "reviewed_sha": "abc1234",
            "accepted": [_accepted_finding(finding)],
            "review": Review(
                must_fix_initial=1,
                should_fix=0,
                fix_cycles_used=0,
                deferred=0,
                agents_run=1,
            ),
        }
    )


def _raise_oserror(*_args: object, **_kwargs: object) -> None:
    msg = "simulated I/O failure"
    raise OSError(msg)


# ---------------------------------------------------------------------------
# Owner table and import surface
# ---------------------------------------------------------------------------


def test_owner_table_covers_the_historic_surface() -> None:
    """The owner table names exactly the 55 historic top-level names."""
    assert len(_OWNER) == 55
    assert _CONSTANTS.issubset(_OWNER)
    assert set(_SURFACE).issubset(_OWNER)


@pytest.mark.parametrize("name", sorted(_OWNER))
def test_historic_name_is_bound_in_its_owner(name: str) -> None:
    """Every historic name is an attribute of the submodule that owns it."""
    assert name in vars(_owner(name))


@pytest.mark.parametrize("name", _SURFACE)
def test_surface_name_resolves_through_the_package(name: str) -> None:
    """``from cw.review_finding_dispositions import X`` reaches the owner's X."""
    assert getattr(cw.review_finding_dispositions, name) is _owned(name)


@pytest.mark.parametrize("name", sorted(_OWNER.keys() - _CONSTANTS))
def test_callable_is_defined_in_its_owner(name: str) -> None:
    """Functions and classes report their owning module as ``__module__``."""
    obj = _owned(name)
    assert inspect.isfunction(obj) or inspect.isclass(obj)
    assert obj.__module__ == _OWNER[name]


@pytest.mark.parametrize("name", sorted(_CONSTANTS))
def test_constant_is_not_a_def(name: str) -> None:
    """The constant set holds no function or class (those are checked above)."""
    obj = _owned(name)
    assert not inspect.isfunction(obj)
    assert not inspect.isclass(obj)


# ---------------------------------------------------------------------------
# Logger pin: every logging module emits on the historic logger name
# ---------------------------------------------------------------------------


def _probe_parse(_monkeypatch: pytest.MonkeyPatch, _tmp_path: Path) -> None:
    """A malformed sentinel body logs a WARNING and degrades to nothing."""
    body = (
        f"{_MARKER_TITLE}<!-- {DISPOSITION_SENTINEL}\n{{not json\n"
        f"{DISPOSITION_SENTINEL} -->"
    )
    assert parse_finding_disposition_block([body]) == ({}, [])


def _probe_provenance(_monkeypatch: pytest.MonkeyPatch, _tmp_path: Path) -> None:
    """A refused record is warned about once."""
    log_refused_dispositions([RefusedDisposition(key="k", missing=["actor"])], _TICKET)


def _probe_match(_monkeypatch: pytest.MonkeyPatch, _tmp_path: Path) -> None:
    """A contested exact match is admitted at INFO and dropped."""
    finding = _make_finding(
        severity="MUST_FIX", contests_adjudication="the guard was deleted"
    )
    ledger_matches = _owned("_ledger_matches")
    assert callable(ledger_matches)
    matches = ledger_matches(
        [_accepted_finding(finding)],
        _settled(finding.file, finding.summary),
        ticket_id=_TICKET,
    )
    assert matches == {}


def _probe_drift(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """A git invocation that raises ``OSError`` is warned about and read as drift."""
    monkeypatch.setattr("cw._git.run_git", _raise_oserror)
    assert disposition_drifted(tmp_path, "aaa", "bbb", "src/cw/foo.py") is True


def _probe_emit(monkeypatch: pytest.MonkeyPatch, _tmp_path: Path) -> None:
    """A shadow event that cannot be written is logged, never raised."""
    monkeypatch.setattr("cw.events.record_event", _raise_oserror)
    finding = _make_finding(severity="MUST_FIX")
    ((key, entry),) = _settled(finding.file, finding.summary).items()
    ledger_match = _owned("_LedgerMatch")
    emit_shadow = _owned("_emit_shadow")
    assert callable(ledger_match)
    assert callable(emit_shadow)
    emit_shadow(
        _accepted_finding(finding),
        ledger_match(entry, key, "claim", 0.9),
        _TICKET,
        "abc1234",
    )


# Keyed by a function whose owner is the logging module the probe exercises,
# so the probe follows the owner table through every extraction commit.
_LOG_PROBES: dict[str, Callable[[pytest.MonkeyPatch, Path], None]] = {
    "_parse_one_disposition_block": _probe_parse,
    "log_refused_dispositions": _probe_provenance,
    "_ledger_matches": _probe_match,
    "disposition_drifted": _probe_drift,
    "_emit_shadow": _probe_emit,
}


@pytest.mark.parametrize("name", sorted(_LOG_PROBES))
def test_logging_module_emits_on_the_historic_logger(
    name: str,
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A real log line from each logging module carries the exact historic name.

    ``tests/test_codex_review_context.py`` filters on this name and asserts it
    on ``record.name``, and operator log routing keys on it, so a submodule
    logging under its own ``__name__`` would be a visible behavior change.
    """
    owner_file = Path(inspect.getfile(_owner(name))).resolve()
    with caplog.at_level(logging.DEBUG, logger=_LOGGER):
        _LOG_PROBES[name](monkeypatch, tmp_path)
    from_owner = [
        record
        for record in caplog.records
        if Path(record.pathname).resolve() == owner_file
    ]
    assert from_owner
    assert [record.name for record in caplog.records] == [_LOGGER] * len(caplog.records)


@pytest.mark.parametrize("name", sorted(_LOG_PROBES))
def test_logging_module_logger_has_the_historic_name(name: str) -> None:
    """Each module that defines ``_log`` binds it to the historic logger."""
    log = vars(_owner(name))["_log"]
    assert isinstance(log, logging.Logger)
    assert log.name == _LOGGER


@pytest.mark.parametrize("path", _package_files(), ids=lambda path: path.name)
def test_no_package_file_logs_under_its_own_name(path: Path) -> None:
    """``__name__`` would be the submodule's name, not the historic one."""
    assert "getLogger(__name__)" not in path.read_text(encoding="utf-8")


def test_logger_name_is_defined_once_in_constants() -> None:
    """One literal, in ``_constants``; every other module imports it."""
    assert vars(importlib.import_module(_CONSTANTS_MODULE))["_LOGGER_NAME"] == _LOGGER
    defining = [
        path.name
        for path in _package_files()
        for node in ast.parse(path.read_text(encoding="utf-8")).body
        if isinstance(node, ast.Assign)
        and any(
            isinstance(target, ast.Name) and target.id == "_LOGGER_NAME"
            for target in node.targets
        )
    ]
    assert defining == ["_constants.py"]


# ---------------------------------------------------------------------------
# Import discipline (#1838): the cycle guard
# ---------------------------------------------------------------------------


def _is_type_checking(test: ast.expr) -> bool:
    if isinstance(test, ast.Name):
        return test.id == "TYPE_CHECKING"
    return isinstance(test, ast.Attribute) and test.attr == "TYPE_CHECKING"


def _runtime_imports(
    statements: list[ast.stmt],
) -> Iterator[ast.Import | ast.ImportFrom]:
    """Imports that run when the module loads.

    Function bodies run later, and ``if TYPE_CHECKING:`` bodies never run, so
    both are skipped; class bodies and other compound statements run at import
    time and are walked.
    """
    for stmt in statements:
        if isinstance(stmt, ast.Import | ast.ImportFrom):
            yield stmt
        elif isinstance(stmt, ast.FunctionDef | ast.AsyncFunctionDef):
            continue
        elif isinstance(stmt, ast.If) and _is_type_checking(stmt.test):
            yield from _runtime_imports(stmt.orelse)
        else:
            for child in ast.iter_child_nodes(stmt):
                if isinstance(child, ast.stmt):
                    yield from _runtime_imports([child])
                elif isinstance(child, ast.excepthandler):
                    yield from _runtime_imports(child.body)


def _imported_modules(node: ast.Import | ast.ImportFrom) -> list[str]:
    if isinstance(node, ast.Import):
        return [alias.name for alias in node.names]
    # A relative import has no absolute name; report it so it is never allowed.
    return [("." * node.level) + (node.module or "")]


def _is_cw(name: str) -> bool:
    return name == "cw" or name.startswith((".", "cw."))


def _sibling_modules() -> set[str]:
    return {_module_name(path) for path in _package_files()} - {_PKG}


@pytest.mark.parametrize("path", _package_files(), ids=lambda path: path.name)
def test_module_scope_cw_imports_are_the_leaf_and_direct_siblings(path: Path) -> None:
    """The cycle guard: no module-scope ``cw`` import beyond the allowed set.

    Allowed at module scope: ``cw.review_markers`` (which imports nothing from
    ``cw``) and sibling submodules by their direct path. Every other ``cw``
    import must sit in a function body or under ``TYPE_CHECKING``.
    """
    tree = ast.parse(path.read_text(encoding="utf-8"))
    allowed = {"cw.review_markers"} | (_sibling_modules() - {_module_name(path)})
    imported = {
        name
        for node in _runtime_imports(tree.body)
        for name in _imported_modules(node)
        if _is_cw(name)
    }
    assert imported <= allowed, sorted(imported - allowed)


@pytest.mark.parametrize("path", _package_files(), ids=lambda path: path.name)
def test_no_package_file_imports_the_bare_package(path: Path) -> None:
    """Siblings reach each other by direct path, never through the package."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    bare = [
        node.lineno
        for node in ast.walk(tree)
        if isinstance(node, ast.Import | ast.ImportFrom)
        and _PKG in _imported_modules(node)
    ]
    assert bare == []


@pytest.mark.parametrize(
    "relative",
    ["review_findings/_models.py", "codex_review/_context/_prompt_text.py"],
)
def test_the_freed_modules_import_nothing_from_the_ledger(relative: str) -> None:
    """Neither the package nor any of its submodules, anywhere in the file.

    ``tests/test_review_markers.py`` checks only the exact package name, which
    a ``cw.review_finding_dispositions.<sub>`` import would slip past.
    """
    tree = ast.parse((_SRC / relative).read_text(encoding="utf-8"))
    offenders = {
        name
        for node in ast.walk(tree)
        if isinstance(node, ast.Import | ast.ImportFrom)
        for name in _imported_modules(node)
        if name == _PKG or name.startswith(f"{_PKG}.")
    }
    assert offenders == set()


@pytest.mark.parametrize("first", _FIRST_IMPORTS)
def test_module_imports_cleanly_whichever_module_loads_first(first: str) -> None:
    """Import-order smoke test (#1838), one cold interpreter per first import.

    ``cw.models.tasks`` imports this package, so a module-scope ``cw`` import
    here could close a cycle that only fails in ONE import order — invisible
    to a test suite whose conftest always warms ``cw.models`` first. Each
    interpreter below starts cold and imports one module, then the rest.

    A smoke test, not cycle proof: importing a submodule runs the package
    ``__init__`` first, so these orders cannot isolate a submodule. The AST
    test above is the cycle guard. Uses ``sys.executable`` (not a bare
    ``python3``) per PYTHON-PATTERNS' compiled-dependency isolation rule —
    ``pydantic_core`` is ABI-bound to this venv.
    """
    script = (
        f"import {first}\n"
        "import cw.review_finding_dispositions, cw.models, cw.events, cw.review_debt\n"
        "from cw.models import TicketTask\n"
        "assert TicketTask(ticket_id='T-1', client='c').finding_dispositions == {}\n"
    )
    subprocess.run([sys.executable, "-I", "-c", script], check=True)


def test_models_tasks_imported_first_loads_the_model_owner() -> None:
    """Smoke test: a cold ``import cw.models.tasks`` pulls in the model's owner."""
    expected = sorted({_PKG, _OWNER["FindingDisposition"]})
    script = (
        "import sys\n"
        "import cw.models.tasks\n"
        f"missing = [m for m in {expected!r} if m not in sys.modules]\n"
        "assert not missing, missing\n"
    )
    subprocess.run([sys.executable, "-I", "-c", script], check=True)


@pytest.mark.parametrize("module", _MODULES)
def test_module_imports_cold(module: str) -> None:
    """Smoke test: each module imports in a fresh isolated interpreter."""
    subprocess.run([sys.executable, "-I", "-c", f"import {module}"], check=True)


# ---------------------------------------------------------------------------
# Patch ownership: patches on the source module reach the reader
# ---------------------------------------------------------------------------


class TestPatchOwnership:
    """``record_event`` and ``run_git`` are patched on their SOURCE modules.

    That only reaches the reader because each reader imports the name inside
    its own body, at call time. A move that binds either name at module scope
    would leave every such patch silently inert; this pins that it does not.
    """

    @pytest.mark.parametrize(("reader", "source", "name"), _PATCHED_READERS)
    def test_reader_imports_the_patched_name_in_its_body(
        self, reader: str, source: str, name: str
    ) -> None:
        fn = _owned(reader)
        assert inspect.isfunction(fn)
        assert fn.__module__ == _OWNER[reader]
        tree = ast.parse(textwrap.dedent(inspect.getsource(fn)))
        imported = {
            (node.module, alias.name)
            for node in ast.walk(tree)
            if isinstance(node, ast.ImportFrom)
            for alias in node.names
        }
        assert (source, name) in imported
        assert name not in fn.__globals__

    @pytest.mark.parametrize("module", _MODULES)
    def test_no_module_binds_a_patched_name(self, module: str) -> None:
        assert _PATCHED_NAMES.isdisjoint(vars(importlib.import_module(module)))

    def test_suppress_reaches_the_patched_names_only_through_its_callees(
        self,
    ) -> None:
        fn = _owned("suppress_adjudicated_findings")
        assert inspect.isfunction(fn)
        assert _PATCHED_NAMES.isdisjoint(fn.__globals__)

    def test_a_record_event_patch_reaches_the_suppression_emitter(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        captured: list[object] = []

        def _capture(event_type: object, **_kwargs: object) -> None:
            captured.append(event_type)

        monkeypatch.setattr("cw.events.record_event", _capture)
        finding = _make_finding(severity="MUST_FIX")
        result = suppress_adjudicated_findings(
            _blocking_verdict(finding),
            _settled(finding.file, finding.summary),
            ticket_id=_TICKET,
        )
        assert result.blocking is False
        assert len(captured) == 1

    def test_a_run_git_patch_reaches_the_drift_check(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        monkeypatch.setattr("cw._git.run_git", _raise_oserror)
        assert disposition_drifted(tmp_path, "aaa", "bbb", "src/cw/foo.py") is True


# ---------------------------------------------------------------------------
# Entrypoint: ``cw review dispositions`` through the package's re-exports
# ---------------------------------------------------------------------------


def test_review_dispositions_command_reports_a_drifted_record(
    make_git_repo: Callable[..., Path],
) -> None:
    """``cw review dispositions --worktree`` end to end, drift path included.

    The command imports ``disposition_drifted`` and ``split_disposition_key``
    from the package and returns early without a worktree or a HEAD, so the
    record is settled against a first commit and the worktree's HEAD is a
    second commit that changed the file: STALE must read ``yes``.
    """
    worktree = make_git_repo("wt-2498-entrypoint")
    commit_tracked_file(worktree, "src/cw/foo.py", "a = 1\n")
    settled_at = git_in(worktree, "rev-parse", "HEAD")
    commit_tracked_file(worktree, "src/cw/foo.py", "a = 2  # reworked\n")
    ledger = _settled("src/cw/foo.py", "Bug here")
    ((key, entry),) = ledger.items()
    ledger[key] = entry.model_copy(update={"reviewed_sha": settled_at})
    add_ticket(
        TicketTask(
            ticket_id=_TICKET,
            client="acme",
            stage=Stage.REVIEW,
            finding_dispositions=ledger,
        )
    )
    args = ["review", "dispositions", _TICKET, "--client", "acme"]
    runner = CliRunner()

    table = runner.invoke(main, [*args, "--worktree", str(worktree)])
    rows = runner.invoke(main, [*args, "--worktree", str(worktree), "--json"])

    assert table.exit_code == 0, table.output
    assert "STALE" in table.output
    assert "src/cw/foo.py" in table.output
    assert "REJECTED" in table.output
    assert rows.exit_code == 0, rows.output
    assert [(row["key"], row["stale"]) for row in json.loads(rows.output)] == [
        (key, "yes")
    ]
