"""Schemas and state for the read-only review agent."""

from __future__ import annotations

from operator import add
from typing import Annotated, Literal, TypedDict

from langchain_core.messages import AnyMessage
from langgraph.graph.message import add_messages
from pydantic import BaseModel, ConfigDict, Field


MAX_TOOL_CALLS = 50
# In the worst case every action consumes an agent node and a tool node, then
# the graph needs one rejected request, force-submit, final agent, and finish.
GRAPH_RECURSION_LIMIT = MAX_TOOL_CALLS * 2 + 6
ToolKind = Literal["action", "terminal"]


class Finding(BaseModel):
    model_config = ConfigDict(extra="forbid")

    file: str
    line: int = Field(ge=1)
    title: str
    description: str


class FindingReport(BaseModel):
    """The only part of the final result authored by the model."""

    model_config = ConfigDict(extra="forbid")

    findings: list[Finding]
    affected_symbols: list[str] = Field(default_factory=list)
    affected_files: list[str] = Field(default_factory=list)
    affected_entries: list[str] = Field(default_factory=list)
    tests: list[str] = Field(default_factory=list)


class ReviewState(TypedDict, total=False):
    messages: Annotated[list[AnyMessage], add_messages]
    repo_path: str
    diff: str
    change_summary: dict[str, object]
    tool_call_count: Annotated[int, add]
    final_report: FindingReport | None
    failure_reason: str | None
    retry_count: Annotated[int, add]
    force_submit: bool
