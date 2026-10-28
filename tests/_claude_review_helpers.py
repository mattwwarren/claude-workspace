"""``claude -p --output-format json`` envelope builders for the Claude confirm (#2791).

The only home of Claude envelope fixtures. Every envelope here starts from a
REAL capture taken by the orchestrator on 2026-10-28 against claude 2.1.312,
redacted (``session_id``, ``uuid``, and the throwaway temp paths) but not
tidied: the key set, key order, nulls and float noise are as observed. The
literals are split across source lines only to satisfy the line-length rule;
the concatenated value is the captured text byte for byte.

Capture command for :data:`CAPTURED_REVIEW_ENVELOPE` (cwd a throwaway git
directory, environment copied from the live ``cw dev-queue serve`` process,
``start_new_session=True``, prompt over stdin)::

    claude -p --output-format json \\
        --json-schema "$(ReviewerFindingsDocument.model_json_schema())" \\
        --tools Read,Grep,Glob --permission-mode dontAsk \\
        --setting-sources "" --strict-mcp-config --disable-slash-commands \\
        --no-session-persistence

Observed shape: ``result`` carries the reviewer document as a JSON *string*
and ``structured_output`` carries the same document as an *object*.
``stop_reason`` was ``tool_use`` on that schema run, so it is not an error
signal.

Deferred-premise evidence (orchestrator runs, ``.cw/dp-evidence``):

- DP3 (detached-driver auth): exit 0, ``is_error: false``, ``subtype:
  "success"``, ``result: "OK"`` -- :data:`CAPTURED_AUTH_ENVELOPE`. That run
  loaded user settings and cost ~$0.48 (120k cache-creation tokens); with
  ``--setting-sources ""`` a review role cost ~$0.04.
- DP2 (``dontAsk`` permits in-worktree reads) and DP1 (out-of-cwd Read
  denied under ``--tools Read,Grep,Glob --permission-mode dontAsk
  --setting-sources ""``): :data:`CAPTURED_DENIAL_ENVELOPE`, whose
  ``permission_denials`` names the outside path and whose ``result`` quotes
  "Permission to use Read has been denied because Claude Code is running in
  don't ask mode." ``--setting-sources project`` was observed to run a
  project ``UserPromptSubmit`` hook; ``""`` did not.

The error, auth-failure and truncated envelopes were NOT captured live. They
are synthesised from the real envelope's key set (``is_error`` flipped, a
non-document ``result``, or the captured text cut short) and say so.
"""

from __future__ import annotations

import copy
import json
import re
from typing import TYPE_CHECKING

from cw.claude_review import ClaudeRunResult

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping
    from pathlib import Path

_ROLE_LINE = re.compile(r"^# Reviewer Role: (.+)$", re.MULTILINE)


class RoleAwareClaudeRunner:
    """A ``ClaudeRunner`` answering each role with *respond(role)*.

    Reads the role from the prompt's ``# Reviewer Role:`` line and records
    every role, prompt and argv. *side_effect*, when set, runs against the worktree
    during each call (the G4 worktree-mutation cases).
    """

    def __init__(
        self,
        respond: Callable[[str], ClaudeRunResult],
        side_effect: Callable[[Path], None] | None = None,
    ) -> None:
        self._respond = respond
        self._side_effect = side_effect
        self.roles: list[str] = []
        self.prompts: list[str] = []
        self.argvs: list[list[str]] = []

    def run(
        self,
        worktree: Path,
        argv: list[str],
        _timeout_seconds: int | None,
        *,
        stdin: str,
    ) -> ClaudeRunResult:
        match = _ROLE_LINE.search(stdin)
        assert match is not None, "every reviewer prompt names its role"
        role = match.group(1).strip()
        self.roles.append(role)
        self.prompts.append(stdin)
        self.argvs.append(list(argv))
        if self._side_effect is not None:
            self._side_effect(worktree)
        return self._respond(role)


CAPTURED_REVIEW_ENVELOPE = (
    '{"duration_api_ms":8181,"stop_reason":"tool_use","session_id":"<re'
    'dacted-session-id>","total_cost_usd":0.036682000000000006,"usage":'
    '{"input_tokens":4,"cache_creation_input_tokens":8140,"cache_read_i'
    'nput_tokens":8040,"output_tokens":331,"output_tokens_details":{"th'
    'inking_tokens":0},"server_tool_use":{"web_search_requests":0,"web_'
    'fetch_requests":0},"service_tier":"standard","cache_creation":{"ep'
    'hemeral_1h_input_tokens":8140,"ephemeral_5m_input_tokens":0},"infe'
    'rence_geo":"global","iterations":[{"input_tokens":2,"output_tokens'
    '":257,"cache_read_input_tokens":8040,"cache_creation_input_tokens"'
    ':100,"cache_creation":{"ephemeral_5m_input_tokens":0,"ephemeral_1h'
    '_input_tokens":100},"type":"message"}],"speed":"standard","fallbac'
    'k_credit":null},"modelUsage":{"claude-sonnet-5-5":{"inputTokens":4'
    ',"outputTokens":331,"cacheReadInputTokens":8040,"cacheCreationInpu'
    'tTokens":8140,"webSearchRequests":0,"costUSD":0.036682000000000006'
    ',"contextWindow":1000000,"maxOutputTokens":128000,"thinkingTokens"'
    ':0,"cacheCreationEphemeral5mInputTokens":0,"cacheCreationEphemeral'
    '1hInputTokens":8140,"canonicalModel":"claude-sonnet-5-5","provider'
    '":"firstParty","costBasis":"list"}},"permission_denials":[],"termi'
    'nal_reason":"completed","fast_mode_state":"off","fast_mode_disable'
    'd_reason":"sdk_opt_in_required","subagent_stats":{"spawned":0,"req'
    'uested":{"background":0,"foreground":0,"unset":0},"started_in_back'
    'ground":0,"max_depth":0,"spawned_by_subagents":0,"completed":0,"fa'
    'iled":0,"killed":{"parent":0,"user":0,"system":0},"refused":{"dept'
    'h_limit":0,"concurrency_limit":0,"budget":0},"by_type":{}},"safety'
    '_stops":0,"is_error":false,"num_turns":3,"subtype":"success","api_'
    'error_status":null,"result":"{\\"reviewer_role\\":\\"correctness\\",\\"'
    'status\\":\\"ok\\",\\"findings\\":[{\\"severity\\":\\"MUST_FIX\\",\\"file\\":'
    '\\"a.py\\",\\"line_start\\":2,\\"line_end\\":2,\\"summary\\":\\"f(x) uncond'
    'itionally divides by zero\\",\\"consequence\\":\\"Every call to f rais'
    'es ZeroDivisionError; the function can never return a value.\\",\\"s'
    'uggested_fix\\":\\"Divide by a meaningful non-zero divisor, or guard'
    "/handle the zero case explicitly if the intent is something else.\\"
    '",\\"evidence\\":\\"return x/0\\",\\"confidence\\":\\"HIGH\\"}]}","structu'
    'red_output":{"reviewer_role":"correctness","status":"ok","findings'
    '":[{"severity":"MUST_FIX","file":"a.py","line_start":2,"line_end":'
    '2,"summary":"f(x) unconditionally divides by zero","consequence":"'
    "Every call to f raises ZeroDivisionError; the function can never r"
    'eturn a value.","suggested_fix":"Divide by a meaningful non-zero d'
    "ivisor, or guard/handle the zero case explicitly if the intent is "
    'something else.","evidence":"return x/0","confidence":"HIGH"}]},"t'
    'tft_ms":5835,"type":"result","duration_ms":8341,"uuid":"<redacted-'
    'uuid>","ttft_stream_ms":5158,"time_to_request_ms":138,"first_conte'
    'nt_frame_ms":5158,"queued_turn_count":0,"result_index":0}'
)

CAPTURED_AUTH_ENVELOPE = (
    '{"duration_api_ms":3972,"stop_reason":"end_turn","session_id":"<re'
    'dacted-session-id>","total_cost_usd":0.48396,"usage":{"input_token'
    's":2,"cache_creation_input_tokens":120979,"cache_read_input_tokens'
    '":0,"output_tokens":4,"output_tokens_details":{"thinking_tokens":0'
    '},"server_tool_use":{"web_search_requests":0,"web_fetch_requests":'
    '0},"service_tier":"standard","cache_creation":{"ephemeral_1h_input'
    '_tokens":120979,"ephemeral_5m_input_tokens":0},"inference_geo":"gl'
    'obal","iterations":[{"input_tokens":2,"output_tokens":4,"cache_rea'
    'd_input_tokens":0,"cache_creation_input_tokens":120979,"cache_crea'
    'tion":{"ephemeral_5m_input_tokens":0,"ephemeral_1h_input_tokens":1'
    '20979},"type":"message"}],"speed":"standard","fallback_credit":nul'
    'l},"modelUsage":{"claude-sonnet-5-5":{"inputTokens":2,"outputToken'
    's":4,"cacheReadInputTokens":0,"cacheCreationInputTokens":120979,"w'
    'ebSearchRequests":0,"costUSD":0.48396,"contextWindow":1000000,"max'
    'OutputTokens":128000,"thinkingTokens":0,"cacheCreationEphemeral5mI'
    'nputTokens":0,"cacheCreationEphemeral1hInputTokens":120979,"canoni'
    'calModel":"claude-sonnet-5-5","provider":"firstParty","costBasis":'
    '"list"}},"permission_denials":[],"terminal_reason":"completed","fa'
    'st_mode_state":"off","fast_mode_disabled_reason":"sdk_opt_in_requi'
    'red","subagent_stats":{"spawned":0,"requested":{"background":0,"fo'
    'reground":0,"unset":0},"started_in_background":0,"max_depth":0,"sp'
    'awned_by_subagents":0,"completed":0,"failed":0,"killed":{"parent":'
    '0,"user":0,"system":0},"refused":{"depth_limit":0,"concurrency_lim'
    'it":0,"budget":0},"by_type":{}},"safety_stops":0,"is_error":false,'
    '"num_turns":1,"subtype":"success","api_error_status":null,"result"'
    ':"OK","ttft_ms":3846,"type":"result","duration_ms":4136,"uuid":"<r'
    'edacted-uuid>","ttft_stream_ms":3412,"time_to_request_ms":121,"fir'
    'st_content_frame_ms":3412,"queued_turn_count":0,"result_index":0}'
)

CAPTURED_DENIAL_ENVELOPE = (
    '{"duration_api_ms":4276,"stop_reason":"end_turn","session_id":"<re'
    'dacted-session-id>","total_cost_usd":0.015066000000000001,"usage":'
    '{"input_tokens":4,"cache_creation_input_tokens":2790,"cache_read_i'
    'nput_tokens":8780,"output_tokens":302,"output_tokens_details":{"th'
    'inking_tokens":0},"server_tool_use":{"web_search_requests":0,"web_'
    'fetch_requests":0},"service_tier":"standard","cache_creation":{"ep'
    'hemeral_1h_input_tokens":2790,"ephemeral_5m_input_tokens":0},"infe'
    'rence_geo":"global","iterations":[{"input_tokens":2,"output_tokens'
    '":137,"cache_read_input_tokens":5557,"cache_creation_input_tokens"'
    ':456,"cache_creation":{"ephemeral_5m_input_tokens":0,"ephemeral_1h'
    '_input_tokens":456},"type":"message"}],"speed":"standard","fallbac'
    'k_credit":null},"modelUsage":{"claude-sonnet-5-5":{"inputTokens":4'
    ',"outputTokens":302,"cacheReadInputTokens":8780,"cacheCreationInpu'
    'tTokens":2790,"webSearchRequests":0,"costUSD":0.015066000000000001'
    ',"contextWindow":1000000,"maxOutputTokens":128000,"thinkingTokens"'
    ':0,"cacheCreationEphemeral5mInputTokens":0,"cacheCreationEphemeral'
    '1hInputTokens":2790,"canonicalModel":"claude-sonnet-5-5","provider'
    '":"firstParty","costBasis":"list"}},"permission_denials":[{"tool_n'
    'ame":"Read","tool_use_id":"toolu_01NV8KWkmjT2xpQSwyFy2FwY","tool_i'
    'nput":{"file_path":"<outside-dir>/secret.txt"}}],"terminal_reason"'
    ':"completed","fast_mode_state":"off","fast_mode_disabled_reason":"'
    'sdk_opt_in_required","subagent_stats":{"spawned":0,"requested":{"b'
    'ackground":0,"foreground":0,"unset":0},"started_in_background":0,"'
    'max_depth":0,"spawned_by_subagents":0,"completed":0,"failed":0,"ki'
    'lled":{"parent":0,"user":0,"system":0},"refused":{"depth_limit":0,'
    '"concurrency_limit":0,"budget":0},"by_type":{}},"safety_stops":0,"'
    'is_error":false,"num_turns":3,"subtype":"success","api_error_statu'
    's":null,"result":"**inside.txt** contains:\\n\\n> INSIDE-SENTINEL-44'
    "71\\n\\n**secret.txt**: the read was denied. The exact error was:\\n\\"
    "n> Permission to use Read has been denied because Claude Code is r"
    "unning in don't ask mode.\\n\\nThe file is outside the working direc"
    "tory, and I didn't try to get around the denial. To read it, you c"
    "ould allow access to that path or change the permission mode. Tell"
    ' me if you want me to try again after that.","ttft_ms":1937,"type"'
    ':"result","duration_ms":4420,"uuid":"<redacted-uuid>","ttft_stream'
    '_ms":1499,"time_to_request_ms":125,"first_content_frame_ms":1500,"'
    'queued_turn_count":0,"result_index":0}'
)

#: The captured run's ``total_cost_usd`` (logged per role, never persisted).
CAPTURED_COST_USD = 0.036682000000000006


def _captured() -> dict[str, object]:
    loaded: dict[str, object] = json.loads(CAPTURED_REVIEW_ENVELOPE)
    return copy.deepcopy(loaded)


def review_envelope(
    document: Mapping[str, object] | str | None = None,
    *,
    structured_output: bool = True,
) -> str:
    """The captured review envelope carrying *document* in place of the real one.

    *document* is a mapping or JSON text (``_doc_json``'s output); ``None``
    keeps the captured ``correctness`` document. Both ``result`` (as a JSON
    string) and ``structured_output`` (as an object) are replaced, exactly as
    the real envelope carries them. ``structured_output=False`` drops that key,
    exercising the ``json.loads(result)`` fallback.
    """
    envelope = _captured()
    if document is not None:
        doc = json.loads(document) if isinstance(document, str) else dict(document)
        envelope["result"] = json.dumps(doc)
        envelope["structured_output"] = doc
    if not structured_output:
        del envelope["structured_output"]
    return json.dumps(envelope)


def error_envelope(message: str = "API Error: 500 Internal server error") -> str:
    """SYNTHESISED from the real envelope's key set: ``is_error: true``.

    Not captured live. ``structured_output`` is dropped and ``result`` carries
    an error string, since an errored run produced no document.
    """
    envelope = _captured()
    envelope["is_error"] = True
    envelope["result"] = message
    del envelope["structured_output"]
    return json.dumps(envelope)


def auth_failure_envelope() -> str:
    """SYNTHESISED from the real envelope's key set: an authentication failure.

    Not captured live (DP3 passed, so no failure was observed). The wording
    of ``result`` is illustrative only; nothing parses it.
    """
    return error_envelope("Invalid API key - Please run /login")


def truncated_envelope() -> str:
    """SYNTHESISED: the captured envelope text cut off mid-object."""
    return CAPTURED_REVIEW_ENVELOPE[: len(CAPTURED_REVIEW_ENVELOPE) // 2]


def non_document_envelope() -> str:
    """The captured DP3 auth envelope: a success whose ``result`` is not a document."""
    return CAPTURED_AUTH_ENVELOPE
