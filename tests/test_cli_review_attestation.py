"""``cw review attestation check`` end to end (#3040, RFC 0017 II1).

The attestation rides in the worktree cw-context; the review mode, the merge
tolerance flag and the renumber rules come from the LIVE client config
written by ``write_clients_yaml``. Hermetic ``tmp_path`` repos.
"""

from __future__ import annotations

import json
import re
from typing import TYPE_CHECKING

import pytest
from click.testing import CliRunner

from cw.cli import main
from cw.cli.review import attestation as cli_mod
from cw.config import clients_file
from tests._clients_yaml import ClientSpec, write_clients_yaml
from tests._review_approval_helpers import (
    cw_context,
    head,
    write_context,
)
from tests._review_attestation_helpers import (
    ATTEST_MIGRATION,
    ATTEST_TICKET,
    REVIEW_CLIENT,
    build_attested_setup,
    review_row,
)
from tests._review_attestation_helpers import (
    AttestedSetup as Setup,
)
from tests._review_attestation_helpers import (
    write_attested as _write_attested,
)
from tests._shared.git_repos import git_in

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    from click.testing import Result

TICKET = ATTEST_TICKET
CLIENT = REVIEW_CLIENT
_MIGRATION = ATTEST_MIGRATION


@pytest.fixture
def setup(
    tmp_path: Path,
    make_git_repo_with_origin: Callable[..., tuple[Path, Path]],
) -> Setup:
    return build_attested_setup(tmp_path, make_git_repo_with_origin, "wt-attest-cli")


CLEAN_ON: dict[str, object] = {"convergence": "clean"}
CLEAN_OFF: dict[str, object] = {
    "convergence": "clean",
    "default_merge_tolerance": False,
}
BLOCKING: dict[str, object] = {"convergence": "blocking"}


def _verdict(result: Result) -> str:
    return result.stdout.strip()


class TestClassAMatrix:
    def test_clean_mode_flag_on_tolerates_clean_default_merge(
        self, setup: Setup
    ) -> None:
        setup.live_client(CLEAN_ON)
        setup.ledger()
        setup.merge_main()
        result = setup.run()
        assert (result.exit_code, _verdict(result)) == (0, "valid")
        code = setup.commit("src/later.py", "x\n")
        result = setup.run("--json")
        assert result.exit_code == 1
        payload = json.loads(result.stdout)
        assert payload["verdict"] == "invalid:untolerated_commit"
        assert payload["offending_commit"] == code

    def _assert_merge_offends(self, setup: Setup) -> None:
        merge = setup.merge_main()
        result = setup.run("--json")
        assert result.exit_code == 1
        payload = json.loads(result.stdout)
        assert payload["verdict"] == "invalid:untolerated_commit"
        assert payload["offending_commit"] == merge
        assert "tolerance_off" in payload["detail"]

    def test_flag_off_invalidates_at_first_merge(self, setup: Setup) -> None:
        setup.live_client(CLEAN_OFF)
        setup.ledger()
        self._assert_merge_offends(setup)

    def test_blocking_mode_invalidates_at_first_merge(self, setup: Setup) -> None:
        setup.live_client(BLOCKING)
        setup.ledger()
        self._assert_merge_offends(setup)

    @pytest.mark.parametrize("kind", ["absent", "malformed", "unparseable", "unknown"])
    def test_unreadable_live_config_invalidates_at_first_merge(
        self, setup: Setup, kind: str
    ) -> None:
        setup.ledger()
        path = clients_file()
        if kind == "malformed":
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("clients:\n  test-client:\n    bogus_key: 1\n")
        elif kind == "unparseable":
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("clients:\n  test-client:\n    bogus_key: [\n")
        elif kind == "unknown":
            write_clients_yaml(ClientSpec("someone-else", setup.worktree))
        self._assert_merge_offends(setup)

    @pytest.mark.parametrize("ledger", ["none", "unconverged", "other_tip"])
    def test_ledger_not_naming_attested_head_invalidates_at_first_merge(
        self, setup: Setup, ledger: str
    ) -> None:
        from tests._review_convergence_helpers import fresh_ledger, write_ledger

        setup.live_client(CLEAN_ON)
        if ledger == "unconverged":
            write_ledger(setup.worktree, fresh_ledger(ticket_id=TICKET))
        elif ledger == "other_tip":
            setup.ledger("f" * 40)
        self._assert_merge_offends(setup)

    def test_flag_is_read_from_live_config_not_context(self, setup: Setup) -> None:
        worker_edit = {"convergence": "clean", "default_merge_tolerance": True}
        _write_attested(setup, review=worker_edit)
        setup.live_client(CLEAN_OFF)
        setup.ledger()
        self._assert_merge_offends(setup)

    def test_context_saying_off_does_not_override_live_on(self, setup: Setup) -> None:
        _write_attested(
            setup, review={"convergence": "blocking", "default_merge_tolerance": False}
        )
        setup.live_client(CLEAN_ON)
        setup.ledger()
        setup.merge_main()
        assert _verdict(setup.run()) == "valid"


@pytest.mark.parametrize(
    "review",
    [CLEAN_ON, CLEAN_OFF, BLOCKING, None],
    ids=["clean_on", "clean_off", "blocking", "absent"],
)
def test_non_merge_classes_tolerated_for_every_client_kind(
    setup: Setup, review: dict[str, object] | None
) -> None:
    if review is not None:
        setup.live_client(review)
    setup.commit("CHANGELOG.md", "# Changelog\n\n- entry\n")
    setup.rename("feature.py", "renamed.py")
    git_in(setup.worktree, "mv", _MIGRATION, "alembic/versions/0002_x.py")
    (setup.worktree / "alembic/versions/0002_x.py").write_text(
        'revision = "a2"\ndown_revision = None\nbody = 1\n', encoding="utf-8"
    )
    git_in(setup.worktree, "add", "alembic")
    git_in(setup.worktree, "commit", "-m", "renumber")
    result = setup.run()
    assert (result.exit_code, _verdict(result)) == (0, "valid")


def test_client_renumber_rule_comes_from_live_config(setup: Setup) -> None:
    body = "".join(f"line {n}\n" for n in range(8))
    setup.commit("db/migrate/1_x.rb", f"version: 1\n{body}")
    setup.attested = head(setup.worktree)
    _write_attested(setup)
    git_in(setup.worktree, "mv", "db/migrate/1_x.rb", "db/migrate/2_x.rb")
    (setup.worktree / "db/migrate/2_x.rb").write_text(
        f"version: 2\n{body}", encoding="utf-8"
    )
    git_in(setup.worktree, "add", "db")
    git_in(setup.worktree, "commit", "-m", "renumber")
    assert _verdict(setup.run()) == "invalid:untolerated_commit"
    rule = {"path_glob": "db/migrate/*.rb", "line_regex": "^version:"}
    setup.live_client({"attestation": {"renumber_patterns": [rule]}})
    assert _verdict(setup.run()) == "valid"


class TestTrustModel:
    def test_attestation_for_another_ticket_is_wrong_ticket(self, setup: Setup) -> None:
        _write_attested(setup, ticket_id="T-other")
        result = setup.run()
        assert (result.exit_code, _verdict(result)) == (1, "invalid:wrong_ticket")
        assert result.stderr.strip()

    def test_ticket_and_client_overrides(self, setup: Setup) -> None:
        assert _verdict(setup.run("--ticket", "T-2")) == "invalid:wrong_ticket"
        assert _verdict(setup.run("--client", "other")) == "invalid:wrong_client"
        assert _verdict(setup.run("--ticket", TICKET, "--client", CLIENT)) == "valid"

    def test_verifier_leaves_refs_index_and_worktree_unchanged(
        self, setup: Setup
    ) -> None:
        setup.live_client(CLEAN_ON)
        setup.ledger()
        setup.merge_main()

        def snapshot() -> tuple[str, str, bytes]:
            refs = git_in(setup.worktree, "for-each-ref")
            status = git_in(setup.worktree, "status", "--porcelain")
            index = (setup.worktree / ".git" / "index").read_bytes()
            return refs, status, index

        before = snapshot()
        assert _verdict(setup.run()) == "valid"
        assert snapshot() == before


class TestCliShape:
    def test_missing_attestation(self, setup: Setup) -> None:
        write_context(
            setup.worktree,
            cw_context(
                setup.worktree,
                review_row(setup.worktree),
                ticket_id=TICKET,
                client=CLIENT,
            ),
        )
        result = setup.run()
        assert (result.exit_code, _verdict(result)) == (1, "invalid:missing")

    def test_unreadable_context(self, setup: Setup) -> None:
        write_context(setup.worktree, "{not json")
        assert _verdict(setup.run()) == "invalid:unreadable"

    def test_old_context_schema(self, setup: Setup) -> None:
        _write_attested(setup, schema_version=19)
        assert _verdict(setup.run()) == "invalid:schema_version"

    def test_explicit_context_path(self, setup: Setup, tmp_path: Path) -> None:
        moved = tmp_path / "ctx.json"
        moved.write_text(
            (setup.worktree / ".claude" / "cw-context.json").read_text(),
            encoding="utf-8",
        )
        write_context(setup.worktree, "{not json")
        assert _verdict(setup.run("--context", str(moved))) == "valid"

    def test_json_shape(self, setup: Setup) -> None:
        result = setup.run("--json")
        assert result.exit_code == 0
        payload = json.loads(result.stdout)
        assert list(payload) == [
            "verdict",
            "valid",
            "detail",
            "offending_commit",
            "offending_path",
            "attestation_head_sha",
            "head_sha",
            "engine",
            "reviewer_backend",
            "ticket_id",
            "client",
        ]
        assert payload["valid"] is True
        assert payload["client"] == CLIENT

    def test_usage_error_exits_2(self, setup: Setup) -> None:
        assert setup.run("--bogus").exit_code == 2

    def test_help_text_matches_the_contract(self) -> None:
        def flat(text: str) -> str:
            return re.sub(r"\s+", "", text)

        runner = CliRunner()
        group_help = flat(
            runner.invoke(main, ["review", "attestation", "--help"]).output
        )
        check_help = flat(
            runner.invoke(main, ["review", "attestation", "check", "--help"]).output
        )
        group_doc = cli_mod.attestation.help or ""
        check_doc = cli_mod.check.help or ""
        assert flat(group_doc.split("\n\n")[0]) in group_help
        for paragraph in check_doc.split("\n\n"):
            assert flat(paragraph) in check_help
        for constant in (
            cli_mod._TICKET_HELP,
            cli_mod._CLIENT_HELP,
            cli_mod._WORKTREE_HELP,
            cli_mod._CONTEXT_HELP,
            cli_mod._JSON_HELP,
        ):
            assert flat(constant) in check_help
        assert "valid`` or ``invalid:<reason>``" in check_doc
