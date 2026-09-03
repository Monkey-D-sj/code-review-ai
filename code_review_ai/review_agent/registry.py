"""Small, intentionally non-executing registry for review tools."""

from __future__ import annotations

from dataclasses import dataclass

from langchain_core.tools import BaseTool

from code_review_ai.review_agent.schemas import ToolKind


@dataclass(frozen=True)
class RegisteredTool:
    tool: BaseTool
    kind: ToolKind = "action"


class ToolRegistry:
    def __init__(self, tools: list[RegisteredTool]):
        self._registered: dict[str, RegisteredTool] = {}
        for registered in tools:
            name = registered.tool.name
            if name in self._registered:
                raise ValueError(f"duplicate review tool: {name}")
            self._registered[name] = registered

    def all_tools(self) -> list[BaseTool]:
        return [item.tool for item in self._registered.values()]

    def action_tools(self) -> list[BaseTool]:
        return [item.tool for item in self._registered.values()
                if item.kind == "action"]

    def select(self, names: list[str]) -> list[BaseTool]:
        missing = [name for name in names if name not in self._registered]
        if missing:
            raise ValueError(f"unknown review tool(s): {', '.join(missing)}")
        return [self._registered[name].tool for name in names]

    def subset(self, names: list[str]) -> "ToolRegistry":
        self.select(names)  # Preserve select's useful unknown-name error.
        return ToolRegistry([self._registered[name] for name in names])

    def is_terminal(self, name: str) -> bool:
        item = self._registered.get(name)
        return item is not None and item.kind == "terminal"
