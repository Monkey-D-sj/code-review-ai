"""Shared structured gold data and scoring for evaluation harnesses."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class GoldFinding:
    finding_id: str
    file: str
    line_start: int | None
    line_end: int | None
    keywords: tuple[str, ...]
    alternate_files: tuple[str, ...] = ()
    min_matches: int = 1


@dataclass(frozen=True)
class GoldContext:
    symbols: tuple[str, ...] = ()
    files: tuple[str, ...] = ()
    entries: tuple[str, ...] = ()
    tests: tuple[str, ...] = ()
    hard_negative_symbols: tuple[str, ...] = ()
    hard_negative_files: tuple[str, ...] = ()


@dataclass(frozen=True)
class EvaluationGold:
    root_causes: tuple[GoldFinding, ...]
    context: GoldContext = GoldContext()


def evaluation_gold_to_dict(gold: EvaluationGold) -> dict:
    return {
        "root_causes": [{
            "id": finding.finding_id,
            "fix_file": finding.file,
            **({"line_start": finding.line_start,
                "line_end": finding.line_end}
               if finding.line_start is not None else {}),
            "mechanism_terms": list(finding.keywords),
            "alternate_files": list(finding.alternate_files),
            "min_matches": finding.min_matches,
        } for finding in gold.root_causes],
        "context": {
            "symbols": list(gold.context.symbols),
            "files": list(gold.context.files),
            "entries": list(gold.context.entries),
            "tests": list(gold.context.tests),
            "hard_negatives": {
                "symbols": list(gold.context.hard_negative_symbols),
                "files": list(gold.context.hard_negative_files),
            },
        },
    }


def parse_evaluation_gold(record: dict, case_id: str) -> EvaluationGold:
    """Load the unified ``gold`` schema or adapt legacy manifest fields."""
    if "gold" not in record:
        return _parse_legacy_gold(record, case_id)
    raw = record["gold"]
    if not isinstance(raw, dict):
        raise ValueError(f"case {case_id} has an invalid gold object")
    root_causes = raw.get("root_causes")
    if not isinstance(root_causes, list) or not root_causes:
        raise ValueError(f"case {case_id} requires gold.root_causes")
    findings = tuple(_parse_root_cause(item, case_id) for item in root_causes)
    context = _parse_context(raw.get("context", {}), case_id)
    return EvaluationGold(findings, context)


def _parse_legacy_gold(record: dict, case_id: str) -> EvaluationGold:
    raw_findings = record.get("gold_findings")
    if not isinstance(raw_findings, list) or not raw_findings:
        raise ValueError(
            f"case {case_id} requires gold.root_causes or gold_findings")
    findings = tuple(_parse_legacy_finding(item, case_id)
                     for item in raw_findings)
    files = _string_tuple(record.get("gold_files", []), case_id,
                          "gold_files", paths=True)
    return EvaluationGold(findings, GoldContext(files=files))


def _parse_root_cause(record: object, case_id: str) -> GoldFinding:
    if not isinstance(record, dict):
        raise ValueError(f"case {case_id} has an invalid gold root cause")
    adapted = {
        "id": record.get("id"),
        "file": record.get("fix_file"),
        "line_start": record.get("line_start"),
        "line_end": record.get("line_end"),
        "keywords": record.get("mechanism_terms", []),
        "alternate_files": record.get("alternate_files", []),
        "min_matches": record.get("min_matches", 1),
    }
    return _parse_finding(adapted, case_id, "gold.root_causes")


def _parse_legacy_finding(record: object, case_id: str) -> GoldFinding:
    return _parse_finding(record, case_id, "gold_findings")


def _parse_finding(record: object, case_id: str, field: str) -> GoldFinding:
    if not isinstance(record, dict):
        raise ValueError(f"case {case_id} has an invalid {field} item")
    finding_id = record.get("id")
    file_path = record.get("file")
    line_start = record.get("line_start")
    line_end = record.get("line_end", line_start)
    keywords = record.get("keywords", [])
    alternates = record.get("alternate_files", [])
    min_matches = record.get("min_matches", 1)
    _validate_finding(case_id, finding_id, file_path, line_start, line_end,
                      keywords, alternates, min_matches)
    return GoldFinding(
        finding_id, _normalize(file_path), line_start, line_end,
        tuple(keyword.lower() for keyword in keywords),
        tuple(_normalize(path) for path in alternates), min_matches)


def _validate_finding(case_id: str, finding_id: object, file_path: object,
                      line_start: object, line_end: object, keywords: object,
                      alternates: object, min_matches: object) -> None:
    valid_lines = ((line_start is None and line_end is None) or
                   (isinstance(line_start, int) and isinstance(line_end, int)
                    and 1 <= line_start <= line_end))
    valid_keywords = isinstance(keywords, list) and all(
        isinstance(keyword, str) and keyword for keyword in keywords)
    valid_min = (isinstance(min_matches, int) and min_matches >= 1
                 and valid_keywords and min_matches <= len(keywords))
    if not isinstance(finding_id, str) or not finding_id:
        raise ValueError(f"case {case_id} gold root cause requires id")
    if not _valid_path(file_path) or not valid_lines:
        raise ValueError(f"case {case_id} gold root cause has invalid file/lines")
    if not valid_keywords or not valid_min:
        raise ValueError(f"case {case_id} gold root cause has invalid terms")
    if not isinstance(alternates, list) or not all(
            _valid_path(path) for path in alternates):
        raise ValueError(f"case {case_id} gold root cause has invalid alternates")


def _parse_context(record: object, case_id: str) -> GoldContext:
    if not isinstance(record, dict):
        raise ValueError(f"case {case_id} has an invalid gold.context")
    negatives = record.get("hard_negatives", {})
    if isinstance(negatives, list):
        negative_symbols = [item for item in negatives if "::" in str(item)]
        negative_files = [item for item in negatives if "::" not in str(item)]
    elif isinstance(negatives, dict):
        negative_symbols = negatives.get("symbols", [])
        negative_files = negatives.get("files", [])
    else:
        raise ValueError(f"case {case_id} has invalid gold.context.hard_negatives")
    return GoldContext(
        symbols=_string_tuple(record.get("symbols", []), case_id, "symbols"),
        files=_string_tuple(record.get("files", []), case_id, "files", paths=True),
        entries=_string_tuple(record.get("entries", []), case_id, "entries"),
        tests=_string_tuple(record.get("tests", []), case_id, "tests"),
        hard_negative_symbols=_string_tuple(
            negative_symbols, case_id, "hard_negatives.symbols"),
        hard_negative_files=_string_tuple(
            negative_files, case_id, "hard_negatives.files", paths=True),
    )


def _string_tuple(value: object, case_id: str, field: str,
                  paths: bool = False) -> tuple[str, ...]:
    valid = isinstance(value, list) and all(
        _valid_path(item) if paths else isinstance(item, str) and bool(item)
        for item in value)
    if not valid:
        raise ValueError(f"case {case_id} has invalid gold.context.{field}")
    values = (_normalize(item) for item in value) if paths else iter(value)
    return tuple(dict.fromkeys(values))


def score_root_causes(predictions: list[object],
                      golds: tuple[GoldFinding, ...]) -> dict:
    valid_predictions = [item for item in predictions if isinstance(item, dict)]
    gold_to_prediction: dict[int, int] = {}

    def assign(prediction_index: int, seen: set[int]) -> bool:
        for gold_index, gold in enumerate(golds):
            if gold_index in seen or not _matches(
                    valid_predictions[prediction_index], gold):
                continue
            seen.add(gold_index)
            previous = gold_to_prediction.get(gold_index)
            if previous is None or assign(previous, seen):
                gold_to_prediction[gold_index] = prediction_index
                return True
        return False

    for prediction_index in range(len(valid_predictions)):
        assign(prediction_index, set())
    matches = [{"gold_id": golds[index].finding_id,
                "file": golds[index].file}
               for index in sorted(gold_to_prediction)]
    return _finding_metrics(len(valid_predictions), len(golds), matches)


def _matches(prediction: dict, gold: GoldFinding) -> bool:
    if _normalize(str(prediction.get("file", ""))) not in (
            gold.file, *gold.alternate_files):
        return False
    if gold.line_start is not None:
        line = prediction.get("line")
        if not isinstance(line, int) or not gold.line_start <= line <= gold.line_end:
            return False
    text = f"{prediction.get('title', '')} {prediction.get('description', '')}".lower()
    return sum(keyword in text for keyword in gold.keywords) >= gold.min_matches


def _finding_metrics(predicted: int, expected: int, matches: list[dict]) -> dict:
    true_positives = len(matches)
    precision = true_positives / predicted if predicted else 0.0
    recall = true_positives / expected if expected else 0.0
    f1 = _f1(precision, recall)
    return {"predicted_findings": predicted, "gold_findings": expected,
            "matched_findings": matches, "precision": round(precision, 4),
            "recall": round(recall, 4), "f1": round(f1, 4)}


def score_context(evidence: dict[str, list[str] | tuple[str, ...]],
                  gold: GoldContext) -> dict:
    dimensions = {
        "symbols": (evidence.get("symbols", ()), gold.symbols),
        "files": (evidence.get("files", ()), gold.files),
        "entries": (evidence.get("entries", ()), gold.entries),
        "tests": (evidence.get("tests", ()), gold.tests),
    }
    scores = {name: _set_metrics(actual, expected)
              for name, (actual, expected) in dimensions.items()}
    negative_hits = {
        "symbols": sorted(set(evidence.get("symbols", ())) &
                          set(gold.hard_negative_symbols)),
        "files": sorted(set(evidence.get("files", ())) &
                        set(gold.hard_negative_files)),
    }
    negative_total = (len(gold.hard_negative_symbols) +
                      len(gold.hard_negative_files))
    hit_total = sum(len(items) for items in negative_hits.values())
    return {**scores, "macro_recall": _applicable_mean(scores, "recall"),
            "hard_negatives": {
                "applicable": negative_total > 0,
                "expected": negative_total, "hits": negative_hits,
                "correctness": round(1 - hit_total / negative_total, 4)
                if negative_total else None,
            }}


def score_agent_review(payload: dict, gold: EvaluationGold) -> dict:
    findings = payload.get("findings", [])
    root_causes = score_root_causes(
        findings if isinstance(findings, list) else [], gold.root_causes)
    evidence = {
        "symbols": _payload_strings(payload, "affected_symbols"),
        "files": tuple(_normalize(path) for path in
                       _payload_strings(payload, "affected_files")),
        "entries": _payload_strings(payload, "affected_entries"),
        "tests": _payload_strings(payload, "tests"),
    }
    return {"root_causes": root_causes,
            "affected_context": score_context(evidence, gold.context)}


def _payload_strings(payload: dict, key: str) -> tuple[str, ...]:
    value = payload.get(key, [])
    return tuple(dict.fromkeys(item for item in value
                               if isinstance(item, str) and item)) \
        if isinstance(value, list) else ()


def _set_metrics(actual_values, expected_values) -> dict:
    actual = set(actual_values)
    expected = set(expected_values)
    if not expected:
        return {"applicable": False, "expected": 0, "returned": len(actual),
                "hits": [], "misses": [], "precision": None,
                "recall": None, "f1": None}
    hits = actual & expected
    precision = len(hits) / len(actual) if actual else 0.0
    recall = len(hits) / len(expected)
    return {"applicable": True, "expected": len(expected),
            "returned": len(actual), "hits": sorted(hits),
            "misses": sorted(expected - actual),
            "precision": round(precision, 4), "recall": round(recall, 4),
            "f1": round(_f1(precision, recall), 4)}


def _applicable_mean(scores: dict[str, dict], metric: str) -> float | None:
    values = [score[metric] for score in scores.values()
              if score.get("applicable") and score.get(metric) is not None]
    return round(sum(values) / len(values), 4) if values else None


def _f1(precision: float, recall: float) -> float:
    return (2 * precision * recall / (precision + recall)
            if precision + recall else 0.0)


def _valid_path(value: object) -> bool:
    return (isinstance(value, str) and bool(value)
            and not Path(value).is_absolute() and ".." not in Path(value).parts)


def _normalize(path: str) -> str:
    return path.replace("\\", "/").removeprefix("./")
