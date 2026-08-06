from code_review_ai import qname
import argparse
import json
import sys

from code_review_ai.changes import build_change_summary, detect_changed_symbols
from code_review_ai.benchmark import load_cases, run_benchmark
from code_review_ai.config import load_config
from code_review_ai.db import connect, init_schema
from code_review_ai.graph import query_graph
from code_review_ai.export_graph import export as export_graph
from code_review_ai.impact import get_impact
from code_review_ai.indexer import rebuild
from code_review_ai.installer import DEFAULT_SOURCE, install
from code_review_ai.update import sync, update_nodes_edges


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
        Path(output_path).write_text(rendered + "\n", encoding="utf-8")
    else:
        print(rendered)


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="code-review-ai")
    sub = p.add_subparsers(dest="cmd", required=True)

    _add_common(sub.add_parser("rebuild"))
    s = sub.add_parser("query")
    _add_common(s)
    s.add_argument("--symbols", nargs="*")
    s.add_argument("--files", nargs="*")
    s = sub.add_parser("summary")
    _add_common(s)
    s.add_argument("--symbols", nargs="*")
    s.add_argument("--files", nargs="*")
    s = sub.add_parser("query-graph")
    _add_common(s)
    s.add_argument("qualified_name")
    s.add_argument("--edge-kind", default="call")
    s.add_argument("--direction", default="both")
    sp = sub.add_parser("search")
    _add_common(sp)
    sp.add_argument("query")
    up = sub.add_parser("update")
    _add_common(up)
    sp = sub.add_parser("sync")
    _add_common(sp)
    hp = sub.add_parser("install-hooks")
    _add_common(hp)
    hp.add_argument("--launch", default="code-review-ai")
    hp.add_argument("--review", action="store_true",
                    help="also review each commit's change impact with an LLM "
                         "(post-commit hook only)")
    hp.add_argument("--review-launch", default="claude -p",
                    help="LLM review command (default: 'claude -p')")
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
    bp = sub.add_parser("benchmark")
    _add_common(bp)
    bp.add_argument("--cases", required=True)
    bp.add_argument("--top-k", type=int, default=10)
    bp.add_argument("-o", "--out")
    ip = sub.add_parser("install")
    ip.add_argument("--platform", default="claude-code")
    ip.add_argument("--scope", default="user", choices=["user", "project", "local"])
    ip.add_argument("--from", dest="source", default=DEFAULT_SOURCE)
    ip.add_argument("--name", default="code-review-ai")

    args = p.parse_args(argv)

    if args.cmd == "install":
        return _run_install(args)

    # Config comes from the current project (cwd), matching the MCP server;
    # --repo/--db only select what gets analyzed, not where config is read.
    cfg = load_config()
    cfg.repo_path = args.repo
    cfg.db_path = args.db
    conn = _conn(args.db)

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
    elif args.cmd == "summary":
        try:
            payload = build_change_summary(cfg, conn,
                                           symbols=args.symbols, files=args.files)
        except RuntimeError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 1
        print(json.dumps(payload))
    elif args.cmd == "query-graph":
        try:
            payload = query_graph(conn, args.qualified_name,
                                  edge_kind=args.edge_kind, direction=args.direction)
        except ValueError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 1
        print(json.dumps(payload))
    elif args.cmd == "search":
        import fnmatch
        rows = conn.execute(
            "SELECT qualified_name,kind,file_path,start_line,end_line FROM nodes "
            "WHERE kind IN ('function','method','class')").fetchall()
        matches = [r for r in rows
                   if fnmatch.fnmatch(qname.short(r["qualified_name"]), args.query)]
        for r in matches:
            print(f"{r['qualified_name']}  {r['kind']}  {r['file_path']}:{r['start_line']}-{r['end_line']}")
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
                                  review_launch=args.review_launch,
                                  review_out=args.review_out):
            print(f"installed {path}")
    elif args.cmd == "graph":
        export_graph(args.db, args.out, args.max_nodes, args.mode)
    elif args.cmd == "benchmark":
        try:
            payload = run_benchmark(cfg, conn, load_cases(args.cases), args.top_k)
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 1
        _write_json(payload, args.out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
