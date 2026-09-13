"""manifest 的加载与结构校验，以及一批得分的汇总。

manifest 是手写 + 脚本生成的混合产物（diff 是实测的，gold 坐标也是实测的），
所以加载器必须**响亮地拒绝**结构不合法的输入，而不是把它读成一条语义不同
的 case —— 悄悄读错比读不出来危险得多。

这批测试还兼作**语料本身的守卫**：``test_shipped_manifest_*`` 直接断言仓库里
那份 ``field-contract-cases.json`` 的不变量，造新 case 时写错会被这里挡住。
"""

import json
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "benchmarks"))

from field_contract_eval import (  # noqa: E402
    DEFAULT_MANIFEST,
    CaseScore,
    ContractCase,
    GoldSite,
    load_cases,
    missed_sites,
    score,
    summarize,
)


def _record(case_id="c1", gold=None, neutral=None, difficulty=2):
    """一条 manifest 记录（和落盘的 JSON 同形）。"""
    return {
        "id": case_id,
        "family": "cross-layer-field-contract",
        "difficulty": difficulty,
        "motive": "动机",
        "diff": "diff --git a/app/x.py b/app/x.py\n",
        "gold": gold if gold is not None else [
            {"file": "app/x.py", "start": 10, "end": 20, "why": "该加而没加"}],
        "neutral": neutral if neutral is not None else [],
        "mechanism": "机理",
    }


def _write(tmp_path, records):
    path = tmp_path / "cases.json"
    path.write_text(json.dumps({"cases": records}, ensure_ascii=False), encoding="utf-8")
    return path


def _score(case_id, hits, gold_total, reported, on_neutral, recall, precision, difficulty=2):
    return CaseScore(case_id=case_id, difficulty=difficulty, hits=hits,
                     gold_total=gold_total, reported=reported, on_neutral=on_neutral,
                     recall=recall, precision=precision)


# ── load_cases ───────────────────────────────────────────────────────

def test_load_cases_reads_a_well_formed_manifest(tmp_path):
    path = _write(tmp_path, [_record(case_id="alpha")])

    cases = load_cases(path)

    assert [case.id for case in cases] == ["alpha"]
    assert cases[0].gold == (GoldSite(file="app/x.py", start=10, end=20, why="该加而没加"),)


def test_load_cases_narrows_to_the_requested_ids(tmp_path):
    path = _write(tmp_path, [_record("alpha"), _record("beta")])

    cases = load_cases(path, ["beta"])

    assert [case.id for case in cases] == ["beta"]


def test_load_cases_rejects_an_unknown_id(tmp_path):
    path = _write(tmp_path, [_record("alpha")])

    with pytest.raises(ValueError, match="unknown case id"):
        load_cases(path, ["nope"])


def test_load_cases_rejects_duplicate_ids(tmp_path):
    path = _write(tmp_path, [_record("alpha"), _record("alpha")])

    with pytest.raises(ValueError, match="duplicate case id"):
        load_cases(path)


def test_load_cases_rejects_a_reversed_range(tmp_path):
    path = _write(tmp_path, [_record(gold=[{"file": "app/x.py", "start": 20, "end": 10}])])

    with pytest.raises(ValueError, match="start > end"):
        load_cases(path)


def test_load_cases_rejects_a_non_positive_line(tmp_path):
    path = _write(tmp_path, [_record(gold=[{"file": "app/x.py", "start": 0, "end": 5}])])

    with pytest.raises(ValueError, match="line"):
        load_cases(path)


def test_load_cases_rejects_gold_overlapping_neutral(tmp_path):
    record = _record(gold=[{"file": "app/x.py", "start": 10, "end": 20}],
                     neutral=[{"file": "app/x.py", "start": 15, "end": 25}])
    path = _write(tmp_path, [record])

    with pytest.raises(ValueError, match="overlap"):
        load_cases(path)


def test_load_cases_rejects_a_missing_diff(tmp_path):
    record = _record()
    record["diff"] = ""
    path = _write(tmp_path, [record])

    with pytest.raises(ValueError, match="diff"):
        load_cases(path)


def test_load_cases_rejects_a_missing_mechanism(tmp_path):
    record = _record()
    record["mechanism"] = "   "
    path = _write(tmp_path, [record])

    with pytest.raises(ValueError, match="mechanism"):
        load_cases(path)


# ── 语料自身的守卫 ────────────────────────────────────────────────────

def test_shipped_manifest_loads():
    cases = load_cases()

    assert cases, "评测集不该是空的"
    assert len({case.id for case in cases}) == len(cases)


def test_shipped_manifest_keeps_one_empty_case():
    """空 case 是防作弊的那条腿：没有它，见谁喊 bug 就能拿满分。"""
    cases = load_cases()

    empties = [case for case in cases if not case.gold]

    assert len(empties) == 1


def test_shipped_manifest_has_a_case_with_several_gold_sites():
    """gold 全是单点的话，召回率退化成「答对没答对」，集合判分就没意义了。"""
    cases = load_cases()

    assert max(len(case.gold) for case in cases) >= 3


def test_shipped_manifest_marks_every_diff_hunk_neutral():
    """diff 自身的落点必须登记为 neutral，否则「只报改动处」会被当成乱报。"""
    cases = load_cases()

    assert all(case.neutral for case in cases if case.gold or case.neutral)


# ── missed_sites ─────────────────────────────────────────────────────

def test_missed_sites_lists_only_the_unreported_gold():
    case = ContractCase(
        id="synth", motive="", diff="d", mechanism="m", difficulty=2,
        gold=(GoldSite(file="app/x.py", start=10, end=20),
              GoldSite(file="app/y.py", start=1, end=5)),
        neutral=())

    missed = missed_sites(case, [{"file": "app/x.py", "line": 15}])

    assert missed == [GoldSite(file="app/y.py", start=1, end=5)]


# ── summarize ────────────────────────────────────────────────────────

def test_summarize_averages_recall_over_scored_cases_only():
    scores = [_score("a", 1, 1, 1, 0, 1.0, 1.0),
              _score("b", 1, 2, 1, 0, 0.5, 1.0),
              _score("c", 0, 0, 0, 0, None, 1.0)]

    summary = summarize(scores)

    assert summary["mean_recall"] == pytest.approx(0.75)
    assert summary["scored_cases"] == 2
    assert summary["empty_cases"] == 1


def test_summarize_reports_clean_rate_over_empty_cases_only():
    scores = [_score("a", 1, 1, 1, 0, 1.0, 1.0),
              _score("c", 0, 0, 0, 0, None, 1.0),
              _score("d", 0, 0, 3, 0, None, 0.0)]

    summary = summarize(scores)

    assert summary["clean_rate"] == pytest.approx(0.5)


def test_summarize_breaks_down_by_difficulty():
    scores = [_score("a", 1, 1, 1, 0, 1.0, 1.0, difficulty=1),
              _score("b", 0, 1, 1, 0, 0.0, 0.0, difficulty=3)]

    summary = summarize(scores)

    assert summary["by_difficulty"][1]["mean_recall"] == pytest.approx(1.0)
    assert summary["by_difficulty"][3]["mean_recall"] == pytest.approx(0.0)


def test_summarize_of_nothing_does_not_divide_by_zero():
    summary = summarize([])

    assert summary["cases"] == 0
    assert summary["mean_recall"] is None
    assert summary["clean_rate"] is None
