"""Behavior tests for the hand-rolled review loop (review_loop.loop).

No DB, no network: a scripted model returns one ``(content, tool_calls)`` pair
per turn and stub tools echo/deny/fail on demand. The contract under test is the
minimal natural-stop loop: keep running tools while the model asks for them, end
when a turn asks for none.
"""

from __future__ import annotations

import json

from langchain_core.messages import AIMessage, ToolMessage
from pydantic import BaseModel, ConfigDict

from code_review_ai.review_loop import (
    Hooks,
    POINT_MODEL_REQUEST_STARTED,
    POINT_MODEL_RESPONSE_RECEIVED,
    POINT_POST_TOOL,
    POINT_PRE_TOOL,
    POINT_RUN_FINISHED,
    ToolSpec,
    run_loop,
)


class EchoArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")

    text: str


class ImpactArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")

    symbols: list[str]


class BoomArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")

    text: str


class GuardedArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")

    target: str


def _echo(text: str) -> str:
    return f"echo:{text}"


def _boom(text: str) -> str:
    raise RuntimeError(f"boom: {text}")


def _guarded(target: str) -> str:
    """A tool that reports its own policy denial as JSON content on success."""
    return json.dumps({"status": "rejected_policy", "error": f"denied:{target}"},
                      ensure_ascii=False)


def _call(name: str, args: dict | None = None, ident: str | None = None) -> dict:
    return {"name": name, "args": args or {}, "id": ident or f"{name}-call"}


def _make_tools():
    """Four action tools; there is deliberately no submit/terminal tool."""
    return [
        ToolSpec(name="echo", description="echo text back", args_schema=EchoArgs,
                 run=_echo),
        ToolSpec(name="impact", description="stub impact", args_schema=ImpactArgs,
                 run=lambda symbols: json.dumps({"found": symbols}, ensure_ascii=False)),
        ToolSpec(name="boom", description="stub that fails", args_schema=BoomArgs,
                 run=_boom),
        ToolSpec(name="guarded", description="stub that denies", args_schema=GuardedArgs,
                 run=_guarded),
    ]


class FakeModel:
    """bind_tools-shaped fake: records what it binds, replays a script of turns.

    Each schedule entry is a ``(content, tool_calls)`` pair, or an ``Exception``
    to simulate a provider failure.
    """

    def __init__(self, schedule):
        self._schedule = list(schedule)
        self.bound_schemas: list[dict] | None = None
        self.invoked: list[list] = []

    def bind_tools(self, tools):
        self.bound_schemas = list(tools)
        return self

    def invoke(self, messages):
        self.invoked.append(list(messages))
        step = self._schedule.pop(0)
        if isinstance(step, Exception):
            raise step
        content, calls = step
        return AIMessage(content=content, tool_calls=calls)


def _run(model, tools, **kwargs):
    return run_loop(model, tools, initial_messages=[], **kwargs)


def _tool_contents(model: FakeModel) -> list[str]:
    """Every ToolMessage content the model has been shown, in order."""
    return [str(message.content)
            for batch in model.invoked
            for message in batch if isinstance(message, ToolMessage)]


def test_action_then_natural_stop_returns_the_final_text():
    model = FakeModel([("", [_call("impact", {"symbols": ["app::run"]}, "impact-1")]),
                       ("1 bug in app.py, line 7: unchecked return.", [])])

    result = _run(model, _make_tools())

    assert result.failure_reason is None
    assert result.final_text == "1 bug in app.py, line 7: unchecked return."
    assert result.tool_calls == ["impact"]
    assert result.tool_call_count == 1
    assert result.tool_request_count == 1
    assert [record["status"] for record in result.tool_trace] == ["executed"]


def test_immediate_stop_without_tools_is_a_clean_finish():
    model = FakeModel([("nothing to flag.", [])])

    result = _run(model, _make_tools())

    assert result.failure_reason is None
    assert result.final_text == "nothing to flag."
    assert result.tool_call_count == 0
    assert result.tool_trace == []


def test_bound_schemas_are_schema_only_dicts():
    model = FakeModel([("done.", [])])
    _run(model, _make_tools())

    names = {schema["name"] for schema in model.bound_schemas}
    assert names == {"echo", "impact", "boom", "guarded"}
    assert all(isinstance(schema["input_schema"], dict) for schema in model.bound_schemas)


def test_invalid_args_are_a_policy_rejection_not_an_execution():
    model = FakeModel([("", [_call("impact", {"symbols": "not-a-list"}, "impact-1")]),
                       ("done.", [])])

    result = _run(model, _make_tools())

    assert result.final_text == "done."
    assert result.tool_call_count == 0
    assert result.tool_request_count == 1
    assert [record["status"] for record in result.tool_trace] == ["rejected_policy"]
    assert any("rejected_policy" in content for content in _tool_contents(model))


def test_runtime_tool_failure_is_answered_and_the_loop_continues():
    model = FakeModel([("", [_call("boom", {"text": "kaboom"}, "boom-1")]),
                       ("done.", [])])

    result = _run(model, _make_tools())

    assert result.final_text == "done."
    assert [record["status"] for record in result.tool_trace] == ["error"]
    assert any("boom: kaboom" in content for content in _tool_contents(model))


def test_tool_self_reported_policy_denial_does_not_count_as_execution():
    model = FakeModel([("", [_call("guarded", {"target": "x"}, "guard-1")]),
                       ("done.", [])])

    result = _run(model, _make_tools())

    assert result.tool_call_count == 0
    assert result.tool_request_count == 1
    assert [record["status"] for record in result.tool_trace] == ["rejected_policy"]


def test_several_tools_in_one_turn_all_run_in_order():
    model = FakeModel([("", [_call("impact", {"symbols": ["x"]}, "impact-1"),
                             _call("echo", {"text": "hi"}, "echo-1")]),
                       ("done.", [])])

    result = _run(model, _make_tools())

    assert result.final_text == "done."
    assert result.tool_calls == ["impact", "echo"]
    assert result.tool_call_count == 2
    assert [record["status"] for record in result.tool_trace] == ["executed", "executed"]


def test_unknown_tool_is_rejected_and_the_loop_continues():
    model = FakeModel([("", [_call("no_such_tool", ident="ghost-1")]),
                       ("done.", [])])

    result = _run(model, _make_tools())

    assert result.final_text == "done."
    assert result.tool_call_count == 0
    assert result.tool_request_count == 1
    assert [record["status"] for record in result.tool_trace] == ["rejected_policy"]
    assert any("unknown tool" in content for content in _tool_contents(model))


def test_provider_failure_keeps_the_partial_audit_trail():
    model = FakeModel([("", [_call("impact", {"symbols": ["x"]}, "impact-1")]),
                       RuntimeError("connection reset")])

    result = _run(model, _make_tools())

    assert result.final_text is None
    assert result.failure_reason == "provider call failed: connection reset"
    assert len(result.tool_trace) == 1
    assert result.tool_trace[0]["status"] == "executed"
    assert result.tool_call_count == 1


def test_turn_cap_stops_a_looping_model():
    model = FakeModel([("", [_call("echo", {"text": "a"}, "a-1")]),
                       ("", [_call("echo", {"text": "b"}, "b-1")]),
                       ("", [_call("echo", {"text": "c"}, "c-1")]),
                       ("", [_call("echo", {"text": "d"}, "d-1")])])

    result = run_loop(model, _make_tools(), initial_messages=[], max_turns=3)

    assert result.final_text is None
    assert result.failure_reason == "agent kept requesting tools for 3 turns"
    assert result.tool_call_count == 3
    assert result.tool_request_count == 3


def test_happy_path_emits_the_observer_event_sequence():
    model = FakeModel([("", [_call("impact", {"symbols": ["x"]}, "impact-1")]),
                       ("done: no findings.", [])])
    hooks = Hooks()
    seen: list[str] = []
    run_context: dict = {}

    for point in (POINT_MODEL_REQUEST_STARTED, POINT_MODEL_RESPONSE_RECEIVED,
                  POINT_PRE_TOOL, POINT_POST_TOOL, POINT_RUN_FINISHED):
        hooks.on(point, lambda event, context, point=point: seen.append(point))
    hooks.on(POINT_RUN_FINISHED, lambda _event, context: run_context.update(context))

    result = _run(model, _make_tools(), hooks=hooks)

    assert result.final_text == "done: no findings."
    assert seen == ["model_request_started", "model_response_received",
                    "pre_tool", "post_tool",
                    "model_request_started", "model_response_received",
                    "run_finished"]
    assert run_context["final_chars"] == len("done: no findings.")
    assert run_context["failure_reason"] is None


def test_schema_rejected_call_never_fires_pre_or_post_tool():
    model = FakeModel([("", [_call("impact", {"symbols": "not-a-list"}, "impact-1")]),
                       ("done.", [])])
    hooks = Hooks()
    tool_events: list[str] = []
    for point in (POINT_PRE_TOOL, POINT_POST_TOOL):
        hooks.on(point, lambda event, _context, point=point: tool_events.append(point))

    result = _run(model, _make_tools(), hooks=hooks)

    assert result.final_text == "done."
    assert tool_events == []  # the call was rejected before it could run


def test_hooks_run_observers_in_registration_order_with_context():
    hooks = Hooks()
    received: list[tuple[str, int]] = []

    def first(point: str, context: dict) -> None:
        received.append((point, context["n"]))  # type: ignore[arg-type]

    def second(point: str, context: dict) -> None:
        received.append((point, context["n"] + 10))  # type: ignore[arg-type]

    hooks.on("event-x", first)
    hooks.on("event-x", second)
    hooks.emit("event-x", n=1)

    assert received == [("event-x", 1), ("event-x", 11)]


def test_hooks_emit_without_registered_observers_is_a_noop():
    hooks = Hooks()
    hooks.emit("event-x", n=1)  # point never subscribed
    hooks.emit("never-registered")  # no observers at all: must not raise
