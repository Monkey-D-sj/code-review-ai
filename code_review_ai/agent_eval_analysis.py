"""Statistical summaries for completed Agentic Eval reports."""

from __future__ import annotations

import json
import random
from collections import defaultdict
from pathlib import Path

from code_review_ai.agent_eval import (DEFAULT_DIFFICULTY, DIFFICULTIES,
                                       AgentEvalCase, _create_case_snapshot,
                                       _remove_case_snapshot)
from code_review_ai.changes import assess_symbol_risk
from code_review_ai.config import Config


def analyze_agent_report(report: dict, bootstrap_samples: int = 5000,
                         seed: int = 42) -> dict:
    runs = report.get("runs")
    if not isinstance(runs, list) or not runs:
        raise ValueError("agent eval report has no runs")
    if bootstrap_samples < 100:
        raise ValueError("bootstrap_samples must be at least 100")
    modes = list(dict.fromkeys(run["mode"] for run in runs))
    baseline = report.get("baseline_mode")
    if baseline not in modes:
        baseline = "diff_only" if "diff_only" in modes else modes[0]
    paired_key = f"paired_vs_{baseline}"
    result = {
        "schema_version": 1,
        "source_schema_version": report.get("schema_version"),
        "run_count": len(runs),
        "case_count": len({run["case_id"] for run in runs}),
        "repetitions": report.get("repetitions"),
        "bootstrap_samples": bootstrap_samples,
        "baseline_mode": baseline,
        "modes": {mode: _mode_analysis(runs, mode, bootstrap_samples, seed)
                  for mode in modes},
        paired_key: {
            mode: _paired_analysis(runs, baseline, mode, bootstrap_samples, seed)
            for mode in modes if mode != baseline
        },
    }
    result["by_difficulty"] = _difficulty_analyses(
        runs, baseline, bootstrap_samples, seed)
    return result


def _difficulty_analyses(runs: list[dict], requested_baseline: str,
                         samples: int, seed: int) -> dict:
    """Repeat the paired report within each preregistered difficulty tier."""
    case_difficulties: dict[str, set[str]] = defaultdict(set)
    for run in runs:
        difficulty = run.get("difficulty", DEFAULT_DIFFICULTY)
        if difficulty not in (*DIFFICULTIES, DEFAULT_DIFFICULTY):
            raise ValueError(f"invalid run difficulty: {difficulty}")
        case_difficulties[run["case_id"]].add(difficulty)
    inconsistent = sorted(
        case_id for case_id, values in case_difficulties.items()
        if len(values) != 1)
    if inconsistent:
        raise ValueError(
            "inconsistent difficulty labels for cases: " + ", ".join(inconsistent))

    result = {}
    order = (*DIFFICULTIES, DEFAULT_DIFFICULTY)
    for offset, difficulty in enumerate(order):
        selected = [run for run in runs
                    if run.get("difficulty", DEFAULT_DIFFICULTY) == difficulty]
        if not selected:
            continue
        modes = list(dict.fromkeys(run["mode"] for run in selected))
        baseline = requested_baseline if requested_baseline in modes else modes[0]
        paired_key = f"paired_vs_{baseline}"
        result[difficulty] = {
            "case_count": len({run["case_id"] for run in selected}),
            "run_count": len(selected),
            "baseline_mode": baseline,
            "modes": {
                mode: _mode_analysis(selected, mode, samples,
                                     seed + offset * 100)
                for mode in modes
            },
            paired_key: {
                mode: _paired_analysis(selected, baseline, mode, samples,
                                       seed + offset * 100)
                for mode in modes if mode != baseline
            },
        }
    return result


def _mode_analysis(runs: list[dict], mode: str, samples: int,
                   seed: int) -> dict:
    selected = [run for run in runs if run["mode"] == mode]
    costs = [_cost(run) for run in selected]
    cases: dict[str, list[dict]] = defaultdict(list)
    for run in selected:
        cases[run["case_id"]].append(run)
    return {
        "runs": len(selected),
        "successful_runs": sum(bool(run["success"]) for run in selected),
        "provider_failures": sum(not run["success"] for run in selected),
        "precision": _clustered_estimate(cases, "precision", samples, seed),
        "recall": _clustered_estimate(cases, "recall", samples, seed + 1),
        "f1": _clustered_estimate(cases, "f1", samples, seed + 2),
        "total_cost_usd": round(sum(costs), 6),
        "mean_cost_usd": round(sum(costs) / len(selected), 6),
        "mean_input_tokens": _mean_values(
            [_usage_metric(run, "input_tokens") for run in selected]),
        "mean_output_tokens": _mean_values(
            [_usage_metric(run, "output_tokens") for run in selected]),
        "mean_total_tokens": _mean_values(
            [_total_tokens(run) for run in selected]),
        "mean_files_read": _mean_values(
            [float(len(run.get("files_read", []))) for run in selected]),
        "mean_unique_files_touched": _mean_values([
            float(len(run.get("unique_files_touched",
                              run.get("files_read", []))))
            for run in selected]),
        "mean_read_calls": _mean_values([
            float(run.get("read_calls", 0)) for run in selected]),
        "mean_search_calls": _mean_values([
            float(run.get("search_calls", 0)) for run in selected]),
        "mean_bash_calls": _mean_values([
            float(run.get("bash_calls", 0)) for run in selected]),
        "unknown_file_access_rate": _mean_values([
            float(bool(run.get("unknown_file_access", False)))
            for run in selected]),
        "mean_native_response_chars": _mean_values([
            float(run.get("native_response_chars", 0)) for run in selected]),
        "mean_mcp_response_chars": _mean_values([
            float(run.get("mcp_response_chars", 0)) for run in selected]),
        "mean_total_tool_calls": _mean_values([
            float(run.get("total_tool_calls", _tool_call_count(run)))
            for run in selected]),
        "stable_case_hits": sum(all(run["recall"] == 1 for run in case_runs)
                                for case_runs in cases.values()),
        "cases_with_any_hit": sum(any(run["recall"] > 0 for run in case_runs)
                                  for case_runs in cases.values()),
        "mean_elapsed_ms": round(sum(float(run.get("elapsed_ms", 0)) for run in selected)
                                 / len(selected), 4),
        "mean_actual_tool_calls": round(sum(
            int(run.get("tool_call_count", len(run.get("tool_calls", []))))
            for run in selected) / len(selected), 4),
        "mcp_adoption_rate": round(sum(any(
            isinstance(call, str) and call.startswith("mcp__")
            for call in run.get("tool_calls", [])) for run in selected)
            / len(selected), 4),
    }


def _paired_analysis(runs: list[dict], baseline_mode: str, mode: str,
                     samples: int, seed: int) -> dict:
    keyed = {(run["case_id"], run["repetition"], run["mode"]): run
             for run in runs}
    keys = sorted({(run["case_id"], run["repetition"])
                   for run in runs if run["mode"] == baseline_mode})
    pairs = [(keyed[(*key, baseline_mode)], keyed[(*key, mode)])
             for key in keys if (*key, mode) in keyed]
    f1_deltas = [float(candidate["f1"]) - float(baseline["f1"])
                 for baseline, candidate in pairs]
    f1_by_case = _group_deltas(pairs, "f1")
    recall_by_case = _group_deltas(pairs, "recall")
    return {
        "pairs": len(pairs),
        "f1_delta": _clustered_values(f1_by_case, samples, seed),
        "recall_delta": _clustered_values(recall_by_case, samples, seed + 1),
        "f1_wins": sum(delta > 0 for delta in f1_deltas),
        "f1_ties": sum(delta == 0 for delta in f1_deltas),
        "f1_losses": sum(delta < 0 for delta in f1_deltas),
        "cost_delta_usd": round(sum(_cost(candidate) - _cost(baseline)
                                    for baseline, candidate in pairs), 6),
        "input_tokens_delta": _paired_metric(
            pairs, lambda run: _usage_metric(run, "input_tokens"),
            samples, seed + 2),
        "output_tokens_delta": _paired_metric(
            pairs, lambda run: _usage_metric(run, "output_tokens"),
            samples, seed + 3),
        "total_tokens_delta": _paired_metric(
            pairs, _total_tokens, samples, seed + 4),
        "elapsed_ms_delta": _paired_metric(
            pairs, lambda run: float(run.get("elapsed_ms", 0)),
            samples, seed + 5),
        "tool_calls_delta": _paired_metric(
            pairs, _tool_call_count, samples, seed + 6),
        "files_read_delta": _paired_metric(
            pairs, lambda run: float(len(run.get("files_read", []))),
            samples, seed + 7),
    }


def _paired_metric(pairs: list[tuple[dict, dict]], getter,
                   samples: int, seed: int) -> dict:
    grouped: dict[str, list[float]] = defaultdict(list)
    for baseline, candidate in pairs:
        grouped[baseline["case_id"]].append(
            float(getter(candidate)) - float(getter(baseline)))
    return _clustered_values(grouped, samples, seed)


def _group_deltas(pairs: list[tuple[dict, dict]], metric: str
                  ) -> dict[str, list[float]]:
    grouped: dict[str, list[float]] = defaultdict(list)
    for baseline, candidate in pairs:
        grouped[baseline["case_id"]].append(
            float(candidate[metric]) - float(baseline[metric]))
    return grouped


def _clustered_estimate(cases: dict[str, list[dict]], metric: str,
                        samples: int, seed: int) -> dict:
    grouped = {case_id: [float(run[metric]) for run in runs]
               for case_id, runs in cases.items()}
    return _clustered_values(grouped, samples, seed)


def _clustered_values(grouped: dict[str, list[float]], samples: int,
                      seed: int) -> dict:
    values = [value for case_values in grouped.values() for value in case_values]
    mean = sum(values) / len(values)
    case_ids = list(grouped)
    generator = random.Random(seed)
    bootstrapped = []
    for _sample in range(samples):
        sampled = [generator.choice(case_ids) for _case_id in case_ids]
        sample_values = [value for case_id in sampled for value in grouped[case_id]]
        bootstrapped.append(sum(sample_values) / len(sample_values))
    bootstrapped.sort()
    return {"mean": round(mean, 4),
            "ci95": [round(_percentile(bootstrapped, 0.025), 4),
                     round(_percentile(bootstrapped, 0.975), 4)]}


def _percentile(values: list[float], fraction: float) -> float:
    index = min(round((len(values) - 1) * fraction), len(values) - 1)
    return values[index]


def _cost(run: dict) -> float:
    value = run.get("usage", {}).get("total_cost_usd")
    return float(value) if isinstance(value, (int, float)) else 0.0


def _usage_metric(run: dict, key: str) -> float:
    value = run.get("usage", {}).get(key)
    return float(value) if isinstance(value, (int, float)) else 0.0


def _total_tokens(run: dict) -> float:
    return (_usage_metric(run, "input_tokens") +
            _usage_metric(run, "output_tokens"))


def _tool_call_count(run: dict) -> float:
    value = run.get("tool_call_count")
    if isinstance(value, int):
        return float(value)
    return float(len(run.get("tool_calls", [])))


def _mean_values(values: list[float]) -> float:
    return round(sum(values) / len(values), 4) if values else 0.0


def analyze_file(report_path: str, output_path: str | None = None) -> dict:
    report = json.loads(Path(report_path).read_text(encoding="utf-8"))
    analysis = analyze_agent_report(report)
    if output_path:
        Path(output_path).write_text(
            json.dumps(analysis, indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8")
    return analysis


def route_check_analysis(conn, cases: list[AgentEvalCase], runs_dir: str,
                         config: Config | None = None,
                         work_dir: str | None = None,
                         repos_dir: str = ".code-review-ai/external-repos") -> dict:
    """Per-case max risk vs impact-context F1 delta over existing transcripts.

    Reads <runs_dir>/<case_id>/<mode>/run-*.json (each record has
    result.f1), averages per mode, and compares graph/hybrid against
    diff_only. Confirms the risk signal: high-risk cases should benefit
    from impact context, low-risk cases should not.
    """
    per_case = []
    for case in cases:
        f1 = _case_mode_f1(runs_dir, case.case_id)
        repetitions = _validate_route_coverage(case.case_id, f1)
        risk_conn = conn
        risk_symbols = case.changed_symbols
        snapshot = None
        if case.source_commit is not None:
            if config is None or work_dir is None:
                raise ValueError(
                    f"case {case.case_id} requires config and work_dir "
                    "to evaluate source_commit")
            snapshot = _create_case_snapshot(
                config, case, Path(work_dir), repos_dir=repos_dir)
            risk_conn = snapshot.conn
            risk_symbols = snapshot.changed_symbols
        try:
            risks = [assess_symbol_risk(risk_conn, symbol)
                     for symbol in risk_symbols]
        finally:
            if snapshot is not None:
                _remove_case_snapshot(snapshot)
        if not risks:
            continue
        baseline = _mean(f1.get("diff_only", []))
        per_case.append({
            "case_id": case.case_id,
            "max_risk": max(risks),
            "repetitions": repetitions,
            "graph_delta_f1": round(_mean(f1.get("graph_agent", [])) - baseline, 4),
            "hybrid_delta_f1": round(_mean(f1.get("hybrid_agent", [])) - baseline, 4),
        })
    risk_values = [row["max_risk"] for row in per_case]
    return {
        "case_count": len(per_case),
        "cases": per_case,
        "correlation": {
            "graph_delta_f1": _pearson(risk_values,
                                       [row["graph_delta_f1"] for row in per_case]),
            "hybrid_delta_f1": _pearson(risk_values,
                                        [row["hybrid_delta_f1"] for row in per_case]),
        },
        "groups": {
            "high_risk": _group_summary([row for row in per_case if row["max_risk"] >= 60]),
            "low_risk": _group_summary([row for row in per_case if row["max_risk"] < 60]),
        },
    }


def _case_mode_f1(runs_dir: str, case_id: str) -> dict[str, list[float]]:
    """{mode: [f1,...]} from transcripts <runs_dir>/<case_id>/<mode>/run-*.json."""
    case_dir = Path(runs_dir) / case_id
    if not case_dir.is_dir():
        return {}
    by_mode: dict[str, list[float]] = defaultdict(list)
    for mode_path in sorted(case_dir.iterdir()):
        if not mode_path.is_dir():
            continue
        for run_file in sorted(mode_path.glob("run-*.json")):
            record = json.loads(run_file.read_text(encoding="utf-8"))
            f1 = record.get("result", {}).get("f1")
            if isinstance(f1, (int, float)):
                by_mode[mode_path.name].append(float(f1))
    return by_mode


def _validate_route_coverage(case_id: str,
                             f1: dict[str, list[float]]) -> int:
    required = ("diff_only", "graph_agent", "hybrid_agent")
    missing = [mode for mode in required if not f1.get(mode)]
    if missing:
        raise ValueError(
            f"case {case_id} has no scored transcripts for: {', '.join(missing)}")
    counts = {mode: len(f1[mode]) for mode in required}
    if len(set(counts.values())) != 1:
        detail = ", ".join(f"{mode}={count}" for mode, count in counts.items())
        raise ValueError(f"case {case_id} has incomplete repetitions: {detail}")
    return counts[required[0]]


def _group_summary(rows: list[dict]) -> dict:
    return {
        "n": len(rows),
        "mean_graph_delta": round(_mean([r["graph_delta_f1"] for r in rows]), 4) if rows else 0.0,
        "mean_hybrid_delta": round(_mean([r["hybrid_delta_f1"] for r in rows]), 4) if rows else 0.0,
        "graph_positive": sum(r["graph_delta_f1"] > 0 for r in rows),
    }


def _mean(values: list[float]) -> float:
    return sum(values) / len(values) if values else 0.0


def _pearson(xs: list[float], ys: list[float]) -> float | None:
    """Pearson correlation; None when n<2 or zero variance."""
    if len(xs) < 2 or len(xs) != len(ys):
        return None
    mean_x = sum(xs) / len(xs)
    mean_y = sum(ys) / len(ys)
    numerator = sum((x - mean_x) * (y - mean_y) for x, y in zip(xs, ys))
    denom_x = sum((x - mean_x) ** 2 for x in xs)
    denom_y = sum((y - mean_y) ** 2 for y in ys)
    if denom_x == 0 or denom_y == 0:
        return None
    return round(numerator / (denom_x * denom_y) ** 0.5, 4)
