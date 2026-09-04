from langchain_core.messages import AIMessage, ToolMessage

from conftest import FIXTURES as FIX, Q
from code_review_ai.config import load_config
from code_review_ai.db import connect, init_schema
from code_review_ai.indexer import rebuild
from code_review_ai.review_agent.graph import _record_evidence, _resolve_review_item
from code_review_ai.review_agent.runner import _review_paths, run_review
from code_review_ai.review_agent.schemas import ReviewItem
from code_review_ai.review_agent.tools import create_tool_registry


def _finding(file: str = "auth.py", line: int = 1) -> dict:
    return {"file": file, "line": line, "title": "Resolved by manifest",
            "description": "Uses impact evidence."}


def test_review_paths_always_exclude_uv_lock():
    assert _review_paths(None) == [":(exclude)uv.lock"]
    assert _review_paths(["code_review_ai"]) == [
        "code_review_ai", ":(exclude)uv.lock"]


class ConfirmingModel:
    """Gathers real impact evidence, then confirms the single candidate."""

    def bind_tools(self, tools):
        self.names = {tool.name for tool in tools}
        return self

    def invoke(self, messages):
        if any(isinstance(message, ToolMessage) for message in messages):
            return AIMessage(content="", tool_calls=[{
                "name": "update_review_item", "id": "confirm-1", "args": {
                    "qname": Q("auth", "login"), "state": "confirmed",
                    "finding": _finding()}}])
        return AIMessage(content="", tool_calls=[{
            "name": "get_impact", "id": "impact-1",
            "args": {"symbols": [Q("auth", "login")],
                     "for_qname": Q("auth", "login")}}])


def test_graph_resolves_candidate_then_auto_finishes(tmp_path):
    config = load_config(FIX)
    config.repo_path = FIX
    config.db_path = str(tmp_path / "index.db")
    conn = connect(config.db_path)
    init_schema(conn)
    rebuild(config, conn)

    events = []
    result = run_review(config, conn, model=ConfirmingModel(),
                        diff="diff --git a/auth.py b/auth.py",
                        symbols=[Q("auth", "login")],
                        progress=lambda event, data: events.append((event, data)))

    assert result["failure_reason"] is None
    assert result["findings"] == [_finding()]
    assert result["findings"] == result["confirmed_findings"]
    assert result["review_complete"] is True
    # The candidate flow offers no terminal tool and never needs submit_review.
    assert result["tool_calls"] == ["get_impact", "update_review_item"]
    assert result["tool_call_count"] == 2
    assert [item["status"] for item in result["tool_trace"]] == ["executed", "executed"]
    assert result["tool_trace"][0]["tool_call_id"] == "impact-1"
    assert result["review_items"][Q("auth", "login")]["state"] == "confirmed"
    assert result["review_items"][Q("auth", "login")]["evidence_refs"] == ["impact-1"]
    assert result["initial_context"]["change_summary_chars"] > 0
    assert [event for event, _ in events] == [
        "summary_ready", "agent_started", "model_request_started",
        "model_response_received", "tool_requests", "tool_completed",
        "model_request_started", "model_response_received", "tool_requests",
        "tool_completed", "finished"]
    tool_event = next(data for event, data in events if event == "tool_requests")
    assert tool_event["calls"] == [{
        "name": "get_impact", "args": {"symbols": [Q("auth", "login")],
                                       "for_qname": Q("auth", "login")}}]


def test_dismissing_the_last_candidate_also_finishes_empty(tmp_path):
    config = load_config(FIX)
    config.repo_path = FIX
    config.db_path = str(tmp_path / "index.db")
    conn = connect(config.db_path)
    init_schema(conn)
    rebuild(config, conn)

    class DismissAllModel:
        def bind_tools(self, tools):
            return self

        def invoke(self, messages):
            return AIMessage(content="", tool_calls=[{
                "name": "update_review_item", "id": "dismiss-1", "args": {
                    "qname": Q("auth", "login"), "state": "dismissed",
                    "reason": "no regression introduced by this change"}}])

    result = run_review(config, conn, model=DismissAllModel(),
                        diff="diff --git a/auth.py b/auth.py",
                        symbols=[Q("auth", "login")])

    assert result["failure_reason"] is None
    assert result["review_complete"] is True
    assert result["findings"] == []
    assert result["confirmed_findings"] == []
    assert result["review_items"][Q("auth", "login")]["state"] == "dismissed"


class UnknownToolRecoveryModel:
    """Mixes a valid action with an unknown tool, then recovers and resolves."""

    def __init__(self):
        self.turn = 0

    def bind_tools(self, tools):
        return self

    def invoke(self, messages):
        if self.turn == 0:
            response = AIMessage(content="", tool_calls=[
                {"name": "get_impact", "id": "impact-mixed",
                 "args": {"symbols": [Q("auth", "login")]}},
                {"name": "not_a_tool", "id": "unknown-1", "args": {}}])
        elif self.turn == 1:
            # The nudge node answered both mixed calls with rejections.
            response = AIMessage(content="", tool_calls=[{
                "name": "get_impact", "id": "impact-recovered",
                "args": {"symbols": [Q("auth", "login")],
                         "for_qname": Q("auth", "login")}}])
        else:
            response = AIMessage(content="", tool_calls=[{
                "name": "update_review_item", "id": "confirm-recovered", "args": {
                    "qname": Q("auth", "login"), "state": "confirmed",
                    "finding": _finding()}}])
        self.turn += 1
        return response


def test_graph_replies_to_every_unusable_tool_call_before_retrying(tmp_path):
    """A turn mixing an action with an unknown tool is nudged, never partially run."""
    config = load_config(FIX)
    config.repo_path = FIX
    config.db_path = str(tmp_path / "index.db")
    conn = connect(config.db_path)
    init_schema(conn)
    rebuild(config, conn)

    result = run_review(config, conn, model=UnknownToolRecoveryModel(),
                        diff="diff --git a/auth.py b/auth.py",
                        symbols=[Q("auth", "login")])

    assert result["failure_reason"] is None
    assert result["review_complete"] is True
    assert result["tool_calls"] == [
        "get_impact", "not_a_tool", "get_impact", "update_review_item"]
    assert [item["status"] for item in result["tool_trace"]] == [
        "rejected_protocol", "rejected_protocol", "executed", "executed"]


class BudgetExhaustedModel:
    """Requests two read-only calls every turn until the action budget ends."""

    def __init__(self):
        self.turn = 0

    def bind_tools(self, tools):
        return self

    def invoke(self, messages):
        first = self.turn * 2 + 1
        self.turn += 1
        return AIMessage(content="", tool_calls=[
            {"name": "get_impact", "id": f"impact-{first}",
             "args": {"symbols": [Q("auth", "login")]}},
            {"name": "get_impact", "id": f"impact-{first + 1}",
             "args": {"symbols": [Q("auth", "login")]}}])


def test_budget_exhaustion_with_open_candidates_is_a_failure(tmp_path):
    """No terminal handshake remains: exceeding the budget with items unresolved fails."""
    config = load_config(FIX)
    config.repo_path = FIX
    config.db_path = str(tmp_path / "index.db")
    conn = connect(config.db_path)
    init_schema(conn)
    rebuild(config, conn)

    result = run_review(config, conn, model=BudgetExhaustedModel(),
                        diff="diff --git a/auth.py b/auth.py",
                        symbols=[Q("auth", "login")])

    assert result["failure_reason"] is not None
    assert "unresolved" in result["failure_reason"]
    assert Q("auth", "login") in result["failure_reason"]
    assert result["tool_call_count"] == 50
    assert result["tool_request_count"] == 50
    assert all(item["status"] == "executed"
               for item in result["tool_trace"][:50])
    assert result["review_complete"] is False
    assert result["findings"] == []


class Utf8CheckingModel:
    """Fails the test if a lone surrogate reaches the provider boundary."""

    def bind_tools(self, tools):
        return self

    def invoke(self, messages):
        import json

        json.dumps([message.model_dump() for message in messages],
                   ensure_ascii=False).encode("utf-8")
        if any(isinstance(message, ToolMessage) for message in messages):
            return AIMessage(content="", tool_calls=[{
                "name": "update_review_item", "id": "confirm-utf8", "args": {
                    "qname": Q("auth", "login"), "state": "confirmed",
                    "finding": _finding()}}])
        return AIMessage(content="", tool_calls=[{
            "name": "get_impact", "id": "impact-utf8",
            "args": {"symbols": [Q("auth", "login")],
                     "for_qname": Q("auth", "login")}}])


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
    assert result["review_complete"] is True


class ForbiddenReadModel:
    """Attempts a protected read, then gathers evidence and confirms."""

    def __init__(self):
        self.turn = 0

    def bind_tools(self, tools):
        return self

    def invoke(self, messages):
        if self.turn == 0:
            response = AIMessage(content="", tool_calls=[{
                "name": "read_file", "id": "protected-read",
                "args": {"path": ".env", "start_line": 1, "end_line": 1}}])
        elif self.turn == 1:
            response = AIMessage(content="", tool_calls=[{
                "name": "get_impact", "id": "impact-safe",
                "args": {"symbols": [Q("auth", "login")],
                         "for_qname": Q("auth", "login")}}])
        else:
            response = AIMessage(content="", tool_calls=[{
                "name": "update_review_item", "id": "confirm-safe", "args": {
                    "qname": Q("auth", "login"), "state": "confirmed",
                    "finding": _finding()}}])
        self.turn += 1
        return response


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
    assert result["tool_request_count"] == 3
    assert result["tool_call_count"] == 2
    assert [item["status"] for item in result["tool_trace"]] == [
        "rejected_policy", "executed", "executed"]
    assert result["files_read"] == []


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
                "args": {"symbols": [Q("auth", "login")],
                         "for_qname": Q("auth", "login")}}])
        elif self.turn == 1:
            assert tool_ids == {"impact-first"}
            response = AIMessage(content="", tool_calls=[{
                "name": "get_impact", "id": "impact-second",
                "args": {"symbols": [Q("auth", "login")],
                         "for_qname": Q("auth", "login")}}])
        else:
            assert tool_ids == {"impact-second"}
            response = AIMessage(content="", tool_calls=[{
                "name": "update_review_item", "id": "confirm-compacted", "args": {
                    "qname": Q("auth", "login"), "state": "confirmed",
                    "finding": _finding()}}])
        self.turn += 1
        return response


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
    assert result["tool_call_count"] == 3
    assert result["review_complete"] is True


def test_system_created_qname_candidate_can_be_confirmed(tmp_path):
    config = load_config(FIX)
    config.repo_path = FIX
    config.db_path = str(tmp_path / "index.db")
    conn = connect(config.db_path)
    init_schema(conn)
    rebuild(config, conn)

    result = run_review(config, conn, model=ConfirmingModel(),
                        diff="diff --git a/auth.py b/auth.py",
                        symbols=[Q("auth", "login")])

    item = result["review_items"][Q("auth", "login")]
    assert item["state"] == "confirmed"
    assert item["evidence_refs"] == ["impact-1"]
    assert result["confirmed_findings"] == [item["finding"]]
    assert result["findings"] == result["confirmed_findings"]
    assert result["review_complete"] is True


class InventedEvidenceModel:
    """Confirms with fabricated evidence_refs after real evidence."""

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
        else:
            # The model invents file:line refs instead of tool call ids.
            response = AIMessage(content="", tool_calls=[{
                "name": "update_review_item", "id": "resolve-invented", "args": {
                    "qname": Q("auth", "login"), "state": "confirmed",
                    "finding": _finding(),
                    "evidence_refs": ["auth.py:1", "not-a-call-id"]}}])
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
    """Confirms without recorded evidence, then dismisses when rejected."""

    def __init__(self):
        self.turn = 0

    def bind_tools(self, tools):
        return self

    def invoke(self, messages):
        if self.turn == 0:
            response = AIMessage(content="", tool_calls=[{
                "name": "update_review_item", "id": "resolve-no-evidence", "args": {
                    "qname": Q("auth", "login"), "state": "confirmed",
                    "finding": _finding()}}])
        else:
            response = AIMessage(content="", tool_calls=[{
                "name": "update_review_item", "id": "dismiss-no-evidence", "args": {
                    "qname": Q("auth", "login"), "state": "dismissed",
                    "reason": "cannot verify without evidence"}}])
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
    # The evidence-less confirm was rejected; only the later dismiss took effect.
    assert item["state"] == "dismissed"
    statuses = [t["status"] for t in result["tool_trace"]]
    assert "rejected_policy" in statuses
    assert result["confirmed_findings"] == []
    assert result["findings"] == []
    assert result["review_complete"] is True


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
                "id": "call-malformed-1", "name": "get_impact",
                "args": '{"symbols": [', "error": "not valid JSON"}])
        if self.turns == 2:
            paired = [message for message in messages
                      if isinstance(message, ToolMessage)
                      and message.tool_call_id == "call-malformed-1"]
            assert paired, "the malformed tool call must be paired with a ToolMessage"
            return AIMessage(content="", tool_calls=[{
                "name": "get_impact", "id": "impact-clean",
                "args": {"symbols": [Q("auth", "login")],
                         "for_qname": Q("auth", "login")}}])
        return AIMessage(content="", tool_calls=[{
            "name": "update_review_item", "id": "confirm-clean", "args": {
                "qname": Q("auth", "login"), "state": "confirmed",
                "finding": _finding()}}])


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
    assert result["review_complete"] is True


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


def test_affected_entries_are_filled_deterministically(tmp_path):
    """affected_entries come from the graph, not from anything the model authors."""
    from code_review_ai.impact import affected_entries as graph_entries

    config = load_config(FIX)
    config.repo_path = FIX
    config.db_path = str(tmp_path / "index.db")
    conn = connect(config.db_path)
    init_schema(conn)
    rebuild(config, conn)

    result = run_review(config, conn, model=ConfirmingModel(),
                        diff="diff --git a/auth.py b/auth.py",
                        symbols=[Q("auth", "login")])

    expected = sorted(graph_entries(conn, Q("auth", "login")))
    assert result["affected_entries"] == expected


class TerminalSubmitModel:
    """The native_agent profile keeps a terminal tool; a lone call ends the run."""

    def __init__(self):
        self.turn = 0

    def bind_tools(self, tools):
        return self

    def invoke(self, messages):
        if self.turn == 0:
            response = AIMessage(content="", tool_calls=[{
                "name": "read_file", "id": "native-read",
                "args": {"path": "auth.py", "start_line": 1, "end_line": 5}}])
        else:
            response = AIMessage(content="", tool_calls=[{
                "name": "submit_review", "id": "native-submit", "args": {
                    "findings": [_finding(line=2)], "affected_symbols": [],
                    "affected_files": [], "affected_entries": [], "tests": []}}])
        self.turn += 1
        return response


def test_terminal_submit_still_ends_when_a_profile_offers_it(tmp_path):
    """Profiles without a candidate-resolving tool (native_agent) keep submit_review."""
    config = load_config(FIX)
    config.repo_path = FIX
    config.db_path = str(tmp_path / "index.db")
    conn = connect(config.db_path)
    init_schema(conn)
    rebuild(config, conn)

    result = run_review(config, conn, model=TerminalSubmitModel(),
                        diff="diff --git a/auth.py b/auth.py",
                        symbols=[Q("auth", "login")],
                        tool_names=["read_file", "search_code", "submit_review"])

    assert result["failure_reason"] is None
    # No candidate tool exists in this profile, so the report is the model's.
    assert result["findings"] == [_finding(line=2)]
    assert result["tool_calls"] == ["read_file", "submit_review"]
    assert result["tool_trace"][-1]["tool"] == "submit_review"


def test_agent_get_impact_tool_omits_affected_entries(tmp_path):
    """The review agent's get_impact carries callers/callees, not flow entries."""
    config = load_config(FIX)
    config.repo_path = FIX
    config.db_path = str(tmp_path / "index.db")
    conn = connect(config.db_path)
    init_schema(conn)
    rebuild(config, conn)
    registry = create_tool_registry(config, conn)
    tool = next(item for item in registry.action_tools()
                if item.name == "get_impact")
    output = tool.invoke({"symbols": [Q("auth", "login")]})
    assert isinstance(output, str)
    assert '"affected_entries"' not in output
    assert '"upstream"' in output
    conn.close()
