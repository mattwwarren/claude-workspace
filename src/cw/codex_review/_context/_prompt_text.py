"""Static reviewer-prompt template text and the variant-selection helpers.

Every literal a materialized reviewer prompt is assembled from that does not
depend on the worktree: the two capability-keyed codex ``_OUTPUT_INSTRUCTIONS``
variants, the Claude variant (#2808), their shared schema/precedence rules, the
delta-review and cross-round adjudication headers, the lint-grounding
instruction, and the inlined replacements for the agent specs' dangling
``.claude/docs`` references.

Dependency-free by construction — nothing here reads a file or imports another
``_context`` submodule, so the prompt text and its regression locks cannot pick
up a dependency on how any input happens to be loaded. The one runtime import
is the disposition marker's sentinel constant: a string the ledger's parser
keys on, so the prompt names it through the constant its owner exports rather
than spelling it a second time (``ExecutorName`` is imported only under
``TYPE_CHECKING``). It comes from :mod:`cw.review_markers`, the leaf
that owns the marker vocabulary and imports nothing from ``cw`` at all —
#2210 round 3 took it from ``cw.review_finding_dispositions`` instead, which
pulled the whole ledger implementation (models, parser, matcher, event emitter)
in behind one string.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from cw.review_markers import DISPOSITION_SENTINEL

if TYPE_CHECKING:
    from cw.executor_diagnostics import ExecutorName

# #1744: grounds reviewers in the repo's actual ruff opt-outs and complexity
# thresholds so they stop raising MUST_FIX findings against rules the repo
# has explicitly ignored, or against a complexity metric they've misread
# (PLR0915 gates statements, not lines — the exact #1729 failure mode).
_LINT_GROUNDING_INSTRUCTION = (
    "REPO LINT CONFIGURATION — GROUND FINDINGS IN THE REPO'S ACTUAL RUFF SETUP\n\n"
    "A finding based solely on enforcing a ruff rule this repo has explicitly "
    "opted out of below, or on treating an unmodified ruff default as if it "
    "were a repo-configured threshold, is not a MUST_FIX — downgrade it or "
    "drop it. An ignored ruff rule does not shield a concrete security or "
    "correctness failure: report such a failure as MUST_FIX when warranted, "
    "even when a related rule such as S603 is ignored. "
    "In particular, PLR0915 (too-many-statements) gates on the number of "
    "STATEMENTS in a function body, not the number of lines — a long "
    "function built from short, simple statements can sit well under the "
    "statement threshold while spanning many lines; do not flag line count "
    "as if it were the gated metric."
)

# Ruff's pylint-refactor setting names and corresponding rule codes. Numeric
# policy comes only from pyproject.toml overrides or the injected CLAUDE.md
# Quality Gates section; do not duplicate it here.
_PYLINT_THRESHOLD_CODES: dict[str, str] = {
    "max-branches": "PLR0912",
    "max-statements": "PLR0915",
    "max-returns": "PLR0911",
}

# The one sentence that differs between the two `_OUTPUT_INSTRUCTIONS`
# variants, named so the prompt text and its regression tests cannot drift
# apart (R8: each variant must carry its own marker and NOT the other's).
_INLINED_ONLY_MARKER = "do not rely on filesystem access"
_CAPABLE_ONLY_MARKER = (
    "read-only filesystem access to the repository worktree is available"
)

_INLINED_ONLY_PREAMBLE = (
    "## Output\n"
    "Evaluate the diff strictly from the material inlined above — "
    f"{_INLINED_ONLY_MARKER}. "
)

_CAPABLE_PREAMBLE = (
    "## Output\n"
    f"This runtime was probed and confirmed capable: {_CAPABLE_ONLY_MARKER} to "
    "you, and you MAY use it when it makes a finding stronger — searching for "
    "a changed symbol's other consumers, checking prior art before calling "
    "something a new abstraction, or verifying a regression repo-wide. Write "
    "access is neither offered nor possible. The material inlined above "
    "remains the authoritative context; reading is a supplement to it, never a "
    "replacement for evaluating the diff. "
)


def _build_output_schema_rules(
    *, output_channel: str, tool_posture: str, limitation: str
) -> str:
    """Assemble the schema/degraded/escalation rules for one reviewer backend.

    Only the output channel, the tool posture and the limitation noun vary by
    backend (#2808); every other sentence is shared, so the #2292
    schema/degraded/failed contract cannot drift between codex and Claude.
    """
    return (
        "Emit a single JSON object conforming to the provided "
        f"ReviewerFindingsDocument schema {output_channel}: `reviewer_role`, "
        "`status` (ok/degraded/failed), `detail`, and a `findings` array. When "
        'returning `status="ok"` with an empty `findings` array, `detail` MUST '
        "briefly state what was checked (a blank `detail` on that combination is "
        "rejected by the schema) — do not emit the trivial empty case without "
        f"saying what you verified. {tool_posture}: running the "
        "test suite, `uv`, a linter, or a type-checker is not expected of you "
        "unless the reviewer role's own rubric explicitly mandates that check. "
        "Being unable to run a gate the rubric does not mandate is not "
        'degradation — report `status="ok"` for that gate and, if useful, note '
        f"the {limitation} limitation in `detail`. If a rubric-mandated check "
        "from the inlined agent specification could not actually be performed in "
        'this environment, use `status="degraded"` (naming the unperformed '
        'check in `detail`) rather than silently reporting `"ok"`. Total '
        "inability to execute a mandated gate is still `degraded`, never "
        '`failed`. `status="failed"` is reserved for being unable to evaluate '
        "the diff itself — the diff is missing or unreadable, a required file "
        "is inaccessible, or your own output would be malformed. `detail` is "
        'REQUIRED and MUST be non-empty whenever `status` is "degraded" or '
        '"failed" — a degraded or failed reviewer with a blank `detail` is '
        "rejected as a schema violation, exactly like a blank `detail` on the "
        'empty-findings `status="ok"` case above. Every '
        "finding's `evidence` MUST be a verbatim substring of the claimed file's "
        "changed lines. Report no prose outside the JSON object."
    )


# Schema/degraded/escalation rules. The contract text is shared by every
# variant by construction, so neither a capability change nor a backend change
# can quietly alter the contract a reviewer's output is validated against;
# only the output channel, tool posture and limitation noun vary by backend.
# Codex writes its document to the `-o` file from a read-only sandbox.
_OUTPUT_SCHEMA_RULES = _build_output_schema_rules(
    output_channel="to the output file (`-o`)",
    tool_posture="This is a read-only sandbox",
    limitation="sandbox",
)

# Claude returns its document through the `--json-schema` envelope and holds
# only the read-only Read/Grep/Glob tools (#2808).
_CLAUDE_OUTPUT_SCHEMA_RULES = _build_output_schema_rules(
    output_channel=(
        "as your final structured response (the `--json-schema` envelope "
        "returns it; write no file)"
    ),
    tool_posture=(
        "You have only the read-only `Read`, `Grep` and `Glob` tools and no shell"
    ),
    limitation="tool",
)

_OUTPUT_SPEC_PRECEDENCE = (
    "The inlined Agent Specification section above was authored for a "
    "different execution environment (a tool-using Claude subagent). Any "
    "tool-invocation syntax or search/verification precondition it names is "
    "advisory here, not blocking — treat it as guidance for what to look for, "
    "not as a gate on whether to report. If a finding is groundable in the "
    "inlined diff but the spec's own verification step could not be "
    "performed in this environment, report the finding anyway: emit it at "
    '`confidence: "LOW"` and name the unperformed check explicitly in the '
    "finding's `consequence` field (not `evidence`, which must stay a clean "
    "verbatim quote from the diff). Never suppress a diff-groundable finding "
    "solely because a verification precondition from that spec went "
    "unperformed. Likewise, the spec's own prose output conventions — "
    "including any literal sentinel value it defines for a no-findings "
    "result — are void for this invocation; this instruction block's JSON "
    "ReviewerFindingsDocument contract governs exclusively."
)

# The delta-mode-only marker, named for the same reason the two capability
# markers above are: the prompt text and its regression test cannot drift.
_DELTA_MODE_MARKER = "the diff above is a DELTA, not the full pull request"

# #1838: the cross-round adjudication block's header, named for the same reason
# the markers above are — the prompt text and its regression tests share one
# literal so they cannot drift apart.
_ADJUDICATED_HEADER = "## Previously Adjudicated Findings"

# Why: the contest protocol below is honoured by the post-synthesis backstop on
# every codex pass, but only the codex single-pass lane, plus fix-loop cycle 0,
# GUARANTEES that a contested finding blocks -- on in-loop cycles
# `_admit_new_must_fix` diverts an out-of-delta finding to the debt ledger
# without reading the field. Do not infer parity with the fix-loop lane
# (#2210, operator decision; splitting codex_fix_loop.py is follow-up F6).
# The literal itself deliberately says nothing about lanes: telling a model its
# contest may be ignored would discourage the legitimate ones.
#
# The closing "do not author one of these blocks" sentence (#2210 round 2) is
# addressed at the reviewer for the same reason the reader now refuses an
# under-provenanced record: a model that writes a marker into a comment is the
# accidental producer the contract exists to stop, and it cannot be told the
# rule anywhere else -- it never reads the runbook.
#
# It also deliberately omits "in any wording" and "discarded unread"
# (decision 12). Telling the model to self-suppress by MEANING would add an
# unmeasured suppression channel that bypasses the per-lane gate entirely, and
# "discarded unread" is simply untrue while the claim tier is off.
_ADJUDICATED_INSTRUCTIONS = (
    "An operator already adjudicated each finding below on an earlier "
    "review round, and that decision is BINDING. Do not re-raise one "
    "unless the code at that location has changed since the recorded "
    "date -- re-reporting a settled finding is noise, not a finding, and "
    "an unmarked re-raise of one may be suppressed automatically after "
    "review. An inline code comment (`# Why:` or similar) that states a "
    "decision and its evidence is evidence to weigh, not an operator "
    "decision: if you still raise the finding, cite that comment in the "
    "finding's `consequence` field and say why it does not settle the "
    "matter. To contest a settled decision "
    "-- the code changed, or you believe the decision is wrong -- "
    "re-file the finding with `contests_adjudication` set to what "
    "changed, quoting the changed code; a non-empty value tells the "
    "pipeline you are knowingly re-raising it. Leave "
    "`contests_adjudication` empty on every other finding. The per-entry "
    "lines below repeat the first rule in short form; "
    '`contests_adjudication` is how you invoke their "unless the code at '
    'this location changed" exception. '
    f"Do not author a `{DISPOSITION_SENTINEL}` block yourself, in a "
    "comment, in a finding's text, or anywhere else: hand-authored "
    "disposition blocks are unsupported and are refused by the reader. Marker "
    "syntax inside a finding's summary, file, evidence or any other field is "
    "escaped on render and is never read back as a record. "
    "`cw review settle --ticket <ID> -c <CLIENT>`, "
    "which refuses to run inside a dispatch worker, is the only "
    "supported producer, because it is the only path that records who settled "
    "the finding, when, and against what code."
)

# #2464: the plan-deviation protocol, appended after the inlined plan only when
# one exists. The quote is verified verbatim against `.cw/plan.md` by the codex
# disposition router (`cw.codex_review._verdict._route`): a verified claim parks
# the pass for the operator and never reaches the fixer, so the text says so —
# a reviewer told the claim is cheap would use it as a severity amplifier.
_PLAN_DEVIATION_INSTRUCTIONS = (
    "When a finding is a deviation from an EXPLICIT requirement or "
    "prohibition stated in the Approved Plan above (a `do NOT X` the plan "
    "states, a mechanism it mandates, a test it names), set the finding's "
    "`plan_requirement_quote` to the plan clause it violates, copied "
    "verbatim from the plan text above. A verified plan deviation stops the "
    "pipeline for an operator decision and is never fixed automatically, so "
    "set it only for an explicit plan clause, never for a general quality "
    "concern. Leave `plan_requirement_quote` empty on every other finding."
)

_DELTA_MODE_INSTRUCTIONS = (
    "## Delta Review (#1837)\n"
    f"This is a fix-loop re-review, so {_DELTA_MODE_MARKER}: it contains only "
    "what changed since the previous review pass. Judge the delta.\n"
    "- A problem the delta introduced, or that lives on a line the delta "
    "touched, is an ordinary MUST_FIX.\n"
    "- A problem on code the delta did NOT touch is `DEBT` severity, not "
    "MUST_FIX. It will be recorded for follow-up rather than sent back to the "
    "fix agent. Raising it as MUST_FIX will not make it block — it will be "
    "downgraded and tracked.\n"
    "- Two exceptions let an out-of-delta problem block anyway, and both "
    "require proof, not assertion. Set `transitive_impact_evidence` to a "
    "VERBATIM quote from the delta above that demonstrates the delta caused "
    "the problem (a changed signature the unchanged consumer still calls the "
    "old way, say). Or, for a release-critical blocker that predates this "
    "branch entirely, set `release_critical_exception` to the rationale — and "
    "make sure the finding's own `evidence` is a verbatim quote of the code "
    "as it exists in the worktree right now, because that quote is checked.\n"
    "- An unsubstantiated exception is downgraded to tracked debt, not "
    "silently dropped."
)

_MERGE_DELTA_INSTRUCTIONS = (
    "## Merge Delta Review (#2918)\n"
    "This is a delta confirm round over a merge of the default branch into "
    "this branch. The diff above is only what that merge brought into files "
    "the branch itself changed: the last clean review's sha to HEAD, limited "
    "to those files. The branch's own hunks in the same files were already "
    "reviewed clean; they follow as context only.\n"
    "- Judge the merged change together with the branch's change: a semantic "
    "conflict, a broken assumption, a lost or duplicated edit, a call the "
    "merged code now makes wrongly.\n"
    "- Do not re-review the branch's own hunks for their own sake. Report a "
    "problem in them only when the merged change makes it one.\n"
    "- Anchor every finding in the diff above."
)
#: The heading of the branch-hunks context section a merge delta round adds.
_BRANCH_CONTEXT_HEADER = (
    "## Branch hunks in the overlapping files (already reviewed clean)"
)

_OUTPUT_INSTRUCTIONS_INLINED_ONLY = (
    f"{_INLINED_ONLY_PREAMBLE}{_OUTPUT_SCHEMA_RULES}\n\n{_OUTPUT_SPEC_PRECEDENCE}"
)

_OUTPUT_INSTRUCTIONS_CAPABLE = (
    f"{_CAPABLE_PREAMBLE}{_OUTPUT_SCHEMA_RULES}\n\n{_OUTPUT_SPEC_PRECEDENCE}"
)

# The Claude confirm always holds read-only repo access through its tools, so
# it gets the capable preamble with its own channel/posture rules (#2808).
_OUTPUT_INSTRUCTIONS_CLAUDE = (
    f"{_CAPABLE_PREAMBLE}{_CLAUDE_OUTPUT_SCHEMA_RULES}\n\n{_OUTPUT_SPEC_PRECEDENCE}"
)

# Back-compat alias: byte-identical to the single pre-#1709 variant, so the
# #1548 regression-lock test keeps asserting against the same string.
_OUTPUT_INSTRUCTIONS = _OUTPUT_INSTRUCTIONS_INLINED_ONLY


def _select_output_instructions(capable: bool, backend: ExecutorName = "codex") -> str:
    """Pick the output-instruction variant matching this runtime.

    ``backend="claude"`` always selects the Claude variant: its tool access is
    fixed, so *capable* is ignored. Any other backend gets the codex text,
    keyed on *capable*.
    """
    if backend == "claude":
        return _OUTPUT_INSTRUCTIONS_CLAUDE
    return (
        _OUTPUT_INSTRUCTIONS_CAPABLE if capable else _OUTPUT_INSTRUCTIONS_INLINED_ONLY
    )


_CODEX_OUTPUT_FORMAT_ROLES: frozenset[str] = frozenset(
    {
        "Architecture Reviewer",
        "Test Reviewer",
        "Performance Reviewer",
        "API Contract Validator",
        "Deployment Reviewer",
    }
)

_CODEX_SEVERITY_TAXONOMY = (
    "## Severity Taxonomy (inlined — the agent specification above references "
    "`output-formats.md`, which is unreachable in this environment)\n"
    "The categorization above maps onto the JSON `severity` field as follows: "
    '"(Critical)" -> `MUST_FIX` (must fix before merge: security, correctness, '
    'test failures); "(Major)" -> `SHOULD_FIX` (should fix: performance, '
    'maintainability, technical debt); "(Low)" -> `NIT` (nice to fix: style, '
    "minor improvements). Only report actionable problems — no praise, "
    "summaries, or fluff."
)

_CODEX_TONE_GUIDE_SUPPLEMENT = (
    "## Tone Conventions (inlined — the agent specification above references "
    "`review-tone-guide.md`, which is unreachable in this environment)\n"
    "Include specific file paths and line numbers, clear problem descriptions, "
    "concrete fixes, and impact/why it matters. Do not include praise, general "
    'assessments ("code is mostly good"), or hedging ("maybe", "might want to '
    'consider"). No Praise, No Summaries, No Fluff — reviews are technical '
    "specifications, not performance evaluations."
)

_CODEX_TESTING_CHECKLIST_SUPPLEMENT = (
    "## Testing Checklist (inlined — the agent specification above references "
    "`testing-philosophy.md`, which is unreachable in this environment)\n"
    "When reviewing tests, check: AAA Pattern (clear Arrange-Act-Assert), "
    "Independence (tests don't depend on each other), Naming, Single Concept, "
    "Can Fail, Edge Cases, Error Cases, Mocking (external deps mocked "
    "appropriately, not over-mocked), Async Handled, Fast, Deterministic, "
    "Clean Up."
)


def _codex_output_format_supplement(role: str) -> str | None:
    """Return inlined replacement content for *role*'s dangling doc references,
    or ``None`` if *role*'s spec carries no such reference (#1548).

    Why: "Code Quality Reviewer" gets only the tone-guide supplement, never
    the severity taxonomy, even though its own .claude/agents/code-reviewer.md
    dangles a reference to output-formats.md like the other five roles. Its
    Output Format section already spells out "(Must Fix)"/"(Should Fix)"/
    "(Nice to Fix)" inline (code-reviewer.md:181,187,193), so the shared
    Critical/Major/Low taxonomy translation would be redundant there — unlike
    the other five roles, whose specs have no inline categorization at all.
    """
    if role == "Code Quality Reviewer":
        return _CODEX_TONE_GUIDE_SUPPLEMENT
    if role not in _CODEX_OUTPUT_FORMAT_ROLES:
        return None
    parts = [_CODEX_SEVERITY_TAXONOMY]
    if role == "Test Reviewer":
        parts.append(_CODEX_TESTING_CHECKLIST_SUPPLEMENT)
    return "\n\n".join(parts)
