"""Build and install the Zellij WASM status bar plugin."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

from cw.exceptions import CwError

PLUGIN_FILENAME = "cw_status.wasm"
WASM_TARGET = "wasm32-wasip1"
WASM_REL_PATH = Path("target") / WASM_TARGET / "release" / PLUGIN_FILENAME
INSTALL_DIR = Path.home() / ".config" / "zellij" / "plugins"


def _find_plugin_dir() -> Path | None:
    """Locate the zellij-plugin source directory relative to this package.

    Returns the path if Cargo.toml exists (editable/source install),
    or None if running from an installed wheel.
    """
    candidate = Path(__file__).resolve().parent.parent.parent / "zellij-plugin"
    if (candidate / "Cargo.toml").exists():
        return candidate
    return None


def build_plugin(plugin_dir: Path | None = None) -> Path:
    """Build the Zellij plugin WASM binary.

    Returns the path to the built .wasm file.

    Raises CwError if cargo is not found or build fails.
    """
    src = plugin_dir or _find_plugin_dir()
    if src is None:
        msg = (
            "Cannot auto-detect plugin source directory "
            "(not an editable install). Pass --plugin-dir explicitly."
        )
        raise CwError(msg)

    cargo_toml = src / "Cargo.toml"
    if not cargo_toml.exists():
        msg = f"Cargo.toml not found at {cargo_toml}"
        raise CwError(msg)

    if not shutil.which("cargo"):
        msg = "cargo is not installed — install Rust via https://rustup.rs"
        raise CwError(msg)

    try:
        subprocess.run(
            ["cargo", "build", "--target", WASM_TARGET, "--release"],
            cwd=str(src),
            check=True,
            capture_output=True,
            text=True,
        )
    except subprocess.CalledProcessError as e:
        msg = f"Plugin build failed:\n{e.stderr}"
        raise CwError(msg) from e

    wasm_path = src / WASM_REL_PATH
    if not wasm_path.exists():
        msg = f"Build succeeded but WASM file not found at {wasm_path}"
        raise CwError(msg)

    return wasm_path


def install_plugin(
    wasm_path: Path | None = None,
    install_dir: Path | None = None,
    plugin_dir: Path | None = None,
) -> Path:
    """Copy the built WASM plugin to Zellij's plugin directory.

    Returns the installed path.

    Raises CwError if the source WASM doesn't exist.
    """
    resolved_dir = plugin_dir or _find_plugin_dir()
    default_wasm = resolved_dir / WASM_REL_PATH if resolved_dir is not None else None
    src = wasm_path or default_wasm
    if src is None:
        msg = (
            "Cannot auto-detect WASM path "
            "(not an editable install). Pass the wasm_path explicitly."
        )
        raise CwError(msg)

    dest_dir = install_dir or INSTALL_DIR

    if not src.exists():
        msg = f"WASM file not found at {src} — run 'cw plugin build' first"
        raise CwError(msg)

    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / PLUGIN_FILENAME
    shutil.copy2(str(src), str(dest))
    return dest
