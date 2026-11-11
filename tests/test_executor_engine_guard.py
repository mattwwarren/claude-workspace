"""The REVIEW engine guard of the opencode and local pre-flights (#3216).

``cw.executor._engine_guard.engine_block_details`` decides, and words, the
fail-closed refusal of a REVIEW spawn on a backend that cannot run a repo review
engine round. The executors build the blocked result themselves.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from cw.executor._engine_guard import engine_block_details
from cw.models import ClientConfig, Stage
from cw.models.client import ReviewConfig, ReviewEngineConfig
from cw.models.orchestrator_config.constants import (
    CLAUDE_NATIVE_BACKEND,
    CODEX_BACKEND,
    LOCAL_BACKEND,
    OPENCODE_BACKEND,
)
from cw.review_engine.constants import engine_refusal_details


def _client(*, engine: bool) -> ClientConfig:
    review = (
        ReviewConfig(
            convergence="clean", engine=ReviewEngineConfig(skill="repo-review")
        )
        if engine
        else ReviewConfig()
    )
    return ClientConfig(name="c", workspace_path=Path("/unused"), review=review)


class TestEngineBlockDetails:
    @pytest.mark.parametrize("backend", [CLAUDE_NATIVE_BACKEND, CODEX_BACKEND])
    def test_a_backend_that_can_run_an_engine_is_never_blocked(
        self, backend: str
    ) -> None:
        assert engine_block_details(_client(engine=True), Stage.REVIEW, backend) is None

    @pytest.mark.parametrize("backend", [OPENCODE_BACKEND, LOCAL_BACKEND])
    @pytest.mark.parametrize(
        "stage", [Stage.HARDEN, Stage.PLAN, Stage.IMPL, Stage.FINALIZE]
    )
    def test_a_stage_other_than_review_is_never_blocked(
        self, backend: str, stage: Stage
    ) -> None:
        assert engine_block_details(_client(engine=True), stage, backend) is None

    @pytest.mark.parametrize("backend", [OPENCODE_BACKEND, LOCAL_BACKEND])
    def test_a_client_with_no_engine_is_never_blocked(self, backend: str) -> None:
        assert (
            engine_block_details(_client(engine=False), Stage.REVIEW, backend) is None
        )

    @pytest.mark.parametrize("backend", [OPENCODE_BACKEND, LOCAL_BACKEND])
    def test_review_on_an_unsupported_backend_is_blocked_naming_it(
        self, backend: str
    ) -> None:
        details = engine_block_details(_client(engine=True), Stage.REVIEW, backend)

        assert details == engine_refusal_details(
            backend, "backend_unsupported", "skill=repo-review"
        )
        assert details is not None
        assert f"review engine refused on {backend} lane" in details
        assert "cause=backend_unsupported" in details
        assert "skill=repo-review" in details
