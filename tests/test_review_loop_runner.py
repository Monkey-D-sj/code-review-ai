"""End-to-end driver test: prompt + diff + tools -> findings out.

The driver's one job is to assemble the request (policy, prompt, diff), offer
the caller's tool set, and price the run. Which tools exist is the only thing
that distinguishes the two arms, so the arms are tested as two tool lists
through the same entry point.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
from langchain_core.messages import (
    AIMessage,
    HumanMessage,
    SystemMessage,
    ToolMessage,
)

from code_review_ai.config import load_config
from code_review_ai.db import init_schema
from code_review_ai.review_loop import Hooks, SkillReview
from code_review_ai.review_loop.hooks import POINT_RUN_FINISHED
from code_review_ai.review_loop.runner import (
    NOINDEX_TOOLS,
    _POLICY,
    build_initial_messages,
    run_review,
)
from code_review_ai.review_loop.schemas import FINISH_REVIEW_TOOL
from code_review_ai.review_loop.skill_review import (
    SKILL_REVIEW_INSTRUCTION,
    submission_changes,
)

FINDING = {"file": "app.py", "line": 2, "title": "leaks None on empty user",
           "description": "login returns None for an empty user."}


class ScriptedReviewModel:
    """Turn 1 reads app.py; turn 2 submits one finding over finish_review."""

    def __init__(self):
        self.saw_system = False
        self.system_content = ""
        self.saw_tool_reply = False
        self.schemas: list[str] = []

    def bind_tools(self, schemas):
        self.schemas = [schema["name"] for schema in schemas]
        return self

    def invoke(self, messages):
        for message in messages:
            if isinstance(message, SystemMessage):
                self.saw_system = True
                self.system_content = message.content
            if isinstance(message, ToolMessage):
                self.saw_tool_reply = True
        turn = len([m for m in messages if m.type == "ai"])
        if turn == 0:
            return AIMessage(content="", tool_calls=[
                {"name": "read_file", "args": {"path": "app.py",
                                               "start_line": 1, "end_line": 3},
                 "id": "read-1"}])
        return AIMessage(content="", tool_calls=[
            {"name": FINISH_REVIEW_TOOL, "args": {"findings": [FINDING]},
             "id": "submit-1"}])


@pytest.fixture()
def env(tmp_path):
    (tmp_path / "app.py").write_text("def login(user):\n"
                                     "    return user or None\n", encoding="utf-8")
    config = load_config(repo_path=str(tmp_path))
    config.repo_path = str(tmp_path)
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    init_schema(conn)
    return config, conn


class CostReportingModel(ScriptedReviewModel):
    """Like ScriptedReviewModel, but the submit turn reports provider usage."""

    def invoke(self, messages):
        response = super().invoke(messages)
        if any(call["name"] == FINISH_REVIEW_TOOL
               for call in getattr(response, "tool_calls", [])):
            response.usage_metadata = {
                "input_tokens": 1_000_000, "output_tokens": 1_000_000,
                "total_tokens": 2_000_000}
        return response


def test_run_review_reports_yuan_cost_from_usage(env):
    config, conn = env

    result = run_review(config, conn, prompt="check auth for regressions",
                        diff="DIFF-BODY", model=CostReportingModel(), max_turns=5)

    assert result.review_complete is True
    # the submit turn priced: 1M miss input (1.5) + 1M output (4.5) = 6.0 元
    assert result.usage["total_tokens"] == 2_000_000
    assert result.cost == pytest.approx(6.0)


def test_run_review_offers_the_graph_tools_and_reports_findings(env):
    config, conn = env
    model = ScriptedReviewModel()
    hooks = Hooks()
    finished: dict = {}

    hooks.on(POINT_RUN_FINISHED, lambda _event, context: finished.update(context))
    result = run_review(config, conn, prompt="check auth for regressions",
                        diff="DIFF-BODY", model=model, hooks=hooks, max_turns=5)

    assert result.failure_reason is None
    assert result.review_complete is True
    assert [finding.title for finding in result.findings] == ["leaks None on empty user"]
    assert [finding.file for finding in result.findings] == ["app.py"]
    assert result.tool_calls == ["read_file", FINISH_REVIEW_TOOL]
    assert result.tool_call_count == 2
    assert result.tool_request_count == 2
    assert [record["status"] for record in result.tool_trace] == ["success", "success"]
    assert model.saw_system and model.saw_tool_reply
    assert {"read_file", "get_impact", FINISH_REVIEW_TOOL} <= set(model.schemas)
    assert finished["failure_reason"] is None
    assert finished["finding_count"] == 1


def test_run_review_needs_no_index_for_the_narrowed_tool_set(env):
    """The no-index arm: the same diff, read/search only, no graph and no conn."""
    config, _conn = env
    model = ScriptedReviewModel()

    result = run_review(config, prompt="review this diff", diff="DIFF-BODY",
                        tool_names=list(NOINDEX_TOOLS), model=model, max_turns=5)

    assert result.failure_reason is None
    assert result.review_complete is True
    assert [finding.file for finding in result.findings] == ["app.py"]
    assert set(model.schemas) == {"read_file", "search_code", FINISH_REVIEW_TOOL}


def test_run_review_carries_the_diff_into_the_request(env):
    config, _conn = env
    seen: dict = {}

    class CapturingModel(ScriptedReviewModel):
        def invoke(self, messages):
            seen.setdefault("request", messages[1].content)
            return super().invoke(messages)

    run_review(config, prompt="review this diff", diff="+ leaked = True",
               model=CapturingModel(), max_turns=5)

    assert "review this diff" in seen["request"]
    assert "+ leaked = True" in seen["request"]


def test_build_initial_messages_carries_the_prompt_and_the_diff():
    messages = build_initial_messages("review auth", "+ x = None")

    assert messages[0].type == "system"
    assert messages[1].type == "human"
    assert "review auth" in messages[1].content
    assert "+ x = None" in messages[1].content


def test_build_initial_messages_uses_the_injected_policy():
    messages = build_initial_messages("review auth", "d", policy="CUSTOM POLICY TEXT")

    assert messages[0].content == "CUSTOM POLICY TEXT"


def test_build_initial_messages_defaults_to_the_builtin_policy():
    messages = build_initial_messages("review auth", "d")

    assert messages[0].content == _POLICY


def test_build_initial_messages_omits_the_change_summary_by_default():
    """The baseline stays the baseline: with no summary the model gets the
    diff and nothing else. Every existing run must be byte-identical."""
    messages = build_initial_messages("review auth", "+ x = None")

    assert "CHANGE SUMMARY" not in messages[1].content


def test_build_initial_messages_carries_the_summary_ahead_of_the_diff():
    """When injected the summary is its own labelled block, not folded into
    the diff -- and the diff stays last, so it is what the model reads before
    it starts reasoning."""
    messages = build_initial_messages("review auth", "+ x = None",
                                      summary='{"qname": "m::UserModel"}')

    body = messages[1].content
    assert "CHANGE SUMMARY" in body
    assert '{"qname": "m::UserModel"}' in body
    assert body.index("CHANGE SUMMARY") < body.index("DIFF")


def test_run_review_threads_the_summary_into_the_request(env):
    config, conn = env
    seen: dict = {}

    class CapturingModel(ScriptedReviewModel):
        def invoke(self, messages):
            seen.setdefault("request", messages[1].content)
            return super().invoke(messages)

    run_review(config, conn, prompt="review this diff", diff="+ leaked = True",
               summary="CHANGED: m::UserModel", model=CapturingModel(),
               max_turns=5)

    assert "CHANGED: m::UserModel" in seen["request"]


def test_run_review_threads_the_policy_into_the_request(env):
    config, conn = env
    model = ScriptedReviewModel()

    run_review(config, conn, prompt="p", diff="DIFF-BODY", model=model,
               policy="CUSTOM POLICY TEXT")

    assert model.system_content == "CUSTOM POLICY TEXT"


# ---------------------------------------------------------------------------
# the harness skill and the retrospective over the run
# ---------------------------------------------------------------------------


def test_build_initial_messages_injects_the_harness_skill_second():
    messages = build_initial_messages("review auth", "d",
                                      harness_skill="HARNESS BODY")

    assert [message.type for message in messages] == ["system", "system", "human"]
    assert messages[0].content == _POLICY
    # Second, always: the retrospective is told to revise "the second system
    # message", a phrase that only means something if this is where it lands.
    assert messages[1].content == "HARNESS BODY"


def test_build_initial_messages_omits_the_harness_skill_by_default():
    """The baseline stays the baseline -- no second system message unless asked."""
    messages = build_initial_messages("review auth", "d")

    assert [message.type for message in messages] == ["system", "human"]


def test_run_review_injects_the_harness_skill_into_the_request(env):
    config, conn = env
    captured: dict = {}

    class CapturingModel(ScriptedReviewModel):
        def invoke(self, messages):
            captured.setdefault(
                "systems", [message.content for message in messages
                            if isinstance(message, SystemMessage)])
            return super().invoke(messages)

    run_review(config, conn, prompt="p", diff="DIFF-BODY", model=CapturingModel(),
               harness_skill="HARNESS BODY", max_turns=5)

    assert captured["systems"] == [_POLICY, "HARNESS BODY"]


def test_a_retrospective_without_a_harness_skill_is_a_configuration_error(env):
    """With no second system message there is nothing for it to revise.

    Saying so beats running a retrospective that is pointed at nothing and
    reports, convincingly, that the skill needed no changes.
    """
    config, conn = env

    with pytest.raises(ValueError) as excinfo:
        run_review(config, conn, prompt="p", diff="d", model=ScriptedReviewModel(),
                   skill_review=SkillReview(out_dir=Path("candidates")))

    assert "harness_skill" in str(excinfo.value)


class RetrospectiveModel(ScriptedReviewModel):
    """One object serving both runs, as the wiring does.

    A retrospective reuses the parent's model object unless
    ``--skill-review-model`` names another, so a test of the wiring cannot use
    two scripts -- it has to answer both requests and tell them apart, which it
    does by the instruction the retrospective request ends with.
    """

    def __init__(self):
        super().__init__()
        self.retrospective_requests: list[list] = []

    @staticmethod
    def _is_retrospective(messages) -> bool:
        return (isinstance(messages[-1], HumanMessage)
                and messages[-1].content == SKILL_REVIEW_INSTRUCTION)

    def invoke(self, messages):
        if self._is_retrospective(messages):
            self.retrospective_requests.append(list(messages))
            response = AIMessage(content="", tool_calls=[
                {"name": "submit_skill", "id": "skill-1",
                 "args": {"skill": "改好的全文", "changes": ["改了停止条件"]}}])
            response.usage_metadata = {"input_tokens": 100, "output_tokens": 10,
                                       "total_tokens": 110}
            return response
        response = super().invoke(messages)
        if any(call["name"] == FINISH_REVIEW_TOOL for call in response.tool_calls):
            response.usage_metadata = {"input_tokens": 1_000_000,
                                       "output_tokens": 1_000_000,
                                       "total_tokens": 2_000_000}
        return response


def test_the_retrospective_runs_after_the_review_and_leaves_it_untouched(env, tmp_path):
    config, conn = env
    model = RetrospectiveModel()
    out_dir = tmp_path / "candidates"

    result = run_review(config, conn, prompt="p", diff="DIFF-BODY", model=model,
                        max_turns=5, harness_skill="HARNESS BODY",
                        skill_review=SkillReview(out_dir=out_dir))

    # The review's own report is exactly what it would have been alone.
    assert result.review_complete is True
    assert result.failure_reason is None
    assert [finding.title for finding in result.findings] == [
        "leaks None on empty user"]
    assert result.cost == pytest.approx(6.0)

    # The retrospective replayed that run's history verbatim...
    assert len(model.retrospective_requests) == 1
    replay = model.retrospective_requests[0]
    assert replay[:len(result.messages)] == result.messages

    # ...and wrote a candidate, priced apart from the review it read.
    candidates = list(out_dir.glob("*.md"))
    assert len(candidates) == 1
    assert candidates[0].read_text(encoding="utf-8") == "改好的全文"
    assert result.skill_review is not None
    assert result.skill_review.cost == pytest.approx(100 / 1_000_000 * 1.5
                                                     + 10 / 1_000_000 * 4.5)
    assert submission_changes(result.skill_review) == ["改了停止条件"]


def test_the_reserved_accept_gate_is_never_called(env, tmp_path):
    """``accept`` is a reserved position, not a feature: nothing may read it.

    The design leaves candidate adoption out of scope, and a gate that
    half-fires would be worse than none -- it would silently decide which
    candidate is worth keeping.
    """
    config, conn = env
    calls: list = []
    review = SkillReview(out_dir=tmp_path / "candidates",
                         accept=lambda *args: calls.append(args) or True)

    run_review(config, conn, prompt="p", diff="DIFF-BODY",
               model=RetrospectiveModel(), max_turns=5,
               harness_skill="HARNESS BODY", skill_review=review)

    assert calls == []
