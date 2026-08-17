# Full-project Agentic Eval: real repositories

Date: 2026-08-10

This experiment evaluates the installed project on its online review path, not
only precomputed graph context. It is a reproducible engineering result, not a
broad production-accuracy claim.

`agentic-eval-real-repos.json` is the canonical manifest for both
`agent-eval` (controlled context injection) and `full-agent-eval` (online tool
use). Both evaluators now consume the same twelve mutations, tasks, and gold
findings. Six additions deliberately require cross-module, lifecycle, ORM, or
database context; the older six-case results below predate that expansion and
the cross-evaluator alignment.

## Experiment

- Five public repositories and three languages: Pallets itsdangerous and
  FastAPI (Python), sindresorhus/p-limit (JavaScript), and Google Gson plus
  Spring PetClinic (Java).
- Twelve reverse mutations derived from real bug-fix commits, with selected
  production files restored to the fix commit's parent while fixed tests remain
  available for inspection.
- Two paired modes: Native Agent (`Read`, `Glob`, `Grep`) and Full Project Agent
  (the same native tools plus this project's MCP server).
- Three independent runs per case and mode: 36 Claude calls total.
- Claude Sonnet alias; successful runs reported `claude-sonnet-5`.
- `$1.00` per-run ceiling, 600-second timeout, four concurrent workers.
- Bare Claude sessions disable user plugins, hooks, memory, and unrelated MCP
  configuration so both modes run in the same controlled environment.
- The current evaluator gives both modes the exact same review policy: classify
  whether each change is self-contained, inspect upstream callers first for
  non-local changes, inspect downstream callees only when data/call semantics
  require it, and check relevant tests and integration boundaries. Native obtains
  that evidence with `Read`/`Glob`/`Grep`; Full Project may additionally use MCP.

The online lifecycle now matches the product architecture:

1. Each historical worktree gets one graph index before any Agent timer starts.
2. MCP startup reuses that index and skips startup sync and the file watcher.
3. `rebuild_index` is unavailable to the evaluated Agent.
4. Full Project starts with `get_change_summary`, uses `query_graph` when context
   is needed, and calls `get_impact` only when local graph evidence leaves the
   blast radius uncertain or the change has important cross-boundary semantics.
5. Index setup latency is reported separately and is not included in Agent
   `elapsed_ms`, tokens, or model cost.

In the historical six-case run, all 36 calls succeeded. Every Full Project run
used project MCP tools. Across 18
Full Project runs, `get_change_summary` was used 18 times, `query_graph` 14,
`get_impact` 9, `get_test_impact` 9, and `search_symbol` 13. No run called
`rebuild_index`.

All twelve fixes are traceable to their upstream commits:

- itsdangerous: [`b00d8fd`](https://github.com/pallets/itsdangerous/commit/b00d8fd9dc3d74bdafb6b90b691bbb9616cff835),
  [`ce5e2cd`](https://github.com/pallets/itsdangerous/commit/ce5e2cd0afebadb5dd732ee1c151824a0de8b5d4)
- p-limit: [`ef37eb2`](https://github.com/sindresorhus/p-limit/commit/ef37eb2f372d385883d113803c98ba0bf3828ad1),
  [`ad8afe6`](https://github.com/sindresorhus/p-limit/commit/ad8afe6f46429e726b32fdedf063c553ebcb0196)
- Gson: [`f4d371d`](https://github.com/google/gson/commit/f4d371d29c04066dbe7fdb31f642831f9c7f40cd),
  [`c395dd1`](https://github.com/google/gson/commit/c395dd1fdf2590c575965208f1b479dd76a6c926)
- FastAPI: [`d623544`](https://github.com/fastapi/fastapi/commit/d62354434b2e508fe89024213b220ca8e67dea5e),
  [`d86c474`](https://github.com/fastapi/fastapi/commit/d86c47477e4d91b5e1f07973b3437908558a8b4b),
  [`ad03e11`](https://github.com/fastapi/fastapi/commit/ad03e117c0010a563067740c97cb7ab011cb5174),
  [`65e42bd`](https://github.com/fastapi/fastapi/commit/65e42bd5eca657daf97c6762b9632e7c2cb3317a)
- Spring PetClinic: [`e0db9b1`](https://github.com/spring-projects/spring-petclinic/commit/e0db9b184e028d41bcb626f3cbf03a942f67e104),
  [`e765e3f`](https://github.com/spring-projects/spring-petclinic/commit/e765e3ffe16002beb30c6a5c6f06409816259bf4)

## Gold labels

The current twelve-case manifest freezes eighteen gold findings, including
alternative root-cause files for cross-module defects. The historical v2 run
used nine of those findings. Those labels were refined
after the legacy guided experiment by splitting multi-part production fixes
using upstream diffs, tests, and commit descriptions. The v2 model outputs did
not cause further label changes, but the earlier refinement remains a benchmark
adaptation risk. A larger benchmark should preregister blinded labels.

## Online v2 results

> Historical-result note: the table below was produced before the shared review
> policy was introduced. Its Full Project prompt contained more explicit review
> strategy than its Native prompt, so the F1 difference cannot be attributed to
> MCP context alone. Rerun both paired modes before treating it as the new fair
> baseline.

Confidence intervals use 5,000 case-clustered bootstrap samples. Each draw
samples cases and retains all three repetitions, avoiding the false assumption
that 18 runs represent 18 independent defects.

| Mode | Success | Precision (95% CI) | Recall (95% CI) | F1 (95% CI) | Stable cases |
|---|---:|---:|---:|---:|---:|
| Native Agent | 18/18 | 85.2% (70.4-96.3) | **100.0%** (100.0-100.0) | 90.4% (80.4-97.8) | **6/6** |
| Full Project Agent | 18/18 | **88.0%** (74.1-100.0) | 97.2% (91.7-100.0) | **91.3%** (81.3-100.0) | 5/6 |

Paired against Native Agent:

| Comparison | F1 delta (95% CI) | Recall delta (95% CI) | Win / tie / loss |
|---|---:|---:|---:|
| Full Project - Native | +0.9 pp (-9.8 to +8.5) | -2.8 pp (-8.3 to 0.0) | 3 / 12 / 3 |

The point estimate shows slightly higher precision and F1, but the interval is
wide and crosses zero. This six-case experiment does not establish a quality
improvement. One Full Project repetition missed a gold finding, so the current
routing policy also does not preserve recall perfectly.

## Per-case macro F1

| Case | Native | Full Project | Full runs using `get_impact` |
|---|---:|---:|---:|
| itsdangerous payload forwarding | 0.933 | 1.000 | 3/3 |
| itsdangerous unsafe separator | 0.933 | 0.700 | 1/3 |
| p-limit detached map | 0.889 | 1.000 | 0/3 |
| p-limit async context | 1.000 | 1.000 | 3/3 |
| Gson duplicate null map key | 0.667 | 0.778 | 0/3 |
| Gson GraphAdapterBuilder reuse | 1.000 | 1.000 | 2/3 |

This is evidence that `get_impact` is genuinely conditional: only 9/18 Full
Project runs used it, including none of the detached-map and duplicate-null-key
runs. Improvements also occurred without full impact expansion.

## Online cost, latency, and context use

| Mode | Total cost | Mean/run | Mean latency | Mean files read | Mean actual tool calls |
|---|---:|---:|---:|---:|---:|
| Native Agent | $4.3184 | $0.2399 | 84.9s | 4.17 | 6.61 |
| Full Project Agent | $4.4468 | $0.2470 | 86.8s | **3.11** | 8.44 |

Compared with Native Agent, Full Project:

- cost 3.0% more per run;
- was 2.3% slower;
- read 25.3% fewer files;
- increased F1 by 0.9 points and precision by 2.8 points, while recall decreased
  by 2.8 points.

The corrected online measurement removes the legacy benchmark's roughly 26%
cost and latency penalty. The project now narrows repository exploration, but
MCP round trips and returned context still slightly outweigh the saved file
reads in model cost and wall time.

## Index setup outside Agent timing

| Repository/case class | Cold setup observed |
|---|---:|
| itsdangerous | 0.62-0.71s |
| p-limit | 0.48-0.63s |
| Gson | 4.14-4.55s |

The report marks every setup record with `timed_with_agent: false`. Production
uses incremental sync, so these cold numbers are onboarding/setup measurements,
not online review latency.

## What this establishes

- The complete MCP product can be used by a real Agent against prebuilt indexes
  in isolated Python, JavaScript, and Java repositories.
- Changed-symbol detection found all 8/8 preflight symbols.
- The Agent adopted MCP in 18/18 runs without forced `get_impact`; only 9/18
  chose full impact expansion.
- Graph-guided review reduced files read by about 25% while keeping cost and
  latency within roughly 3% of Native Agent in this small dataset.
- Quality is effectively inconclusive at six cases: the F1 interval includes
  meaningful gains and losses.

## Legacy guided baseline

The 2026-08-09 run forced `rebuild_index`, `get_change_summary`, and
`get_impact` inside every Full Project Agent session. It reported F1 93.5% vs
89.5%, but cost and latency were about 26% higher. That run remains useful as a
guided-tool-use experiment, but its performance numbers do not represent the
current online product lifecycle and should not be used in resume claims.

## Limitations and next experiment

- Only six selected reverse mutations from three repositories.
- Fixed tests remain in the checkout and can make intended behavior easier to
  discover than in a natural pull request.
- Gold labels were refined after the legacy run, although frozen before v2.
- One provider/model family was used.
- User plugins are now isolated, but provider nondeterminism remains.
- The benchmark needs 30+ blinded cases, natural PRs with incomplete tests, and
  explicit local-versus-cross-file strata before making accuracy claims.

Machine-readable artifacts:

- `benchmark-results/full-agent-eval-online-v2-r3.json`
- `benchmark-results/full-agent-eval-online-v2-r3-analysis.json`
- `benchmark-results/full-agent-eval-online-v2-preflight.json`

## Online v3 results (deepseek-v4-flash)

> **Model note — do not compare against v2.** This run used the session-default
> `deepseek-v4-flash` provider, not the `claude-sonnet-5` alias of v2. The
> tables below are a self-contained Native-vs-Full-Project comparison under one
> model; absolute F1 is much lower than v2 because the underlying model is much
> weaker. Only the paired delta and tool-usage patterns are interpretable.

Setup identical to the methodology above: 12 cases × 2 modes × 3 repetitions =
72 runs, 4 concurrent workers, 600-second timeout, `$1.00` per-run budget
ceiling, provider failures scored as zero without retry. Index setup outside
agent timing.

Confidence intervals use 5,000 case-clustered bootstrap samples (each draw
samples cases and retains all three repetitions).

| Mode | Success | Precision (95% CI) | Recall (95% CI) | F1 (95% CI) |
|---|---:|---:|---:|---:|
| Native Agent | 27/36 | 51.8% (29.1-73.9) | 58.3% (34.7-80.6) | 53.0% (29.5-74.3) |
| Full Project Agent | 25/36 | **58.7%** (38.2-78.4) | **68.1%** (47.2-86.1) | **61.4%** (40.4-81.0) |

Paired against Native Agent:

| Comparison | F1 delta (95% CI) | Win / tie / loss |
|---|---:|---:|
| Full Project - Native | +8.5 pp (-8.2 to +26.9) | 4 / 5 / 3 |

Full Project scores higher on precision and recall under the same model and
reads fewer files to do it, but the interval crosses zero (positive in 84% of
bootstrap samples), so this run does not establish a quality improvement.
Three cases regressed under Full Project (`gson-graph-adapter-builder-reuse`,
`itsdangerous-unsafe-separator`, `spring-petclinic-owner-scoped-pet-uniqueness`),
and two fastapi cases scored zero in both modes.

### Failure attribution (20/72)

Failures are scored as zero per methodology; Full Project carried two more than
Native, so its F1 advantage is if anything conservative:

| Cause | Count |
|---|---:|
| `$1.00` budget ceiling hit mid-run | 11 |
| 600s timeout | 6 |
| `claude.exe` not on PATH (transient, Windows) | 3 |

### Tool adoption (Full Project, 36 runs)

`get_change_summary` 26 · `search_symbol` 24 · `query_graph` 22 ·
`get_test_impact` 22 · `get_impact` 20 · `get_symbol_detail` 9 ·
`get_community` 2 · `list_entry_points` 2. **`rebuild_index` was never called.**

### Per-case macro F1

| Case | Native | Full Project | Full runs using `get_impact` |
|---|---:|---:|---:|
| fastapi-frontend-dependency-response-propagation | 0.000 | **0.667** | 2/3 |
| fastapi-include-router-stream-item-type | 0.000 | **0.444** | 2/3 |
| fastapi-nested-annotated-sequence | 0.000 | 0.000 | 0/3 |
| fastapi-validation-alias-pipeline | 0.000 | 0.000 | 0/3 |
| gson-duplicate-null-map-key | 0.778 | 0.778 | 1/3 |
| gson-graph-adapter-builder-reuse | **0.889** | 0.667 | 2/3 |
| itsdangerous-load-payload-forwarding | 0.667 | **1.000** | 3/3 |
| itsdangerous-unsafe-separator | **0.822** | 0.500 | 3/3 |
| p-limit-async-context | 1.000 | 1.000 | 2/3 |
| p-limit-detached-map | 0.556 | **1.000** | 2/3 |
| spring-petclinic-owner-details-lazy-loading | 1.000 | 1.000 | 3/3 |
| spring-petclinic-owner-scoped-pet-uniqueness | **0.648** | 0.315 | 0/3 |

The Full Project advantage concentrates in the cross-module cases this product
targets (`fastapi-frontend`, `fastapi-include-router`, `itsdangerous-load`,
`p-limit-detached`), but `get_impact` use is not a reliable success signal: the
biggest regression (`petclinic-uniqueness`) never used it, while the two
zero-zero fastapi cases varied.

### Online cost, latency, and context use

| Mode | Total cost | Mean/run | Mean latency | Mean files read | Mean tool calls | Mean tokens/run |
|---|---:|---:|---:|---:|---:|---:|
| Native Agent | $9.06 | $0.252 | 210.2s | 5.36 | 6.69 | 17,631 |
| Full Project Agent | $13.50 | $0.375 | 251.3s | **3.94** | 9.97 | 27,628 |

Compared with Native Agent, Full Project cost 49% more per run and was 20%
slower (latency inflated by the same failure mix on both sides), while reading
26% fewer files. The extra MCP round trips and returned context cost more than
the saved file reads — the same trade-off v2 measured, at a larger scale.

### What this establishes

- The complete MCP product runs end-to-end against prebuilt indexes on all
  twelve real cases under a weak model, with `rebuild_index` never invoked and
  the routing policy genuinely conditional (`get_impact` used in 20/36 runs).
- Under one model, Full Project narrowed repository exploration (3.94 vs 5.36
  files read) with directionally higher precision and recall, but the delta is
  not significant at 3 repetitions.
- v3 is not comparable to v2: different model, different absolute F1 scale.
  Rerun both paired modes on `claude-sonnet-5` with more repetitions before
  judging the routing policy against the published baseline.

Machine-readable artifact:

- `.code-review-ai/full-agent-eval/report-v3.json` (gitignored runtime artifact)
