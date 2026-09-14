"""End-to-end driver test: prompt + diff + tools -> findings out.

The driver's one job is to assemble the request (policy, prompt, diff), offer
the caller's tool set, and price the run. Which tools exist is the only thing
that distinguishes the two arms, so the arms are tested as two tool lists
through the same entry point.
"""

from __future__ import annotations

import sqlite3

import pytest
from langchain_core.messages import AIMessage, SystemMessage, ToolMessage

from code_review_ai.config import load_config
from code_review_ai.db import init_schema
from code_review_ai.review_loop import Hooks
from code_review_ai.review_loop.hooks import POINT_RUN_FINISHED
from code_review_ai.review_loop.runner import (
    NOINDEX_TOOLS,
    _POLICY,
    build_initial_messages,
    run_review,
)
from code_review_ai.review_loop.schemas import FINISH_REVIEW_TOOL

FINDING = {"file": "app.py", "line": 2, "title": "leaks None on empty user",
           "description": "login returns None for an empty user."}


class ScriptedReviewModel:
    """Turn 1 reads app.py; turn 2 submits one finding over finish_review."""

    def __init__(self):
        self.saw_system = False
        self.system_content = ""
        self.saw_tool_reply = False
        self.schemas: list[str] = []

    def bind_tools(self, schemas):
        self.schemas = [schema["name"] for schema in schemas]
        return self

    def invoke(self, messages):
        for message in messages:
            if isinstance(message, SystemMessage):
                self.saw_system = True
                self.system_content = message.content
            if isinstance(message, ToolMessage):
                self.saw_tool_reply = True
        turn = len([m for m in messages if m.type == "ai"])
        if turn == 0:
            return AIMessage(content="", tool_calls=[
                {"name": "read_file", "args": {"path": "app.py",
                                               "start_line": 1, "end_line": 3},
                 "id": "read-1"}])
        return AIMessage(content="", tool_calls=[
            {"name": FINISH_REVIEW_TOOL, "args": {"findings": [FINDING]},
             "id": "submit-1"}])


@pytest.fixture()
def env(tmp_path):
    (tmp_path / "app.py").write_text("def login(user):\n"
                                     "    return user or None\n", encoding="utf-8")
    config = load_config(repo_path=str(tmp_path))
    config.repo_path = str(tmp_path)
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    init_schema(conn)
    return config, conn


class CostReportingModel(ScriptedReviewModel):
    """Like ScriptedReviewModel, but the submit turn reports provider usage."""

    def invoke(self, messages):
        response = super().invoke(messages)
        if any(call["name"] == FINISH_REVIEW_TOOL
               for call in getattr(response, "tool_calls", [])):
            response.usage_metadata = {
                "input_tokens": 1_000_000, "output_tokens": 1_000_000,
                "total_tokens": 2_000_000}
        return response


def test_run_review_reports_yuan_cost_from_usage(env):
    config, conn = env

    result = run_review(config, conn, prompt="check auth for regressions",
                        diff="DIFF-BODY", model=CostReportingModel(), max_turns=5)

    assert result.review_complete is True
    # the submit turn priced: 1M miss input (1.5) + 1M output (4.5) = 6.0 元
    assert result.usage["total_tokens"] == 2_000_000
    assert result.cost == pytest.approx(6.0)


def test_run_review_offers_the_graph_tools_and_reports_findings(env):
    config, conn = env
    model = ScriptedReviewModel()
    hooks = Hooks()
    finished: dict = {}

    hooks.on(POINT_RUN_FINISHED, lambda _event, context: finished.update(context))
    result = run_review(config, conn, prompt="check auth for regressions",
                        diff="DIFF-BODY", model=model, hooks=hooks, max_turns=5)

    assert result.failure_reason is None
    assert result.review_complete is True
    assert [finding.title for finding in result.findings] == ["leaks None on empty user"]
    assert [finding.file for finding in result.findings] == ["app.py"]
    assert result.tool_calls == ["read_file", FINISH_REVIEW_TOOL]
    assert result.tool_call_count == 2
    assert result.tool_request_count == 2
    assert [record["status"] for record in result.tool_trace] == ["success", "success"]
    assert model.saw_system and model.saw_tool_reply
    assert {"read_file", "get_impact", FINISH_REVIEW_TOOL} <= set(model.schemas)
    assert finished["failure_reason"] is None
    assert finished["finding_count"] == 1


def test_run_review_needs_no_index_for_the_narrowed_tool_set(env):
    """The no-index arm: the same diff, read/search only, no graph and no conn."""
    config, _conn = env
    model = ScriptedReviewModel()

    result = run_review(config, prompt="review this diff", diff="DIFF-BODY",
                        tool_names=list(NOINDEX_TOOLS), model=model, max_turns=5)

    assert result.failure_reason is None
    assert result.review_complete is True
    assert [finding.file for finding in result.findings] == ["app.py"]
    assert set(model.schemas) == {"read_file", "search_code", FINISH_REVIEW_TOOL}


def test_run_review_carries_the_diff_into_the_request(env):
    config, _conn = env
    seen: dict = {}

    class CapturingModel(ScriptedReviewModel):
        def invoke(self, messages):
            seen.setdefault("request", messages[1].content)
            return super().invoke(messages)

    run_review(config, prompt="review this diff", diff="+ leaked = True",
               model=CapturingModel(), max_turns=5)

    assert "review this diff" in seen["request"]
    assert "+ leaked = True" in seen["request"]


def test_build_initial_messages_carries_the_prompt_and_the_diff():
    messages = build_initial_messages("review auth", "+ x = None")

    assert messages[0].type == "system"
    assert messages[1].type == "human"
    assert "review auth" in messages[1].content
    assert "+ x = None" in messages[1].content


def test_build_initial_messages_uses_the_injected_policy():
    messages = build_initial_messages("review auth", "d", policy="CUSTOM POLICY TEXT")

    assert messages[0].content == "CUSTOM POLICY TEXT"


def test_build_initial_messages_defaults_to_the_builtin_policy():
    messages = build_initial_messages("review auth", "d")

    assert messages[0].content == _POLICY


def test_build_initial_messages_omits_the_change_summary_by_default():
    """The baseline stays the baseline: with no summary the model gets the
    diff and nothing else. Every existing run must be byte-identical."""
    messages = build_initial_messages("review auth", "+ x = None")

    assert "CHANGE SUMMARY" not in messages[1].content


def test_build_initial_messages_carries_the_summary_ahead_of_the_diff():
    """When injected the summary is its own labelled block, not folded into
    the diff -- and the diff stays last, so it is what the model reads before
    it starts reasoning."""
    messages = build_initial_messages("review auth", "+ x = None",
                                      summary='{"qname": "m::UserModel"}')

    body = messages[1].content
    assert "CHANGE SUMMARY" in body
    assert '{"qname": "m::UserModel"}' in body
    assert body.index("CHANGE SUMMARY") < body.index("DIFF")


def test_run_review_threads_the_summary_into_the_request(env):
    config, conn = env
    seen: dict = {}

    class CapturingModel(ScriptedReviewModel):
        def invoke(self, messages):
            seen.setdefault("request", messages[1].content)
            return super().invoke(messages)

    run_review(config, conn, prompt="review this diff", diff="+ leaked = True",
               summary="CHANGED: m::UserModel", model=CapturingModel(),
               max_turns=5)

    assert "CHANGED: m::UserModel" in seen["request"]


def test_run_review_threads_the_policy_into_the_request(env):
    config, conn = env
    model = ScriptedReviewModel()

    run_review(config, conn, prompt="p", diff="DIFF-BODY", model=model,
               policy="CUSTOM POLICY TEXT")

    assert model.system_content == "CUSTOM POLICY TEXT"
