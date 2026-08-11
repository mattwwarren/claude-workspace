# Token Attribution Assessment (#1810) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Produce a measured token-attribution breakdown (fixed bookkeeping vs actual work, per session class) for cw pipeline sessions, plus per-server MCP schema costs and a ranked lever list, delivered as `docs/token-attribution-2026-08.md`.

**Architecture:** Throwaway analysis scripts in the session scratchpad parse Claude Code transcript JSONL files for real worker/orchestrator sessions; a probe matrix of one-shot headless sessions isolates per-server MCP schema cost; results assemble into a repo doc via PR. No `src/cw` changes.

**Tech Stack:** Python 3 stdlib (json, pathlib), `claude` CLI for probes, `gh` for the issue comment.

## Global Constraints

- Spec: `docs/superpowers/specs/2026-08-10-token-attribution-design.md` — follow its approved scope decisions verbatim.
- **No changes to `src/cw`** — analysis-first; plumbing is a drafted follow-up ticket only.
- Analysis scripts live in `$SCRATCH` (see below), never committed to the repo.
- Worker sample: 12 completed sessions — 8 from `-cw-wt-7dc983e2-*` lane, 4 from `-cw-wt-bdf6a9bf-*` lane; all five pipeline stages represented across the sample.
- Orchestrator sample: 2–3 interactive cockpit sessions.
- Reconciliation tolerance: estimated fixed constituents may not exceed measured first-turn total by more than 5%; violations flagged in report, never silently included.
- chars÷4 token estimates must be calibrated against a probe-measured delta and every estimated (non-measured) number in the report flagged as such.
- Follow-up tickets are DRAFTED in the report, not filed. Issue comment on #1810 IS authorized (approved in design).
- PR creation goes through `/prep-pr` (global rule), not `gh pr create`.
- `SCRATCH=/tmp/claude-1000/-home-matthew-workspace-projects-claude-workspace/ca219764-eaa7-4ec8-8b53-78590570bb8a/scratchpad/1810` — create with `mkdir -p` at first use. All scripts and JSON outputs live there.
- Transcript roots: `TRANSCRIPTS=/home/matthew/.claude/projects`.
- Repo worktree (report + spec home): `/home/matthew/workspace/projects/claude-workspace/.claude/worktrees/purring-toasting-yao`.

---

### Task 1: Sample selection — inventory sessions and pick the sample

**Files:**
- Create: `$SCRATCH/select_sample.py`
- Output: `$SCRATCH/sample.json`

**Interfaces:**
- Produces: `sample.json` — `{"workers": [{"dir": str, "jsonl": str, "lane": "7dc983e2"|"bdf6a9bf", "ticket": str, "stages": [str], "turns": int}], "orchestrators": [{"dir": str, "jsonl": str, "turns": int}]}`. Later tasks read this file; the key names above are load-bearing.

- [ ] **Step 1: Write the inventory script**

```python
#!/usr/bin/env python3
"""Select the #1810 measurement sample from on-disk transcripts."""
import json
import re
from pathlib import Path

TRANSCRIPTS = Path("/home/matthew/.claude/projects")
STAGES = ["auto-dev-intake", "auto-dev-plan", "auto-dev-impl",
          "auto-dev-review", "auto-dev-finalize"]
OUT = Path(__file__).parent / "sample.json"


def biggest_jsonl(d: Path) -> Path | None:
    files = sorted(d.glob("*.jsonl"), key=lambda p: p.stat().st_size)
    return files[-1] if files else None


def scan(jsonl: Path) -> tuple[list[str], int, bool]:
    """Return (stages seen, assistant turn count, has usage)."""
    stages: set[str] = set()
    turns = 0
    has_usage = False
    with jsonl.open(encoding="utf-8", errors="replace") as fh:
        for line in fh:
            for s in STAGES:
                if s in line:
                    stages.add(s)
            if '"type":"assistant"' in line:
                turns += 1
                if '"usage"' in line:
                    has_usage = True
    return sorted(stages), turns, has_usage


def main() -> None:
    workers = []
    for d in sorted(TRANSCRIPTS.glob("-home-matthew--cw-wt-*"),
                    key=lambda p: p.stat().st_mtime, reverse=True):
        m = re.match(r".*-cw-wt-([0-9a-f]{8})-dev-(.+)$", d.name)
        if not m:
            continue
        lane, ticket = m.group(1), m.group(2)
        jsonl = biggest_jsonl(d)
        if jsonl is None:
            continue
        stages, turns, has_usage = scan(jsonl)
        if turns < 5 or not has_usage:
            continue  # too small / unparseable to attribute
        workers.append({"dir": str(d), "jsonl": str(jsonl), "lane": lane,
                        "ticket": ticket, "stages": stages, "turns": turns})

    lane_a = [w for w in workers if w["lane"] == "7dc983e2"][:8]
    lane_b = [w for w in workers if w["lane"] == "bdf6a9bf"][:4]
    sample_workers = lane_a + lane_b

    covered = {s for w in sample_workers for s in w["stages"]}
    missing = [s for s in STAGES if s not in covered]
    if missing:
        # swap in older lane-A sessions that carry the missing stages
        pool = [w for w in workers if w not in sample_workers]
        for s in missing:
            for w in pool:
                if s in w["stages"]:
                    sample_workers.append(w)
                    break
    covered = {s for w in sample_workers for s in w["stages"]}
    assert all(s in covered for s in STAGES), f"stage coverage gap: {missing}"

    orchestrators = []
    for d in sorted(TRANSCRIPTS.glob("-home-matthew-workspace*"),
                    key=lambda p: p.stat().st_mtime, reverse=True):
        jsonl = biggest_jsonl(d)
        if jsonl is None:
            continue
        text_probe = jsonl.read_text(encoding="utf-8", errors="replace")
        if "dev-queue" in text_probe or "cw event tail" in text_probe:
            _, turns, has_usage = scan(jsonl)
            if turns >= 10 and has_usage:
                orchestrators.append(
                    {"dir": str(d), "jsonl": str(jsonl), "turns": turns})
        if len(orchestrators) == 3:
            break

    OUT.write_text(json.dumps(
        {"workers": sample_workers, "orchestrators": orchestrators}, indent=2))
    print(f"workers={len(sample_workers)} "
          f"(laneA={len(lane_a)} laneB={len(lane_b)}) "
          f"orchestrators={len(orchestrators)} stages_covered={sorted(covered)}")


if __name__ == "__main__":
    main()
```

- [ ] **Step 2: Run it**

Run: `mkdir -p $SCRATCH && python3 $SCRATCH/select_sample.py`
Expected: `workers=12` (or slightly more if stage-coverage swaps fired), `orchestrators=2..3`, all five stages in `stages_covered`, no assertion error.

- [ ] **Step 3: Sanity-check the output by hand**

Run: `python3 -m json.tool $SCRATCH/sample.json | head -40`
Expected: real paths, both lanes present, ticket ids look like recent issues (e.g. 1805, 1763). If lane B yields <4 usable sessions, relax the `turns < 5` floor to `turns < 3` and re-run; note the relaxation for the report's method section.

### Task 2: Usage extraction — per-turn usage series per session

**Files:**
- Create: `$SCRATCH/extract_usage.py`
- Output: `$SCRATCH/usage.json`

**Interfaces:**
- Consumes: `$SCRATCH/sample.json` (Task 1 shape).
- Produces: `usage.json` — `{<jsonl path>: {"turns": [{"i": int, "input": int, "cache_creation": int, "cache_read": int, "output": int}], "first_turn_fixed": int, "total_output": int, "skipped_lines": int}}`. `first_turn_fixed` = first assistant turn's `cache_creation + input`.

- [ ] **Step 1: Write the extraction script**

```python
#!/usr/bin/env python3
"""Extract per-turn usage from sampled transcripts."""
import json
from pathlib import Path

HERE = Path(__file__).parent
SAMPLE = json.loads((HERE / "sample.json").read_text())
OUT = HERE / "usage.json"


def extract(jsonl: Path) -> dict:
    turns, skipped = [], 0
    with jsonl.open(encoding="utf-8", errors="replace") as fh:
        for line in fh:
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                skipped += 1
                continue
            if obj.get("type") != "assistant":
                continue
            usage = (obj.get("message") or {}).get("usage")
            if not usage:
                continue
            turns.append({
                "i": len(turns),
                "input": usage.get("input_tokens", 0),
                "cache_creation": usage.get("cache_creation_input_tokens", 0),
                "cache_read": usage.get("cache_read_input_tokens", 0),
                "output": usage.get("output_tokens", 0),
            })
    if not turns:
        return {"turns": [], "first_turn_fixed": 0, "total_output": 0,
                "skipped_lines": skipped}
    first = turns[0]
    return {
        "turns": turns,
        "first_turn_fixed": first["cache_creation"] + first["input"],
        "total_output": sum(t["output"] for t in turns),
        "skipped_lines": skipped,
    }


def main() -> None:
    out = {}
    for sess in SAMPLE["workers"] + SAMPLE["orchestrators"]:
        out[sess["jsonl"]] = extract(Path(sess["jsonl"]))
    OUT.write_text(json.dumps(out, indent=2))
    for path, u in out.items():
        tag = Path(path).parent.name
        print(f"{tag}: turns={len(u['turns'])} "
              f"fixed={u['first_turn_fixed']} skipped={u['skipped_lines']}")


if __name__ == "__main__":
    main()
```

- [ ] **Step 2: Run and verify against a hand-checked session**

Run: `python3 $SCRATCH/extract_usage.py`
Then hand-verify one session: `grep -o '"usage":{[^}]*}' <that session's jsonl> | head -1` and confirm the script's `first_turn_fixed` equals that line's `cache_creation_input_tokens + input_tokens`.
Expected: every sampled session has `turns > 0`; `first_turn_fixed` in the tens of thousands (e.g. ~55k for dev-1805). Any session with `turns == 0`: drop it, re-run Task 1 selection with that path excluded (add an `EXCLUDE` set), and note the replacement.

### Task 3: Constituent measurement — tokenize fixed inputs and bookkeeping payloads

**Files:**
- Create: `$SCRATCH/constituents.py`
- Output: `$SCRATCH/constituents.json`

**Interfaces:**
- Consumes: `sample.json`, transcripts on disk, repo docs.
- Produces: `constituents.json` — per session: `{"claude_md": int, "stage_docs": {name: int}, "bookkeeping_read": {label: int}, "bookkeeping_written": {label: int}, "estimator": "chars/4"}` (token estimates). Also a top-level `"files"` map of every fixed doc's `{bytes, est_tokens}`.

- [ ] **Step 1: Write the constituent script**

```python
#!/usr/bin/env python3
"""Estimate token weight of fixed inputs and bookkeeping artifacts."""
import json
import re
from pathlib import Path

HERE = Path(__file__).parent
SAMPLE = json.loads((HERE / "sample.json").read_text())
OUT = HERE / "constituents.json"
REPO = Path("/home/matthew/workspace/projects/claude-workspace"
            "/.claude/worktrees/purring-toasting-yao")
CHARS_PER_TOKEN = 4.0  # recalibrated by Task 4 against probe data

STAGE_DOC_DIR = REPO / ".claude" / "commands"
CLAUDE_MDS = [REPO / "CLAUDE.md", Path("/home/matthew/.claude/CLAUDE.md")]
# bookkeeping payloads recognized inside transcripts, by marker → label
READ_MARKERS = {
    ".cw/context.json": "context.json",
    ".cw/plan.md": "plan.md",
    "handoff": "handoff",
}
WRITE_MARKERS = {
    "AUTO_DEV_RESULT": "sentinel",
    "## Plan": "plan_comment",
    "truth table": "pr_status",
}


def est(text: str) -> int:
    return int(len(text) / CHARS_PER_TOKEN)


def session_breakdown(jsonl: Path) -> dict:
    reads: dict[str, int] = {}
    writes: dict[str, int] = {}
    with jsonl.open(encoding="utf-8", errors="replace") as fh:
        for line in fh:
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue
            msg = obj.get("message") or {}
            content = msg.get("content")
            if not isinstance(content, list):
                continue
            for block in content:
                if not isinstance(block, dict):
                    continue
                if block.get("type") == "tool_result":
                    payload = json.dumps(block.get("content", ""))
                    for marker, label in READ_MARKERS.items():
                        if marker in payload:
                            reads[label] = reads.get(label, 0) + est(payload)
                elif block.get("type") == "tool_use":
                    payload = json.dumps(block.get("input", {}))
                    for marker, label in WRITE_MARKERS.items():
                        if marker in payload:
                            writes[label] = writes.get(label, 0) + est(payload)
    return {"bookkeeping_read": reads, "bookkeeping_written": writes}


def main() -> None:
    files = {}
    for p in [*CLAUDE_MDS, *sorted(STAGE_DOC_DIR.glob("auto-dev*.md"))]:
        if p.exists():
            text = p.read_text(encoding="utf-8", errors="replace")
            files[str(p)] = {"bytes": len(text), "est_tokens": est(text)}

    sessions = {}
    for sess in SAMPLE["workers"] + SAMPLE["orchestrators"]:
        jsonl = Path(sess["jsonl"])
        b = session_breakdown(jsonl)
        stage_docs = {}
        for s in sess.get("stages", []):
            doc = STAGE_DOC_DIR / f"{s}.md"
            if doc.exists():
                stage_docs[s] = est(
                    doc.read_text(encoding="utf-8", errors="replace"))
        sessions[sess["jsonl"]] = {
            "claude_md": sum(
                f["est_tokens"] for k, f in files.items() if "CLAUDE.md" in k),
            "stage_docs": stage_docs,
            **b,
            "estimator": "chars/4",
        }
    OUT.write_text(json.dumps({"files": files, "sessions": sessions}, indent=2))
    print(json.dumps(files, indent=2))


if __name__ == "__main__":
    main()
```

- [ ] **Step 2: Run and spot-check**

Run: `python3 $SCRATCH/constituents.py`
Expected: `files` shows the six `auto-dev*.md` docs with est_tokens ≈ bytes/4 (auto-dev-plan.md ≈ 17k tokens), both CLAUDE.md files present. Spot-check one worker session's `bookkeeping_read` shows a `context.json` entry (workers read `.cw/context.json` at intake).

- [ ] **Step 3: Verify marker precision on one transcript**

Pick one worker jsonl; run a quick grep for each READ/WRITE marker to confirm hits are the intended artifacts, not incidental prose. If a marker over-matches (e.g. `handoff` matching skill text), tighten it (e.g. `handoff-` filename pattern or `.cw/handoff`) and re-run. Record final marker set for the report's method section.

### Task 4: MCP probe matrix — measure per-server schema cost

**Files:**
- Create: `$SCRATCH/probes/` (variant configs + probe cwd), `$SCRATCH/run_probes.sh`, `$SCRATCH/probe_results.py`
- Output: `$SCRATCH/probes.json`

**Interfaces:**
- Consumes: `~/.claude/.mcp.json` (server definitions).
- Produces: `probes.json` — `{"baseline": [int, int], "variants": {name: {"first_turn_fixed": int, "delta_vs_baseline": int}}, "calibration": {"chars_per_token": float}}`. Task 5 consumes `delta_vs_baseline` and `calibration`.

- [ ] **Step 1: Enumerate what servers a worker actually sees**

Run: `cd $SCRATCH/probes && claude mcp list` (create dir first).
Record the list. Configurable stdio servers (`playwright`, `chrome-devtools` from `~/.claude/.mcp.json`) get real probe variants. Plugin/connector servers (linear, slack, notion, gdrive) that cannot be attached via `--mcp-config` get the schema-tokenization fallback in Step 5.

- [ ] **Step 2: Write variant configs**

```bash
mkdir -p $SCRATCH/probes && cd $SCRATCH/probes
cat > empty.json <<'EOF'
{"mcpServers": {}}
EOF
cat > playwright.json <<'EOF'
{"mcpServers": {"playwright": {"command": "npx", "args": ["-y", "@playwright/mcp@latest"]}}}
EOF
cat > chrome.json <<'EOF'
{"mcpServers": {"chrome-devtools": {"command": "npx", "args": ["-y", "chrome-devtools-mcp@latest"]}}}
EOF
cat > full.json <<'EOF'
{"mcpServers": {"playwright": {"command": "npx", "args": ["-y", "@playwright/mcp@latest"]},
                "chrome-devtools": {"command": "npx", "args": ["-y", "chrome-devtools-mcp@latest"]}}}
EOF
```

- [ ] **Step 3: Run the probe matrix (baseline twice)**

```bash
cd $SCRATCH/probes
for v in empty empty2:empty playwright chrome full; do
  name="${v%%:*}"; cfg="${v#*:}"; [ "$cfg" = "$v" ] && cfg="$name"
  claude -p "reply with exactly: ok" --model claude-haiku-4-5-20251001 \
    --strict-mcp-config --mcp-config "$cfg.json" > "out-$name.txt" 2>&1
done
```

Expected: each run prints roughly `ok`. Probe transcripts land under `$TRANSCRIPTS/<slug-of-$SCRATCH-probes>/`.

- [ ] **Step 4: Collect probe results**

```python
#!/usr/bin/env python3
"""Collect first-turn fixed cost from probe transcripts, compute deltas."""
import json
from pathlib import Path

TRANSCRIPTS = Path("/home/matthew/.claude/projects")
HERE = Path(__file__).parent
OUT = HERE / "probes.json"

# probe cwd slug: '/' and '.' become '-', prefixed with '-'
slug = "-" + str(HERE / "probes").replace("/", "-").replace(".", "-").lstrip("-")
probe_dir = TRANSCRIPTS / slug
assert probe_dir.exists(), f"no transcripts at {probe_dir}; check slug"


def first_turn_fixed(jsonl: Path) -> int:
    with jsonl.open(encoding="utf-8", errors="replace") as fh:
        for line in fh:
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue
            if obj.get("type") == "assistant":
                u = (obj.get("message") or {}).get("usage") or {}
                return (u.get("cache_creation_input_tokens", 0)
                        + u.get("input_tokens", 0))
    return 0


def main() -> None:
    # order transcripts by mtime; matches probe run order from Step 3
    runs = sorted(probe_dir.glob("*.jsonl"), key=lambda p: p.stat().st_mtime)
    names = ["empty", "empty2", "playwright", "chrome", "full"]
    assert len(runs) >= len(names), f"expected {len(names)} probes, found {len(runs)}"
    fixed = {n: first_turn_fixed(p) for n, p in zip(names, runs[-len(names):])}
    base = fixed["empty"]
    drift = abs(fixed["empty2"] - base)
    assert drift <= max(50, base * 0.01), f"baseline unstable: {fixed}"
    variants = {n: {"first_turn_fixed": v, "delta_vs_baseline": v - base}
                for n, v in fixed.items() if not n.startswith("empty")}
    OUT.write_text(json.dumps(
        {"baseline": [base, fixed["empty2"]], "variants": variants,
         "calibration": {"chars_per_token": None}}, indent=2))
    print(json.dumps(variants, indent=2))


if __name__ == "__main__":
    main()
```

Run: `python3 $SCRATCH/probe_results.py`
Expected: baseline stable within 1%; playwright/chrome deltas positive (schema tax in tokens); `full ≈ playwright + chrome` within ~10%.

- [ ] **Step 5: Calibrate chars÷4 and cover non-reproducible servers**

Calibration: dump the playwright server's tool schema JSON (from a probe session, `claude mcp` output, or the `@playwright/mcp` package's declared tools), compute `chars_per_token = len(schema_json) / delta_vs_baseline`, and write it into `probes.json` `calibration`. Then for each plugin/connector server a worker sees (Linear etc.): obtain its tool-schema JSON (e.g. from a session where the server is connected, or the plugin's manifest), estimate tokens with the calibrated factor, and add it to `probes.json` under `variants` with `"estimated": true`. Every such number is flagged `estimated` in the final report.

- [ ] **Step 6: Commit nothing** — probe artifacts stay in scratchpad. Verify with `git -C <worktree> status --short` (clean).

### Task 5: Attribution assembly — categories, residuals, reconciliation

**Files:**
- Create: `$SCRATCH/attribute.py`
- Output: `$SCRATCH/attribution.json`

**Interfaces:**
- Consumes: `usage.json` (Task 2), `constituents.json` (Task 3), `probes.json` (Task 4).
- Produces: `attribution.json` — per session: `{"class": "worker"|"orchestrator", "fixed_measured": int, "categories": {"mcp_schemas": int, "instruction_layer": int, "system_baseline": int, "bookkeeping_read": int, "bookkeeping_written_out": int, "bookkeeping_written_recurring": int, "residual_work": int}, "cache": {"read": int, "creation": int, "ratio": float}, "reconciliation": {"fixed_estimate_vs_measured_pct": float, "flag": bool}}` plus `"class_summary"` aggregates. Task 6 (report) consumes all of it.

- [ ] **Step 1: Write the assembly script**

```python
#!/usr/bin/env python3
"""Assemble per-session and per-class token attribution."""
import json
from pathlib import Path

HERE = Path(__file__).parent
usage = json.loads((HERE / "usage.json").read_text())
cons = json.loads((HERE / "constituents.json").read_text())
probes = json.loads((HERE / "probes.json").read_text())
sample = json.loads((HERE / "sample.json").read_text())
OUT = HERE / "attribution.json"

BASELINE = probes["baseline"][0]
MCP_TOTAL = sum(v["delta_vs_baseline"] for v in probes["variants"].values())
TOL = 0.05


def classify(jsonl: str) -> str:
    return ("worker" if any(w["jsonl"] == jsonl for w in sample["workers"])
            else "orchestrator")


def attribute(jsonl: str) -> dict:
    u = usage[jsonl]
    c = cons["sessions"][jsonl]
    fixed = u["first_turn_fixed"]
    instruction = c["claude_md"] + sum(c["stage_docs"].values())
    read_in = sum(c["bookkeeping_read"].values())
    written = sum(c["bookkeeping_written"].values())
    n_turns = len(u["turns"])
    # a written artifact re-enters context on every later turn (cache-read)
    recurring = written * max(0, n_turns - 1)
    known_fixed = BASELINE + MCP_TOTAL + instruction
    residual = fixed - known_fixed
    recon_pct = (known_fixed - fixed) / fixed * 100 if fixed else 0.0
    total_read = sum(t["cache_read"] for t in u["turns"])
    total_create = sum(t["cache_creation"] for t in u["turns"])
    return {
        "class": classify(jsonl),
        "fixed_measured": fixed,
        "categories": {
            "mcp_schemas": MCP_TOTAL,
            "instruction_layer": instruction,
            "system_baseline": BASELINE,
            "bookkeeping_read": read_in,
            "bookkeeping_written_out": written,
            "bookkeeping_written_recurring": recurring,
            "residual_work": residual,
        },
        "cache": {"read": total_read, "creation": total_create,
                  "ratio": round(total_read / total_create, 2)
                  if total_create else 0.0},
        "reconciliation": {
            "fixed_estimate_vs_measured_pct": round(recon_pct, 1),
            "flag": recon_pct > TOL * 100,
        },
    }


def main() -> None:
    sessions = {j: attribute(j) for j in usage}
    summary: dict[str, dict] = {}
    for s in sessions.values():
        agg = summary.setdefault(s["class"], {"n": 0, "fixed": 0, "read": 0,
                                              "written": 0, "residual": 0})
        agg["n"] += 1
        agg["fixed"] += s["fixed_measured"]
        agg["read"] += s["categories"]["bookkeeping_read"]
        agg["written"] += s["categories"]["bookkeeping_written_out"]
        agg["residual"] += s["categories"]["residual_work"]
    OUT.write_text(json.dumps(
        {"sessions": sessions, "class_summary": summary}, indent=2))
    flagged = [j for j, s in sessions.items() if s["reconciliation"]["flag"]]
    print(f"sessions={len(sessions)} flagged={len(flagged)}")
    for j in flagged:
        print("FLAG", j, sessions[j]["reconciliation"])


if __name__ == "__main__":
    main()
```

- [ ] **Step 2: Run and check the invariant**

Run: `python3 $SCRATCH/attribute.py`
Expected: `flagged=0` ideally. Any flagged session means estimated fixed constituents exceed the measured first-turn total by >5% — inspect whether that session genuinely didn't load the attributed docs (e.g. resumed session with compacted context: its first turn is not a fresh fixed cost). Legitimate structural mismatches (resume/compaction) get the session replaced via Task 1 EXCLUDE; anything else means an estimate is wrong — fix before proceeding. Every remaining flag is carried into the report, not dropped.

- [ ] **Step 3: Eyeball plausibility**

`residual_work` must be positive for every worker session; `mcp_schemas + instruction_layer` for a worker should land in the same order of magnitude as the operator's "23% of context" observation scaled to session size. If instruction_layer alone exceeds `fixed_measured`, the session did not load all attributed stage docs — attribute only stage docs actually seen in that transcript (already per-session via Task 1 `stages`).

### Task 6: Report + levers + drafted tickets

**Files:**
- Create: `docs/token-attribution-2026-08.md` (in the worktree, committed)

**Interfaces:**
- Consumes: `attribution.json`, `probes.json`, `constituents.json`, spec.
- Produces: the committed report; drafted ticket bodies inside it.

- [ ] **Step 1: Write the report**

Structure (all numbers from the JSON outputs; every estimated figure marked `~` and footnoted):

```markdown
# Token Attribution Assessment — 2026-08 (#1810)

## Method          <- how measured: forensics + probes, marker set, calibration
                      factor, estimator disclosure, invariant, replacements made
## Sample          <- the sessions: lane, ticket, stages, turns each
## Fixed cost per session class
   - worker: first-turn fixed (median/range), decomposed:
     system baseline / MCP schemas (per server, measured vs estimated) /
     instruction layer (CLAUDE.md, per stage doc) / residual
   - orchestrator: same decomposition
   - reviewer: #1710 recorded numbers, cited
## Variable bookkeeping cost
   - read-in artifacts (context.json, plan.md, handoff) per class
   - written-out artifacts (plan comments, sentinel, PR status) as output
     + recurring context weight (x remaining turns, cache-discounted)
## Cache economics <- read/creation ratios; raw vs effective fixed cost
## Ranked levers   <- per lever: affected class, est. % of class input saved,
                      confidence, evidence pointer
## Drafted follow-up tickets  <- one per lever >10%, full ticket body each,
                                 NOT filed
## Reconciliation & limitations <- flagged sessions, tolerance misses,
                                    estimator caveats
```

Every lever >10% of a session class's input gets a complete drafted ticket body (title, Why with measured numbers, Method, Acceptance). Candidate levers from the issue to evaluate against the data: per-session-class MCP allowlists, compact reprs for persisted artifacts, stage-doc diet, lean reviewer profiles generalized. An explicit "fixed overhead acceptable, no action" verdict is allowed if the numbers say so.

- [ ] **Step 2: Verify no unverified claims**

Cross-check every number in the report against the JSON files (`grep` the figure in `$SCRATCH/*.json`). Every claim either cites a measured value, an `estimated` flag, or a linked issue (#1710, #1549).

- [ ] **Step 3: Commit**

```bash
git add docs/token-attribution-2026-08.md
git commit -m "docs(#1810): token-attribution assessment report"
```

### Task 7: Ship — PR via /prep-pr, then #1810 summary comment

**Files:**
- None new (ships Tasks 6's commit + the spec/plan commits on `assess/1810-token-attribution`)

- [ ] **Step 1: Invoke `/prep-pr`** (global rule — no direct `gh pr create`). PR title: `docs(#1810): token-attribution assessment — MCP schema tax, stage-doc fixed cost, bookkeeping artifacts`. Body summarizes findings + links the report path.

- [ ] **Step 2: Post the #1810 summary comment** (authorized in design): 5–10 line summary — headline per-class fixed-cost numbers, top 2 levers, link to the PR/report, note that follow-up tickets are drafted in the report awaiting operator approval.

```bash
gh issue comment 1810 --body-file $SCRATCH/issue-comment.md
```

- [ ] **Step 3: Present drafted tickets to operator** — list the drafted ticket titles + one-line savings estimates in the session summary; file nothing until approval.

---

## Self-Review Notes

- Spec coverage: sampling (T1), extraction (T2), constituents (T3), probes+calibration (T4), attribution+invariant (T5), report+levers+drafts (T6), PR+comment+gated tickets (T7). Reviewer class covered by citation in T6. "No src/cw changes" held throughout.
- Resume/compacted sessions are the known threat to `first_turn_fixed` validity — handled in T5 Step 2.
- Probe transcript-to-name matching relies on mtime order (T4 Step 4) — fragile if probes are re-run partially; re-run the whole matrix if in doubt.
