"""Tests for the cw install-kind classifier and its skip labels (#3579).

``_classify_cw_install`` distinguishes local, git, registry and malformed
installs from ``direct_url.json``. ``_resolve_cw_source_path`` maps that
classification onto the legacy return type. Git payloads here come from an
observed ``direct_url.json`` (a uv-cached cw 0.6.0 dist-info) with only the
commit sha redacted; the sparse variant reflects real payloads that omit
fields.
"""

from __future__ import annotations

import importlib.metadata
from pathlib import Path

import pytest

from cw.doctor._shared import CheckResult
from cw.doctor.versions import (
    _CW_REPO_URL,
    CwInstall,
    _classify_cw_install,
    _InstallKind,
    _resolve_cw_source_path,
)
from tests._shared.cli_preflight import _patch_cw_dist, _patch_cw_dist_not_found

# Observed capture (uv cache, claude_workspace-0.6.0.dist-info), sha redacted.
_GIT_PAYLOAD = (
    '{"url":"https://github.com/mattwwarren/claude-workspace.git",'
    '"vcs_info":{"vcs":"git","commit_id":"<redacted-sha>",'
    '"requested_revision":"v0.6.0"}}'
)
# Sparse git variant: real payloads omit fields.
_GIT_SPARSE_PAYLOAD = '{"url":"https://github.com/mattwwarren/claude-workspace.git","vcs_info":{"vcs":"git"}}'
# Observed local (editable) capture shape, path redacted to a tmp dir below.
_LOCAL_PAYLOAD_TEMPLATE = '{{"url":"file://{path}","dir_info":{{"editable":true}}}}'


def test_git_payload_classified_with_fields(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_cw_dist(monkeypatch, direct_url_text=_GIT_PAYLOAD, version="0.6.0")

    install = _classify_cw_install()

    assert install == CwInstall(
        kind=_InstallKind.GIT,
        source_path=None,
        url="https://github.com/mattwwarren/claude-workspace.git",
        requested_revision="v0.6.0",
        commit_id="<redacted-sha>",
    )


def test_sparse_git_payload_has_none_fields(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_cw_dist(monkeypatch, direct_url_text=_GIT_SPARSE_PAYLOAD)

    install = _classify_cw_install()

    assert install.kind is _InstallKind.GIT
    assert install.requested_revision is None
    assert install.commit_id is None
    assert install.url == "https://github.com/mattwwarren/claude-workspace.git"


def test_file_url_classified_local(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    payload = _LOCAL_PAYLOAD_TEMPLATE.format(path=str(tmp_path))
    _patch_cw_dist(monkeypatch, direct_url_text=payload)

    install = _classify_cw_install()

    assert install.kind is _InstallKind.LOCAL
    assert install.source_path == Path(str(tmp_path))


def test_percent_encoded_space_stays_encoded(monkeypatch: pytest.MonkeyPatch) -> None:
    # Behaviour is kept from today: urlparse().path, not unquoted.
    _patch_cw_dist(
        monkeypatch,
        direct_url_text='{"url":"file:///tmp/my%20repo","dir_info":{"editable":true}}',
    )

    install = _classify_cw_install()

    assert install.kind is _InstallKind.LOCAL
    assert install.source_path == Path("/tmp/my%20repo")


def test_https_archive_without_vcs_info_is_registry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_cw_dist(
        monkeypatch,
        direct_url_text='{"url":"https://files.example.test/cw-1.0.whl"}',
    )

    assert _classify_cw_install().kind is _InstallKind.REGISTRY


def test_absent_direct_url_is_registry(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_cw_dist(monkeypatch, direct_url_text=None)

    assert _classify_cw_install().kind is _InstallKind.REGISTRY


def test_package_not_found_is_registry(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_cw_dist_not_found(monkeypatch)

    assert _classify_cw_install().kind is _InstallKind.REGISTRY


def test_missing_url_key_is_registry(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_cw_dist(monkeypatch, direct_url_text='{"dir_info":{"editable":true}}')

    assert _classify_cw_install().kind is _InstallKind.REGISTRY


def test_non_str_url_is_registry(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_cw_dist(monkeypatch, direct_url_text='{"url":42}')

    assert _classify_cw_install().kind is _InstallKind.REGISTRY


def test_malformed_json_is_malformed(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_cw_dist(monkeypatch, direct_url_text="{not json")

    assert _classify_cw_install().kind is _InstallKind.MALFORMED


def test_non_object_json_is_malformed(monkeypatch: pytest.MonkeyPatch) -> None:
    # Valid JSON, not an object: the pre-#3579 code raised AttributeError here.
    _patch_cw_dist(monkeypatch, direct_url_text='["file:///x"]')

    assert _classify_cw_install().kind is _InstallKind.MALFORMED


def test_resolve_local_returns_path(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    payload = _LOCAL_PAYLOAD_TEMPLATE.format(path=str(tmp_path))
    _patch_cw_dist(monkeypatch, direct_url_text=payload)

    assert _resolve_cw_source_path() == Path(str(tmp_path))


def test_resolve_git_skip_names_revision(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_cw_dist(monkeypatch, direct_url_text=_GIT_PAYLOAD)

    result = _resolve_cw_source_path()

    assert result == CheckResult(
        "cw-version",
        ok=True,
        warn=False,
        detail="installed from git (v0.6.0); skipping source check",
    )


def test_resolve_git_revision_falls_back_to_commit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_cw_dist(
        monkeypatch,
        direct_url_text='{"url":"https://x.test/r.git","vcs_info":{"vcs":"git","commit_id":"abc1234"}}',
    )

    result = _resolve_cw_source_path()

    assert isinstance(result, CheckResult)
    assert result.detail == "installed from git (abc1234); skipping source check"


def test_resolve_git_revision_falls_back_to_unpinned(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_cw_dist(
        monkeypatch,
        direct_url_text='{"url":"https://x.test/r.git","vcs_info":{"vcs":"git"}}',
    )

    result = _resolve_cw_source_path()

    assert isinstance(result, CheckResult)
    assert result.detail == "installed from git (unpinned); skipping source check"


def test_resolve_registry_keeps_legacy_text(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_cw_dist(monkeypatch, direct_url_text=None)

    result = _resolve_cw_source_path()

    assert result == CheckResult(
        "cw-version",
        ok=True,
        warn=False,
        detail="installed from registry; skipping source check",
    )


def test_resolve_malformed_keeps_legacy_text(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_cw_dist(monkeypatch, direct_url_text="{not json")

    result = _resolve_cw_source_path()

    assert result == CheckResult(
        "cw-version",
        ok=True,
        warn=False,
        detail="malformed direct_url.json; skipping source check",
    )


@pytest.mark.parametrize(
    "check_name_fn",
    ["_check_cw_version", "_check_cw_deps", "_check_cw_deps_drift"],
)
def test_git_label_flows_through_version_checks(
    monkeypatch: pytest.MonkeyPatch, check_name_fn: str
) -> None:
    from cw.doctor import versions

    _patch_cw_dist(monkeypatch, direct_url_text=_GIT_PAYLOAD, version="0.6.0")

    result = getattr(versions, check_name_fn)()

    assert result.ok is True
    assert result.warn is False
    assert "installed from git (v0.6.0); skipping source check" in result.detail


def test_cw_repo_url_constant_matches_observed_origin() -> None:
    assert _CW_REPO_URL == "https://github.com/mattwwarren/claude-workspace.git"


def test_distribution_lookup_uses_package_name(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[str] = []

    class _Dist:
        version = "0.0.0"

        def read_text(self, _filename: str) -> None:
            return None

    def _fake(name: str) -> _Dist:
        seen.append(name)
        return _Dist()

    monkeypatch.setattr(importlib.metadata, "distribution", _fake)

    _classify_cw_install()

    assert seen == ["claude-workspace"]
