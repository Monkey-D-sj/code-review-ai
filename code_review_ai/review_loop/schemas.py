"""Contracts for the review_loop worksheet flow.

Follows ``review_agent`` on master: a deterministic worksheet is built from the
change summary (one candidate row per changed symbol), the model only updates
rows via ``update_review_item`` (confirmed with a finding, or dismissed with a
reason), and the run ends once every candidate is resolved. There is no free-form
report -- the structured rows are the result.

Depends only on ``langchain_core``/``pydantic`` -- no langgraph, no StructuredTool.
``ToolTrace`` mirrors ``review_agent``'s shape so trace consumers stay compatible.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Literal, TypedDict

from pydantic import BaseModel, ConfigDict, Field, model_validator


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


class Usage(TypedDict, total=False):
    """Aggregated model-side tokens for one run.

    ``total=False``: only the keys the provider actually reports are present
    (a turn that omits ``usage_metadata`` contributes nothing). Fields mirror
    the ``usage_metadata`` an ``AIMessage`` carries from the provider, summed
    across every model turn -- this is the model-side ground truth, not an
    estimate of tool-output tokens (that comes later, if at all).
    """

    input_tokens: int
    output_tokens: int
    total_tokens: int


@dataclass(frozen=True)
class ToolSpec:
    """A tool the loop can execute, without LangChain StructuredTool machinery.

    ``args_schema`` is plain pydantic (extra forbidden); ``run`` receives the
    validated keyword arguments and returns the content string verbatim. The
    worksheet updater (``update_review_item``) is schema-only: the loop applies
    it to the candidate rows itself and never calls its ``run``.
    """

    name: str
    description: str
    args_schema: type[BaseModel]
    run: Callable[..., str]


class Finding(BaseModel):
    """One confirmed defect, authored by the model inside a confirmed row."""

    model_config = ConfigDict(extra="forbid")

    file: str
    line: int = Field(ge=1)
    title: str
    description: str


FindingState = Literal["candidate", "confirmed", "dismissed"]


class ReviewItem(BaseModel):
    """One deterministic worksheet row, created from the change summary."""

    model_config = ConfigDict(extra="forbid")

    qname: str
    file: str | None = None
    start_line: int | None = None
    end_line: int | None = None
    state: FindingState = "candidate"
    finding: Finding | None = None
    reason: str | None = None


# The tool that flips candidate rows. Named here so the loop and the runner's
# tool list agree on one constant.
UPDATE_REVIEW_TOOL = "update_review_item"


class ReviewItemUpdate(BaseModel):
    """Arguments for ``update_review_item``: resolve one candidate row."""

    model_config = ConfigDict(extra="forbid")

    qname: str = Field(min_length=1)
    state: Literal["confirmed", "dismissed"]
    finding: Finding | None = None
    reason: str | None = None

    @model_validator(mode="after")
    def _validate_resolution(self) -> "ReviewItemUpdate":
        if self.state == "confirmed" and self.finding is None:
            raise ValueError("confirmed rows require a finding")
        if self.state == "dismissed" and not (self.reason and self.reason.strip()):
            raise ValueError("dismissed rows require a reason")
        return self


@dataclass
class LoopResult:
    """Outcome of one worksheet review run.

    ``items`` holds the final state of every candidate row (resolved when the
    run completed), ``findings`` the confirmed rows' findings, and
    ``affected_entries`` the deterministic entry points the runner computes.
    ``review_complete`` is true only when every candidate was resolved.
    ``usage`` aggregates the model-side tokens the provider reported across the
    run's model turns (see ``Usage``); it survives a partial run so a truncated
    review still shows what was spent.
    """

    items: dict[str, ReviewItem] = field(default_factory=dict)
    findings: list[Finding] = field(default_factory=list)
    affected_entries: list[str] = field(default_factory=list)
    review_complete: bool = False
    failure_reason: str | None = None
    usage: Usage = field(default_factory=dict)
    tool_trace: list[ToolTrace] = field(default_factory=list)
    tool_calls: list[str] = field(default_factory=list)
    tool_call_count: int = 0
    tool_request_count: int = 0
