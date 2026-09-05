"""Behavior tests for the worksheet review loop (review_loop.loop).

No DB, no network: a scripted model returns one ``(content, tool_calls)`` pair
per turn and stub tools echo/deny/fail on demand. The contract under test is
worksheet mode: the model resolves candidate rows through ``update_review_item``
(confirmed with a finding, or dismissed with a reason), and the run ends when
every candidate is resolved. A turn with no tool calls while rows remain is an
incomplete finish, never a report.
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
from code_review_ai.review_loop.schemas import (
    ReviewItem,
    ReviewItemUpdate,
    UPDATE_REVIEW_TOOL,
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


def _echo(text: str) -> str:
    return f"echo:{text}"


def _boom(text: str) -> str:
    raise RuntimeError(f"boom: {text}")


def _call(name: str, args: dict | None = None, ident: str | None = None) -> dict:
    return {"name": name, "args": args or {}, "id": ident or f"{name}-call"}


def _update_call(qname: str, **resolution) -> dict:
    payload = {"qname": qname, **resolution}
    return _call(UPDATE_REVIEW_TOOL, payload, ident=f"update-{qname}")


def _make_tools():
    """Three action tools plus the schema-only worksheet updater."""
    def _handled(*_args, **_kwargs) -> str:
        raise AssertionError("update_review_item is applied by the loop, never run")

    return [
        ToolSpec(name="echo", description="echo text back", args_schema=EchoArgs,
                 run=_echo),
        ToolSpec(name="impact", description="stub impact", args_schema=ImpactArgs,
                 run=lambda symbols: json.dumps({"found": symbols}, ensure_ascii=False)),
        ToolSpec(name="boom", description="stub that fails", args_schema=BoomArgs,
                 run=_boom),
        ToolSpec(name=UPDATE_REVIEW_TOOL, description="resolve one candidate row",
                 args_schema=ReviewItemUpdate, run=_handled),
    ]


def _candidates(*qnames: str) -> list[ReviewItem]:
    return [ReviewItem(qname=qname) for qname in qnames]


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


def _run(model, candidates, **kwargs):
    return run_loop(model, _make_tools(), candidates=candidates,
                    initial_messages=[], **kwargs)


def _tool_contents(model: FakeModel) -> list[str]:
    """Every ToolMessage content the model has been shown, in order."""
    return [str(message.content)
            for batch in model.invoked
            for message in batch if isinstance(message, ToolMessage)]


def _confirm_update(qname: str) -> dict:
    return _update_call(qname, state="confirmed",
                        finding={"file": f"{qname}.py", "line": 1,
                                 "title": "bug", "description": "broken"})


def _dismiss_update(qname: str) -> dict:
    return _update_call(qname, state="dismissed", reason="not a real issue")


# ---------------------------------------------------------------------------
# worksheet resolution
# ---------------------------------------------------------------------------

def test_all_candidates_resolved_completes_with_confirmed_findings():
    model = FakeModel([("", [_confirm_update("app::run"),
                             _dismiss_update("app::helper")]),
                       ("review done.", [])])

    result = _run(model, _candidates("app::run", "app::helper"))

    assert result.review_complete is True
    assert result.failure_reason is None
    assert result.items["app::run"].state == "confirmed"
    assert result.items["app::helper"].state == "dismissed"
    assert [finding.title for finding in result.findings] == ["bug"]
    assert result.items["app::run"].finding is not None
    assert result.items["app::helper"].reason == "not a real issue"


def test_dismissed_only_run_has_no_findings():
    model = FakeModel([("", [_dismiss_update("app::run")]),
                       ("clean.", [])])

    result = _run(model, _candidates("app::run"))

    assert result.review_complete is True
    assert result.findings == []
    assert result.items["app::run"].state == "dismissed"


def test_model_stops_before_all_resolved_is_incomplete_not_a_report():
    model = FakeModel([("no issues to flag.", [])])

    result = _run(model, _candidates("app::run"))

    assert result.review_complete is False
    assert result.failure_reason is None
    assert result.items["app::run"].state == "candidate"
    assert result.findings == []


def test_stop_between_updates_leaves_remaining_rows_candidate():
    model = FakeModel([("", [_confirm_update("app::a")]),
                       ("", [_dismiss_update("app::b")]),
                       ("nothing else.", [])])

    result = _run(model, _candidates("app::a", "app::b", "app::c"))

    assert result.review_complete is False
    assert result.items["app::a"].state == "confirmed"
    assert result.items["app::b"].state == "dismissed"
    assert result.items["app::c"].state == "candidate"
    assert len(result.findings) == 1


def test_invalid_update_payload_is_rejected_and_worksheet_untouched():
    # turn 1: confirmed without a finding -> rejected, row untouched
    model = FakeModel([("", [_update_call("app::run", state="confirmed")]),
                       ("", [_dismiss_update("app::run"),
                             _confirm_update("app::helper")])])

    result = _run(model, _candidates("app::run", "app::helper"))

    assert result.review_complete is True
    assert result.items["app::run"].state == "dismissed"
    assert result.tool_request_count == 3
    statuses = [record["status"] for record in result.tool_trace]
    assert statuses == ["error", "success", "success"]
    assert any("invalid update_review_item payload" in content
               for content in _tool_contents(model))


def test_update_of_unknown_qname_is_rejected():
    model = FakeModel([("", [_confirm_update("no::such::symbol")]),
                       ("", [_dismiss_update("app::run")]),
                       ("done.", [])])

    result = _run(model, _candidates("app::run"))

    assert result.review_complete is True
    assert result.items["app::run"].state == "dismissed"
    assert [record["status"] for record in result.tool_trace] == ["error", "success"]
    assert any("is not an active candidate" in content
               for content in _tool_contents(model))


def test_resolving_last_candidate_in_an_action_turn_completes_the_run():
    model = FakeModel([("", [_confirm_update("app::run"),
                             _call("impact", {"symbols": ["app::run"]}, "impact-1")]),
                       ("done.", [])])

    result = _run(model, _candidates("app::run"))

    assert result.review_complete is True
    assert result.tool_calls == [UPDATE_REVIEW_TOOL, "impact"]
    assert result.tool_call_count == 2


# ---------------------------------------------------------------------------
# tool execution mechanics (kept from the natural-stop loop)
# ---------------------------------------------------------------------------

def test_bound_schemas_are_schema_only_dicts():
    model = FakeModel([("done.", [])])
    _run(model, _candidates("app::run"))

    names = {schema["name"] for schema in model.bound_schemas}
    assert names == {"echo", "impact", "boom", UPDATE_REVIEW_TOOL}
    assert all(isinstance(schema["input_schema"], dict) for schema in model.bound_schemas)


def test_invalid_args_are_an_error_not_an_execution():
    model = FakeModel([("", [_call("impact", {"symbols": "not-a-list"}, "impact-1")]),
                       ("done.", [])])

    result = _run(model, _candidates("app::run"))

    assert result.tool_request_count == 1
    assert [record["status"] for record in result.tool_trace] == ["error"]
    assert any("do not match the allowed schema" in content
               for content in _tool_contents(model))


def test_runtime_tool_failure_is_answered_and_the_loop_continues():
    model = FakeModel([("", [_call("boom", {"text": "kaboom"}, "boom-1")]),
                       ("done.", [])])

    result = _run(model, _candidates("app::run"))

    assert result.items["app::run"].state == "candidate"
    assert [record["status"] for record in result.tool_trace] == ["error"]
    assert any("boom: kaboom" in content for content in _tool_contents(model))


def test_several_tools_in_one_turn_all_run_in_order():
    model = FakeModel([("", [_call("impact", {"symbols": ["x"]}, "impact-1"),
                             _call("echo", {"text": "hi"}, "echo-1")]),
                       ("done.", [])])

    result = _run(model, _candidates("app::run"))

    assert result.tool_calls == ["impact", "echo"]
    assert result.tool_call_count == 2
    assert [record["status"] for record in result.tool_trace] == ["success", "success"]


def test_unknown_tool_is_rejected_and_the_loop_continues():
    model = FakeModel([("", [_call("no_such_tool", ident="ghost-1")]),
                       ("done.", [])])

    result = _run(model, _candidates("app::run"))

    assert result.tool_request_count == 1
    assert [record["status"] for record in result.tool_trace] == ["error"]
    assert any("unknown tool" in content for content in _tool_contents(model))


def test_provider_failure_keeps_the_partial_audit_trail():
    # one row is resolved before the provider call that should resolve the rest
    model = FakeModel([("", [_confirm_update("app::run")]),
                       RuntimeError("connection reset")])

    result = _run(model, _candidates("app::run", "app::helper"))

    assert result.review_complete is False
    assert result.failure_reason == "provider call failed: connection reset"
    assert result.items["app::run"].state == "confirmed"
    assert result.items["app::helper"].state == "candidate"
    assert len(result.tool_trace) == 1
    assert result.tool_trace[0]["status"] == "success"
    assert result.tool_call_count == 1


def test_turn_cap_stops_a_looping_model():
    model = FakeModel([("", [_call("echo", {"text": "a"}, "a-1")]),
                       ("", [_call("echo", {"text": "b"}, "b-1")]),
                       ("", [_call("echo", {"text": "c"}, "c-1")]),
                       ("", [_call("echo", {"text": "d"}, "d-1")])])

    result = run_loop(model, _make_tools(), candidates=_candidates("app::run"),
                      initial_messages=[], max_turns=3)

    assert result.review_complete is False
    assert result.failure_reason == "agent kept requesting tools for 3 turns"
    assert result.tool_call_count == 3
    assert result.tool_request_count == 3


# ---------------------------------------------------------------------------
# hooks
# ---------------------------------------------------------------------------

def test_happy_path_emits_the_observer_event_sequence():
    # turn 1 reads with a real tool; turn 2 resolves the only candidate and ends.
    # the updater is applied by the loop, so resolving never fires pre/post_tool.
    model = FakeModel([("", [_call("impact", {"symbols": ["x"]}, "impact-1")]),
                       ("", [_confirm_update("app::run")])])
    hooks = Hooks()
    seen: list[str] = []
    run_context: dict = {}

    for point in (POINT_MODEL_REQUEST_STARTED, POINT_MODEL_RESPONSE_RECEIVED,
                  POINT_PRE_TOOL, POINT_POST_TOOL, POINT_RUN_FINISHED):
        hooks.on(point, lambda event, context, point=point: seen.append(point))
    hooks.on(POINT_RUN_FINISHED, lambda _event, context: run_context.update(context))

    result = _run(model, _candidates("app::run"), hooks=hooks)

    assert result.review_complete is True
    assert seen == ["model_request_started", "model_response_received",
                    "pre_tool", "post_tool",
                    "model_request_started", "model_response_received",
                    "run_finished"]
    assert run_context["finding_count"] == 1
    assert run_context["failure_reason"] is None


def test_schema_rejected_call_never_fires_pre_or_post_tool():
    model = FakeModel([("", [_call("impact", {"symbols": "not-a-list"}, "impact-1")]),
                       ("done.", [])])
    hooks = Hooks()
    tool_events: list[str] = []
    for point in (POINT_PRE_TOOL, POINT_POST_TOOL):
        hooks.on(point, lambda event, _context, point=point: tool_events.append(point))

    result = _run(model, _candidates("app::run"), hooks=hooks)

    assert tool_events == []  # the call was rejected before it could run


def test_pre_and_post_tool_fire_around_a_real_tool_run():
    model = FakeModel([("", [_call("impact", {"symbols": ["x"]}, "impact-1")]),
                       ("done.", [])])
    hooks = Hooks()
    tool_events: list[tuple[str, str]] = []
    hooks.on(POINT_PRE_TOOL, lambda _e, ctx: tool_events.append(("pre", ctx["name"])))
    hooks.on(POINT_POST_TOOL, lambda _e, ctx: tool_events.append(("post", ctx["name"])))

    _run(model, _candidates("app::run"), hooks=hooks)

    assert tool_events == [("pre", "impact"), ("post", "impact")]


def test_assistant_turn_precedes_tool_replies_in_history():
    # resolving one row keeps the run going, so a second model turn happens
    model = FakeModel([("", [_confirm_update("app::run")]),
                       ("", [_dismiss_update("app::helper")])])

    _run(model, _candidates("app::run", "app::helper"))

    second_turn = model.invoked[1]
    types = [message.type for message in second_turn]
    # the assistant message carrying tool_calls must precede the tool reply,
    # or the provider rejects the 'tool' message as dangling.
    assert types.index("ai") < types.index("tool")


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
