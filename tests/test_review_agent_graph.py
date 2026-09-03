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


_FINDING = {"file": "auth.py", "line": 1, "title": "Claimed without evidence",
            "description": "No tool ever looked at this symbol."}


def _submit_call(call_id: str) -> dict:
    return {"name": "submit_review", "id": call_id, "args": {
        "findings": [], "affected_symbols": [], "affected_files": [],
        "affected_entries": [], "tests": []}}


class UnbackedConfirmModel:
    """Confirms a candidate it never gathered any evidence for."""

    def __init__(self, evidence_refs: list[str] | None = None):
        self.turn = 0
        self.evidence_refs = evidence_refs or []
        self.refusals: list[str] = []

    def bind_tools(self, tools):
        return self

    def invoke(self, messages):
        self.refusals.extend(
            str(message.content) for message in messages
            if isinstance(message, ToolMessage) and "rejected_policy" in str(message.content))
        if self.turn == 0:
            response = AIMessage(content="", tool_calls=[{
                "name": "update_review_item", "id": "confirm-unbacked", "args": {
                    "qname": Q("auth", "login"), "state": "confirmed",
                    "finding": _FINDING, "evidence_refs": self.evidence_refs}}])
        else:
            response = AIMessage(content="", tool_calls=[_submit_call("submit-unbacked")])
        self.turn += 1
        return response


def _indexed_config(tmp_path):
    config = load_config(FIX)
    config.repo_path = FIX
    config.db_path = str(tmp_path / "index.db")
    conn = connect(config.db_path)
    init_schema(conn)
    rebuild(config, conn)
    return config, conn


def test_confirmation_without_recorded_evidence_is_refused(tmp_path):
    config, conn = _indexed_config(tmp_path)
    model = UnbackedConfirmModel()

    result = run_review(config, conn, model=model,
                        diff="diff --git a/auth.py b/auth.py",
                        symbols=[Q("auth", "login")])

    item = result["review_items"][Q("auth", "login")]
    assert item["state"] == "candidate"
    assert item["finding"] is None
    assert result["confirmed_findings"] == []
    assert result["review_complete"] is False
    assert [record["status"] for record in result["tool_trace"]] == [
        "rejected_policy", "executed"]
    assert "for_qname" in model.refusals[0]


def test_confirmation_with_forged_evidence_refs_is_refused(tmp_path):
    config, conn = _indexed_config(tmp_path)
    model = UnbackedConfirmModel(evidence_refs=["call-that-never-ran"])

    result = run_review(config, conn, model=model,
                        diff="diff --git a/auth.py b/auth.py",
                        symbols=[Q("auth", "login")])

    assert result["review_items"][Q("auth", "login")]["state"] == "candidate"
    assert "previously executed evidence tools" in model.refusals[0]


class ExplodingModel:
    """Succeeds once, then fails the way a provider outage would."""

    def __init__(self):
        self.turn = 0

    def bind_tools(self, tools):
        return self

    def invoke(self, messages):
        self.turn += 1
        if self.turn == 1:
            return AIMessage(content="", tool_calls=[{
                "name": "get_impact", "id": "impact-before-outage",
                "args": {"symbols": [Q("auth", "login")],
                         "for_qname": Q("auth", "login")}}])
        raise RuntimeError("provider connection reset")


def test_provider_failure_keeps_the_partial_audit_trail(tmp_path):
    config, conn = _indexed_config(tmp_path)

    result = run_review(config, conn, model=ExplodingModel(),
                        diff="diff --git a/auth.py b/auth.py",
                        symbols=[Q("auth", "login")])

    assert "provider connection reset" in result["failure_reason"]
    assert result["tool_call_count"] == 1
    assert [record["tool_call_id"] for record in result["tool_trace"]] == [
        "impact-before-outage"]
    assert result["review_items"][Q("auth", "login")]["evidence_refs"] == [
        "impact-before-outage"]
    assert result["files_read"] == []


class TokenHeavyModel:
    """Reports enough usage per turn to exhaust a small token budget."""

    def __init__(self, tokens_per_turn: int = 1_000, action_turns: int = 2):
        self.turn = 0
        self.tokens_per_turn = tokens_per_turn
        self.action_turns = action_turns

    def bind_tools(self, tools):
        return self

    def invoke(self, messages):
        self.turn += 1
        usage = {"input_tokens": self.tokens_per_turn - 1, "output_tokens": 1,
                 "total_tokens": self.tokens_per_turn}
        if self.turn <= self.action_turns:
            return AIMessage(content="", tool_calls=[{
                "name": "get_impact", "id": f"impact-{self.turn}",
                "args": {"symbols": [Q("auth", "login")],
                         "for_qname": Q("auth", "login")}}], usage_metadata=usage)
        return AIMessage(content="", tool_calls=[_submit_call(f"submit-{self.turn}")],
                         usage_metadata=usage)


def test_token_budget_forces_submission_before_the_call_budget(tmp_path, monkeypatch):
    config, conn = _indexed_config(tmp_path)
    monkeypatch.setenv("CRAI_REVIEW_MAX_TOTAL_TOKENS", "1500")
    events = []

    result = run_review(config, conn, model=TokenHeavyModel(),
                        diff="diff --git a/auth.py b/auth.py",
                        symbols=[Q("auth", "login")],
                        progress=lambda event, data: events.append((event, data)))

    exhausted = [data for event, data in events if event == "budget_exhausted"]
    assert [data["limit"] for data in exhausted] == ["total_tokens"]
    assert result["budgets"]["max_total_tokens"] == 1500
    assert result["budgets"]["total_tokens"] == 3000
    assert result["tool_call_count"] == 1
    assert [record["status"] for record in result["tool_trace"]] == [
        "executed", "rejected_budget", "executed"]
    assert result["failure_reason"] is None


def test_wall_clock_budget_forces_submission(tmp_path, monkeypatch):
    config, conn = _indexed_config(tmp_path)
    monkeypatch.setenv("CRAI_REVIEW_WALL_CLOCK_SECONDS", "0.001")
    events = []

    result = run_review(config, conn, model=TokenHeavyModel(action_turns=1),
                        diff="diff --git a/auth.py b/auth.py",
                        symbols=[Q("auth", "login")],
                        progress=lambda event, data: events.append((event, data)))

    assert [data["limit"] for event, data in events
            if event == "budget_exhausted"] == ["wall_clock"]
    assert result["tool_call_count"] == 0
    assert result["failure_reason"] is None
