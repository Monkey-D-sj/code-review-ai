# P0 Edge Syntax Conformance Report

> Generated from `tests/p0/syntax-catalog.json` and the public P0 case files.
> Do not edit the metric rows by hand; regenerate them through `render_syntax_report`.

| Language | Edge | Covered | Partial | Missing | Dynamic | N/A | Static Coverage |
|---|---|---:|---:|---:|---:|---:|---:|
| java | call | 23 | 2 | 0 | 4 | 0 | 92.00% |
| java | contains | 8 | 3 | 0 | 0 | 0 | 72.73% |
| java | extends | 5 | 4 | 0 | 0 | 0 | 55.56% |
| java | implements | 5 | 4 | 0 | 0 | 0 | 55.56% |
| java | import | 6 | 7 | 0 | 0 | 0 | 46.15% |
| python | call | 22 | 2 | 0 | 8 | 0 | 91.67% |
| python | contains | 7 | 0 | 0 | 0 | 0 | 100.00% |
| python | extends | 3 | 3 | 0 | 1 | 0 | 50.00% |
| python | implements | 0 | 0 | 0 | 0 | 1 | 100.00% |
| typescript | call | 22 | 5 | 0 | 3 | 0 | 81.48% |
| typescript | contains | 11 | 0 | 0 | 0 | 0 | 100.00% |
| typescript | extends | 3 | 4 | 0 | 0 | 0 | 42.86% |
| typescript | implements | 3 | 3 | 0 | 0 | 0 | 50.00% |
| typescript | import | 14 | 11 | 0 | 2 | 0 | 56.00% |

## Totals

- Catalog items: 215
- Registered public cases: 125
- Dynamic Honesty evidence: 100.00%
- `not_applicable` is excluded from Static Coverage.
- `partial` is not counted as `covered`.

A syntax item can move to `covered` only after the catalog validator sees both a positive case and a boundary case, with complete public query neighbor sets. Dynamic and candidate targets remain outside resolved query results.
