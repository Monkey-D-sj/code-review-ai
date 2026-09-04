from langchain_core.messages import AIMessage, ToolMessage

from conftest import FIXTURES as FIX, Q
from code_review_ai.config import load_config
from code_review_ai.db import connect, init_schema
from code_review_ai.indexer import rebuild
from code_review_ai.review_agent.graph import _record_evidence, _resolve_review_item
from code_review_ai.review_agent.runner import _review_paths, run_review
from code_review_ai.review_agent.schemas import ReviewItem


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


class InventedEvidenceModel:
    """Confirms with fabricated file:line evidence_refs after real evidence."""

    def __init__(self):
        self.turn = 0

    def bind_tools(self, tools):
        return self

    def invoke(self, messages):
        if self.turn == 0:
            response = AIMessage(content="", tool_calls=[{
                "name": "get_impact", "id": "impact-real",
                "args": {"symbols": [Q("auth", "login")],
                         "for_qname": Q("auth", "login")}}])
        elif self.turn == 1:
            # The model invents file:line refs instead of tool call ids.
            response = AIMessage(content="", tool_calls=[{
                "name": "update_review_item", "id": "resolve-invented", "args": {
                    "qname": Q("auth", "login"), "state": "confirmed",
                    "finding": {"file": "auth.py", "line": 1,
                                "title": "Resolved by manifest",
                                "description": "Uses impact evidence."},
                    "evidence_refs": ["auth.py:1", "not-a-call-id"],
                }}])
        else:
            response = AIMessage(content="", tool_calls=[{
                "name": "submit_review", "id": "submit-invented", "args": {
                    "findings": [], "affected_symbols": [], "affected_files": [],
                    "affected_entries": [], "tests": [],
                }}])
        self.turn += 1
        return response


def test_confirm_ignores_invented_evidence_refs(tmp_path):
    """Real auto-recorded evidence is what counts; junk refs are dropped, not fatal."""
    config = load_config(FIX)
    config.repo_path = FIX
    config.db_path = str(tmp_path / "index.db")
    conn = connect(config.db_path)
    init_schema(conn)
    rebuild(config, conn)

    result = run_review(config, conn, model=InventedEvidenceModel(),
                        diff="diff --git a/auth.py b/auth.py",
                        symbols=[Q("auth", "login")])

    item = result["review_items"][Q("auth", "login")]
    assert item["state"] == "confirmed"
    assert item["evidence_refs"] == ["impact-real"]
    assert result["review_complete"] is True


class NoEvidenceModel:
    """Confirms a candidate without ever recording evidence via for_qname."""

    def __init__(self):
        self.turn = 0

    def bind_tools(self, tools):
        return self

    def invoke(self, messages):
        tool_messages = [message for message in messages
                         if isinstance(message, ToolMessage)]
        if tool_messages and self.turn == 1:
            assert '"status": "rejected_policy"' in str(tool_messages[-1].content)
        if self.turn == 0:
            response = AIMessage(content="", tool_calls=[{
                "name": "update_review_item", "id": "resolve-no-evidence", "args": {
                    "qname": Q("auth", "login"), "state": "confirmed",
                    "finding": {"file": "auth.py", "line": 1,
                                "title": "No evidence",
                                "description": "Never investigated."},
                }}])
        else:
            response = AIMessage(content="", tool_calls=[{
                "name": "submit_review", "id": "submit-no-evidence", "args": {
                    "findings": [], "affected_symbols": [], "affected_files": [],
                    "affected_entries": [], "tests": [],
                }}])
        self.turn += 1
        return response


def test_confirm_without_recorded_evidence_is_rejected(tmp_path):
    """A confirmed item must have graph-recorded evidence, or it is rejected."""
    config = load_config(FIX)
    config.repo_path = FIX
    config.db_path = str(tmp_path / "index.db")
    conn = connect(config.db_path)
    init_schema(conn)
    rebuild(config, conn)

    result = run_review(config, conn, model=NoEvidenceModel(),
                        diff="diff --git a/auth.py b/auth.py",
                        symbols=[Q("auth", "login")])

    item = result["review_items"][Q("auth", "login")]
    assert item["state"] == "candidate"
    statuses = [t["status"] for t in result["tool_trace"]]
    assert "rejected_policy" in statuses
    assert result["review_complete"] is False


class InvalidToolCallModel:
    """First assistant reply carries a tool call whose arguments are bad JSON.

    langchain keeps such calls in ``invalid_tool_calls`` with ``tool_calls``
    empty, yet the outbound serializer still emits them as ``tool_calls``. The
    graph must pair a rejection ToolMessage for the id before calling the model
    again, or the provider rejects the follow-up request.
    """

    def __init__(self):
        self.turns = 0

    def bind_tools(self, tools):
        return self

    def invoke(self, messages):
        self.turns += 1
        if self.turns == 1:
            return AIMessage(content="", invalid_tool_calls=[{
                "id": "call-malformed-1", "name": "submit_review",
                "args": '{"findings": [', "error": "not valid JSON"}])
        paired = [message for message in messages
                  if isinstance(message, ToolMessage)
                  and message.tool_call_id == "call-malformed-1"]
        assert paired, "the malformed tool call must be paired with a ToolMessage"
        return AIMessage(content="", tool_calls=[{
            "name": "submit_review", "id": "submit-clean", "args": {
                "findings": [], "affected_symbols": [], "affected_files": [],
                "affected_entries": [], "tests": []}}])


def test_malformed_tool_call_arguments_are_paired_with_rejection(tmp_path):
    """A call left in invalid_tool_calls gets a ToolMessage so retries stay valid."""
    config = load_config(FIX)
    config.repo_path = FIX
    config.db_path = str(tmp_path / "index.db")
    conn = connect(config.db_path)
    init_schema(conn)
    rebuild(config, conn)

    result = run_review(config, conn, model=InvalidToolCallModel(),
                        diff="diff --git a/auth.py b/auth.py",
                        symbols=[Q("auth", "login")])

    assert result["failure_reason"] is None


def _review_item(state: str = "candidate", refs: tuple[str, ...] = ()) -> ReviewItem:
    return ReviewItem(qname=Q("auth", "login"), file="auth.py", state=state,
                      evidence_refs=list(refs))


def test_record_evidence_guards_for_qname():
    """A for_qname evidence call records its id, guarded by an active candidate."""
    items = {Q("auth", "login"): _review_item()}
    recorded, reason = _record_evidence(
        {"args": {"for_qname": Q("auth", "login")}}, items, "call-ev")
    assert reason is None
    assert recorded[Q("auth", "login")].evidence_refs == ["call-ev"]
    # an inactive or unknown item is rejected, never recorded
    recorded, reason = _record_evidence(
        {"args": {"for_qname": Q("auth", "missing")}}, items, "call-ev")
    assert reason and reason.startswith("for_qname")
    # no for_qname is a no-op
    recorded, reason = _record_evidence({"args": {}}, items, "call-ev")
    assert reason is None and recorded[Q("auth", "login")].evidence_refs == []


def test_resolve_review_item_confirm_gate_and_dismiss():
    """Confirm is gated on recorded evidence; dismiss records its reason."""
    finding = {"file": "auth.py", "line": 1, "title": "T", "description": "D"}
    # confirming without recorded evidence is rejected with guidance
    items = {Q("auth", "login"): _review_item(state="candidate", refs=())}
    resolved, reason = _resolve_review_item(
        {"args": {"qname": Q("auth", "login"), "state": "confirmed",
                  "finding": finding}},
        items, {"call-ev"})
    assert reason and "evidence" in reason
    # confirming with recorded evidence drops invented refs but confirms the item
    items = {Q("auth", "login"): _review_item(state="candidate", refs=["call-ev"])}
    resolved, reason = _resolve_review_item(
        {"args": {"qname": Q("auth", "login"), "state": "confirmed",
                  "finding": finding, "evidence_refs": ["auth.py:1", "bogus"]}},
        items, {"call-ev"})
    assert reason is None
    assert resolved[Q("auth", "login")].state == "confirmed"
    assert resolved[Q("auth", "login")].evidence_refs == ["call-ev"]
    # dismissing records the reason
    items = {Q("auth", "login"): _review_item(state="candidate")}
    resolved, reason = _resolve_review_item(
        {"args": {"qname": Q("auth", "login"), "state": "dismissed",
                  "reason": "wontfix"}},
        items, set())
    assert reason is None and resolved[Q("auth", "login")].state == "dismissed"
    # an unknown qname cannot be resolved
    resolved, reason = _resolve_review_item(
        {"args": {"qname": Q("auth", "missing"), "state": "dismissed",
                  "reason": "x"}},
        items, set())
    assert reason and reason.startswith("qname")
