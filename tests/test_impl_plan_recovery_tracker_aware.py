"""Guard tests: Stage 2's plan-recovery fallback is tracker-aware (#1906).

Pure-markdown assertions over `.claude/commands/auto-dev-impl.md`'s Orientation
paragraph. Mirrors the ``read_text()`` + literal-substring/window convention of
``test_auto_dev_preflight_resolutions.py`` / ``test_plan_persistence.py``.
``_cmd`` is imported from ``tests.conftest`` (#1787); ``_after``/``_nearby``
are imported from
``test_auto_dev_preflight_resolutions`` rather than duplicated, since that file
already defines and exports them.

Background: the Orientation paragraph's `.cw/plan.md`-absent fallback (#943)
hardcoded a GitHub-only recovery path (`gh api user`, `gh issue view ... --json
comments`) with zero `linear` branch, even though the very next paragraph in
the same file ("Comments are live, not cached", #1794) already branches
correctly by tracker. For a Linear-tracked ticket this meant Stage 2 could
never recover an approved plan already posted to the tracker and would
hard-exit `blocker.reason: "plan_missing"` even though Stage 1 genuinely did
complete (GEN-5485). This adds a `linear` sub-branch to the Orientation
paragraph, additive only -- the GitHub sub-branch is untouched.
"""

from tests.conftest import _appendix, _cmd
from tests.test_auto_dev_preflight_resolutions import _after, _nearby

ORIENTATION_START = "## Orientation: tracker-aware plan recovery"

GH_ME_RESOLVE = "ME=$(gh api user --jq .login)"
GH_JQ_FILTER = (
    'gh issue view <ticket> --json comments -q "[.comments[] | '
    'select(.body | contains(\\"plan-spec-reviewed\\")) | '
    'select(.author.login == \\"$ME\\")] | last | .body"'
)
GH_ME_EMPTY_SENTENCE = (
    "If `$ME` resolves empty, treat that identically to "
    '"no reviewed-plan comment found" — do not fall back to an '
    "unauthenticated substring match."
)

PLAN_MISSING_EXIT = 'exit `blocked` with `blocker.reason: "plan_missing"`'
WRITE_INSTRUCTION = "Write the comment verbatim to `.cw/plan.md`"
MARKER = "<!-- plan-spec-reviewed"


def _orientation_section() -> str:
    """The Orientation paragraph's ``.cw/plan.md``-absent recovery fallback.

    #1879 relocated this block to ``auto-dev-impl-appendix.md``: per-stage
    dispatch carries Stage 1's plan file forward in the same worktree, so a
    missing ``.cw/plan.md`` is a rare recovery path, not the common one. The
    core doc keeps the ``.cw/plan.md`` read and the trigger sentence; every
    assertion below follows the content rather than being dropped.
    """
    content = _appendix("impl")
    start = content.index(ORIENTATION_START)
    end = content.index("\n## ", start)
    return content[start:end]


def test_core_doc_keeps_plan_read_and_appendix_trigger() -> None:
    """Reading the plan stays common-path; only the fallback moved."""
    content = _cmd("auto-dev-impl.md")
    assert "Read `.cw/plan.md` for the approved plan from Stage 1" in content
    assert "Orientation: tracker-aware plan recovery" in content
    assert "do not exit `plan_missing` from this summary alone" in content


def test_orientation_branches_by_tracker_for_plan_recovery() -> None:
    """A `linear` branch exists for the plan-recovery fallback itself (not
    just for comments), proving a Linear-tracked ticket's recovery path
    exists at all (#1906)."""
    section = _orientation_section()
    assert "**Linear" in section
    assert "list_comments(<id>)" in section


def test_orientation_github_branch_byte_identical() -> None:
    """The pre-existing GitHub sub-branch is untouched -- the fix is
    additive only."""
    section = _orientation_section()
    assert GH_ME_RESOLVE in section
    assert GH_JQ_FILTER in section
    assert GH_ME_EMPTY_SENTENCE in section


def test_orientation_plan_missing_only_after_all_tracker_branches_exhausted() -> None:
    """The `plan_missing` exit reads as covering both branches, not
    implicitly GitHub-only."""
    section = _orientation_section()
    window = _nearby(section, PLAN_MISSING_EXIT, span=250)
    assert "GitHub or Linear" in window
    assert "active tracker's branch above" in window

    linear_idx = section.index("**Linear")
    exit_idx = section.index(PLAN_MISSING_EXIT)
    assert linear_idx < exit_idx


def test_orientation_preserves_marker_requirement() -> None:
    """The marker substring and the `.cw/plan.md` write instruction each
    still appear exactly once -- no duplicated/drifted write instruction."""
    section = _orientation_section()
    assert section.count(MARKER) == 1
    assert section.count(WRITE_INSTRUCTION) == 1


def test_orientation_linear_branch_precedes_or_follows_github_consistently() -> None:
    """Structural ordering lock: the GitHub sub-branch precedes the Linear
    sub-branch (additive-after-existing-text shape), so a future edit can't
    silently reorder/duplicate them."""
    section = _orientation_section()
    github_idx = section.index("(GitHub: resolve")
    linear_idx = section.index("**Linear")
    assert github_idx < linear_idx


def test_orientation_linear_branch_requires_authorship_check() -> None:
    """The Linear sub-branch requires a viewer/identity comparison against
    the candidate comment's author before honoring the marker (mirrors
    GitHub's `.author.login == $ME` shape), and fails closed -- never
    trusting any marker-bearing comment or a sentinel-string check -- when
    no viewer/identity operation is exposed (Settled Item A1, ALT-b)."""
    section = _orientation_section()
    linear_window = _after(section, "**Linear", span=900)
    assert "viewer" in linear_window
    assert "identity operation" in linear_window
    assert "author to match" in linear_window
    assert "mirrors GitHub's authorship check" in linear_window

    fallback_window = _after(section, "**Fail-closed fallback:**", span=500)
    assert "do NOT fall back to trusting any marker-bearing comment" in fallback_window
    assert "sentinel-string check" in fallback_window
    assert '"no reviewed-plan comment found"' in fallback_window
