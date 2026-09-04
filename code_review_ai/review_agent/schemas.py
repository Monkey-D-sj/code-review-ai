"""Schemas and state for the read-only review agent."""

from __future__ import annotations

from operator import add
from typing import Annotated, Literal, TypedDict

from langchain_core.messages import AnyMessage
from langgraph.graph.message import add_messages
from pydantic import BaseModel, ConfigDict, Field, model_validator


MAX_TOOL_CALLS = 50
# In the worst case every action consumes an agent node and a tool node, then
# the graph needs one rejected request, force-submit, final agent, and finish.
GRAPH_RECURSION_LIMIT = MAX_TOOL_CALLS * 2 + 6
ToolKind = Literal["action", "terminal"]
ToolCallStatus = Literal[
    "executed",
    "rejected_policy",
    "rejected_protocol",
    "error",
]


class ToolTrace(TypedDict):
    """One auditable disposition of a model-requested tool call."""

    sequence: int
    tool_call_id: str
    tool: str
    input: object
    status: ToolCallStatus
    response_chars: int


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


FindingState = Literal["candidate", "confirmed", "dismissed"]


class ReviewItem(BaseModel):
    """One system-created changed-symbol review item."""

    model_config = ConfigDict(extra="forbid")

    qname: str
    file: str | None = None
    start_line: int | None = None
    end_line: int | None = None
    state: FindingState = "candidate"
    finding: Finding | None = None
    evidence_refs: list[str] = Field(default_factory=list)
    reason: str | None = None


class ReviewItemUpdate(BaseModel):
    """The model may only resolve a system-created candidate item."""

    model_config = ConfigDict(extra="forbid")

    qname: str = Field(min_length=1)
    state: Literal["confirmed", "dismissed"]
    finding: Finding | None = None
    evidence_refs: list[str] = Field(
        default_factory=list,
        description=("Optional. Leave empty: evidence is recorded automatically "
                     "when read_file/search_code/get_impact is called with "
                     "for_qname. Ignored when not a real executed tool call."))
    reason: str | None = None

    @model_validator(mode="after")
    def _validate_resolution(self) -> "ReviewItemUpdate":
        if self.state == "confirmed" and self.finding is None:
            raise ValueError("confirmed review items require a finding")
        if self.state == "dismissed" and not (self.reason and self.reason.strip()):
            raise ValueError("dismissed review items require a reason")
        return self


class ReviewState(TypedDict, total=False):
    messages: Annotated[list[AnyMessage], add_messages]
    # Immutable system + initial review request. These form a cache-friendly
    # prompt prefix and remain available after historical tool messages age out.
    base_messages: list[AnyMessage]
    repo_path: str
    diff: str
    change_summary: dict[str, object]
    # Every requested action consumes this budget, including a policy denial,
    # so an uncooperative model cannot loop around the execution counter.
    tool_request_count: Annotated[int, add]
    tool_call_count: Annotated[int, add]
    # Unlike messages, this is an auditable account of what happened to each
    # requested tool call. A rejected request still gets a ToolMessage (the
    # provider protocol requires it), but is never represented as executed.
    tool_trace: Annotated[list[ToolTrace], add]
    review_items: dict[str, ReviewItem]
    final_report: FindingReport | None
    failure_reason: str | None
    retry_count: Annotated[int, add]
