"""Tests for ``cw.contract_manifest`` and the real contract drift (#3053).

Two halves. The unit half drives the loader, the tag scanner, the ref
resolver and the pure checker against in-memory inputs and ``tmp_path``
repos, asserting the exact ``ContractViolation`` kind for every rule. The
real-data half runs the checker over the packaged manifest and the thirteen
command docs and fails on any gap: tag every new contract rule.

Hermetic: no ``gh``, no ``git``, no ``claude``; writes only to ``tmp_path``.
"""

from __future__ import annotations

import copy
import functools
import importlib.resources
import re
from typing import TYPE_CHECKING, Any, get_args

import pydantic
import pytest
import yaml

from cw.auto_dev_result import KNOWN_BLOCKER_REASONS, Status
from cw.contract_manifest import (
    AUTHORING_RULE,
    DOC_SCOPES,
    NATIVE_BACKEND,
    ORCH_GOVERNS,
    STAGES,
    STRICT_RANGE_TAG,
    STRICT_TAG,
    TAG_SHAPED,
    TODO_CELL,
    CodeRef,
    ContractViolation,
    DeferredDisposition,
    DocRange,
    DocRef,
    DocScan,
    FailsClosedDisposition,
    ImplementedDisposition,
    Manifest,
    ManifestLoadError,
    NotApplicableDisposition,
    PromptRef,
    TextRef,
    TicketRef,
    check_contract,
    elements_with,
    load_manifest,
    make_resolver,
    parse_manifest,
    scan_doc_tags,
    scope_stage,
    strip_doc_tags,
)
from cw.contract_manifest import _models as models_module
from cw.models import Stage
from cw.models.orchestrator_config.constants import (
    CLAUDE_NATIVE_BACKEND,
    CODEX_BACKEND,
    LOCAL_BACKEND,
    OPENCODE_BACKEND,
)
from cw.opencode_clean_review import clean_review_body
from cw.opencode_runner import build_stage_prompt
from tests._shared.workflow_text import _REPO_ROOT, _appendix, _cmd, _section_between

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping
    from pathlib import Path

    from cw.contract_manifest import BackendDisposition, ElementRef, Resolver

# ---------------------------------------------------------------------------
# File-local builders
# ---------------------------------------------------------------------------

_AIDER_REASON = "aider has no agent turn; result synthesized from git"
_BACKENDS = ("claude-native", "codex", "opencode", "local")
_STATUSES = ("shipped", "blocked")
_REASONS = ("impl_failed",)

_BASELINE_YAML = f"""
schema_version: 1
backends: [claude-native, codex, opencode, local]
elements:
  orch.status-enum:
    covers: [shipped, blocked, impl_failed]
    cells:
      claude-native: {{disposition: implemented}}
      codex:
        {{disposition: implemented, ref: {{kind: code, path: x.py, symbol: f}}}}
      opencode:
        {{disposition: implemented, ref: {{kind: prompt, stage: plan, needle: x}}}}
      local: {{disposition: not-applicable, reason: "{_AIDER_REASON}"}}
  review.engine-round:
    cells:
      claude-native: {{disposition: implemented, ref: {{kind: doc, path: d.md}}}}
      codex: {{disposition: deferred, ref: {{kind: ticket, number: 3216}}}}
      opencode:
        disposition: fails-closed
        guard_pending: true
        ref: {{kind: ticket, number: 3216}}
      local:
        {{disposition: fails-closed, ref: {{kind: code, path: g.py, symbol: guard}}}}
"""

_BASELINE_DOCS = {
    "auto-dev.md": "Intro.\n\n<!-- el: orch.status-enum -->\n**Status enum (#100).**\n",
    "auto-dev-review.md": (
        "<!-- el: review.engine-round -->\n### Step 3a.5: Engine round (#3039)\n"
    ),
}


def _baseline_dict() -> dict[str, Any]:
    loaded: dict[str, Any] = yaml.safe_load(_BASELINE_YAML)
    return copy.deepcopy(loaded)


def _baseline_manifest(raw: dict[str, Any] | None = None) -> Manifest:
    return Manifest.model_validate(_baseline_dict() if raw is None else raw)


def _docs(**by_name: str) -> dict[str, str]:
    """In-memory docs keyed by file name (``_`` in a kwarg becomes ``-``)."""
    return {f"{name.replace('_', '-')}.md": text for name, text in by_name.items()}


def _tagged(element_id: str, anchor: str = "**Lead (#1).**", above: str = "") -> str:
    """One valid tagged doc body: optional line above, tag, anchor."""
    prefix = f"{above}\n" if above else ""
    return f"{prefix}<!-- el: {element_id} -->\n{anchor}\n"


def _stub_resolver(fail: ElementRef | None = None) -> Resolver:
    def resolve(ref: ElementRef) -> str | None:
        return "stub failure" if ref == fail else None

    return resolve


def _repo(tmp_path: Path, files: Mapping[str, str]) -> Path:
    for relative, text in files.items():
        target = tmp_path / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")
    return tmp_path


def _check(
    manifest: Manifest | None = None,
    scan: DocScan | None = None,
    *,
    backends: tuple[str, ...] = _BACKENDS,
    resolver: Resolver | None = None,
) -> list[ContractViolation]:
    return check_contract(
        _baseline_manifest() if manifest is None else manifest,
        scan_doc_tags(_BASELINE_DOCS) if scan is None else scan,
        backends=backends,
        statuses=_STATUSES,
        blocker_reasons=_REASONS,
        resolver=_stub_resolver() if resolver is None else resolver,
    )


def _kinds(violations: list[ContractViolation]) -> list[str]:
    return [v.kind for v in violations]


def _scan_kinds(docs: Mapping[str, str]) -> list[str]:
    return [v.kind for v in scan_doc_tags(docs).violations]


# ---------------------------------------------------------------------------
# Loader
# ---------------------------------------------------------------------------


def _load_error(text: str) -> str:
    with pytest.raises(ManifestLoadError) as info:
        parse_manifest(text)
    return str(info.value)


def _with_cell(cell: str) -> str:
    return (
        "schema_version: 1\nbackends: [codex]\nelements:\n"
        f"  orch.x:\n    cells:\n      codex: {cell}\n"
    )


def test_load_malformed_yaml() -> None:
    assert "not valid YAML" in _load_error("schema_version: [1\n")


def test_load_non_mapping_root() -> None:
    assert "root must be a mapping" in _load_error("- 1\n- 2\n")


def test_load_missing_schema_version() -> None:
    assert "schema_version" in _load_error("backends: []\nelements: {}\n")


def test_load_wrong_schema_version() -> None:
    assert "schema_version" in _load_error(
        "schema_version: 2\nbackends: []\nelements: {}\n"
    )


def test_load_duplicate_element_id() -> None:
    text = (
        "schema_version: 1\nbackends: []\nelements:\n"
        "  orch.x: {cells: {}}\n  orch.x: {cells: {}}\n"
    )
    assert "duplicate key 'orch.x'" in _load_error(text)


def test_load_unknown_top_level_key() -> None:
    assert "extra" in _load_error(
        "schema_version: 1\nbackends: []\nelements: {}\nextra: 1\n"
    )


def test_load_unknown_disposition() -> None:
    _load_error(_with_cell("{disposition: maybe}"))


def test_load_extra_key_on_cell() -> None:
    _load_error(_with_cell("{disposition: implemented, colour: red}"))


def test_load_cell_neither_mapping_nor_todo() -> None:
    _load_error(_with_cell("DONE"))


def test_load_deferred_without_ref() -> None:
    _load_error(_with_cell("{disposition: deferred}"))


def test_load_deferred_with_non_ticket_ref() -> None:
    _load_error(
        _with_cell("{disposition: deferred, ref: {kind: doc, path: README.md}}")
    )


def test_load_not_applicable_with_empty_reason() -> None:
    _load_error(_with_cell('{disposition: not-applicable, reason: ""}'))


def test_load_not_applicable_with_ref() -> None:
    _load_error(
        _with_cell(
            "{disposition: not-applicable, reason: r, ref: {kind: ticket, number: 1}}"
        )
    )


def test_load_fails_closed_with_text_ref() -> None:
    _load_error(
        _with_cell("{disposition: fails-closed, ref: {kind: text, path: a, needle: b}}")
    )


def test_load_unknown_ref_kind() -> None:
    _load_error(_with_cell("{disposition: implemented, ref: {kind: url, path: x}}"))


def test_load_non_int_ticket_number() -> None:
    _load_error(
        _with_cell('{disposition: deferred, ref: {kind: ticket, number: "3059"}}')
    )


def test_load_todo_parses() -> None:
    manifest = parse_manifest(_with_cell("TODO"))
    assert manifest.elements["orch.x"].cells["codex"] == "TODO"


def test_load_duplicate_key_check_ignores_non_scalar_keys() -> None:
    text = "? [a, b]\n: 1\n"
    assert "root must be a mapping" not in _load_error(text)


def test_frozen_models_reject_mutation() -> None:
    manifest = _baseline_manifest()
    field = "schema_version"
    with pytest.raises(pydantic.ValidationError):
        setattr(manifest, field, 1)


def test_manifest_load_error_is_a_value_error_defined_in_models() -> None:
    assert issubclass(ManifestLoadError, ValueError)
    assert ManifestLoadError.__module__ == models_module.__name__


def test_ids_with_returns_sorted_ids() -> None:
    manifest = _baseline_manifest()
    assert manifest.ids_with("claude-native", "implemented") == (
        "orch.status-enum",
        "review.engine-round",
    )
    assert manifest.ids_with("local", "fails-closed") == ("review.engine-round",)


def test_ids_with_unknown_backend_is_empty() -> None:
    assert _baseline_manifest().ids_with("aider", "implemented") == ()


def test_ids_with_unknown_disposition_raises() -> None:
    with pytest.raises(ValueError, match="unknown disposition"):
        _baseline_manifest().ids_with("codex", "maybe")


def test_ids_with_skips_todo_cells() -> None:
    assert parse_manifest(_with_cell("TODO")).ids_with("codex", "deferred") == ()


# ---------------------------------------------------------------------------
# Scanner and strip
# ---------------------------------------------------------------------------


def test_scan_collects_a_valid_tag_with_doc_and_line() -> None:
    scan = scan_doc_tags({"auto-dev.md": "Intro.\n\n" + _tagged("orch.x")})
    assert scan.violations == ()
    assert [(t.element_id, t.scope, t.doc, t.line) for t in scan.tags] == [
        ("orch.x", "orch", "auto-dev.md", 3)
    ]


@pytest.mark.parametrize(
    "line",
    [
        " <!-- el: orch.x -->",
        "<!--  el: orch.x -->",
        "<!-- el: orch.x --> trailing",
        "<!-- el: Orch.X -->",
        "<!-- el: cw-orch.x -->x",
        "<!-- el: orchx -->",
    ],
    ids=["indented", "extra-space", "trailing", "uppercase", "cw-prefix", "no-dot"],
)
def test_scan_tag_malformed(line: str) -> None:
    assert _scan_kinds({"auto-dev.md": f"{line}\n**Lead.**\n"}) == ["tag_malformed"]


@pytest.mark.parametrize(
    "fence",
    [
        "```\n<!-- el: orch.x -->\n```\n",
        "~~~\n<!-- el: orch.x -->\n~~~\n",
        "````\n```\n<!-- el: orch.x -->\n```\n````\n",
    ],
    ids=["backtick", "tilde", "nested-longer"],
)
def test_scan_tag_in_fence(fence: str) -> None:
    assert _scan_kinds({"auto-dev.md": fence}) == ["tag_in_fence"]


def test_scan_tag_in_table() -> None:
    docs = {"auto-dev.md": "| a |\n|---|\n<!-- el: orch.x -->\n| b |\n"}
    assert _scan_kinds(docs) == ["tag_in_table"]


def test_scan_tag_after_a_table_is_not_in_it() -> None:
    docs = {"auto-dev.md": "| a |\n|---|\n<!-- el: orch.x -->\n**Lead.**\n"}
    assert _scan_kinds(docs) == []


_RANGE_OK = (
    "<!-- el-begin: orch.x -->\n<!-- el: orch.x -->\n**Lead (#1).**\nbody\n\n"
    "<!-- el-end: orch.x -->\n"
)


def test_scan_range_pair_is_clean() -> None:
    scan = scan_doc_tags({"auto-dev.md": _RANGE_OK})
    assert scan.violations == ()
    assert scan.ranges == (
        DocRange(element_id="orch.x", doc="auto-dev.md", begin_line=1, end_line=6),
    )
    assert [(t.element_id, t.line) for t in scan.tags] == [("orch.x", 2)]


def test_scan_adjacent_ranged_pair_is_clean() -> None:
    body = (
        _RANGE_OK
        + "<!-- el-begin: orch.y -->\n<!-- el: orch.y -->\n**Lead (#2).**\nb\n\n"
        + "<!-- el-end: orch.y -->\n"
    )
    scan = scan_doc_tags({"auto-dev.md": body})
    assert scan.violations == ()
    assert [r.element_id for r in scan.ranges] == ["orch.x", "orch.y"]


@pytest.mark.parametrize(
    ("body", "kinds"),
    [
        (
            "<!-- el-begin: orch.x -->\n<!-- el: orch.x -->\n**Lead.**\n",
            ["range_unpaired"],
        ),
        ("<!-- el-end: orch.x -->\n", ["range_unpaired"]),
        (
            "**Lead.**\n<!-- el-begin: orch.x -->",
            ["range_id_mismatch", "range_unpaired"],
        ),
    ],
    ids=["begin-no-end", "end-no-begin", "begin-at-eof"],
)
def test_scan_range_unpaired(body: str, kinds: list[str]) -> None:
    assert _scan_kinds({"auto-dev.md": body}) == kinds


@pytest.mark.parametrize(
    "body",
    [
        "<!-- el-begin: orch.x -->\n<!-- el: orch.x -->\n**Lead.**\n"
        "<!-- el-begin: orch.y -->\n<!-- el: orch.y -->\n**Lead.**\n\n"
        "<!-- el-end: orch.x -->\n",
        "<!-- el-begin: orch.x -->\n<!-- el: orch.x -->\n**Lead.**\n\n"
        "<!-- el: orch.y -->\n**Lead.**\n\n<!-- el-end: orch.x -->\n",
    ],
    ids=["second-begin", "other-el-tag"],
)
def test_scan_range_nested(body: str) -> None:
    assert "range_nested" in _scan_kinds({"auto-dev.md": body})


@pytest.mark.parametrize(
    ("body", "kinds"),
    [
        (
            "<!-- el-begin: orch.x -->\n<!-- el: orch.x -->\n**Lead.**\n\n"
            "<!-- el-end: orch.y -->\n",
            ["range_id_mismatch"],
        ),
        (
            "<!-- el-begin: orch.x -->\n<!-- el: orch.y -->\n**Lead.**\n\n"
            "<!-- el-end: orch.x -->\n",
            ["range_id_mismatch"],
        ),
        (
            "<!-- el-begin: orch.x -->\n**Lead.**\n\n<!-- el-end: orch.x -->\n",
            ["range_id_mismatch"],
        ),
    ],
    ids=["end-id-differs", "begin-above-other-el", "begin-above-non-tag"],
)
def test_scan_range_id_mismatch(body: str, kinds: list[str]) -> None:
    assert _scan_kinds({"auto-dev.md": body}) == kinds


@pytest.mark.parametrize(
    "body",
    [
        "```\n<!-- el-begin: orch.x -->\n```\n",
        "```\n<!-- el-end: orch.x -->\n```\n",
        "| a |\n|---|\n<!-- el-begin: orch.x -->\n| b |\n",
        "| a |\n|---|\n<!-- el-end: orch.x -->\n| b |\n",
    ],
    ids=["begin-fence", "end-fence", "begin-table", "end-table"],
)
def test_scan_range_in_fence(body: str) -> None:
    assert _scan_kinds({"auto-dev.md": body}) == ["range_in_fence"]


def test_scan_range_non_strict_is_malformed() -> None:
    docs = {"auto-dev.md": "<!-- el-begin: Bad_Id -->\n**Lead.**\n"}
    assert _scan_kinds(docs) == ["tag_malformed"]


def test_scan_range_duplicate_id() -> None:
    body = _RANGE_OK + "\n" + _RANGE_OK
    kinds = _scan_kinds({"auto-dev.md": body})
    assert "tag_duplicate" in kinds


def test_scan_el_end_is_exempt_from_anchor_rule() -> None:
    body = _RANGE_OK + "\nplain prose after the end\n"
    assert _scan_kinds({"auto-dev.md": body}) == []


def test_scan_el_begin_above_el_tag_skips_mid_paragraph() -> None:
    tail = "<!-- el: orch.x -->\n  **Lead.**\nb\n\n<!-- el-end: orch.x -->\n"
    begin = "<!-- el-begin: orch.x -->\n"
    clean = "Prose above.\n\n" + begin + tail
    assert _scan_kinds({"auto-dev.md": clean}) == []
    glued = "Prose above.\n" + begin + tail
    assert _scan_kinds({"auto-dev.md": glued}) == ["tag_mid_paragraph"]
    bad = begin + "<!-- el: orch.x -->\nplain\n\n<!-- el-end: orch.x -->\n"
    assert _scan_kinds({"auto-dev.md": bad}) == ["tag_bad_anchor"]


def test_scan_ranges_are_per_doc() -> None:
    other = _RANGE_OK.replace("orch.x", "review.x")
    docs = {"auto-dev.md": _RANGE_OK, "auto-dev-review.md": other}
    scan = scan_doc_tags(docs)
    assert scan.violations == ()
    assert [(r.doc, r.element_id) for r in scan.ranges] == [
        ("auto-dev.md", "orch.x"),
        ("auto-dev-review.md", "review.x"),
    ]
    dup = scan_doc_tags({"auto-dev.md": _RANGE_OK, "other.md": _RANGE_OK})
    assert "tag_duplicate" in [v.kind for v in dup.violations]


def test_strip_doc_tags_removes_both_forms() -> None:
    text = (
        "keep\n<!-- el-begin: a.b -->\r\n<!-- el: a.b -->\n**L.**\n\n"
        "<!-- el-end: a.b -->\n<!-- el-end: Bad_Id -->\nend\n"
    )
    assert strip_doc_tags(text) == "keep\n**L.**\n\n<!-- el-end: Bad_Id -->\nend\n"
    assert STRICT_RANGE_TAG.match("<!-- el-begin: a.b -->") is not None
    assert STRICT_RANGE_TAG.match("<!-- el: a.b -->") is None


def test_scan_unknown_scope() -> None:
    scan = scan_doc_tags({"auto-dev.md": _tagged("deploy.x")})
    assert [v.kind for v in scan.violations] == ["unknown_scope"]
    assert scan.tags == ()


def test_scan_tag_wrong_scope() -> None:
    scan = scan_doc_tags({"auto-dev.md": _tagged("review.x")})
    assert [v.kind for v in scan.violations] == ["tag_wrong_scope"]
    assert [t.element_id for t in scan.tags] == ["review.x"]


def test_scan_doc_outside_doc_scopes_has_no_scope_constraint() -> None:
    assert _scan_kinds({"other.md": _tagged("review.x")}) == []


def test_scan_tag_duplicate_within_one_doc() -> None:
    body = _tagged("orch.x") + "\n" + _tagged("orch.x")
    scan = scan_doc_tags({"auto-dev.md": body})
    assert [v.kind for v in scan.violations] == ["tag_duplicate"]
    assert "auto-dev.md:1" in scan.violations[0].detail
    assert len(scan.tags) == 1


def test_scan_tag_duplicate_across_docs() -> None:
    docs = {"auto-dev.md": _tagged("orch.x"), "other.md": _tagged("orch.x")}
    violation = scan_doc_tags(docs).violations[0]
    assert (violation.kind, violation.doc) == ("tag_duplicate", "other.md")


@pytest.mark.parametrize(
    "body",
    [
        "<!-- el: orch.x -->\n\n**Lead.**\n",
        "Intro.\n\n<!-- el: orch.x -->",
        "\n<!-- el: orch.x -->\nplain prose line\n",
        "Prose above.\n<!-- el: orch.x -->\n\n**Lead.**\n",
    ],
    ids=["blank-after", "end-of-file", "prose-anchor", "prose-above-blank-after"],
)
def test_scan_tag_bad_anchor(body: str) -> None:
    assert _scan_kinds({"auto-dev.md": body}) == ["tag_bad_anchor"]


def test_scan_tag_mid_paragraph_between_two_prose_lines() -> None:
    body = "Prose above.\n<!-- el: orch.x -->\nprose below.\n"
    assert _scan_kinds({"auto-dev.md": body}) == ["tag_mid_paragraph"]


def test_scan_tag_mid_paragraph_above_indented_bold_lead() -> None:
    body = "Prose above.\n<!-- el: orch.x -->\n   **Indented (#1).**\n"
    assert _scan_kinds({"auto-dev.md": body}) == ["tag_mid_paragraph"]


@pytest.mark.parametrize(
    "anchor",
    ["- **x**", "* **x**", "1. **x**", "3.6. **x**", "**x**", "## Heading (#1)"],
)
def test_scan_accepts_tag_under_prose_above_block_start(anchor: str) -> None:
    body = _tagged("orch.x", anchor=anchor, above="Prose above.")
    assert _scan_kinds({"auto-dev.md": body}) == []


def test_scan_accepts_tag_after_blank_above_two_level_number() -> None:
    body = "Prose.\n\n" + _tagged("orch.x", anchor="3.6. **Clean (#2787).**")
    assert _scan_kinds({"auto-dev.md": body}) == []


def test_scan_accepts_tag_under_list_item() -> None:
    body = _tagged("orch.x", anchor="- **y**", above="- **x** first item")
    assert _scan_kinds({"auto-dev.md": body}) == []


def test_scan_accepts_tag_above_indented_bold_lead_after_blank() -> None:
    body = "2. **Item.**\n\n" + _tagged("orch.x", anchor="   **Sign-off (#1).**")
    assert _scan_kinds({"auto-dev.md": body}) == []


def test_scan_rejects_tag_above_indented_non_bold_line() -> None:
    body = "2. **Item.**\n\n" + _tagged("orch.x", anchor="   plain indented")
    assert _scan_kinds({"auto-dev.md": body}) == ["tag_bad_anchor"]


def test_scan_prose_check_ignores_fence_and_indented_lines_above() -> None:
    above_fence = "```\ncode\n```\n<!-- el: orch.x -->\nprose\n"
    assert _scan_kinds({"auto-dev.md": above_fence}) == ["tag_bad_anchor"]
    above_indented = "  continuation\n<!-- el: orch.y -->\nprose\n"
    assert _scan_kinds({"auto-dev.md": above_indented}) == ["tag_bad_anchor"]


def test_strip_deletes_whole_tag_lines() -> None:
    text = "a\n<!-- el: orch.x -->\n**b**\n"
    assert strip_doc_tags(text) == "a\n**b**\n"


def test_strip_handles_last_line_without_newline() -> None:
    assert strip_doc_tags("a\n<!-- el: orch.x -->") == "a\n"


def test_strip_is_idempotent_and_keeps_non_strict_lines() -> None:
    text = "a\n <!-- el: orch.x -->\n<!-- el: orch.y -->\r\nb"
    once = strip_doc_tags(text)
    assert once == "a\n <!-- el: orch.x -->\nb"
    assert strip_doc_tags(once) == once


# ---------------------------------------------------------------------------
# Resolver
# ---------------------------------------------------------------------------

_MODULE = """
import x

def func(): ...
async def afunc(): ...
class Klass:
    attr = 1
    def method(self): ...
CONST = 1
TYPED: int = 2
obj.attr = 3
"""


def _no_prompt(_stage: str) -> str:
    return ""


def _code_resolver(tmp_path: Path) -> Resolver:
    repo = _repo(tmp_path, {"pkg/mod.py": _MODULE, "pkg/bad.py": "def (:\n"})
    return make_resolver(repo, _no_prompt)


@pytest.mark.parametrize(
    "symbol", ["func", "afunc", "Klass", "CONST", "TYPED", "Klass.method"]
)
def test_resolver_code_resolves(tmp_path: Path, symbol: str) -> None:
    resolve = _code_resolver(tmp_path)
    assert resolve(CodeRef(kind="code", path="pkg/mod.py", symbol=symbol)) is None


@pytest.mark.parametrize(
    ("path", "symbol", "detail"),
    [
        ("pkg/missing.py", "func", "does not exist"),
        ("pkg/mod.txt", "func", "is not a .py file"),
        ("pkg/bad.py", "func", "does not parse"),
        ("pkg/mod.py", "absent", "not defined"),
        ("pkg/mod.py", "func.inner", "not defined"),
        ("pkg/mod.py", "Klass.absent", "not defined"),
        ("../outside.py", "func", "escapes the repo root"),
        ("/etc/abs.py", "func", "is absolute"),
    ],
    ids=[
        "missing",
        "non-py",
        "syntax",
        "absent",
        "non-class-descent",
        "absent-member",
        "escape",
        "absolute",
    ],
)
def test_resolver_code_error_paths(
    tmp_path: Path, path: str, symbol: str, detail: str
) -> None:
    resolve = _code_resolver(tmp_path / "repo")
    result = resolve(CodeRef(kind="code", path=path, symbol=symbol))
    assert result is not None
    assert detail in result


@pytest.mark.parametrize(
    ("path", "symbol"),
    [
        ("src/cw/opencode_clean_review.py", "clean_review_body"),
        ("src/cw/codex_review_debt.py", "file_codex_deferrals"),
    ],
)
def test_resolver_code_against_the_real_repo(path: str, symbol: str) -> None:
    resolve = make_resolver(_REPO_ROOT, _no_prompt)
    assert resolve(CodeRef(kind="code", path=path, symbol=symbol)) is None


def test_resolver_text(tmp_path: Path) -> None:
    resolve = make_resolver(_repo(tmp_path, {"a.md": "hello world"}), _no_prompt)
    assert resolve(TextRef(kind="text", path="a.md", needle="world")) is None
    absent = resolve(TextRef(kind="text", path="a.md", needle="nope"))
    assert absent is not None
    assert "not found" in absent
    missing = resolve(TextRef(kind="text", path="b.md", needle="x"))
    assert missing is not None
    assert "does not exist" in missing


def test_resolver_doc(tmp_path: Path) -> None:
    resolve = make_resolver(_repo(tmp_path, {"a.md": ""}), _no_prompt)
    assert resolve(DocRef(kind="doc", path="a.md")) is None
    missing = resolve(DocRef(kind="doc", path="b.md"))
    assert missing is not None
    assert "does not exist" in missing


def test_resolver_prompt(tmp_path: Path) -> None:
    calls: list[str] = []

    def prompt_for_stage(stage: str) -> str:
        calls.append(stage)
        if stage == "finalize":
            raise KeyError(stage)
        return f"{stage} prompt with needle"

    resolve = make_resolver(tmp_path, prompt_for_stage)
    assert resolve(PromptRef(kind="prompt", stage="plan", needle="needle")) is None
    absent = resolve(PromptRef(kind="prompt", stage="plan", needle="nope"))
    assert absent is not None
    assert "not in the plan prompt" in absent
    unknown = resolve(PromptRef(kind="prompt", stage="finalize", needle="x"))
    assert unknown is not None
    assert "no prompt for stage" in unknown
    assert calls == ["plan", "finalize"]


def test_resolver_ticket_needs_no_lookup(tmp_path: Path) -> None:
    def boom(stage: str) -> str:
        raise AssertionError(stage)

    resolve = make_resolver(tmp_path / "absent", boom)
    assert resolve(TicketRef(kind="ticket", number=1)) is None


# ---------------------------------------------------------------------------
# Checker kill-checks
# ---------------------------------------------------------------------------


def _cells(raw: dict[str, Any], element_id: str) -> dict[str, Any]:
    cells: dict[str, Any] = raw["elements"][element_id]["cells"]
    return cells


def _mutated(mutate: Callable[[dict[str, Any]], None]) -> Manifest:
    raw = _baseline_dict()
    mutate(raw)
    return _baseline_manifest(raw)


def test_baseline_is_clean() -> None:
    assert _check() == []


def test_dropping_a_tag_is_orphan_element() -> None:
    docs = dict(_BASELINE_DOCS)
    docs["auto-dev-review.md"] = "### Step 3a.5: Engine round (#3039)\n"
    violations = _check(scan=scan_doc_tags(docs))
    assert _kinds(violations) == ["orphan_element"]
    assert violations[0].element_id == "review.engine-round"


def test_dropping_a_cell_is_missing_cell() -> None:
    manifest = _mutated(lambda raw: _cells(raw, "review.engine-round").pop("codex"))
    violations = _check(manifest)
    assert [(v.kind, v.backend) for v in violations] == [("missing_cell", "codex")]


def test_doc_id_without_element_is_missing_cell_per_backend() -> None:
    docs = dict(_BASELINE_DOCS)
    docs["auto-dev.md"] += "\n" + _tagged("orch.new-rule")
    violations = _check(scan=scan_doc_tags(docs))
    assert _kinds(violations) == ["missing_cell"] * 4
    assert {v.backend for v in violations} == set(_BACKENDS)
    assert {(v.element_id, v.doc) for v in violations} == {
        ("orch.new-rule", "auto-dev.md")
    }


def test_extra_backend_key_is_unknown_backend() -> None:
    manifest = _mutated(
        lambda raw: _cells(raw, "orch.status-enum").update(
            aider={"disposition": "not-applicable", "reason": "r"}
        )
    )
    assert _kinds(_check(manifest)) == ["unknown_backend"]


def test_backend_list_drift_is_backend_set_drift() -> None:
    manifest = _mutated(lambda raw: raw.update(backends=["claude-native", "codex"]))
    assert _kinds(_check(manifest)) == ["backend_set_drift"]


def test_todo_cell_is_todo_cell() -> None:
    manifest = _mutated(
        lambda raw: _cells(raw, "orch.status-enum").update(codex="TODO")
    )
    assert _kinds(_check(manifest)) == ["todo_cell"]


@pytest.mark.parametrize(
    "ref",
    [
        None,
        {"kind": "ticket", "number": 1},
        {"kind": "text", "path": "a", "needle": "b"},
    ],
    ids=["no-ref", "ticket", "text"],
)
def test_off_native_implemented_without_code_or_prompt_ref(
    ref: dict[str, Any] | None,
) -> None:
    cell: dict[str, Any] = {"disposition": "implemented"}
    if ref is not None:
        cell["ref"] = ref
    manifest = _mutated(lambda raw: _cells(raw, "orch.status-enum").update(codex=cell))
    assert _kinds(_check(manifest)) == ["implemented_needs_ref"]


def test_native_implemented_without_ref_passes() -> None:
    manifest = _mutated(
        lambda raw: _cells(raw, "review.engine-round").update(
            {"claude-native": {"disposition": "implemented"}}
        )
    )
    assert _check(manifest) == []


@pytest.mark.parametrize(
    ("element_id", "backend"),
    [
        ("orch.status-enum", "codex"),
        ("orch.status-enum", "opencode"),
        ("review.engine-round", "local"),
    ],
    ids=["implemented-code", "implemented-prompt", "fails-closed-code"],
)
def test_failing_resolver_is_ref_unresolved(element_id: str, backend: str) -> None:
    manifest = _baseline_manifest()
    cell = manifest.elements[element_id].cells[backend]
    assert not isinstance(cell, str)
    fail = getattr(cell, "ref", None)
    violations = _check(manifest, resolver=_stub_resolver(fail=fail))
    assert [(v.kind, v.element_id, v.backend, v.detail) for v in violations] == [
        ("ref_unresolved", element_id, backend, "stub failure")
    ]


@pytest.mark.parametrize(
    "cell",
    [
        {"disposition": "fails-closed", "ref": {"kind": "ticket", "number": 3216}},
        {
            "disposition": "fails-closed",
            "guard_pending": True,
            "ref": {"kind": "code", "path": "g.py", "symbol": "guard"},
        },
    ],
    ids=["ticket-without-pending", "code-with-pending"],
)
def test_fails_closed_guard_rule(cell: dict[str, Any]) -> None:
    manifest = _mutated(
        lambda raw: _cells(raw, "review.engine-round").update(opencode=cell)
    )
    assert _kinds(_check(manifest)) == ["fails_closed_needs_guard"]


def test_ticket_zero_is_ticket_invalid() -> None:
    manifest = _mutated(
        lambda raw: _cells(raw, "review.engine-round").update(
            codex={"disposition": "deferred", "ref": {"kind": "ticket", "number": 0}}
        )
    )
    assert _kinds(_check(manifest)) == ["ticket_invalid"]


def test_uncovered_status_and_reason() -> None:
    manifest = _mutated(
        lambda raw: raw["elements"]["orch.status-enum"].update(covers=["shipped"])
    )
    violations = _check(manifest)
    assert _kinds(violations) == ["blocker_reason_not_covered", "status_not_covered"]
    assert all(AUTHORING_RULE in v.detail for v in violations)
    assert all("tag every new contract rule" in v.detail for v in violations)


def test_unknown_covers_entry() -> None:
    manifest = _mutated(
        lambda raw: raw["elements"]["review.engine-round"].update(covers=["bogus"])
    )
    assert _kinds(_check(manifest)) == ["covers_unknown"]


def _with_extra_element(element_id: str) -> Manifest:
    def mutate(raw: dict[str, Any]) -> None:
        raw["elements"][element_id] = copy.deepcopy(
            raw["elements"]["review.engine-round"]
        )

    return _mutated(mutate)


def test_manifest_id_grammar_is_id_malformed() -> None:
    kinds = _kinds(_check(_with_extra_element("Review.Bad Id")))
    assert kinds == ["id_malformed", "orphan_element"]


def test_manifest_id_unknown_scope() -> None:
    kinds = _kinds(_check(_with_extra_element("deploy.thing")))
    assert kinds == ["orphan_element", "unknown_scope"]


def test_scan_violations_pass_through() -> None:
    docs = dict(_BASELINE_DOCS)
    docs["auto-dev.md"] += "```\n<!-- el: orch.fenced -->\n```\n"
    violations = _check(scan=scan_doc_tags(docs))
    assert _kinds(violations) == ["tag_in_fence"]
    assert (violations[0].doc, violations[0].line) == ("auto-dev.md", 6)


def test_output_is_sorted_and_deterministic() -> None:
    def mutate(raw: dict[str, Any]) -> None:
        _cells(raw, "review.engine-round").update(codex="TODO", local="TODO")
        _cells(raw, "orch.status-enum").pop("local")
        raw["elements"]["orch.status-enum"]["covers"] = []

    manifest = _mutated(mutate)
    first = _check(manifest)
    assert first == _check(manifest)
    keys = [
        (v.kind, v.element_id or "", v.backend or "", v.doc or "", v.line or 0)
        for v in first
    ]
    assert keys == sorted(keys)
    assert _kinds(first) == [
        "blocker_reason_not_covered",
        "missing_cell",
        "status_not_covered",
        "status_not_covered",
        "todo_cell",
        "todo_cell",
    ]


def test_contract_violation_is_frozen_and_prints_kind_and_id() -> None:
    violation = ContractViolation(
        kind="todo_cell", element_id="orch.x", backend="codex", detail="d"
    )
    field = "detail"
    with pytest.raises(pydantic.ValidationError):
        setattr(violation, field, "e")
    assert "todo_cell" in str(violation)
    assert "orch.x" in str(violation)
    located = ContractViolation(kind="tag_in_fence", doc="a.md", line=3, detail="d")
    assert "a.md:3" in str(located)


# ---------------------------------------------------------------------------
# Real-data drift tests (the acceptance tests)
# ---------------------------------------------------------------------------

_THIRTEEN_DOCS = (
    "auto-dev.md",
    "auto-dev-intake.md",
    "auto-dev-intake-appendix.md",
    "auto-dev-plan.md",
    "auto-dev-plan-appendix.md",
    "auto-dev-impl.md",
    "auto-dev-impl-appendix.md",
    "auto-dev-review.md",
    "auto-dev-review-appendix.md",
    "auto-dev-finalize.md",
    "auto-dev-finalize-appendix.md",
    "prep-pr.md",
    "ship-it.md",
)

_RFC_0017_LEAD = re.compile(
    r"^(?:#{1,6} .*RFC 0017"  # heading text
    r"|\s*(?:(?:\d+(?:\.\d+)*\.|[-*])\s+)?\*\*[^*]*RFC 0017)"  # first bold run
)
_CLEAN_REVIEW_LEAD = re.compile(
    r"^(?:#{1,6} .*#2787|(?:(?:\d+(?:\.\d+)*\.|[-*])\s+)?\*\*[^*]*#2787)"
)

_RFC_0017_MEMBERS = (
    "review.engine-branch",
    "review.engine-round",
    "review.engine-round-adjudication",
    "review.review-debt-filing",
    "review.fourth-trigger-review-engine-modified-park",
    "review.fifth-trigger-review-confirm-must-fix-park",
    "finalize.gate-review-debt-re-file",
    "finalize.review-sign-off",
)
_REVIEW_DEBT_MEMBERS = (
    "review.review-debt-filing",
    "finalize.gate-review-debt-re-file",
)
#: The members RFC 0018 A4 (#3216) implemented on codex; opencode fails closed
#: on the pre-flight engine guard.
_RFC_0017_A4_MEMBERS = (
    "review.engine-branch",
    "review.engine-round",
    "review.engine-round-adjudication",
    "review.fourth-trigger-review-engine-modified-park",
    "review.fifth-trigger-review-confirm-must-fix-park",
)
#: The finalize members, retargeted to the finalize review-debt follow-up (#3543).
_RFC_0017_FINALIZE_MEMBERS = (
    "finalize.gate-review-debt-re-file",
    "finalize.review-sign-off",
)
_ENGINE_GUARD_REF = CodeRef(
    kind="code",
    path="src/cw/executor/_engine_guard.py",
    symbol="engine_block_details",
)
_CODEX_DEBT_NOTE = "src/cw/codex_review_debt.py::file_codex_deferrals"
#: RFC 0017 members whose codex cell is ``implemented`` (R17 exception route).
#: Empty: the codex review-debt filer is a partial record-time path (R21).
_CODEX_IMPLEMENTED_EXCEPTIONS: dict[str, CodeRef] = {}

#: The opencode clean-review set (R24): member -> pinned token of
#: ``clean_review_body("TKT-1")``. Implemented members must carry the token;
#: exceptions must lack it (B1 adding it should prompt a flip).
_CLEAN_REVIEW_IMPLEMENTED = {
    "review.clean-mode-run-start-guard": "dispatch_refusal",
    "review.clean-mode-head": "converge init",
    "review.clean-mode-only-record-round": "record-round",
    "review.clean-mode-review-fields": "final_round_clean_sha",
}
_CLEAN_REVIEW_EXCEPTIONS = {
    "review.converge-config-error-rule": "convergence_config_error",
    "review.clean-mode-only-cw-review-converge-suppress": "converge suppress",
    "review.clean-mode-confirm-round-and-exits": "history-comment",
    "review.clean-mode-step-3c-additions": "verify-fixes",
}
_CLEAN_BODY_REF = CodeRef(
    kind="code", path="src/cw/opencode_clean_review.py", symbol="clean_review_body"
)

_CODEX_STAGE_TICKETS: dict[str, int] = {}
#: The C3 ticket (#3062, codex FINALIZE executor); no cell may defer to it once
#: it lands. Its finalize-stage cells are implemented,
#: not-applicable, or deferred to one of its three follow-ups.
_C3_TICKET = 3062
_CODEX_FINALIZE_FOLLOW_UPS = frozenset({3542, 3543, 3544})
#: The C1 ticket (#3060, codex PLAN executor); no cell may defer to it once it
#: lands. Its plan-stage cells are implemented, not-applicable,
#: or deferred to one of its two follow-ups.
_C1_TICKET = 3060
_CODEX_PLAN_FOLLOW_UPS = frozenset({3464, 3465})
#: The C2 ticket (#3061, codex IMPL executor); no cell may defer to it once it
#: lands. Its impl-stage cells are implemented, not-applicable,
#: or deferred to one of its three follow-ups.
_C2_TICKET = 3061
_CODEX_IMPL_FOLLOW_UPS = frozenset({3529, 3530, 3531})
# #3055 closed with RFC 0018 S3: its codex review cells moved to #3057 (A2).
_CODEX_REVIEW_TICKETS = frozenset({3318, 2464, 3057})
#: The C4 ticket (#3063, usage accounting); no cell may defer to it once it lands.
_C4_TICKET = 3063
#: The S3 ticket; no cell may defer to it once it lands (#3055).
_S3_TICKET = 3055
#: The A1 ticket; no cell may defer to it once it lands (#3056).
_A1_TICKET = 3056

#: Seeded floor (R4, R5): (id, backend, disposition, ticket or None).
_FINALIZE_GUARD_REF = CodeRef(
    kind="code",
    path="src/cw/executor/_finalize_guard.py",
    symbol="finalize_block_details",
)
#: The 81 FINALIZE-stage opencode cells flip (RFC 0018 B1); a deletion must bite.
_FINALIZE_GUARD_MIN_CELLS = 80

_NAMED_GAP_DISPOSITIONS = (
    ("impl.review-feedback-guard", "opencode", "fails-closed", None),
    ("review.checkpoint-3a-adjudicate-every-finding", "codex", "deferred", 2464),
    ("review.spawn-review-agents", "codex", "deferred", 2464),
    ("review.clean-mode-head", "local", "fails-closed", None),
    ("impl.repair-entry-no-plan", "local", "fails-closed", None),
    ("impl.repair-entry-with-plan", "local", "fails-closed", None),
    ("impl.file-set-is-within-plan-s", "local", "implemented", None),
    ("plan.ambiguity-verification", "local", "deferred", 3219),
    ("review.fix-loop", "local", "deferred", 3219),
    ("finalize.merge-gate-check", "local", "deferred", 3219),
)

#: Every named RFC 0018 gap -> the element that carries it (test 9).
_GAP_SEED_FLOOR = {
    "operator_resolutions": "plan.row-delivered-resolutions-read-first",
    "scope_hint": "impl.resolve-impl-model-from-diff",
    "plan_approved": "plan.headless-plan-approval",
    "must_fix_override": "finalize.must-fix-override-verification",
    "Decided Constraints": "review.decided-constraints-block",
    "pre-flight resolutions marker": "plan.setup-pre-flight-resolution-pre-extraction",
    "plan-spec-reviewed marker": "plan.signoff-marker-check",
    "DEFERRED-REVIEW-FINDINGS": "impl.orchestrator-record",
    "plan_source": "orch.sentinel-plan-source",
    "commits": "review.fix-loop",
    "fork_point_sha": "impl.branch-freshness",
    "post-fix quality gates": "review.fix-loop",
    "non-GitHub tracker comment skip": "orch.tracker-post-seam",
    "usage accounting": "orch.sentinel-structured-output",
}

#: The four R22 sites: a tag whose line above is prose. (doc, anchor lead).
_KNOWN_MID_PARAGRAPH_SITES = (
    ("auto-dev-plan.md", "- **>1 marker comment**"),
    ("auto-dev-plan.md", "- **Headless:** do NOT persist the stale-tiered stamp"),
    ("auto-dev-plan.md", "**THEN** post the same plan as a comment"),
    ("auto-dev-review-appendix.md", "**Informational sub-section (#2008).**"),
)


@functools.cache
def _manifest() -> Manifest:
    """The packaged manifest, parsed once per session (it is frozen)."""
    return load_manifest()


def _raw_docs() -> dict[str, str]:
    return {doc: _cmd(doc, raw=True) for doc in DOC_SCOPES}


def _real_scan() -> DocScan:
    return scan_doc_tags(_raw_docs())


def _prompt_for_stage(tmp_path: Path) -> Callable[[str], str]:
    def prompt(stage: str) -> str:
        return build_stage_prompt(stage, "TKT-1", tmp_path)

    return prompt


def _real_cell(element_id: str, backend: str) -> BackendDisposition:
    cell = _manifest().elements[element_id].cells[backend]
    assert not isinstance(cell, str), f"{element_id}/{backend} is TODO"
    return cell


def _ticket_of(cell: BackendDisposition) -> int | None:
    ref = getattr(cell, "ref", None)
    return ref.number if isinstance(ref, TicketRef) else None


def _derived_members(lead: re.Pattern[str], docs: tuple[str, ...]) -> set[str]:
    derived: set[str] = set()
    for doc in docs:
        lines = _cmd(doc, raw=True).splitlines()
        for index, line in enumerate(lines):
            if lead.match(line) is None:
                continue
            tag = STRICT_TAG.match(lines[index - 1]) if index else None
            assert tag is not None, f"untagged member lead at {doc}:{index + 1}"
            derived.add(tag.group(1))
    return derived


def test_contract_is_clean(tmp_path: Path) -> None:
    violations = check_contract(
        load_manifest(),
        _real_scan(),
        backends=(
            CLAUDE_NATIVE_BACKEND,
            CODEX_BACKEND,
            OPENCODE_BACKEND,
            LOCAL_BACKEND,
        ),
        statuses=get_args(Status),
        blocker_reasons=KNOWN_BLOCKER_REASONS,
        resolver=make_resolver(_REPO_ROOT, _prompt_for_stage(tmp_path)),
    )
    report = "\n".join(str(v) for v in violations)
    assert violations == [], f"{AUTHORING_RULE}:\n{report}"


def test_manifest_has_no_todo_cells() -> None:
    todo = [
        f"{element_id}/{backend}"
        for element_id, element in _manifest().elements.items()
        for backend, cell in element.cells.items()
        if cell == TODO_CELL
    ]
    assert todo == []


def test_doc_set_is_the_thirteen_in_scope_docs() -> None:
    assert tuple(DOC_SCOPES) == _THIRTEEN_DOCS
    for doc in _THIRTEEN_DOCS:
        assert _cmd(doc, raw=True).strip(), doc


def test_backend_set_pinned_to_constants() -> None:
    expected = {CLAUDE_NATIVE_BACKEND, CODEX_BACKEND, OPENCODE_BACKEND, LOCAL_BACKEND}
    manifest = _manifest()
    assert set(manifest.backends) == expected
    assert len(manifest.backends) == len(expected)
    assert NATIVE_BACKEND == CLAUDE_NATIVE_BACKEND
    for element_id, element in manifest.elements.items():
        assert set(element.cells) == expected, element_id


def test_every_status_and_blocker_reason_is_covered() -> None:
    covered = {c for e in _manifest().elements.values() for c in e.covers}
    missing = (set(get_args(Status)) | set(KNOWN_BLOCKER_REASONS)) - covered
    assert not missing, (
        f"tag every new contract rule: add a tagged element and list it in "
        f"covers: {sorted(missing)}"
    )


def test_gate_timeout_sentinel_element() -> None:
    """#3372: the reuse-path gate-timeout sentinel carries the claude-native,
    opencode and local cells of its finalize sibling
    ``finalize.sentinel-automerge-not-armed``. Its codex cell differs (#3062):
    no cw timer bounds a codex FINALIZE gate (ADR-0014), so ``gate_timeout`` is
    never produced there and the cell is not-applicable."""
    elements = _manifest().elements
    element = elements["finalize.sentinel-gate-timeout"]
    sibling = elements["finalize.sentinel-automerge-not-armed"]
    assert list(element.covers) == ["gate_timeout"]
    for backend in set(element.cells) - {CODEX_BACKEND}:
        assert element.cells[backend] == sibling.cells[backend], backend
    codex = element.cells[CODEX_BACKEND]
    assert isinstance(codex, NotApplicableDisposition)
    assert "ADR-0014" in codex.reason
    assert isinstance(sibling.cells[CODEX_BACKEND], ImplementedDisposition)


def test_element_count_floor() -> None:
    assert len(_real_scan().tags) >= 200


def test_rfc_0017_member_set_is_mechanical() -> None:
    assert _derived_members(_RFC_0017_LEAD, _THIRTEEN_DOCS) == set(_RFC_0017_MEMBERS)


def test_rfc_0017_member_cells(tmp_path: Path) -> None:
    assert _CODEX_IMPLEMENTED_EXCEPTIONS == {}
    assert set(_RFC_0017_A4_MEMBERS) | set(_RFC_0017_FINALIZE_MEMBERS) | {
        "review.review-debt-filing"
    } == set(_RFC_0017_MEMBERS)
    resolve = make_resolver(_REPO_ROOT, _prompt_for_stage(tmp_path))
    for element_id in _RFC_0017_A4_MEMBERS:
        codex = _real_cell(element_id, CODEX_BACKEND)
        assert isinstance(codex, ImplementedDisposition), element_id
        assert isinstance(codex.ref, CodeRef), element_id
        assert resolve(codex.ref) is None, element_id
        opencode = _real_cell(element_id, OPENCODE_BACKEND)
        assert isinstance(opencode, FailsClosedDisposition), element_id
        assert not opencode.guard_pending, element_id
        assert opencode.ref == _ENGINE_GUARD_REF, element_id
    for element_id in _RFC_0017_FINALIZE_MEMBERS:
        codex = _real_cell(element_id, CODEX_BACKEND)
        assert isinstance(codex, DeferredDisposition), element_id
        assert codex.ref == TicketRef(kind="ticket", number=3543)
        opencode = _real_cell(element_id, OPENCODE_BACKEND)
        assert isinstance(opencode, FailsClosedDisposition), element_id
        assert opencode.guard_pending
        assert opencode.ref == TicketRef(kind="ticket", number=3543)
    debt = _real_cell("review.review-debt-filing", CODEX_BACKEND)
    assert isinstance(debt, DeferredDisposition)
    assert debt.ref == TicketRef(kind="ticket", number=3216)


def test_engine_park_release_covers_the_unsupported_backend_reason(
    tmp_path: Path,
) -> None:
    """#3216: the refusal reason is covered, fails closed off the engine lanes."""
    element = _manifest().elements["review.engine-park-release"]
    assert tuple(element.covers) == ("review_engine_unsupported_backend",)
    resolve = make_resolver(_REPO_ROOT, _prompt_for_stage(tmp_path))
    codex = _real_cell("review.engine-park-release", CODEX_BACKEND)
    assert isinstance(codex, ImplementedDisposition)
    assert isinstance(codex.ref, CodeRef)
    assert resolve(codex.ref) is None
    for backend in (OPENCODE_BACKEND, LOCAL_BACKEND):
        cell = _real_cell("review.engine-park-release", backend)
        assert isinstance(cell, FailsClosedDisposition), backend
        assert cell.ref == _ENGINE_GUARD_REF, backend
    for element_id in _REVIEW_DEBT_MEMBERS:
        note = _real_cell(element_id, CODEX_BACKEND).note
        assert note is not None
        assert _CODEX_DEBT_NOTE in note


def test_codex_implemented_exceptions_resolve(tmp_path: Path) -> None:
    resolve = make_resolver(_REPO_ROOT, _prompt_for_stage(tmp_path))
    for ref in _CODEX_IMPLEMENTED_EXCEPTIONS.values():
        assert resolve(ref) is None


@pytest.mark.parametrize(
    ("element_id", "backend", "disposition", "ticket"), _NAMED_GAP_DISPOSITIONS
)
def test_named_gap_dispositions(
    element_id: str, backend: str, disposition: str, ticket: int | None
) -> None:
    cell = _real_cell(element_id, backend)
    assert cell.disposition == disposition
    assert _ticket_of(cell) == ticket
    assert element_id in elements_with(backend, disposition)
    if (element_id, backend) == ("impl.review-feedback-guard", OPENCODE_BACKEND):
        assert cell.ref == CodeRef(
            kind="code",
            path="src/cw/executor/_repair_entry.py",
            symbol="repair_entry_block_details",
        )


def test_local_ticket_citations_follow_the_stage_split() -> None:
    for element_id, element in _manifest().elements.items():
        cell = element.cells[LOCAL_BACKEND]
        assert not isinstance(cell, str)
        stage = scope_stage(element_id, LOCAL_BACKEND)
        ticket = _ticket_of(cell)
        if ticket == 3260:
            assert stage == "impl", element_id
        if ticket == 3219:
            assert stage in {"plan", "review", "finalize"}, element_id
        if isinstance(cell, NotApplicableDisposition):
            assert cell.reason == _AIDER_REASON, element_id


def _assert_codex_plan_cell(element_id: str, cell: object) -> int | None:
    """A codex PLAN cell (#3060): a code ref, a reason, or a C1 follow-up."""
    if isinstance(cell, ImplementedDisposition):
        assert isinstance(cell.ref, CodeRef), element_id
        assert cell.ref.path.startswith("src/cw/"), element_id
        return None
    if isinstance(cell, NotApplicableDisposition):
        assert cell.reason.strip(), element_id
        return None
    assert isinstance(cell, DeferredDisposition), element_id
    assert cell.ref.number in _CODEX_PLAN_FOLLOW_UPS, element_id
    return cell.ref.number


def _assert_codex_impl_cell(element_id: str, cell: object) -> int | None:
    """A codex IMPL cell (#3061): a code ref, a reason, or a C2 follow-up."""
    if isinstance(cell, ImplementedDisposition):
        assert isinstance(cell.ref, CodeRef), element_id
        assert cell.ref.path.startswith("src/cw/"), element_id
        return None
    if isinstance(cell, NotApplicableDisposition):
        assert cell.reason.strip(), element_id
        return None
    assert isinstance(cell, DeferredDisposition), element_id
    assert cell.ref.number in _CODEX_IMPL_FOLLOW_UPS, element_id
    return cell.ref.number


def _assert_codex_finalize_cell(element_id: str, cell: object) -> int | None:
    """A codex FINALIZE cell (#3062): a code ref, a reason, or a C3 follow-up."""
    if isinstance(cell, ImplementedDisposition):
        assert isinstance(cell.ref, CodeRef), element_id
        assert cell.ref.path.startswith("src/cw/codex_finalize/"), element_id
        return None
    if isinstance(cell, NotApplicableDisposition):
        assert cell.reason.strip(), element_id
        return None
    assert isinstance(cell, DeferredDisposition), element_id
    assert cell.ref.number in _CODEX_FINALIZE_FOLLOW_UPS, element_id
    return cell.ref.number


def test_codex_cells_follow_the_stage_ticket_map() -> None:
    used: set[int] = set()
    plan_follow_ups: set[int] = set()
    impl_follow_ups: set[int] = set()
    finalize_follow_ups: set[int] = set()
    for element_id, element in _manifest().elements.items():
        if element_id == "review.review-debt-filing":
            continue
        cell = element.cells[CODEX_BACKEND]
        assert not isinstance(cell, str)
        stage = scope_stage(element_id, CODEX_BACKEND)
        if stage == "plan":
            follow_up = _assert_codex_plan_cell(element_id, cell)
            if follow_up is not None:
                plan_follow_ups.add(follow_up)
        elif stage == "impl":
            follow_up = _assert_codex_impl_cell(element_id, cell)
            if follow_up is not None:
                impl_follow_ups.add(follow_up)
        elif stage == "finalize":
            follow_up = _assert_codex_finalize_cell(element_id, cell)
            if follow_up is not None:
                finalize_follow_ups.add(follow_up)
        elif stage in _CODEX_STAGE_TICKETS:
            assert isinstance(cell, DeferredDisposition), element_id
            assert cell.ref.number == _CODEX_STAGE_TICKETS[stage], element_id
            used.add(cell.ref.number)
        elif isinstance(cell, ImplementedDisposition):
            assert isinstance(cell.ref, CodeRef), element_id
        else:
            assert isinstance(cell, DeferredDisposition), element_id
            assert cell.ref.number in _CODEX_REVIEW_TICKETS, element_id
    assert used == set(_CODEX_STAGE_TICKETS.values())
    assert plan_follow_ups == _CODEX_PLAN_FOLLOW_UPS
    assert impl_follow_ups == _CODEX_IMPL_FOLLOW_UPS
    assert finalize_follow_ups == _CODEX_FINALIZE_FOLLOW_UPS


#: The codex IMPL cells that point at a ``cw.codex_impl`` symbol (#3061).
_C2_CODE_CELLS = {
    "impl.review-feedback-guard": ("_feedback.py", "verify_review_feedback"),
    "impl.repair-entry-no-plan": ("_gates.py", "run_scope_conformance"),
    "impl.repair-entry-with-plan": ("_prompt.py", "render_impl_prompt"),
    "impl.stage-2-completion": ("_result.py", "stamp_impl_result"),
    "impl.marker-s2-completion": ("_result.py", "impl_commit_message"),
    "impl.emit-through-cw-then-frame": ("_result.py", "parse_impl_turn"),
    "impl.guard-script-path-resolution-and-staleness": (
        "_gates.py",
        "run_scope_conformance",
    ),
    "impl.gate-orchestrator-completion": ("_gates.py", "run_quality_gates"),
    "impl.file-set-is-within-plan-s": ("_gates.py", "run_scope_conformance"),
    "impl.gate-1-why-empty-diff-check": ("_gates.py", "check_empty_diff"),
    "orch.post-impl-scope-conformance-threshold": (
        "_gates.py",
        "run_scope_conformance",
    ),
}


@pytest.mark.parametrize("element_id", sorted(_C2_CODE_CELLS))
def test_codex_impl_code_cells_resolve_to_the_package(element_id: str) -> None:
    cell = _real_cell(element_id, CODEX_BACKEND)
    assert isinstance(cell, ImplementedDisposition)
    filename, symbol = _C2_CODE_CELLS[element_id]
    assert cell.ref == CodeRef(
        kind="code", path=f"src/cw/codex_impl/{filename}", symbol=symbol
    )


_C2_CW_RUN_GATES = (
    "impl.dispatch-detection",
    "impl.worktree-isolation-guard",
    "impl.absent-owner-record",
    "impl.implementation-failure-escalation",
)


@pytest.mark.parametrize("element_id", _C2_CW_RUN_GATES)
def test_codex_impl_cw_run_ranged_blocks_are_not_applicable(element_id: str) -> None:
    assert isinstance(_real_cell(element_id, CODEX_BACKEND), NotApplicableDisposition)


def test_no_cell_defers_to_the_landed_c2_ticket() -> None:
    for element_id, element in _manifest().elements.items():
        for backend, cell in element.cells.items():
            if isinstance(cell, DeferredDisposition):
                assert cell.ref.number != _C2_TICKET, (element_id, backend)


def test_no_cell_defers_to_the_landed_c3_ticket() -> None:
    for element_id, element in _manifest().elements.items():
        for backend, cell in element.cells.items():
            if isinstance(cell, DeferredDisposition):
                assert cell.ref.number != _C3_TICKET, (element_id, backend)


def test_no_cell_defers_to_the_landed_c4_ticket() -> None:
    for element_id, element in _manifest().elements.items():
        for backend, cell in element.cells.items():
            if isinstance(cell, DeferredDisposition):
                assert cell.ref.number != _C4_TICKET, (element_id, backend)


def test_codex_usage_accounting_cell_is_implemented_with_a_code_ref() -> None:
    cell = _manifest().elements["orch.sentinel-structured-output"].cells["codex"]

    assert isinstance(cell, ImplementedDisposition)
    assert cell.ref == CodeRef(
        kind="code", path="src/cw/codex_usage.py", symbol="UsageAccumulator"
    )


#: The codex FINALIZE implemented cells that point at a
#: ``cw.codex_finalize`` symbol (#3062): element id -> (file, symbol).
_C3_CODE_CELLS = {
    "finalize.claude-review-verdict-md-if-present-is-this": (
        "_gates.py",
        "run_must_fix_override",
    ),
    "finalize.resolve-carried-through-context-before-emitting": (
        "_gates.py",
        "resolve_tier",
    ),
    "finalize.must-fix-override-verification": ("_gates.py", "run_must_fix_override"),
    "finalize.pre-stage-detector-guard": ("_pr.py", "detect_branch_pr"),
    "finalize.large-tier-review-approval-check": ("_gates.py", "run_approval_fence"),
    "finalize.clean-mode-final-tip-check": ("_gates.py", "run_approval_fence"),
    "finalize.what-it-runs": ("_gates.py", "run_approval_fence"),
    "finalize.on-failure": ("_gates.py", "run_approval_fence"),
    "finalize.merge-gate-disabled-merge-gate-disabled-by-client-config": (
        "_merge_gate.py",
        "run_merge_gate",
    ),
    "finalize.unavailability-classifier": ("_result.py", "classify_unavailability"),
    "finalize.self-heal-arm-only-when-automerge-enabled": ("_arm.py", "arm"),
    "finalize.post-push-mergeability-verification": ("_pr.py", "check_mergeability"),
    "finalize.reuse-path-quality-gates": ("_gates.py", "run_finalize_quality_gates"),
    "finalize.scope": ("_run.py", "run_finalize_stage_and_complete"),
    "finalize.failure-headless": ("_result.py", "finalize_blocked"),
    "finalize.post-to-linear": ("_post.py", "post_pr_link_comment"),
    "finalize.stage-4-5-completion": ("_run.py", "run_finalize_stage_and_complete"),
    "finalize.emit-through-cw-then-frame": ("_result.py", "finalize_shipped"),
    "finalize.merge-gate-check": ("_merge_gate.py", "run_merge_gate"),
    "finalize.adopted-pr-inventory-fails": ("_merge_gate.py", "run_merge_gate"),
    "finalize.absent-from-both-locations": ("_merge_gate.py", "run_merge_gate"),
    "finalize.compose-pr-title-in-parent": ("_result.py", "compose_pr_title"),
    "finalize.main-session-re-verification": ("_arm.py", "verify_pr_state"),
    "finalize.enable-auto-merge": ("_arm.py", "arm"),
    "finalize.sentinel-merge-conflict-post-push": ("_pr.py", "probe_default"),
    "finalize.unavailability-classifier-signature-table-sentinel": (
        "_result.py",
        "classify_unavailability",
    ),
    "finalize.cw-side-classification": ("_result.py", "classify_unavailability"),
    "finalize.push-sites-covered": ("_pr.py", "push_ahead"),
    "finalize.sentinel-automerge-not-armed": ("_arm.py", "arm"),
    "finalize.reuse-path-gate-failure-agent-block-sentinel": (
        "_gates.py",
        "run_finalize_quality_gates",
    ),
}


def test_codex_finalize_code_cell_table_is_exactly_the_code_cells() -> None:
    """The 30 implemented codex finalize code cells, no others."""
    code_cells = {
        element_id
        for element_id, element in _manifest().elements.items()
        if scope_stage(element_id, CODEX_BACKEND) == "finalize"
        and isinstance(
            element.cells[CODEX_BACKEND],
            ImplementedDisposition,
        )
    }
    assert code_cells == set(_C3_CODE_CELLS)
    assert len(_C3_CODE_CELLS) == 30
    implemented = set(elements_with(CODEX_BACKEND, "implemented"))
    assert len(implemented & set(_C3_CODE_CELLS)) == 30


@pytest.mark.parametrize("element_id", sorted(_C3_CODE_CELLS))
def test_codex_finalize_code_cells_resolve_to_the_package(
    element_id: str, tmp_path: Path
) -> None:
    cell = _real_cell(element_id, CODEX_BACKEND)
    assert isinstance(cell, ImplementedDisposition)
    filename, symbol = _C3_CODE_CELLS[element_id]
    assert cell.ref == CodeRef(
        kind="code", path=f"src/cw/codex_finalize/{filename}", symbol=symbol
    )
    resolve = make_resolver(_REPO_ROOT, _prompt_for_stage(tmp_path))
    assert resolve(cell.ref) is None


def test_codex_finalize_follow_up_cells_cover_the_three_tickets() -> None:
    by_ticket: dict[int, int] = {}
    for element_id, element in _manifest().elements.items():
        cell = element.cells[CODEX_BACKEND]
        if scope_stage(element_id, CODEX_BACKEND) != "finalize":
            continue
        if isinstance(cell, DeferredDisposition):
            by_ticket[cell.ref.number] = by_ticket.get(cell.ref.number, 0) + 1
    assert set(by_ticket) == _CODEX_FINALIZE_FOLLOW_UPS
    assert sum(by_ticket.values()) == 24


def test_codex_prep_pr_and_ship_it_cells_are_not_applicable() -> None:
    ids = [
        element_id
        for element_id in _manifest().elements
        if element_id.startswith(("prep-pr.", "ship-it."))
    ]
    assert len([i for i in ids if i.startswith("prep-pr.")]) == 12
    assert len([i for i in ids if i.startswith("ship-it.")]) == 8
    for element_id in ids:
        cell = _real_cell(element_id, CODEX_BACKEND)
        assert isinstance(cell, NotApplicableDisposition), element_id
        assert "cw does this in Python" in cell.reason, element_id
    gates = _real_cell("prep-pr.run-quality-gates", CODEX_BACKEND)
    assert isinstance(gates, NotApplicableDisposition)
    assert "codex_finalize/_gates.py::run_finalize_quality_gates" in gates.reason


def test_no_codex_cell_is_fails_closed() -> None:
    """RFC 0018 D1 (#3064): codex has no stage-level refusal left, so no codex
    cell is fails-closed. Every former fails-closed codex cell is implemented
    with the cw-run gate that carries it."""
    assert elements_with(CODEX_BACKEND, "fails-closed") == ()


def test_codex_finalize_cell_counts() -> None:
    counts: dict[str, int] = {}
    for element_id, element in _manifest().elements.items():
        if element_id.split(".")[0] in {"finalize", "prep-pr", "ship-it"}:
            cell = element.cells[CODEX_BACKEND]
            counts[cell.disposition] = counts.get(cell.disposition, 0) + 1
    assert counts == {
        "implemented": 30,
        "not-applicable": 32,
        "deferred": 24,
    }


def test_no_cell_defers_to_the_landed_c1_ticket() -> None:
    for element_id, element in _manifest().elements.items():
        for backend, cell in element.cells.items():
            if isinstance(cell, DeferredDisposition):
                assert cell.ref.number != _C1_TICKET, (element_id, backend)


#: The codex cells A2 (#3057) implements with the tracker-IO turn.
_A2_TRACKER_IO_CELLS = (
    "orch.tracker-post-seam",
    "orch.comment-provenance-rule",
    "orch.marker-cw-agent-authored",
    "review.blocking-findings-comment-rule",
    "review.blocking-findings-comment-rule-body-shape",
)


@pytest.mark.parametrize("element_id", _A2_TRACKER_IO_CELLS)
def test_a2_cells_point_at_the_tracker_io_turn(element_id: str) -> None:
    cell = _real_cell(element_id, CODEX_BACKEND)
    assert isinstance(cell, ImplementedDisposition), element_id
    assert cell.ref == CodeRef(
        kind="code", path="src/cw/tracker_io/_turn.py", symbol="post_parts_via_turn"
    )


def test_no_cell_defers_to_the_closing_ticket() -> None:
    """#3055 and #3056 resolved every cell that cited them; none may again."""
    for element_id, element in _manifest().elements.items():
        for backend in _BACKENDS:
            cell = element.cells[backend]
            assert not isinstance(cell, str), (element_id, backend)
            for ticket in (_S3_TICKET, _A1_TICKET):
                assert _ticket_of(cell) != ticket, (element_id, backend)


def test_scope_stage_maps_scopes_and_orch_governs() -> None:
    assert scope_stage("intake.x", CODEX_BACKEND) == "plan"
    assert scope_stage("prep-pr.x", LOCAL_BACKEND) == "finalize"
    assert scope_stage("ship-it.x", CODEX_BACKEND) == "finalize"
    for element_id, stage in ORCH_GOVERNS.items():
        for backend in _BACKENDS:
            assert scope_stage(element_id, backend) == stage
    assert scope_stage("orch.unlisted", CODEX_BACKEND) == "review"
    assert scope_stage("orch.unlisted", LOCAL_BACKEND) == "impl"
    assert scope_stage("orch.unlisted", OPENCODE_BACKEND) == "impl"


def test_stages_match_the_stage_enum_values() -> None:
    assert (
        tuple(Stage[name].value for name in ("PLAN", "IMPL", "REVIEW", "FINALIZE"))
        == STAGES
    )


def test_orch_governs_table_is_complete() -> None:
    orch_ids = {i for i in _manifest().elements if i.startswith("orch.")}
    assert set(ORCH_GOVERNS) <= orch_ids
    assert set(ORCH_GOVERNS.values()) <= set(STAGES)


def test_opencode_clean_review_member_set_is_mechanical() -> None:
    derived = _derived_members(
        _CLEAN_REVIEW_LEAD, ("auto-dev-review.md", "auto-dev-review-appendix.md")
    )
    assert derived == set(_CLEAN_REVIEW_IMPLEMENTED) | set(_CLEAN_REVIEW_EXCEPTIONS)


def test_opencode_clean_review_cells_track_clean_review_body() -> None:
    body = clean_review_body("TKT-1")
    for element_id, token in _CLEAN_REVIEW_IMPLEMENTED.items():
        cell = _real_cell(element_id, OPENCODE_BACKEND)
        assert isinstance(cell, ImplementedDisposition), element_id
        assert cell.ref == _CLEAN_BODY_REF
        assert token in body, element_id
    for element_id, token in _CLEAN_REVIEW_EXCEPTIONS.items():
        cell = _real_cell(element_id, OPENCODE_BACKEND)
        assert isinstance(cell, DeferredDisposition), element_id
        assert cell.ref.number == 3059
        assert cell.note is not None
        assert token in cell.note, element_id
        assert token not in body, f"{element_id}: B1 landed {token!r}; flip it"
    guard = CodeRef(
        kind="code",
        path="src/cw/executor/_clean_review.py",
        symbol="clean_review_block_details",
    )
    for element_id in (*_CLEAN_REVIEW_IMPLEMENTED, *_CLEAN_REVIEW_EXCEPTIONS):
        cell = _real_cell(element_id, LOCAL_BACKEND)
        assert isinstance(cell, FailsClosedDisposition), element_id
        assert cell.ref == guard


@pytest.mark.parametrize(("gap", "element_id"), sorted(_GAP_SEED_FLOOR.items()))
def test_gap_seed_floor_has_a_deliberate_cell(gap: str, element_id: str) -> None:
    for backend in (OPENCODE_BACKEND, CODEX_BACKEND):
        cell = _manifest().elements[element_id].cells[backend]
        assert cell != TODO_CELL, f"{gap}: {element_id}/{backend}"


def _is_strict_tag_line(line: str) -> bool:
    return bool(STRICT_TAG.match(line) or STRICT_RANGE_TAG.match(line))


@pytest.mark.parametrize("doc", _THIRTEEN_DOCS)
def test_strip_removes_exactly_the_tag_lines(doc: str) -> None:
    raw = _cmd(doc, raw=True)
    stripped = strip_doc_tags(raw)
    assert _cmd(doc) == stripped
    raw_lines = raw.splitlines()
    tag_lines = [line for line in raw_lines if _is_strict_tag_line(line)]
    kept = [line for line in raw_lines if not _is_strict_tag_line(line)]
    assert kept == stripped.splitlines()
    assert len(raw_lines) - len(stripped.splitlines()) == len(tag_lines)
    assert tag_lines


def _has_tag_line(text: str) -> bool:
    return any(TAG_SHAPED.match(line) for line in text.splitlines())


@pytest.mark.parametrize("doc", _THIRTEEN_DOCS)
def test_conftest_readers_are_tag_blind(doc: str) -> None:
    assert not _has_tag_line(_cmd(doc))
    assert _has_tag_line(_cmd(doc, raw=True))


@pytest.mark.parametrize("stage", ["intake", "plan", "impl", "review", "finalize"])
def test_conftest_appendix_reader_is_tag_blind(stage: str) -> None:
    assert not _has_tag_line(_appendix(stage))
    assert _has_tag_line(_appendix(stage, raw=True))


def test_authoring_rule_documented() -> None:
    contract = (_REPO_ROOT / "docs" / "headless-contract.md").read_text("utf-8")
    section = _section_between(contract, "### 4.5 Contract element manifest", "## 5")
    assert "tag every new contract rule" in section
    assert "src/cw/data/contract_manifest.yaml" in section
    assert "<!-- el: <scope>.<slug> -->" in section
    assert "CW_UPDATE_GOLDEN=1" in section
    assert "<!-- el-begin: <scope>.<slug> -->" in section
    assert "<!-- el-end: <scope>.<slug> -->" in section


def test_packaged_manifest_resource_exists() -> None:
    resource = importlib.resources.files("cw").joinpath("data/contract_manifest.yaml")
    assert resource.is_file()


def test_engine_summary_is_not_tagged() -> None:
    assert not [t for t in _real_scan().tags if "engine-summary" in t.element_id]


def test_elements_with_over_the_packaged_manifest() -> None:
    ids = elements_with(LOCAL_BACKEND, "fails-closed")
    assert list(ids) == sorted(ids)
    assert "review.clean-mode-head" in ids
    assert elements_with("aider", "implemented") == ()
    with pytest.raises(ValueError, match="unknown disposition"):
        elements_with(CODEX_BACKEND, "maybe")


@pytest.mark.parametrize(("doc", "lead"), _KNOWN_MID_PARAGRAPH_SITES)
def test_known_mid_paragraph_sites_scan_clean(doc: str, lead: str) -> None:
    lines = _cmd(doc, raw=True).splitlines()
    hits = [i for i, line in enumerate(lines) if line.startswith(lead)]
    assert len(hits) == 1, (doc, lead)
    anchor = hits[0]
    tag = STRICT_TAG.match(lines[anchor - 1])
    assert tag is not None
    above = lines[anchor - 2]
    assert above.strip()
    assert not above[0].isspace()
    assert not above.startswith(("#", "- ", "* "))
    scan = scan_doc_tags({doc: "\n".join(lines)})
    tag_line = anchor  # 1-based line of the tag
    assert not [v for v in scan.violations if v.line == tag_line]


def test_opencode_finalize_stage_cells_fail_closed_on_the_guard() -> None:
    checked = 0
    for element_id, element in _manifest().elements.items():
        if scope_stage(element_id, OPENCODE_BACKEND) != "finalize":
            continue
        if element_id in _RFC_0017_FINALIZE_MEMBERS:
            continue
        cell = element.cells[OPENCODE_BACKEND]
        assert not isinstance(cell, str), element_id
        if isinstance(cell, NotApplicableDisposition):
            continue
        assert isinstance(cell, FailsClosedDisposition), element_id
        assert cell.ref == _FINALIZE_GUARD_REF, element_id
        assert not cell.guard_pending, element_id
        checked += 1
    assert checked >= _FINALIZE_GUARD_MIN_CELLS


def test_opencode_deferred_cells_carry_a_note() -> None:
    for element_id, element in _manifest().elements.items():
        cell = element.cells[OPENCODE_BACKEND]
        assert not isinstance(cell, str), element_id
        if isinstance(cell, DeferredDisposition):
            assert cell.note, element_id


def test_plan_signoff_marker_cell_records_the_marker_decision() -> None:
    cell = _real_cell("plan.signoff-marker-check", OPENCODE_BACKEND)
    assert isinstance(cell, DeferredDisposition)
    assert cell.note is not None
    assert "cw-opencode-plan" in cell.note


def test_opencode_has_no_implemented_cell_without_a_prompt_or_code_ref(
    tmp_path: Path,
) -> None:
    resolve = make_resolver(_REPO_ROOT, _prompt_for_stage(tmp_path))
    for element_id, element in _manifest().elements.items():
        cell = element.cells[OPENCODE_BACKEND]
        assert not isinstance(cell, str), element_id
        if not isinstance(cell, ImplementedDisposition):
            continue
        assert isinstance(cell.ref, (PromptRef, CodeRef)), element_id
        assert resolve(cell.ref) is None, element_id
