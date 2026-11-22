"""Static guard: every ``.default_branch`` read in ``src/`` is classified (RFC 0020).

A read is classified one of three ways:

- the seam: a whole-file allowlist (``cw/models/branch.py``, ``cw/models/client.py``);
- annotated in place: a ``# stays on client.default_branch: <reason>`` comment on
  the read's statement or the comment block directly above it;
- allowlisted by ``(relpath, qualname, receiver)`` with a reason. A trust-root
  entry keeps the client default on purpose (a trust root).

The guard cannot see kwargs or parameters; the per-crossing tests in
``tests/test_review_base_branch_sites.py`` cover those. The allowlist is
shrink-only, pinned by ``_MAX_ALLOWLIST_ENTRIES``.
"""

from __future__ import annotations

import ast
import io
import tokenize
from typing import NamedTuple

from tests._shared.workflow_text import _SRC_ROOT, _iter_src_files

_MARKER = "stays on client.default_branch:"
_SEAM_FILES = frozenset({"cw/models/branch.py", "cw/models/client.py"})
_MAX_ALLOWLIST_ENTRIES = 12
_MIN_READS = 34  # non-vacuity; S3b (#3840) flipped codex merge-target reads off
# .default_branch, so the count fell 40 -> 35. Flagged in the PR, not silent.


class Read(NamedTuple):
    relpath: str
    qualname: str
    receiver: str
    lineno: int
    reason: str | None  # the ``# stays on client.default_branch:`` reason, if any

    @property
    def annotated(self) -> bool:
        return self.reason is not None


class _Entry(NamedTuple):
    relpath: str
    qualname: str
    receiver: str
    reason: str


_TRUST = "trust root: "

_ALLOWLIST: tuple[_Entry, ...] = (
    _Entry(
        "cw/cli/_freshness.py",
        "_resolve_default_branch_and_workspace",
        "cc",
        _TRUST + "primary-checkout freshness reads the client default",
    ),
    _Entry(
        "cw/config.py",
        "show_config",
        "client",
        _TRUST + "display of the configured default branch",
    ),
    _Entry(
        "cw/codex_review/_context/_default_branch_config.py",
        "review_config_status",
        "source",
        _TRUST + "persisted verdict field; the name is not renamed",
    ),
    _Entry(
        "cw/codex_review/_verdict/_render/_notes.py",
        "_render_review_config_note",
        "config",
        _TRUST + "display of the review-config verdict",
    ),
    _Entry(
        "cw/codex_review/_engine_start.py",
        "engine_round_spec",
        "client",
        _TRUST + "engine source and manifest (ADR-0024 D-3)",
    ),
    _Entry(
        "cw/doctor/review_engine.py",
        "_ClientManifest.get",
        "self._client",
        _TRUST + "probes the default branch's manifest",
    ),
    _Entry(
        "cw/doctor/linkage.py",
        "_check_dispatch_repo_head",
        "client",
        _TRUST + "primary-checkout HEAD check",
    ),
    _Entry(
        "cw/review_engine/materialize.py",
        "materialize_skill",
        "context",
        _TRUST + "engine skill copy source",
    ),
    _Entry(
        "cw/review_engine/preflight.py",
        "run_preflight",
        "context",
        _TRUST + "guard-set source",
    ),
    _Entry(
        "cw/spawn.py",
        "spawn_create_impl",
        "client",
        _TRUST + "feeds origin_main_branch, written at spawn",
    ),
    _Entry(
        "cw/executor/codex.py", "_codex_preflight", "client", _TRUST + "context writer"
    ),
    _Entry(
        "cw/executor/opencode.py",
        "_opencode_preflight",
        "client",
        _TRUST + "context writer",
    ),
)


def _qualname(tree: ast.Module, target: ast.AST) -> str:
    """Dotted name of the enclosing function/class chain of *target*."""
    found: list[str] = []

    def visit(node: ast.AST, stack: list[str]) -> bool:
        for child in ast.iter_child_nodes(node):
            if child is target:
                found.extend(stack)
                return True
            nested = isinstance(
                child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)
            )
            if visit(child, [*stack, child.name] if nested else stack):
                return True
        return False

    visit(tree, [])
    return ".".join(found) or "<module>"


def _full_reason(source_lines: list[str], comments: dict[int, str], start: int) -> str:
    """The marker comment's reason joined with its standalone continuation lines.

    A reason may wrap onto the comment lines directly below the marker line; they
    are joined with single spaces so the one-line table row carries all of it.
    A trailing comment on a code line is not a continuation.
    """
    parts = [comments[start].split(_MARKER, 1)[1].strip()]
    number = start + 1
    while (
        number in comments
        and source_lines[number - 1].lstrip().startswith("#")
        and _MARKER not in comments[number]
    ):
        parts.append(comments[number].lstrip("#").strip())
        number += 1
    return " ".join(part for part in parts if part)


def _annotation_reason(source: str, tree: ast.Module, node: ast.AST) -> str | None:
    """The reason on the marker comment for *node*'s statement, or ``None``."""
    comments = {
        tok.start[0]: tok.string
        for tok in tokenize.generate_tokens(io.StringIO(source).readline)
        if tok.type == tokenize.COMMENT
    }
    line = getattr(node, "lineno", 0)
    statements = [s for s in ast.walk(tree) if isinstance(s, ast.stmt)]
    enclosing = max(
        (s for s in statements if s.lineno <= line <= (s.end_lineno or s.lineno)),
        key=lambda s: s.lineno,
    )
    lines = list(range(enclosing.lineno, line + 1))
    above = enclosing.lineno - 1
    while above in comments:
        lines.append(above)
        above -= 1
    for number in lines:
        text = comments.get(number, "")
        if _MARKER in text and text.split(_MARKER, 1)[1].strip():
            return _full_reason(source.splitlines(), comments, number)
    return None


def default_branch_reads(source: str, relpath: str) -> list[Read]:
    """Every ``.default_branch`` load and ``getattr(x, "default_branch")``."""
    tree = ast.parse(source)
    reads: list[Read] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute) and node.attr == "default_branch":
            if not isinstance(node.ctx, ast.Load):
                continue
            receiver = ast.unparse(node.value)
        elif (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "getattr"
            and len(node.args) >= 2
            and isinstance(node.args[1], ast.Constant)
            and node.args[1].value == "default_branch"
        ):
            receiver = ast.unparse(node.args[0])
        else:
            continue
        reason = _annotation_reason(source, tree, node)
        reads.append(
            Read(relpath, _qualname(tree, node), receiver, node.lineno, reason)
        )
    return reads


def _all_reads() -> list[Read]:
    reads: list[Read] = []
    for path in _iter_src_files():
        relpath = path.relative_to(_SRC_ROOT).as_posix()
        reads.extend(default_branch_reads(path.read_text(encoding="utf-8"), relpath))
    return reads


def _entry_keys() -> set[tuple[str, str, str]]:
    return {(e.relpath, e.qualname, e.receiver) for e in _ALLOWLIST}


def unclassified(
    reads: list[Read],
    keys: set[tuple[str, str, str]],
    seam_files: frozenset[str],
) -> list[Read]:
    """Reads that are not in a seam file, not annotated and not allowlisted."""
    return [
        r
        for r in reads
        if r.relpath not in seam_files
        and not r.annotated
        and (r.relpath, r.qualname, r.receiver) not in keys
    ]


def test_every_read_is_seam_annotated_or_allowlisted() -> None:
    found = [
        f"{r.relpath}:{r.lineno} {r.qualname} ({r.receiver})"
        for r in unclassified(_all_reads(), _entry_keys(), _SEAM_FILES)
    ]
    assert found == [], (
        "unclassified .default_branch read: route it through resolve_base_branch, "
        "annotate it '# stays on client.default_branch: <reason>', or add an "
        f"allowlist entry with a reason: {found}"
    )


_BARE = Read("cw/x.py", "f", "client", 3, None)


def test_unclassified_reports_a_bare_read() -> None:
    assert unclassified([_BARE], set(), _SEAM_FILES) == [_BARE]


def test_unclassified_passes_an_annotated_read() -> None:
    annotated = _BARE._replace(reason="primary checkout")

    assert unclassified([annotated], set(), _SEAM_FILES) == []


def test_unclassified_passes_an_allowlisted_read() -> None:
    keys = {("cw/x.py", "f", "client")}

    assert unclassified([_BARE], keys, _SEAM_FILES) == []


def test_unclassified_reports_a_read_whose_receiver_differs_from_the_entry() -> None:
    keys = {("cw/x.py", "f", "other")}

    assert unclassified([_BARE], keys, _SEAM_FILES) == [_BARE]


def test_unclassified_passes_a_seam_file_read() -> None:
    seam_read = _BARE._replace(relpath="cw/models/branch.py")

    assert unclassified([seam_read], set(), _SEAM_FILES) == []


def test_every_allowlist_entry_matches_a_read() -> None:
    seen = {(r.relpath, r.qualname, r.receiver) for r in _all_reads()}
    stale = sorted(k for k in _entry_keys() if k not in seen)
    assert stale == [], f"stale allowlist entries (no matching read): {stale}"


def test_allowlist_reasons_are_non_empty() -> None:
    assert all(entry.reason.strip() for entry in _ALLOWLIST)


def test_every_entry_is_a_trust_root_and_none_names_s3b() -> None:
    """S3b (#3840) emptied the deferred codex entries; only trust roots remain."""
    assert all(e.reason.startswith(_TRUST) for e in _ALLOWLIST)
    assert not any("S3b" in e.reason for e in _ALLOWLIST)


def test_allowlist_count_is_pinned_shrink_only() -> None:
    assert len(_ALLOWLIST) == _MAX_ALLOWLIST_ENTRIES


def test_guard_is_not_vacuous() -> None:
    reads = _all_reads()
    assert len(reads) > _MIN_READS
    assert any(r.relpath in _SEAM_FILES for r in reads)
    assert sum(r.annotated for r in reads) >= 14  # S2 (#2994 family) + confirm.py


def test_collector_flags_bare_read() -> None:
    reads = default_branch_reads(
        "def f(client):\n    return client.default_branch\n", "x.py"
    )
    assert [(r.qualname, r.receiver, r.annotated) for r in reads] == [
        ("f", "client", False)
    ]


def test_collector_honours_annotation_with_a_reason() -> None:
    source = (
        "def f(client):\n"
        "    # stays on client.default_branch: the primary checkout\n"
        "    return client.default_branch\n"
    )
    [read] = default_branch_reads(source, "x.py")
    assert read.annotated is True


def test_collector_rejects_an_empty_reason() -> None:
    source = (
        "def f(client):\n"
        "    # stays on client.default_branch:\n"
        "    return client.default_branch\n"
    )
    [read] = default_branch_reads(source, "x.py")
    assert read.annotated is False


def test_collector_ignores_stores_and_flags_getattr() -> None:
    source = (
        "def f(client):\n"
        "    client.default_branch = 'x'\n"
        "    return getattr(client, 'default_branch')\n"
    )
    reads = default_branch_reads(source, "x.py")
    assert [(r.receiver, r.lineno) for r in reads] == [("client", 3)]


def test_collector_honours_annotation_above_an_assignment_with_a_conditional() -> None:
    source = (
        "def f(client, flag):\n"
        "    # stays on client.default_branch: the primary checkout\n"
        "    base = client.default_branch if flag else 'main'\n"
        "    return base\n"
    )

    [read] = default_branch_reads(source, "x.py")

    assert read.reason == "the primary checkout"


def test_collector_joins_a_multi_line_annotation_into_one_reason() -> None:
    source = (
        "def f(client):\n"
        "    # stays on client.default_branch: the primary checkout's HEAD must\n"
        "    # be on the default branch, so the\n"
        "    # operator remedy names it\n"
        "    return client.default_branch\n"
    )

    [read] = default_branch_reads(source, "x.py")

    assert read.reason == (
        "the primary checkout's HEAD must be on the default branch, so the "
        "operator remedy names it"
    )


def test_collector_does_not_swallow_a_trailing_code_comment_as_continuation() -> None:
    source = (
        "def f(client):\n"
        "    # stays on client.default_branch: the primary checkout\n"
        "    base = client.default_branch\n"
        "    other = 1  # unrelated\n"
        "    return base, other\n"
    )

    [read] = default_branch_reads(source, "x.py")

    assert read.reason == "the primary checkout"


def test_collector_ignores_a_class_body_annotation() -> None:
    source = "class Config:\n    default_branch: str\n    other: str = 'x'\n"

    assert default_branch_reads(source, "x.py") == []


def test_collector_reports_the_enclosing_class_and_function() -> None:
    source = (
        "class C:\n    def m(self, client):\n        return client.default_branch\n"
    )

    [read] = default_branch_reads(source, "x.py")

    assert read.qualname == "C.m"


_SEAM_WHY = {
    "cw/models/branch.py": "the single reader (`resolve_base_branch`)",
    "cw/models/client.py": "the field definition",
}


def _allowlist_row(entry: _Entry) -> str:
    """One bullet for an allowlist entry: the category label, then the reason once."""
    label = "trust root; S3a"
    body = entry.reason.removeprefix(_TRUST)
    return f"- {entry.relpath} `{entry.qualname}` ({entry.receiver}): {label}; {body}"


def _read_row(read: Read) -> str:
    where = f"- {read.relpath}:{read.lineno} `{read.qualname}` ({read.receiver}): "
    if read.relpath in _SEAM_FILES:
        return f"{where}seam; S3a; {_SEAM_WHY[read.relpath]}"
    return f"{where}classified in place; S3a; {read.reason}"


def render_classification_table() -> str:
    """The PR-description listing: one plain bullet per seam read, annotated read
    and allowlist entry, so every remaining ``.default_branch`` read appears once.
    """
    rows = [
        _read_row(r) for r in _all_reads() if r.relpath in _SEAM_FILES or r.annotated
    ]
    rows.extend(_allowlist_row(e) for e in _ALLOWLIST)
    return "\n".join(rows)


def _table_lines() -> list[str]:
    return render_classification_table().splitlines()


def test_table_has_a_row_per_annotation_entry_and_seam_read() -> None:
    reads = _all_reads()
    seam = sum(r.relpath in _SEAM_FILES for r in reads)
    annotated = sum(r.annotated and r.relpath not in _SEAM_FILES for r in reads)
    lines = _table_lines()

    assert annotated > 0
    assert seam > 0
    assert len(lines) == annotated + seam + len(_ALLOWLIST)
    assert all(line.startswith("- ") for line in lines)


def test_table_carries_a_multi_line_reason_in_full_on_one_row() -> None:
    lines = _table_lines()
    # Every annotated read's whole reason is on its row, never a cut-off first line.
    for read in _all_reads():
        if read.annotated and read.relpath not in _SEAM_FILES:
            assert any(f"; {read.reason}" in line for line in lines)
    assert any("the primary checkout's HEAD must be on " in line for line in lines)


def test_table_rows_do_not_repeat_the_category_prefix() -> None:
    lines = _table_lines()

    assert not any("trust root; S3a; trust root" in line for line in lines)


def test_table_lists_the_seam_reader() -> None:
    lines = _table_lines()

    assert any(
        line.startswith("- cw/models/branch.py:")
        and "`resolve_base_branch`" in line
        and ": seam; S3a; " in line
        for line in lines
    )


def test_table_does_not_label_an_annotated_read_a_trust_root() -> None:
    annotated = [r for r in _all_reads() if r.annotated]
    lines = _table_lines()

    for read in annotated:
        [row] = [
            line
            for line in lines
            if line.startswith(f"- {read.relpath}:{read.lineno} ")
        ]
        assert "): classified in place; S3a; " in row
        assert "): trust root" not in row
