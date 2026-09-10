"""End-to-end driver test: prompt+summary -> worksheet -> resolved LoopResult."""

from __future__ import annotations

import sqlite3

import pytest
from langchain_core.messages import AIMessage

from code_review_ai.config import load_config
from code_review_ai.db import init_schema
from code_review_ai.review_loop import Hooks
from code_review_ai.review_loop.hooks import POINT_RUN_FINISHED
from code_review_ai.review_loop.runner import (
    build_initial_messages,
    run_free_review,
    run_review,
    worksheet_from_summary,
)
from code_review_ai.review_loop.schemas import (
    FINISH_REVIEW_TOOL,
    UPDATE_REVIEW_TOOL,
    ReviewItem,
)


class ScriptedReviewModel:
    """Turn 1 reads app.py; turn 2 confirms the candidate; turn 3 stops."""

    def __init__(self):
        self.saw_system = False
        self.saw_tool_reply = False

    def bind_tools(self, schemas):
        self.schemas = [schema["name"] for schema in schemas]
        return self

    def invoke(self, messages):
        from langchain_core.messages import SystemMessage, ToolMessage

        for message in messages:
            if isinstance(message, SystemMessage):
                self.saw_system = True
            if isinstance(message, ToolMessage):
                self.saw_tool_reply = True
        turn = len([m for m in messages if m.type == "ai"])
        if turn == 0:
            return AIMessage(content="", tool_calls=[
                {"name": "read_file", "args": {"path": "app.py",
                                               "start_line": 1, "end_line": 3},
                 "id": "read-1"}])
        if turn == 1:
            return AIMessage(content="", tool_calls=[
                {"name": UPDATE_REVIEW_TOOL,
                 "args": {"qname": "app::login", "state": "confirmed",
                          "finding": {"file": "app.py", "line": 2,
                                      "title": "leaks None on empty user",
                                      "description": "login returns None for "
                                                     "an empty user."}},
                 "id": "confirm-login"}])
        return AIMessage(content="all resolved.", tool_calls=[])


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


def test_worksheet_from_summary_covers_both_change_collections():
    summary = {
        "changed_functions": [{"qname": "app::login", "file": "app.py",
                               "start_line": 1, "end_line": 3}],
        "delete_change": [{"qname": "app::dead"}],
    }

    items = worksheet_from_summary(summary)

    assert [item.qname for item in items] == ["app::login", "app::dead"]
    assert items[0].file == "app.py"
    assert all(item.state == "candidate" for item in items)


def test_build_initial_messages_carries_prompt_summary_and_worksheet():
    items = [ReviewItem(qname="app::login")]

    messages = build_initial_messages("review auth", {"changed_functions": []}, items)

    assert messages[0].type == "system"
    assert "review auth" in messages[1].content
    assert "changed_functions" in messages[1].content
    assert '["app::login"]' in messages[1].content


def test_worksheet_roster_does_not_repeat_the_summarys_coordinates():
    """The roster is qnames only; repeating file/lines doubled the summary.

    Every changed_functions record already carries file/start_line/end_line, so
    a row-shaped worksheet was a second copy of the same coordinates. The order
    is still the worksheet's (changed_functions, then delete_change).
    """
    items = [ReviewItem(qname="app::login", file="app.py", start_line=1, end_line=9)]
    summary = {"changed_functions": [
        {"qname": "app::login", "file": "app.py", "start_line": 1, "end_line": 9}]}

    content = build_initial_messages("p", summary, items)[1].content

    # The roster is the only JSON array in the request.
    roster_line = next(line for line in content.splitlines() if line.startswith("["))
    assert roster_line == '["app::login"]'
    # ...and the coordinates appear once, in the summary rather than twice.
    assert content.count("app.py") == 1


class CostReportingModel(ScriptedReviewModel):
    """Like ScriptedReviewModel, but the confirm turn reports provider usage."""

    def invoke(self, messages):
        response = super().invoke(messages)
        if any(call["name"] == UPDATE_REVIEW_TOOL
               for call in getattr(response, "tool_calls", [])):
            response.usage_metadata = {
                "input_tokens": 1_000_000, "output_tokens": 1_000_000,
                "total_tokens": 2_000_000}
        return response


def test_run_review_reports_yuan_cost_from_usage(env):
    config, conn = env

    result = run_review(
        config, conn,
        prompt="check auth for regressions",
        summary={"changed_functions": [{"qname": "app::login"}]},
        model=CostReportingModel(), max_turns=5)

    assert result.review_complete is True
    # turn 1 priced: 1M miss input (1.5) + 1M output (4.5) = 6.0 元
    assert result.usage["total_tokens"] == 2_000_000
    assert result.cost == pytest.approx(6.0)


def test_run_review_resolves_worksheet_and_reports_structured_result(env):
    config, conn = env
    model = ScriptedReviewModel()
    hooks = Hooks()
    finished = {}

    hooks.on(POINT_RUN_FINISHED, lambda _event, context: finished.update(context))
    result = run_review(
        config, conn,
        prompt="check auth for regressions",
        summary={"changed_functions": [{"qname": "app::login"}]},
        model=model, hooks=hooks, max_turns=5)

    assert result.failure_reason is None
    assert result.review_complete is True
    assert result.items["app::login"].state == "confirmed"
    assert result.findings[0].title == "leaks None on empty user"
    # no index rows in the empty DB, so no flows and therefore no entries
    assert result.affected_entries == []
    assert result.tool_calls == ["read_file", UPDATE_REVIEW_TOOL]
    assert result.tool_call_count == 2
    assert result.tool_request_count == 2
    assert [record["status"] for record in result.tool_trace] == ["success", "success"]
    assert model.saw_system and model.saw_tool_reply
    assert "read_file" in model.schemas and "get_impact" in model.schemas
    assert UPDATE_REVIEW_TOOL in model.schemas
    assert finished["failure_reason"] is None
    assert finished["finding_count"] == 1


class FreeFormModel:
    """Turn 1 reads the file, turn 2 submits one finding over finish_review."""

    def bind_tools(self, schemas):
        self.schemas = [schema["name"] for schema in schemas]
        return self

    def invoke(self, messages):
        turn = len([m for m in messages if m.type == "ai"])
        if turn == 0:
            return AIMessage(content="", tool_calls=[
                {"name": "read_file", "args": {"path": "app.py",
                                               "start_line": 1, "end_line": 3},
                 "id": "read-1"}])
        return AIMessage(content="", tool_calls=[
            {"name": FINISH_REVIEW_TOOL,
             "args": {"findings": [{"file": "app.py", "line": 2,
                                    "title": "leaks None on empty user",
                                    "description": "login returns None."}]},
             "id": "submit-1"}])


def test_run_free_review_needs_no_worksheet_and_no_index(env):
    """The no-index arm: diff in, findings out, no graph tool and no conn."""
    config, _conn = env
    model = FreeFormModel()

    result = run_free_review(config, prompt="review this diff", diff="DIFF-BODY",
                             model=model, max_turns=5)

    assert result.failure_reason is None
    assert result.review_complete is True
    assert result.items == {}  # nothing was resolved row by row
    assert [finding.file for finding in result.findings] == ["app.py"]
    assert "get_impact" not in model.schemas
    assert FINISH_REVIEW_TOOL in model.schemas


def test_run_free_review_carries_the_diff_into_the_request(env):
    config, _conn = env
    seen = {}

    class CapturingModel(FreeFormModel):
        def invoke(self, messages):
            seen.setdefault("request", messages[1].content)
            return super().invoke(messages)

    run_free_review(config, prompt="review this diff", diff="+ leaked = True",
                    model=CapturingModel(), max_turns=5)

    assert "review this diff" in seen["request"]
    assert "+ leaked = True" in seen["request"]
