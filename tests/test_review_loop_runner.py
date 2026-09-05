"""End-to-end driver test: prompt+summary -> run_loop -> final_text review."""

from __future__ import annotations

import sqlite3

import pytest
from langchain_core.messages import AIMessage

from code_review_ai.config import load_config
from code_review_ai.db import init_schema
from code_review_ai.review_loop import Hooks
from code_review_ai.review_loop.hooks import POINT_RUN_FINISHED
from code_review_ai.review_loop.runner import build_initial_messages, run_review


class ScriptedReviewModel:
    """Turn 1 reads app.py; turn 2 stops with the written review."""

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
        if not self.saw_tool_reply:
            return AIMessage(content="", tool_calls=[
                {"name": "read_file", "args": {"path": "app.py",
                                               "start_line": 1, "end_line": 3},
                 "id": "read-1"}])
        return AIMessage(content="app.py:1 returns early on empty user; "
                                 "login() leaks None.", tool_calls=[])


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


def test_build_initial_messages_carries_prompt_and_summary():
    messages = build_initial_messages("review auth", {"changed_functions": []})
    assert messages[0].type == "system"
    assert "review auth" in messages[1].content
    assert "changed_functions" in messages[1].content


def test_run_review_feeds_summary_and_returns_the_ai_review_text(env):
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
    assert result.final_text == ("app.py:1 returns early on empty user; "
                                 "login() leaks None.")
    assert result.tool_calls == ["read_file"]
    assert result.tool_call_count == 1
    assert [record["status"] for record in result.tool_trace] == ["success"]
    assert model.saw_system and model.saw_tool_reply
    assert "read_file" in model.schemas and "get_impact" in model.schemas
    assert finished["failure_reason"] is None
    assert finished["final_chars"] == len(result.final_text)
