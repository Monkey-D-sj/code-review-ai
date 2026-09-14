"""The harness-skill retrospective: what it replays, what it may do, what it writes.

No DB, no network. The contract under test is the one the design rests on --
the parent's messages are handed over *as they are* (same objects, same order,
reasoning and tool-call ids intact), the whole parent tool set stays bound while
only the whitelist may run, and the retrospective produces a candidate file or
nothing at all. Nothing here reaches the parent review.
"""

from __future__ import annotations

from langchain_core.messages import (
    AIMessage,
    HumanMessage,
    SystemMessage,
    ToolMessage,
)
from pydantic import BaseModel, ConfigDict

from code_review_ai.review_loop.hooks import Hooks, POINT_MODEL_REQUEST_STARTED
from code_review_ai.review_loop.schemas import LoopResult, ToolSpec
from code_review_ai.review_loop.skill_review import (
    SKILL_REVIEW_INSTRUCTION,
    SKILL_REVIEW_PHASE,
    SUBMIT_SKILL_TOOL,
    run_skill_review,
    submission_changes,
)


class ReadArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")

    path: str
    start_line: int = 1
    end_line: int = 2


class SearchArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")

    query: str


class ScriptedModel:
    """bind_tools-shaped fake: records the bound names and every request."""

    def __init__(self, schedule):
        self._schedule = list(schedule)
        self.schemas: list[str] = []
        self.requests: list[list] = []

    def bind_tools(self, schemas):
        self.schemas = [schema["name"] for schema in schemas]
        return self

    def invoke(self, messages):
        self.requests.append(list(messages))
        content, calls = self._schedule.pop(0)
        return AIMessage(content=content, tool_calls=calls)


def _call(name: str, args: dict | None = None, ident: str | None = None) -> dict:
    return {"name": name, "args": args or {}, "id": ident or f"{name}-call"}


def _parent_result() -> LoopResult:
    """A finished parent run, shaped as ``run_loop`` hands one over.

    Carries the two things the replay exists for and the trace cannot hold: a
    tool reply's full text, and an assistant turn's ``reasoning_content``.
    """
    assistant = AIMessage(content="", tool_calls=[
        {"name": "read_file",
         "args": {"path": "app.py", "start_line": 1, "end_line": 2},
         "id": "read-1"}])
    assistant.additional_kwargs["reasoning_content"] = "先读现场"
    result = LoopResult()
    result.messages = [
        SystemMessage(content="评审政策"),
        SystemMessage(content="harness skill"),
        HumanMessage(content="DIFF"),
        assistant,
        ToolMessage(content="1: code", tool_call_id="read-1", name="read_file"),
    ]
    return result


def _parent_tools(ran: list[str]) -> list[ToolSpec]:
    """The read/search pair a parent run would have had."""

    def _read(path: str, start_line: int = 1, end_line: int = 2) -> str:
        ran.append(f"read:{path}")
        return "file body"

    def _search(query: str) -> str:
        ran.append(f"search:{query}")
        return "hits"

    return [
        ToolSpec(name="read_file", description="read", args_schema=ReadArgs,
                 run=_read),
        ToolSpec(name="search_code", description="search", args_schema=SearchArgs,
                 run=_search),
    ]


# ---------------------------------------------------------------------------
# the replay
# ---------------------------------------------------------------------------


def test_the_parent_history_is_replayed_verbatim_with_the_instruction_appended(tmp_path):
    parent = _parent_result()
    model = ScriptedModel([("", [_call(SUBMIT_SKILL_TOOL, {"skill": "S"})])])

    run_skill_review(model, parent, [], out_dir=tmp_path / "candidates")

    sent = model.requests[0]
    assert sent[:len(parent.messages)] == parent.messages
    # Identity, not just equality: the replay hands over the same message
    # objects, so nothing is lost to a re-serialization.
    assert sent[0] is parent.messages[0]
    assert sent[3].additional_kwargs["reasoning_content"] == "先读现场"
    assert sent[4].tool_call_id == "read-1"
    assert isinstance(sent[-1], HumanMessage)
    assert sent[-1].content == SKILL_REVIEW_INSTRUCTION


def test_a_parent_run_with_no_history_has_nothing_to_retrospect(tmp_path):
    model = ScriptedModel([])  # any invoke would pop from an empty schedule

    assert run_skill_review(model, LoopResult(), [], out_dir=tmp_path) == ""
    assert model.requests == []


def test_a_declining_trigger_runs_nothing_at_all(tmp_path):
    parent = _parent_result()
    model = ScriptedModel([])

    skill = run_skill_review(model, parent, [], out_dir=tmp_path,
                             trigger=lambda _result: False)

    assert skill == ""
    assert model.requests == []
    assert parent.skill_review is None


# ---------------------------------------------------------------------------
# what it may do
# ---------------------------------------------------------------------------


def test_the_parent_tools_stay_bound_but_only_the_whitelist_runs(tmp_path):
    ran: list[str] = []
    model = ScriptedModel([("", [_call("search_code", {"query": "x"}, "s-1")]),
                           ("", [_call(SUBMIT_SKILL_TOOL, {"skill": "S"}, "k-1")])])

    run_skill_review(model, _parent_result(), _parent_tools(ran),
                     out_dir=tmp_path / "candidates")

    # Bound, so the replayed history's tool_call ids all resolve...
    assert set(model.schemas) == {"read_file", "search_code", SUBMIT_SKILL_TOOL}
    # ...but refused, so the reviewer cannot repeat the wandering it diagnoses.
    assert ran == []
    replies = {message.tool_call_id: str(message.content)
               for message in model.requests[1] if isinstance(message, ToolMessage)}
    assert "not permitted in this run" in replies["s-1"]


# ---------------------------------------------------------------------------
# what it writes
# ---------------------------------------------------------------------------


def test_a_submission_is_written_as_a_timestamped_candidate(tmp_path):
    out_dir = tmp_path / "candidates"
    parent = _parent_result()
    model = ScriptedModel([("", [_call(SUBMIT_SKILL_TOOL, {
        "skill": "改好的全文",
        "changes": ["删掉了「研究完成后」——它没有给出停止条件"],
    }, "k-1")])])

    skill = run_skill_review(model, parent, [], out_dir=out_dir)

    assert skill == "改好的全文"
    written = list(out_dir.glob("*__skill.md"))
    assert len(written) == 1
    assert written[0].read_text(encoding="utf-8") == "改好的全文"
    # The candidate file holds the text alone, so it can be fed straight back to
    # --harness-skill; the reviewer's account of what it changed is reported
    # beside it, not inside it.
    assert submission_changes(parent.skill_review) == [
        "删掉了「研究完成后」——它没有给出停止条件"]


def test_a_retrospective_that_never_submits_writes_nothing(tmp_path):
    out_dir = tmp_path / "candidates"
    parent = _parent_result()
    model = ScriptedModel([("我觉得这份 skill 没问题。", [])])

    assert run_skill_review(model, parent, [], out_dir=out_dir) == ""
    assert not out_dir.exists()
    # The run itself is still reported: "produced nothing" and "never ran" are
    # different answers, and only the caller can tell them apart.
    assert parent.skill_review is not None
    assert parent.skill_review.failure_reason is not None
    assert submission_changes(parent.skill_review) == []


def test_the_nested_run_emits_its_events_under_its_own_phase(tmp_path):
    """An observer of the outer run must be able to tell the two apart.

    Both runs count turns from 1, so without the phase label a progress line --
    or a cost segment -- could belong to either.
    """
    model = ScriptedModel([("", [_call(SUBMIT_SKILL_TOOL, {"skill": "S"})])])
    hooks = Hooks()
    phases: list[str | None] = []
    hooks.on(POINT_MODEL_REQUEST_STARTED,
             lambda _event, context: phases.append(context.get("phase")))

    run_skill_review(model, _parent_result(), [],
                     out_dir=tmp_path / "candidates", hooks=hooks)

    assert phases == [SKILL_REVIEW_PHASE]
