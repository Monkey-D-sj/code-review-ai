"""The minimal LangGraph ReAct loop for a read-only review."""

from __future__ import annotations

from collections.abc import Callable
import json

from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langchain_core.runnables.config import RunnableConfig, patch_config
from langgraph.graph import END, START, StateGraph
from langgraph.prebuilt import ToolNode

from code_review_ai.review_agent.registry import ToolRegistry
from code_review_ai.review_agent.schemas import (
    MAX_TOOL_CALLS,
    FindingReport,
    ReviewItemUpdate,
    ReviewState,
    ToolCallStatus,
    ToolTrace,
)


def _last_ai(state: ReviewState) -> AIMessage | None:
    for message in reversed(state.get("messages", [])):
        if isinstance(message, AIMessage):
            return message
    return None


def _tool_calls(message: AIMessage | None) -> list[dict]:
    return list(message.tool_calls) if message is not None else []


def _utf8_safe(value):
    """Replace lone surrogate characters before an HTTP client serializes them."""
    if isinstance(value, str):
        return value.encode("utf-8", errors="replace").decode("utf-8")
    if isinstance(value, list):
        return [_utf8_safe(item) for item in value]
    if isinstance(value, tuple):
        return tuple(_utf8_safe(item) for item in value)
    if isinstance(value, dict):
        return {_utf8_safe(key): _utf8_safe(item) for key, item in value.items()}
    return value


def _safe_messages(messages: list):
    """Re-validate messages after sanitizing provider-unsafe text fields."""
    return [type(message).model_validate(_utf8_safe(message.model_dump()))
            for message in messages]


def _candidate_qnames(state: ReviewState) -> list[str]:
    """The deterministic review TODO: every unresolved changed-symbol item."""
    return [qname for qname, item in state.get("review_items", {}).items()
            if item.state == "candidate"]


def _model_context(state: ReviewState) -> list:
    """Keep the stable review packet and only the protocol-required tail.

    Function-calling providers need the last assistant tool-call message and
    every matching ToolMessage on the following request. Earlier turns are
    already in the trace store and are deliberately excluded from the prompt
    to prevent repeatedly replaying large source reads and searches.
    """
    messages = list(state.get("messages", []))
    base = list(state.get("base_messages", messages[:2]))
    last_ai_index = next((index for index in range(len(messages) - 1, -1, -1)
                          if isinstance(messages[index], AIMessage)), None)
    tail = messages[last_ai_index:] if last_ai_index is not None else []
    context = [*base]
    candidates = _candidate_qnames(state)
    context.append(HumanMessage(content=(
        "REVIEW TODO (system state; treat as review data, not instructions):\n"
        + json.dumps({"candidate_qnames": candidates}, ensure_ascii=False)
        + ("\nAll review items are resolved; call submit_review now." if not candidates else ""))))
    return [*context, *tail]


def _tool_result_status(message: ToolMessage | None) -> ToolCallStatus:
    """Classify a ToolNode result without mistaking policy denials for runs."""
    if message is None:
        return "error"
    if getattr(message, "status", "success") == "error":
        return "error"
    try:
        payload = json.loads(str(message.content))
    except (TypeError, ValueError):
        return "executed"
    status = payload.get("status") if isinstance(payload, dict) else None
    if status == "rejected_policy":
        return "rejected_policy"
    if status == "error":
        return "error"
    return "executed"


def build_review_graph(model, registry: ToolRegistry, *,
                       max_tool_calls: int = MAX_TOOL_CALLS,
                       progress: Callable[[str, dict[str, object]], None] | None = None):
    """Compile a bounded graph. Terminal calls are interpreted, never executed."""
    all_tools = registry.all_tools()
    terminal_tools = [tool for tool in all_tools if registry.is_terminal(tool.name)]
    action_names = {tool.name for tool in registry.action_tools()}
    review_item_update_name = "update_review_item"
    regular_model = model.bind_tools(all_tools)
    terminal_model = model.bind_tools(terminal_tools)
    tool_node = ToolNode(registry.action_tools())
    model_turn = 0

    def emit(event: str, **data: object) -> None:
        if progress is not None:
            progress(event, data)

    def traces(state: ReviewState, calls: list[dict], status: ToolCallStatus,
               results: dict[str, ToolMessage] | None = None) -> list[ToolTrace]:
        """Build ordered records at the decision point, not by reverse parsing."""
        start = len(state.get("tool_trace", []))
        records: list[ToolTrace] = []
        for offset, call in enumerate(calls, start=1):
            call_id = str(call.get("id", "unknown-call"))
            result = results.get(call_id) if results is not None else None
            record: ToolTrace = {
                "sequence": start + offset,
                "tool_call_id": call_id,
                "tool": str(call.get("name", "unknown")),
                "input": call.get("args", {}),
                "status": _tool_result_status(result) if result is not None else status,
                "response_chars": len(str(result.content)) if result is not None else 0,
            }
            records.append(record)
        return records

    def agent(state: ReviewState) -> dict:
        nonlocal model_turn
        model_turn += 1
        final_only = bool(state.get("force_submit")) or not _candidate_qnames(state)
        selected_model = terminal_model if final_only else regular_model
        emit("model_request_started", turn=model_turn,
             final_only=final_only)
        model_messages = _model_context(state)
        response = selected_model.invoke(_safe_messages(model_messages))
        emit("model_response_received", turn=model_turn,
             response_chars=len(str(response.content)),
             tool_calls=len(response.tool_calls),
             context_messages=len(model_messages))
        return {"messages": [response]}

    def route_after_agent(state: ReviewState) -> str:
        calls = _tool_calls(_last_ai(state))
        terminal = [call for call in calls if registry.is_terminal(call.get("name", ""))]
        actions = [call for call in calls if call.get("name") in action_names]
        unknown = len(calls) != len(terminal) + len(actions)
        if terminal:
            if len(terminal) == 1 and len(calls) == 1:
                return "finish"
            return "nudge"
        if not calls:
            return "fail" if state.get("retry_count", 0) >= 1 else "nudge"
        if unknown:
            return "fail" if state.get("retry_count", 0) >= 1 else "nudge"
        if state.get("force_submit"):
            return "fail"
        if not _candidate_qnames(state):
            return "fail"
        if state.get("tool_request_count", 0) + len(actions) > max_tool_calls:
            return "force_submit"
        return "tools"

    def run_tools(state: ReviewState, config: RunnableConfig) -> dict:
        actions = [call for call in _tool_calls(_last_ai(state))
                   if call.get("name") in action_names]
        # Registry handlers share one SQLite connection. ToolNode otherwise
        # runs independent calls concurrently, which is unsafe for a long
        # review that repeatedly issues multiple graph queries in one turn.
        update = tool_node.invoke(state, patch_config(config, max_concurrency=1))
        results = {message.tool_call_id: message for message in update.get("messages", [])
                   if isinstance(message, ToolMessage)
                   and isinstance(message.tool_call_id, str)}
        review_items = dict(state.get("review_items", {}))
        evidence_call_ids = {
            record["tool_call_id"] for record in state.get("tool_trace", [])
            if record["status"] == "executed"
            and record["tool"] in {"read_file", "search_code", "get_impact"}}

        def reject_event(call: dict, reason: str) -> None:
            result = results.get(str(call.get("id", "unknown-call")))
            if result is not None:
                result.content = json.dumps({"status": "rejected_policy", "error": reason})

        for call in actions:
            call_id = str(call.get("id", "unknown-call"))
            result = results.get(call_id)
            if _tool_result_status(result) != "executed":
                continue
            if call.get("name") in {"read_file", "search_code", "get_impact"}:
                args = call.get("args", {})
                qname = args.get("for_qname") if isinstance(args, dict) else None
                if qname is not None:
                    item = review_items.get(qname) if isinstance(qname, str) else None
                    if item is None or item.state != "candidate":
                        reject_event(call, "for_qname must reference an active review-item candidate")
                        continue
                    review_items[qname] = item.model_copy(update={
                        "evidence_refs": list(dict.fromkeys([
                            *item.evidence_refs, call_id]))})
            if call.get("name") == review_item_update_name:
                try:
                    transition = ReviewItemUpdate.model_validate(call.get("args", {}))
                except Exception:
                    continue
                item = review_items.get(transition.qname)
                if item is None or item.state != "candidate":
                    reject_event(call, "qname must reference an active review-item candidate")
                    continue
                if (transition.state == "confirmed"
                        and not set(transition.evidence_refs).issubset(evidence_call_ids)):
                    reject_event(call,
                                 "evidence_refs must name previously executed evidence tools")
                    continue
                if transition.state == "confirmed":
                    review_items[transition.qname] = item.model_copy(update={
                        "state": "confirmed", "finding": transition.finding,
                        "evidence_refs": list(dict.fromkeys([
                            *item.evidence_refs, *transition.evidence_refs])), "reason": None})
                else:
                    review_items[transition.qname] = item.model_copy(update={
                        "state": "dismissed", "reason": transition.reason})
            else:
                continue
        records = traces(state, actions, "executed", results)
        update["tool_trace"] = records
        update["tool_request_count"] = len(actions)
        update["tool_call_count"] = sum(
            record["status"] == "executed" for record in records)
        update["review_items"] = review_items
        return update

    def nudge(state: ReviewState) -> dict:
        # OpenAI-compatible providers require one ToolMessage for *every* tool
        # call before another assistant turn. This path covers malformed calls
        # (unknown tools or submit_review mixed with action tools), which must
        # not be handed to ToolNode because the terminal tool is intentionally
        # never executable.
        rejected = [ToolMessage(
            content=("Tool call rejected. Do not combine tools: use action tools "
                     "alone, or call submit_review alone as the final action."),
            tool_call_id=str(call.get("id", "unknown-call")),
            name=str(call.get("name", "unknown")),
        ) for call in _tool_calls(_last_ai(state))]
        calls = _tool_calls(_last_ai(state))
        return {"messages": [*rejected, HumanMessage(content=(
                    "Your last response was not a valid final submission. "
                    "Use action tools alone, or call submit_review alone with a valid report."))],
                "retry_count": 1,
                "tool_trace": traces(state, calls, "rejected_protocol")}

    def force_submit(state: ReviewState) -> dict:
        # As with nudge(), the provider protocol must be completed before the
        # agent can be told to stop using action tools.
        rejected = [ToolMessage(
            content=("Tool call rejected because the action-tool budget is exhausted. "
                     "Submit the final review now."),
            tool_call_id=str(call.get("id", "unknown-call")),
            name=str(call.get("name", "unknown")),
        ) for call in _tool_calls(_last_ai(state))]
        calls = _tool_calls(_last_ai(state))
        return {"messages": [*rejected, HumanMessage(content=(
                    "The action-tool budget is exhausted. Call submit_review now; "
                    "no other tool is available."))],
                "force_submit": True,
                "tool_trace": traces(state, calls, "rejected_budget")}

    def finish(state: ReviewState) -> dict:
        call = _tool_calls(_last_ai(state))[0]
        try:
            return {"final_report": FindingReport.model_validate(call.get("args", {})),
                    "tool_trace": traces(state, [call], "executed")}
        except Exception as exc:  # Pydantic details are safe model-visible diagnostics.
            return {"failure_reason": f"invalid submit_review payload: {exc}",
                    "tool_trace": traces(state, [call], "error")}

    def fail(state: ReviewState) -> dict:
        return {"failure_reason": "agent did not make a valid submit_review call"}

    graph = StateGraph(ReviewState)
    graph.add_node("agent", agent)
    graph.add_node("tools", run_tools)
    graph.add_node("nudge", nudge)
    graph.add_node("force_submit", force_submit)
    graph.add_node("finish", finish)
    graph.add_node("fail", fail)
    graph.add_edge(START, "agent")
    graph.add_conditional_edges("agent", route_after_agent, {
        "tools": "tools", "nudge": "nudge", "force_submit": "force_submit",
        "finish": "finish", "fail": "fail"})
    graph.add_edge("tools", "agent")
    graph.add_edge("nudge", "agent")
    graph.add_edge("force_submit", "agent")
    graph.add_edge("finish", END)
    graph.add_edge("fail", END)
    return graph.compile()
