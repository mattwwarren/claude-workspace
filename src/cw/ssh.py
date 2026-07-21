"""SSH agent key preflight check (#927)."""

from __future__ import annotations

import subprocess as _sp

_SSH_KEY_MARKERS = ("ED25519", "RSA")


def check_ssh_key_available(*, timeout: int = 5) -> bool:
    """Return True iff ``ssh-add -l`` reports at least one ED25519/RSA identity.

    Direct port of the ticket's ``ssh-add -l 2>/dev/null | grep -q
    "ED25519\\|RSA"``. Instant, local, no network (R1) -- unlike
    check_gh_availability there is no TTL cache. Fails closed (False) on a
    missing ssh-add binary, subprocess OSError, or timeout -- matching
    check_gh_availability's posture -- as well as on a clean-but-empty/no-agent
    response, since only stdout content (not returncode) determines the
    match, mirroring the shell pipeline exactly.
    """
    try:
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
