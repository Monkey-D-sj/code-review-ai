"""跨层字段契约评测集的加载与判分。

评测集的形状：一次改动引入了「某个字段在所有层的一致性」的破坏，gold 是
**该改而没改的那些落点**（一组位置），不是改动本身。所以判分是集合比对，
不是命中/未命中的布尔 —— 见 :func:`score`。

纯函数：不跑模型、不碰仓库、不建索引。唯一 I/O 是读 manifest。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

HERE = Path(__file__).resolve().parent
DEFAULT_MANIFEST = HERE / "field-contract-cases.json"


@dataclass(frozen=True)
class GoldSite:
    """一个「该改而没改」的落点：文件 + 行区间。"""

    file: str
    start: int
    end: int
    why: str = ""


@dataclass(frozen=True)
class ContractCase:
    """一条改动，以及它种下的字段契约破坏。"""

    id: str
    motive: str
    diff: str
    gold: tuple[GoldSite, ...]
    neutral: tuple[GoldSite, ...]
    mechanism: str
    difficulty: int


@dataclass(frozen=True)
class CaseScore:
    """一次评审在一条 case 上的得分。

    ``recall`` 是 ``None`` 而不是 0 或 1：gold 为空时 0/0 无定义，硬给一个
    数字会让空 case 混进均值里。空 case 只看 :attr:`clean`。
    """

    case_id: str
    difficulty: int
    hits: int
    gold_total: int
    reported: int
    on_neutral: int
    recall: float | None
    precision: float

    @property
    def empty(self) -> bool:
        return self.gold_total == 0

    @property
    def clean(self) -> bool:
        """空 case 专用：没有报任何「非 diff 落点」的 finding。"""
        return self.reported == self.on_neutral


def score(case: ContractCase, findings) -> CaseScore:
    """一次评审的 findings → 这条 case 的得分。

    ``hits`` 是**被命中的 gold 点个数**，不是命中的 finding 个数：一条 finding
    只能命中一个点，而多个 finding 落在同一个 gold 点上只算一次。否则在同一点
    上重复刷 finding 就能把召回率刷上去。

    ``on_neutral`` 只统计不扣分：它区分「报了 diff 自身的落点」和「乱报」，
    两者都不增加召回，但只有后者说明评审没有落点概念。
    """
    reported = list(findings or ())
    hits = sum(1 for site in case.gold
               if any(_lands_on(finding, site) for finding in reported))
    on_neutral = sum(1 for finding in reported
                     if any(_lands_on(finding, site) for site in case.neutral))
    gold_total = len(case.gold)
    return CaseScore(
        case_id=case.id,
        difficulty=case.difficulty,
        hits=hits,
        gold_total=gold_total,
        reported=len(reported),
        on_neutral=on_neutral,
        recall=None if gold_total == 0 else hits / gold_total,
        # 一条都不报时精确率空定义为 1：分母为零不代表报得准，
        # 而是召回率会为 0，两者要分开看。
        precision=(hits / len(reported)) if reported else 1.0,
    )


def load_cases(manifest: Path | str = DEFAULT_MANIFEST,
               case_ids: list[str] | None = None) -> list[ContractCase]:
    """读 manifest，可选地按 ``case_ids`` 收窄（未知 id 报错）。

    结构性不合法的输入一律抛 :class:`ValueError` 而不是尽力读下去：一条坐标
    写错的 case 读进来会变成一条**语义不同**的 case，安静地给出错误分数，
    比直接读不出来危险得多。
    """
    payload = json.loads(Path(manifest).read_text(encoding="utf-8"))
    records = payload.get("cases") if isinstance(payload, dict) else None
    if not isinstance(records, list):
        raise ValueError(f"{manifest}: 期望一个带 'cases' 列表的对象")
    cases = [_to_case(record, manifest) for record in records]
    _reject_duplicate_ids(cases, manifest)
    if case_ids:
        wanted = set(case_ids)
        unknown = sorted(wanted - {case.id for case in cases})
        if unknown:
            raise ValueError(f"unknown case id(s): {unknown}")
        cases = [case for case in cases if case.id in wanted]
    return cases


def _to_case(record: object, manifest: Path | str) -> ContractCase:
    """一条 manifest 记录 → ContractCase，拒绝任何缺项的记录。"""
    if not isinstance(record, dict):
        raise ValueError(f"{manifest}: 每条 case 必须是一个对象")
    case_id = str(record.get("id") or "").strip()
    if not case_id:
        raise ValueError(f"{manifest}: 有一条 case 缺 'id'")
    diff = str(record.get("diff") or "")
    if not diff.strip():
        raise ValueError(f"{case_id}: 缺 'diff'（{manifest}）")
    mechanism = str(record.get("mechanism") or "")
    if not mechanism.strip():
        raise ValueError(f"{case_id}: 缺 'mechanism'（{manifest}）")
    case = ContractCase(
        id=case_id,
        motive=str(record.get("motive") or ""),
        diff=diff,
        gold=_sites(record.get("gold"), case_id, "gold"),
        neutral=_sites(record.get("neutral"), case_id, "neutral"),
        mechanism=mechanism,
        difficulty=int(record.get("difficulty") or 0),
    )
    _reject_overlap(case, manifest)
    return case


def _sites(entries: object, case_id: str, field: str) -> tuple[GoldSite, ...]:
    """``gold`` / ``neutral`` 的原始列表 → 校验过的 GoldSite 元组。"""
    if entries is None:
        return ()
    if not isinstance(entries, list):
        raise ValueError(f"{case_id}: '{field}' 必须是列表")
    return tuple(_site(entry, case_id, field) for entry in entries)


def _site(entry: object, case_id: str, field: str) -> GoldSite:
    if not isinstance(entry, dict):
        raise ValueError(f"{case_id}: '{field}' 的每一项必须是对象")
    file = str(entry.get("file") or "").strip()
    if not file:
        raise ValueError(f"{case_id}: '{field}' 有一项缺 'file'")
    start, end = _line(entry.get("start"), case_id, file, "start"), \
        _line(entry.get("end"), case_id, file, "end")
    if start > end:
        raise ValueError(f"{case_id}: {file} 的 start > end（{start} > {end}）")
    return GoldSite(file=file, start=start, end=end, why=str(entry.get("why") or ""))


def _line(value: object, case_id: str, file: str, name: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool):
        raise ValueError(f"{case_id}: {file} 的 {name} 不是整数（{value!r}）")
    if value < 1:
        raise ValueError(f"{case_id}: {file} 的 line {name}={value} 不是正数")
    return value


def _reject_duplicate_ids(cases: list[ContractCase], manifest: Path | str) -> None:
    seen: set[str] = set()
    for case in cases:
        if case.id in seen:
            raise ValueError(f"duplicate case id {case.id!r} in {manifest}")
        seen.add(case.id)


def _reject_overlap(case: ContractCase, manifest: Path | str) -> None:
    """gold 与 neutral 不得重叠。

    重叠了的话，落在重叠区的 finding 会**同时**算命中又不算命中，
    判分就不再是一个良定义的函数。
    """
    for gold in case.gold:
        for neutral in case.neutral:
            if gold.file == neutral.file and gold.start <= neutral.end \
                    and neutral.start <= gold.end:
                raise ValueError(
                    f"{case.id}: {gold.file} 的 gold 与 neutral overlap"
                    f"（{gold.start}-{gold.end} / {neutral.start}-{neutral.end}）in {manifest}")


def missed_sites(case: ContractCase, findings) -> list[GoldSite]:
    """这次评审**没**命中的 gold 点 —— 报告里指出漏在哪，是这批数字的可解释性来源。"""
    reported = list(findings or ())
    return [site for site in case.gold
            if not any(_lands_on(finding, site) for finding in reported)]


def summarize(scores) -> dict:
    """一批 :class:`CaseScore` → 汇总。

    召回率只在**非空** case 上取均值（空 case 的召回无定义），空 case 单看
    :attr:`CaseScore.clean` 的比例。精确率则对所有 case 取均值 —— 空 case 上
    乱报同样该拉低它。
    """
    scores = list(scores)
    scored = [entry for entry in scores if not entry.empty]
    empties = [entry for entry in scores if entry.empty]
    return {
        "cases": len(scores),
        "scored_cases": len(scored),
        "empty_cases": len(empties),
        "mean_recall": _mean([entry.recall for entry in scored]),
        "mean_precision": _mean([entry.precision for entry in scores]),
        "clean_rate": _mean([1.0 if entry.clean else 0.0 for entry in empties]),
        "by_difficulty": _by_difficulty(scored),
    }


def _by_difficulty(scored: list[CaseScore]) -> dict:
    buckets: dict[int, list[CaseScore]] = {}
    for entry in scored:
        buckets.setdefault(entry.difficulty, []).append(entry)
    return {level: {
        "cases": len(group),
        "mean_recall": _mean([entry.recall for entry in group]),
        "mean_precision": _mean([entry.precision for entry in group]),
    } for level, group in sorted(buckets.items())}


def _mean(values: list) -> float | None:
    """空序列返回 ``None`` 而不是 0 —— 5 条 case 里一条没跑出来不是「得了 0 分」。"""
    present = [value for value in values if value is not None]
    return round(sum(present) / len(present), 4) if present else None


def _lands_on(finding: object, site: GoldSite) -> bool:
    """这条 finding 是否落在该 gold 点的行区间内。

    同时吃 finding 的 dict 和 pydantic 模型两种形态：评测脚本读的是评审
    payload 的 JSON，而库内调用方拿到的是 ``Finding``。
    """
    if isinstance(finding, dict):
        file, line = finding.get("file"), finding.get("line")
    else:
        file, line = getattr(finding, "file", None), getattr(finding, "line", None)
    if not isinstance(line, int) or isinstance(line, bool):
        return False
    return _normalize_path(file) == _normalize_path(site.file) and site.start <= line <= site.end


def _normalize_path(path: object) -> str:
    """仓库相对、正斜杠、无 ``./`` 前缀。

    两侧都过一遍：gold 是手写的，finding 的 ``file`` 是模型写的自由文本
    (``app/x.py`` / ``./app/x.py`` / ``app\\x.py`` 是同一个文件)。
    与 ``eval_cases._normalize_path`` 同一口径 —— 两套 harness 对「同一个
    路径」的看法必须一致。
    """
    text = str(path or "").strip().replace("\\", "/")
    while text.startswith("./"):
        text = text[2:]
    return text
