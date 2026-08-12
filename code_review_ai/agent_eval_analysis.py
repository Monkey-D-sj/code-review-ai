"""Statistical summaries for completed Agentic Eval reports."""

from __future__ import annotations

import json
import random
from collections import defaultdict
from pathlib import Path

from code_review_ai.agent_eval import MCP_TOOL_PREFIX


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
    return {
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
            isinstance(call, str) and call.startswith(MCP_TOOL_PREFIX)
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
    }


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


def analyze_file(report_path: str, output_path: str | None = None) -> dict:
    report = json.loads(Path(report_path).read_text(encoding="utf-8"))
    analysis = analyze_agent_report(report)
    if output_path:
        Path(output_path).write_text(
            json.dumps(analysis, indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8")
    return analysis


