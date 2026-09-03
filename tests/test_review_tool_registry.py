from langchain_core.tools import StructuredTool
import pytest

from code_review_ai.review_agent.registry import RegisteredTool, ToolRegistry


def _tool(name):
    def handler(value: str = "") -> str:
        """Test tool."""
        return value
    return StructuredTool.from_function(handler, name=name, description="Test tool")


def test_registry_preserves_order_and_separates_terminal_tools():
    first, terminal, second = _tool("first"), _tool("submit"), _tool("second")
    registry = ToolRegistry([
        RegisteredTool(first), RegisteredTool(terminal, "terminal"), RegisteredTool(second)])

    assert [tool.name for tool in registry.all_tools()] == ["first", "submit", "second"]
    assert [tool.name for tool in registry.action_tools()] == ["first", "second"]
    assert [tool.name for tool in registry.select(["second", "first"])] == ["second", "first"]
    assert registry.is_terminal("submit") is True
    assert registry.is_terminal("first") is False


def test_registry_rejects_duplicates_and_unknown_subsets():
    with pytest.raises(ValueError, match="duplicate"):
        ToolRegistry([RegisteredTool(_tool("same")), RegisteredTool(_tool("same"))])
    registry = ToolRegistry([RegisteredTool(_tool("known"))])
    with pytest.raises(ValueError, match="unknown"):
        registry.select(["missing"])
