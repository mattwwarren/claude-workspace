"""Measure wall-clock cost of cw's four dispatch-worktree hook commands (#2229).

Run: uv run python scripts/measure_hook_cost.py --worktree "$PWD" \
    --cw-bin "$PWD/.venv/bin" --iterations 30 --out .cw/hook-cost-before.txt
Uses an isolated 0700 temp HOME (real state/config copies, headless scratch
contexts, a no-op `claude` shim first on PATH) and reports aggregates only.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import statistics
import subprocess
import sys
import tempfile
import time
import uuid
from functools import partial
from pathlib import Path

WARMUP = 3
TRANSCRIPT_BYTES = 4 * 1024 * 1024
MS_PER_S = 1000.0
CTX = Path(".claude/cw-context.json")
STATE = Path(".local/share/cw/sessions.json")
CONFIGS = (Path(".config/cw/clients.yaml"), Path(".claude-workspace/orchestrator.yaml"))
SUFFIXES = ("signal-stop", "guard-cwd", "guard-busy-wait", "agent-spawn-pre")
HEADLESS = "stop: headless no-sentinel"
# (label, command, payload extras, context session_id); "@@" -> call index
Case = tuple[str, str, dict[str, object], str | None]


def _put(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _cases(worktree: Path, tuid: str) -> list[Case]:
    cfg = json.loads((worktree / ".claude/settings.local.json").read_text("utf-8"))
    hooks = [h["command"] for g in cfg["hooks"].values() for e in g for h in e["hooks"]]
    cmd = {s: next((h for h in hooks if h.endswith(s)), "") for s in SUFFIXES}
    if not all(cmd.values()):
        msg = "settings.local.json lacks one of the four cw hook commands"
        raise SystemExit(msg)
    fg = {"tool_name": "Bash", "tool_input": {"command": "echo @@"}}
    bg = {
        "tool_name": "Bash",
        "tool_input": {"command": "ls", "run_in_background": True},
    }
    stop = cmd["signal-stop"]
    return [
        ("python -c pass", "python -c pass", {}, None),
        ("cw --version", "cw --version", {}, None),
        ("stop: deferral turn", stop, {"background_tasks": [{"id": "t1"}]}, None),
        ("stop: no-op turn", stop, {}, "no-such-session"),
        (HEADLESS, stop, {"session_id": tuid}, None),
        ("guard-cwd", cmd["guard-cwd"], fg, None),
        ("guard-busy-wait fg", cmd["guard-busy-wait"], fg, None),
        ("guard-busy-wait bg", cmd["guard-busy-wait"], bg, None),
        ("agent-spawn-pre", cmd["agent-spawn-pre"], {"tool_name": "Agent"}, None),
    ]


def _seed_home(home: Path, sid: object, headless_dir: Path, tuid: str) -> int:
    real = Path.home()
    for rel in CONFIGS:
        _put(home / rel, (real / rel).read_text("utf-8"))
    state = json.loads((real / STATE).read_text("utf-8"))
    [target] = [s for s in state["sessions"] if s["id"] == sid]  # ValueError if absent
    target.update(worktree_path=str(headless_dir), surface_ref=tuid[:8])
    target.update(status="active", origin="daemon", last_result=None)
    _put(home / STATE, json.dumps(state))
    encoded = str(headless_dir).replace("/", "-").replace(".", "-")
    block = {"type": "text", "text": "neutral assistant text " * 40}
    line = json.dumps({"type": "assistant", "message": {"content": [block]}}) + "\n"
    transcript = home / ".claude" / "projects" / encoded / f"{tuid}.jsonl"
    _put(transcript, line * (TRANSCRIPT_BYTES // len(line)))
    return len(state["sessions"])


def _time_case(
    extra: dict[str, object], cwd: Path, cmd: str, env: dict[str, str], n: int
) -> str:
    run = partial(subprocess.run, capture_output=True, check=False, env=env, cwd=cwd)
    samples, codes = [], set()
    for i in range(WARMUP + n):
        payload = {"session_id": "claude-uuid", "cwd": str(cwd), **extra}
        start = time.perf_counter()
        data = json.dumps(payload).replace("@@", str(i))
        proc = run(["sh", "-c", cmd], input=data, text=True)
        codes.add(proc.returncode)
        if i >= WARMUP:
            samples.append((time.perf_counter() - start) * MS_PER_S)
    p95 = statistics.quantiles(samples, n=20)[-1]  # last of 19 cuts = 95th percentile
    return f"{statistics.median(samples):8.1f} {p95:8.1f}  {sorted(codes)}"


def _measure(args: argparse.Namespace, worktree: Path, tmp: Path) -> str:
    ctx = json.loads((worktree / CTX).read_text("utf-8"))
    tuid = str(uuid.uuid4())
    cases = _cases(worktree, tuid)
    scratch = {label: tmp / f"scratch{k}" for k, (label, *_) in enumerate(cases)}
    for label, _cmd, _extra, sid in cases:
        context = {**ctx, "headless": True, "session_id": sid or ctx["session_id"]}
        context["agent_spawn_stamp"] = {"unresolved_count": 0, "last_stamped_at": None}
        _put(scratch[label] / CTX, json.dumps(context))
    home, shim = tmp / "home", tmp / "shim" / "claude"
    count = _seed_home(home, ctx["session_id"], scratch[HEADLESS], tuid)
    _put(shim, "#!/bin/sh\nexit 0\n")
    shim.chmod(0o700)
    env = {k: v for k, v in os.environ.items() if not k.startswith("XDG_")}
    env.update(HOME=str(home), PATH=f"{shim.parent}:{args.cw_bin}:{os.environ['PATH']}")
    git = ["git", "-C", str(worktree), "rev-parse", "--short", "HEAD"]
    sha = subprocess.run(git, capture_output=True, text=True, check=True).stdout.strip()
    head = f"git {sha}  python {sys.version.split()[0]}  N={args.iterations}"
    out = [f"{head}  sessions={count}  transcript={TRANSCRIPT_BYTES} bytes"]
    out += [f"hook: {cmd}" for cmd in sorted({c[1] for c in cases[2:]})]
    out.append(f"{'case':28} {'med ms':>8} {'p95 ms':>8}  exit codes")
    medians = {}
    for label, command, extra, _sid in cases:
        cmd = command.replace(str(worktree / CTX), str(scratch[label] / CTX))
        row = _time_case(extra, scratch[label], cmd, env, args.iterations)
        medians[label] = float(row.split()[0])
        out.append(f"{label:28} {row}")
    total = medians["guard-cwd"] + medians["guard-busy-wait fg"]
    out.append(f"{'Bash-call total (sequential)':28} {total:8.1f}")
    return "\n".join(out).replace(str(worktree), "<worktree>") + "\n"


def main() -> None:
    parser = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    parser.add_argument("--worktree", required=True)
    parser.add_argument("--cw-bin", required=True)
    parser.add_argument("--iterations", type=int, default=30)
    parser.add_argument("--out", required=True)
    args = parser.parse_args()
    tmp = Path(tempfile.mkdtemp(prefix="cw-hook-cost-"))
    try:
        report = _measure(args, Path(args.worktree).resolve(), tmp)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    Path(args.out).write_text(report, encoding="utf-8")
    sys.stdout.write(report)


if __name__ == "__main__":
    main()
