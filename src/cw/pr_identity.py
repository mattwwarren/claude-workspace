"""Read one GitHub PR payload's identity and ref fields, once (#2985).

:func:`read_pr_identity` normalizes the state, url, head SHA and head/base
ref fields of a ``gh pr view`` payload and runs each validator once. It never
raises and never picks a "first failure": each field comes back as a value
plus a flag or error, so every caller walks its own precedence order and
builds its own refusal text. This module is domain-neutral and sits below
``cw.adopt_pr`` and ``cw.pr_feedback``; nothing under ``cw.dev_queue`` may
import it.
"""

from __future__ import annotations

from typing import NamedTuple

from cw._git import FULL_COMMIT_ID_RE
from cw.dev_queue.adoption import branch_charset_error
from cw.gh import _GH_PR_STATE_OPEN, parse_pr_url
from cw.pr_feedback_text import payload_string


class PrIdentity(NamedTuple):
    """The identity and ref fields of one PR read, normalized."""

    state: str
    is_open: bool
    url: str
    url_target: tuple[str, int] | None
    head_sha: str
    head_sha_valid: bool
    head_ref: str
    head_ref_error: str | None
    base_ref: str
    base_ref_error: str | None


def read_pr_identity(payload: dict[str, object]) -> PrIdentity:
    """Normalize the identity and ref fields of *payload*; never raises."""
    state = payload_string(payload, "state")
    url = payload_string(payload, "url")
    head_sha = payload_string(payload, "headRefOid")
    head_ref = payload_string(payload, "headRefName")
    base_ref = payload_string(payload, "baseRefName")
    return PrIdentity(
        state=state,
        is_open=state == _GH_PR_STATE_OPEN,
        url=url,
        url_target=parse_pr_url(url),
        head_sha=head_sha,
        head_sha_valid=FULL_COMMIT_ID_RE.fullmatch(head_sha) is not None,
        head_ref=head_ref,
        head_ref_error=branch_charset_error(head_ref),
        base_ref=base_ref,
        base_ref_error=branch_charset_error(base_ref),
    )
