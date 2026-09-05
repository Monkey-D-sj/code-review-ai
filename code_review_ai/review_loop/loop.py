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
from langchain_core.messages import AIMessage, BaseMessage, ToolCall, ToolMessage
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
    Finding,
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


_USAGE_KEYS = ("input_tokens", "output_tokens", "total_tokens")


def _accumulate_usage(usage: dict[str, int], response: AIMessage) -> None:
    """Add one model response's provider-reported tokens to the running total.

    ``usage_metadata`` is optional and provider-shaped; only the keys that are
    present and integral contribute, so a provider that omits a field just never
    populates it.
    """
    metadata = getattr(response, "usage_metadata", None)
    if not isinstance(metadata, dict):
        return
    for key in _USAGE_KEYS:
        value = metadata.get(key)
        if isinstance(value, int):
            usage[key] = usage.get(key, 0) + value


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
) -> LoopResult:
    """Run the review loop until every candidate row is resolved.

    Each model turn may read code with the tools or call ``update_review_item``
    to confirm/dismiss a candidate. The run ends when all candidates are
    resolved (``review_complete``), or when the model stops requesting tools
    while some rows remain unresolved (incomplete), or at ``max_turns`` / on a
    provider failure (``failure_reason``). The resolved worksheet is the result.
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
        state.messages.append(response)
        calls = response.tool_calls  # already a list[ToolCall]
        if not calls:
            break  # the model stopped without resolving every row
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
