# SWE-bench Verified retrieval baseline

Date: 2026-08-04

The committed 30-case suite uses production patch line ranges as change seeds
and official `test_patch` files as retrieval targets. All repositories are
indexed at their pinned pre-fix `base_commit`, with test files included.

| Repository | Cases | Test-file Recall@10 |
|---|---:|---:|
| pallets/flask | 1 | 0.0% |
| psf/requests | 8 | 50.0% |
| pytest-dev/pytest | 11 | 0.0% |
| pydata/xarray | 10 | 20.0% |
| **Overall (macro)** | **30** | **20.0%** |

Additional first-run results:

- Symbol found rate: 100.0%
- Macro test-file Precision@10: 7.89%
- Cases with at least one test-file hit: 6/30
- Mean impact query latency: 0.72 ms
- Mean full-index build time per historical snapshot: 3.22 s
- Mean indexed nodes / source files: 3,735 / 149
- Mean resolved-call rate: 9.7%
- Mean SQLite index size: 8.87 MB

This is a baseline, not a claim of production accuracy. The zero-result Flask
and pytest cases expose known static-analysis gaps around constructor dispatch,
hooks, fixtures, decorators, and other framework-driven control flow. Re-run
the suite after resolver changes and compare the generated JSON report rather
than replacing failures with hand-selected examples.
