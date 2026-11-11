"""Render a codex stage prompt from the tagged command docs (RFC 0018 A3, #3058).

**Grammar contract.** An element body is *neutral* when its ``el-begin`` /
``el-end`` range (see :mod:`cw.contract_manifest._tags`) brackets text that
holds no Claude-only machinery. The renderer reads the five tagged docs
(``auto-dev-plan.md``, ``auto-dev-impl.md``, ``auto-dev-finalize.md``,
``prep-pr.md``, ``ship-it.md``), takes each ranged body verbatim, and wraps the
blocks in a small hand-written glue. Nothing is substituted, trimmed or
filtered inside a block: an element that mixes in Claude-only machinery stays
unranged. :data:`CLAUDE_ONLY_DENYLIST` is the enforced floor, checked at
runtime on the whole output, glue included.

**Needed set.** :func:`needed_element_ids` lists every manifest id whose stage
is the requested one and whose codex cell is not ``not-applicable``. An id the
renderer does not emit is covered by its ``deferred`` (or ``fails-closed``)
cell. A codex cell ``deferred`` to :data:`RFC_0017_TICKET` is skipped; flipping
that cell later renders the block with no doc change (D-7).

**Leaf rule.** ``import cw.contract_manifest`` stays light: the sentinel
fragments live in ``cw.opencode_runner`` and are imported inside functions
only (the per-file ``PLC0415`` ignore in ``pyproject.toml``).

**Goldens.** ``tests/fixtures/contract_render/<stage>-codex.txt`` pin the three
renders. After an intended change to a ranged block or to the glue, regenerate
them with ``CW_UPDATE_GOLDEN=1 uv run pytest tests/test_contract_render.py``.
``docs_root`` must be the repo-tracked ``.claude/commands`` copy, never
``~/.claude/commands``.

The codex PLAN executor (``cw.codex_plan``, RFC 0018 C1, #3060) consumes the
plan render; the IMPL and FINALIZE runners (C2-C3) will consume theirs. A
stage-specific codex addendum (:func:`_stage_addendum`) is appended last; only
plan has one today, so only ``plan-codex.txt`` carries it.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from pydantic import BaseModel, ConfigDict

from cw.contract_manifest._models import (
    DeferredDisposition,
    Manifest,
    NotApplicableDisposition,
)
from cw.contract_manifest._stages import scope_stage
from cw.contract_manifest._tags import STRICT_RANGE_TAG, scan_doc_tags
from cw.models.orchestrator_config.constants import CODEX_BACKEND

if TYPE_CHECKING:
    from pathlib import Path

__all__ = [
    "CLAUDE_ONLY_DENYLIST",
    "RENDER_BACKENDS",
    "RENDER_STAGES",
    "RFC_0017_TICKET",
    "TICKET_PLACEHOLDER",
    "NeutralBlock",
    "RenderError",
    "UnsupportedBackendError",
    "UnsupportedStageError",
    "needed_element_ids",
    "render_stage_prompt",
    "stage_blocks",
]

#: Claude-only machinery. A rendered prompt holding any of these (literal,
#: case-sensitive substring) is refused.
CLAUDE_ONLY_DENYLIST: tuple[str, ...] = (
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

RENDER_BACKENDS: tuple[str, ...] = (CODEX_BACKEND,)
RENDER_STAGES: tuple[str, ...] = ("plan", "impl", "finalize")

#: The docs each stage renders from, in order.
_STAGE_DOCS: dict[str, tuple[str, ...]] = {
    "plan": ("auto-dev-plan.md",),
    "impl": ("auto-dev-impl.md",),
    "finalize": ("auto-dev-finalize.md", "prep-pr.md"),
}
_SHIP_IT_DOC = "ship-it.md"

#: The RFC 0017 cells that remain deferred on codex (D-7 switch). The C3
#: finalize cells use ticket 3062, but only these two IDs are RFC 0017
#: deferrals; other 3062 cells are separate C3 work and remain renderable.
RFC_0017_TICKET = 3216
_CODEX_FINALIZE_TICKET = 3062
_RFC_0017_DEFERRAL_IDS = frozenset(
    {
        "review.review-debt-filing",
        "finalize.gate-review-debt-re-file",
        "finalize.review-sign-off",
    }
)

#: The ticket id a render carries when the caller names none.
TICKET_PLACEHOLDER = "<ticket-id>"

_CONTRACT_INTRO = (
    "The stage contract follows. It is rendered from the command docs and is "
    "the whole contract for this turn. Do every step inline in this one turn: "
    "there is no sub-session, no delegate and no second agent to hand work to. "
    "A step that names a slash command or a skill file means: Read that file "
    "and follow it step by step."
)

_SHIP_IT_FALLBACK = (
    "Ship step. Find this project's own ship-it by probing "
    ".claude/commands/ship-it.md, .claude/skills/ship-it/SKILL.md and "
    ".agents/skills/ship-it/SKILL.md, resolving each hit to its real path: two "
    "hits with one resolved path are one ship-it, not two. Read the file that "
    "exists and follow it step by step in this turn; every step with a side "
    "effect (push, gh pr create, auto-merge arming, monitor registration) must "
    "run or end in a blocked sentinel. If a command file and a distinct skill "
    "both exist, prefer the command file. If both skill paths exist and "
    "resolve to different files, prefer .claude/skills/ship-it/SKILL.md. Never "
    "run both, and say in the Ship Summary which ship-it ran and which was "
    "skipped. A --skip-review flag never reaches the project ship-it: run "
    "every pre-PR gate it defines exactly as it says, and never fill in a "
    "value the ship-it copies from a gate that did not run. If none exists, "
    'emit status "blocked" with blocker.reason "agent_block" and details '
    '"no project /ship-it"; never fall back to another project\'s ship-it and '
    "never create the PR by hand."
)

_FINALIZE_SENTINEL_NOTE = (
    "Finalize sentinel: run `cw schema stage-output finalize` for the JSON "
    "shape. Set stage_reached to stage4a_merge_gate, stage4b_pr_create or "
    "stage5_post_create, whichever stage you reached.\n"
)


#: The codex PLAN addendum (#3060): it resolves what the rendered plan blocks
#: say that is false for a single read-only codex turn whose sentinel, artifacts
#: and review stations are cw's. Appended last, plan only.
_PLAN_CODEX_ADDENDUM = (
    "Codex PLAN addendum (overrides anything above that conflicts with it).\n"
    "\n"
    "1. You are the Plan agent. There is no sub-session and no spawned agent. "
    "cw performs every step above that spawns one, including the two plan "
    "review stations (Plan Reviewer and Plan Soundness Reviewer), which cw "
    "runs itself after this turn; do not perform or simulate them.\n"
    "2. The state-writing cw commands named above (event recording, result "
    "emission, plan-approval revocation) are done by cw outside the sandbox; "
    "leave them alone. You write no files: the sandbox is read-only, and cw "
    "writes .cw/plan-draft.md and .cw/plan.md from your answer.\n"
    "3. Read .claude/cw-context.json for queue_metadata (operator_resolutions, "
    "plan_approved_at, plan_approved_fingerprint, plan_source): use jq, or "
    "cat the file if jq is absent.\n"
    "4. The ticket body and comments are injected below on GitHub; on any "
    "other tracker read them with the MCP get_issue and list_comments tools.\n"
    '5. If a "Resumed plan draft" section is injected below, it is the '
    "persisted draft from a prior park (the resume check): do not regenerate "
    "it from scratch; re-run the scan; return it as plan_markdown, changed "
    "only to apply resolved operator items. When that section says "
    '"approval: matches", return it unchanged.\n'
    "6. .claude/commands/auto-dev-plan-appendix.md and "
    ".claude/commands/auto-dev.md are not available in this turn. Their "
    "machinery (the resume delta and fast path, settlement folding and the "
    "round cap, the revision cycle, the operator-authority delta and "
    "comment-path approval) is deferred: on a resumed draft always re-scan, "
    "treat only exact fingerprint equality on the row as approval evidence, "
    "and treat the comment approval path as unavailable.\n"
    "7. The one sentinel template that applies is the plan template: "
    "stage_reached stage1_plan, scope.lines_actual null, status one of "
    "stage_complete | plan_pending_approval | ambiguities_pending_resolution "
    "| premises_pending_verification | no_op | blocked.\n"
    "8. Classify the scope tier yourself (small unless the plan touches "
    "migrations or an auth or security core, at any file or line count), "
    "write exactly one **Scope tier:** line and a ## Files Modified section "
    "in the plan, and put the same tier in the sentinel; cw only "
    "cross-checks it.\n"
    "9. Your final message is one JSON object with two string fields, "
    "plan_markdown (the whole plan) and sentinel (the framed "
    "<<<AUTO_DEV_RESULT ... AUTO_DEV_RESULT>>> block). Do not write any "
    "sign-off marker or bookkeeping comment line into plan_markdown; cw owns "
    "them.\n"
)


def _stage_addendum(stage: str) -> str:
    """The codex stage addendum appended last to *stage*'s render, or ``""``."""
    if stage == "plan":
        return "\n" + _PLAN_CODEX_ADDENDUM
    return ""


class RenderError(ValueError):
    """A stage prompt cannot be rendered."""


class UnsupportedBackendError(RenderError):
    """The backend is not one the renderer serves (codex only)."""


class UnsupportedStageError(RenderError):
    """The stage is not one the renderer serves (plan, impl, finalize)."""


class NeutralBlock(BaseModel):
    """One ranged element body, verbatim from its doc."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    element_id: str
    doc: str
    #: The 1-based line of the element's ``el:`` tag.
    line: int
    text: str


def _check_target(stage: str, backend: str) -> None:
    if backend not in RENDER_BACKENDS:
        msg = f"cannot render for backend {backend!r}; supported: {RENDER_BACKENDS}"
        raise UnsupportedBackendError(msg)
    if stage not in RENDER_STAGES:
        msg = f"cannot render stage {stage!r}; supported: {RENDER_STAGES}"
        raise UnsupportedStageError(msg)


def _read(docs_root: Path, name: str) -> str:
    """The text of one doc, UTF-8 with newlines normalised to ``\\n``."""
    path = docs_root / name
    try:
        text = path.read_bytes().decode("utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        msg = f"cannot read command doc {name}: {exc}"
        raise RenderError(msg) from exc
    return text.replace("\r\n", "\n").replace("\r", "\n")


def _ship_it_text(docs_root: Path) -> str | None:
    """The text of a *tagged* ``ship-it.md`` in *docs_root*, else ``None``."""
    if not (docs_root / _SHIP_IT_DOC).is_file():
        return None
    text = _read(docs_root, _SHIP_IT_DOC)
    if any(STRICT_RANGE_TAG.match(line) for line in text.split("\n")):
        return text
    return None


def _load_docs(stage: str, docs_root: Path) -> dict[str, str]:
    """The stage's docs (plus a tagged ship-it for finalize), scanned clean."""
    docs = {name: _read(docs_root, name) for name in _STAGE_DOCS[stage]}
    if stage == "finalize":
        ship_it = _ship_it_text(docs_root)
        if ship_it is not None:
            docs[_SHIP_IT_DOC] = ship_it
    violations = scan_doc_tags(docs).violations
    if violations:
        first = violations[0]
        msg = f"{first.doc}:{first.line} {first.kind}: {first.detail}"
        raise RenderError(msg)
    return docs


def needed_element_ids(manifest: Manifest, stage: str, backend: str) -> tuple[str, ...]:
    """Sorted ids of *stage* whose *backend* cell is not ``not-applicable``."""
    return tuple(
        sorted(
            element_id
            for element_id, element in manifest.elements.items()
            if scope_stage(element_id, backend) == stage
            and not isinstance(element.cells.get(backend), NotApplicableDisposition)
        )
    )


def _is_rfc_0017_deferral(manifest: Manifest, element_id: str, backend: str) -> bool:
    cell = manifest.elements[element_id].cells.get(backend)
    return isinstance(cell, DeferredDisposition) and (
        cell.ref.number == RFC_0017_TICKET
        or (
            element_id in _RFC_0017_DEFERRAL_IDS
            and cell.ref.number == _CODEX_FINALIZE_TICKET
        )
    )


def _body(lines: list[str], begin_line: int, end_line: int) -> str:
    """Lines between the ``el:`` tag and ``el-end``, trailing blanks dropped."""
    body = lines[begin_line + 1 : end_line - 1]
    while body and not body[-1].strip():
        body.pop()
    return "\n".join(body)


def stage_blocks(
    stage: str,
    backend: str,
    *,
    docs_root: Path,
    manifest: Manifest | None = None,
) -> tuple[NeutralBlock, ...]:
    """The neutral blocks of *stage*, in doc order.

    Raises :class:`UnsupportedBackendError`, :class:`UnsupportedStageError` or
    :class:`RenderError` (a missing or malformed doc, or a range whose id is
    not in the manifest).
    """
    _check_target(stage, backend)
    # Lazy: ``cw.contract_manifest`` imports this module at load time.
    from cw.contract_manifest import load_manifest

    manifest = load_manifest() if manifest is None else manifest
    docs = _load_docs(stage, docs_root)
    ranges = scan_doc_tags(docs).ranges
    needed = set(needed_element_ids(manifest, stage, backend))
    blocks: list[NeutralBlock] = []
    for doc, text in docs.items():
        lines = text.split("\n")
        for rng in (r for r in ranges if r.doc == doc):
            if rng.element_id not in manifest.elements:
                where = f"{doc}:{rng.begin_line}"
                msg = f"{where} range id {rng.element_id!r} is not in the manifest"
                raise RenderError(msg)
            if rng.element_id not in needed or _is_rfc_0017_deferral(
                manifest, rng.element_id, backend
            ):
                continue
            blocks.append(
                NeutralBlock(
                    element_id=rng.element_id,
                    doc=doc,
                    line=rng.begin_line + 1,
                    text=_body(lines, rng.begin_line, rng.end_line),
                )
            )
    return tuple(blocks)


def _preamble(stage: str, ticket_id: str) -> str:
    from cw.opencode_runner import _stage_preamble

    return _stage_preamble(stage, ticket_id)


def _sentinel_section(stage: str, session_id: str | None) -> str:
    from cw.opencode_runner import (
        _IMPL_SENTINEL_TEMPLATE,
        _PLAN_SENTINEL_TEMPLATE,
        _sentinel_rules,
    )

    rules = _sentinel_rules(session_id)
    if stage == "plan":
        return rules + _PLAN_SENTINEL_TEMPLATE
    if stage == "impl":
        return rules + _IMPL_SENTINEL_TEMPLATE
    return rules + _FINALIZE_SENTINEL_NOTE


def _check_denylist(output: str) -> None:
    for token in CLAUDE_ONLY_DENYLIST:
        if token in output:
            msg = f"rendered prompt holds Claude-only machinery {token!r}"
            raise RenderError(msg)


def render_stage_prompt(
    stage: str,
    backend: str,
    *,
    docs_root: Path,
    ticket_id: str = TICKET_PLACEHOLDER,
    session_id: str | None = None,
    manifest: Manifest | None = None,
) -> str:
    """The codex prompt for *stage*, rendered from the tagged docs.

    Layout: the imported stage preamble, the glue intro, the neutral blocks in
    doc order, for finalize without a tagged ship-it the glue's ship step, then
    the imported sentinel framing, then the stage's codex addendum (plan
    only, :data:`_PLAN_CODEX_ADDENDUM`). The result is UTF-8 text with ``\\n`` line
    endings and exactly one trailing newline. Raises :class:`RenderError` (or a
    subclass) when it cannot render a clean prompt.
    """
    blocks = stage_blocks(stage, backend, docs_root=docs_root, manifest=manifest)
    parts = [
        _preamble(stage, ticket_id),
        _CONTRACT_INTRO + "\n\n",
        "\n\n".join(block.text for block in blocks) + "\n",
    ]
    if stage == "finalize" and _ship_it_text(docs_root) is None:
        parts.append("\n" + _SHIP_IT_FALLBACK + "\n")
    parts.append(_sentinel_section(stage, session_id))
    parts.append(_stage_addendum(stage))
    output = "".join(parts)
    _check_denylist(output)
    return output.rstrip("\n") + "\n"
