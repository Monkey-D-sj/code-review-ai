"""Append-only terminal timeline for a review-agent run."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from time import perf_counter

from rich.console import Console
from rich.rule import Rule
from rich.text import Text


@dataclass
class ReviewProgressDisplay:
    """Print permanent phase and model-turn entries instead of redrawing a panel."""

    model_name: str
    model_turn: int = 0
    tool_calls: int = 0
    findings: int = 0
    failed: bool = False
    active_model_call: bool = False
    completed_steps: int = 0
    _started_at: float = field(default_factory=perf_counter)
    _console: Console = field(default_factory=lambda: Console(stderr=True),
                                       init=False, repr=False)

    def __enter__(self) -> "ReviewProgressDisplay":
        self._console.print(Rule("代码评审 Agent", style="cyan"))
        self._console.print(f"[dim]模型：{self.model_name}[/]")
        return self

    def __exit__(self, *args) -> None:
        return None

    def on_event(self, event: str, data: dict[str, object]) -> None:
        if event == "model_request_started":
            self.model_turn = int(data["turn"])
            self.active_model_call = True
            self._console.print(Rule(f"模型第 {self.model_turn} 轮", style="blue"))
            self._console.print(f"  [blue]→ 请求 {self.model_name} 推理[/]")
            return
        if event == "model_response_received":
            self.model_turn = int(data["turn"])
            self.active_model_call = False
            self.completed_steps = max(self.completed_steps, 3)
            self._console.print(
                f"  [green]← 第 {self.model_turn} 轮响应："
                f"{data['tool_calls']} 个工具调用[/]")
            return
        if event == "tool_requests":
            calls = data.get("calls")
            if not isinstance(calls, list):
                calls = [{"name": name, "args": {}} for name in data["names"]]
            self.tool_calls += len(calls)
            for call in calls:
                if not isinstance(call, dict):
                    continue
                name = str(call.get("name", "unknown"))
                args = call.get("args", {})
                parameters = json.dumps(args, ensure_ascii=False,
                                        separators=(",", ":"), default=str)
                self._console.print(Text(f"  ├─ 工具请求：{name}", style="yellow"))
                self._console.print(Text(f"  │  参数：{parameters}", style="dim"))
            return
        if event == "tool_completed":
            self._console.print(
                f"  [green]└─ 工具完成：{data['name']} "
                f"({data['response_chars']} 字符)[/]")
            return
        if event == "finished":
            self.findings = int(data["findings"])
            self.failed = bool(data["failed"])
            self.completed_steps = 4
            elapsed = perf_counter() - self._started_at
            title = "评审失败" if self.failed else "评审完成"
            color = "red" if self.failed else "green"
            self._console.print(Rule(title, style=color))
            self._console.print(
                f"[{color}]发现：{self.findings}；动作工具：{self.tool_calls}；"
                f"耗时：{elapsed:.1f}s[/]")
            return

        label = self._phase_label(event, data)
        if label is not None:
            if event in {"incremental_sync_finished", "rebuild_finished"}:
                self.completed_steps = max(self.completed_steps, 1)
            elif event == "summary_ready":
                self.completed_steps = max(self.completed_steps, 2)
            self._console.print(f"[dim]• {label}[/]")

    @staticmethod
    def _phase_label(event: str, data: dict[str, object]) -> str | None:
        labels = {
            "full_rebuild_required": "索引版本或配置变化，执行全量重建",
            "source_scan_started": "扫描可索引源文件",
            "source_scan_finished": f"扫描完成：{data.get('files', '?')} 个源文件",
            "parse_started": f"解析 {data.get('files', '?')} 个源文件",
            "parse_finished": f"解析完成：{data.get('nodes', '?')} 个符号",
            "resolve_started": "解析调用关系",
            "resolve_finished": f"调用图完成：{data.get('edges', '?')} 条边",
            "clear_previous_index": "清理旧索引",
            "write_graph_started": "写入图数据库",
            "flows_started": "构建调用流",
            "communities_started": "计算代码社区",
            "rebuild_finished": "索引重建完成",
            "incremental_sync_started": "检查增量索引变更",
            "incremental_sync_finished": "增量索引同步完成",
            "summary_ready": f"变更摘要：{data.get('changed_symbols', '?')} 个符号",
            "agent_started": "准备发送评审上下文",
        }
        return labels.get(event)
