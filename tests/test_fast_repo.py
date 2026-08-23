"""Fast single-repo full-agent eval: manifest + local git-history build.

These tests exercise the local-repo path added for the fast iteration loop:
``benchmarks/fast-repo`` (committed seed) materializes a deterministic git
history via ``build_repo.py``, and ``prepare_full_agent_cases(..., local_repo=)``
runs the same reverse-mutation machinery against it instead of a remote clone.
"""

import inspect
import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from code_review_ai.full_agent_eval import (
    FullAgentCase, GoldFinding, _ensure_local_repo, load_full_agent_cases,
    prepare_full_agent_cases, select_full_agent_cases,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
SEED = REPO_ROOT / "benchmarks" / "fast-repo"
MANIFEST = REPO_ROOT / "benchmarks" / "fast-cases.json"

CASE_SLUGS = ("auth-swallow-exception", "caller-return-shape",
              "deep-chain-contract", "dropped-default-arg",
              "feature-flag-inversion", "large-noise",
              "notify-required-arg", "same-name-callee",
              "shipment-init-order")


def _run_git(cwd: Path, *args: str) -> str:
    completed = subprocess.run(["git", "-C", str(cwd), *args],
                               check=True, capture_output=True, text=True,
                               encoding="utf-8", errors="replace")
    return completed.stdout


def _build(seed: Path, target: Path) -> str:
    return subprocess.run(
        [sys.executable, str(seed / "build_repo.py"),
         "--seed", str(seed), "--target", str(target)],
        check=True, capture_output=True, text=True).stdout


def test_parse_case_allows_empty_repo_url(tmp_path):
    manifest = tmp_path / "cases.json"
    manifest.write_text(json.dumps([{
        "id": "local-fix", "repo_name": "fast-repo", "repo_url": "",
        "source_commit": "fix-x", "mutation_paths": ["src/x.py"],
        "prompt": "review", "gold_findings": [{
            "id": "bug", "file": "src/x.py", "keywords": ["regression"]}],
    }]), encoding="utf-8")
    cases = load_full_agent_cases(str(manifest))
    assert cases[0].repo_url == ""
    assert cases[0].source_commit == "fix-x"


def test_local_repo_rejects_remote_cases(tmp_path):
    remote = FullAgentCase(
        "remote-fix", "sample", "https://github.com/example/sample.git",
        "abc123", ("src/app.py",), "review it",
        (GoldFinding("bug", "src/app.py", None, None, ("regression",)),),
    )
    with pytest.raises(ValueError, match="empty repo_url"):
        _ensure_local_repo([remote], str(tmp_path / "seed"), tmp_path)


def test_build_repo_creates_fix_branches_and_is_idempotent(tmp_path):
    seed = tmp_path / "seed"
    shutil.copytree(SEED, seed)
    target = tmp_path / "repo"

    first = _build(seed, target)
    assert "built fast-repo (9 cases)" in first

    # every fix-* branch exists and carries the fixed module; its parent (^)
    # carries the buggy module.
    for case in CASE_SLUGS:
        _run_git(target, "cat-file", "-e", f"fix-{case}^{{commit}}")
        _run_git(target, "cat-file", "-e", f"buggy-{case}^{{commit}}")
    buggy = _run_git(target, "show",
                     "fix-caller-return-shape^:src/fast_bench/pricing.py")
    fixed = _run_git(target, "show",
                     "fix-caller-return-shape:src/fast_bench/pricing.py")
    assert "return (subtotal_cents, tax_cents, shipping_cents)" in buggy
    assert "return OrderTotal(subtotal_cents, tax_cents, shipping_cents)" in fixed
    # the two graph-rewarding cases added for the case-mix experiment.
    assert "return token.strip() or None" in _run_git(
        target, "show", "fix-same-name-callee^:src/fast_bench/token.py")
    assert "return token.strip()" in _run_git(
        target, "show", "fix-same-name-callee:src/fast_bench/token.py")
    assert "return f\"blob:{key}\"" in _run_git(
        target, "show", "fix-deep-chain-contract^:src/fast_bench/storage.py")
    assert "return _blob_for(key)" in _run_git(
        target, "show", "fix-deep-chain-contract:src/fast_bench/storage.py")
    # large-noise: the timeout default is dropped (buggy) and restored (fixed).
    assert "timeout=_to_int(payload, \"timeout\")" in _run_git(
        target, "show", "fix-large-noise^:src/bigapp/config.py")
    assert "timeout=_to_int(payload, \"timeout\", default=30)" in _run_git(
        target, "show", "fix-large-noise:src/bigapp/config.py")

    # re-running is a no-op thanks to the marker hash.
    again = _build(seed, target)
    assert "up to date" in again


def test_large_noise_design_isolates_one_crash_to_dispatch(monkeypatch):
    """The redesigned large-noise case has one observable regression.

    Generated consumers must do more than avoid a crash: every resolver,
    description, and guarded compute_wait decoy must return the same value for
    the fixed 30-second config and the mutated None config. Only dispatch may
    pass the raw optional value into queue.compute_wait and fail.
    """
    src = SEED / "src" / "bigapp"
    # the unguarded forward lives in dispatch.py, the multiply in queue.py
    dispatch_src = (src / "dispatch.py").read_text(encoding="utf-8")
    queue_src = (src / "queue.py").read_text(encoding="utf-8")
    assert "wait_ms = compute_wait(cfg.timeout)" in dispatch_src
    assert "cfg.timeout or" not in dispatch_src.split("compute_wait")[0]
    assert "seconds * 1000.0" in queue_src
    # the alerts decoy calls the same crash function but guarded
    alerts_src = (src / "alerts.py").read_text(encoding="utf-8")
    assert "compute_wait(cfg.timeout or DEFAULT_ALERTS_TIMEOUT)" in alerts_src
    # Runtime differential: fixed and mutated config states are externally
    # equivalent everywhere except the deliberately unguarded dispatch path.
    sys.path.insert(0, str(SEED / "src"))
    try:
        from bigapp.config import AppConfig
        fixed = AppConfig("x", "r", "postgres://db", 30, 3, 60, False, ())
        buggy = AppConfig("x", "r", "postgres://db", None, 3, 60, False, ())
        assert buggy.timeout_seconds == fixed.timeout_seconds == 30
        assert buggy.to_json() == fixed.to_json()

        changed_noise = []
        for module_name in ("archive", "store", "analytics", "ingest",
                            "transform", "sync", "reporting", "export",
                            "backfill", "dashboard", "metrics_ui", "alerts",
                            "gateway", "pipeline"):
            module = __import__(f"bigapp.{module_name}",
                                fromlist=[module_name])
            for fn_name, fn in inspect.getmembers(module, inspect.isfunction):
                if not fn_name.startswith(("_resolve", "_describe")):
                    continue
                fixed_value = fn(fixed)
                buggy_value = fn(buggy)
                if (fixed_value != buggy_value
                        or type(fixed_value) is not type(buggy_value)):
                    changed_noise.append(
                        (module_name, fn_name, fixed_value, buggy_value))
        import bigapp.alerts as alerts
        assert alerts._wait_before_alert(fixed) == alerts._wait_before_alert(buggy)
        assert changed_noise == [], (
            f"noise behavior must be invariant: {changed_noise}")

        import bigapp.dispatch as dispatch
        monkeypatch.setattr(dispatch, "parse_config", lambda raw: fixed)
        assert dispatch.build_plan(
            {"config": {}, "kind": "webhook"}).wait_seconds == 30
        monkeypatch.setattr(dispatch, "parse_config", lambda raw: buggy)
        with pytest.raises(TypeError):
            dispatch.build_plan({"config": {}, "kind": "webhook"})
    finally:
        sys.path.pop(0)


def test_large_noise_gold_requires_wait_computation_evidence():
    case = select_full_agent_cases(
        load_full_agent_cases(str(MANIFEST)), ["large-noise"])[0]
    assert len(case.gold_findings) == 1
    gold = case.gold_findings[0]
    assert gold.min_matches == 2
    assert set(gold.keywords) == {
        "typeerror", "compute_wait", "multiply", "multiplication", "millis",
    }
    assert "none" not in gold.keywords
    assert "dispatch" not in gold.keywords


def test_prepare_local_repo_end_to_end(tmp_path):
    cases = load_full_agent_cases(str(MANIFEST))
    case = select_full_agent_cases(cases, ["caller-return-shape"])[0]
    prepared = prepare_full_agent_cases([case], str(tmp_path / "repos"),
                                        str(tmp_path / "work"),
                                        local_repo=str(SEED))
    assert len(prepared) == 1
    item = prepared[0]
    # the worktree holds the buggy module (parent version restored)...
    worktree = Path(item.repo_path)
    restored = (worktree / "src/fast_bench/pricing.py").read_text(encoding="utf-8")
    assert "return (subtotal_cents, tax_cents, shipping_cents)" in restored
    # ...and the reported diff is exactly the mutation the agent reviews.
    assert "return (subtotal_cents, tax_cents, shipping_cents)" in item.diff
    assert "return OrderTotal(subtotal_cents, tax_cents, shipping_cents)" in item.diff
