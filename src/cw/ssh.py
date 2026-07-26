"""SSH agent key preflight check (#927), with IdentityAgent resolution (#1436)."""

from __future__ import annotations

import os
import subprocess as _sp

_SSH_KEY_MARKERS = ("ED25519", "RSA")
_DEFAULT_SSH_HOST = "github.com"
_IDENTITYAGENT_LINE_PARTS = 2


def _resolve_identity_agent_sock(host: str, *, timeout: int) -> str | None:
    """Return the effective IdentityAgent socket path for *host* via ``ssh -G``.

    Returns None (not an error) when ssh -G reports no identityagent line,
    when it resolves to the literal sentinel "none", or when the ssh -G
    subprocess itself fails for any of the three reasons check_ssh_key_available
    already tolerates -- caller falls back to the unmodified ssh-add -l probe.
    """
    try:
        result = _sp.run(
            ["ssh", "-G", host],
            capture_output=True,
            text=True,
            check=False,
            timeout=timeout,
        )
    except FileNotFoundError:
        return None
    except (OSError, _sp.TimeoutExpired):
        return None
    for line in result.stdout.splitlines():
        parts = line.strip().split(None, 1)
        if (
            len(parts) == _IDENTITYAGENT_LINE_PARTS
            and parts[0].lower() == "identityagent"
        ):
            value = parts[1].strip()
            return None if value.lower() == "none" else value
    return None


def check_ssh_key_available(*, timeout: int = 5, host: str = _DEFAULT_SSH_HOST) -> bool:
    """Return True iff ``ssh-add -l`` reports at least one ED25519/RSA identity.

    Direct port of the ticket's ``ssh-add -l 2>/dev/null | grep -q
    "ED25519\\|RSA"``. Instant, local, no network (R1) -- unlike
    check_gh_availability there is no TTL cache. Fails closed (False) on a
    missing ssh-add binary, subprocess OSError, or timeout -- matching
    check_gh_availability's posture -- as well as on a clean-but-empty/no-agent
    response, since only stdout content (not returncode) determines the
    match, mirroring the shell pipeline exactly.

    Before probing ``ssh-add -l``, resolves the effective ``IdentityAgent``
    for *host* via ``ssh -G`` (#1436) so an agent configured only through
    ``~/.ssh/config`` (e.g. 1Password's SSH agent) is visible to the
    subprocess call, which otherwise only sees the inherited environment's
    ``SSH_AUTH_SOCK``. When ``ssh -G`` finds no ``identityagent`` line,
    resolves it to the literal sentinel ``none``, or fails outright, this
    falls back to the original unmodified ``ssh-add -l`` call (no env
    override) -- this is the common-default case, not an error path. Both
    ``ssh`` and ``ssh-add`` are local binaries; no network I/O is introduced.
    """
    agent_sock = _resolve_identity_agent_sock(host, timeout=timeout)
    try:
        if agent_sock is not None:
            env = {**os.environ, "SSH_AUTH_SOCK": agent_sock}
            result = _sp.run(
                ["ssh-add", "-l"],
                capture_output=True,
                text=True,
                check=False,
                timeout=timeout,
                env=env,
            )
        else:
            result = _sp.run(
                ["ssh-add", "-l"],
                capture_output=True,
                text=True,
                check=False,
                timeout=timeout,
            )
    except FileNotFoundError:
        return False
    except (OSError, _sp.TimeoutExpired):
        return False
    return any(marker in result.stdout for marker in _SSH_KEY_MARKERS)
