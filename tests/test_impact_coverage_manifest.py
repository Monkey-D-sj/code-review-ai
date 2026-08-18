"""Phase 0 guard: benchmarks/impact-coverage.json stays in sync with the
Coverage Matrix doc and the status overlay in benchmarks/gen_impact_coverage.py.

The manifest is the machine-readable source of truth for coverage status.
If a feature commit moves IDs without regenerating the manifest, these tests
fail and the reviewer sees the drift.
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
MANIFEST = ROOT / "benchmarks" / "impact-coverage.json"
GEN_SCRIPT = ROOT / "benchmarks" / "gen_impact_coverage.py"

VALID_STATUSES = {"missing", "partial", "covered", "unsupported"}


def _load_generator() -> object:
    spec = importlib.util.spec_from_file_location("gen_impact_coverage", GEN_SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)  # type: ignore[union-attr]
    return module


def _committed() -> dict:
    return json.loads(MANIFEST.read_text(encoding="utf-8"))


def test_manifest_matches_matrix_and_overlay():
    """Regenerating from the doc + overlay reproduces the committed file exactly."""
    generator = _load_generator()
    regenerated = generator.build(generator.DEFAULT_MATRIX, generator.BASELINE)
    committed = _committed()
    assert regenerated["items"] == committed["items"]
    assert regenerated["summary"] == committed["summary"]


def test_manifest_is_well_formed():
    manifest = _committed()
    assert set(manifest["status_values"]) == VALID_STATUSES
    items = manifest["items"]
    summary = manifest["summary"]
    assert len(items) == summary["total"]
    assert summary["total"] == sum(summary[status] for status in VALID_STATUSES)
    assert all(item["status"] in VALID_STATUSES for item in items.values())
    assert all(item["level"] for item in items.values())
    assert all(item["desc"] for item in items.values())
    assert all(isinstance(item["reason"], str) for item in items.values())
    assert all(isinstance(item["evidence"], list) for item in items.values())


def test_manifest_baseline_is_pinned():
    manifest = _committed()
    assert manifest["baseline"]["commit"]
    assert manifest["baseline"]["tests"]["command"] == "uv run pytest"
    assert manifest["baseline"]["tests"]["passed"] >= 0
