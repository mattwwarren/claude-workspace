"""Tests for ``cw.contract_manifest._render`` (RFC 0018 A3, #3058).

Regenerate the goldens after an intended change to a ranged doc block or to
the glue::

    CW_UPDATE_GOLDEN=1 uv run pytest tests/test_contract_render.py
"""

from __future__ import annotations

import ast
import os
import re
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from cw import contract_manifest
from cw.contract_manifest import (
    CLAUDE_ONLY_DENYLIST,
    ORCH_GOVERNS,
    STAGES,
    STRICT_RANGE_TAG,
    TAG_SHAPED,
    DeferredDisposition,
    Manifest,
    NeutralBlock,
    NotApplicableDisposition,
    RenderError,
    UnsupportedBackendError,
    UnsupportedStageError,
    load_manifest,
    needed_element_ids,
    render_stage_prompt,
    scan_doc_tags,
    scope_stage,
    stage_blocks,
    strip_doc_tags,
)
from cw.contract_manifest import _render as render_module
from cw.models.orchestrator_config.constants import CODEX_BACKEND
from cw.opencode_runner import (
    _IMPL_SENTINEL_TEMPLATE,
    _PLAN_SENTINEL_TEMPLATE,
    _sentinel_rules,
    _stage_preamble,
)
from tests.conftest import _COMMANDS_ROOT, _cmd

TICKET_PLACEHOLDER = render_module.TICKET_PLACEHOLDER

if TYPE_CHECKING:
    from collections.abc import Mapping

_FIXTURES = Path(__file__).resolve().parent / "fixtures" / "contract_render"
_STAGES = ("plan", "impl", "finalize")
_FIVE_DOCS = (
    "auto-dev-plan.md",
    "auto-dev-impl.md",
    "auto-dev-finalize.md",
    "prep-pr.md",
    "ship-it.md",
)
_RANGE_COUNTS = {
    "auto-dev-plan.md": 31,
    "auto-dev-impl.md": 13,
    "auto-dev-finalize.md": 35,
    "prep-pr.md": 5,
    "ship-it.md": 7,
}
_EXPECTED_DENYLIST = (
    "Agent tool",
    "Skill tool",
    "Skill(",
    "AskUserQuestion",
    "$CW_SESSION",
    "CW_SESSION",
    "Task tool",
    "SendMessage",
    "run_in_background",
    "subagent_type",
    "Stop hook",
    "claude --bg",
    "Monitor tool",
    "TodoWrite",
)
_REGEN = "regenerate: CW_UPDATE_GOLDEN=1 uv run pytest tests/test_contract_render.py"
_SHIP_IT_STEP = "## Step 3: Create the PR"
_PROJECT_MARKER = "PROJECT-SHIP-IT-MARKER"
_PROBE_PATHS = (
    ".claude/commands/ship-it.md",
    ".claude/skills/ship-it/SKILL.md",
    ".agents/skills/ship-it/SKILL.md",
)
#: Phrases the ship-it fallback glue and raw ``prep-pr.md`` Step 8 must share.
_SHIP_IT_PINS = (
    "no project /ship-it",
    "agent_block",
    "never reaches the project ship-it",
    "never fill in a value the ship-it copies from a gate that did not run",
    "one ship-it, not two",
    "prefer the command",
    "different files",
    "run both",
    "Ship Summary",
)


# ---------------------------------------------------------------------------
# Test-local helpers
# ---------------------------------------------------------------------------


def _raw(doc: str) -> str:
    return _cmd(doc, raw=True)


def _docs_copy(
    tmp_path: Path, overrides: Mapping[str, str | None] | None = None
) -> Path:
    """Copy the five R6 docs under *tmp_path*; an override replaces or (None) drops."""
    root = tmp_path / "docs_root"
    root.mkdir()
    changes = dict(overrides or {})
    for doc in _FIVE_DOCS:
        text = changes.pop(doc, _raw(doc))
        if text is not None:
            (root / doc).write_bytes(text.encode("utf-8"))
    assert not changes, f"unknown override docs: {sorted(changes)}"
    return root


def _assert_golden(stage: str, text: str) -> None:
    path = _FIXTURES / f"{stage}-codex.txt"
    if os.environ.get("CW_UPDATE_GOLDEN") == "1":
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8", newline="")
    assert path.is_file(), _REGEN
    assert path.read_text(encoding="utf-8") == text, _REGEN


def _render(
    stage: str, *, ticket_id: str = TICKET_PLACEHOLDER, session_id: str | None = None
) -> str:
    return render_stage_prompt(
        stage,
        CODEX_BACKEND,
        docs_root=_COMMANDS_ROOT,
        ticket_id=ticket_id,
        session_id=session_id,
    )


def _manifest_variant(**cells: dict[str, object]) -> Manifest:
    """A copy of the packaged manifest with the codex cell of each id replaced.

    A cell of ``{}`` removes the element.
    """
    raw = load_manifest().model_dump(mode="json")
    for element_id, cell in cells.items():
        if cell:
            raw["elements"][element_id]["cells"][CODEX_BACKEND] = cell
        else:
            del raw["elements"][element_id]
    return Manifest.model_validate(raw)


def _normalized(text: str) -> str:
    return " ".join(text.split())


def _inject(doc: str, element_id: str, token: str) -> str:
    """*doc* with *token* appended to the anchor line of *element_id*."""
    lines = _raw(doc).split("\n")
    index = lines.index(f"<!-- el: {element_id} -->") + 1
    lines[index] = f"{lines[index]} {token}"
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Refusals and re-exports
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("backend", ["claude-native", "local", "opencode", "aider", ""])
def test_unsupported_backend_raises(backend: str) -> None:
    with pytest.raises(UnsupportedBackendError) as info:
        render_stage_prompt("plan", backend, docs_root=_COMMANDS_ROOT)
    assert isinstance(info.value, RenderError)
    assert isinstance(info.value, ValueError)
    with pytest.raises(UnsupportedBackendError):
        stage_blocks("plan", backend, docs_root=_COMMANDS_ROOT)


@pytest.mark.parametrize("stage", ["review", "intake", "harden", "bogus"])
def test_unsupported_stage_raises(stage: str) -> None:
    with pytest.raises(UnsupportedStageError) as info:
        render_stage_prompt(stage, CODEX_BACKEND, docs_root=_COMMANDS_ROOT)
    assert isinstance(info.value, RenderError)
    with pytest.raises(UnsupportedStageError):
        stage_blocks(stage, CODEX_BACKEND, docs_root=_COMMANDS_ROOT)


def test_backend_is_checked_before_stage() -> None:
    with pytest.raises(UnsupportedBackendError):
        render_stage_prompt("review", "opencode", docs_root=_COMMANDS_ROOT)


def test_render_constants() -> None:
    assert render_module.RENDER_BACKENDS == (CODEX_BACKEND,)
    assert render_module.RENDER_STAGES == _STAGES


def test_public_names_are_re_exported() -> None:
    for name in (
        "STRICT_RANGE_TAG",
        "DocRange",
        "scope_stage",
        "STAGES",
        "ORCH_GOVERNS",
        "render_stage_prompt",
        "stage_blocks",
        "needed_element_ids",
        "NeutralBlock",
        "CLAUDE_ONLY_DENYLIST",
        "RenderError",
        "UnsupportedBackendError",
        "UnsupportedStageError",
    ):
        assert name in contract_manifest.__all__, name
        assert hasattr(contract_manifest, name), name
    assert len(set(contract_manifest.__all__)) == len(contract_manifest.__all__)
    assert ORCH_GOVERNS
    assert STAGES


# ---------------------------------------------------------------------------
# Denylist
# ---------------------------------------------------------------------------


def test_denylist_constant_is_the_r5_tuple() -> None:
    assert CLAUDE_ONLY_DENYLIST == _EXPECTED_DENYLIST


@pytest.mark.parametrize("token", _EXPECTED_DENYLIST)
@pytest.mark.parametrize("stage", _STAGES)
def test_goldens_are_denylist_clean(stage: str, token: str) -> None:
    assert token not in _render(stage)


@pytest.mark.parametrize("token", _EXPECTED_DENYLIST)
def test_denylist_token_in_a_range_raises(tmp_path: Path, token: str) -> None:
    root = _docs_copy(
        tmp_path,
        {"auto-dev-plan.md": _inject("auto-dev-plan.md", "plan.checkpoint-1", token)},
    )
    with pytest.raises(RenderError, match=re.escape(token)):
        render_stage_prompt("plan", CODEX_BACKEND, docs_root=root)


def test_glue_and_imported_fragments_are_denylist_clean() -> None:
    glue = (
        render_module._CONTRACT_INTRO,
        render_module._SHIP_IT_FALLBACK,
        render_module._FINALIZE_SENTINEL_NOTE,
        _stage_preamble("plan", "T-1"),
        _sentinel_rules(None),
        _sentinel_rules("abc"),
        _PLAN_SENTINEL_TEMPLATE,
        _IMPL_SENTINEL_TEMPLATE,
    )
    for text in glue:
        for token in CLAUDE_ONLY_DENYLIST:
            assert token not in text, token


def test_every_range_body_is_denylist_clean() -> None:
    for doc in _FIVE_DOCS:
        raw = _raw(doc)
        scan = scan_doc_tags({doc: raw})
        lines = raw.split("\n")
        for rng in scan.ranges:
            body = "\n".join(lines[rng.begin_line : rng.end_line - 1])
            for token in CLAUDE_ONLY_DENYLIST:
                assert token not in body, (rng.element_id, token)


# ---------------------------------------------------------------------------
# Determinism
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("stage", _STAGES)
def test_render_is_deterministic(stage: str) -> None:
    first = _render(stage)
    assert first == _render(stage)
    assert first.endswith("\n")
    assert not first.endswith("\n\n")


@pytest.mark.parametrize("stage", _STAGES)
def test_render_ignores_line_endings_and_docs_root(tmp_path: Path, stage: str) -> None:
    crlf = {doc: _raw(doc).replace("\n", "\r\n") for doc in _FIVE_DOCS}
    root = _docs_copy(tmp_path, crlf)
    rendered = render_stage_prompt(stage, CODEX_BACKEND, docs_root=root)
    assert rendered == _render(stage)
    assert "\r" not in rendered
    assert str(tmp_path) not in rendered
    assert str(_COMMANDS_ROOT) not in rendered


def test_lone_cr_line_endings_are_normalised(tmp_path: Path) -> None:
    cr = {doc: _raw(doc).replace("\n", "\r") for doc in _FIVE_DOCS}
    root = _docs_copy(tmp_path, cr)
    rendered = render_stage_prompt("impl", CODEX_BACKEND, docs_root=root)
    assert rendered == _render("impl")


# ---------------------------------------------------------------------------
# Needed set (R7)
# ---------------------------------------------------------------------------


def _expected_needed(manifest: Manifest, stage: str) -> tuple[str, ...]:
    return tuple(
        sorted(
            element_id
            for element_id, element in manifest.elements.items()
            if scope_stage(element_id, CODEX_BACKEND) == stage
            and not isinstance(element.cells[CODEX_BACKEND], NotApplicableDisposition)
        )
    )


@pytest.mark.parametrize("stage", _STAGES)
def test_needed_set_matches_the_manifest(stage: str) -> None:
    manifest = load_manifest()
    needed = needed_element_ids(manifest, stage, CODEX_BACKEND)
    assert needed == _expected_needed(manifest, stage)
    assert needed


@pytest.mark.parametrize("stage", _STAGES)
def test_every_needed_id_is_rendered_or_deferred(stage: str) -> None:
    manifest = load_manifest()
    needed = set(needed_element_ids(manifest, stage, CODEX_BACKEND))
    blocks = stage_blocks(stage, CODEX_BACKEND, docs_root=_COMMANDS_ROOT)
    rendered = {block.element_id for block in blocks}
    assert rendered <= needed
    for element_id in needed - rendered:
        cell = manifest.elements[element_id].cells[CODEX_BACKEND]
        assert not isinstance(cell, str)
        assert cell.disposition in {"fails-closed", "deferred"}, element_id


def test_rfc_0017_members_are_skipped_and_pinned() -> None:
    manifest = load_manifest()
    deferred = set()
    for element_id, element in manifest.elements.items():
        cell = element.cells[CODEX_BACKEND]
        if (
            isinstance(cell, DeferredDisposition)
            and cell.ref.number == render_module.RFC_0017_TICKET
        ):
            deferred.add(element_id)
    assert render_module.RFC_0017_TICKET == 3216
    # #3216 implemented five members on codex and retargeted the two finalize
    # members to C3 (#3062); all three RFC 0017 deferrals stay out of prompts.
    assert deferred == {"review.review-debt-filing"}
    rendered = {
        block.element_id
        for stage in _STAGES
        for block in stage_blocks(stage, CODEX_BACKEND, docs_root=_COMMANDS_ROOT)
    }
    assert not rendered & deferred
    assert "finalize.review-sign-off" not in rendered
    assert "finalize.gate-review-debt-re-file" not in rendered


def test_intake_ids_are_folded_into_the_plan_needed_set() -> None:
    needed = needed_element_ids(load_manifest(), "plan", CODEX_BACKEND)
    assert any(element_id.startswith("intake.") for element_id in needed)


def test_a_3216_deferral_skips_the_block() -> None:
    manifest = _manifest_variant(
        **{
            "finalize.review-sign-off": {
                "disposition": "deferred",
                "ref": {"kind": "ticket", "number": render_module.RFC_0017_TICKET},
            }
        }
    )
    ids = [
        b.element_id
        for b in stage_blocks(
            "finalize", CODEX_BACKEND, docs_root=_COMMANDS_ROOT, manifest=manifest
        )
    ]
    assert "finalize.review-sign-off" not in ids
    default = [
        b.element_id
        for b in stage_blocks("finalize", CODEX_BACKEND, docs_root=_COMMANDS_ROOT)
    ]
    assert "finalize.review-sign-off" not in default


def test_not_applicable_cell_drops_the_block() -> None:
    manifest = _manifest_variant(
        **{"plan.checkpoint-1": {"disposition": "not-applicable", "reason": "x"}}
    )
    ids = [
        b.element_id
        for b in stage_blocks(
            "plan", CODEX_BACKEND, docs_root=_COMMANDS_ROOT, manifest=manifest
        )
    ]
    assert "plan.checkpoint-1" not in ids
    assert "plan.checkpoint-1" in [
        b.element_id
        for b in stage_blocks("plan", CODEX_BACKEND, docs_root=_COMMANDS_ROOT)
    ]


def test_unknown_range_id_raises() -> None:
    manifest = _manifest_variant(**{"plan.checkpoint-1": {}})
    with pytest.raises(RenderError, match=r"plan\.checkpoint-1"):
        stage_blocks("plan", CODEX_BACKEND, docs_root=_COMMANDS_ROOT, manifest=manifest)


def test_blocks_are_verbatim_doc_text_in_doc_order() -> None:
    blocks = stage_blocks("plan", CODEX_BACKEND, docs_root=_COMMANDS_ROOT)
    assert blocks
    assert all(isinstance(block, NeutralBlock) for block in blocks)
    raw = _raw("auto-dev-plan.md")
    stripped = strip_doc_tags(raw)
    lines = [block.line for block in blocks]
    assert lines == sorted(lines)
    for block in blocks:
        assert block.text in stripped
        assert not block.text.endswith("\n")
        assert not TAG_SHAPED.search(block.text)
        assert raw.split("\n")[block.line - 1] == f"<!-- el: {block.element_id} -->"


# ---------------------------------------------------------------------------
# ship-it (R12)
# ---------------------------------------------------------------------------


def test_tagged_ship_it_follows_prep_pr_blocks() -> None:
    blocks = stage_blocks("finalize", CODEX_BACKEND, docs_root=_COMMANDS_ROOT)
    docs = [b.doc for b in blocks]
    assert docs.index("ship-it.md") > max(
        i for i, doc in enumerate(docs) if doc == "prep-pr.md"
    )
    text = _render("finalize")
    assert _SHIP_IT_STEP in text
    assert render_module._SHIP_IT_FALLBACK not in text


@pytest.mark.parametrize("variant", ["untagged", "absent"])
def test_untagged_or_absent_ship_it_gets_the_fallback(
    tmp_path: Path, variant: str
) -> None:
    ship_it = (
        strip_doc_tags(_raw("ship-it.md")) + f"\n{_PROJECT_MARKER}\n"
        if variant == "untagged"
        else None
    )
    root = _docs_copy(tmp_path, {"ship-it.md": ship_it})
    text = render_stage_prompt("finalize", CODEX_BACKEND, docs_root=root)
    assert render_module._SHIP_IT_FALLBACK in text
    for path in _PROBE_PATHS:
        assert path in text
    assert _SHIP_IT_STEP not in text
    assert _PROJECT_MARKER not in text
    assert "ship-it.md" not in [
        b.doc for b in stage_blocks("finalize", CODEX_BACKEND, docs_root=root)
    ]


def test_ship_it_fallback_is_only_for_finalize(tmp_path: Path) -> None:
    root = _docs_copy(tmp_path, {"ship-it.md": None})
    for stage in ("plan", "impl"):
        text = render_stage_prompt(stage, CODEX_BACKEND, docs_root=root)
        assert render_module._SHIP_IT_FALLBACK not in text


def test_tagged_ship_it_with_unpaired_range_raises(tmp_path: Path) -> None:
    lines = _raw("ship-it.md").split("\n")
    drop = next(i for i, line in enumerate(lines) if line.startswith("<!-- el-end:"))
    del lines[drop]
    root = _docs_copy(tmp_path, {"ship-it.md": "\n".join(lines)})
    with pytest.raises(RenderError, match="range_"):
        render_stage_prompt("finalize", CODEX_BACKEND, docs_root=root)


def test_ship_it_fallback_is_pinned_to_prep_pr() -> None:
    doc = _normalized(_raw("prep-pr.md")).replace("*", "")
    glue = _normalized(render_module._SHIP_IT_FALLBACK)
    for path in _PROBE_PATHS:
        assert path in doc
        assert path in glue
    for phrase in _SHIP_IT_PINS:
        assert phrase in doc, phrase
        assert phrase in glue, phrase
    assert "--skip-review" in doc
    assert "--skip-review" in glue
    assert 'blocker.reason "agent_block"' in glue


# ---------------------------------------------------------------------------
# Doc errors
# ---------------------------------------------------------------------------


def test_missing_core_doc_raises(tmp_path: Path) -> None:
    root = _docs_copy(tmp_path, {"auto-dev-plan.md": None})
    with pytest.raises(RenderError, match=r"auto-dev-plan\.md"):
        render_stage_prompt("plan", CODEX_BACKEND, docs_root=root)


def test_undecodable_core_doc_raises(tmp_path: Path) -> None:
    root = _docs_copy(tmp_path)
    (root / "auto-dev-impl.md").write_bytes(b"\xff\xfe not utf-8")
    with pytest.raises(RenderError, match=r"auto-dev-impl\.md"):
        render_stage_prompt("impl", CODEX_BACKEND, docs_root=root)


def test_scan_violation_in_a_core_doc_raises(tmp_path: Path) -> None:
    lines = _raw("auto-dev-plan.md").split("\n")
    drop = next(i for i, line in enumerate(lines) if line.startswith("<!-- el-end:"))
    del lines[drop]
    root = _docs_copy(tmp_path, {"auto-dev-plan.md": "\n".join(lines)})
    with pytest.raises(RenderError, match="range_"):
        render_stage_prompt("plan", CODEX_BACKEND, docs_root=root)


# ---------------------------------------------------------------------------
# Glue and import proof
# ---------------------------------------------------------------------------


def test_plan_and_impl_carry_the_imported_sentinel_fragments() -> None:
    plan = _render("plan")
    assert _sentinel_rules(None) + _PLAN_SENTINEL_TEMPLATE in plan
    assert _stage_preamble("plan", TICKET_PLACEHOLDER) in plan
    impl = _render("impl")
    assert _sentinel_rules(None) + _IMPL_SENTINEL_TEMPLATE in impl
    assert _stage_preamble("impl", TICKET_PLACEHOLDER) in impl


def test_finalize_names_the_schema_command() -> None:
    text = _render("finalize")
    assert "cw schema stage-output finalize" in text
    assert render_module._FINALIZE_SENTINEL_NOTE in text
    assert _sentinel_rules(None) in text


def test_session_id_and_ticket_id_are_threaded() -> None:
    text = _render("plan", session_id="abc", ticket_id="T-9")
    assert "--session-id abc" in text
    assert "ticket T-9" in text
    assert "--session-id" not in _render("plan")


@pytest.mark.parametrize("stage", _STAGES)
def test_contract_intro_follows_the_preamble(stage: str) -> None:
    text = _render(stage)
    assert render_module._CONTRACT_INTRO in text
    assert text.index(_stage_preamble(stage, "<ticket-id>")) == 0


# ---------------------------------------------------------------------------
# Structural tags-only check (R15)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("doc", _FIVE_DOCS)
def test_ranges_are_tags_only_and_well_placed(doc: str) -> None:
    raw = _raw(doc)
    scan = scan_doc_tags({doc: raw})
    assert [v for v in scan.violations if v.kind.startswith("range_")] == []
    assert scan.violations == ()
    raw_lines = raw.split("\n")
    stripped = strip_doc_tags(raw)
    assert not [line for line in stripped.split("\n") if TAG_SHAPED.match(line)]
    el_lines = [line for line in raw_lines if line.startswith("<!-- el: ")]
    removed = len(raw.splitlines()) - len(stripped.splitlines())
    assert removed == len(el_lines) + 2 * len(scan.ranges)
    for rng in scan.ranges:
        assert raw_lines[rng.begin_line - 1] == f"<!-- el-begin: {rng.element_id} -->"
        assert raw_lines[rng.begin_line] == f"<!-- el: {rng.element_id} -->"
        assert raw_lines[rng.end_line - 1] == f"<!-- el-end: {rng.element_id} -->"
        assert raw_lines[rng.end_line - 2] == ""
        after = raw_lines[rng.end_line]
        assert after.startswith(("<!-- el: ", "<!-- el-begin: ")), (doc, rng)
    assert len(scan.ranges) == _RANGE_COUNTS[doc]


def test_ranges_exist_only_in_the_five_docs() -> None:
    for doc in contract_manifest.DOC_SCOPES:
        if doc in _FIVE_DOCS:
            continue
        has_range = any(STRICT_RANGE_TAG.match(line) for line in _raw(doc).splitlines())
        assert not has_range, doc


# ---------------------------------------------------------------------------
# Leaf rule and goldens
# ---------------------------------------------------------------------------


def test_render_module_imports_opencode_runner_lazily() -> None:
    tree = ast.parse(Path(render_module.__file__).read_text(encoding="utf-8"))

    def imports_runner(node: ast.AST) -> bool:
        if isinstance(node, ast.ImportFrom):
            return (node.module or "").startswith("cw.opencode_runner")
        if isinstance(node, ast.Import):
            return any(a.name.startswith("cw.opencode_runner") for a in node.names)
        return False

    assert not [n for n in tree.body if imports_runner(n)]
    nested = [
        child
        for fn in ast.walk(tree)
        if isinstance(fn, ast.FunctionDef)
        for child in ast.walk(fn)
        if imports_runner(child)
    ]
    assert nested


@pytest.mark.parametrize("stage", _STAGES)
def test_golden(stage: str) -> None:
    _assert_golden(stage, _render(stage))
