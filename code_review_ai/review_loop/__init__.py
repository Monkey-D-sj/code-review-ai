"""Hand-rolled ReAct loop for the code-review agent.

Ships the control flow on a minimal tool contract with no langgraph dependency.
``runner.run_review`` wires it to the index: a deterministic worksheet built from
the change summary, per-row resolution rules (confirmed needs a finding,
dismissed needs a reason), and turn/token budgets. The CLI ``review`` command and
the eval harness's ``review_loop`` agent adapter both drive this package.
"""

from __future__ import annotations

from code_review_ai.review_loop.hooks import (
    Hooks,
    POINT_MODEL_REQUEST_STARTED,
    POINT_MODEL_RESPONSE_RECEIVED,
    POINT_POST_TOOL,
    POINT_PRE_TOOL,
    POINT_RUN_FINISHED,
)
from code_review_ai.review_loop.loop import run_free_loop, run_loop
from code_review_ai.review_loop.pricing import compute_cost
from code_review_ai.review_loop.schemas import (
    FINISH_REVIEW_TOOL,
    AssistantTurn,
    Finding,
    FindingState,
    LoopResult,
    ReviewItem,
    ReviewItemUpdate,
    ReviewSubmission,
    ToolCallStatus,
    ToolSpec,
    ToolTrace,
    UPDATE_REVIEW_TOOL,
    Usage,
)

__all__ = [
    "FINISH_REVIEW_TOOL",
    "AssistantTurn",
    "Finding",
    "FindingState",
    "Hooks",
    "LoopResult",
    "POINT_MODEL_REQUEST_STARTED",
    "POINT_MODEL_RESPONSE_RECEIVED",
    "POINT_POST_TOOL",
    "POINT_PRE_TOOL",
    "POINT_RUN_FINISHED",
    "ReviewItem",
    "ReviewItemUpdate",
    "ReviewSubmission",
    "ToolCallStatus",
    "ToolSpec",
    "ToolTrace",
    "UPDATE_REVIEW_TOOL",
    "Usage",
    "compute_cost",
    "run_free_loop",
    "run_loop",
]
