"""SSH agent key preflight check (#927), with IdentityAgent resolution (#1436).

Also owns the push-remote scheme classification (#1495) that decides whether
the probe applies to a client at all: an HTTP(S) or local-path remote never
needs an SSH key, so the gate must not hold such a client PENDING on the
state of an ssh-agent it will never use.
"""

from __future__ import annotations

import os
import re
import subprocess as _sp
from typing import TYPE_CHECKING, Literal

if TYPE_CHECKING:
    from pathlib import Path

_SSH_KEY_MARKERS = ("ED25519", "RSA")
_DEFAULT_SSH_HOST = "github.com"
_IDENTITYAGENT_LINE_PARTS = 2

# Push-remote transport classification (#1495). ``unknown`` covers every
# unresolvable case (not a git dir, no such remote, git missing, timeout,
# an unrecognised URL shape) and is treated like ``ssh`` by the gate -- the
# probe is engaged -- so a resolution failure degrades to pre-#1495
# behaviour rather than silently disabling the gate.
RemoteScheme = Literal["ssh", "http", "local", "unknown"]

# Schemes whose pushes never touch an ssh-agent, so the SSH-key probe is
# skipped for clients using them.
_SCHEMES_WITHOUT_SSH: frozenset[str] = frozenset({"http", "local"})

_HTTP_URL_PREFIXES = ("http://", "https://")
_SSH_URL_PREFIXES = ("ssh://", "git+ssh://", "ssh+git://")
_LOCAL_URL_PREFIXES = ("file://", "/", "./", "../")
# scp-like syntax (``[user@]host:path``): no ``://``, and a colon before the
# first slash. Matches ``git@github.com:owner/repo.git`` and ``host:path``.
_SCP_LIKE_URL_RE = re.compile(r"^(?:[^@/:]+@)?[^/:]+:")


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


def _classify_remote_url(url: str) -> RemoteScheme:
    """Classify a git remote URL into a :data:`RemoteScheme`."""
    lowered = url.strip().lower()
    if lowered.startswith(_HTTP_URL_PREFIXES):
        return "http"
    if lowered.startswith(_SSH_URL_PREFIXES):
        return "ssh"
    if lowered.startswith(_LOCAL_URL_PREFIXES):
        return "local"
    # scp-like syntax has no scheme separator at all; anything with ``://``
    # that reached here is some other transport (``git://``), not ssh.
    if "://" not in lowered and _SCP_LIKE_URL_RE.match(lowered):
        return "ssh"
    return "unknown"


def push_remote_scheme(
    repo_path: Path, *, timeout: int = 5, remote: str = "origin"
) -> RemoteScheme:
    """Return the transport scheme of *repo_path*'s effective push URL for *remote*.

    Runs ``git remote get-url --push <remote>`` so ``insteadOf`` /
    ``pushInsteadOf`` rewrites are already applied -- the answer is the
    transport a push actually uses, not the one written in ``.git/config``.
    Fails open to ``unknown`` (never raises) on a missing git binary,
    OSError, timeout, non-zero exit, or an empty/unrecognised URL; the gate
    treats ``unknown`` exactly like ``ssh`` (probe engaged), so this helper
    can only ever *narrow* the set of clients the probe applies to, never
    widen a bypass. Local, no network (R1), same subprocess shape as
    ``cw.pr_hydrate._resolve_repo_slug``.
    """
    try:
        result = _sp.run(
            ["git", "-C", str(repo_path), "remote", "get-url", "--push", remote],
            capture_output=True,
            text=True,
            check=False,
            timeout=timeout,
        )
    except (OSError, _sp.TimeoutExpired):
        return "unknown"
    if result.returncode != 0:
        return "unknown"
    url = result.stdout.strip()
    return _classify_remote_url(url) if url else "unknown"


def remote_needs_ssh_probe(scheme: RemoteScheme) -> bool:
    """Return True iff the SSH-key preflight probe applies to *scheme* (#1495).

    Only ``http`` and ``local`` remotes are exempt; ``ssh`` and ``unknown``
    both engage the probe, so a scheme-resolution failure keeps the gate
    fail-closed rather than turning it off.
    """
    return scheme not in _SCHEMES_WITHOUT_SSH
