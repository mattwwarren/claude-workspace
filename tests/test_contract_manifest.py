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
from typing import TYPE_CHECKING, Any

import pydantic
import pytest
import yaml

from cw.contract_manifest import (
    AUTHORING_RULE,
    CodeRef,
    ContractViolation,
    DocRef,
    DocScan,
    Manifest,
    ManifestLoadError,
    PromptRef,
    TextRef,
    TicketRef,
    check_contract,
    make_resolver,
    parse_manifest,
    scan_doc_tags,
    strip_doc_tags,
)
from cw.contract_manifest import _models as models_module

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping
    from pathlib import Path

    from cw.contract_manifest import ElementRef, Resolver

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


@pytest.mark.parametrize("form", ["el-begin", "el-end"])
def test_scan_tag_reserved_range(form: str) -> None:
    docs = {"auto-dev.md": f"<!-- {form}: orch.x -->\n**Lead.**\n"}
    assert _scan_kinds(docs) == ["tag_reserved_range"]


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
    from tests.conftest import _REPO_ROOT

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
