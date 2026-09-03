"""The minimal LangGraph ReAct loop for a read-only review."""

from __future__ import annotations

from collections.abc import Callable

from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langchain_core.runnables.config import RunnableConfig, patch_config
from langgraph.graph import END, START, StateGraph
from langgraph.prebuilt import ToolNode

from code_review_ai.review_agent.registry import ToolRegistry
from code_review_ai.review_agent.schemas import MAX_TOOL_CALLS, FindingReport, ReviewState


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


def build_review_graph(model, registry: ToolRegistry, *,
                       max_tool_calls: int = MAX_TOOL_CALLS,
                       progress: Callable[[str, dict[str, object]], None] | None = None):
    """Compile a bounded graph. Terminal calls are interpreted, never executed."""
    all_tools = registry.all_tools()
    terminal_tools = [tool for tool in all_tools if registry.is_terminal(tool.name)]
    action_names = {tool.name for tool in registry.action_tools()}
    regular_model = model.bind_tools(all_tools)
    terminal_model = model.bind_tools(terminal_tools)
    tool_node = ToolNode(registry.action_tools())
    model_turn = 0

    def emit(event: str, **data: object) -> None:
        if progress is not None:
            progress(event, data)

    def agent(state: ReviewState) -> dict:
        nonlocal model_turn
        model_turn += 1
        selected_model = terminal_model if state.get("force_submit") else regular_model
        emit("model_request_started", turn=model_turn,
             final_only=bool(state.get("force_submit")))
        response = selected_model.invoke(_safe_messages(state["messages"]))
        emit("model_response_received", turn=model_turn,
             response_chars=len(str(response.content)),
             tool_calls=len(response.tool_calls))
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
        if state.get("tool_call_count", 0) + len(actions) > max_tool_calls:
            return "force_submit"
        return "tools"

    def run_tools(state: ReviewState, config: RunnableConfig) -> dict:
        actions = [call for call in _tool_calls(_last_ai(state))
                   if call.get("name") in action_names]
        # Registry handlers share one SQLite connection. ToolNode otherwise
        # runs independent calls concurrently, which is unsafe for a long
        # review that repeatedly issues multiple graph queries in one turn.
        update = tool_node.invoke(state, patch_config(config, max_concurrency=1))
        update["tool_call_count"] = len(actions)
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
        return {"messages": [*rejected, HumanMessage(content=(
                    "Your last response was not a valid final submission. "
                    "Use action tools alone, or call submit_review alone with a valid report."))],
                "retry_count": 1}

    def force_submit(state: ReviewState) -> dict:
        # As with nudge(), the provider protocol must be completed before the
        # agent can be told to stop using action tools.
        rejected = [ToolMessage(
            content=("Tool call rejected because the action-tool budget is exhausted. "
                     "Submit the final review now."),
            tool_call_id=str(call.get("id", "unknown-call")),
            name=str(call.get("name", "unknown")),
        ) for call in _tool_calls(_last_ai(state))]
        return {"messages": [*rejected, HumanMessage(content=(
                    "The action-tool budget is exhausted. Call submit_review now; "
                    "no other tool is available."))], "force_submit": True}

    def finish(state: ReviewState) -> dict:
        call = _tool_calls(_last_ai(state))[0]
        try:
            return {"final_report": FindingReport.model_validate(call.get("args", {}))}
        except Exception as exc:  # Pydantic details are safe model-visible diagnostics.
            return {"failure_reason": f"invalid submit_review payload: {exc}"}

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
