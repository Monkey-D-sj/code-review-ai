import argparse
import json
import sys
from pathlib import Path

from code_review_ai.changes import build_change_summary, detect_changed_symbols
from code_review_ai.agent_eval import (MODES, load_agent_cases,
                                       preflight_agent_eval,
                                       parse_agent_command, run_agent_eval,
                                       select_agent_cases)
from code_review_ai.full_agent_eval import (DEFAULT_FULL_EVAL_MODES,
                                            FULL_EVAL_MODES,
                                            load_full_agent_cases,
                                            preflight_full_agent_eval,
                                            rescore_full_agent_report,
                                            run_full_agent_eval,
                                            select_full_agent_cases)
from code_review_ai.config import load_config
from code_review_ai.db import connect, init_schema
from code_review_ai.graph import query_graph
from code_review_ai.export_graph import export as export_graph
from code_review_ai.impact import get_impact
from code_review_ai.testimpact import get_test_impact
from code_review_ai.deadcode import find_dead_code
from code_review_ai.indexer import rebuild
from code_review_ai.search import fts_search
from code_review_ai.installer import DEFAULT_SOURCE, install
from code_review_ai.update import sync, update_nodes_edges
from code_review_ai.context_planner import (
    DEFAULT_MAX_CHARS, plan_context, run_context_plan_eval,
)


def _conn(db_path):
    conn = connect(db_path)
    init_schema(conn)
    return conn


def _add_common(sp):
    """Add --repo and --db flags to a subparser."""
    sp.add_argument("--repo", default=".")
    sp.add_argument("--db", default=".code-review-ai/index.db")


def _run_install(args) -> int:
    """Register the MCP server with an AI tool. No repo/db needed."""
    res = install(platform=args.platform, source=args.source,
                  scope=args.scope, name=args.name)
    print(res.message)
    return 0 if res.success else 1


def _write_json(payload: dict, output_path: str | None) -> None:
    rendered = json.dumps(payload, indent=2, ensure_ascii=False)
    if output_path:
        from pathlib import Path
        path = Path(output_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(rendered + "\n", encoding="utf-8")
    else:
        print(rendered)


def _write_full_agent_routes(payload: dict, report_path: str,
                             work_dir: str) -> Path:
    """Write the automatic compact trace artifact beside an eval report."""
    from code_review_ai.full_agent_trace import render
    report = Path(report_path)
    output = report.with_name(f"{report.stem}-routes.md")
    output.write_text(
        render(payload, Path(work_dir) / "transcripts"), encoding="utf-8")
    return output


def _normalize_test_paths(files: list[str]) -> list[str]:
    """Normalize test file paths for shell consumption: forward slashes and
    no leading ``./`` so ``pytest $(test-impact --format paths)`` works on
    Linux and Windows runners alike."""
    normalized = []
    for file_path in files:
        path = file_path.replace("\\", "/")
        if path.startswith("./"):
            path = path[2:]
        normalized.append(path)
    return normalized


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="code-review-ai")
    sub = p.add_subparsers(dest="cmd", required=True)

    _add_common(sub.add_parser("rebuild"))
    s = sub.add_parser("query")
    _add_common(s)
    s.add_argument("--symbols", nargs="*")
    s.add_argument("--files", nargs="*")
    s = sub.add_parser("test-impact")
    _add_common(s)
    s.add_argument("--symbols", nargs="*")
    s.add_argument("--files", nargs="*")
    s.add_argument("--format", choices=["json", "paths"], default="json",
                   help="output format: json (default) or paths "
                        "(space-separated test files for `pytest $(...)`)")
    s = sub.add_parser("dead-code")
    _add_common(s)
    s.add_argument("--format", choices=["json", "text"], default="json",
                   help="output format (default: json)")
    s = sub.add_parser("summary")
    _add_common(s)
    s.add_argument("--symbols", nargs="*")
    s.add_argument("--files", nargs="*")
    cp = sub.add_parser("context-plan")
    _add_common(cp)
    cp.add_argument("--files", nargs="*")
    cp.add_argument("--max-chars", type=int, default=DEFAULT_MAX_CHARS)
    cp.add_argument("-o", "--out")
    s = sub.add_parser("query-graph")
    _add_common(s)
    s.add_argument("qualified_name")
    s.add_argument("--edge-kind", default="call")
    s.add_argument("--direction", default="both")
    sp = sub.add_parser("search")
    _add_common(sp)
    sp.add_argument("query")
    sp.add_argument("--limit", type=int, default=50,
                    help="max results (default: 50)")
    up = sub.add_parser("update")
    _add_common(up)
    sp = sub.add_parser("sync")
    _add_common(sp)
    hp = sub.add_parser("install-hooks")
    _add_common(hp)
    hp.add_argument("--launch", default="code-review-ai",
                    help="command the hook uses to run code-review-ai "
                         "(default: prefer PATH, fall back to uvx --from <source>)")
    hp.add_argument("--from", dest="source", default=DEFAULT_SOURCE,
                    help="package source for the uvx fallback launcher "
                         "(default: %(default)s)")
    hp.add_argument("--review", action="store_true",
                    help="also review each commit's change impact with an LLM "
                         "(post-commit hook only)")
    hp.add_argument("--platform", default="claude-code",
                    choices=["claude-code", "codex"],
                    help="AI platform running the review LLM; sets the default "
                         "review command (default: %(default)s)")
    hp.add_argument("--review-launch", default=None,
                    help="override the platform's default review command, e.g. "
                         "'codex exec'")
    hp.add_argument("--review-out", default=None,
                    help="review report path "
                         "(default: <repo>/.code-review-ai/last-review.md)")
    sp = sub.add_parser("communities")
    _add_common(sp)
    sp.add_argument("--symbol", default=None)
    gp = sub.add_parser("graph")
    _add_common(gp)
    gp.add_argument("-o", "--out", default="graph.html")
    gp.add_argument("-n", "--max-nodes", type=int, default=200)
    gp.add_argument("-m", "--mode", default="communities",
                    choices=["communities", "graph", "flow"])
    ae = sub.add_parser("agent-eval")
    _add_common(ae)
    ae.add_argument("--cases", required=True)
    ae.add_argument("--case-ids", nargs="+",
                    help="run only the selected case ids")
    ae.add_argument("--agent-command",
                    help="command that reads the eval prompt from stdin and "
                         "writes the required JSON object to stdout")
    ae.add_argument("--dry-run", action="store_true",
                    help="build contexts and validate symbols without calling an agent")
    ae.add_argument("--modes", nargs="+", choices=MODES, default=list(MODES))
    ae.add_argument("--repetitions", type=int, default=1)
    ae.add_argument("--workers", type=int, default=1,
                    help="concurrent agent processes (default: 1)")
    ae.add_argument("--timeout", type=int, default=300)
    ae.add_argument("--runs-dir", default=".code-review-ai/agent-eval")
    ae.add_argument("--repos-dir", default=".code-review-ai/external-repos",
                    help="cache for repositories referenced by canonical cases")
    ae.add_argument("-o", "--out")
    rc = sub.add_parser("agent-eval-route-check")
    _add_common(rc)
    rc.add_argument("--cases", required=True)
    rc.add_argument("--runs-dir", required=True)
    rc.add_argument("--repos-dir", default=".code-review-ai/external-repos")
    rc.add_argument("-o", "--out")
    aa = sub.add_parser("agent-eval-analyze")
    aa.add_argument("--report", required=True)
    aa.add_argument("-o", "--out")
    fe = sub.add_parser("full-agent-eval")
    fe.add_argument("--cases", required=True)
    fe.add_argument("--case-ids", nargs="+")
    fe.add_argument("--repos-dir", default=".code-review-ai/external-repos")
    fe.add_argument("--local-repo",
                    help="single local git repo used as the source for every "
                         "case (cases must have empty repo_url); built by its "
                         "build_repo.py if it has no history yet")
    fe.add_argument("--work-dir", default=".code-review-ai/full-agent-eval")
    fe.add_argument("--agent-command")
    fe.add_argument("--dry-run", action="store_true")
    fe.add_argument("--modes", nargs="+", choices=FULL_EVAL_MODES,
                    default=list(DEFAULT_FULL_EVAL_MODES))
    fe.add_argument("--hinted", action="store_true",
                    help="inject each case's hint prose into the prompt "
                         "(ablation arm); blind by default, because a hint "
                         "that names the affected callers removes the "
                         "traversal the graph tools exist to do")
    fe.add_argument("--repetitions", type=int, default=1)
    fe.add_argument("--workers", type=int, default=1)
    fe.add_argument("--timeout", type=int, default=600)
    fe.add_argument("-o", "--out")
    fr = sub.add_parser("full-agent-eval-rescore")
    fr.add_argument("--report", required=True)
    fr.add_argument("--cases", required=True)
    fr.add_argument("--transcripts", required=True)
    fr.add_argument("-o", "--out")
    pe = sub.add_parser("context-plan-eval")
    pe.add_argument("--cases", required=True)
    pe.add_argument("--case-ids", nargs="+")
    pe.add_argument("--repos-dir", default=".code-review-ai/external-repos")
    pe.add_argument("--work-dir", default=".code-review-ai/context-plan-eval")
    pe.add_argument("--max-chars", type=int, default=DEFAULT_MAX_CHARS)
    pe.add_argument("-o", "--out")
    xr = sub.add_parser("extract-review")
    xr.add_argument("debug", help="claude stream-json transcript to read")
    xr.add_argument("out", help="file to write the final answer to")
    tr = sub.add_parser("trace-review")
    tr.add_argument("debug", help="claude stream-json transcript to read")
    tr.add_argument("out", help="file to write the concise tool trace to")
    ft = sub.add_parser(
        "summarize-full-agent-trace",
        aliases=["eval-trace"],
        help="render compact complete routes from a full-agent-eval report",
    )
    ft.add_argument("report", help="full-agent-eval report JSON")
    ft.add_argument("--transcripts-root",
                    help="transcripts root used to recover each run cwd")
    ft.add_argument("-o", "--out", help="Markdown output (stdout if omitted)")
    ip = sub.add_parser("install")
    ip.add_argument("--platform", default="claude-code")
    ip.add_argument("--scope", default="user", choices=["user", "project", "local"])
    ip.add_argument("--from", dest="source", default=DEFAULT_SOURCE)
    ip.add_argument("--name", default="code-review-ai")

    args = p.parse_args(argv)

    if args.cmd == "install":
        return _run_install(args)
    if args.cmd == "extract-review":
        from code_review_ai.extract import extract_review
        ok = extract_review(args.debug, args.out)
        if ok:
            print(f"extracted review to {args.out}")
        else:
            print("error: no answer text found in debug log", file=sys.stderr)
        return 0 if ok else 1
    if args.cmd == "trace-review":
        from code_review_ai.extract import trace_review
        count = trace_review(args.debug, args.out)
        if count:
            print(f"wrote tool trace ({count} calls) to {args.out}")
        else:
            print("error: no tool calls found in debug log", file=sys.stderr)
        return 0 if count else 1
    if args.cmd in {"summarize-full-agent-trace", "eval-trace"}:
        from code_review_ai.full_agent_trace import summarize_file
        try:
            output = summarize_file(
                args.report, args.transcripts_root, args.out)
        except (OSError, ValueError, KeyError, json.JSONDecodeError) as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 1
        if not args.out:
            print(output, end="")
        return 0
    if args.cmd == "agent-eval-analyze":
        from code_review_ai.agent_eval_analysis import analyze_file
        try:
            payload = analyze_file(args.report, args.out)
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 1
        if not args.out:
            _write_json(payload, None)
        return 0
    if args.cmd == "full-agent-eval":
        try:
            cases = select_full_agent_cases(
                load_full_agent_cases(args.cases), args.case_ids)
            if args.dry_run:
                payload = preflight_full_agent_eval(
                    cases, args.repos_dir, args.work_dir,
                    local_repo=args.local_repo)
            else:
                if not args.agent_command:
                    raise ValueError("--agent-command is required unless --dry-run")
                payload = run_full_agent_eval(
                    cases, args.repos_dir, args.work_dir,
                    parse_agent_command(args.agent_command),
                    modes=tuple(args.modes), repetitions=args.repetitions,
                    timeout_seconds=args.timeout, workers=args.workers,
                    local_repo=args.local_repo, hinted=args.hinted)
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 1
        try:
            _write_json(payload, args.out)
            if args.out and not args.dry_run:
                route_path = _write_full_agent_routes(
                    payload, args.out, args.work_dir)
                print(f"wrote tool routes to {route_path}")
        except OSError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 1
        return 0
    if args.cmd == "full-agent-eval-rescore":
        try:
            payload = rescore_full_agent_report(
                args.report, load_full_agent_cases(args.cases), args.transcripts)
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 1
        _write_json(payload, args.out)
        return 0
    if args.cmd == "context-plan-eval":
        try:
            payload = run_context_plan_eval(
                args.cases, args.repos_dir, args.work_dir,
                case_ids=args.case_ids, max_chars=args.max_chars)
        except (OSError, ValueError, RuntimeError, json.JSONDecodeError) as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 1
        _write_json(payload, args.out)
        return 0

    # Config comes from the current project (cwd), matching the MCP server;
    # --repo/--db only select what gets analyzed, not where config is read.
    cfg = load_config()
    cfg.repo_path = args.repo
    cfg.db_path = args.db
    conn = _conn(args.db)

    if args.cmd == "agent-eval-route-check":
        from code_review_ai.agent_eval_analysis import route_check_analysis
        try:
            cases = load_agent_cases(args.cases)
            if any(case.source_commit is None for case in cases):
                rebuild(cfg, conn)
            payload = route_check_analysis(
                conn, cases, args.runs_dir, config=cfg,
                work_dir=str(Path(args.runs_dir) / ".route-check-snapshots"),
                repos_dir=args.repos_dir)
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 1
        _write_json(payload, args.out)
        return 0

    if args.cmd == "rebuild":
        stats = rebuild(cfg, conn)
        print(json.dumps({"nodes": stats.node_count, "edges": stats.edge_count,
                          "flows": stats.flow_count, "built_at": stats.built_at,
                          "timings_ms": stats.stage_timings}))
    elif args.cmd == "query":
        try:
            changed = detect_changed_symbols(cfg, symbols=args.symbols, files=args.files)
        except RuntimeError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 1
        print(json.dumps(get_impact(conn, changed)))
    elif args.cmd == "test-impact":
        try:
            changed = detect_changed_symbols(cfg, symbols=args.symbols, files=args.files)
        except RuntimeError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 1
        result = get_test_impact(conn, changed)
        if args.format == "paths":
            print(" ".join(_normalize_test_paths(result["test_files"])))
        else:
            print(json.dumps(result))
    elif args.cmd == "dead-code":
        payload = find_dead_code(conn, cfg)
        if args.format == "text":
            for symbol in payload["symbols"]:
                print(f"{symbol['file']}:{symbol['line']}\t{symbol['kind']}\t{symbol['qname']}")
            for file_entry in payload["files"]:
                print(f"FILE\t{file_entry['path']}\t{file_entry['qname']}"
                      f"\t{file_entry['symbol_count']} symbols")
        else:
            print(json.dumps(payload))
    elif args.cmd == "summary":
        try:
            payload = build_change_summary(cfg, conn,
                                           symbols=args.symbols, files=args.files)
        except RuntimeError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 1
        print(json.dumps(payload))
    elif args.cmd == "context-plan":
        try:
            payload = plan_context(
                cfg, conn, files=args.files, max_chars=args.max_chars)
        except (OSError, ValueError, RuntimeError) as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 1
        _write_json(payload, args.out)
    elif args.cmd == "query-graph":
        try:
            payload = query_graph(conn, args.qualified_name,
                                  edge_kind=args.edge_kind, direction=args.direction)
        except ValueError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 1
        print(json.dumps(payload))
    elif args.cmd == "search":
        for r in fts_search(conn, args.query, limit=args.limit):
            signature = f"  {r['signature']}" if r.get("signature") else ""
            print(f"{r['qname']}  {r['kind']}  {r['file']}:{r['line']}-{r['end_line']}{signature}")
    elif args.cmd == "communities":
        from code_review_ai.community import list_communities, get_community
        if args.symbol:
            print(json.dumps(get_community(conn, args.symbol), indent=2, ensure_ascii=False))
        else:
            for c in list_communities(conn):
                print(f"{c['id']}  {c['label']}  nodes={c['node_count']}  modularity={c['modularity']}")
    elif args.cmd == "update":
        print(json.dumps(update_nodes_edges(cfg, conn)))
    elif args.cmd == "sync":
        print(json.dumps(sync(cfg, conn)))
    elif args.cmd == "install-hooks":
        from code_review_ai.hooks import install_hooks
        for path in install_hooks(cfg.repo_path, cfg.db_path, args.launch,
                                  with_review=args.review,
                                  platform=args.platform,
                                  review_launch=args.review_launch,
                                  review_out=args.review_out,
                                  source=args.source):
            print(f"installed {path}")
    elif args.cmd == "graph":
        export_graph(args.db, args.out, args.max_nodes, args.mode)
    elif args.cmd == "agent-eval":
        try:
            cases = select_agent_cases(load_agent_cases(args.cases), args.case_ids)
            if args.dry_run:
                payload = preflight_agent_eval(cfg, conn, cases,
                                               modes=tuple(args.modes),
                                               repos_dir=args.repos_dir)
            else:
                if not args.agent_command:
                    raise ValueError("--agent-command is required unless --dry-run")
                payload = run_agent_eval(
                    cfg, conn, cases, parse_agent_command(args.agent_command),
                    args.runs_dir, modes=tuple(args.modes),
                    repetitions=args.repetitions,
                    timeout_seconds=args.timeout, workers=args.workers,
                    repos_dir=args.repos_dir,
                )
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 1
        _write_json(payload, args.out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
