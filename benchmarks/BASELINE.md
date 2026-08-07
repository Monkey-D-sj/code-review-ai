# Historical change retrieval baseline

Date: 2026-08-06

The 50-case suite combines three explicitly labelled sources:

- 30 SWE-bench Verified cases from Flask, Requests, pytest, and Xarray;
- 10 commits mined from the official FastAPI Git history because FastAPI is
  not part of classic SWE-bench Verified.
- 10 commits mined from the official Spring PetClinic Git history as the Java
  and Spring Boot subset.

Every repository is indexed at its pinned pre-fix `base_commit`. Production
patch ranges are change seeds, while test files changed by the real fix are
retrieval targets. Tests are included in indexing.

| Repository | Source | Cases | Test Recall@10 | Test Recall@All |
|---|---|---:|---:|---:|
| pallets/flask | SWE-bench Verified | 1 | 100.0% | 100.0% |
| psf/requests | SWE-bench Verified | 8 | 75.0% | 75.0% |
| pytest-dev/pytest | SWE-bench Verified | 11 | 9.09% | 9.09% |
| pydata/xarray | SWE-bench Verified | 10 | 20.0% | 90.0% |
| fastapi/fastapi | Official Git history | 10 | 10.0% | 30.0% |
| spring-projects/spring-petclinic | Official Git history | 10 | 23.33% | 23.33% |
| **Overall (macro)** | **Combined** | **50** | **26.67%** | **44.67%** |

Multi-production-file leave-one-out results:

| Repository | Eligible cases | Folds | Production Recall@10 | Production Recall@All |
|---|---:|---:|---:|---:|
| pytest-dev/pytest | 2 | 4 | 25.0% | 50.0% |
| pydata/xarray | 5 | 10 | 50.0% | 100.0% |
| fastapi/fastapi | 4 | 11 | 21.21% | 51.52% |
| spring-projects/spring-petclinic | 5 | 16 | 10.94% | 10.94% |
| **Overall (macro-fold)** | **16** | **41** | **24.59%** | **47.36%** |

The Spring PetClinic subset was run independently with the same evaluator and
current code. Its symbol found rate was 100%, mean index time was 332.9 ms,
mean query time was 1.53 ms, and mean resolved-call rate was 5.65%. The overall
rows above are case/fold-weighted macro metrics from the fixed 40-case baseline
and this fixed 10-case Java run.

Original 40-case current-code diagnostics:

- Symbol found rate: 97.5%
- Macro test-file Precision@10 / Precision@All: 3.18% / 2.91%
- Mean full candidate files per test case: 38.48
- Cases with at least one Top-10 test-file hit: 11/40
- Macro related-production-file Precision@10 / Precision@All: 4.8% / 4.24%
- Mean full candidate files per production fold: 57.68
- Production folds with at least one Top-10 hit: 10/25
- Mean impact query latency: 174.80 ms
- Mean full-index build time per historical snapshot: 4.43 s
- Mean indexed nodes / source files: 4,464 / 395
- Mean resolved-call rate: 26.58%
- Mean SQLite index size: 10.66 MB

This is a baseline, not a claim of production accuracy. Co-changed production
files are an observable proxy for related impact, not a complete semantic
ground truth. The weak FastAPI, Xarray, and pytest test-file results still
expose static-analysis gaps around decorators, dependency injection, dynamic
dispatch, hooks, and fixtures. Re-run the fixed suite after resolver changes
instead of replacing failures with hand-selected examples.
