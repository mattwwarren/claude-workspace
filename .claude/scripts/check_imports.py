"""CI gate: smoke-import every .claude script to catch import-time defects."""

from __future__ import annotations

import os
import subprocess
import sys

GROUPS: list[tuple[str, list[str]]] = [
    (
        ".claude/scripts",
        ["cw_queue_peek", "post_review", "prep_pr_finalize", "prep_pr_state", "review_monitor"],
    ),
    (".claude/skills/cw-fanout/scripts", ["wave_status"]),
    (".claude/skills/cw-followup/scripts", ["parse_sentinel", "render_decisions"]),
    (".claude/skills/cw-smoke-test/scripts", ["preflight"]),
    (".claude/skills/cw-validate-result/scripts", ["validate_sentinel"]),
]


def main() -> int:
    failed: list[str] = []
    for pythonpath, modules in GROUPS:
        env = {**os.environ, "PYTHONPATH": pythonpath}
        for module in modules:
            result = subprocess.run(
                [sys.executable, "-c", f"import {module}"],
                env=env,
                capture_output=True,
                text=True,
            )
            if result.returncode != 0:
                stderr = result.stderr.strip()
                print(f"FAIL {module} (PYTHONPATH={pythonpath}): {stderr}", file=sys.stderr)
                failed.append(module)
    if failed:
        print(f"\n{len(failed)} module(s) failed to import.", file=sys.stderr)
        return 1
    print(f"All {sum(len(m) for _, m in GROUPS)} modules imported successfully.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
