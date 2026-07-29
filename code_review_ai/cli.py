from code_review_ai import qname
import argparse
import json
import sys

from code_review_ai.changes import detect_changed_symbols
from code_review_ai.config import load_config
from code_review_ai.db import connect, init_schema
from code_review_ai.impact import get_impact
from code_review_ai.indexer import rebuild


def _conn(db_path):
    conn = connect(db_path)
    init_schema(conn)
    return conn


def _add_common(sp):
    """Add --repo and --db flags to a subparser."""
    sp.add_argument("--repo", default=".")
    sp.add_argument("--db", default=".code-review-ai/index.db")


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="code-review-ai")
    sub = p.add_subparsers(dest="cmd", required=True)

    _add_common(sub.add_parser("rebuild"))
    s = sub.add_parser("query")
    _add_common(s)
    s.add_argument("--symbols", nargs="*")
    s.add_argument("--files", nargs="*")
    sp = sub.add_parser("search")
    _add_common(sp)
    sp.add_argument("query")
    sp = sub.add_parser("communities")
    _add_common(sp)
    sp.add_argument("--symbol", default=None)

    args = p.parse_args(argv)
    cfg = load_config(args.repo)
    cfg.repo_path = args.repo
    cfg.db_path = args.db
    conn = _conn(args.db)

    if args.cmd == "rebuild":
        stats = rebuild(cfg, conn)
        print(json.dumps({"nodes": stats.node_count, "edges": stats.edge_count,
                          "flows": stats.flow_count, "built_at": stats.built_at,
                          "timings_ms": stats.stage_timings}))
    elif args.cmd == "query":
        changed = detect_changed_symbols(cfg, symbols=args.symbols, files=args.files)
        print(json.dumps(get_impact(conn, changed)))
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
    return 0


if __name__ == "__main__":
    sys.exit(main())
