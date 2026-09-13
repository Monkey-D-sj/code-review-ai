"""跨层字段契约评测集的判分器（``benchmarks/field_contract_eval.py``）。

判分器是纯的：给它一个 case 和一次评审的 findings，它算出命中/召回/精确。
不跑模型、不碰仓库、不建索引 —— 唯一 I/O 是读 manifest。

``benchmarks/`` 是开发工具而非发布包的一部分，所以测试把它放上 ``sys.path``
来导入，和 ``test_eval_cases.py`` 同一个套路。
"""

import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "benchmarks"))

from code_review_ai.review_loop.schemas import Finding  # noqa: E402

from field_contract_eval import (  # noqa: E402
    ContractCase,
    GoldSite,
    score,
)


def _case(*gold, neutral=(), case_id="synth", difficulty=2):
    """一个最小 case：只关心 gold / neutral 的坐标。"""
    def sites(entries):
        return tuple(GoldSite(file=f, start=s, end=e, why="") for f, s, e in entries)

    return ContractCase(id=case_id, motive="", diff="", mechanism="",
                        difficulty=difficulty, gold=sites(gold),
                        neutral=sites(neutral))


def _finding(file, line):
    """一条 finding，形状与评审 payload 的 Finding 一致。"""
    return {"file": file, "line": line, "title": "t", "description": "d"}


# ── 命中判定 ──────────────────────────────────────────────────────────

def test_finding_inside_a_gold_range_hits():
    case = _case(("app/x.py", 10, 20))

    result = score(case, [_finding("app/x.py", 15)])

    assert result.hits == 1
    assert result.recall == 1.0


def test_gold_range_boundaries_are_inclusive():
    case = _case(("app/x.py", 10, 20))

    assert score(case, [_finding("app/x.py", 10)]).hits == 1
    assert score(case, [_finding("app/x.py", 20)]).hits == 1


def test_finding_one_line_outside_the_range_misses():
    case = _case(("app/x.py", 10, 20))

    assert score(case, [_finding("app/x.py", 9)]).hits == 0
    assert score(case, [_finding("app/x.py", 21)]).hits == 0


def test_finding_on_the_same_line_in_another_file_misses():
    case = _case(("app/x.py", 10, 20))

    assert score(case, [_finding("app/y.py", 15)]).hits == 0


def test_finding_paths_are_normalized_before_comparison():
    case = _case(("app/x.py", 10, 20))

    assert score(case, [_finding(".\\app\\x.py", 15)]).hits == 1


def test_findings_without_an_integer_line_do_not_hit():
    case = _case(("app/x.py", 10, 20))

    result = score(case, [{"file": "app/x.py"}, {"file": "app/x.py", "line": None}])

    assert result.hits == 0


def test_findings_may_be_models_instead_of_dicts():
    case = _case(("app/x.py", 10, 20))
    finding = Finding(file="app/x.py", line=15, title="t", description="d")

    assert score(case, [finding]).hits == 1


# ── 召回与精确 ────────────────────────────────────────────────────────

def test_recall_is_the_fraction_of_gold_sites_hit():
    case = _case(("app/x.py", 10, 20), ("app/x.py", 30, 40), ("app/y.py", 1, 5))

    result = score(case, [_finding("app/x.py", 15)])

    assert result.hits == 1
    assert result.gold_total == 3
    assert result.recall == pytest.approx(1 / 3)


def test_many_findings_on_one_gold_site_still_count_once():
    case = _case(("app/x.py", 10, 20))

    result = score(case, [_finding("app/x.py", 12),
                          _finding("app/x.py", 15),
                          _finding("app/x.py", 18)])

    assert result.hits == 1
    assert result.reported == 3


def test_precision_drops_when_findings_land_outside_gold():
    case = _case(("app/x.py", 10, 20))

    result = score(case, [_finding("app/x.py", 15), _finding("app/z.py", 99)])

    assert result.hits == 1
    assert result.precision == pytest.approx(0.5)


def test_no_findings_scores_zero_recall_and_vacuous_precision():
    case = _case(("app/x.py", 10, 20))

    result = score(case, [])

    assert result.recall == 0.0
    assert result.precision == 1.0


# ── neutral：diff 自身的落点 ──────────────────────────────────────────

def test_findings_on_neutral_sites_do_not_hit():
    case = _case(("app/x.py", 10, 20), neutral=[("app/x.py", 50, 60)])

    result = score(case, [_finding("app/x.py", 55)])

    assert result.hits == 0
    assert result.on_neutral == 1
    assert result.recall == 0.0


# ── 空 case：gold 为空 ───────────────────────────────────────────────

def test_empty_case_has_no_recall_ratio():
    result = score(_case(), [_finding("app/x.py", 15)])

    assert result.empty is True
    assert result.recall is None


def test_empty_case_is_clean_when_nothing_is_reported():
    assert score(_case(), []).clean is True


def test_empty_case_is_clean_when_only_the_diff_itself_is_reported():
    case = _case(neutral=[("app/x.py", 10, 20)])

    assert score(case, [_finding("app/x.py", 15)]).clean is True


def test_empty_case_is_dirty_when_a_finding_lands_outside_the_diff():
    case = _case(neutral=[("app/x.py", 10, 20)])

    assert score(case, [_finding("app/z.py", 3)]).clean is False
