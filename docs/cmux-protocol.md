# cmux Socket Protocol

Reference document for the cmux v2 Unix-socket JSON-RPC protocol, reverse-engineered from the cmux source at `/home/matthew/workspace/personal/oss/cmux`.

Source files consulted:
- `tests_v2/cmux.py` — canonical Python v2 client
- `Sources/SocketControlSettings.swift` — socket path resolution logic
- `CLI/cmux.swift` — CLI socket path resolution and connection code
- `docs/v2-api-migration.md` — v1→v2 method parity table
- `tests_v2/test_workspace_create_*.py` — workspace params documentation
- `tests_v2/test_browser_open_split_reuse_policy.py` — surface.split params

---

## 1. Socket Discovery

cmux exposes a Unix-domain stream socket. Clients locate it using the following priority order (implemented in `tests_v2/cmux.py::_default_socket_path` and mirrored in `CLI/cmux.swift::CLISocketPathResolver`):

### 1.1 Environment Variables

| Variable | Behaviour |
|---|---|
| `CMUX_SOCKET_PATH` | Preferred override. Used as-is if set and non-empty. |
| `CMUX_SOCKET` | Legacy alias. Checked if `CMUX_SOCKET_PATH` is absent. |

Both variables are checked in that order. If the env path equals one of the known stable paths and the file does not exist, discovery falls through to the path list below.

### 1.2 Stable Path (macOS)

The primary stable path is inside Application Support:

```
~/Library/Application Support/cmux/cmux.sock
```

Legacy fallback (older builds):

```
/tmp/cmux.sock
```

### 1.3 Last-Used Path

cmux writes the active socket path to a marker file on startup. Clients read it as an additional hint:

```
~/Library/Application Support/cmux/last-socket-path
/tmp/cmux-last-socket-path          # legacy location
```

### 1.4 Debug / Tagged Builds

Debug and tagged builds use named sockets in `/tmp`:

| Build type | Socket path |
|---|---|
| Debug (default) | `/tmp/cmux-debug.sock` |
| Nightly | `/tmp/cmux-nightly.sock` |
| Staging | `/tmp/cmux-staging.sock` |
| Tagged debug build | `/tmp/cmux-debug-<tag>.sock` |

The `CMUX_TAG` environment variable sets a session-scoped tag. When set, the client also checks `/tmp/cmux-<tag>.sock` and `/tmp/cmux-debug-<tag>.sock` ahead of stable paths.

### 1.5 Discovery Fallback

If none of the above exist, the client globs for any `cmux*.sock` file under `/tmp` and `~/Library/Application Support/cmux/`, sorted by mtime descending, and uses the most recent.

### 1.6 Socket Security

The socket file mode is determined by the user's "Socket Control" setting in the app:

| Mode | Permissions | Access |
|---|---|---|
| `off` | Socket not created | None |
| `cmuxOnly` (default) | `0600` | Processes spawned inside cmux terminals only (ancestry check) |
| `automation` | `0600` | Any process owned by the same macOS user (no ancestry check) |
| `password` | `0600` | Any process with the correct password |
| `allowAll` | `0666` | Any local process (unsafe) |

The socket is owned by the running user; the CLI verifies ownership before connecting to prevent fake-socket attacks.

---

## 2. Request Format

Each request is a single JSON object terminated by a newline (`\n`). There is no framing header.

```json
{"id":"1","method":"workspace.list","params":{}}
```

| Field | Type | Required | Description |
|---|---|---|---|
| `id` | string or integer | No | Echoed back in the response. Omit for fire-and-forget. |
| `method` | string | Yes | Method name (see sections below). |
| `params` | object | Yes | Method parameters. Pass `{}` when none are needed. |

The client opens a new TCP/Unix connection per request (reference implementation), sends the serialised line, reads until newline, then closes. Long-running subscriptions (push events) keep the connection open.

---

## 3. Response Format

### 3.1 Success

```json
{"id":"1","ok":true,"result":{...}}
```

| Field | Type | Description |
|---|---|---|
| `id` | string or integer | Echoed from the request. |
| `ok` | `true` | Always `true` on success. |
| `result` | object | Method-specific payload. Never `null` on success (may be `{}`). |

### 3.2 Error

```json
{"id":"1","ok":false,"error":{"code":"not_found","message":"workspace not found"}}
```

| Field | Type | Description |
|---|---|---|
| `id` | string or integer | Echoed from the request. |
| `ok` | `false` | Always `false` on error. |
| `error.code` | string | Machine-readable error code (e.g. `not_found`, `invalid_params`, `method_not_found`). |
| `error.message` | string | Human-readable description. |
| `error.data` | any | Optional extended context. |

---

## 4. Auth (Password Mode)

When the socket is configured in `password` mode, each connection must send a password line before the first JSON request. The password is stored in:

```
~/Library/Application Support/cmux/socket-control-password
```

or can be provided via the `CMUX_SOCKET_PASSWORD` environment variable.

The password is sent as a plain text line:

```
<password>\n
{"id":1,"method":"system.ping","params":{}}\n
```

The server rejects unauthenticated connections silently (closes the socket). Most automation setups use `automation` mode instead, which requires no password.

---

## 5. System Methods

### `system.ping`

```json
{"id":1,"method":"system.ping","params":{}}
```

Response:

```json
{"id":1,"ok":true,"result":{"pong":true}}
```

### `system.capabilities`

Returns the set of methods supported by the running instance:

```json
{"id":1,"method":"system.capabilities","params":{}}
```

### `system.identify`

Returns the currently focused workspace and surface. Optionally accepts a `caller` object with explicit `workspace_id` / `surface_id` to resolve a target context:

```json
{"id":1,"method":"system.identify","params":{}}
```

Response shape:

```json
{
  "id": 1,
  "ok": true,
  "result": {
    "focused": {
      "workspace_id": "<uuid>",
      "workspace_ref": "workspace:0",
      "surface_id": "<uuid>",
      "surface_ref": "surface:0"
    }
  }
}
```

`workspace_ref` and `surface_ref` are ordinal handles (`kind:N`) that are stable within the lifetime of a session but not across app restarts. UUIDs are persistent.

---

## 6. Workspace Methods

### `workspace.list`

```json
{"id":1,"method":"workspace.list","params":{}}
```

Optional param: `window_id` (string) — filter to a specific window.

Response:

```json
{
  "workspaces": [
    {
      "id": "<uuid>",
      "ref": "workspace:0",
      "index": 0,
      "title": "My Workspace",
      "selected": true
    }
  ]
}
```

**To target a workspace by label/title:** call `workspace.list`, iterate the array and match on `title`. Store the `id` (UUID) for subsequent calls. There is no `workspace.find_by_label` method — label matching must be done client-side.

### `workspace.current`

```json
{"id":1,"method":"workspace.current","params":{}}
```

Response: `{"workspace_id": "<uuid>"}`.

### `workspace.create`

Creates a new workspace in the background (does not steal focus from the current workspace).

```json
{
  "id": 1,
  "method": "workspace.create",
  "params": {
    "title": "my-workspace",
    "window_id": "<uuid>",
    "initial_command": "claude -w /path/to/worktree",
    "initial_env": {"MY_VAR": "value"},
    "layout": { ... }
  }
}
```

| Param | Type | Description |
|---|---|---|
| `title` | string | Optional display name shown in the workspace tab. |
| `window_id` | string | Optional — place in a specific window. |
| `initial_command` | string | Shell command run in the initial terminal on creation. Overridden by `layout`. |
| `initial_env` | object | Extra env vars injected into the initial terminal. Overridden by `layout`. |
| `layout` | object | Structured split/pane descriptor (see section 6.1). Overrides `initial_command` / `initial_env` when present. |

Response:

```json
{"workspace_id": "<uuid>"}
```

### `workspace.select`

```json
{"id":1,"method":"workspace.select","params":{"workspace_id":"<uuid>"}}
```

### `workspace.rename`

```json
{"id":1,"method":"workspace.rename","params":{"workspace_id":"<uuid>","title":"New Name"}}
```

### `workspace.close`

```json
{"id":1,"method":"workspace.close","params":{"workspace_id":"<uuid>"}}
```

### 6.1 Layout Object

The `layout` param to `workspace.create` is a recursive split descriptor:

```json
{
  "direction": "horizontal",
  "split": 0.5,
  "children": [
    {
      "pane": {
        "surfaces": [
          {
            "type": "terminal",
            "name": "Left",
            "command": "echo hello",
            "env": {"FOO": "bar"}
          }
        ]
      }
    },
    {
      "pane": {
        "surfaces": [{"type": "terminal", "name": "Right"}]
      }
    }
  ]
}
```

| Field | Description |
|---|---|
| `direction` | `"horizontal"` (side-by-side) or `"vertical"` (top-bottom). |
| `split` | Float 0–1, proportion for the first child. |
| `children` | Array of exactly 2 child nodes (split or pane). A split with 1 child is rejected with `invalid_params`. |
| `pane.surfaces` | Array of 1+ surface descriptors. Multiple surfaces create tabbed views within one pane. Empty array is rejected. |
| `surface.type` | `"terminal"` or `"browser"`. |
| `surface.name` | Optional display name for the surface tab. |
| `surface.command` | Shell command to run in the terminal on creation. |
| `surface.env` | Per-surface environment variable overrides. |

---

## 7. Surface Methods

A **surface** is a single terminal or browser panel. A **pane** is a split region that can contain multiple surfaces as tabs.

### `surface.list`

```json
{"id":1,"method":"surface.list","params":{"workspace_id":"<uuid>"}}
```

Response:

```json
{
  "surfaces": [
    {
      "id": "<uuid>",
      "ref": "surface:0",
      "index": 0,
      "type": "terminal",
      "focused": true,
      "title": ""
    }
  ]
}
```

### `surface.current`

```json
{"id":1,"method":"surface.current","params":{"workspace_id":"<uuid>"}}
```

Response: `{"surface_id": "<uuid>"}`.

### `surface.split`

Creates a new terminal surface by splitting an existing surface. Returns the new `surface_id`.

```json
{
  "id": 1,
  "method": "surface.split",
  "params": {
    "workspace_id": "<uuid>",
    "surface_id": "<uuid>",
    "direction": "right"
  }
}
```

| Param | Type | Description |
|---|---|---|
| `workspace_id` | string | UUID of the workspace containing the surface to split. |
| `surface_id` | string | UUID of the existing surface to split from. Optional — defaults to the focused surface. |
| `direction` | string | `"right"`, `"left"`, `"up"`, or `"down"`. |

Response:

```json
{"surface_id": "<uuid>"}
```

The returned `surface_id` is the stable UUID for the new pane. This is the value to store as `Session.surface_ref`.

### `surface.focus`

```json
{"id":1,"method":"surface.focus","params":{"surface_id":"<uuid>"}}
```

### `surface.close`

```json
{"id":1,"method":"surface.close","params":{"surface_id":"<uuid>"}}
```

Closes (kills) the terminal pane identified by `surface_id`. No response payload.

### `surface.send_text`

Sends text input to a surface (as if typed):

```json
{
  "id": 1,
  "method": "surface.send_text",
  "params": {
    "surface_id": "<uuid>",
    "text": "claude -w /path/to/worktree\n"
  }
}
```

Include `\n` to submit the command.

### `surface.send_key`

```json
{"id":1,"method":"surface.send_key","params":{"surface_id":"<uuid>","key":"ctrl-c"}}
```

### `surface.create`

Creates a new surface (tab) inside an existing pane:

```json
{
  "id": 1,
  "method": "surface.create",
  "params": {
    "pane_id": "<uuid>",
    "type": "terminal"
  }
}
```

Response: `{"surface_id": "<uuid>"}`.

---

## 8. Command Execution

To launch `claude -w <worktree>` in a new pane inside an existing workspace:

### 8.1 Via `workspace.create` + `initial_command`

Use this when you want a dedicated workspace per worktree:

```json
{
  "method": "workspace.create",
  "params": {
    "title": "my-feature",
    "initial_command": "claude -w /path/to/worktree"
  }
}
```

The `initial_command` is run as a shell command in the new workspace's initial terminal. The workspace is created in the background (current workspace focus is preserved). Store `result.workspace_id` to manage the workspace later.

There is no direct way to retrieve the `surface_id` of the initial terminal from the `workspace.create` response. To get it, call `surface.list` after creation:

```json
{"method":"surface.list","params":{"workspace_id":"<uuid>"}}
```

### 8.2 Via `surface.split` + `surface.send_text`

Use this to add a pane to an existing workspace:

1. Call `surface.split` to create the new terminal:

```json
{
  "method": "surface.split",
  "params": {
    "workspace_id": "<uuid>",
    "direction": "right"
  }
}
```

Response: `{"surface_id": "<uuid>"}` — store this as `Session.surface_ref`.

2. Call `surface.send_text` to run the command:

```json
{
  "method": "surface.send_text",
  "params": {
    "surface_id": "<uuid>",
    "text": "claude -w /path/to/worktree\n"
  }
}
```

### 8.3 Surface Identifier to Store as `Session.surface_ref`

The `surface_id` UUID returned by `surface.split` (field name `surface_id` in the result object) is the stable handle for the pane. Store it as `Session.surface_ref`. Pass it to `surface.close` to kill the pane later.

---

## 9. Close / Kill a Pane

```json
{"id":1,"method":"surface.close","params":{"surface_id":"<uuid>"}}
```

This closes the terminal pane. If the surface is running a process (e.g., `claude`), the process receives SIGHUP. No confirmation is required; the call returns `{}` on success.

---

## 10. Shell One-Liner

The following Python one-liner demonstrates the full spawn-identify-close cycle against a live cmux socket. Requires Python 3 (no external deps).

```python
python3 - <<'EOF'
import json, socket, os

SOCK = (
    os.environ.get("CMUX_SOCKET_PATH")
    or os.environ.get("CMUX_SOCKET")
    or os.path.expanduser("~/Library/Application Support/cmux/cmux.sock")
)

def rpc(method, params={}):
    req = json.dumps({"id": 1, "method": method, "params": params}) + "\n"
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.connect(SOCK)
    s.sendall(req.encode())
    buf = b""
    while b"\n" not in buf:
        buf += s.recv(4096)
    s.close()
    resp = json.loads(buf.split(b"\n")[0])
    assert resp["ok"], resp
    return resp["result"]

# 1. Find (or create) a workspace
ws_list = rpc("workspace.list")["workspaces"]
ws_id = ws_list[0]["id"]
print("workspace:", ws_id)

# 2. Get the focused surface to split from
current = rpc("surface.current", {"workspace_id": ws_id})
src_id = current["surface_id"]
print("source surface:", src_id)

# 3. Create a new split to the right
split = rpc("surface.split", {"workspace_id": ws_id, "surface_id": src_id, "direction": "right"})
new_id = split["surface_id"]
print("new surface:", new_id)

# 4. Send a command
rpc("surface.send_text", {"surface_id": new_id, "text": "echo hello-from-cmux\n"})
print("command sent")

# 5. Close the new pane
import time; time.sleep(1)  # let the echo run
rpc("surface.close", {"surface_id": new_id})
print("surface closed")
EOF
```

To use `socat` for raw inspection:

```bash
echo '{"id":1,"method":"system.ping","params":{}}' \
  | socat - UNIX-CONNECT:"$HOME/Library/Application Support/cmux/cmux.sock"
```

---

## 11. Implementation Notes

The following discrepancies were found between the cmux source and `src/cw/cmux.py`.

### 11.1 `_find_socket()` — Missing Discovery Steps

**Current code** (`src/cw/cmux.py`, lines 19–28):

```python
def _find_socket() -> Path:
    if path := os.environ.get("CMUX_SOCKET_PATH"):
        return Path(path)
    if tag := os.environ.get("CMUX_TAG"):
        return Path(f"/tmp/cmux-{tag}.sock")
    stable = Path.home() / "Library" / "Application Support" / "cmux" / "cmux.sock"
    if stable.exists():
        return stable
    return Path("/tmp/cmux.sock")
```

**Issues:**

1. **`CMUX_SOCKET` alias not checked.** The reference client checks both `CMUX_SOCKET_PATH` and `CMUX_SOCKET` (in that order). The cw implementation only checks `CMUX_SOCKET_PATH`.

2. **`CMUX_TAG` socket path is wrong.** The reference client builds `/tmp/cmux-debug-<slug>.sock` for debug builds. The cw code builds `/tmp/cmux-<tag>.sock` — this misses the `-debug-` infix that the actual app writes. The correct path for a `CMUX_TAG=foo` debug session is `/tmp/cmux-debug-foo.sock`.

3. **`last-socket-path` hint file not read.** The reference client reads `~/Library/Application Support/cmux/last-socket-path` (and the legacy `/tmp/cmux-last-socket-path`) as an additional fallback. The cw implementation skips this.

4. **No glob fallback.** The reference client globs for any `cmux*.sock` in `/tmp` and `~/Library/Application Support/cmux/` if all named paths miss. The cw implementation does not.

### 11.2 `spawn()` — New Connection Per Sub-call

**Current code** opens a fresh socket connection for each `_call()` invocation. This means `spawn()` opens and closes three connections (workspace.list, surface.split, surface.send_text). The reference `tests_v2/cmux.py` keeps a single persistent connection for the lifetime of the client. This is inefficient but correct — the protocol is stateless per request.

### 11.3 `spawn()` — `surface.split` Extra Params

**Current call:**

```python
self._call("surface.split", {"workspace_id": ws_id, "direction": surface})
```

The reference implementation (see `test_browser_open_split_reuse_policy.py`) passes `surface_id` to target a specific surface to split from:

```json
{"workspace_id": "<uuid>", "surface_id": "<uuid>", "direction": "right"}
```

Without `surface_id`, the server splits from whatever is currently focused, which may not be in the intended workspace. This is a latent bug: if focus has moved to a different workspace between the `workspace.list` call and the `surface.split` call, the split lands in the wrong workspace. Passing `workspace_id` alone is not guaranteed to anchor the split to a specific surface.

### 11.4 `_call()` — Error Detection

**Current code:**

```python
if not raw.get("ok", True):
```

The default `True` means that if the response is missing the `ok` field entirely (malformed response), no error is raised. The reference client checks `resp.get("ok") is True` explicitly:

```python
if resp.get("ok") is True:
    return resp.get("result")
# otherwise fall through to error handling
```

The cw implementation would silently return `{}` for a malformed response instead of raising.

### 11.5 `spawn()` — No `initial_command` Path

The cw `spawn()` method creates a split and sends the command as keystrokes via `surface.send_text`. An alternative is to use `workspace.create` with `initial_command`, which runs the command as a login shell command rather than simulated typing. The `initial_command` approach is more reliable for long commands with special characters. Neither approach is wrong, but `surface.send_text` is fragile if the terminal has a pre-existing prompt that needs to be cleared first.

### 11.6 Field Name Consistency: `surface_ref` vs `surface_id`

The cmux protocol consistently uses the field name `surface_id` (a UUID string) in both request params and response payloads. The cw `Session` model uses `surface_ref` as the attribute name for the stored value. This is a naming divergence between the Python model and the wire protocol, but is not a bug — it is an internal naming choice.
