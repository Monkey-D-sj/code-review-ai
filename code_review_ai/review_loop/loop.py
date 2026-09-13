"""The hand-rolled ReAct loop over a review.

The runner hands the model a policy, the change and a set of bounded tools; the
loop lets it research and ends on an accepted ``finish_review`` (the submission
is the result), on an empty turn before submitting (the model stopped without a
verdict), or at ``max_turns``. The caller decides which tools exist -- the loop
itself has no notion of "arms".

Dependency discipline: this module imports only ``langchain_core`` message/model
abstractions, never ``langgraph`` and never ``code_review_ai.review_agent``.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from dataclasses import dataclass, field

from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import (
    AIMessage,
    BaseMessage,
    ToolCall,
    ToolMessage,
)
from langchain_core.runnables import Runnable
from pydantic import ValidationError

from code_review_ai.review_loop.hooks import (
    Hooks,
    POINT_MODEL_REQUEST_STARTED,
    POINT_MODEL_RESPONSE_RECEIVED,
    POINT_POST_TOOL,
    POINT_PRE_TOOL,
    POINT_RUN_FINISHED,
)
from code_review_ai.review_loop.schemas import (
    FINISH_REVIEW_TOOL,
    AssistantTurn,
    LoopResult,
    ReviewSubmission,
    ToolCallStatus,
    ToolSpec,
    ToolTrace,
)

# A sentinel bound on model turns so the loop always terminates.
MAX_TURNS = 50
# How much of a tool's returned content the trace keeps. The optimizer needs to
# see what the agent actually read; unbounded, a 50-turn run over whole source
# files would balloon every payload that carries the trace.
TRACE_RESPONSE_EXCERPT_CHARS = 2000


@dataclass
class _LoopState:
    """Everything one run mutates, so the step functions stay single-purpose."""

    tool_map: dict[str, ToolSpec]
    bound: Runnable  # model.bind_tools(...), answering one invoke per turn
    messages: list[BaseMessage]
    result: LoopResult
    hooks: Hooks = field(default_factory=Hooks)
    max_turns: int = MAX_TURNS
    turn: int = 0
    trace_response_excerpt_chars: int = TRACE_RESPONSE_EXCERPT_CHARS

    def emit(self, point: str, **context: object) -> None:
        self.hooks.emit(point, **context)


def _error_content(status: str, message: str) -> str:
    """A stable, machine-readable tool failure without Python internals."""
    return json.dumps({"status": status, "error": message}, ensure_ascii=False)


def _result_status(content: str) -> ToolCallStatus:
    """``success`` unless a tool's returned content reports an error."""
    try:
        payload = json.loads(content)
    except ValueError:
        return "success"
    if isinstance(payload, dict) and payload.get("status") in ("error", "rejected_policy"):
        return "error"
    return "success"


def _bound_schema(spec: ToolSpec) -> dict:
    """Provider-facing schema-only tool definition (no executable StructuredTool)."""
    return {
        "name": spec.name,
        "description": spec.description,
        "input_schema": spec.args_schema.model_json_schema(),
    }


def _validate_args(spec: ToolSpec, call: ToolCall) -> tuple[dict | None, str | None]:
    """Schema-check a tool call before anything runs.

    Returns ``(kwargs, None)`` when the args are valid, or ``(None, rejection)``
    with a machine-readable ``error`` string when they are not. A call that fails
    validation never runs, so it must not fire ``pre_tool``/``post_tool``.
    """
    try:
        validated = spec.args_schema.model_validate(call["args"])
    except ValidationError as exc:
        return None, _error_content(
            "error",
            f"tool arguments do not match the allowed schema "
            f"({exc.error_count()} validation error(s))")
    return validated.model_dump(exclude_unset=True), None


def _execute_tool(spec: ToolSpec, kwargs: dict) -> str:
    """Run a validated tool; a raised failure becomes an error string, never a crash."""
    try:
        return spec.run(**kwargs)
    except Exception as exc:  # a failing tool must not kill the whole review
        return _error_content("error", str(exc))


def _trace_record(call: ToolCall, tool_call_id: str, status: ToolCallStatus, *,
                  response_chars: int = 0,
                  response_excerpt: str = "") -> ToolTrace:
    return {
        "tool_call_id": tool_call_id,
        "tool": call["name"],
        "input": call["args"],
        "status": status,
        "response_chars": response_chars,
        "response_excerpt": response_excerpt,
    }


def _accumulate_usage(usage: dict[str, int], response: AIMessage) -> None:
    """Add one model response's provider-reported tokens to the running total.

    ``usage_metadata`` is optional and provider-shaped; only the keys that are
    present and integral contribute, so a provider that omits a field just never
    populates it. ``cache_read`` (prompt-cache hits) is not part of the top-level
    input total breakdown, so it is read from ``input_token_details.cache_read``
    and accumulated separately -- it prices differently from cache misses, and
    ``miss = input_tokens - cache_read``.
    """
    metadata = getattr(response, "usage_metadata", None)
    if not isinstance(metadata, dict):
        return
    for key in ("input_tokens", "output_tokens", "total_tokens"):
        value = metadata.get(key)
        if isinstance(value, int):
            usage[key] = usage.get(key, 0) + value
    details = metadata.get("input_token_details")
    if not isinstance(details, dict):
        return
    cache_read = details.get("cache_read")
    if isinstance(cache_read, int):
        usage["cache_read"] = usage.get("cache_read", 0) + cache_read


def _coerce_tool_call(call: object) -> dict | None:
    """Normalize one provider tool_call dict (openai or our ToolCall shape)."""
    if not isinstance(call, dict):
        return None
    call_id = call.get("id")
    name = call.get("name")
    args = call.get("args")
    function = call.get("function")
    if isinstance(function, dict):  # openai: {"id", "type", "function":{...}}
        name = function.get("name", name)
        raw_args = function.get("arguments")
        if isinstance(raw_args, str):
            try:
                args = json.loads(raw_args) or {}
            except ValueError:
                args = {}
        else:
            args = raw_args
    if not isinstance(call_id, str) or not isinstance(name, str):
        return None
    return {"id": call_id, "name": name, "args": args if isinstance(args, dict) else {}}


def _pending_tool_call_ids(message: BaseMessage) -> list[str]:
    """tool_call ids an assistant turn requests, from top-level or hidden kwargs."""
    ids = [call["id"] for call in (getattr(message, "tool_calls", None) or [])
           if isinstance(call.get("id"), str)]
    hidden = getattr(message, "additional_kwargs", {}).get("tool_calls")
    if isinstance(hidden, list):
        for call in hidden:
            coerced = _coerce_tool_call(call)
            if coerced is not None:
                ids.append(coerced["id"])
    return ids


def _ensure_tool_replies(messages: list[BaseMessage]) -> None:
    """Guarantee every assistant tool_call has a reply before sending.

    DeepSeek 400s when an assistant message carries tool_calls (including ones
    a provider parser hid in additional_kwargs) with no following tool reply.
    Self-heal the history: whenever a non-tool message appears while some
    tool_call ids are still unanswered, insert an error ToolMessage for each
    right before it. Normally a no-op.
    """
    pending: dict[str, str] = {}
    for index, message in enumerate(messages):
        if isinstance(message, ToolMessage):
            pending.pop(message.tool_call_id, None)
            continue
        if isinstance(message, AIMessage) and message.tool_calls:
            pending = {call_id: index for call_id in _pending_tool_call_ids(message)}
            continue
        if pending:  # a system/user/plain-assistant turn interrupts the replies
            for call_id in sorted(pending):
                messages.insert(index, ToolMessage(
                    content=_error_content(
                        "error", "no recorded reply for this tool call"),
                    tool_call_id=call_id))
            pending = {}
    for call_id in sorted(pending):  # trailing assistant tool_calls at the end
        messages.append(ToolMessage(
            content=_error_content("error", "no recorded reply for this tool call"),
            tool_call_id=call_id))


def _promote_hidden_tool_calls(response: AIMessage) -> None:
    """Lift tool_calls that langchain-deepseek left in additional_kwargs.

    DeepSeek occasionally returns a tool_calls array that the parser stores in
    ``additional_kwargs["tool_calls"]`` instead of the top-level ``.tool_calls``.
    The loop would then see an "empty" assistant turn, nudge instead of
    executing, but the outbound serializer still re-sends the hidden tool_calls
    -- and DeepSeek 400s for lacking replies to it. Promote (and remove) them so
    the loop executes and replies them like any other tool call.
    """
    hidden = response.additional_kwargs.pop("tool_calls", None)
    if response.tool_calls or not isinstance(hidden, list):
        return
    promoted = [call for call in (_coerce_tool_call(call) for call in hidden)
                if call is not None]
    if promoted:
        response.tool_calls = promoted


def _assistant_text(content: object) -> str:
    """Flatten an assistant content (string or list of text blocks) to text."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for block in content:
            if isinstance(block, str):
                parts.append(block)
            elif isinstance(block, dict):
                parts.append(str(block.get("text", "")))
        return "".join(parts)
    return str(content) if content is not None else ""


def _record_assistant_turn(state: _LoopState, response: AIMessage) -> None:
    """Keep every model reply (text + reasoning) for post-hoc debugging.

    Empty turns are deliberately dropped from the sent history (they carry no
    state), so without this the reviewer could not see what the model wrote on
    an empty/failed run.
    """
    reasoning = response.additional_kwargs.get("reasoning_content")
    state.result.assistant_turns.append(AssistantTurn(
        turn=state.turn,
        content=_assistant_text(response.content),
        reasoning=reasoning if isinstance(reasoning, str) else None,
        tool_calls=[call["name"] for call in response.tool_calls]))


def _transient_tool_400(exc: Exception) -> bool:
    """True for DeepSeek's "tool_calls must be followed by tool replies" 400.

    The usual cause is a hidden tool_calls array (see _promote_hidden_tool_calls)
    that left the assistant turn without replies; that is now fixed at the
    source. This retry remains as a second line of defence for the same 400
    from any other shape.
    """
    text = str(exc)
    return ("400" in text and "tool messages following tool_calls" in text)


def _model_turn(state: _LoopState) -> AIMessage | None:
    """One model invoke; ``None`` means the run must stop (provider failure).

    The one retried failure is DeepSeek's tool-reply 400 above: the exact same
    request is sent again once. Everything else fails the run and keeps the
    partial audit trail.
    """
    state.turn += 1
    state.emit(POINT_MODEL_REQUEST_STARTED, turn=state.turn)
    _ensure_tool_replies(state.messages)
    for attempt in (0, 1):
        try:
            response = state.bound.invoke(state.messages)
            break
        except Exception as exc:  # provider failure keeps the partial audit trail
            if attempt == 0 and _transient_tool_400(exc):
                continue
            state.result.failure_reason = f"provider call failed: {exc}"
            return None
    _promote_hidden_tool_calls(response)
    _record_assistant_turn(state, response)
    _accumulate_usage(state.result.usage, response)
    state.emit(POINT_MODEL_RESPONSE_RECEIVED, turn=state.turn,
               response_chars=len(str(response.content)),
               tool_calls=len(response.tool_calls))
    return response


def _reply_call(state: _LoopState, call: ToolCall, name: str,
                content: str, status: ToolCallStatus) -> None:
    """Record and reply to one resolved call: trace entry + one ToolMessage."""
    tool_call_id = call["id"]  # ToolCall.id is a required key
    state.result.tool_trace.append(
        _trace_record(call, tool_call_id, status, response_chars=len(content),
                      response_excerpt=content[:state.trace_response_excerpt_chars]))
    state.messages.append(ToolMessage(
        content=content,
        tool_call_id=tool_call_id,
        name=name))


def _execute_call(state: _LoopState, call: ToolCall) -> None:
    """Answer one requested tool call by running it.

    Error paths return early and never fire ``pre_tool``/``post_tool``; every
    call still gets exactly one ToolMessage back, as the provider protocol
    requires.
    """
    name = call["name"]
    spec = state.tool_map.get(name)
    if spec is None:
        _reply_call(state, call, name,
                    _error_content("error", f"unknown tool {name!r}"), "error")
        return
    kwargs, rejection = _validate_args(spec, call)
    if rejection is not None:
        _reply_call(state, call, name, rejection, "error")
        return
    state.emit(POINT_PRE_TOOL, name=spec.name, args=call["args"])
    content = _execute_tool(spec, kwargs)
    status = _result_status(content)
    state.emit(POINT_POST_TOOL, name=spec.name, status=status,
               response_chars=len(content))
    _reply_call(state, call, name, content, status)


def _settle_result(state: _LoopState) -> None:
    """Shared tail: derive trace counts, emit run_finished."""
    trace = state.result.tool_trace
    state.result.tool_request_count = len(trace)
    state.result.tool_call_count = sum(
        record["status"] == "success" for record in trace)
    state.result.tool_calls = [record["tool"] for record in trace]
    state.emit(POINT_RUN_FINISHED,
               failure_reason=state.result.failure_reason,
               finding_count=len(state.result.findings))


def _apply_finish(state: _LoopState, call: ToolCall) -> bool:
    """Validate a ``finish_review`` submission and stop the run on success.

    Returns True only when the submission was accepted (the review is done);
    an invalid payload is answered as an error and the run continues so the
    model can retry.
    """
    try:
        submission = ReviewSubmission.model_validate(call["args"])
    except ValidationError as exc:
        _reply_call(state, call, FINISH_REVIEW_TOOL,
                    _error_content("error", f"invalid finish_review payload: {exc}"),
                    "error")
        return False
    state.result.findings = list(submission.findings)
    state.result.review_complete = True
    _reply_call(state, call, FINISH_REVIEW_TOOL,
                json.dumps({"accepted": True, "findings": len(submission.findings)},
                           ensure_ascii=False), "success")
    return True


def run_loop(
    model: BaseChatModel,
    tools: Sequence[ToolSpec],
    *,
    initial_messages: list[BaseMessage],
    hooks: Hooks | None = None,
    max_turns: int = MAX_TURNS,
    max_total_tokens: int | None = None,
    trace_response_excerpt_chars: int = TRACE_RESPONSE_EXCERPT_CHARS,
) -> LoopResult:
    """Run one review: the model researches with the tools, then reports.

    Input is only what the caller put in ``initial_messages`` (e.g. a diff plus
    a policy). The model ends by calling ``finish_review`` with its structured
    findings (empty is a valid "no regression" verdict); an empty turn before
    that is a failure (the model stopped without submitting). Which tools exist
    is entirely the caller's choice -- the loop does not distinguish arms.
    """
    state = _LoopState(
        tool_map={spec.name: spec for spec in tools},
        bound=model.bind_tools([_bound_schema(spec) for spec in tools]),
        messages=list(initial_messages),
        result=LoopResult(),
        hooks=hooks if hooks is not None else Hooks(),
        max_turns=max_turns,
        trace_response_excerpt_chars=trace_response_excerpt_chars,
    )
    while True:
        if state.turn >= state.max_turns:
            state.result.failure_reason = (
                f"agent kept requesting tools for {state.max_turns} turns")
            break
        response = _model_turn(state)
        if response is None:
            break
        spent_tokens = state.result.usage.get("total_tokens", 0)
        if max_total_tokens is not None and spent_tokens > max_total_tokens:
            state.result.failure_reason = (
                f"token budget exceeded: spent {spent_tokens} total_tokens, "
                f"limit {max_total_tokens}")
            break
        calls = response.tool_calls  # already a list[ToolCall]
        if not calls:
            # A tool-less assistant turn carries no state (tools carry it) and
            # DeepSeek's serializer can hallucinate a tool_call onto some empty
            # reasoning turns, so it is never appended to the history.
            state.result.failure_reason = (
                "agent stopped without submitting finish_review")
            break
        state.messages.append(response)
        # TODO: history grows unboundedly and every invoke re-sends all of it (a
        # real run hit ~70k chars / 36k input tokens by turn 15). Once history
        # exceeds a threshold, compress early turns (drop or summarize, keeping
        # system + recent turns) without separating an ai message that carries
        # tool_calls from the tool replies that follow it.
        submitted = False
        for call in calls:
            if call["name"] == FINISH_REVIEW_TOOL:
                if _apply_finish(state, call):
                    submitted = True
                    break
                continue  # invalid payload: answered as error, keep going
            _execute_call(state, call)
        if submitted:
            break
    _settle_result(state)
    return state.result

