"""Repo paths and readers for the tracked workflow prose that doc-guard tests
assert against: ``_REPO_ROOT``/``_SRC_ROOT``/``_COMMANDS_ROOT``, the ``src/``
file walk, and the slash-command, appendix, section and bash-fence readers.
Moved from ``tests/conftest.py`` (#2668).
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any

import yaml

from cw.contract_manifest import strip_doc_tags

if TYPE_CHECKING:
    pass


# Repo-root-relative path constants + src/ discovery, hoisted from
# test_review_approval_guard.py's pre-existing private copy (#1240).
# Shared by test_review_approval_guard.py (which keeps its own private
# copy, unmodified, deliberately left as-is) and test_ticket_boundary_guard.py
# (which imports these). Pure, generic path/discovery helpers with no
# scan-semantics coupling, so they are the ones hoisted; each test file's
# scan-specific `_run_scan` driver stays file-local.
_REPO_ROOT = Path(__file__).resolve().parents[2]


_SRC_ROOT = _REPO_ROOT / "src"


# Root of the repo-tracked slash-command prose that the doc-guard test files
# assert against, backing the shared ``_cmd`` reader below (#1787).
_COMMANDS_ROOT = _REPO_ROOT / ".claude" / "commands"


# Root of the repo-tracked skill prose, backing the shared ``_skill`` reader.
_SKILLS_ROOT = _REPO_ROOT / ".claude" / "skills"


def _iter_src_files() -> list[Path]:
    """Return every ``*.py`` file under ``src/``, sorted for determinism."""
    return sorted(_SRC_ROOT.rglob("*.py"))


def _files_under(prefix: str) -> list[Path]:
    """Return the ``src/`` ``*.py`` files whose posix relpath starts with *prefix*.

    *prefix* is relative to ``src/`` and MUST end in ``/`` (for example
    ``"cw/reconcile/"``): without the slash ``"cw/reconcile"`` would also match
    ``cw/reconcile_x.py``. Pure discovery, no scan semantics (#2630), in line
    with the pure, generic path/discovery helpers hoisted above.
    """
    return [
        path
        for path in _iter_src_files()
        if path.relative_to(_SRC_ROOT).as_posix().startswith(prefix)
    ]


def _load_workflow(path: Path) -> dict[Any, Any]:
    """Parse a GitHub Actions workflow YAML file at *path* (#1612).

    Parameterized hoist of the byte-identical private ``_workflow()`` helpers
    in test_changelog_advisory_workflow.py and test_pr_events_workflow.py,
    which differ only in the module-level ``WORKFLOW_PATH`` each closes over.
    Those two private copies are deliberately left unmodified; this is the
    canonical version a new workflow-guard test should import rather than
    adding a third copy. Each file's step/script accessors stay file-local —
    they are coupled to a specific job and step id, not generic.
    """
    workflow: dict[Any, Any] = yaml.safe_load(path.read_text())
    return workflow


def _on_block(workflow: dict[Any, Any]) -> dict[str, Any]:
    """Return the trigger block of a parsed *workflow* (#1612).

    PyYAML's SafeLoader follows YAML 1.1, which parses the bare ``on`` scalar
    key as the boolean ``True`` rather than the string "on" -- a well-known
    GitHub Actions YAML gotcha. Callers must not index ``workflow["on"]``.
    """
    on_block: dict[str, Any] = workflow[True]
    return on_block


def _cmd(name: str, *, raw: bool = False) -> str:
    """Return the text of ``.claude/commands/<name>`` (#1787), tags stripped.

    The ``<!-- el: <id> -->`` contract tags (#3053) are removed through
    ``cw.contract_manifest.strip_doc_tags`` so prose-window assertions never
    see them. The ``<!-- el-begin: <id> -->`` / ``<!-- el-end: <id> -->``
    range tags (#3058) go the same way; ``raw=True`` returns the file exactly
    as read (the contract manifest test scans the tags themselves).

    Canonical reader for the doc-guard test files that assert against
    slash-command prose. Consolidates 20 byte-identical (modulo an explicit
    ``encoding="utf-8"``) private per-file copies, the same "hoist a duplicated
    private test helper into conftest.py" pattern as ``_load_workflow`` above;
    a new command-prose guard test should import this rather than adding a
    twenty-first copy. Imported by::

        test_ambiguity_scan_adopted_assumptions.py
        test_auto_dev_finalize_automerge_verification.py
        test_auto_dev_finalize_early_push.py
        test_auto_dev_finalize_semantic_resolve.py
        test_auto_dev_gate_worktree_leak.py
        test_auto_dev_intake_context_schema.py
        test_auto_dev_model_pins.py
        test_auto_dev_preflight_resolutions.py
        test_blocking_findings_comment.py
        test_bodyfile_write_tool_conformance.py
        test_completion_artifacts_per_gate.py
        test_consolidated_park.py
        test_impl_guard_staleness_docs.py
        test_impl_plan_recovery_tracker_aware.py
        test_operator_actionable_findings_comment.py
        test_plan_format_only_findings.py
        test_plan_persistence.py
        test_plan_stage_settlement.py
        test_scope_conformance_gate_docs.py
        test_sentinel_emission_discipline.py
        test_unavailability.py

    ``test_auto_dev_intake_origin_sync_retry.py`` keeps its own copy: its
    signature is genuinely divergent (zero-argument, hardcoded filename), so it
    is not a duplicate of this helper. Each file's sibling ``_agent``/``_doc``/
    ``_skill`` readers stay file-local — out of scope for #1787.
    """
    text = (_COMMANDS_ROOT / name).read_text(encoding="utf-8")
    return text if raw else strip_doc_tags(text)


def _checkpoint1_section() -> str:
    """Return ``auto-dev-plan.md``'s Checkpoint 1 plan-approval section."""
    content = _cmd("auto-dev-plan.md")
    start = content.index("### Checkpoint 1 (Plan Approval)")
    end = content.index("### Step 1e:")
    return content[start:end]


def _appendix(stage: str, *, raw: bool = False) -> str:
    """Return the text of ``.claude/commands/auto-dev-<stage>-appendix.md`` (#1879).

    Sibling of ``_cmd`` for the core+appendix split. #1879 moved each stage
    doc's genuinely rare-path procedures (fetch-failure handling, divergence
    handling, the fix loop, CI-wait polling, ...) out of the core file the
    worker loads on every run and into a companion appendix the worker reads
    only when the named trigger condition fires. Guard tests whose pinned
    literals moved with their content assert against this reader instead of
    ``_cmd``; no assertion was dropped in the move. Contract tags are stripped
    as in ``_cmd`` unless ``raw=True``.
    """
    return _cmd(f"auto-dev-{stage}-appendix.md", raw=raw)


def _skill(name: str) -> str:
    """Return the text of ``.claude/skills/<name>``, *name* relative to it.

    For example ``"harden-ticket/SKILL.md"`` or
    ``"harden-ticket/references/sweep-checklist-cases.md"``. New skill-prose
    guard tests import this rather than adding another local copy. Skill files
    carry no ``<!-- el: -->`` contract tags, so there is no ``strip_doc_tags``
    step (unlike ``_cmd``).
    """
    return (_SKILLS_ROOT / name).read_text(encoding="utf-8")


def _section_between(content: str, start: str, end: str | None = None) -> str:
    """Return the slice of *content* from heading *start* up to *end* (#2974).

    *end* is an exact substring searched after *start*; when ``None`` the slice
    runs to the next ``"\\n## "`` level-2 heading or to end of file. Doc-guard
    tests hoist their section slicers here rather than carrying a private copy
    per file (the ``_cmd`` / ``_appendix`` convention above).
    """
    begin = content.index(start)
    if end is not None:
        return content[begin : content.index(end, begin)]
    nxt = content.find("\n## ", begin + len(start))
    return content[begin:] if nxt == -1 else content[begin:nxt]


def _norm(text: str) -> str:
    """Collapse *text* to single-spaced words (#2438).

    Doc-guard tests slice a markdown section and then need to assert a
    phrase regardless of the exact whitespace/line-wrapping around it.
    Hoisted out of ``test_plan_approval_operator_delta_fast_path.py`` and
    ``test_impl_operator_authority_delta.py``, which carried byte-identical
    private copies (#2438 MUST_FIX B) — import this instead of adding a third.
    """
    return " ".join(text.split())


def _step4c2_section() -> str:
    """Return ``auto-dev-finalize.md``'s Step 4c.2 agent-prompt region.

    Hoisted from ``test_auto_dev_finalize_early_push.py`` (#2354) once
    ``test_auto_dev_finalize_ahead_of_origin_push.py`` needed the same slice.
    """
    content = _cmd("auto-dev-finalize.md")
    start = content.index("#### Step 4c.2 — spawn the agent")
    end = content.index("### Step 4c.5")
    return content[start:end]


def _bash_fences(content: str) -> list[str]:
    """Return the body of every ```bash fenced block in *content* (#2141).

    Sibling of ``_cmd``/``_appendix`` above, and hoisted for the same reason
    (#1787's precedent): the guard-script doc tests in
    ``test_scope_conformance_gate_docs.py`` and
    ``test_auto_dev_finalize_semantic_resolve.py`` each carried a
    byte-identical private copy, so a fix to the (deliberately crude,
    no-markdown-parser) fence scanner could land in one and not the other.
    A new doc-guard test that needs to execute or inspect a fenced snippet
    should import this rather than adding a third copy.
    """
    fences: list[str] = []
    lines = content.splitlines()
    index = 0
    while index < len(lines):
        if lines[index].strip().startswith("```bash"):
            body: list[str] = []
            index += 1
            while index < len(lines) and not lines[index].strip().startswith("```"):
                body.append(lines[index])
                index += 1
            fences.append("\n".join(body))
        index += 1
    return fences
