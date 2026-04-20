"""
MCP Client - Model Context Protocol 客户端
支持连接到 MCP 服务器并调用其工具
"""

import json
import subprocess
import asyncio
from typing import Any, Optional
from dataclasses import dataclass


@dataclass
class MCPTool:
    name: str
    description: str
    input_schema: dict


class MCPClient:
    """MCP 客户端 - 简化实现"""

    def __init__(self, command: list[str], env: Optional[dict] = None):
        self.command = command
        self.env = env or {}
        self.process: Optional[subprocess.Popen] = None
        self.request_id = 0
        self._tool_schemas: list[dict] = []
        self._connected = False

    def connect(self) -> bool:
        """连接到 MCP 服务器"""
        try:
            full_env = {**subprocess.os.environ, **self.env}
            self.process = subprocess.Popen(
                self.command,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                env=full_env
            )
            # 发送 initialize 请求
            self._send_request("initialize", {
                "protocolVersion": "2024-11-05",
                "capabilities": {"tools": {}},
                "clientInfo": {"name": "agent-demo", "version": "1.0.0"}
            })
            # 接收响应
            response = self._read_response()
            if response:
                self._connected = True
                # 发送 initialized 通知
                self._send_notification("initialized", {})
                # 获取可用工具列表
                self._fetch_tools()
                return True
            return False
        except Exception as e:
            print(f"MCP connection failed: {e}")
            return False

    def _send_request(self, method: str, params: dict) -> Optional[dict]:
        """发送 JSON-RPC 请求"""
        if not self.process:
            return None

        self.request_id += 1
        request = {
            "jsonrpc": "2.0",
            "id": self.request_id,
            "method": method,
            "params": params
        }

        try:
            self.process.stdin.write(json.dumps(request).encode() + b"\n")
            self.process.stdin.flush()
            return self._read_response()
        except Exception as e:
            print(f"Failed to send request: {e}")
            return None

    def _send_notification(self, method: str, params: dict):
        """发送 JSON-RPC 通知（无响应）"""
        if not self.process:
            return

        notification = {
            "jsonrpc": "2.0",
            "method": method,
            "params": params
        }

        try:
            self.process.stdin.write(json.dumps(notification).encode() + b"\n")
            self.process.stdin.flush()
        except Exception:
            pass

    def _read_response(self) -> Optional[dict]:
        """读取响应"""
        if not self.process:
            return None

        try:
            line = self.process.stdout.readline()
            if line:
                return json.loads(line.decode())
        except Exception:
            pass
        return None

    def _fetch_tools(self):
        """获取可用工具列表"""
        response = self._send_request("tools/list", {})
        if response and "result" in response:
            self._tool_schemas = response["result"].get("tools", [])

    def list_tools(self) -> list[MCPTool]:
        """列出可用工具"""
        tools = []
        for schema in self._tool_schemas:
            tools.append(MCPTool(
                name=schema.get("name", ""),
                description=schema.get("description", ""),
                input_schema=schema.get("inputSchema", {})
            ))
        return tools

    def call_tool(self, tool_name: str, arguments: dict) -> str:
        """调用工具"""
        response = self._send_request("tools/call", {
            "name": tool_name,
            "arguments": arguments
        })

        if response and "result" in response:
            result = response["result"]
            if "content" in result:
                # 提取文本内容
                for item in result["content"]:
                    if item.get("type") == "text":
                        return item["text"]
                return str(result["content"])
            return str(result)

        if response and "error" in response:
            return f"Error: {response['error']}"

        return "No response from MCP server"

    def disconnect(self):
        """断开连接"""
        if self.process:
            self.process.terminate()
            self.process = None
            self._connected = False


# MCP 服务器注册表
_mcp_servers = {}


def register_mcp_server(name: str, command: list[str], env: Optional[dict] = None):
    """注册 MCP 服务器"""
    _mcp_servers[name] = {
        "command": command,
        "env": env,
        "client": None
    }


def get_mcp_client(name: str) -> Optional[MCPClient]:
    """获取 MCP 客户端"""
    if name in _mcp_servers:
        server = _mcp_servers[name]
        if server["client"] is None:
            client = MCPClient(server["command"], server["env"])
            if client.connect():
                server["client"] = client
                return client
        return server["client"]
    return None


def list_mcp_tools() -> list[MCPTool]:
    """列出所有 MCP 工具"""
    all_tools = []
    for name in _mcp_servers:
        client = get_mcp_client(name)
        if client:
            all_tools.extend(client.list_tools())
    return all_tools


# 默认 MCP 服务器配置（可选）
# 可以通过环境变量或配置文件添加
def load_mcp_config():
    """从环境变量加载 MCP 配置"""
    import os

    # 示例: MCP_SERVERS="server1:command1,server2:command2"
    mcp_servers = os.environ.get("MCP_SERVERS", "")
    if mcp_servers:
        for server_def in mcp_servers.split(","):
            if ":" in server_def:
                name, cmd = server_def.split(":", 1)
                register_mcp_server(name, cmd.split())
