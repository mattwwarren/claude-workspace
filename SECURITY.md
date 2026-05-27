# Security policy

## Reporting a vulnerability

If you believe you've found a security vulnerability in `cw`, please do
**not** open a public GitHub issue. Instead, report it privately via
GitHub's built-in advisory mechanism:

1. Go to https://github.com/mattwwarren/claude-workspace/security/advisories/new
2. Fill out the advisory form with a description, impact assessment, and
   reproduction steps.

You should expect an acknowledgement within a few business days. Because
this is a single-maintainer project, please be patient on weekends and
holidays.

## Threat model

`cw` is an orchestrator that runs `claude` (Anthropic's CLI) and `git` on
your local machine. It does not itself accept network input. Areas worth
extra care when reviewing changes:

- **Subprocess invocation.** Anywhere `cw` builds a `git` or `claude`
  command from config or user input, that input must not be shell-spliced.
  All subprocess calls should pass arguments as lists, never as strings.
- **State file writes.** `~/.local/share/cw/sessions.json`,
  `~/.local/share/cw/dev_queue.json`, and friends are read by multiple
  concurrent sessions. Atomic writes + file-based locking are
  load-bearing; bypassing them risks corrupt state.
- **MCP / PR channel server.** `cw pr-channel` exposes an MCP endpoint
  that publishes PR events. It binds to localhost by default; any change
  that widens the bind address or weakens auth should be flagged.
- **Bypass-permissions disclaimer.** `cw` spawns `claude --bg`, which
  requires the user to have accepted Claude Code's bypass-permissions
  disclaimer. Don't auto-accept it on the user's behalf.

## What's out of scope

- Vulnerabilities in `claude` itself — please report those to Anthropic
  via Claude Code's own channels.
- Vulnerabilities in `git`, `uv`, `cmux`, or `tmux` — please report
  those upstream.
- Local-only attacker scenarios where the attacker already has shell
  access as your user. `cw` does not attempt to defend against a local
  attacker with your privileges.
