"""The sections reporting disposition records that did not fire.

A refused record (#2210 round 2) lacked provenance; a stale one (#2232) was
settled against code that has since moved. Either way the finding keeps
blocking, and these sections say why a record the operator made did not apply.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from cw.codex_review._verdict._render._common import _safe, _truncate
from cw.review_finding_dispositions import split_disposition_key
from cw.review_markers import DISPOSITION_SENTINEL

if TYPE_CHECKING:
    from cw.review_findings import ReviewVerdict

# #2210 round 2: disposition records the reader refused to apply. A refusal's
# EFFECT is already visible (the finding keeps blocking); what would otherwise
# be invisible is the attempt, so it gets its own section rather than an
# annotation on a finding that may not even be in this pass.
_REFUSED_DISPOSITION_HEADING = "### Disposition records refused (no provenance)"
_REFUSED_DISPOSITION_NOTE = (
    "These recorded dispositions were IGNORED, not applied. Each is missing "
    "provenance that `cw review settle` always records, and a suppression "
    "that cannot say who settled the finding, when, against what code and "
    f"why is not an audit record. Hand-authoring a `{DISPOSITION_SENTINEL}` "
    "block is unsupported — `cw review settle` "
    "is the only supported producer, because it is the only path that records "
    "provenance and refuses to run inside a dispatch worker. Re-settle each "
    "finding below with `cw review settle <file> --reason '<why>' --ticket "
    "<ticket> -c <client> --out settle.md`: that records the decision on the "
    "cw dev-queue row, and `settle.md` is the marker to post as its own "
    "comment."
)
# Bounded for the same reason the settle section is: the rest of the comment
# needs the remaining budget, and GitHub caps a body at 65,536 characters. A
# row is one short line, so this cap costs far less than the payload section's.
_REFUSED_MAX_ROWS = 20
_REFUSED_SUMMARY_MAX = 120

# #2232: disposition records the reader keyed onto a finding but declined to
# apply, because the code at that location moved since the settle. Its own
# section rather than a per-finding annotation, for the same reason the
# refusals above get one: the finding this concerns may read as a plain
# re-raise, and what needs saying is why a record the operator knows they made
# did not fire.
_STALE_DISPOSITION_HEADING = "### Settled findings re-raised (the code moved)"
_STALE_DISPOSITION_NOTE = (
    "These findings WERE settled, and the settle was NOT applied on this "
    "pass: the file changed between the commit the record was settled "
    "against and the one reviewed here, so the finding is being re-raised "
    "rather than silently suppressed against code nobody adjudicated. The "
    "ledger record has not been expired — it is still there, and still "
    "applies to any pass where that file has not moved. If the finding is "
    "still settled against the current code, re-run `cw review settle <file> "
    "--reason '<why>' --ticket <ticket> -c <client>` for it with the new "
    "reviewed sha; if the change reintroduced the problem, "
    "fix it. Reviewing the diff between the two shas below is how you tell "
    "the two apart."
)
# Sized like the refusals above and for the same reasons, but as its own pair
# rather than a reuse of theirs: the two sections cap independently, and a
# shared constant would couple a future tuning of one to the other.
_STALE_MAX_ROWS = 20
_STALE_SUMMARY_MAX = 120


def _render_refused_dispositions(verdict: ReviewVerdict) -> list[str]:
    """Report every disposition record the reader refused to apply (#2210).

    The reader drops an under-provenanced ledger entry rather than letting it
    silence a finding. Dropping it silently would trade one invisible act for
    another: the operator would see a finding they believe they settled come
    back, with nothing anywhere saying why. This section says why, names the
    record, and points at the only supported way to produce one.

    Empty-returns-``[]`` like every other per-concern helper here, and renders
    on blocking and clean passes alike — a refused ``ACCEPTED`` record is worth
    reporting even when nothing blocks.
    """
    refused = verdict.refused_dispositions
    if not refused:
        return []
    lines = [_REFUSED_DISPOSITION_HEADING, "", _REFUSED_DISPOSITION_NOTE, ""]
    for record in refused[:_REFUSED_MAX_ROWS]:
        file, summary = split_disposition_key(record.key)
        missing = ", ".join(record.missing)
        lines.append(
            f"- **{_safe(file)}** — {_truncate(summary, _REFUSED_SUMMARY_MAX)} "
            f"(missing: {missing})"
        )
    residue = len(refused) - _REFUSED_MAX_ROWS
    if residue > 0:
        lines.append(f"- …and {residue} more refused record(s).")
    lines.append("")
    return lines


def _render_stale_dispositions(verdict: ReviewVerdict) -> list[str]:
    """Report every settle the drift check declined to apply (#2232).

    The structural twin of :func:`_render_refused_dispositions`, and the
    operator-facing half of the gap ADR-0016 named: an identity that is
    deliberately not evidence-anchored lets a suppression outlive its code,
    and the answer this repo already chose for that class of cost is to make
    the act visible rather than to add a silent expiry.

    Each row carries BOTH shas so the reader can run the diff themselves
    instead of taking the pipeline's word for it. Bounded rows then a counted
    residue, and ``_safe``/``_truncate`` over the ledger key's summary half,
    which is model-authored text like every other span in this module.

    Empty-returns-``[]``, and renders on blocking and clean passes alike — a
    stale record on a pass that happens not to block is still something the
    operator settled and needs to know did not fire.
    """
    stale = verdict.stale_dispositions
    if not stale:
        return []
    lines = [_STALE_DISPOSITION_HEADING, "", _STALE_DISPOSITION_NOTE, ""]
    for record in stale[:_STALE_MAX_ROWS]:
        file, summary = split_disposition_key(record.key)
        lines.append(
            f"- **{_safe(file)}** — {_truncate(summary, _STALE_SUMMARY_MAX)} "
            f"(settled against `{_safe(record.reviewed_sha)}`, reviewed at "
            f"`{_safe(record.current_sha)}`)"
        )
    residue = len(stale) - _STALE_MAX_ROWS
    if residue > 0:
        lines.append(f"- …and {residue} more stale record(s).")
    lines.append("")
    return lines
