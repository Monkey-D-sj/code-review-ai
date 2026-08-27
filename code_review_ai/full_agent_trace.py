"""Compact Markdown rendering for full-agent-eval tool traces."""

from __future__ import annotations

import json
import re
from pathlib import Path


def _load(path: Path) -> dict:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"expected a JSON object: {path}")
    return value


def _transcript_path(root: Path, run: dict) -> Path:
    return (root / str(run["case_id"]) / str(run["mode"])
            / f"run-{run['repetition']}.json")


def _repo_path(root: Path | None, run: dict) -> str | None:
    if root is None:
        return None
    path = _transcript_path(root, run)
    if not path.exists():
        return None
    value = _load(path).get("repo_path")
    return value if isinstance(value, str) else None


def _json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _relative(value: str, repo_path: str | None) -> str:
    if not repo_path:
        return value
    normalized = value.replace("\\", "/")
    root = repo_path.replace("\\", "/").rstrip("/")
    if normalized.lower().startswith(root.lower() + "/"):
        return normalized[len(root) + 1:]
    return value


def _bash(command: str, repo_path: str | None) -> str:
    if not repo_path:
        return command
    escaped = re.escape(repo_path.replace("/", "\\"))
    return re.sub(
        rf'^cd\s+["\']{escaped}["\']\s*&&\s*', "", command,
        count=1, flags=re.IGNORECASE,
    )


def _step(record: dict, repo_path: str | None) -> str:
    tool = str(record.get("tool", "?"))
    data = record.get("input")
    data = data if isinstance(data, dict) else {}
    if tool == "Read":
        path = data.get("file_path") or data.get("path") or "?"
        detail = _relative(str(path), repo_path)
        offset, limit = data.get("offset"), data.get("limit")
        if isinstance(offset, int) and isinstance(limit, int):
            detail += f":{offset}-{offset + limit - 1}"
        elif isinstance(offset, int):
            detail += f":{offset}-EOF"
        elif isinstance(limit, int):
            detail += f":1-{limit}"
    elif tool == "Bash":
        detail = _bash(str(data.get("command", "")), repo_path)
    elif tool.startswith("mcp__code-review-ai__"):
        detail = f"{tool.removeprefix('mcp__code-review-ai__')} {_json(data)}"
        tool = "MCP"
    else:
        detail = _json(data)
    suffix = f"response={record.get('response_chars', 0)} chars"
    if record.get("is_error") is True:
        suffix += ", ERROR"
    return f"{int(record.get('sequence', 0)):02d}. {tool} | {detail} | {suffix}"


def render(report: dict, transcripts_root: Path | None = None) -> str:
    """Render all report runs as compact, complete ordered tool routes."""
    lines = ["# Full-agent execution routes", ""]
    for run in report.get("runs", []):
        if not isinstance(run, dict):
            continue
        repo_path = _repo_path(transcripts_root, run)
        usage = run.get("usage") if isinstance(run.get("usage"), dict) else {}
        trace = run.get("tool_trace") if isinstance(run.get("tool_trace"), list) else []
        lines.extend([
            f"## {run.get('case_id')} / {run.get('mode')} / run-{run.get('repetition')}",
            "",
        ])
        if repo_path:
            lines.extend([f"- cwd: `{repo_path}`", ""])
        lines.extend([
            f"- score: P={run.get('precision')} R={run.get('recall')} F1={run.get('f1')}",
            f"- elapsed: {run.get('elapsed_ms')} ms; calls: {len(trace)}; files: {len(run.get('files_read', []))}",
            f"- access: read={run.get('read_calls', 0)}, search={run.get('search_calls', 0)}, bash={run.get('bash_calls', 0)}, unique_files={len(run.get('unique_files_touched', run.get('files_read', [])))}, unknown_file_access={run.get('unknown_file_access', False)}",
            f"- responses: native={run.get('native_response_chars', 0)} chars; mcp={run.get('mcp_response_chars', 0)} chars; total_tool_calls={run.get('total_tool_calls', run.get('tool_call_count', len(trace)))}",
            f"- tokens: input={usage.get('input_tokens', 0)}, cache_read={usage.get('cache_read_input_tokens', 0)}, output={usage.get('output_tokens', 0)}; cost=${usage.get('total_cost_usd', 0)}",
            "",
            "```text",
        ])
        lines.extend(_step(item, repo_path) for item in trace
                     if isinstance(item, dict))
        lines.extend(["```", ""])
    return "\n".join(lines)


def _merge_reports(reports: list[dict]) -> dict:
    """Concatenate runs from several reports into one merged report.

    The first report supplies schema / aggregate metadata; ``runs`` are the
    union (native + core + … across repeated eval invocations) so one HTML page
    can show every arm of a comparison.
    """
    merged = dict(reports[0])
    merged["runs"] = [run for report in reports for run in report.get("runs", [])]
    return merged


def summarize_file(report_path: str | Path | list[str | Path],
                   transcripts_root: str | Path | None = None,
                   out_path: str | Path | None = None,
                   as_html: bool = False) -> str:
    """Render one or more reports and optionally write the result.

    With multiple report paths the runs are concatenated so a single page
    (Markdown or HTML) shows every mode / repetition of a comparison.
    """
    paths = [report_path] if isinstance(report_path, (str, Path)) \
        else list(report_path)
    if not paths:
        raise ValueError("at least one report path is required")
    reports = [_load(Path(path)) for path in paths]
    root = Path(transcripts_root) if transcripts_root is not None else None
    if as_html:
        output = render_html(_merge_reports(reports), root)
    else:
        output = render(_merge_reports(reports), root)
    if out_path is not None:
        Path(out_path).write_text(output, encoding="utf-8")
    return output


_HTML_HEAD = """<!DOCTYPE html>
<html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Full-agent execution routes</title>
<style>
:root { color-scheme: light dark; --bg:#f6f8fa; --card:#fff; --fg:#24292f;
  --muted:#57606a; --border:#d0d7de; --accent:#0969da; --err:#cf222e; }
@media (prefers-color-scheme: dark) {
  :root { --bg:#0d1117; --card:#161b22; --fg:#e6edf3; --muted:#8b949e;
    --border:#30363d; --accent:#58a6ff; --err:#f85149; }
}
body { margin:0; background:var(--bg); color:var(--fg);
  font:14px/1.5 -apple-system,"Segoe UI",Roboto,Helvetica,Arial,sans-serif; }
header { padding:16px 24px; border-bottom:1px solid var(--border); background:var(--card); }
h1 { margin:0 0 4px; font-size:18px; } header p { margin:2px 0; color:var(--muted); }
main { max-width:1000px; margin:0 auto; padding:16px 24px 64px; }
.run { background:var(--card); border:1px solid var(--border); border-radius:8px;
  margin:12px 0; overflow:hidden; }
.run-head { display:block; padding:12px 16px 10px;
  cursor:pointer; user-select:none; }
.run-head:hover { background:var(--bg); }
.run-title { display:flex; align-items:center; gap:12px; }
.run-head h2 { margin:0; font-size:15px; }
.stats { display:flex; flex-wrap:wrap; gap:6px 18px; margin-top:8px; }
.stat { display:inline-flex; align-items:baseline; gap:4px; font-size:12px;
  color:var(--muted); white-space:nowrap; }
.stat b { color:var(--accent); font-weight:600; }
.stat b.good { color:#1a7f37; }
.stat b.bad { color:#cf222e; }
.stat .k { font-weight:400; }
.f1 { font-weight:600; padding:2px 8px; border-radius:10px; font-size:12px; }
.f1.ok { background:rgba(46,160,67,.15); color:#1a7f37; }
.f1.bad { background:rgba(248,81,73,.15); color:#cf222e; }
.caret { transition:transform .15s; color:var(--muted); font-size:10px; }
.run.open .caret { transform:rotate(90deg); }
.step.open .caret { transform:rotate(90deg); }
.steps { display:none; border-top:1px solid var(--border); }
.run.open .steps { display:block; }
.step { border-bottom:1px solid var(--border); }
.step:last-child { border-bottom:none; }
.step-head { display:flex; align-items:center; gap:10px; padding:10px 16px;
  cursor:pointer; user-select:none; }
.step-head:hover { background:var(--bg); }
.step-head .num { color:var(--muted); font-size:12px; width:28px; flex:none; }
.badge { flex:none; font-size:11px; padding:1px 8px; border-radius:10px;
  background:rgba(88,166,255,.12); color:var(--accent); font-weight:600; }
.badge.bash { background:rgba(187,128,9,.15); color:#9a6700; }
.badge.mcp { background:rgba(130,80,223,.15); color:#8957e5; }
.step-head .arg { font-family:ui-monospace,"Cascadia Code",Consolas,monospace;
  font-size:12.5px; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
.chars { margin-left:auto; color:var(--muted); font-size:11px; flex:none; }
.chars.err { color:var(--err); font-weight:600; }
.detail { display:none; padding:10px 16px 14px; background:var(--bg);
  border-top:1px dashed var(--border); }
.step.open .detail { display:block; }
.detail pre { margin:6px 0 0; font:12px/1.5 ui-monospace,"Cascadia Code",Consolas,monospace;
  white-space:pre-wrap; word-break:break-word; }
.detail .label { color:var(--muted); font-size:11px; font-weight:600;
  text-transform:uppercase; letter-spacing:.04em; }
@media (max-width:720px){ .step-head .arg{ white-space:normal; } }
</style></head><body>"""

_HTML_FOOT = """<script>
for (const el of document.querySelectorAll('.run-head,.step-head')) {
  el.addEventListener('click', () => el.parentElement.classList.toggle('open'));
}
</script></body></html>"""


def _unwrap_result_wrapper(parsed: object) -> object:
    """Unwrap MCP's ``{"result": "<json-string>"}`` envelope when present.

    The graph tools return a JSON string, which the MCP transport and claude
    both wrap in ``text``/``result`` layers; the route viewer should show the
    inner payload, not a quoted string, so pretty-printing stays readable.
    """
    for _ in range(3):
        if not isinstance(parsed, dict) or len(parsed) != 1:
            break
        only = next(iter(parsed.values()))
        if not isinstance(only, str):
            break
        try:
            parsed = json.loads(only)
        except (ValueError, TypeError):
            break
    return parsed


def _pretty_json(value: str) -> str:
    """Pretty-print a JSON string for display; return it verbatim if not JSON."""
    try:
        parsed = json.loads(value)
    except (ValueError, TypeError):
        return value
    return json.dumps(_unwrap_result_wrapper(parsed), ensure_ascii=False,
                      indent=2)


def _html_step(record: dict, repo_path: str | None) -> str:
    """One collapsible tool step: title = operation + formatted argument."""
    tool = str(record.get("tool", "?"))
    data = record.get("input")
    data = data if isinstance(data, dict) else {}
    if tool == "Read":
        badge, arg = "READ", ""
        path = data.get("file_path") or data.get("path") or "?"
        arg = _relative(str(path), repo_path)
        offset, limit = data.get("offset"), data.get("limit")
        if isinstance(offset, int) and isinstance(limit, int):
            arg += f" · lines {offset}-{offset + limit - 1}"
        elif isinstance(offset, int):
            arg += f" · line {offset}"
        elif isinstance(limit, int):
            arg += f" · first {limit} lines"
        badge_cls = ""
    elif tool == "Bash":
        badge, badge_cls = "BASH", " bash"
        arg = _bash(str(data.get("command", "")), repo_path)
    elif tool.startswith("mcp__code-review-ai__"):
        badge, badge_cls = "MCP", " mcp"
        arg = f"{tool.removeprefix('mcp__code-review-ai__')}"
        if data:
            arg += " · " + _json(data)
    elif tool in {"Glob", "Grep"}:
        badge, badge_cls = tool.upper(), ""
        arg = _json(data)
    else:
        badge, badge_cls = tool.upper(), ""
        arg = _json(data)
    chars = record.get("response_chars", 0)
    err = record.get("is_error") is True
    chars_html = f'{chars} chars' + (" · ERROR" if err else "")
    response = record.get("response")
    is_graph = tool.startswith("mcp__code-review-ai__")
    if is_graph and isinstance(response, str) and response:
        # Graph tools: show the complete returned JSON, pretty-printed.
        response_html = f"<pre>{_html_escape(_pretty_json(response))}</pre>"
    else:
        response_html = f"<pre>{_html_escape(chars_html)}</pre>"
    detail = (f"<div class='detail'><div class='label'>input</div>"
              f"<pre>{_html_escape(_json(data))}</pre>"
              f"<div class='label' style='margin-top:8px'>response</div>"
              f"{response_html}</div>")
    return (f"<div class='step open'><div class='step-head'>"
            f"<span class='num'>{int(record.get('sequence', 0)):02d}</span>"
            f"<span class='badge{badge_cls}'>{badge}</span>"
            f"<span class='arg'>{_html_escape(arg)}</span>"
            f"<span class='chars{' err' if err else ''}'>{chars_html}</span>"
            f"<span class='caret'>▶</span></div>{detail}</div>")


def _stat(key: str, value: str, tone: str = "") -> str:
    cls = f" class='{tone}'" if tone else ""
    return (f"<span class='stat'><span class='k'>{_html_escape(key)}</span>"
            f"<b{cls}>{_html_escape(value)}</b></span>")


def _html_run(run: dict, repo_path: str | None) -> str:
    usage = run.get("usage") if isinstance(run.get("usage"), dict) else {}
    trace = run.get("tool_trace") if isinstance(run.get("tool_trace"), list) else []
    cost = usage.get("total_cost_usd")
    cost_str = f"${cost:.3f}" if isinstance(cost, (int, float)) else "—"
    f1 = run.get("f1")
    f1_cls = "ok" if f1 == 1.0 else ("bad" if f1 == 0.0 else "")
    files = len(run.get("unique_files_touched", run.get("files_read", [])))
    elapsed_s = (run.get("elapsed_ms") or 0) / 1000
    elapsed_tone = "bad" if elapsed_s > 60 else "good"
    title = (f"{run.get('case_id')} / {run.get('mode')} / run-{run.get('repetition')}")
    stats = (
        _stat("P", f"{run.get('precision')}") +
        _stat("R", f"{run.get('recall')}") +
        f"<span class='f1 {f1_cls}'>F1 {f1}</span>" +
        _stat("elapsed", f"{elapsed_s:.1f}s", elapsed_tone) +
        _stat("calls", f"{len(trace)}") +
        _stat("files", f"{files}") +
        _stat("read", f"{run.get('read_calls', 0)}") +
        _stat("search", f"{run.get('search_calls', 0)}") +
        _stat("bash", f"{run.get('bash_calls', 0)}") +
        _stat("tokens in", f"{usage.get('input_tokens', 0)}") +
        _stat("tokens out", f"{usage.get('output_tokens', 0)}") +
        _stat("cost", cost_str)
    )
    steps = "".join(_html_step(item, repo_path) for item in trace
                    if isinstance(item, dict))
    return (f"<section class='run open'><div class='run-head'>"
            f"<div class='run-title'><span class='caret'>▶</span>"
            f"<h2>{_html_escape(title)}</h2></div>"
            f"<div class='stats'>{stats}</div>"
            f"</div><div class='steps'>{steps}</div></section>")


def _html_escape(value: str) -> str:
    return (value.replace("&", "&amp;").replace("<", "&lt;")
            .replace(">", "&gt;").replace('"', "&quot;"))


def render_html(report: dict, transcripts_root: Path | None = None) -> str:
    """Render all runs as a collapsible HTML page (no external deps)."""
    body: list[str] = []
    for run in report.get("runs", []):
        if not isinstance(run, dict):
            continue
        repo_path = _repo_path(transcripts_root, run)
        body.append(_html_run(run, repo_path))
    head = (f"<header><h1>Full-agent execution routes</h1>"
            f"<p>{len(report.get('runs', []))} runs · click any row to collapse</p>"
            f"</header><main>")
    return _HTML_HEAD + head + "".join(body) + "</main>" + _HTML_FOOT


def render_html_file(report_path: str | Path,
                     transcripts_root: str | Path | None = None,
                     out_path: str | Path | None = None) -> str:
    """Render a report to self-contained HTML and optionally write it."""
    root = Path(transcripts_root) if transcripts_root is not None else None
    output = render_html(_load(Path(report_path)), root)
    if out_path is not None:
        Path(out_path).write_text(output, encoding="utf-8")
    return output
