"""The hand-rolled ReAct loop over a review worksheet.

The runner seeds a deterministic worksheet (one candidate row per changed
symbol); the loop lets the model read code with the bounded tools and resolve
rows through ``update_review_item`` (schema-only: the loop applies it to the
rows itself). The run ends when every candidate is resolved, when the model
stops requesting tools without resolving everything (incomplete), or at
``max_turns`` -- there is no free-form report, the resolved rows are the result.

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
    HumanMessage,
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
    LoopResult,
    ReviewItem,
    ReviewItemUpdate,
    ToolCallStatus,
    ToolSpec,
    ToolTrace,
    UPDATE_REVIEW_TOOL,
)

# A sentinel bound on model turns so the loop always terminates.
MAX_TURNS = 50
# How many consecutive empty (no tool_calls) turns with unresolved rows are
# tolerated before the run fails; each one before the cap is nudged instead.
MAX_EMPTY_TURNS = 2


@dataclass
class _LoopState:
    """Everything one run mutates, so the step functions stay single-purpose."""

    tool_map: dict[str, ToolSpec]
    bound: Runnable  # model.bind_tools(...), answering one invoke per turn
    messages: list[BaseMessage]
    candidates: dict[str, ReviewItem]  # the worksheet, mutated by updates
    result: LoopResult
    hooks: Hooks = field(default_factory=Hooks)
    max_turns: int = MAX_TURNS
    max_empty_turns: int = MAX_EMPTY_TURNS
    empty_turns: int = 0  # consecutive empty turns with rows still unresolved
    turn: int = 0

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
                  response_chars: int = 0) -> ToolTrace:
    return {
        "tool_call_id": tool_call_id,
        "tool": call["name"],
        "input": call["args"],
        "status": status,
        "response_chars": response_chars,
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


def _model_turn(state: _LoopState) -> AIMessage | None:
    """One model invoke; ``None`` means the run must stop (provider failure)."""
    state.turn += 1
    state.emit(POINT_MODEL_REQUEST_STARTED, turn=state.turn)
    try:
        response = state.bound.invoke(state.messages)
    except Exception as exc:  # provider failure keeps the partial audit trail
        state.result.failure_reason = f"provider call failed: {exc}"
        return None
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
        _trace_record(call, tool_call_id, status, response_chars=len(content)))
    state.messages.append(ToolMessage(
        content=content,
        tool_call_id=tool_call_id,
        name=name))


def _all_resolved(candidates: dict[str, ReviewItem]) -> bool:
    return all(item.state != "candidate" for item in candidates.values())


def _unresolved_qnames(candidates: dict[str, ReviewItem]) -> list[str]:
    return [item.qname for item in candidates.values() if item.state == "candidate"]


def _nudge_message(candidates: dict[str, ReviewItem]) -> HumanMessage:
    """One user turn pushing an empty-turn stop back to finish the worksheet."""
    remaining = _unresolved_qnames(candidates)
    content = ("尚有 candidate 未决。请对以下每一行调用 update_review_item 给出决定"
               "（confirmed 附 finding / dismissed 附 reason）："
               + ", ".join(remaining)
               + "。全部行决完前不要以空轮结束。")
    return HumanMessage(content=content)


def _apply_update(state: _LoopState, call: ToolCall) -> None:
    """Resolve one candidate row from an ``update_review_item`` call.

    The model may only flip an existing candidate row (confirmed with a finding,
    or dismissed with a reason); anything else is answered as an error and the
    worksheet is left untouched.
    """
    try:
        transition = ReviewItemUpdate.model_validate(call["args"])
    except ValidationError as exc:
        _reply_call(state, call, UPDATE_REVIEW_TOOL,
                    _error_content("error", f"invalid update_review_item payload: {exc}"),
                    "error")
        return
    item = state.candidates.get(transition.qname)
    if item is None or item.state != "candidate":
        _reply_call(state, call, UPDATE_REVIEW_TOOL,
                    _error_content("error",
                                   f"qname {transition.qname!r} is not an active candidate"),
                    "error")
        return
    resolved = item.model_copy(update={
        "state": transition.state,
        "finding": transition.finding,
        "reason": transition.reason,
    })
    state.candidates[transition.qname] = resolved
    _reply_call(state, call, UPDATE_REVIEW_TOOL,
                json.dumps({"accepted": True, "qname": transition.qname},
                           ensure_ascii=False), "success")


def _execute_call(state: _LoopState, call: ToolCall) -> None:
    """Answer one requested tool call: resolve the worksheet or run a tool.

    Error paths return early and never fire ``pre_tool``/``post_tool``; every
    call still gets exactly one ToolMessage back, as the provider protocol
    requires.
    """
    name = call["name"]
    if name == UPDATE_REVIEW_TOOL:
        _apply_update(state, call)
        return
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


def run_loop(
    model: BaseChatModel,
    tools: Sequence[ToolSpec],
    candidates: Sequence[ReviewItem],
    *,
    initial_messages: list[BaseMessage],
    hooks: Hooks | None = None,
    max_turns: int = MAX_TURNS,
    max_total_tokens: int | None = None,
    max_empty_turns: int = MAX_EMPTY_TURNS,
) -> LoopResult:
    """Run the review loop until every candidate row is resolved.

    Each model turn may read code with the tools or call ``update_review_item``
    to confirm/dismiss a candidate. An empty turn (no tool calls) with rows
    still unresolved is not a finish: it is nudged back with the pending rows,
    and only ``max_empty_turns`` consecutive such turns fail the run. The run
    otherwise ends when all candidates are resolved (``review_complete``), or at
    ``max_turns`` / on a provider failure / past ``max_total_tokens``
    (``failure_reason`` -- the token cap is checked right after each model turn,
    against the accumulated provider-reported ``total_tokens``). The resolved
    worksheet is the result.
    """
    tool_map = {spec.name: spec for spec in tools}
    state = _LoopState(
        tool_map=tool_map,
        bound=model.bind_tools([_bound_schema(spec) for spec in tools]),
        messages=list(initial_messages),
        candidates={item.qname: item.model_copy() for item in candidates},
        result=LoopResult(),
        hooks=hooks if hooks is not None else Hooks(),
        max_turns=max_turns,
        max_empty_turns=max_empty_turns,
    )
    while True:
        if state.turn >= state.max_turns:
            state.result.failure_reason = (
                f"agent kept requesting tools for {state.max_turns} turns")
            break
        response = _model_turn(state)
        if response is None:
            break
        # The assistant turn must precede the tool replies it asked for: a
        # provider rejects a 'tool' message unless the assistant message with
        # the matching tool_calls is already in the history.
        spent_tokens = state.result.usage.get("total_tokens", 0)
        if max_total_tokens is not None and spent_tokens > max_total_tokens:
            state.result.failure_reason = (
                f"token budget exceeded: spent {spent_tokens} total_tokens, "
                f"limit {max_total_tokens}")
            break
        state.messages.append(response)
        # TODO: history grows unboundedly and every invoke re-sends all of it (a
        # real run hit ~70k chars / 36k input tokens by turn 15). Once history
        # exceeds a threshold, compress early turns (drop or summarize, keeping
        # system + worksheet + recent turns) without separating an ai message
        # that carries tool_calls from the tool replies that follow it.
        calls = response.tool_calls  # already a list[ToolCall]
        if not calls:
            if _all_resolved(state.candidates):
                state.result.review_complete = True
                break  # every row decided; the model's closing turn is fine
            state.empty_turns += 1
            if state.empty_turns > state.max_empty_turns:
                state.result.failure_reason = (
                    "agent stopped without resolving the worksheet: "
                    f"{len(_unresolved_qnames(state.candidates))} candidate(s) "
                    f"unresolved after {state.empty_turns} empty turn(s)")
                break
            state.messages.append(_nudge_message(state.candidates))
            continue
        state.empty_turns = 0  # the model acted; a later empty turn restarts
        for call in calls:
            _execute_call(state, call)
        if _all_resolved(state.candidates):
            state.result.review_complete = True
            break
    trace = state.result.tool_trace
    state.result.tool_request_count = len(trace)
    state.result.tool_call_count = sum(
        record["status"] == "success" for record in trace)
    state.result.tool_calls = [record["tool"] for record in trace]
    state.result.items = {qname: item.model_copy() for qname, item in state.candidates.items()}
    state.result.findings = [
        item.finding for item in state.result.items.values()
        if item.state == "confirmed" and item.finding is not None]
    state.emit(POINT_RUN_FINISHED,
               failure_reason=state.result.failure_reason,
               finding_count=len(state.result.findings))
    return state.result
