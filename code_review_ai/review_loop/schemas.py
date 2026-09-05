"""Minimal contracts for the hand-rolled review loop.

Lives in a package that depends only on ``langchain_core`` -- no langgraph, no
StructuredTool. ``ToolTrace`` mirrors ``review_agent``'s shape so trace consumers
stay compatible, and the status vocabulary is single-sourced here instead of
being rebuilt across two layers. There is no structured report model: the loop
ends on the first model turn with no tool calls and returns that turn's text,
which a caller parses.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Literal, TypedDict

from pydantic import BaseModel


ToolCallStatus = Literal[
    "success",  # the call ran and returned usable content
    "error",    # the call did not complete: bad args, unknown tool, or failed
]


class ToolTrace(TypedDict):
    """One auditable disposition of a requested tool call.

    Records are appended in execution order, so list position is the ordinal --
    no explicit sequence number is stored. ``tool_call_id`` is the tool call's
    ``id``, which ``ToolCall`` guarantees is present.
    """

    tool_call_id: str
    tool: str
    input: object
    status: ToolCallStatus
    response_chars: int


@dataclass(frozen=True)
class ToolSpec:
    """A tool the loop can execute, without LangChain StructuredTool machinery.

    ``args_schema`` is plain pydantic (extra forbidden); ``run`` receives the
    validated keyword arguments and returns the content string verbatim. There is
    no terminal/submit tool: the loop stops when a model turn requests no tools,
    and the caller parses that turn's text into a structured report.
    """

    name: str
    description: str
    args_schema: type[BaseModel]
    run: Callable[..., str]


@dataclass
class LoopResult:
    """Outcome of one review loop run.

    ``final_text`` is the text of the model turn that ended the run (no tool
    calls); a caller parses it into a structured report. Parity fields such as
    ``files_read``/``usage`` land on wiring.
    """

    final_text: str | None = None
    failure_reason: str | None = None
    tool_trace: list[ToolTrace] = field(default_factory=list)
    tool_calls: list[str] = field(default_factory=list)
    tool_call_count: int = 0
    tool_request_count: int = 0
