"""Hand-rolled ReAct loop for the code-review agent.

Ships the control flow on a minimal tool contract with no langgraph dependency.
``runner.run_review`` wires it to a repo: a policy, the change, and a tool set
chosen by the caller, plus turn/token budgets. The model researches and reports
through ``finish_review``. The CLI ``review`` command and the eval harness's
``review_loop`` agent adapter both drive this package.
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
from code_review_ai.review_loop.loop import run_loop
from code_review_ai.review_loop.payload import loop_result_payload
from code_review_ai.review_loop.pricing import compute_cost
from code_review_ai.review_loop.schemas import (
    FINISH_REVIEW_TOOL,
    AssistantTurn,
    Finding,
    LoopResult,
    ReviewSubmission,
    ToolCallStatus,
    ToolSpec,
    ToolTrace,
    Usage,
)

__all__ = [
    "FINISH_REVIEW_TOOL",
    "AssistantTurn",
    "Finding",
    "Hooks",
    "LoopResult",
    "POINT_MODEL_REQUEST_STARTED",
    "POINT_MODEL_RESPONSE_RECEIVED",
    "POINT_POST_TOOL",
    "POINT_PRE_TOOL",
    "POINT_RUN_FINISHED",
    "ReviewSubmission",
    "ToolCallStatus",
    "ToolSpec",
    "ToolTrace",
    "Usage",
    "compute_cost",
    "loop_result_payload",
    "run_loop",
]
