"""MCP stdio proxy: forwards cw-pr-events SSE notifications to Claude."""

from __future__ import annotations

import functools
import logging
from pathlib import Path
from typing import Any

from cw import _events_channel_base
from cw.config import load_clients
from cw.pr_hydrate import _resolve_repo_slug
from cw.reconcile.tasks import _client_cwd, _is_dangling_client

logger = logging.getLogger(__name__)

_DEFAULT_BASE_URL = "http://127.0.0.1:8788"
# Intentionally not imported from cw_pr_events_server to avoid triggering
# module-level I/O (_cursors.update/_event_offset init) at import time.
_NOTIFICATION_TYPE = "cw-pr-event"


def _build_meta(data: dict[str, Any]) -> dict[str, str]:
    """Build the meta dict for notifications/claude/channel params."""
    payload = data.get("payload") or {}
    meta: dict[str, str] = {
        "event_type": str(data.get("event_type", "")),
        "pr_number": str(data.get("pr_number", "")),
        "repo": str(data.get("repo", "")),
    }
    if "role" in payload:
        meta["role"] = str(payload["role"])
    if "client" in payload:
        meta["client"] = str(payload["client"])
    return {k: v for k, v in meta.items() if v}


def _resolve_client_repo(client_id: str) -> str | None:
    """Resolve *client_id*'s workspace to a github ``owner/repo`` slug.

    Returning None is a genuine resolution failure, which the base layer
    treats as fail-closed -- the proxy forwards nothing for this client
    rather than falling open to every repo (ARCHITECTURE.md §7 principle 12).
    """
    clients = load_clients()
    if _is_dangling_client(client_id, clients):
        logger.error(
            "pr-channel repo filter: client %r has no entry in clients.yaml "
            "(dangling client); cannot resolve repo -- forwarding no "
            "pr-events for this client until clients.yaml is fixed",
            client_id,
        )
        return None
    # No clients.yaml at all is single-tenant mode, not a failure: fall back
    # to the ambient cwd exactly as stale_dispatch_watch.py does.
    git_dir = _client_cwd(client_id, clients) or Path.cwd()
    slug = _resolve_repo_slug(git_dir)
    if slug is None:
        logger.error(
            "pr-channel repo filter: could not resolve a github owner/repo "
            "slug from %s's origin remote (client %r) -- forwarding no "
            "pr-events for this client",
            git_dir,
            client_id,
        )
    return slug


_CONFIG = _events_channel_base.ChannelProxyConfig(
    server_name="cw-pr-events",
    default_base_url=_DEFAULT_BASE_URL,
    base_url_env="CW_PR_EVENTS_BASE_URL",
    client_id_env="CW_PR_EVENTS_CLIENT_ID",
    sse_path="/sse/",
    notification_type=_NOTIFICATION_TYPE,
    build_meta=_build_meta,
    # Two independent axes. pr's proxy has never filtered on the ``client``
    # payload field -- preserve that. It DOES scope by the client's resolved
    # repo (#2146), and a resolve_repo failure is fail-closed by base-layer
    # contract: forward nothing, not everything. --all-repos opts out.
    filter_by_client=False,
    filter_by_repo=True,
    resolve_repo=_resolve_client_repo,
)

_extract_payload = functools.partial(
    _events_channel_base.extract_payload, notification_type=_NOTIFICATION_TYPE
)
_build_outbound_notification = functools.partial(
    _events_channel_base.build_outbound_notification, build_meta=_build_meta
)
_relay_upstream = functools.partial(_events_channel_base.relay_upstream, config=_CONFIG)
run_proxy = functools.partial(_events_channel_base.run_proxy, config=_CONFIG)


if __name__ == "__main__":
    run_proxy()
