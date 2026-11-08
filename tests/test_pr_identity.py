"""Shared PR identity reader (#2985): unit cases and per-caller characterization.

``read_pr_identity`` normalizes the identity and ref fields once; each caller
(``cw.adopt_pr`` and ``cw.pr_feedback``) keeps its own precedence order and its
own message text. :class:`TestEachCallerKeepsItsOwnText` pins those texts
byte-for-byte, so the consolidation cannot move a refusal.
"""

from __future__ import annotations

import ast
from datetime import UTC, datetime
from pathlib import Path

import pytest

from cw.adopt_pr import read_adopted_pr
from cw.exceptions import AdoptionRefusedError
from cw.pr_feedback import FeedbackSynthesisError, synthesize_pr_feedback
from cw.pr_identity import read_pr_identity
from tests._adopt_pr_helpers import (
    FIXTURE_CONFLICTING,
    adopt_client,
    load_adopt_fixture,
    stub_pr_view,
)

REPO = "mattwwarren/claude-workspace"
PR_NUMBER = 2933
PR_999_URL = f"https://github.com/{REPO}/pull/999"
FIXED_NOW = datetime(2026, 11, 8, tzinfo=UTC)
#: The dev-queue package must never import the shared identity reader.
DEV_QUEUE_SRC = Path(__file__).parent.parent / "src" / "cw" / "dev_queue"


def _noop_sleep(seconds: float) -> None:
    assert seconds >= 0


def _charset_error(ref: str) -> str:
    """The ``branch_charset_error`` text for *ref*, as the adopt refusal embeds it."""
    return (
        f"Value error, Invalid branch {ref!r}: must start with alphanumeric and"
        " contain only [a-zA-Z0-9/_.-], with no '..' sequence"
    )


def _adopt_charset(ref: str) -> str:
    return (
        f"Refusing to adopt PR #2933: head branch {ref!r} fails the row branch"
        f" charset ({_charset_error(ref)})."
    )


class TestReadPrIdentity:
    """Well-formed capture plus each malformed field, normalized and flagged."""

    def test_well_formed_capture_is_clean(self) -> None:
        ident = read_pr_identity(load_adopt_fixture(FIXTURE_CONFLICTING))
        assert ident.state == "OPEN"
        assert ident.is_open is True
        assert ident.url_target == (REPO, PR_NUMBER)
        assert ident.head_sha_valid is True
        assert ident.head_ref_error is None
        assert ident.base_ref_error is None

    @pytest.mark.parametrize(
        ("mutations", "state"),
        [
            pytest.param({"state": "CLOSED"}, "CLOSED", id="closed"),
            pytest.param({"state": None}, "", id="state-none"),
            pytest.param({"state": 123}, "", id="state-int"),
            # mutated from capture: state (a missing field reads as None)
            pytest.param({"state": None}, "", id="state-missing"),
        ],
    )
    def test_state(self, mutations: dict[str, object], state: str) -> None:
        ident = read_pr_identity(load_adopt_fixture(FIXTURE_CONFLICTING, **mutations))
        assert ident.is_open is False
        assert ident.state == state

    @pytest.mark.parametrize(
        ("url", "target"),
        [
            pytest.param("not a url", None, id="garbage"),
            pytest.param(123, None, id="non-string"),
            pytest.param(PR_999_URL, (REPO, 999), id="other-pr"),
            pytest.param(
                "https://github.com/acme/widgets/pull/2933",
                ("acme/widgets", 2933),
                id="other-repo",
            ),
        ],
    )
    def test_url_target(self, url: object, target: tuple[str, int] | None) -> None:
        ident = read_pr_identity(load_adopt_fixture(FIXTURE_CONFLICTING, url=url))
        assert ident.url_target == target

    @pytest.mark.parametrize(
        ("sha", "valid"),
        [
            pytest.param("abc1234", False, id="short"),
            pytest.param("BB649BBC53CD4972D5C8D4C50B11030AE3AB57AA", False, id="upper"),
            pytest.param("a" * 41, False, id="41-chars"),
            pytest.param("a" * 64, True, id="64-hex"),
            pytest.param(None, False, id="none"),
        ],
    )
    def test_head_sha(self, sha: object, valid: bool) -> None:
        ident = read_pr_identity(
            load_adopt_fixture(FIXTURE_CONFLICTING, headRefOid=sha)
        )
        assert ident.head_sha_valid is valid

    @pytest.mark.parametrize(
        ("ref", "error"),
        [
            pytest.param("", True, id="empty"),
            pytest.param("-x", True, id="leading-dash"),
            pytest.param("feat/issue#1", True, id="hash"),
            pytest.param("a b", True, id="space"),
            pytest.param("a..b", True, id="dotdot"),
            pytest.param("main", False, id="main"),
            pytest.param(None, True, id="non-string"),
        ],
    )
    def test_head_ref(self, ref: object, error: bool) -> None:
        ident = read_pr_identity(
            load_adopt_fixture(FIXTURE_CONFLICTING, headRefName=ref)
        )
        assert (ident.head_ref_error is not None) is error

    @pytest.mark.parametrize("ref", ["-x", "a b"])
    def test_base_ref_is_normalized_independently(self, ref: str) -> None:
        ident = read_pr_identity(
            load_adopt_fixture(FIXTURE_CONFLICTING, baseRefName=ref)
        )
        assert ident.base_ref == ref
        assert ident.base_ref_error is not None


class TestLayering:
    def test_dev_queue_does_not_import_pr_identity(self) -> None:
        offenders = [
            str(path)
            for path in DEV_QUEUE_SRC.rglob("*.py")
            if _imports_pr_identity(ast.parse(path.read_text(encoding="utf-8")))
        ]
        assert offenders == []


def _imports_pr_identity(tree: ast.AST) -> bool:
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module == "cw.pr_identity":
            return True
        if isinstance(node, ast.Import) and any(
            alias.name == "cw.pr_identity" for alias in node.names
        ):
            return True
    return False


#: (mutations, adopt message or None when adopt accepts, feedback message).
#: Messages are golden strings captured from the unrefactored callers.
CALLER_ROWS = [
    pytest.param(
        {"state": "CLOSED"},
        "Refusing to adopt PR #2933: it is CLOSED, not OPEN.",
        f"PR {REPO}#2933 is CLOSED, not OPEN",
        id="closed",
    ),
    pytest.param(
        {"state": None},
        "Refusing to adopt PR #2933: it is not OPEN, not OPEN.",
        f"PR {REPO}#2933 is not OPEN, not OPEN",
        id="state-none",
    ),
    pytest.param(
        {"headRefOid": "abc1234"},
        "Refusing to adopt PR #2933: headRefOid 'abc1234' is not a full SHA.",
        "headRefOid 'abc1234' is not a full SHA",
        id="sha-short",
    ),
    pytest.param(
        {"headRefOid": "BB649BBC53CD4972D5C8D4C50B11030AE3AB57AA"},
        "Refusing to adopt PR #2933: headRefOid"
        " 'BB649BBC53CD4972D5C8D4C50B11030AE3AB57AA' is not a full SHA.",
        "headRefOid 'BB649BBC53CD4972D5C8D4C50B11030AE3AB57AA' is not a full SHA",
        id="sha-upper",
    ),
    pytest.param(
        {"url": "not a url"},
        "Refusing to adopt PR #2933: unparseable PR url 'not a url'.",
        f"PR url 'not a url' does not name {REPO}#2933",
        id="url-garbage",
    ),
    pytest.param(
        {"url": PR_999_URL},
        f"Refusing to adopt PR #2933: PR url {PR_999_URL!r} names PR #999,"
        " not PR #2933.",
        f"PR url {PR_999_URL!r} does not name {REPO}#2933",
        id="url-other-pr",
    ),
    pytest.param(
        {"headRefName": "feat/issue#1"},
        _adopt_charset("feat/issue#1"),
        "head ref 'feat/issue#1' fails the safe-name check",
        id="head-ref-hash",
    ),
    pytest.param(
        {"headRefName": ""},
        _adopt_charset(""),
        "head ref '' fails the safe-name check",
        id="head-ref-empty",
    ),
    pytest.param(
        {"headRefName": "-x"},
        _adopt_charset("-x"),
        "head ref '-x' fails the safe-name check",
        id="head-ref-dash",
    ),
    pytest.param(
        {"baseRefName": "-x"},
        None,
        "base ref '-x' fails the safe-name check",
        id="base-ref-dash-adopt-accepts",
    ),
    pytest.param(
        {"headRefName": "a b", "headRefOid": "abc"},
        _adopt_charset("a b"),
        "headRefOid 'abc' is not a full SHA",
        id="precedence-head-ref-over-sha",
    ),
    pytest.param(
        {"url": "garbage", "headRefOid": "abc"},
        "Refusing to adopt PR #2933: headRefOid 'abc' is not a full SHA.",
        f"PR url 'garbage' does not name {REPO}#2933",
        id="precedence-sha-over-url",
    ),
    pytest.param(
        {"baseRefName": "-x", "headRefName": "a b"},
        _adopt_charset("a b"),
        "base ref '-x' fails the safe-name check",
        id="precedence-base-ref-differs",
    ),
    pytest.param(
        {
            "state": "CLOSED",
            "headRefName": "a b",
            "headRefOid": "abc",
            "url": "garbage",
        },
        "Refusing to adopt PR #2933: it is CLOSED, not OPEN.",
        f"PR {REPO}#2933 is CLOSED, not OPEN",
        id="precedence-state-wins",
    ),
    pytest.param(
        {"isCrossRepository": True, "state": "CLOSED"},
        "Refusing to adopt PR #2933: fork PRs are not adoptable (RFC 0016).",
        f"PR {REPO}#2933 is CLOSED, not OPEN",
        id="precedence-fork-first-in-adopt",
    ),
]


class TestEachCallerKeepsItsOwnText:
    @pytest.mark.parametrize(
        ("mutations", "adopt_message", "feedback_message"), CALLER_ROWS
    )
    def test_caller_refusals_are_unchanged(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        mutations: dict[str, object],
        adopt_message: str | None,
        feedback_message: str,
    ) -> None:
        repo = tmp_path / "repo"
        repo.mkdir()
        client = adopt_client(repo)
        monkeypatch.setattr("cw.adopt_pr.repo_slug_mismatch", lambda *_a: None)
        payload = load_adopt_fixture(FIXTURE_CONFLICTING, **mutations)
        stub_pr_view(monkeypatch, payload)
        stub_pr_view(monkeypatch, payload, target="cw.pr_feedback")

        if adopt_message is None:
            read_adopted_pr(
                client,
                PR_NUMBER,
                sleep=_noop_sleep,
                synthesize=False,
                operator_feedback=None,
            )
        else:
            with pytest.raises(AdoptionRefusedError) as adopt_exc:
                read_adopted_pr(
                    client,
                    PR_NUMBER,
                    sleep=_noop_sleep,
                    synthesize=False,
                    operator_feedback=None,
                )
            assert str(adopt_exc.value) == adopt_message

        with pytest.raises(FeedbackSynthesisError) as fb_exc:
            synthesize_pr_feedback(REPO, PR_NUMBER, cwd=tmp_path, now=lambda: FIXED_NOW)
        assert str(fb_exc.value) == feedback_message
