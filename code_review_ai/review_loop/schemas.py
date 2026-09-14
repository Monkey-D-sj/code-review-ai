"""Contracts for the review_loop review flow.

The model is given a policy, the change, and whatever tools the caller offers;
it researches the change and ends by calling a tool the caller marked
``terminates`` -- for a review, ``finish_review`` with its structured findings
(an empty list is a valid "no regression" verdict). The validated payload lands
in ``LoopResult.submission``; the review's ``findings`` is a view over it. The
report is the whole result -- there is no per-row bookkeeping to reconcile.

Depends only on ``langchain_core``/``pydantic`` -- no langgraph, no StructuredTool.
``ToolTrace`` mirrors ``review_agent``'s shape so trace consumers stay compatible.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Literal, TypedDict

from langchain_core.messages import BaseMessage
from pydantic import BaseModel, ConfigDict, Field


ToolCallStatus = Literal[
    "success",  # the call ran and returned usable content
    "error",    # the call did not complete: bad args, unknown tool, or failed
]


class ToolTrace(TypedDict):
    """One auditable disposition of a requested tool call.

    Records are appended in execution order, so list position is the ordinal --
    no explicit sequence number is stored. ``tool_call_id`` is the tool call's
    ``id``, which ``ToolCall`` guarantees is present.

    ``response_chars`` is the returned content's full length; ``response_excerpt``
    the content itself, truncated to the loop's configured cap. Both are kept so
    a consumer can tell "the tool returned 40k chars and here are the first 2000"
    from "the tool returned 1200 chars total".
    """

    tool_call_id: str
    tool: str
    input: object
    status: ToolCallStatus
    response_chars: int
    response_excerpt: str


Usage = dict[str, int]
"""Aggregated model-side tokens for one run (``LoopResult.usage``).

A plain ``dict[str, int]`` so it stays assignable to and from the accumulator.
Keys mirror the ``usage_metadata`` an ``AIMessage`` carries from the provider,
summed across every model turn -- typically ``input_tokens``/``output_tokens``/
``total_tokens``, plus ``cache_read`` (prompt-cache-hit input tokens, read from
``input_token_details.cache_read`` and accumulated separately because it prices
differently; cache-miss input = ``input_tokens - cache_read``). Only the keys
the provider reported are present. This is the model-side ground truth, not an
estimate of tool-output tokens (that comes later, if at all).
"""


@dataclass(frozen=True)
class ToolSpec:
    """A tool the loop can execute, without LangChain StructuredTool machinery.

    ``args_schema`` is plain pydantic (extra forbidden); ``run`` receives the
    validated keyword arguments and returns the content string verbatim.

    ``terminates`` marks a submitter: the loop never runs it, it validates the
    call against ``args_schema``, replies, stores the payload on
    ``LoopResult.submission`` and ends the run. That is the whole of what makes
    ``finish_review`` special -- the reply and the invalid-arguments path are
    what the loop does for any tool. Because the payload lands in a generic
    slot, the loop never learns the shape of any particular submission.
    """

    name: str
    description: str
    args_schema: type[BaseModel]
    run: Callable[..., str]
    terminates: bool = False


class Finding(BaseModel):
    """One concrete defect the model claims this change introduced.

    ``file``/``line`` point at where the defect surfaces, which is often **not**
    a line the diff touched: the change may have altered a contract whose other
    end lives in another module.
    """

    model_config = ConfigDict(extra="forbid")

    file: str
    line: int = Field(ge=1)
    title: str
    description: str


# The tool that submits the review's findings. Named here so the loop, the
# runner's tool list and the driver agree on one constant.
FINISH_REVIEW_TOOL = "finish_review"


class ReviewSubmission(BaseModel):
    """Payload of ``finish_review``: the review's structured findings.

    Empty ``findings`` is a valid submission (the model reviewed and found no
    concrete regression). The model owns the whole report.
    """

    model_config = ConfigDict(extra="forbid")

    findings: list[Finding] = Field(default_factory=list)


class AssistantTurn(BaseModel):
    """One model reply kept for post-hoc debugging (esp. empty turns).

    ``content`` is the visible assistant text, ``reasoning`` the provider's
    ``reasoning_content`` when present, and ``tool_calls`` the tool names that
    reply requested. Recorded for every model turn so a failed/empty run can be
    reconstructed after the fact (the empty-turn replies are otherwise dropped
    from the sent history).

    **Alignment with ``LoopResult.tool_trace``.** ``tool_calls`` carries tool
    *names*, in request order, with no tool-call id and no index into the
    trace: the two lists line up only by **position within a turn** -- the k-th
    name of turn N is the k-th trace record produced while executing turn N.

    Every call that executes produces exactly one record, in order (each of
    ``_execute_call`` and ``_apply_terminating`` ends in a single
    ``_reply_call``).
    The pairing is therefore **prefix-correct but not total**: a trace record
    always sits where its name does, and names can outnumber records only as a
    *trailing* run of never-executed calls. Walk both lists in step and stop
    when the records run out; never shift a later record onto an earlier name.

    Two break points create that tail: the token budget (``max_total_tokens`` /
    ``--max-tokens``) is checked after the turn is recorded and before any of
    its calls execute, and the loop stops at an accepted ``finish_review``,
    leaving the calls that same turn requested after it unexecuted (a turn
    asking for ``[finish_review, read_file]`` records two names and one record).

    Carrying ``tool_call_id`` would make the pairing exact rather than
    positional; ``tool_calls`` does not have it yet.
    """

    model_config = ConfigDict(extra="forbid")

    turn: int = Field(ge=1)
    content: str = ""
    reasoning: str | None = None
    tool_calls: list[str] = Field(default_factory=list)


@dataclass
class LoopResult:
    """Outcome of one review run.

    ``submission`` is the terminating tool's validated payload -- for a review,
    a ``ReviewSubmission``; empty when the run never submitted. ``findings`` is
    a view over it (see the property), so a caller that only wants the review's
    report reads the same field it always did.
    ``review_complete`` is true only when an accepted submission ended the run.
    ``usage`` aggregates the model-side tokens the provider reported across the
    run's model turns (see ``Usage``); it survives a partial run so a truncated
    review still shows what was spent. ``cost`` is the yuan estimate
    ``compute_cost`` derives from ``usage``; the runner fills it (the loop
    itself does not price) and it stays 0 until then.

    ``messages`` is the run's whole history as sent -- system, the diff, every
    assistant turn and every tool reply in full. It exists for the harness-skill
    review, which replays the parent run verbatim; the trace and the assistant
    turns cannot serve that (the trace truncates tool output at
    ``TRACE_RESPONSE_EXCERPT_CHARS`` and the turns carry tool *names*, not the
    calls). ``skill_review`` is that second run's own result, filled by the
    runner, so its cost can be reported separately from the review's.
    """

    submission: BaseModel | None = None
    review_complete: bool = False
    failure_reason: str | None = None
    usage: Usage = field(default_factory=dict)
    cost: float = 0.0
    assistant_turns: list[AssistantTurn] = field(default_factory=list)
    tool_trace: list[ToolTrace] = field(default_factory=list)
    tool_calls: list[str] = field(default_factory=list)
    tool_call_count: int = 0
    tool_request_count: int = 0
    messages: list[BaseMessage] = field(default_factory=list)
    skill_review: "LoopResult | None" = None

    @property
    def findings(self) -> list[Finding]:
        """The review's report, read off whatever the submitter handed in.

        A property rather than a field because the loop stores submissions
        generically (``submission``) and must not know that one of them happens
        to carry findings. A run that never submitted, or one that submitted
        something else, has no findings -- both are empty, not an error.
        """
        payload = self.submission
        return list(payload.findings) if isinstance(payload, ReviewSubmission) else []
