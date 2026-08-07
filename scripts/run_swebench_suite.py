"""Run snapshot-aware code-review-ai evaluation over a SWE-bench manifest."""

import argparse
import json
import subprocess
from pathlib import Path

from code_review_ai.benchmark import BenchmarkCase, load_cases, run_benchmark
from code_review_ai.config import load_config
from code_review_ai.db import connect, init_schema


def _run_git(arguments: list[str]) -> None:
    result = subprocess.run(["git", *arguments], text=True, capture_output=True)
    if result.returncode:
        raise RuntimeError(result.stderr.strip() or "git command failed")


def _repo_path(cache_dir: Path, repo: str) -> Path:
    return cache_dir / "repos" / repo.replace("/", "__")


def _prepare_repo(cache_dir: Path, case: BenchmarkCase) -> Path:
    if not case.repo or not case.base_commit:
        raise ValueError(f"case {case.case_id} requires repo and base_commit")
    repo_path = _repo_path(cache_dir, case.repo)
    if not repo_path.exists():
        repo_path.parent.mkdir(parents=True, exist_ok=True)
        _run_git(["clone", "--filter=blob:none",
                  f"https://github.com/{case.repo}.git", str(repo_path)])
    _run_git(["-C", str(repo_path), "checkout", "--detach", "--force",
              case.base_commit])
    return repo_path


def _case_config(cache_dir: Path, repo_path: Path, case: BenchmarkCase):
    config = load_config(str(repo_path))
    config.repo_path = str(repo_path)
    index_dir = cache_dir / "indexes"
    index_dir.mkdir(parents=True, exist_ok=True)
    config.db_path = str(index_dir / f"{case.case_id}.db")
    config.community_detection = False
    config.exclude = [pattern for pattern in config.exclude
                      if "test" not in pattern.lower()]
    return config


def _run_case(cache_dir: Path, case: BenchmarkCase, top_k: int) -> dict:
    repo_path = _prepare_repo(cache_dir, case)
    config = _case_config(cache_dir, repo_path, case)
    conn = connect(config.db_path)
    try:
        init_schema(conn)
        report = run_benchmark(config, conn, [case], top_k)
        result = report["cases"][0]
        result["index"] = report["index"]
        return result
    finally:
        conn.close()


def _summary(results: list[dict], top_k: int, dataset_name: str) -> dict:
    count = len(results)
    mean = lambda key: round(sum(result[key] for result in results) / count, 4)
    folds = [fold for result in results for fold in result["production_file_folds"]]
    eligible_cases = sum(bool(result["production_file_folds"]) for result in results)
    return {
        "schema_version": 2,
        "dataset": dataset_name,
        "metric_target": "gold historical test files",
        "top_k": top_k,
        "aggregate": {
            "cases": count,
            "macro_test_file_recall_at_k": mean("patch_file_recall_at_k"),
            "macro_test_file_precision_at_k": mean("patch_file_precision_at_k"),
            "macro_test_file_recall_all": mean("patch_file_recall_all"),
            "macro_test_file_precision_all": mean("patch_file_precision_all"),
            "macro_direct_test_file_recall_all": _direct_recall_mean(results),
            "cochange_gold_count": sum(
                len(result["cochange_gold_files"]) for result in results),
            "mean_all_candidate_files": mean("all_candidate_files_count"),
            "symbol_found_rate": mean("symbol_found_rate"),
            "mean_query_ms": mean("query_ms"),
            "mean_index_ms": round(sum(
                result["index"]["timings_ms"]["total"] for result in results
            ) / count, 1),
            "mean_nodes": round(sum(
                result["index"]["nodes"] for result in results
            ) / count, 1),
            "mean_resolved_call_rate": round(sum(
                result["index"]["resolved_call_rate"] for result in results
            ) / count, 4),
            "production_file_eligible_cases": eligible_cases,
            "production_file_folds": len(folds),
            "macro_related_production_file_recall_at_k": _fold_mean(
                folds, "recall_at_k"),
            "macro_related_production_file_precision_at_k": _fold_mean(
                folds, "precision_at_k"),
            "macro_related_production_file_recall_all": _fold_mean(
                folds, "recall_all"),
            "macro_related_production_file_precision_all": _fold_mean(
                folds, "precision_all"),
            "mean_production_all_candidate_files": _fold_mean(
                folds, "all_candidate_files_count"),
        },
        "cases": results,
    }


def _fold_mean(folds: list[dict], key: str) -> float | None:
    if not folds:
        return None
    return round(sum(fold[key] for fold in folds) / len(folds), 4)


def _direct_recall_mean(results: list[dict]) -> float | None:
    """Macro direct-test recall over cases that have at least one direct gold."""
    with_direct = [result for result in results if result["direct_gold_files"]]
    if not with_direct:
        return None
    return round(sum(result["direct_recall_all"]
                     for result in with_direct) / len(with_direct), 4)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--cases", required=True)
    parser.add_argument("--cache-dir", default=".benchmark-cache")
    parser.add_argument("--top-k", type=int, default=10)
    parser.add_argument("--dataset-name", default="Historical change suite")
    parser.add_argument("--limit", type=int)
    parser.add_argument("--out", required=True)
    args = parser.parse_args()
    cases = load_cases(args.cases)
    if args.limit:
        cases = cases[:args.limit]
    cache_dir = Path(args.cache_dir).resolve()
    results = [_run_case(cache_dir, case, args.top_k) for case in cases]
    output_path = Path(args.out)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(
        json.dumps(_summary(results, args.top_k, args.dataset_name), indent=2) + "\n",
        encoding="utf-8",
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
