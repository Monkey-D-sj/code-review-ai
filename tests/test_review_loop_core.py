"""Behavior tests for the review loop (review_loop.loop).

No DB, no network: a scripted model returns one ``(content, tool_calls)`` pair
per turn and stub tools echo/deny/fail on demand. The contract under test:
the model researches with the tools and ends by calling ``finish_review`` with
its findings; a turn with no tool calls before that is a failure, never a
silent success.
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
from code_review_ai.review_loop.loop import TRACE_RESPONSE_EXCERPT_CHARS
from code_review_ai.review_loop.schemas import (
    FINISH_REVIEW_TOOL,
    ReviewSubmission,
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




class FakeModel:
    """bind_tools-shaped fake: records what it binds, replays a script of turns.

    Each schedule entry is a ``(content, tool_calls)`` pair, optionally a
    ``(content, tool_calls, usage_metadata)`` triple or a
    ``(content, tool_calls, usage_metadata, reasoning_content)`` 4-tuple, or an
    ``Exception`` to simulate a provider failure.
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
        usage = None
        reasoning = None
        if len(step) == 4:  # (content, tool_calls, usage_metadata, reasoning)
            content, calls, usage, reasoning = step
        elif len(step) == 3:  # (content, tool_calls, usage_metadata)
            content, calls, usage = step
        else:
            content, calls = step
        # AIMessage's pydantic model requires all three usage keys to be ints,
        # so construct with a placeholder then overwrite: lets a test feed a
        # provider-shaped partial/odd usage_metadata the constructor rejects.
        message = AIMessage(content=content, tool_calls=calls,
                            usage_metadata={"input_tokens": 0, "output_tokens": 0,
                                            "total_tokens": 0})
        message.usage_metadata = usage
        if reasoning is not None:
            message.additional_kwargs["reasoning_content"] = reasoning
        return message


def _tools():
    """Three action tools plus the finish_review submitter."""
    return [
        ToolSpec(name="echo", description="echo text back", args_schema=EchoArgs,
                 run=_echo),
        ToolSpec(name="impact", description="stub impact", args_schema=ImpactArgs,
                 run=lambda symbols: json.dumps({"found": symbols}, ensure_ascii=False)),
        ToolSpec(name="boom", description="stub that fails", args_schema=BoomArgs,
                 run=_boom),
        ToolSpec(name=FINISH_REVIEW_TOOL, description="submit findings",
                 args_schema=ReviewSubmission, run=lambda **_kw: "unused"),
    ]


def _run(model, **kwargs):
    return run_loop(model, _tools(), initial_messages=[], **kwargs)


def _tool_contents(model: FakeModel) -> list[str]:
    """Every ToolMessage content the model has been shown, in order."""
    return [str(message.content)
            for batch in model.invoked
            for message in batch if isinstance(message, ToolMessage)]







def test_tool_less_assistant_turns_are_not_appended_to_history():
    # an empty assistant turn carries no state, and (DeepSeek) serializers have
    # hallucinated tool_calls onto such turns -> provider 400. It must not be
    # sent back. An empty turn before finish_review is a failure, so the run
    # stops there and the second turn's call never executes.
    model = FakeModel([("no issues to flag.", []),
                       ("", [_call("impact", {"symbols": ["app::run"]}, "impact-app::run")])])

    result = _run(model)

    assert result.review_complete is False
    assert result.failure_reason == "agent stopped without submitting finish_review"
    assert len(model.invoked) == 1  # the empty turn ended the run
    assert all(message.type != "ai" for message in model.invoked[0])
def test_empty_turn_failure_records_the_turn_text_and_reasoning():
    # the debugging record must keep what the model actually wrote on the empty
    # turn (text + reasoning), even though that turn never re-enters the history
    # (it carries no state and DeepSeek hallucinates tool_calls onto them). This
    # is the payload a post-mortem reads on the empty-turn failure -- so a blank
    # here means the transcript was lost before analysis.
    model = FakeModel([("no issues to flag.", [], None, "scanning callers...")])

    result = _run(model)

    assert result.review_complete is False
    assert result.failure_reason == "agent stopped without submitting finish_review"
    recorded = result.assistant_turns
    assert [turn.turn for turn in recorded] == [1]
    assert [turn.content for turn in recorded] == ["no issues to flag."]
    assert [turn.reasoning for turn in recorded] == ["scanning callers..."]
    assert all(turn.tool_calls == [] for turn in recorded)
def test_hidden_tool_calls_are_promoted_and_executed():
    from code_review_ai.review_loop.loop import _promote_hidden_tool_calls

    assistant = AIMessage(content="", tool_calls=[])
    assistant.additional_kwargs["tool_calls"] = [{
        "id": "call_hidden", "type": "function",
        "function": {"name": "echo", "arguments": '{"text": "hi"}'}}]

    _promote_hidden_tool_calls(assistant)

    assert assistant.tool_calls == [{"id": "call_hidden", "name": "echo",
                                     "args": {"text": "hi"}}]
    assert "tool_calls" not in assistant.additional_kwargs





# ---------------------------------------------------------------------------
# usage (model-side token accounting)
# ---------------------------------------------------------------------------

def test_usage_aggregates_model_reported_tokens_across_turns():
    model = FakeModel([("", [_call("echo", {"text": "app::run"}, "echo-app::run")],
                        {"input_tokens": 10, "output_tokens": 5, "total_tokens": 15}),
                       ("", [_call("impact", {"symbols": ["app::helper"]}, "impact-app::helper")],
                        {"input_tokens": 12, "output_tokens": 3, "total_tokens": 15}),
                       ("", [_finish_call([])])])

    result = _run(model)

    assert result.review_complete is True
    assert result.usage == {"input_tokens": 22, "output_tokens": 8, "total_tokens": 30}
def test_usage_skips_absent_or_non_integral_provider_fields():
    # the provider reports only output_tokens on turn 1 and a non-int on turn 2
    model = FakeModel([("", [_call("echo", {"text": "app::run"}, "echo-app::run")],
                        {"output_tokens": 7, "total_tokens": None}),
                       ("", [_call("impact", {"symbols": ["app::helper"]}, "impact-app::helper")],
                        {"input_tokens": 3, "output_tokens": "five"})])

    result = _run(model)

    assert result.usage == {"output_tokens": 7, "input_tokens": 3}


def test_usage_stays_empty_when_provider_reports_none():
    model = FakeModel([("", [_call("echo", {"text": "app::run"}, "echo-app::run")]),
                       ("", [_call("impact", {"symbols": ["app::helper"]}, "impact-app::helper")])])

    result = _run(model)

    assert result.usage == {}


def test_provider_failure_keeps_usage_already_spent():
    # turn 1 reports usage before the call that should resolve the rest fails
    model = FakeModel([("", [_call("echo", {"text": "app::run"}, "echo-app::run")],
                        {"input_tokens": 20, "output_tokens": 4, "total_tokens": 24}),
                       RuntimeError("connection reset")])

    result = _run(model)

    assert result.review_complete is False
    assert result.failure_reason == "provider call failed: connection reset"
    assert result.usage == {"input_tokens": 20, "output_tokens": 4, "total_tokens": 24}


def test_cache_read_accumulates_from_input_token_details():
    # input_tokens is the grand total (cache hits included); cache_read is the
    # cheaper slice, reported under input_token_details.
    model = FakeModel([("", [_call("echo", {"text": "app::run"}, "echo-app::run")],
                        {"input_tokens": 200, "output_tokens": 10, "total_tokens": 210,
                         "input_token_details": {"cache_read": 150}}),
                       ("", [_call("impact", {"symbols": ["app::helper"]}, "impact-app::helper")],
                        {"input_tokens": 400, "output_tokens": 20, "total_tokens": 420,
                         "input_token_details": {"cache_read": 350}}),
                       ("", [_finish_call([])])])

    result = _run(model)

    assert result.review_complete is True
    assert result.usage == {"input_tokens": 600, "output_tokens": 30,
                            "total_tokens": 630, "cache_read": 500}
def test_cache_read_skipped_when_details_absent_or_non_int():
    # turn 1 has no input_token_details; turn 2's cache_read is not an int
    model = FakeModel([("", [_call("echo", {"text": "app::run"}, "echo-app::run")],
                        {"input_tokens": 50, "output_tokens": 5, "total_tokens": 55}),
                       ("", [_call("impact", {"symbols": ["app::helper"]}, "impact-app::helper")],
                        {"input_tokens": 60, "output_tokens": 6, "total_tokens": 66,
                         "input_token_details": {"cache_read": "lots"}})])

    result = _run(model)

    assert result.usage == {"input_tokens": 110, "output_tokens": 11, "total_tokens": 121}
    assert "cache_read" not in result.usage


# ---------------------------------------------------------------------------
# token budget gate
# ---------------------------------------------------------------------------

def test_token_budget_stops_an_overspending_model():
    # turn 1 stays under the cap; turn 2 crosses it, so its calls never run
    model = FakeModel([("", [_call("echo", {"text": "app::run"}, "echo-app::run")],
                        {"input_tokens": 1000, "output_tokens": 500, "total_tokens": 1500}),
                       ("", [_call("impact", {"symbols": ["app::helper"]}, "impact-app::helper")],
                        {"input_tokens": 1000, "output_tokens": 500, "total_tokens": 1500})])

    result = _run(model, max_total_tokens=2000)

    assert result.review_complete is False
    assert result.failure_reason == ("token budget exceeded: spent 3000 total_tokens, "
                                     "limit 2000")
    assert result.usage["total_tokens"] == 3000


def test_token_budget_allows_spending_up_to_the_cap():
    # exactly at the cap is allowed; the model then submits and the run completes
    model = FakeModel([("", [_call("echo", {"text": "app::run"}, "echo-app::run")],
                        {"input_tokens": 1000, "output_tokens": 500, "total_tokens": 1500}),
                       ("", [_call("impact", {"symbols": ["app::helper"]}, "impact-app::helper")],
                        {"input_tokens": 1000, "output_tokens": 500, "total_tokens": 1500}),
                       ("", [_finish_call([])])])

    result = _run(model, max_total_tokens=3000)

    assert result.review_complete is True
    assert result.failure_reason is None
def test_token_budget_ignores_non_reporting_turns():
    # turn 1 sits exactly at the cap; turn 2 reports no usage, so the cap never
    # sees further spend (a documented blind spot: the gate reads reported totals)
    model = FakeModel([("", [_call("echo", {"text": "app::run"}, "echo-app::run")],
                        {"input_tokens": 1, "output_tokens": 0, "total_tokens": 1}),
                       ("", [_call("impact", {"symbols": ["app::helper"]}, "impact-app::helper")]),
                       ("", [_finish_call([])])])

    result = _run(model, max_total_tokens=1)

    assert result.review_complete is True
    assert result.failure_reason is None
    assert result.usage["total_tokens"] == 1
# ---------------------------------------------------------------------------
# tool execution mechanics (kept from the natural-stop loop)
# ---------------------------------------------------------------------------

def test_bound_schemas_are_schema_only_dicts():
    model = FakeModel([("done.", [])])
    _run(model)

    names = {schema["name"] for schema in model.bound_schemas}
    assert names == {"echo", "impact", "boom", FINISH_REVIEW_TOOL}
    assert all(isinstance(schema["input_schema"], dict) for schema in model.bound_schemas)


def test_invalid_args_are_an_error_not_an_execution():
    model = FakeModel([("", [_call("impact", {"symbols": "not-a-list"}, "impact-1")]),
                       ("done.", [])])

    result = _run(model)

    assert result.tool_request_count == 1
    assert [record["status"] for record in result.tool_trace] == ["error"]
    assert any("do not match the allowed schema" in content
               for content in _tool_contents(model))


def test_runtime_tool_failure_is_answered_and_the_loop_continues():
    model = FakeModel([("", [_call("boom", {"text": "kaboom"}, "boom-1")]),
                       ("done.", [])])

    result = _run(model)

    assert [record["status"] for record in result.tool_trace] == ["error"]
    assert any("boom: kaboom" in content for content in _tool_contents(model))


def test_several_tools_in_one_turn_all_run_in_order():
    model = FakeModel([("", [_call("impact", {"symbols": ["x"]}, "impact-1"),
                             _call("echo", {"text": "hi"}, "echo-1")]),
                       ("done.", [])])

    result = _run(model)

    assert result.tool_calls == ["impact", "echo"]
    assert result.tool_call_count == 2
    assert [record["status"] for record in result.tool_trace] == ["success", "success"]


def test_unknown_tool_is_rejected_and_the_loop_continues():
    model = FakeModel([("", [_call("no_such_tool", ident="ghost-1")]),
                       ("done.", [])])

    result = _run(model)

    assert result.tool_request_count == 1
    assert [record["status"] for record in result.tool_trace] == ["error"]
    assert any("unknown tool" in content for content in _tool_contents(model))


def test_provider_failure_keeps_the_partial_audit_trail():
    # one row is resolved before the provider call that should resolve the rest
    model = FakeModel([("", [_call("echo", {"text": "app::run"}, "echo-app::run")]),
                       RuntimeError("connection reset")])

    result = _run(model)

    assert result.review_complete is False
    assert result.failure_reason == "provider call failed: connection reset"
    assert len(result.tool_trace) == 1
    assert result.tool_trace[0]["status"] == "success"
    assert result.tool_call_count == 1


def test_turn_cap_stops_a_looping_model():
    model = FakeModel([("", [_call("echo", {"text": "a"}, "a-1")]),
                       ("", [_call("echo", {"text": "b"}, "b-1")]),
                       ("", [_call("echo", {"text": "c"}, "c-1")]),
                       ("", [_call("echo", {"text": "d"}, "d-1")])])

    result = _run(model, max_turns=3)

    assert result.review_complete is False
    assert result.failure_reason == "agent kept requesting tools for 3 turns"
    assert result.tool_call_count == 3
    assert result.tool_request_count == 3


# ---------------------------------------------------------------------------
# hooks
# ---------------------------------------------------------------------------

def test_happy_path_emits_the_observer_event_sequence():
    # turn 1 reads with a real tool; turn 2 submits findings and ends the run.
    # finish_review is the loop's own control tool, so it never fires pre/post_tool
    # -- only the real tool call in turn 1 does.
    finding = {"file": "app.py", "line": 3, "title": "bug", "description": "why"}
    model = FakeModel([("", [_call("impact", {"symbols": ["x"]}, "impact-1")]),
                       ("", [_finish_call([finding])])])
    hooks = Hooks()
    seen: list[str] = []
    run_context: dict = {}

    for point in (POINT_MODEL_REQUEST_STARTED, POINT_MODEL_RESPONSE_RECEIVED,
                  POINT_PRE_TOOL, POINT_POST_TOOL, POINT_RUN_FINISHED):
        hooks.on(point, lambda event, context, point=point: seen.append(point))
    hooks.on(POINT_RUN_FINISHED, lambda _event, context: run_context.update(context))

    result = _run(model, hooks=hooks)

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

    result = _run(model, hooks=hooks)

    assert tool_events == []  # the call was rejected before it could run


def test_pre_and_post_tool_fire_around_a_real_tool_run():
    model = FakeModel([("", [_call("impact", {"symbols": ["x"]}, "impact-1")]),
                       ("done.", [])])
    hooks = Hooks()
    tool_events: list[tuple[str, str]] = []
    hooks.on(POINT_PRE_TOOL, lambda _e, ctx: tool_events.append(("pre", ctx["name"])))
    hooks.on(POINT_POST_TOOL, lambda _e, ctx: tool_events.append(("post", ctx["name"])))

    _run(model, hooks=hooks)

    assert tool_events == [("pre", "impact"), ("post", "impact")]


def test_assistant_turn_precedes_tool_replies_in_history():
    # resolving one row keeps the run going, so a second model turn happens
    model = FakeModel([("", [_call("echo", {"text": "app::run"}, "echo-app::run")]),
                       ("", [_call("impact", {"symbols": ["app::helper"]}, "impact-app::helper")])])

    _run(model)

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


# ---------------------------------------------------------------------------
# the submission (finish_review ends the run)
# ---------------------------------------------------------------------------


def _finish_call(findings, ident="finish-1") -> dict:
    return _call(FINISH_REVIEW_TOOL, {"findings": findings}, ident)



def test_free_loop_researches_then_submits_findings():
    finding = {"file": "app.py", "line": 7, "title": "bug", "description": "why"}
    model = FakeModel([("", [_call("impact", {"symbols": ["x"]}, "impact-1")]),
                       ("", [_finish_call([finding])])])

    result = _run(model)

    assert result.review_complete is True
    assert result.failure_reason is None
    assert [f.title for f in result.findings] == ["bug"]
    assert result.tool_calls == ["impact", FINISH_REVIEW_TOOL]
    assert result.tool_call_count == 2
    assert [record["status"] for record in result.tool_trace] == ["success", "success"]


def test_free_loop_empty_submission_is_a_valid_no_regression_verdict():
    model = FakeModel([("", [_finish_call([])])])

    result = _run(model)

    assert result.review_complete is True
    assert result.failure_reason is None
    assert result.findings == []


def test_free_loop_invalid_submission_is_answered_then_retried():
    # line 0 fails the Finding schema (ge=1); the model retries with a valid one
    bad = {"file": "app.py", "line": 0, "title": "t", "description": "d"}
    good = {"file": "app.py", "line": 3, "title": "t", "description": "d"}
    model = FakeModel([("", [_finish_call([bad])]),
                       ("", [_finish_call([good])])])

    result = _run(model)

    assert result.review_complete is True
    assert result.failure_reason is None
    assert [f.line for f in result.findings] == [3]
    assert [record["status"] for record in result.tool_trace] == ["error", "success"]
    assert any("invalid finish_review payload" in content
               for content in _tool_contents(model))


def test_free_loop_empty_turn_before_submit_is_a_failure():
    model = FakeModel([("clean code, no regressions.", [])])

    result = _run(model)

    assert result.review_complete is False
    assert "finish_review" in result.failure_reason
    assert result.findings == []


def test_free_loop_provider_failure_preserves_partial_research_trace():
    model = FakeModel([("", [_call("impact", {"symbols": ["x"]}, "impact-1")]),
                       RuntimeError("connection reset")])

    result = _run(model)

    assert result.review_complete is False
    assert result.failure_reason == "provider call failed: connection reset"
    assert result.tool_call_count == 1
    assert len(result.tool_trace) == 1


def test_assistant_turns_record_text_and_reasoning_per_turn():
    class ReasoningTextModel:
        """Returns one tool-less reply carrying text + reasoning."""

        def bind_tools(self, schemas):
            return self

        def invoke(self, messages):
            reply = AIMessage(content="clean code, no regression")
            reply.additional_kwargs["reasoning_content"] = "thinking through callers"
            return reply

    result = _run(ReasoningTextModel())

    # the empty turn fails the run, but its text is kept for debugging
    assert result.review_complete is False
    assert result.assistant_turns[0].content == "clean code, no regression"
    assert result.assistant_turns[0].reasoning == "thinking through callers"
    assert result.assistant_turns[0].tool_calls == []


# ---------------------------------------------------------------------------
# tool trace body retention (response_excerpt)
# ---------------------------------------------------------------------------

def test_tool_trace_excerpt_covers_a_short_tool_body_entirely():
    model = FakeModel([("", [_call("echo", {"text": "hi"}, "e-1")]),
                       ("", [_call("echo", {"text": "app::run"}, "echo-app::run")])])

    result = _run(model)

    record = result.tool_trace[0]
    assert len(record["response_excerpt"]) == record["response_chars"]


def test_tool_trace_excerpt_is_capped_and_chars_stays_the_full_length():
    """The optimizer needs the body, but a 50-turn run over whole source files
    would balloon the payload unbounded."""
    model = FakeModel([("", [_call("echo", {"text": "x" * 5000}, "e-1")]),
                       ("", [_call("echo", {"text": "app::run"}, "echo-app::run")])])

    result = _run(model)

    record = result.tool_trace[0]
    assert len(record["response_excerpt"]) == TRACE_RESPONSE_EXCERPT_CHARS
    # The exact length, not just "more than the cap": the loose form would admit
    # a silently-truncated response_chars (2001) as readily as the real 5005.
    assert record["response_chars"] == len(_tool_contents(model)[0])


def test_tool_trace_excerpt_honors_a_custom_cap():
    """The cap is a parameter, not a constant: a caller may want a budget other
    than the default, and nothing else exercises that."""
    model = FakeModel([("", [_call("echo", {"text": "x" * 5000}, "e-1")]),
                       ("", [_call("echo", {"text": "app::run"}, "echo-app::run")])])

    result = _run(model, trace_response_excerpt_chars=50)

    record = result.tool_trace[0]
    assert len(record["response_excerpt"]) == 50
    assert record["response_chars"] == len(_tool_contents(model)[0])
