"""Tests for the review command's JSON payload contract (review_loop.payload)."""

from __future__ import annotations

from code_review_ai.review_loop.payload import loop_result_payload
from code_review_ai.review_loop.schemas import AssistantTurn, LoopResult
from code_review_ai.review_loop.skill_review import SkillSubmission


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


def test_payload_reports_no_retrospective_by_default():
    payload = loop_result_payload(LoopResult(), None)

    assert payload["skill_review"] is None


def test_payload_reports_the_retrospective_apart_from_the_review():
    """Two runs, two costs.

    The retrospective's input is the review's entire history, so its tokens are
    the same order of magnitude as one more turn of the review. Folding them
    into the review's usage would make the one comparison this payload exists to
    serve -- what a configuration costs -- unreadable.
    """
    inner = LoopResult(
        submission=SkillSubmission(skill="改好的全文",
                                   changes=["删掉了「研究完成后」"]),
        review_complete=True,
        usage={"input_tokens": 50_000, "output_tokens": 900, "cache_read": 45_000},
        cost=0.0125,
        assistant_turns=[AssistantTurn(turn=1, content="", tool_calls=["read_file"])],
        tool_trace=[{"tool_call_id": "read-1", "tool": "read_file", "input": {},
                     "status": "success", "response_chars": 10,
                     "response_excerpt": "1: code"}],
    )

    payload = loop_result_payload(LoopResult(skill_review=inner), "m")

    block = payload["skill_review"]
    assert block["chars"] == len("改好的全文")
    # The candidate file holds only the text, so this account survives here or
    # nowhere.
    assert block["changes"] == ["删掉了「研究完成后」"]
    assert block["review_complete"] is True
    assert block["turn_count"] == 1
    assert block["tool_calls"] == ["read_file"]
    assert block["cost"] == 0.0125
    assert block["usage"] == {"input_tokens": 50_000, "output_tokens": 900,
                              "cache_read_input_tokens": 45_000}
