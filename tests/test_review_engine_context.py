"""Tests for ``cw.review_engine.context`` (RFC 0017 I1, #3039)."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

import pytest

from cw.exceptions import CwError
from cw.review_engine.context import (
    FALLBACK_DEFAULT_BRANCH,
    engine_from_review_obj,
    load_context_object,
    read_engine_context,
)

if TYPE_CHECKING:
    from pathlib import Path


def _write(tmp_path: Path, payload: object) -> Path:
    path = tmp_path / "cw-context.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def test_engine_from_review_obj_name_and_guarded_paths(tmp_path: Path) -> None:
    review = {"engine": {"skill": "repo-review", "guarded_paths": ["docs/**"]}}
    assert engine_from_review_obj(review) == ("repo-review", None)

    path = _write(
        tmp_path,
        {
            "review": review,
            "ticket_id": "T-1",
            "client": "acme",
            "world_state_snapshot": {"origin_main_branch": "develop"},
        },
    )
    context = read_engine_context(path)

    assert context.skill == "repo-review"
    assert context.guarded_paths == ("docs/**",)
    assert context.ticket_id == "T-1"
    assert context.client == "acme"
    assert context.default_branch == "develop"


def test_engine_absent_is_none_without_error() -> None:
    assert engine_from_review_obj({}) == (None, None)
    assert engine_from_review_obj({"engine": None}) == (None, None)


@pytest.mark.parametrize(
    "engine",
    [{}, {"skill": 3}, {"skill": ""}, "repo-review", ["repo-review"]],
    ids=["empty", "non-string", "blank", "string", "list"],
)
def test_engine_malformed_object_reports_error(engine: object) -> None:
    skill, error = engine_from_review_obj({"engine": engine})

    assert skill is None
    assert error


def test_default_branch_from_snapshot_option_and_fallback_main(tmp_path: Path) -> None:
    review = {"engine": {"skill": "s"}}
    snapshot = _write(
        tmp_path,
        {"review": review, "world_state_snapshot": {"origin_main_branch": "dev"}},
    )
    assert read_engine_context(snapshot).default_branch == "dev"
    assert read_engine_context(snapshot, default_branch="x").default_branch == "x"

    bare = _write(tmp_path, {"review": review})
    assert read_engine_context(bare).default_branch == FALLBACK_DEFAULT_BRANCH

    blank = _write(
        tmp_path,
        {"review": review, "world_state_snapshot": {"origin_main_branch": ""}},
    )
    assert read_engine_context(blank).default_branch == FALLBACK_DEFAULT_BRANCH

    odd = _write(tmp_path, {"review": review, "world_state_snapshot": "main"})
    assert read_engine_context(odd).default_branch == FALLBACK_DEFAULT_BRANCH


def test_unreadable_context_yields_empty_context(tmp_path: Path) -> None:
    assert load_context_object(tmp_path / "absent.json") == {}
    broken = tmp_path / "broken.json"
    broken.write_text("{nope", encoding="utf-8")
    assert load_context_object(broken) == {}
    assert load_context_object(_write(tmp_path, [1, 2])) == {}

    with pytest.raises(CwError, match=r"no review\.engine"):
        read_engine_context(tmp_path / "absent.json")


def test_read_fails_closed_without_engine(tmp_path: Path) -> None:
    with pytest.raises(CwError, match=r"no review\.engine"):
        read_engine_context(_write(tmp_path, {"review": {"convergence": "clean"}}))
    with pytest.raises(CwError, match="skill is missing"):
        read_engine_context(_write(tmp_path, {"review": {"engine": {"skill": 1}}}))


def test_guarded_paths_and_identity_tolerate_odd_shapes(tmp_path: Path) -> None:
    odd_guard = _write(
        tmp_path,
        {"review": {"engine": {"skill": "s", "guarded_paths": "docs"}}, "ticket_id": 5},
    )
    context = read_engine_context(odd_guard)
    assert context.guarded_paths == ()
    assert context.ticket_id is None
    assert context.client is None
