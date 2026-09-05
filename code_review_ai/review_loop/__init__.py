"""Hand-rolled ReAct loop for the code-review agent (parallel to ``review_agent``).

Milestone 1 ships the bare control flow on a minimal tool contract, with no
langgraph dependency. Wiring to the index, budgets, and the evidence gate arrive
in later milestones; the old ``review_agent`` package stays authoritative until
then.
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
from code_review_ai.review_loop.schemas import (
    LoopResult,
    ToolCallStatus,
    ToolSpec,
    ToolTrace,
)

__all__ = [
    "Hooks",
    "LoopResult",
    "POINT_MODEL_REQUEST_STARTED",
    "POINT_MODEL_RESPONSE_RECEIVED",
    "POINT_POST_TOOL",
    "POINT_PRE_TOOL",
    "POINT_RUN_FINISHED",
    "ToolCallStatus",
    "ToolSpec",
    "ToolTrace",
    "run_loop",
]
