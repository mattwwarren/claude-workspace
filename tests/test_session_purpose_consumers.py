"""Consumers of the new session purposes PLAN, REVIEW, FINALIZE (#3054).

The enum grew three values for codex stage sessions. These pin the consumers a
REVIEW session reaches: persistence, the ``impl`` lookup that must no longer
match it, the purpose prompt, and the client-config purpose validation.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from cw.config import _validate_purposes, load_state, save_state
from cw.exceptions import CwError
from cw.models import CwState, SessionPurpose
from cw.prompts import get_purpose_prompt
from tests._shared.session_seeding import _make_daemon_session

if TYPE_CHECKING:
    from pathlib import Path

NEW_PURPOSES = (SessionPurpose.PLAN, SessionPurpose.REVIEW, SessionPurpose.FINALIZE)


@pytest.mark.parametrize("purpose", NEW_PURPOSES)
def test_persisted_session_round_trips_new_purpose(
    tmp_config_dir: Path, purpose: SessionPurpose
) -> None:
    save_state(CwState(sessions=[_make_daemon_session(id="rt", purpose=purpose)]))
    loaded = load_state().sessions[0]
    assert loaded.purpose is purpose


def test_impl_lookup_no_longer_matches_codex_review_session(
    tmp_config_dir: Path,
) -> None:
    save_state(
        CwState(
            sessions=[
                _make_daemon_session(
                    id="codex-rev", client="test", purpose=SessionPurpose.REVIEW
                )
            ]
        )
    )
    state = load_state()
    assert state.find_session("test", "impl") is None
    found = state.find_session("test", "review")
    assert found is not None
    assert found.id == "codex-rev"


def test_purpose_prompt_is_none_for_review_without_raising() -> None:
    assert get_purpose_prompt("review") is None


def test_auto_purposes_accepts_new_values() -> None:
    _validate_purposes(["review", "plan", "finalize"])


def test_auto_purposes_still_rejects_unknown_value() -> None:
    with pytest.raises(CwError, match="Invalid purpose 'bogus'"):
        _validate_purposes(["bogus"])
