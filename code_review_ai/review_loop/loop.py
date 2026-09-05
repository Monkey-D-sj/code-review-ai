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
from langchain_core.messages import AIMessage, BaseMessage, ToolMessage
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
    request_count: int = 0
    sequence: int = 0
    last_ai: AIMessage | None = None

    def emit(self, point: str, **context: object) -> None:
        self.hooks.emit(point, **context)


def _error_content(status: str, message: str) -> str:
    """A stable, machine-readable tool failure without Python internals."""
    return json.dumps({"status": status, "error": message}, ensure_ascii=False)


def _result_status(content: str) -> ToolCallStatus:
    """Classify what a tool returned.

    A tool reports its *own* policy/runtime rejection as a ``{"status": ...}``
    JSON string on a successful return, so those must not count as executions.
    """
    try:
        payload = json.loads(content)
    except ValueError:
        return "executed"
    if isinstance(payload, dict):
        status = payload.get("status")
        if status == "rejected_policy":
            return "rejected_policy"
        if status == "error":
            return "error"
    return "executed"


def _bound_schema(spec: ToolSpec) -> dict:
    """Provider-facing schema-only tool definition (no executable StructuredTool)."""
    return {
        "name": spec.name,
        "description": spec.description,
        "input_schema": spec.args_schema.model_json_schema(),
    }


def _call_id(call: dict, fallback: str) -> str:
    value = call.get("id")
    return value if isinstance(value, str) else fallback


def _call_args(call: dict) -> dict:
    args = call.get("args")
    return args if isinstance(args, dict) else {}


def _validate_args(spec: ToolSpec, call: dict) -> tuple[dict | None, str | None]:
    """Schema-check a tool call before anything runs.

    Returns ``(kwargs, None)`` when the args are valid, or ``(None, rejection)``
    with a machine-readable ``rejected_policy`` string when they are not. A call
    that fails validation never runs, so it must not fire ``pre_tool``/``post_tool``.
    """
    try:
        validated = spec.args_schema.model_validate(_call_args(call))
    except ValidationError as exc:
        return None, _error_content(
            "rejected_policy",
            f"tool arguments do not match the allowed schema "
            f"({exc.error_count()} validation error(s))")
    return validated.model_dump(exclude_unset=True), None


def _execute_tool(spec: ToolSpec, kwargs: dict) -> str:
    """Run a validated tool; a raised failure becomes an error string, never a crash."""
    try:
        return spec.run(**kwargs)
    except Exception as exc:  # a failing tool must not kill the whole review
        return _error_content("error", str(exc))


def _trace_record(sequence: int, call: dict, status: ToolCallStatus, *,
                  response_chars: int = 0) -> ToolTrace:
    return {
        "sequence": sequence,
        "tool_call_id": _call_id(call, f"unknown-{sequence}"),
        "tool": str(call.get("name", "unknown")),
        "input": _call_args(call),
        "status": status,
        "response_chars": response_chars,
    }


def _model_turn(state: _LoopState) -> list[dict] | None:
    """One model invoke; ``None`` means the run must stop (provider failure)."""
    state.turn += 1
    state.emit(POINT_MODEL_REQUEST_STARTED, turn=state.turn)
    try:
        response = state.bound.invoke(state.messages)
    except Exception as exc:  # provider failure keeps the partial audit trail
        state.result.failure_reason = f"provider call failed: {exc}"
        return None
    state.last_ai = response
    calls = list(response.tool_calls)
    state.emit(POINT_MODEL_RESPONSE_RECEIVED, turn=state.turn,
               response_chars=len(str(response.content)),
               tool_calls=len(calls))
    return calls


def _execute_call(state: _LoopState, call: dict) -> None:
    """Answer one requested tool call: validate, run (or reject), and reply.

    A call that fails schema validation, or names a tool this run does not
    expose, never runs and never fires ``pre_tool``/``post_tool``; every call
    still gets exactly one ToolMessage back, as the provider protocol requires.
    """
    state.request_count += 1
    spec = state.tool_map.get(str(call.get("name", "")))
    if spec is None:
        content = _error_content("rejected_policy",
                                 f"unknown tool {str(call.get('name', ''))!r}")
        status: ToolCallStatus = "rejected_policy"
    else:
        kwargs, rejection = _validate_args(spec, call)
        if rejection is not None:
            content, status = rejection, "rejected_policy"
        else:
            state.emit(POINT_PRE_TOOL, name=spec.name, args=_call_args(call))
            content = _execute_tool(spec, kwargs)
            status = _result_status(content)
            state.emit(POINT_POST_TOOL, name=spec.name, status=status,
                       response_chars=len(content))
    state.sequence += 1
    state.result.tool_trace.append(
        _trace_record(state.sequence, call, status, response_chars=len(content)))
    state.result.tool_calls.append(str(call.get("name", "")))
    if status == "executed":
        state.result.tool_call_count += 1
    state.messages.append(ToolMessage(
        content=content,
        tool_call_id=_call_id(call, f"unknown-{state.sequence}"),
        name=str(call.get("name", "unknown"))))


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
        calls = _model_turn(state)
        if calls is None:
            break
        if not calls:
            content = state.last_ai.content if state.last_ai is not None else ""
            state.result.final_text = content if isinstance(content, str) else ""
            break
        for call in calls:
            _execute_call(state, call)
    state.result.tool_request_count = state.request_count
    state.emit(POINT_RUN_FINISHED,
               failure_reason=state.result.failure_reason,
               final_chars=len(state.result.final_text or ""))
    return state.result
