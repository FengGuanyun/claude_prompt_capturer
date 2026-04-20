"""
Agent Team - 多Agent协作系统
支持多个专业Agent协同工作
"""

import uuid
from dataclasses import dataclass, field
from typing import Any, Iterator, Callable
from enum import Enum
from datetime import datetime


class TaskStatus(Enum):
    PENDING = "pending"
    IN_PROGRESS = "in_progress"
    COMPLETED = "completed"
    FAILED = "failed"


@dataclass
class TeamTask:
    """团队任务"""
    id: str
    description: str
    assigned_agent: str = ""
    status: TaskStatus = TaskStatus.PENDING
    result: Any = None
    created_at: str = ""
    completed_at: str = ""

    def __post_init__(self):
        if not self.created_at:
            self.created_at = datetime.now().strftime("%H:%M:%S")


@dataclass
class AgentMessage:
    """Agent间消息"""
    from_agent: str
    to_agent: str
    content: str
    task_id: str = ""
    timestamp: str = ""


class BaseAgent:
    """基础Agent类"""

    def __init__(self, name: str, role: str, system_prompt: str):
        self.name = name
        self.role = role
        self.system_prompt = system_prompt
        self.tools = {}
        self.messages = []

    def add_tool(self, tool):
        self.tools[tool.name] = tool

    def think(self, context: dict) -> dict:
        """思考下一步行动"""
        raise NotImplementedError


class SpecializedAgent(BaseAgent):
    """专业Agent"""

    def __init__(self, name: str, role: str, specialty: str, system_prompt: str):
        super().__init__(name, role, system_prompt)
        self.specialty = specialty


class AgentTeam:
    """
    Agent团队管理器
    支持多Agent协作、任务分配、消息传递
    """

    def __init__(self, name: str = "Team"):
        self.name = name
        self.agents: dict[str, BaseAgent] = {}
        self.tasks: dict[str, TeamTask] = {}
        self.message_queue: list[AgentMessage] = []

    def register_agent(self, agent: BaseAgent):
        """注册Agent"""
        self.agents[agent.name] = agent

    def create_task(self, description: str) -> str:
        """创建任务"""
        task_id = str(uuid.uuid4())[:8]
        self.tasks[task_id] = TeamTask(
            id=task_id,
            description=description
        )
        return task_id

    def assign_task(self, task_id: str, agent_name: str) -> bool:
        """分配任务给Agent"""
        if task_id not in self.tasks:
            return False
        if agent_name not in self.agents:
            return False

        task = self.tasks[task_id]
        task.assigned_agent = agent_name
        task.status = TaskStatus.IN_PROGRESS
        return True

    def send_message(self, from_agent: str, to_agent: str, content: str, task_id: str = ""):
        """发送消息"""
        msg = AgentMessage(
            from_agent=from_agent,
            to_agent=to_agent,
            content=content,
            task_id=task_id,
            timestamp=datetime.now().strftime("%H:%M:%S")
        )
        self.message_queue.append(msg)

    def get_messages_for(self, agent_name: str) -> list[AgentMessage]:
        """获取发送给某Agent的消息"""
        return [m for m in self.message_queue if m.to_agent == agent_name]

    def get_task_status(self, task_id: str) -> dict:
        """获取任务状态"""
        if task_id not in self.tasks:
            return {"error": "Task not found"}
        task = self.tasks[task_id]
        return {
            "id": task.id,
            "description": task.description,
            "assigned_agent": task.assigned_agent,
            "status": task.status.value,
            "result": task.result
        }

    def complete_task(self, task_id: str, result: Any):
        """完成任务"""
        if task_id in self.tasks:
            task = self.tasks[task_id]
            task.status = TaskStatus.COMPLETED
            task.result = result
            task.completed_at = datetime.now().strftime("%H:%M:%S")

    def get_team_status(self) -> dict:
        """获取团队状态"""
        return {
            "name": self.name,
            "agents": [
                {"name": a.name, "role": a.role, "tools": list(a.tools.keys())}
                for a in self.agents.values()
            ],
            "tasks": {
                "pending": len([t for t in self.tasks.values() if t.status == TaskStatus.PENDING]),
                "in_progress": len([t for t in self.tasks.values() if t.status == TaskStatus.IN_PROGRESS]),
                "completed": len([t for t in self.tasks.values() if t.status == TaskStatus.COMPLETED]),
            }
        }


# 全局团队实例
_team = None


def get_team() -> AgentTeam:
    """获取全局团队"""
    global _team
    if _team is None:
        _team = AgentTeam("MainTeam")
    return _team


def create_specialized_team() -> AgentTeam:
    """创建专业团队"""
    team = AgentTeam("SpecializedTeam")

    # 前端Agent
    frontend = SpecializedAgent(
        name="frontend-dev",
        role="Frontend Developer",
        specialty="HTML/CSS/JavaScript",
        system_prompt="You are a frontend developer. You create beautiful web interfaces."
    )

    # 后端Agent
    backend = SpecializedAgent(
        name="backend-dev",
        role="Backend Developer",
        specialty="Python/Flask APIs",
        system_prompt="You are a backend developer. You create robust APIs and server logic."
    )

    # 测试Agent
    tester = SpecializedAgent(
        name="tester",
        role="QA Engineer",
        specialty="Testing & Verification",
        system_prompt="You are a QA engineer. You verify functionality and find bugs."
    )

    team.register_agent(frontend)
    team.register_agent(backend)
    team.register_agent(tester)

    return team
