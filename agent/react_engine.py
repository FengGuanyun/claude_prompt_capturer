"""
React Agent Engine - React-style Tool Loop
基于 ReAct (Reasoning + Acting) 模式的Agent
"""

import json
import queue
import threading
from dataclasses import dataclass, field
from typing import Any, Iterator, Optional
from enum import Enum


class EventType(Enum):
    TEXT = "text"
    TOOL_CALL = "tool_call"
    TOOL_RESULT = "tool_result"
    THINKING = "thinking"
    WAITING = "waiting"
    ERROR = "error"
    ITERATION_START = "iteration_start"
    ITERATION_END = "iteration_end"
    PROMPT_ASSEMBLED = "prompt_assembled"
    AGENT_MESSAGE = "agent_message"
    TEAM_STATUS = "team_status"


@dataclass
class Event:
    type: EventType
    data: Any = None
    timestamp: str = ""
    details: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "type": self.type.value,
            "data": self.data,
            "timestamp": self.timestamp,
            "details": self.details
        }


@dataclass
class ToolResult:
    content: str
    is_error: bool = False
    observation: str = ""


class Tool:
    @property
    def name(self) -> str:
        raise NotImplementedError

    @property
    def description(self) -> str:
        raise NotImplementedError

    @property
    def input_schema(self) -> dict:
        raise NotImplementedError

    def execute(self, **kwargs) -> ToolResult:
        raise NotImplementedError

    def is_read_only(self) -> bool:
        return True

    def to_api_schema(self) -> dict:
        return {
            "name": self.name,
            "description": self.description,
            "input_schema": self.input_schema,
        }


def get_timestamp() -> str:
    from datetime import datetime
    return datetime.now().strftime("%H:%M:%S.%f")[:-3]


class ReactAgent:
    """
    React-style Agent
    思想-行动-观察循环:
    1. Think: 分析情况，决定下一步
    2. Act: 选择并执行工具
    3. Observe: 观察结果，决定下一步
    """

    def __init__(
        self,
        name: str = "ReactAgent",
        system_prompt: str = "You are a helpful coding assistant.",
        tools: dict[str, Tool] | None = None,
    ):
        self.name = name
        self.system_prompt = system_prompt
        self._tools = tools or {}
        self._messages: list[dict] = []
        self._thoughts: list[str] = []
        self._max_iterations = 20

    def add_tool(self, tool: Tool):
        self._tools[tool.name] = tool

    def add_messages(self, messages: list[dict]):
        self._messages.extend(messages)

    def get_tools_schemas(self) -> list[dict]:
        return [t.to_api_schema() for t in self._tools.values()]

    def run(self, user_input: str, max_iterations: int = 20) -> Iterator[Event]:
        """运行React循环"""
        self._max_iterations = max_iterations
        self._thoughts = []

        # 添加用户消息
        self._messages.append({
            "role": "user",
            "content": user_input
        })

        iteration = 0

        while iteration < max_iterations:
            iteration += 1
            ts = get_timestamp()

            yield Event(
                type=EventType.ITERATION_START,
                data=f"Iteration {iteration}",
                timestamp=ts,
                details={"iteration": iteration, "agent": self.name}
            )

            # Think: 分析当前状态
            thought = self._think()
            self._thoughts.append(thought)

            yield Event(
                type=EventType.THINKING,
                data=thought,
                timestamp=get_timestamp(),
                details={"thought_number": len(self._thoughts)}
            )

            # 决定是否使用工具
            tool_calls = self._decide_action(thought)

            if not tool_calls:
                # 没有工具调用，直接返回文本
                response_text = self._get_response_text()
                yield Event(
                    type=EventType.TEXT,
                    data=response_text,
                    timestamp=get_timestamp()
                )
                yield Event(
                    type=EventType.ITERATION_END,
                    data="Completed",
                    timestamp=get_timestamp(),
                    details={"iteration": iteration, "reason": "no_tool_calls"}
                )
                break

            # Act: 执行工具
            all_results = []
            for tc in tool_calls:
                tool_name = tc.get("name")
                tool_input = tc.get("input", {})

                yield Event(
                    type=EventType.TOOL_CALL,
                    data=f"{tool_name}({json.dumps(tool_input)})",
                    timestamp=get_timestamp(),
                    details={"name": tool_name, "input": tool_input}
                )

                # 执行工具
                tool = self._tools.get(tool_name)
                if not tool:
                    result = ToolResult(content=f"Unknown tool: {tool_name}", is_error=True)
                else:
                    result = tool.execute(**tool_input)

                # Observe: 观察结果
                observation = f"Tool {tool_name} returned: {result.content}"
                all_results.append(observation)

                yield Event(
                    type=EventType.TOOL_RESULT,
                    data=result.content,
                    timestamp=get_timestamp(),
                    details={
                        "name": tool_name,
                        "is_error": result.is_error,
                        "observation": observation
                    }
                )

            # 将工具结果添加到消息
            self._messages.append({
                "role": "assistant",
                "content": " ".join([f"Tool result: {r}" for r in all_results])
            })

        if iteration >= max_iterations:
            yield Event(
                type=EventType.ITERATION_END,
                data="Max iterations reached",
                timestamp=get_timestamp(),
                details={"iteration": iteration, "reason": "max_iterations"}
            )

    def _think(self) -> str:
        """思考当前状态"""
        recent_msgs = self._messages[-3:] if len(self._messages) > 3 else self._messages
        context = "\n".join([f"{m['role']}: {m['content'][:200]}" for m in recent_msgs])

        available_tools = ", ".join(self._tools.keys()) if self._tools else "none"

        return f"Context: {context}\nAvailable tools: {available_tools}\nThought:"

    def _decide_action(self, thought: str) -> list[dict]:
        """决定采取的行动"""
        # 简单实现：检查用户输入是否需要工具
        # 实际应该用LLM判断
        return []

    def _get_response_text(self) -> str:
        """获取响应文本"""
        if self._messages:
            last = self._messages[-1]
            if isinstance(last.get("content"), str):
                return last["content"][:500]
        return "Done"


class EnhancedAgentEngine:
    """
    增强Agent引擎
    整合 React loop + Tool + MCP + Skill + Memory + Team
    """

    def __init__(
        self,
        name: str = "EnhancedAgent",
        system_prompt: str = "You are a helpful coding assistant.",
        provider: str = "anthropic",
        model: str = "MiniMax-M2.7",
        api_key: str | None = None,
        base_url: str | None = None,
    ):
        self.name = name
        self.system_prompt = system_prompt
        self.provider = provider
        self.model = model
        self.api_key = api_key
        self.base_url = base_url

        self._tools: dict[str, Tool] = {}
        self._mcp_clients = {}
        self._skills = {}
        self._memory_compactor = None
        self._team = None

        # 加载默认工具
        self._load_default_tools()

    def _load_default_tools(self):
        """加载默认工具"""
        from .engine import DEFAULT_TOOLS
        self._tools.update(DEFAULT_TOOLS)

    def add_tool(self, tool: Tool):
        """添加工具"""
        self._tools[tool.name] = tool

    def add_mcp_client(self, name: str, client):
        """添加MCP客户端"""
        self._mcp_clients[name] = client

    def add_skill(self, name: str, content: str):
        """添加技能"""
        self._skills[name] = content

    def get_team(self):
        """获取Agent团队"""
        if self._team is None:
            from .team import get_team
            self._team = get_team()
        return self._team

    def get_tools_schemas(self) -> list[dict]:
        """获取所有工具schema"""
        schemas = [t.to_api_schema() for t in self._tools.values()]

        # 添加MCP工具
        for name, client in self._mcp_clients.items():
            for tool in client.list_tools():
                schemas.append({
                    "name": f"mcp_{name}_{tool.name}",
                    "description": f"[MCP:{name}] {tool.description}",
                    "input_schema": tool.input_schema
                })

        return schemas

    def run(self, user_input: str, max_iterations: int = 20) -> Iterator[Event]:
        """运行Agent"""
        # 使用React Agent处理
        react_agent = ReactAgent(
            name=self.name,
            system_prompt=self.system_prompt,
            tools=self._tools
        )

        for event in react_agent.run(user_input, max_iterations):
            yield event

    def get_status(self) -> dict:
        """获取Agent状态"""
        return {
            "name": self.name,
            "tools_count": len(self._tools),
            "mcp_clients": list(self._mcp_clients.keys()),
            "skills": list(self._skills.keys()),
            "memory": self._memory_compactor.get_stats() if self._memory_compactor else None,
            "team": self.get_team().get_team_status() if self._team else None
        }
