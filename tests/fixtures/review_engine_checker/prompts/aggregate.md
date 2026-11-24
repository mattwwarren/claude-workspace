# Aggregation

Combine the role documents below into one review summary.

- Carry every finding from every role document into `findings`, once. Two
  findings about the same file, line and problem are one finding.
- Give each finding a `fingerprint` of 8 to 128 characters drawn only from
  letters, digits, `.`, `_`, `:` and `-`, unique within the summary. Build it
  from the role id and the finding's position, for example `security-1`.
- Give each finding a lowercase dotted `rule_id` such as `security.injection`
  or `quality.logic`. Only lowercase letters, digits and `_` before the first
  dot; lowercase letters, digits, `_` and `-` after it.
- Copy `file` and `line` from the role finding. `line` requires a `file`; when
  a finding has no file, set both `file` and `line` to null.
- Copy `severity`, `summary`, `evidence`, `consequence` and `suggested_fix`
  from the role finding. Use null for `consequence` or `suggested_fix` when
  the role finding left it blank.
- `verdict` is FAIL when any finding is MUST_FIX or SHOULD_FIX, else PASS.
- `coverage` is COMPLETE when every role document has status ok or degraded
  and reviewed the change; PARTIAL when a role was degraded on part of the
  change; FAILED when any role document is failed.
- `risk` is HIGH with a MUST_FIX finding, MEDIUM with a SHOULD_FIX finding,
  else LOW. Put one short sentence per reason in `risk_notes`, or leave it
  empty.
- Leave `acceptance` and `gate_signals` empty.
