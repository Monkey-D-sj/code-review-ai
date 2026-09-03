from langchain_core.messages import AIMessage, ToolMessage

from conftest import FIXTURES as FIX, Q
from code_review_ai.config import load_config
from code_review_ai.db import connect, init_schema
from code_review_ai.indexer import rebuild
from code_review_ai.review_agent.runner import _review_paths, run_review


class ScriptedModel:
    """Small bind_tools-compatible fake model; no network access."""

    def bind_tools(self, tools):
        self.names = {tool.name for tool in tools}
        return self

    def invoke(self, messages):
        if any(isinstance(message, ToolMessage) for message in messages):
            return AIMessage(content="", tool_calls=[{
                "name": "submit_review", "id": "submit-1", "args": {
                    "findings": [{"file": "auth.py", "line": 1,
                                  "title": "Scripted finding",
                                  "description": "Evidence-backed fake result."}],
                    "affected_symbols": [Q("auth", "login")],
                    "affected_files": ["auth.py"], "affected_entries": [], "tests": [],
                }}])
        return AIMessage(content="", tool_calls=[{
            "name": "get_impact", "id": "impact-1",
            "args": {"symbols": [Q("auth", "login")]}}])


def test_review_paths_always_exclude_uv_lock():
    assert _review_paths(None) == [":(exclude)uv.lock"]
    assert _review_paths(["code_review_ai"]) == [
        "code_review_ai", ":(exclude)uv.lock"]


class MixedToolCallModel:
    """Models a provider response that combines an action and final tool."""

    def bind_tools(self, tools):
        return self

    def invoke(self, messages):
        tool_messages = [message for message in messages
                         if isinstance(message, ToolMessage)]
        if tool_messages:
            assert {message.tool_call_id for message in tool_messages} == {
                "impact-1", "submit-1"}
            return AIMessage(content="", tool_calls=[{
                "name": "submit_review", "id": "submit-2", "args": {
                    "findings": [], "affected_symbols": [], "affected_files": [],
                    "affected_entries": [], "tests": [],
                }}])
        return AIMessage(content="", tool_calls=[
            {"name": "get_impact", "id": "impact-1",
             "args": {"symbols": [Q("auth", "login")]}},
            {"name": "submit_review", "id": "submit-1", "args": {
                "findings": [], "affected_symbols": [], "affected_files": [],
                "affected_entries": [], "tests": [],
            }},
        ])


class BudgetLimitedModel:
    """Uses more action calls than allowed, then checks the rejection replies."""

    def __init__(self):
        self.requested_actions = 0

    def bind_tools(self, tools):
        return self

    def invoke(self, messages):
        tool_messages = [message for message in messages
                         if isinstance(message, ToolMessage)]
        rejected = [message for message in tool_messages
                    if "budget is exhausted" in str(message.content)]
        if rejected:
            assert {message.tool_call_id for message in rejected} == {"impact-51", "impact-52"}
            return AIMessage(content="", tool_calls=[{
                "name": "submit_review", "id": "submit-final", "args": {
                    "findings": [], "affected_symbols": [], "affected_files": [],
                    "affected_entries": [], "tests": [],
                }}])
        action_count = self.requested_actions
        self.requested_actions += 2
        return AIMessage(content="", tool_calls=[
            {"name": "get_impact", "id": f"impact-{action_count + 1}",
             "args": {"symbols": [Q("auth", "login")]}},
            {"name": "get_impact", "id": f"impact-{action_count + 2}",
             "args": {"symbols": [Q("auth", "login")]}},
        ])


class Utf8CheckingModel:
    """Fails the test if a lone surrogate reaches the provider boundary."""

    def bind_tools(self, tools):
        return self

    def invoke(self, messages):
        import json

        json.dumps([message.model_dump() for message in messages],
                   ensure_ascii=False).encode("utf-8")
        return AIMessage(content="", tool_calls=[{
            "name": "submit_review", "id": "submit-utf8", "args": {
                "findings": [], "affected_symbols": [], "affected_files": [],
                "affected_entries": [], "tests": [],
            }}])


class ForbiddenReadModel:
    """Attempts a protected read, then submits after receiving its denial."""

    def bind_tools(self, tools):
        return self

    def invoke(self, messages):
        tool_messages = [message for message in messages
                         if isinstance(message, ToolMessage)]
        if tool_messages:
            assert '"status": "rejected_policy"' in str(tool_messages[-1].content)
            return AIMessage(content="", tool_calls=[{
                "name": "submit_review", "id": "submit-policy", "args": {
                    "findings": [], "affected_symbols": [], "affected_files": [],
                    "affected_entries": [], "tests": [],
                }}])
        return AIMessage(content="", tool_calls=[{
            "name": "read_file", "id": "protected-read",
            "args": {"path": ".env", "start_line": 1, "end_line": 1}}])


class ContextCompactingModel:
    """Checks that completed tool exchanges do not survive into later prompts."""

    def __init__(self):
        self.turn = 0

    def bind_tools(self, tools):
        return self

    def invoke(self, messages):
        tool_ids = {message.tool_call_id for message in messages
                    if isinstance(message, ToolMessage)}
        if self.turn == 0:
            assert tool_ids == set()
            response = AIMessage(content="", tool_calls=[{
                "name": "get_impact", "id": "impact-first",
                "args": {"symbols": [Q("auth", "login")]}}])
        elif self.turn == 1:
            assert tool_ids == {"impact-first"}
            response = AIMessage(content="", tool_calls=[{
                "name": "get_impact", "id": "impact-second",
                "args": {"symbols": [Q("auth", "login")]}}])
        else:
            assert tool_ids == {"impact-second"}
            response = AIMessage(content="", tool_calls=[{
                "name": "submit_review", "id": "submit-compacted", "args": {
                    "findings": [], "affected_symbols": [], "affected_files": [],
                    "affected_entries": [], "tests": [],
                }}])
        self.turn += 1
        return response


class ReviewItemModel:
    """Resolves the system-created qname candidate using real evidence."""

    def __init__(self):
        self.turn = 0

    def bind_tools(self, tools):
        return self

    def invoke(self, messages):
        if self.turn == 0:
            response = AIMessage(content="", tool_calls=[{
                "name": "get_impact", "id": "impact-review-item",
                "args": {"symbols": [Q("auth", "login")],
                         "for_qname": Q("auth", "login")}}])
        elif self.turn == 1:
            response = AIMessage(content="", tool_calls=[{
                "name": "update_review_item", "id": "resolve-review-item", "args": {
                    "qname": Q("auth", "login"), "state": "confirmed",
                    "finding": {"file": "auth.py", "line": 1,
                                "title": "Resolved by manifest",
                                "description": "Uses impact evidence."},
                }}])
        else:
            response = AIMessage(content="", tool_calls=[{
                "name": "submit_review", "id": "submit-review-item", "args": {
                    "findings": [{"file": "auth.py", "line": 2,
                                  "title": "Unconfirmed model output",
                                  "description": "Must be filtered."}],
                    "affected_symbols": [], "affected_files": [],
                    "affected_entries": [], "tests": [],
                }}])
        self.turn += 1
        return response


def test_graph_runs_impact_then_terminal_submission(tmp_path):
    config = load_config(FIX)
    config.repo_path = FIX
    config.db_path = str(tmp_path / "index.db")
    conn = connect(config.db_path)
    init_schema(conn)
    rebuild(config, conn)

    events = []
    result = run_review(config, conn, model=ScriptedModel(), diff="diff --git a/auth.py b/auth.py",
                        symbols=[Q("auth", "login")],
                        progress=lambda event, data: events.append((event, data)))

    assert result["failure_reason"] is None
    assert result["findings"][0]["title"] == "Scripted finding"
    assert result["tool_calls"] == ["get_impact", "submit_review"]
    assert result["tool_call_count"] == 1
    assert result["tool_trace"][0]["tool"] == "get_impact"
    assert result["tool_trace"][0]["tool_call_id"] == "impact-1"
    assert [item["status"] for item in result["tool_trace"]] == ["executed", "executed"]
    assert result["initial_context"]["change_summary_chars"] > 0
    assert [event for event, _ in events] == [
        "summary_ready", "agent_started", "model_request_started",
        "model_response_received", "tool_requests", "tool_completed",
        "model_request_started", "model_response_received", "tool_requests", "finished"]
    tool_event = next(data for event, data in events if event == "tool_requests")
    assert tool_event["calls"] == [{
        "name": "get_impact", "args": {"symbols": [Q("auth", "login")]}}]


def test_graph_replies_to_every_mixed_tool_call_before_retrying(tmp_path):
    config = load_config(FIX)
    config.repo_path = FIX
    config.db_path = str(tmp_path / "index.db")
    conn = connect(config.db_path)
    init_schema(conn)
    rebuild(config, conn)

    result = run_review(config, conn, model=MixedToolCallModel(),
                        diff="diff --git a/auth.py b/auth.py",
                        symbols=[Q("auth", "login")])

    assert result["failure_reason"] is None
    assert result["tool_calls"] == ["get_impact", "submit_review", "submit_review"]
    assert [item["status"] for item in result["tool_trace"]] == [
        "rejected_protocol", "rejected_protocol", "executed"]


def test_graph_replies_to_budget_rejected_tool_calls_before_final_submission(tmp_path):
    config = load_config(FIX)
    config.repo_path = FIX
    config.db_path = str(tmp_path / "index.db")
    conn = connect(config.db_path)
    init_schema(conn)
    rebuild(config, conn)

    result = run_review(config, conn, model=BudgetLimitedModel(),
                        diff="diff --git a/auth.py b/auth.py",
                        symbols=[Q("auth", "login")])

    assert result["failure_reason"] is None
    assert result["tool_call_count"] == 50
    assert result["tool_request_count"] == 50
    assert result["tool_calls"][-1] == "submit_review"
    assert all(item["status"] == "executed" for item in result["tool_trace"][:50])
    assert [item["status"] for item in result["tool_trace"][50:]] == [
        "rejected_budget", "rejected_budget", "executed"]


def test_graph_sanitizes_lone_surrogates_before_model_invocation(tmp_path):
    config = load_config(FIX)
    config.repo_path = FIX
    config.db_path = str(tmp_path / "index.db")
    conn = connect(config.db_path)
    init_schema(conn)
    rebuild(config, conn)

    result = run_review(config, conn, model=Utf8CheckingModel(),
                        diff="diff --git a/auth.py b/auth.py\n+\udcaf",
                        symbols=[Q("auth", "login")])

    assert result["failure_reason"] is None


def test_toolnode_policy_denial_is_traced_without_counting_as_execution(tmp_path):
    config = load_config(FIX)
    config.repo_path = FIX
    config.db_path = str(tmp_path / "index.db")
    conn = connect(config.db_path)
    init_schema(conn)
    rebuild(config, conn)

    result = run_review(config, conn, model=ForbiddenReadModel(),
                        diff="diff --git a/auth.py b/auth.py",
                        symbols=[Q("auth", "login")])

    assert result["failure_reason"] is None
    assert result["tool_request_count"] == 1
    assert result["tool_call_count"] == 0
    assert [item["status"] for item in result["tool_trace"]] == [
        "rejected_policy", "executed"]
    assert result["files_read"] == []


def test_model_context_keeps_only_the_latest_completed_tool_exchange(tmp_path):
    config = load_config(FIX)
    config.repo_path = FIX
    config.db_path = str(tmp_path / "index.db")
    conn = connect(config.db_path)
    init_schema(conn)
    rebuild(config, conn)

    result = run_review(config, conn, model=ContextCompactingModel(),
                        diff="diff --git a/auth.py b/auth.py",
                        symbols=[Q("auth", "login")])

    assert result["failure_reason"] is None
    assert result["tool_call_count"] == 2


def test_system_created_qname_candidate_can_be_confirmed(tmp_path):
    config = load_config(FIX)
    config.repo_path = FIX
    config.db_path = str(tmp_path / "index.db")
    conn = connect(config.db_path)
    init_schema(conn)
    rebuild(config, conn)

    result = run_review(config, conn, model=ReviewItemModel(),
                        diff="diff --git a/auth.py b/auth.py",
                        symbols=[Q("auth", "login")])

    item = result["review_items"][Q("auth", "login")]
    assert item["state"] == "confirmed"
    assert item["evidence_refs"] == ["impact-review-item"]
    assert result["confirmed_findings"] == [item["finding"]]
    assert result["findings"] == result["confirmed_findings"]
    assert result["review_complete"] is True
