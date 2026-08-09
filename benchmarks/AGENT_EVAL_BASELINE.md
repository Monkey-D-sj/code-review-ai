# Agentic Eval baseline

Date: 2026-08-09

This is a reproducible engineering baseline, not a production-accuracy claim.
The dataset contains only 10 reverse mutations from this repository; results
must not be generalized to natural pull requests or external projects.

## Experiment

- 10 mutations derived from real fixes in this repository.
- Four context modes: Diff Only, lexical Search, Graph, and Hybrid.
- Three independent model runs per case and mode: 120 runs total.
- Claude Sonnet alias; the provider reported `claude-sonnet-5` on successful runs.
- Same structured-output schema and `$0.35` per-run budget.
- Repository tools disabled. Models saw only the context prepared by the runner.
- Four concurrent workers; every run used a separate provider process and transcript.
- Gold matching requires the expected file and at least one case keyword.
- One Graph run reached the 300-second timeout and remains a failed run in the score.

The review prompt does not contain the gold finding or the case's source commit.
Provider failures are scored as zero rather than silently removed or retried.

## Dataset coverage

Cases cover missing tracked files, configuration scope, nested exclude globs,
benchmark test filtering, production files mistaken for tests, post-commit diff
bases, Python relative imports, `src/` module names, unsupported diff files,
and SQLite watcher thread affinity.

Preflight before model execution:

| Metric | Result |
|---|---:|
| Changed-symbol coverage | 10 / 10 (100%) |
| Diff mean / max characters | 672 / 958 |
| Search mean / max characters | 1,615 / 2,885 |
| Graph mean / max characters | 2,749 / 3,561 |
| Hybrid mean / max characters | 8,394 / 11,680 |
| Hybrid mean supplied files | 6.1 |

## Results

Confidence intervals use 5,000 case-clustered bootstrap samples: a bootstrap
draw samples cases with replacement and retains all three repetitions of each
sampled case. This avoids treating repeated runs of the same mutation as 30
fully independent tasks.

| Mode | Success | Precision (95% CI) | Recall (95% CI) | F1 (95% CI) | Stable case hits |
|---|---:|---:|---:|---:|---:|
| Diff Only | 30/30 | 41.5% (28.6–52.8) | 66.7% (50.0–80.0) | 48.6% (34.6–59.5) | 2/10 |
| Search Baseline | 30/30 | **53.9%** (38.3–70.6) | **83.3%** (70.0–96.7) | **62.1%** (48.2–77.1) | **6/10** |
| Graph Agent | 29/30 | 41.9% (23.9–60.8) | 60.0% (43.3–76.7) | 46.9% (30.0–66.6) | 3/10 |
| Hybrid Agent | 30/30 | 40.1% (25.0–57.2) | **83.3%** (70.0–96.7) | 51.3% (36.0–67.0) | **6/10** |

`Stable case hits` counts cases whose gold was found in all three repetitions.
Search and Hybrid were more stable than Diff/Graph, but Search had materially
better precision than Hybrid in this dataset.

## Paired comparison against Diff Only

Each candidate run is paired with the same case and repetition under Diff Only.

| Mode | Mean F1 delta (95% CI) | Mean recall delta (95% CI) | F1 win / tie / loss |
|---|---:|---:|---:|
| Search Baseline | +13.6 pp (-1.3 to +27.9) | +16.7 pp (0.0 to +33.3) | 10 / 14 / 6 |
| Graph Agent | -1.7 pp (-23.2 to +21.4) | -6.7 pp (-26.7 to +16.7) | 11 / 7 / 12 |
| Hybrid Agent | +2.7 pp (-19.7 to +26.3) | +16.7 pp (-3.3 to +40.0) | 11 / 10 / 9 |

All F1-delta intervals cross zero. The experiment does not establish a
statistically reliable F1 improvement over Diff Only. Search has the strongest
point estimate and is the only mode whose recall-delta interval does not go
below zero, but the lower bound is exactly zero.

## Cost and latency

| Mode | Total cost | Mean cost/run | Mean latency | Mean uncached input | Mean output |
|---|---:|---:|---:|---:|---:|
| Diff Only | $1.9933 | $0.0664 | 27.4s | 4,242 | 3,353 |
| Search Baseline | $2.3324 | $0.0777 | 31.6s | 4,601 | 3,999 |
| Graph Agent | $1.8366 | $0.0612 | 32.9s | 5,041 | 2,746 |
| Hybrid Agent | $4.1035 | $0.1368 | 54.0s | 7,053 | 6,904 |
| **Total** | **$10.2657** | — | — | — | — |

Graph's lower total is partly explained by the timed-out run. Hybrid cost about
76% more per run than Search and took about 71% longer, without improving F1.

## Honest interpretation

- Lexical Search is the best current default for these mostly local regressions.
- Pure call-graph context does not improve review quality on this dataset.
- Hybrid restores Search-level recall but introduces more extra findings and
  substantially higher cost. More context is not automatically better context.
- The call graph may still help cross-file behavior changes, deletion impact,
  routing, and dependency injection, but this dataset is too small and too
  locally weighted to test that claim.
- Precision may be underestimated because non-gold findings have not all been
  manually adjudicated. They remain false positives in the automated score to
  avoid selectively accepting plausible model output after seeing it.
- Reverse mutations are controlled and reproducible, but easier and less
  representative than naturally occurring PRs.

## Measurement defects found during dogfooding

1. Windows command parsing removed backslashes from executable paths.
2. Lexical search traversed `.venv` instead of tracked source files only.
3. Models self-reported tool calls even though tools were disabled.
4. Per-flow graph limits still produced oversized merged contexts.
5. Absolute graph paths wasted context budget.
6. `.json` names were partially counted as `.js` context files.
7. Claude cache tokens were initially merged into uncached input tokens.
8. Run-level bootstrap initially treated repetitions as independent cases;
   the final report uses case-clustered resampling.

All eight issues are fixed in the current runner. Earlier three-case JSON files
predate some fixes and must not be used for cost or confidence claims.

## Impact-routing offline validation

Offline validation of the "upgrade to get_impact only when risk is high"
routing policy against this 10-case baseline. For each case, `max_risk` is the
maximum `assess_symbol_risk` score over its changed symbols (0-100; all 10
symbols resolve in the current index, so none hit the unresolved-symbol
default of 50). Deltas are the mean F1 of Graph/Hybrid minus mean F1 of Diff
Only across the three repetitions, positive meaning the extra context helped.
Computed with `agent-eval-route-check` on the `agent-eval-real-10-r3`
transcripts (2026-08-09).

| Case | max_risk | graph_delta_f1 | hybrid_delta_f1 |
|---|---:|---:|---:|
| indexer-missing-tracked-file-regression | 35 | +0.4444 | +0.4444 |
| cli-external-repo-config-regression | 100 | -0.0778 | +0.1889 |
| nested-exclude-pattern-regression | 100 | +0.0555 | 0.0000 |
| benchmark-test-node-filter-regression | 40 | +0.6667 | +0.7778 |
| production-file-mistagged-as-test | 100 | -0.4444 | -0.5333 |
| post-commit-review-wrong-diff-base | 35 | -0.3889 | -0.2222 |
| python-relative-import-resolution-regression | 35 | -0.2222 | +0.2222 |
| src-layout-qualified-name-regression | 35 | +0.3000 | -0.2937 |
| unsupported-diff-file-crash | 40 | -0.4444 | -0.2222 |
| watcher-cross-thread-sqlite-regression | 10 | -0.0556 | -0.0889 |

Pearson correlation of delta F1 with max_risk: graph **-0.2136**, hybrid
**-0.1904**.

| Risk group | n | mean graph delta | mean hybrid delta | graph_positive |
|---|---:|---:|---:|---:|
| >= 60 (high) | 3 | -0.1556 | -0.1148 | 1 |
| < 60 (low) | 7 | +0.0429 | +0.0882 | 3 |

The data does not support upgrading to get_impact only when risk is high. The
>= 60 group has negative mean graph/hybrid deltas (-0.16 / -0.11) while the
< 60 group is slightly positive (+0.04 / +0.09), and both Pearson correlations
are negative (graph -0.21, hybrid -0.19) - if anything the direction is the
opposite of the hypothesis, with the extra context most often hurting on the
high-risk cases. With only 3 high-risk cases in a 10-case corpus, the group
differences sit within noise and should not be read as a reliable
anti-correlation either.

## Next experiment

- Expand to at least 30 cases from multiple repositories.
- Stratify local logic, cross-file impact, deletion, routing/DI, and test
  selection rather than reporting only one pooled score.
- Use Search as the primary baseline and add graph context only when a routing
  policy predicts cross-file impact.
- Manually adjudicate extra findings blind to context mode.
- Add a second provider/model to test provider-specific behavior.
