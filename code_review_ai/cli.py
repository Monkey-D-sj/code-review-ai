"""The `code-review-ai` command: run a review, or install the tooling.

Everything else the graph can answer is an MCP tool (`code-review-ai-mcp`),
which is the interface the reviewer actually uses; the CLI stays small on
purpose. `install` writes user-scope skills/MCP registration, `review` runs the
built-in read-only review loop against the working tree.

The eval harness (`benchmarks/review_loop_case_compare.py`) drives the same
loop through `review_loop.runner` directly rather than through this CLI.
"""

import argparse
import functools
import json
import sys
import time
from dataclasses import dataclass
from pathlib import Path

from code_review_ai.changes import build_change_summary
from code_review_ai.config import load_config
from code_review_ai.db import connect, init_schema
from code_review_ai.installer import DEFAULT_SOURCE, install
from code_review_ai.update import sync

# Framing for the CLI's one-shot review; the loop's own policy (worksheet,
# evidence rules, read-only guard) is injected by review_loop.runner.
_CLI_REVIEW_PROMPT = "评审本次变更引入的具体回归，逐行核对 worksheet 中的变更符号。"

# Failures a user can act on (missing index, unreadable repo, bad arguments)
# rather than bugs: reported as `error: ...` with exit 1 instead of a traceback.
_USER_ERRORS = (OSError, ValueError, RuntimeError)

# Review distinguishes bad configuration (exit 2) from a failed run (exit 1).
_BAD_CONFIG = 2


@dataclass
class Context:
    """What a command that touches the index needs."""

    cfg: object
    conn: object


def _conn(db_path):
    conn = connect(db_path)
    init_schema(conn)
    return conn


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="code-review-ai")
    sub = parser.add_subparsers(dest="cmd", required=True)

    review = sub.add_parser("review",
                            help="run the built-in read-only review loop")
    review.add_argument("--repo", default=".")
    review.add_argument("--db", default=".code-review-ai/index.db")
    review.add_argument("--symbols", nargs="*")
    review.add_argument("--files", nargs="*")
    review.add_argument("--model", help="OpenAI-compatible model name")
    review.add_argument("--base-url",
                        help="OpenAI-compatible API base URL (optional for OpenAI)")
    review.add_argument("--api-key-env",
                        help="environment variable holding the API key")
    review.add_argument("--no-progress", action="store_true",
                        help="suppress live review progress on stderr")
    # Kept for compatibility: the TTY dashboard lived in the retired agent
    # package, so both flags now fall back to the same one-line progress.
    visual_group = review.add_mutually_exclusive_group()
    visual_group.add_argument("--visual", dest="visual", action="store_true",
                              help="accepted for compatibility (progress is one line)")
    visual_group.add_argument("--no-visual", dest="visual", action="store_false",
                              help="accepted for compatibility (progress is one line)")
    review.set_defaults(visual=None)
    review.add_argument("-o", "--out")

    install_parser = sub.add_parser("install")
    install_parser.add_argument("--platform", default="claude-code")
    install_parser.add_argument("--scope", default="user", choices=["user", "project", "local"])
    install_parser.add_argument("--from", dest="source", default=DEFAULT_SOURCE)
    install_parser.add_argument("--name", default="code-review-ai")
    install_parser.add_argument("--register-mcp", action="store_true",
                                help="also register the MCP server globally (default off: "
                                     "the review hook injects it on-demand, so everyday "
                                     "sessions carry no tool-description token cost)")
    return parser


def user_facing(command):
    """Turn ``_USER_ERRORS`` into ``error: ...`` + exit 1 for one command."""

    @functools.wraps(command)
    def run(args, ctx) -> int:
        try:
            return command(args, ctx)
        except _USER_ERRORS as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 1

    return run


def _write_json(payload: dict, output_path: str | None) -> None:
    rendered = json.dumps(payload, indent=2, ensure_ascii=False)
    if output_path:
        path = Path(output_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(rendered + "\n", encoding="utf-8")
    else:
        print(rendered)


# ---------------------------------------------------------------- review ----

_REVIEW_PROGRESS: dict[str, str] = {
    "full_rebuild_required": "索引版本或配置已变化，开始全量重建",
    "source_scan_started": "扫描可索引源文件…",
    "resolve_started": "解析调用关系：{symbols} 个符号",
    "clear_previous_index": "清理并压缩旧索引…",
    "communities_started": "计算代码社区…",
    "incremental_sync_started": "检查增量索引变更…",
    "incremental_sync_finished": "增量索引同步完成",
}


def _format_progress(event: str, data: dict) -> str:
    """One progress line for a review event (unknown events print verbatim)."""
    if event == "model_request_started":
        return f"模型第 {data['turn']} 轮推理中…"
    if event == "model_response_received":
        return f"模型第 {data['turn']} 轮响应：{data['tool_calls']} 个工具调用"
    if event == "source_scan_finished":
        return f"扫描完成：{data['files']} 个源文件"
    if event == "parse_started":
        return f"解析源文件：{data['files']} 个"
    if event == "parse_finished":
        return f"解析完成：{data['nodes']} 个符号，{data['raw_calls']} 个原始调用"
    if event == "resolve_finished":
        return f"调用图就绪：{data['edges']} 条边"
    if event == "write_graph_started":
        return f"写入图数据库：{data['nodes']} 个节点，{data['edges']} 条边"
    if event == "flows_started":
        return f"构建调用流：{data['call_edges']} 条调用边"
    if event == "rebuild_finished":
        return (f"索引重建完成：{data['nodes']} 个节点，{data['edges']} 条边，"
                f"{data['total_ms']} ms")
    if event == "pre_tool":
        return f"请求工具：{data.get('name')}"
    if event == "post_tool":
        return (f"工具完成：{data.get('name')} ({data.get('response_chars')} 字符，"
                f"{data.get('status')})")
    if event == "run_finished":
        outcome = "失败" if data.get("failure_reason") else "完成"
        return f"评审{outcome}：{data.get('finding_count')} 条发现"
    template = _REVIEW_PROGRESS.get(event)
    return template.format(**data) if template else event


def _review_progress(event: str, data: dict, *, quiet: bool) -> None:
    """The CLI's one-line progress, on stderr so stdout stays the JSON payload."""
    if quiet:
        return
    print(f"[review] {_format_progress(event, data)}", file=sys.stderr, flush=True)


def _review_hooks(quiet: bool):
    """Subscribe the progress printer to every event the review loop emits."""
    from code_review_ai.review_loop import (Hooks, POINT_MODEL_REQUEST_STARTED,
                                            POINT_MODEL_RESPONSE_RECEIVED,
                                            POINT_POST_TOOL, POINT_PRE_TOOL,
                                            POINT_RUN_FINISHED)
    hooks = Hooks()
    printer = functools.partial(_review_progress, quiet=quiet)
    for point in (POINT_MODEL_REQUEST_STARTED, POINT_MODEL_RESPONSE_RECEIVED,
                  POINT_PRE_TOOL, POINT_POST_TOOL, POINT_RUN_FINISHED):
        hooks.on(point, printer)
    return hooks


def _review_settings(args, cfg) -> tuple[str, str | None, str]:
    """Resolve the model, base URL and key variable a review run needs."""
    from code_review_ai.review_loop.runner import resolve_setting
    model_name = args.model or resolve_setting(cfg.repo_path, "CRAI_REVIEW_MODEL")
    if not model_name:
        raise ValueError("--model or CRAI_REVIEW_MODEL is required")
    base_url = args.base_url or resolve_setting(cfg.repo_path, "CRAI_REVIEW_BASE_URL")
    # Every built-in agent uses one conventional local key. A caller may still
    # explicitly select another process/.env variable with --api-key-env, but
    # no second indirection is needed in .env.
    return model_name, base_url, args.api_key_env or "OPENAI_API_KEY"


def _run_review_command(args, ctx, model_name, base_url, api_key_env, hooks) -> dict:
    """Sync the index, summarize the change, run the loop, return its payload."""
    from code_review_ai.review_loop.payload import loop_result_payload
    from code_review_ai.review_loop.runner import resolve_api_key, run_review
    started_at = time.perf_counter()
    resolve_api_key(ctx.cfg.repo_path, api_key_env)

    if not args.no_progress:
        print("[review] 正在同步代码索引…", file=sys.stderr, flush=True)
    # A review must never query a stale graph. sync performs the smallest
    # necessary update (or a full rebuild when required).
    sync(ctx.cfg, ctx.conn,
         progress=functools.partial(_review_progress, quiet=args.no_progress))
    if not args.no_progress:
        print("[review] 索引同步完成", file=sys.stderr, flush=True)

    summary = build_change_summary(ctx.cfg, ctx.conn,
                                   symbols=args.symbols, files=args.files)
    result = run_review(ctx.cfg, ctx.conn, prompt=_CLI_REVIEW_PROMPT, summary=summary,
                        model_name=model_name, base_url=base_url,
                        api_key_env=api_key_env, hooks=hooks)
    if not args.no_progress:
        elapsed = time.perf_counter() - started_at
        print(f"[review] 总耗时：{elapsed:.1f}s", file=sys.stderr, flush=True)
    return loop_result_payload(result, model_name)


@user_facing
def _cmd_review(args, ctx) -> int:
    try:
        model_name, base_url, api_key_env = _review_settings(args, ctx.cfg)
        payload = _run_review_command(args, ctx, model_name, base_url, api_key_env,
                                      _review_hooks(args.no_progress))
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return _BAD_CONFIG
    _write_json(payload, args.out)
    return 0 if payload.get("failure_reason") is None else 1


def _cmd_install(args, ctx) -> int:
    """Deploy skills/docs; optionally register the MCP server globally."""
    result = install(platform=args.platform, source=args.source,
                     scope=args.scope, name=args.name,
                     register_mcp=args.register_mcp)
    print(result.message)
    return 0 if result.success else 1


COMMANDS = {
    "review": _cmd_review,
    "install": _cmd_install,
}


def _context(args) -> Context:
    """Config comes from the current project (cwd), matching the MCP server;
    --repo/--db only select what gets analyzed, not where config is read."""
    cfg = load_config()
    cfg.repo_path = args.repo
    cfg.db_path = args.db
    return Context(cfg=cfg, conn=_conn(args.db))


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    # `install` writes user-scope files only: it must not need a project config
    # or create an index in whatever directory it is run from.
    if args.cmd == "install":
        return COMMANDS["install"](args, None)
    ctx = _context(args)
    try:
        return COMMANDS[args.cmd](args, ctx)
    finally:
        ctx.conn.close()


if __name__ == "__main__":
    sys.exit(main())
