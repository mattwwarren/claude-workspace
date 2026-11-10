"""Tests for the bounded engine manifest loader (#3216, RFC 0018 A4)."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from typing import BinaryIO, Self

import pytest

from cw import review_engine_manifest_io as io_mod
from cw.exceptions import CwError
from cw.models.review_engine_manifest import (
    MAX_ENGINE_MANIFEST_BYTES,
    MAX_ENGINE_SPEC_BYTES,
    EngineManifest,
)
from cw.review_engine_manifest_io import (
    MAX_ISSUE_TEXT_CHARS,
    EngineManifestError,
    EngineManifestIssue,
    EngineManifestRejection,
    LoadedEngineManifest,
    load_engine_manifest,
    manifest_refusal_cause,
)
from cw.review_engine_summary_io import EngineSummaryIssue
from tests._review_engine_helpers import copy_engine_fixture, pad_to
from tests.test_review_engine_manifest import manifest_payload

STUB_DIR = Path(__file__).parent / "fixtures" / "review_engine_stub"
_SENTINEL = "SENTINEL-VALUE-5d2e"


def _write_manifest(
    directory: Path, payload: dict[str, object] | bytes | None = None
) -> Path:
    """A manifest plus the files the default payload names, under *directory*."""
    directory.mkdir(parents=True, exist_ok=True)
    for rel in ("agents/security.md", "agents/quality.md", "prompts/aggregate.md"):
        target = directory / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        if not target.exists():
            target.write_text(f"# {rel}\ntext\n", encoding="utf-8")
    body = manifest_payload() if payload is None else payload
    raw = body if isinstance(body, bytes) else json.dumps(body).encode("utf-8")
    path = directory / "cw-engine.json"
    path.write_bytes(raw)
    return path


class _SpyHandle:
    """A binary read handle that records every ``read`` size."""

    def __init__(self, handle: BinaryIO, sizes: list[int]) -> None:
        self._handle = handle
        self._sizes = sizes

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *exc: object) -> None:
        self._handle.close()

    def read(self, size: int = -1) -> bytes:
        self._sizes.append(size)
        return self._handle.read(size)


def _reject(path: Path) -> EngineManifestError:
    with pytest.raises(EngineManifestError) as excinfo:
        load_engine_manifest(path)
    return excinfo.value


def _reject_payload(tmp_path: Path, payload: dict[str, object]) -> EngineManifestError:
    return _reject(_write_manifest(tmp_path, payload))


def _only(error: EngineManifestError) -> EngineManifestIssue:
    assert len(error.issues) == 1
    return error.issues[0]


class TestRejectionEnum:
    def test_members(self) -> None:
        assert {member.value for member in EngineManifestRejection} == {
            "unreadable",
            "oversized",
            "not_utf8",
            "not_json",
            "duplicate_key",
            "not_object",
            "unsupported_manifest_version",
            "schema_invalid",
            "inconsistent",
            "path_invalid",
            "file_not_utf8",
            "file_blank",
            "file_oversized",
        }


class TestSuccess:
    def test_stub_fixture_loads(self) -> None:
        loaded = load_engine_manifest(STUB_DIR / "cw-engine.json")

        assert isinstance(loaded, LoadedEngineManifest)
        assert isinstance(loaded.manifest, EngineManifest)
        assert list(loaded.agent_specs) == ["security", "quality"]
        assert "Security role" in loaded.agent_specs["security"]
        assert loaded.aggregation_prompt.strip()

    def test_returns_texts_keyed_by_role_id_in_manifest_order(
        self, tmp_path: Path
    ) -> None:
        roles = [
            {"id": "zeta", "agent_spec": "agents/security.md"},
            {"id": "alpha", "agent_spec": "agents/quality.md"},
        ]
        path = _write_manifest(tmp_path, manifest_payload(roles=roles))
        (tmp_path / "agents" / "security.md").write_text("ZETA\n", encoding="utf-8")

        loaded = load_engine_manifest(path)

        assert list(loaded.agent_specs) == ["zeta", "alpha"]
        assert loaded.agent_specs["zeta"] == "ZETA\n"

    def test_reads_only_from_the_manifest_directory(self, tmp_path: Path) -> None:
        engine = tmp_path / "engine"
        path = _write_manifest(engine)
        (tmp_path / "agents").mkdir()
        (tmp_path / "agents" / "security.md").write_text("OUTSIDE", encoding="utf-8")
        elsewhere = tmp_path / "elsewhere"
        elsewhere.mkdir()

        loaded = load_engine_manifest(path)

        assert "OUTSIDE" not in loaded.agent_specs["security"]
        assert not list(elsewhere.iterdir())

    def test_in_directory_symlink_is_allowed(self, tmp_path: Path) -> None:
        path = _write_manifest(tmp_path)
        (tmp_path / "agents" / "alias.md").symlink_to("security.md")
        roles = [{"id": "security", "agent_spec": "agents/alias.md"}]
        path = _write_manifest(tmp_path, manifest_payload(roles=roles))

        assert (
            "agents/security.md" in load_engine_manifest(path).agent_specs["security"]
        )

    def test_file_exactly_at_the_cap_is_accepted(self, tmp_path: Path) -> None:
        path = _write_manifest(tmp_path)
        (tmp_path / "prompts" / "aggregate.md").write_text(
            "x" * MAX_ENGINE_SPEC_BYTES, encoding="utf-8"
        )

        assert len(load_engine_manifest(path).aggregation_prompt) == (
            MAX_ENGINE_SPEC_BYTES
        )


class TestStages:
    def test_missing_file_is_unreadable(self, tmp_path: Path) -> None:
        error = _reject(tmp_path / "absent.json")

        assert isinstance(error, CwError)
        assert error.reason is EngineManifestRejection.UNREADABLE
        assert error.field_path is None

    def test_directory_is_unreadable(self, tmp_path: Path) -> None:
        assert _reject(tmp_path).reason is EngineManifestRejection.UNREADABLE

    def test_oversized(self, tmp_path: Path) -> None:
        raw = pad_to(
            json.dumps(manifest_payload()).encode(), MAX_ENGINE_MANIFEST_BYTES + 1
        )

        error = _reject(_write_manifest(tmp_path, raw))

        assert error.reason is EngineManifestRejection.OVERSIZED
        assert error.field_path is None

    def test_exactly_at_cap_is_not_oversized(self, tmp_path: Path) -> None:
        raw = pad_to(json.dumps(manifest_payload()).encode(), MAX_ENGINE_MANIFEST_BYTES)

        assert load_engine_manifest(_write_manifest(tmp_path, raw)).manifest

    def test_reads_at_most_cap_plus_one_bytes(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        path = _write_manifest(tmp_path, b"{" + b" " * (MAX_ENGINE_MANIFEST_BYTES * 2))
        sizes: list[int] = []

        original = Path.open

        def spy(self: Path, mode: str = "r") -> _SpyHandle:
            return _SpyHandle(original(self, mode), sizes)

        monkeypatch.setattr(Path, "open", spy)

        _reject(path)

        assert sizes == [MAX_ENGINE_MANIFEST_BYTES + 1]

    def test_not_utf8(self, tmp_path: Path) -> None:
        error = _reject(_write_manifest(tmp_path, b'{"a": "\xff"}'))

        assert error.reason is EngineManifestRejection.NOT_UTF8
        assert error.field_path is None

    def test_not_json(self, tmp_path: Path) -> None:
        error = _reject(_write_manifest(tmp_path, b"{nope"))

        assert error.reason is EngineManifestRejection.NOT_JSON
        assert "line 1" in error.message

    def test_deeply_nested_json_is_not_json(self, tmp_path: Path) -> None:
        error = _reject(_write_manifest(tmp_path, b"[" * 100_000))

        assert error.reason is EngineManifestRejection.NOT_JSON
        assert "too deep" in error.message

    @pytest.mark.parametrize(
        ("raw", "kind"),
        [
            (b"[]", "array"),
            (b'"x"', "string"),
            (b"3", "number"),
            (b"null", "null"),
            (b"true", "boolean"),
        ],
    )
    def test_not_object(self, tmp_path: Path, raw: bytes, kind: str) -> None:
        error = _reject(_write_manifest(tmp_path, raw))

        assert error.reason is EngineManifestRejection.NOT_OBJECT
        assert kind in error.message

    def test_duplicate_top_level_key(self, tmp_path: Path) -> None:
        raw = b'{"manifest_version": 1, "manifest_version": 1}'

        error = _reject(_write_manifest(tmp_path, raw))

        assert error.reason is EngineManifestRejection.DUPLICATE_KEY
        assert error.field_path is None
        assert "manifest_version" in error.message

    def test_nested_duplicate_key(self, tmp_path: Path) -> None:
        raw = (
            b'{"manifest_version": 1, "backends": ["codex"], "roles": '
            b'[{"id": "a", "id": "b", "agent_spec": "x"}], '
            b'"aggregation_prompt": "p"}'
        )

        error = _reject(_write_manifest(tmp_path, raw))

        assert error.reason is EngineManifestRejection.DUPLICATE_KEY
        assert "'id'" in error.message

    @pytest.mark.parametrize("value", [2, 0, True, "1", 1.0, None])
    def test_wrong_version(self, tmp_path: Path, value: object) -> None:
        error = _reject_payload(tmp_path, manifest_payload(manifest_version=value))

        assert error.reason is EngineManifestRejection.UNSUPPORTED_MANIFEST_VERSION
        assert error.field_path == "manifest_version"

    def test_missing_version(self, tmp_path: Path) -> None:
        payload = manifest_payload()
        del payload["manifest_version"]

        error = _reject_payload(tmp_path, payload)

        assert error.reason is EngineManifestRejection.UNSUPPORTED_MANIFEST_VERSION
        assert "missing" in error.message

    def test_version_is_checked_before_field_validation(self, tmp_path: Path) -> None:
        error = _reject_payload(
            tmp_path, manifest_payload(manifest_version=9, backends=["opencode"])
        )

        assert error.reason is EngineManifestRejection.UNSUPPORTED_MANIFEST_VERSION

    def test_unknown_key(self, tmp_path: Path) -> None:
        error = _reject_payload(tmp_path, manifest_payload(schema_version=1))

        assert error.reason is EngineManifestRejection.SCHEMA_INVALID
        assert error.field_path == "schema_version"

    def test_dotted_field_paths(self, tmp_path: Path) -> None:
        roles = [
            {"id": "security", "agent_spec": "a.md"},
            {"id": "Bad", "agent_spec": "a"},
        ]

        error = _reject_payload(tmp_path, manifest_payload(roles=roles))

        assert error.field_path == "roles.1.id"

    def test_aggregation_prompt_missing(self, tmp_path: Path) -> None:
        payload = manifest_payload()
        del payload["aggregation_prompt"]

        error = _reject_payload(tmp_path, payload)

        assert error.reason is EngineManifestRejection.SCHEMA_INVALID
        assert error.field_path == "aggregation_prompt"

    def test_validation_never_echoes_input_values(self, tmp_path: Path) -> None:
        payload = manifest_payload(backends=[_SENTINEL], extra_key=_SENTINEL)

        error = _reject_payload(tmp_path, payload)

        text = " ".join([str(error), *(i.message for i in error.issues)])
        assert _SENTINEL not in text


class TestSlugCollision:
    @pytest.mark.parametrize(
        ("first", "second"),
        [("a-b", "a--b"), ("a", "a-"), ("a", "a"), ("x-y", "x-y-")],
    )
    def test_collision(self, tmp_path: Path, first: str, second: str) -> None:
        roles = [
            {"id": first, "agent_spec": "agents/security.md"},
            {"id": second, "agent_spec": "agents/quality.md"},
        ]

        error = _reject_payload(tmp_path, manifest_payload(roles=roles))

        assert error.reason is EngineManifestRejection.INCONSISTENT
        assert error.field_path == "roles.1.id"
        assert "roles.0.id" in error.message

    def test_every_later_collision_is_reported(self, tmp_path: Path) -> None:
        roles = [
            {"id": "a-b", "agent_spec": "agents/security.md"},
            {"id": "a--b", "agent_spec": "agents/security.md"},
            {"id": "a---b", "agent_spec": "agents/security.md"},
        ]

        error = _reject_payload(tmp_path, manifest_payload(roles=roles))

        assert [i.field_path for i in error.issues] == ["roles.1.id", "roles.2.id"]


class TestPathRules:
    def _roles(self, ref: str) -> list[dict[str, str]]:
        return [{"id": "security", "agent_spec": ref}]

    @pytest.mark.parametrize(
        "ref",
        [
            "/etc/passwd",
            "../outside.md",
            "agents/../../outside.md",
            "a/./../..",
            "..",
            ".",
        ],
    )
    def test_spec_path_rejected(self, tmp_path: Path, ref: str) -> None:
        (tmp_path.parent / "outside.md").write_text("x", encoding="utf-8")

        error = _reject_payload(tmp_path, manifest_payload(roles=self._roles(ref)))

        assert error.reason is EngineManifestRejection.PATH_INVALID
        assert error.field_path == "roles.0.agent_spec"

    def test_aggregation_prompt_path_rejected(self, tmp_path: Path) -> None:
        error = _reject_payload(
            tmp_path, manifest_payload(aggregation_prompt="/etc/passwd")
        )

        assert error.reason is EngineManifestRejection.PATH_INVALID
        assert error.field_path == "aggregation_prompt"

    def test_symlink_escaping_the_directory(self, tmp_path: Path) -> None:
        outside = tmp_path / "outside.md"
        outside.write_text("secret", encoding="utf-8")
        engine = tmp_path / "engine"
        _write_manifest(engine)
        (engine / "agents" / "evil.md").symlink_to(outside)

        error = _reject_payload_at(
            engine, manifest_payload(roles=self._roles("agents/evil.md"))
        )

        assert error.reason is EngineManifestRejection.PATH_INVALID
        assert "outside" in error.message
        assert "secret" not in str(error)

    def test_symlinked_directory_escaping(self, tmp_path: Path) -> None:
        outside = tmp_path / "outside"
        outside.mkdir()
        (outside / "spec.md").write_text("secret", encoding="utf-8")
        engine = tmp_path / "engine"
        _write_manifest(engine)
        (engine / "linked").symlink_to(outside, target_is_directory=True)

        error = _reject_payload_at(
            engine, manifest_payload(roles=self._roles("linked/spec.md"))
        )

        assert error.reason is EngineManifestRejection.PATH_INVALID

    def test_symlink_to_a_directory(self, tmp_path: Path) -> None:
        engine = tmp_path / "engine"
        _write_manifest(engine)
        (engine / "dirlink").symlink_to("agents", target_is_directory=True)

        error = _reject_payload_at(
            engine, manifest_payload(roles=self._roles("dirlink"))
        )

        assert error.reason is EngineManifestRejection.PATH_INVALID
        assert "regular file" in error.message

    def test_missing_file(self, tmp_path: Path) -> None:
        error = _reject_payload(
            tmp_path, manifest_payload(roles=self._roles("agents/nope.md"))
        )

        assert error.reason is EngineManifestRejection.PATH_INVALID
        assert "does not exist" in error.message

    def test_aggregation_prompt_missing_file(self, tmp_path: Path) -> None:
        error = _reject_payload(
            tmp_path, manifest_payload(aggregation_prompt="prompts/nope.md")
        )

        assert error.reason is EngineManifestRejection.PATH_INVALID
        assert error.field_path == "aggregation_prompt"

    def test_directory_is_not_a_regular_file(self, tmp_path: Path) -> None:
        error = _reject_payload(tmp_path, manifest_payload(roles=self._roles("agents")))

        assert error.reason is EngineManifestRejection.PATH_INVALID

    def test_symlink_loop(self, tmp_path: Path) -> None:
        engine = tmp_path / "engine"
        _write_manifest(engine)
        (engine / "loop.md").symlink_to("loop.md")

        error = _reject_payload_at(
            engine, manifest_payload(roles=self._roles("loop.md"))
        )

        assert error.reason is EngineManifestRejection.PATH_INVALID

    def test_nul_byte_in_path(self, tmp_path: Path) -> None:
        error = _reject_payload(
            tmp_path, manifest_payload(roles=self._roles("agents/a\x00b.md"))
        )

        assert error.reason is EngineManifestRejection.PATH_INVALID

    def test_unreadable_file(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        path = _write_manifest(tmp_path)
        target = (tmp_path / "agents" / "security.md").resolve()

        original = Path.open

        def deny(self: Path, mode: str = "r") -> object:
            if self == target:
                raise PermissionError(13, "denied")
            return original(self, mode)

        monkeypatch.setattr(Path, "open", deny)

        error = _reject(path)

        assert error.reason is EngineManifestRejection.PATH_INVALID
        assert error.field_path == "roles.0.agent_spec"

    def test_file_not_utf8(self, tmp_path: Path) -> None:
        path = _write_manifest(tmp_path)
        (tmp_path / "agents" / "security.md").write_bytes(b"\xff\xfe")

        error = _reject(path)

        assert error.reason is EngineManifestRejection.FILE_NOT_UTF8
        assert error.field_path == "roles.0.agent_spec"

    @pytest.mark.parametrize("text", ["", "   \n\t \n"])
    def test_file_blank(self, tmp_path: Path, text: str) -> None:
        path = _write_manifest(tmp_path)
        (tmp_path / "prompts" / "aggregate.md").write_text(text, encoding="utf-8")

        error = _reject(path)

        assert error.reason is EngineManifestRejection.FILE_BLANK
        assert error.field_path == "aggregation_prompt"

    def test_file_oversized(self, tmp_path: Path) -> None:
        path = _write_manifest(tmp_path)
        (tmp_path / "agents" / "quality.md").write_text(
            "x" * (MAX_ENGINE_SPEC_BYTES + 1), encoding="utf-8"
        )

        error = _reject(path)

        assert error.reason is EngineManifestRejection.FILE_OVERSIZED
        assert error.field_path == "roles.1.agent_spec"

    def test_oversized_multibyte_file_is_oversized_not_non_utf8(
        self, tmp_path: Path
    ) -> None:
        path = _write_manifest(tmp_path)
        text = "é" * (MAX_ENGINE_SPEC_BYTES // 2 + 1)
        (tmp_path / "agents" / "quality.md").write_text(text, encoding="utf-8")

        assert _reject(path).reason is EngineManifestRejection.FILE_OVERSIZED

    def test_every_file_failure_is_reported(self, tmp_path: Path) -> None:
        path = _write_manifest(tmp_path)
        (tmp_path / "agents" / "security.md").write_text("", encoding="utf-8")
        (tmp_path / "agents" / "quality.md").unlink()
        (tmp_path / "prompts" / "aggregate.md").write_bytes(b"\xff")

        error = _reject(path)

        assert [(i.reason.value, i.field_path) for i in error.issues] == [
            ("file_blank", "roles.0.agent_spec"),
            ("path_invalid", "roles.1.agent_spec"),
            ("file_not_utf8", "aggregation_prompt"),
        ]
        assert error.reason is EngineManifestRejection.FILE_BLANK
        assert str(error).startswith("3 validation issue(s); first: ")


def _reject_payload_at(
    directory: Path, payload: dict[str, object]
) -> EngineManifestError:
    return _reject(_write_manifest(directory, payload))


class TestIssueShape:
    def test_error_carries_the_first_issue(self, tmp_path: Path) -> None:
        error = _reject(tmp_path / "absent.json")

        first = _only(error)
        assert (error.reason, error.field_path, error.message) == (
            first.reason,
            first.field_path,
            first.message,
        )
        assert str(error) == error.message
        assert isinstance(error.issues, list)

    def test_issue_fields_match_the_summary_loader_shape(self) -> None:
        assert list(EngineManifestIssue.__dataclass_fields__) == list(
            EngineSummaryIssue.__dataclass_fields__
        )

    def test_schema_invalid_issues_sort_first(self, tmp_path: Path) -> None:
        issues = [
            io_mod._issue(EngineManifestRejection.INCONSISTENT, "late", "roles.1.id"),
            io_mod._issue(EngineManifestRejection.SCHEMA_INVALID, "first", "backends"),
            io_mod._issue(EngineManifestRejection.PATH_INVALID, "other", "x"),
            io_mod._issue(EngineManifestRejection.SCHEMA_INVALID, "second", "roles"),
        ]

        error = io_mod._reject_all(issues)

        assert [i.message for i in error.issues] == ["first", "second", "late", "other"]
        assert error.reason is EngineManifestRejection.SCHEMA_INVALID
        assert error.field_path == "backends"

    def test_many_pydantic_issues(self, tmp_path: Path) -> None:
        roles = [{"id": "BAD", "agent_spec": "a.md"} for _ in range(5)]

        error = _reject_payload(tmp_path, manifest_payload(roles=roles))

        assert len(error.issues) == 5
        assert {i.reason for i in error.issues} == {
            EngineManifestRejection.SCHEMA_INVALID
        }
        assert str(error).startswith("5 validation issue(s); first: ")

    def test_text_is_sanitized_and_capped(self) -> None:
        issue = io_mod._issue(
            EngineManifestRejection.PATH_INVALID,
            "bad\x00\x1b[31m" + "y" * 500,
            "roles.0." + "k" * 500,
        )

        assert "\x00" not in issue.message
        assert "\x1b" not in issue.message
        assert len(issue.message) == MAX_ISSUE_TEXT_CHARS
        assert issue.field_path is not None
        assert len(issue.field_path) == MAX_ISSUE_TEXT_CHARS

    def test_engine_controlled_key_names_are_sanitized(self, tmp_path: Path) -> None:
        raw = json.dumps({"manifest_version": 1, "evil\u0007key": 1}).encode()

        error = _reject(_write_manifest(tmp_path, raw))

        assert error.field_path is not None
        assert "\u0007" not in error.field_path


class TestRefusalCause:
    @pytest.mark.parametrize(
        ("field_path", "expected"),
        [
            ("backends", "codex_not_declared"),
            ("backends.0", "codex_not_declared"),
            ("backends.3", "codex_not_declared"),
            ("backendsx", "manifest_invalid"),
            ("roles.0.id", "manifest_invalid"),
            ("manifest_version", "manifest_invalid"),
            ("aggregation_prompt", "manifest_invalid"),
            (None, "manifest_invalid"),
        ],
    )
    def test_mapping(self, field_path: str | None, expected: str) -> None:
        error = EngineManifestError(
            [
                EngineManifestIssue(
                    EngineManifestRejection.SCHEMA_INVALID, field_path, "m"
                )
            ]
        )

        assert manifest_refusal_cause(error) == expected

    @pytest.mark.parametrize(
        ("backends", "expected"),
        [
            ([], "codex_not_declared"),
            (["opencode"], "codex_not_declared"),
            ("codex", "codex_not_declared"),
        ],
    )
    def test_loader_errors_map(
        self, tmp_path: Path, backends: object, expected: str
    ) -> None:
        error = _reject_payload(tmp_path, manifest_payload(backends=backends))

        assert manifest_refusal_cause(error) == expected

    def test_other_loader_errors_are_manifest_invalid(self, tmp_path: Path) -> None:
        error = _reject_payload(tmp_path, manifest_payload(roles=[]))

        assert manifest_refusal_cause(error) == "manifest_invalid"

    def test_backends_error_sorted_first_wins(self, tmp_path: Path) -> None:
        # Both a roles and a backends malformation: the first issue decides.
        payload = manifest_payload(roles=[], backends=["opencode"])

        error = _reject_payload(tmp_path, payload)

        assert error.field_path == "backends.0"
        assert manifest_refusal_cause(error) == "codex_not_declared"


def test_loader_never_imports_codex_review() -> None:
    code = (
        "import sys; import cw.review_engine_manifest_io; "
        "bad = [m for m in sys.modules if m.startswith('cw.codex_review')]; "
        "print(bad); sys.exit(1 if bad else 0)"
    )
    completed = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, check=False
    )

    assert completed.returncode == 0, completed.stdout + completed.stderr


def test_stub_directory_copy_loads(tmp_path: Path) -> None:
    copy = copy_engine_fixture(tmp_path, STUB_DIR, name="copy")

    assert load_engine_manifest(copy / "cw-engine.json").manifest.roles
