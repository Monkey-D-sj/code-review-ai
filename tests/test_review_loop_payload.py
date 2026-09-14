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


def test_payload_reports_no_change_summary_by_default():
    """0 is the baseline: this run was handed the diff and nothing else. An
    A/B over the summary is only readable if each payload says which arm of it
    produced the numbers."""
    payload = loop_result_payload(LoopResult(), None)

    assert payload["change_summary_chars"] == 0


def test_payload_reports_the_change_summary_size():
    payload = loop_result_payload(LoopResult(), None, summary="CHANGED: m::UserModel")

    assert payload["change_summary_chars"] == len("CHANGED: m::UserModel")
