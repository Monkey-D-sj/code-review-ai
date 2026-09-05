"""The hand-rolled ReAct loop, milestone 1: bare control flow only.

One rule: while the model keeps requesting tools, run every requested call and
feed its result back; the moment a turn requests no tools, the run is over and
that turn's text is the answer (a caller parses it into a report). There is no
submit tool, no turn classification, no refusal/nudge states -- a single turn
counter is the only thing that can stop a looping model.

Dependency discipline: this module imports only ``langchain_core`` message/model
abstractions, never ``langgraph`` and never ``code_review_ai.review_agent``.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from dataclasses import dataclass, field

from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage, ToolMessage, ToolCall
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
    ToolCallStatus,
    ToolSpec,
    ToolTrace,
)

# A sentinel bound on model turns so the loop always terminates; real budgets
# (tool/token/wall-clock) replace this in a later milestone.
MAX_TURNS = 50


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

    def emit(self, point: str, **context: object) -> None:
        self.hooks.emit(point, **context)


def _error_content(status: str, message: str) -> str:
    """A stable, machine-readable tool failure without Python internals."""
    return json.dumps({"status": status, "error": message}, ensure_ascii=False)


def _result_status(content: str) -> ToolCallStatus:
    """``success`` unless a tool's returned content reports an error.

    A tool reports its own failure as a ``{"status": ...}`` JSON string on a
    successful return -- ``error`` or a policy ``rejected_policy`` -- either way
    the call did not complete, so it counts as ``error``.
    """
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


def _model_turn(state: _LoopState) -> AIMessage | None:
    """One model invoke; ``None`` means the run must stop (provider failure)."""
    state.turn += 1
    state.emit(POINT_MODEL_REQUEST_STARTED, turn=state.turn)
    try:
        response = state.bound.invoke(state.messages)
    except Exception as exc:  # provider failure keeps the partial audit trail
        state.result.failure_reason = f"provider call failed: {exc}"
        return None
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


def _execute_call(state: _LoopState, call: ToolCall) -> None:
    """Answer one requested tool call: validate, run (or reject), then reply.

    Error paths return early and never fire ``pre_tool``/``post_tool``; only a
    validated, executed call emits them. Every call still gets exactly one
    ToolMessage back, as the provider protocol requires.
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


def run_loop(
    model: BaseChatModel,
    tools: Sequence[ToolSpec],
    *,
    initial_messages: list[BaseMessage],
    hooks: Hooks | None = None,
    max_turns: int = MAX_TURNS,
) -> LoopResult:
    """Run the review loop until the model stops requesting tools.

    Each model turn either asks for tool calls -- every one is validated and run,
    with one ToolMessage fed back per call -- or asks for none, which ends the
    run with that turn's text as ``LoopResult.final_text``. A provider failure or
    ``max_turns`` of tool-requesting turns ends the run with a ``failure_reason``.

    ``hooks`` carries observer-only events (progress display); there is no policy
    to register because nothing is gated -- a later milestone adds budgets and
    the evidence gate as hardcoded steps, never as hooks.
    """
    tool_map = {spec.name: spec for spec in tools}
    state = _LoopState(
        tool_map=tool_map,
        bound=model.bind_tools([_bound_schema(spec) for spec in tools]),
        messages=list(initial_messages),
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
        calls = response.tool_calls  # already a list[ToolCall]
        if not calls:
            content = response.content
            state.result.final_text = content if isinstance(content, str) else ""
            break
        for call in calls:
            _execute_call(state, call)
    trace = state.result.tool_trace
    state.result.tool_request_count = len(trace)
    state.result.tool_call_count = sum(
        record["status"] == "success" for record in trace)
    state.result.tool_calls = [record["tool"] for record in trace]
    state.emit(POINT_RUN_FINISHED,
               failure_reason=state.result.failure_reason,
               final_chars=len(state.result.final_text or ""))
    return state.result
