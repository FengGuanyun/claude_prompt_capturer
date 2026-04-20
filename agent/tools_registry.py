"""
Tools Registry - 工具注册表
"""

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .engine import Tool

# 全局工具注册表
_tool_registry: dict[str, "Tool"] = {}
_initialized = False


def register_tool(tool: "Tool"):
    _tool_registry[tool.name] = tool


def get_tool(name: str) -> "Tool | None":
    return _tool_registry.get(name)


def get_all_tools() -> dict[str, "Tool"]:
    global _initialized
    if not _initialized:
        _register_defaults()
        _initialized = True
    return _tool_registry.copy()


def _register_defaults():
    global _initialized
    if _initialized:
        return

    from .engine import DEFAULT_TOOLS
    _tool_registry.clear()
    for name, tool in DEFAULT_TOOLS.items():
        _tool_registry[name] = tool
    _initialized = True
