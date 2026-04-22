# Claude Capturer

统一的 Claude Code / OpenCode 代理服务，支持 Prompt 捕获、WebSocket PTY 终端和 Agent 执行流程可视化。

## 功能

- **Prompt 捕获**：拦截所有 API 请求，在网页上查看完整的 Prompt、消息历史和工具调用
- **WebSocket PTY 终端**：在浏览器中使用 Claude Code 和 OpenCode，支持 Windows (winpty) 和 Unix (pty)
- **API 代理**：将 Claude Code 和 OpenCode 的请求代理到 DashScope 等兼容服务
- **Agent 编程助手**：带完整执行流程可视化的本地 Agent，支持 Bash/文件读写工具

## 快速开始

### 1. 安装依赖

```bash
pip install flask flask-cors httpx websockets
# Windows 需要 winpty
pip install pywinpty
```

### 2. 配置 API Key

在 `~/.claude/settings.json` 中配置：

```json
{
  "env": {
    "ANTHROPIC_AUTH_TOKEN": "your-api-key",
    "ANTHROPIC_BASE_URL": "https://coding.dashscope.aliyuncs.com/apps/anthropic",
    "ANTHROPIC_MODEL": "qwen3.6-plus"
  }
}
```

### 3. 启动服务

```bash
python unified_server.py
```

访问：
- **终端页面**：http://localhost:8080
- **Agent 编程助手**：http://localhost:8080/agent
- **Prompt 捕获**：在终端页面中点击 Captured Prompts

## 项目结构

```
├── unified_server.py          # 入口文件
├── terminal_ui.html           # 终端 + Prompt 捕获界面
├── agent_ui.html              # Agent 编程助手界面
├── server/
│   ├── __init__.py            # Flask app 路由注册
│   ├── config.py              # API 配置加载
│   ├── capture.py             # Prompt 捕获与调试日志
│   ├── proxy.py               # API 代理（Anthropic ↔ OpenAI 格式转换）
│   ├── translate.py           # 翻译接口
│   ├── agent.py               # Agent 工具链 + /chat SSE 端点
│   └── ws_handler.py          # WebSocket PTY 处理
├── mini-cc/                   # 参考的 mini-cc Agent 代码（未直接引用）
└── apps/                      # 用户生成文件目录
```

## 架构

```
浏览器                    本服务                    上游 API
  │                        │                          │
  │  ┌── 终端 (PTY) ──────┤  WebSocket PTY           │
  │  │   Claude Code      ├─────► winpty/pty ──────► Claude
  │  │   OpenCode         │                           │
  │  │                    │                          │
  │  ├── Prompt 捕获 ────┤  拦截请求 → 记录 → 转发 ──► DashScope
  │  │                    │                          │
  │  ├── Agent 对话 ─────┤  工具循环 → SSE 流 ───────► DashScope
  │  │                    │                          │
  │  └── API 代理 ───────┤  OpenAI ↔ Anthropic 转换 ─► DashScope
  │                        │                          │
```

## OpenCode 配置

### Anthropic 格式

在 `~/.config/opencode/opencode.json` 中配置 provider，`baseURL` 指向 DashScope：

```json
{
  "provider": {
    "bailian": {
      "options": {
        "baseURL": "https://coding.dashscope.aliyuncs.com/apps/anthropic"
      },
      "models": {
        "qwen3.6-plus": {}
      }
    }
  },
  "model": {
    "bailian/qwen3.6-plus": "*"
  }
}
```

### OpenAI 格式

```json
{
  "provider": {
    "my-openai": {
      "options": {
        "baseURL": "http://localhost:8080/v1"
      },
      "models": {
        "qwen3.6-plus": {}
      }
    }
  },
  "model": {
    "my-openai/qwen3.6-plus": "*"
  }
}
```

OpenAI 格式的请求会被代理自动转换为 Anthropic 格式后转发到上游。

## Agent 编程助手

Agent 支持 3 个工具：
- **BashTool** — 执行 Shell 命令（有安全沙盒，拒绝危险操作）
- **FileReadTool** — 读取文件内容（自动截断超长文件）
- **FileWriteTool** — 写入文件（自动创建目录）

每次对话包含最多 100 次迭代（工具调用循环）。执行流程以对话/迭代两级容器展示，可查看每一步的 Prompt 组装详情。

## API 端点

| 路径 | 方法 | 说明 |
|------|------|------|
| `/` | GET | 终端页面 |
| `/agent` | GET | Agent 编程助手页面 |
| `/v1/messages` | POST | Anthropic 格式代理 |
| `/v1/chat/completions` | POST | OpenAI 格式代理 |
| `/apps/anthropic/v1/messages` | POST | OpenCode 代理 |
| `/chat` | POST | Agent SSE 流式对话 |
| `/captured` | GET | 获取捕获的 Prompts |
| `/debug_logs` | GET | 调试日志 |
| `/translate` | POST | 翻译接口 |
| `/health` | GET | 健康检查 |
