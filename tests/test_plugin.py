"""Tests for cw.plugin — Zellij WASM plugin build and install."""

from __future__ import annotations

import subprocess
from typing import TYPE_CHECKING
from unittest.mock import MagicMock

import pytest
from click.testing import CliRunner

import cw.plugin
from cw.cli import main
from cw.exceptions import CwError
from cw.plugin import WASM_REL_PATH, _find_plugin_dir, build_plugin, install_plugin

if TYPE_CHECKING:
    from pathlib import Path


class TestFindPluginDir:
    def test_returns_path_when_cargo_toml_exists(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        """Returns the candidate directory when Cargo.toml exists."""
        # Set up: tmp_path/zellij-plugin/Cargo.toml exists
        plugin_dir = tmp_path / "zellij-plugin"
        plugin_dir.mkdir()
        (plugin_dir / "Cargo.toml").touch()

        # Fake __file__ so parent.parent.parent = tmp_path
        fake_file = tmp_path / "src" / "cw" / "plugin.py"
        fake_file.parent.mkdir(parents=True)
        fake_file.touch()
        monkeypatch.setattr(cw.plugin, "__file__", str(fake_file))

        result = _find_plugin_dir()

        assert result == plugin_dir

    def test_returns_none_when_no_cargo_toml(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        """Returns None when Cargo.toml doesn't exist (wheel install)."""
        fake_file = tmp_path / "src" / "cw" / "plugin.py"
        fake_file.parent.mkdir(parents=True)
        fake_file.touch()
        monkeypatch.setattr(cw.plugin, "__file__", str(fake_file))

        result = _find_plugin_dir()

        assert result is None


class TestBuildPlugin:
    def test_missing_cargo_toml(self, tmp_path: Path) -> None:
        """Raises CwError when Cargo.toml doesn't exist."""
        with pytest.raises(CwError, match=r"Cargo\.toml not found"):
            build_plugin(plugin_dir=tmp_path)

    def test_cargo_not_installed(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Raises CwError when cargo binary is missing."""
        (tmp_path / "Cargo.toml").touch()
        monkeypatch.setattr("cw.plugin.shutil.which", lambda _cmd: None)

        with pytest.raises(CwError, match="cargo is not installed"):
            build_plugin(plugin_dir=tmp_path)

    def test_cargo_build_failure(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Raises CwError when cargo build fails."""
        (tmp_path / "Cargo.toml").touch()
        monkeypatch.setattr(
            "cw.plugin.shutil.which",
            lambda _cmd: "/usr/bin/cargo",
        )
        monkeypatch.setattr(
            "cw.plugin.subprocess.run",
            MagicMock(
                side_effect=subprocess.CalledProcessError(
                    1,
                    "cargo",
                    stderr="compilation error",
                ),
            ),
        )

        with pytest.raises(CwError, match="Plugin build failed"):
            build_plugin(plugin_dir=tmp_path)

    def test_successful_build(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Returns WASM path on successful build."""
        (tmp_path / "Cargo.toml").touch()
        wasm_dir = tmp_path / WASM_REL_PATH.parent
        wasm_dir.mkdir(parents=True)
        wasm_file = tmp_path / WASM_REL_PATH
        wasm_file.write_bytes(b"\x00asm")

        monkeypatch.setattr(
            "cw.plugin.shutil.which",
            lambda _cmd: "/usr/bin/cargo",
        )
        monkeypatch.setattr("cw.plugin.subprocess.run", MagicMock())

        result = build_plugin(plugin_dir=tmp_path)

        assert result == wasm_file

    def test_build_succeeds_but_wasm_not_found(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Raises CwError when cargo succeeds but WASM file is missing."""
        (tmp_path / "Cargo.toml").touch()
        monkeypatch.setattr(
            "cw.plugin.shutil.which",
            lambda _cmd: "/usr/bin/cargo",
        )
        monkeypatch.setattr("cw.plugin.subprocess.run", MagicMock())

        with pytest.raises(
            CwError,
            match=r"Build succeeded but WASM file not found",
        ):
            build_plugin(plugin_dir=tmp_path)

    def test_no_plugin_dir_no_editable(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Raises CwError when plugin_dir is None and auto-detect fails."""
        monkeypatch.setattr("cw.plugin._find_plugin_dir", lambda: None)

        with pytest.raises(CwError, match="Cannot auto-detect plugin source directory"):
            build_plugin(plugin_dir=None)


class TestInstallPlugin:
    def test_missing_wasm(self, tmp_path: Path) -> None:
        """Raises CwError when WASM file doesn't exist."""
        missing = tmp_path / "nonexistent.wasm"
        with pytest.raises(CwError, match="WASM file not found"):
            install_plugin(wasm_path=missing)

    def test_successful_install(self, tmp_path: Path) -> None:
        """Copies WASM to install directory."""
        wasm = tmp_path / "cw_status.wasm"
        wasm.write_bytes(b"\x00asm")
        dest_dir = tmp_path / "plugins"

        result = install_plugin(wasm_path=wasm, install_dir=dest_dir)

        assert result == dest_dir / "cw_status.wasm"
        assert result.exists()
        assert result.read_bytes() == b"\x00asm"

    def test_install_creates_directory(self, tmp_path: Path) -> None:
        """Creates the install directory if it doesn't exist."""
        wasm = tmp_path / "cw_status.wasm"
        wasm.write_bytes(b"\x00asm")
        dest_dir = tmp_path / "deep" / "nested" / "plugins"

        result = install_plugin(wasm_path=wasm, install_dir=dest_dir)

        assert result.exists()
        assert dest_dir.is_dir()

    def test_install_overwrites_existing(self, tmp_path: Path) -> None:
        """Silently overwrites an existing plugin file."""
        wasm = tmp_path / "cw_status.wasm"
        wasm.write_bytes(b"\x00asm-v2")
        dest_dir = tmp_path / "plugins"
        dest_dir.mkdir()
        existing = dest_dir / "cw_status.wasm"
        existing.write_bytes(b"\x00asm-v1")

        result = install_plugin(wasm_path=wasm, install_dir=dest_dir)

        assert result.read_bytes() == b"\x00asm-v2"

    def test_default_install_dir(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Uses INSTALL_DIR when install_dir is None."""
        default_dir = tmp_path / "default-plugins"
        monkeypatch.setattr("cw.plugin.INSTALL_DIR", default_dir)

        wasm = tmp_path / "cw_status.wasm"
        wasm.write_bytes(b"\x00asm")

        result = install_plugin(wasm_path=wasm, install_dir=None)

        assert result == default_dir / "cw_status.wasm"
        assert result.exists()

    def test_no_wasm_no_editable(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Raises CwError when wasm_path is None and auto-detect fails."""
        monkeypatch.setattr("cw.plugin._find_plugin_dir", lambda: None)

        with pytest.raises(CwError, match="Cannot auto-detect WASM path"):
            install_plugin(wasm_path=None)

    def test_install_auto_detects_wasm(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Auto-detects WASM path via _find_plugin_dir when wasm_path is None."""
        plugin_dir = tmp_path / "plugin-src"
        wasm_file = plugin_dir / WASM_REL_PATH
        wasm_file.parent.mkdir(parents=True)
        wasm_file.write_bytes(b"\x00asm")
        monkeypatch.setattr("cw.plugin._find_plugin_dir", lambda: plugin_dir)

        dest_dir = tmp_path / "dest"
        result = install_plugin(wasm_path=None, install_dir=dest_dir)

        assert result.exists()
        assert result.read_bytes() == b"\x00asm"

    def test_install_uses_explicit_plugin_dir(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Uses explicit plugin_dir to locate WASM when wasm_path is None."""
        # Make auto-detect fail
        monkeypatch.setattr("cw.plugin._find_plugin_dir", lambda: None)

        # Set up plugin_dir with built WASM
        plugin_dir = tmp_path / "plugin-src"
        wasm_file = plugin_dir / WASM_REL_PATH
        wasm_file.parent.mkdir(parents=True)
        wasm_file.write_bytes(b"\x00asm")

        dest_dir = tmp_path / "dest"

        result = install_plugin(
            wasm_path=None,
            install_dir=dest_dir,
            plugin_dir=plugin_dir,
        )

        assert result.exists()
        assert result.read_bytes() == b"\x00asm"


class TestPluginCLI:
    def test_plugin_build_success(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        """'cw plugin build' exits 0 and prints the built path."""
        wasm_path = tmp_path / "cw_status.wasm"
        mock_build = MagicMock(return_value=wasm_path)
        monkeypatch.setattr("cw.plugin.build_plugin", mock_build)

        runner = CliRunner()
        result = runner.invoke(main, ["plugin", "build"])

        assert result.exit_code == 0
        assert str(wasm_path) in result.output
        mock_build.assert_called_once_with(plugin_dir=None)

    def test_plugin_build_error(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """'cw plugin build' reports CwError via handle_errors."""
        mock_build = MagicMock(side_effect=CwError("cargo is not installed"))
        monkeypatch.setattr("cw.plugin.build_plugin", mock_build)

        runner = CliRunner()
        result = runner.invoke(main, ["plugin", "build"])

        assert result.exit_code == 1
        assert "cargo is not installed" in result.output

    def test_plugin_build_with_plugin_dir(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        """'cw plugin build --plugin-dir' passes directory to build_plugin."""
        plugin_src = tmp_path / "zellij-plugin"
        plugin_src.mkdir()
        wasm_path = tmp_path / "cw_status.wasm"
        mock_build = MagicMock(return_value=wasm_path)
        monkeypatch.setattr("cw.plugin.build_plugin", mock_build)

        runner = CliRunner()
        result = runner.invoke(
            main,
            ["plugin", "build", "--plugin-dir", str(plugin_src)],
        )

        assert result.exit_code == 0
        mock_build.assert_called_once_with(plugin_dir=plugin_src)

    def test_plugin_install_success(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        """'cw plugin install' builds then installs."""
        wasm_path = tmp_path / "cw_status.wasm"
        dest_path = tmp_path / "plugins" / "cw_status.wasm"

        mock_build = MagicMock(return_value=wasm_path)
        mock_install = MagicMock(return_value=dest_path)
        monkeypatch.setattr("cw.plugin.build_plugin", mock_build)
        monkeypatch.setattr("cw.plugin.install_plugin", mock_install)

        runner = CliRunner()
        result = runner.invoke(main, ["plugin", "install"])

        assert result.exit_code == 0
        assert str(dest_path) in result.output
        mock_build.assert_called_once_with(plugin_dir=None)
        mock_install.assert_called_once_with(
            wasm_path=wasm_path,
            plugin_dir=None,
        )

    def test_plugin_install_no_build(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        """'cw plugin install --no-build' skips build_plugin."""
        dest_path = tmp_path / "plugins" / "cw_status.wasm"

        mock_build = MagicMock()
        mock_install = MagicMock(return_value=dest_path)
        monkeypatch.setattr("cw.plugin.build_plugin", mock_build)
        monkeypatch.setattr("cw.plugin.install_plugin", mock_install)

        runner = CliRunner()
        result = runner.invoke(main, ["plugin", "install", "--no-build"])

        assert result.exit_code == 0
        mock_build.assert_not_called()
        mock_install.assert_called_once_with(
            wasm_path=None,
            plugin_dir=None,
        )

    def test_plugin_install_with_plugin_dir(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        """'cw plugin install --plugin-dir' passes dir to both functions."""
        plugin_src = tmp_path / "zellij-plugin"
        plugin_src.mkdir()
        wasm_path = tmp_path / "cw_status.wasm"
        dest_path = tmp_path / "plugins" / "cw_status.wasm"

        mock_build = MagicMock(return_value=wasm_path)
        mock_install = MagicMock(return_value=dest_path)
        monkeypatch.setattr("cw.plugin.build_plugin", mock_build)
        monkeypatch.setattr("cw.plugin.install_plugin", mock_install)

        runner = CliRunner()
        result = runner.invoke(
            main,
            ["plugin", "install", "--plugin-dir", str(plugin_src)],
        )

        assert result.exit_code == 0
        mock_build.assert_called_once_with(plugin_dir=plugin_src)
        mock_install.assert_called_once_with(
            wasm_path=wasm_path,
            plugin_dir=plugin_src,
        )
