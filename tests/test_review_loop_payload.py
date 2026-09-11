"""Tests for the review command's JSON payload contract (review_loop.payload)."""

from __future__ import annotations

from code_review_ai.review_loop.payload import loop_result_payload
from code_review_ai.review_loop.schemas import AssistantTurn, LoopResult


def test_payload_carries_every_assistant_turn():
    """The per-turn reasoning is the only way an out-of-process consumer (the
    SkillOpt optimizer) can see *why* the agent missed a defect, rather than
    only that it missed one."""
    result = LoopResult(assistant_turns=[
        AssistantTurn(turn=1, content="", reasoning="check callers first",
                      tool_calls=["read_file"]),
        AssistantTurn(turn=2, content="no regression", reasoning=None, tool_calls=[]),
    ])

    payload = loop_result_payload(result, "fake-model")

    assert payload["assistant_turns"] == [
        {"turn": 1, "content": "", "reasoning": "check callers first",
         "tool_calls": ["read_file"]},
        {"turn": 2, "content": "no regression", "reasoning": None,
         "tool_calls": []},
    ]


def test_payload_assistant_turns_is_empty_without_turns():
    payload = loop_result_payload(LoopResult(), None)

    assert payload["assistant_turns"] == []
