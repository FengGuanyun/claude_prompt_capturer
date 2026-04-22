"""Agent — tools, loop, and SSE /chat endpoint."""

import asyncio
import json
import os
import queue
import re
import threading
from datetime import datetime
from pathlib import Path

import httpx
from flask import request, Response, stream_with_context

from .config import load_claude_config


# ─── Tools ────────────────────────────────────────────────────────────────────

DANGEROUS_PATTERNS = [
    re.compile(r'rm\s+-r[fF]?\s+/'),
    re.compile(r'mkfs\.'),
    re.compile(r'dd\s+if=.*of=/dev/sda'),
    re.compile(r'>\s*/dev/sd[a-z]'),
    re.compile(r'\$\([^)]+\)'),
    re.compile(r'`[^`]+`'),
]

async def _execute_bash(args: dict) -> str:
    command = args.get("command")
    if not command:
        return "执行命令时出错: command 不能为空"
    for pattern in DANGEROUS_PATTERNS:
        if pattern.search(command):
            return f"命令执行被安全沙盒拒绝：包含高危指令模式 ({pattern.pattern})"
    try:
        process = await asyncio.create_subprocess_shell(
            command, stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE, cwd=os.getcwd()
        )
        stdout, stderr = await process.communicate()
        out = stdout.decode('utf-8', errors='replace')
        err = stderr.decode('utf-8', errors='replace')
        if err:
            return f"[stdout]\n{out}\n[stderr]\n{err}"
        return out or "命令执行成功，但没有输出。"
    except Exception as e:
        return f"执行命令时出错: {str(e)}"

async def _execute_file_read(args: dict) -> str:
    file_path = args.get("file_path")
    if not file_path:
        return "读取文件时出错：file_path 不能为空"
    try:
        with open(file_path, 'r', encoding='utf-8') as f:
            content = f.read()
        lines = content.split('\n')
        if len(lines) > 1000:
            return '\n'.join(lines[:1000]) + '\n\n... (文件已截断，仅显示前 1000 行)'
        return content
    except FileNotFoundError:
        return f"错误：文件未找到。路径：{file_path}"
    except Exception as e:
        return f"读取文件时出错：{str(e)}"

async def _execute_file_write(args: dict) -> str:
    file_path = args.get("file_path")
    content = args.get("content", "")
    if not file_path:
        return "写入文件时出错：file_path 不能为空"
    try:
        parent = Path(file_path).parent
        if parent and not parent.exists():
            parent.mkdir(parents=True, exist_ok=True)
        with open(file_path, 'w', encoding='utf-8') as f:
            f.write(content)
        return f"文件已成功写入: {file_path}"
    except Exception as e:
        return f"写入文件时出错：{str(e)}"

AGENT_TOOLS = [
    {
        "name": "BashTool",
        "description": "在本地系统执行 Bash/Shell 命令。用于运行测试、执行脚本、操作文件系统。注意：命令是 non-interactive 的，避免运行 vim/nano 等需要用户输入的命令。",
        "inputSchema": {"type": "object", "properties": {"command": {"type": "string", "description": "需要执行的 shell 命令"}}, "required": ["command"]},
        "execute": _execute_bash,
    },
    {
        "name": "FileReadTool",
        "description": "读取本地系统上的文件内容。请提供绝对路径。超过 1000 行的文件会被截断。",
        "inputSchema": {"type": "object", "properties": {"file_path": {"type": "string", "description": "需要读取文件的绝对路径"}}, "required": ["file_path"]},
        "execute": _execute_file_read,
    },
    {
        "name": "FileWriteTool",
        "description": "向本地系统写入文件。如果目录不存在会自动创建。",
        "inputSchema": {"type": "object", "properties": {"file_path": {"type": "string", "description": "需要写入文件的绝对路径"}, "content": {"type": "string", "description": "要写入的内容"}}, "required": ["file_path", "content"]},
        "execute": _execute_file_write,
    },
]


# ─── Helpers ──────────────────────────────────────────────────────────────────

def _estimate_tokens(text: str) -> int:
    if not text:
        return 0
    chinese = sum(1 for c in text if '一' <= c <= '鿿')
    other = len(text) - chinese
    return chinese // 2 + other // 4


def _emit_event(event_type, data=None, details=None):
    entry = {"timestamp": datetime.now().strftime("%H:%M:%S.%f")[:-3], "type": event_type}
    if data is not None:
        entry["data"] = data
    if details is not None:
        entry["details"] = details
    return f"data: {json.dumps(entry, ensure_ascii=False)}\n\n"


def _to_anthropic_tools():
    return [{
        "name": t["name"],
        "description": t["description"],
        "input_schema": t["inputSchema"],
    } for t in AGENT_TOOLS]


async def _handle_tool_calls(tool_calls):
    """Execute tool calls and return results."""
    results = []
    for call in tool_calls:
        args = call.get("args", {})
        if args.get("_parse_error"):
            results.append({
                "id": call["id"], "name": call["name"],
                "result": f"[Agent 内部错误] 你输出的工具参数 JSON 格式不合法。\n原始参数:\n{args.get('_raw_arguments')}",
                "isError": True,
            })
            continue
        tool = next((t for t in AGENT_TOOLS if t["name"] == call["name"]), None)
        if not tool:
            results.append({
                "id": call["id"], "name": call["name"],
                "result": f"未知的工具调用: {call['name']}",
                "isError": True,
            })
            continue
        try:
            result = await tool["execute"](args)
            if isinstance(result, str) and len(result) > 8000:
                result = result[:8000] + '\n\n...[由于内容过长，已被系统截断]...'
            results.append({"id": call["id"], "name": call["name"], "result": result, "isError": False})
        except Exception as e:
            results.append({"id": call["id"], "name": call["name"], "result": f"执行工具 {call['name']} 时出错: {str(e)}", "isError": True})
    return results


# ─── Chat endpoint ────────────────────────────────────────────────────────────

def agent_chat():
    """Real agent loop — streams events with full execution flow."""
    try:
        data = request.get_json()
        user_message = data.get("message", "")

        def generate():
            config = load_claude_config()
            api_key = config["api_key"]
            base_url = config["base_url"]
            model = config["model"]

            q = queue.Queue()
            sentinel = object()

            async def run_agent():
                max_loops = 100
                loop_count = 0
                system_prompt = "你是一名agent编程助手。你拥有读取文件、写入文件和执行终端命令的权限。你的目标是帮助用户解决复杂的软件工程问题。"
                messages = [{"role": "user", "content": user_message}]

                while True:
                    # ── Build prompt display ──
                    prompt_msgs = []
                    for m in messages:
                        raw = m.get("content", "")
                        if isinstance(raw, list):
                            texts = []
                            for block in raw:
                                btype = block.get("type", "")
                                if btype == "text":
                                    texts.append(block.get("text", ""))
                                elif btype == "tool_use":
                                    texts.append(f"[Tool Use: {block.get('name', '')}]")
                                elif btype == "tool_result":
                                    texts.append(f"[Tool Result: {block.get('tool_use_id', '')}]")
                            content = "\n".join(texts)
                        else:
                            content = str(raw) if raw else ""
                        prompt_msgs.append({
                            "role": m["role"],
                            "content": content,
                            "tokens": _estimate_tokens(content),
                            "content_preview": content[:200],
                        })

                    prompt_details = {
                        "system_prompt": system_prompt,
                        "messages": prompt_msgs,
                        "tools": [{"name": t["name"], "description": t["description"]} for t in AGENT_TOOLS],
                        "is_agent_loop": loop_count > 0,
                    }
                    q.put(_emit_event("prompt_assembled", details=prompt_details))
                    q.put(_emit_event("waiting", details={"message": "等待模型响应..."}))

                    # ── Call API ──
                    anthropic_request = {
                        "model": model,
                        "system": system_prompt,
                        "messages": _to_anthropic_messages(messages),
                        "tools": _to_anthropic_tools(),
                        "max_tokens": 8192,
                    }

                    headers = {
                        "x-api-key": api_key,
                        "anthropic-version": "2023-06-01",
                        "content-type": "application/json",
                    }

                    full_content = ''
                    tool_calls = []

                    async with httpx.AsyncClient(timeout=120.0, trust_env=False, verify=False) as client:
                        resp = await client.post(
                            base_url + "/v1/messages",
                            json=anthropic_request,
                            headers=headers,
                        )
                        if resp.status_code != 200:
                            q.put(_emit_event("error", details={"message": f"API 请求失败 (HTTP {resp.status_code}): {resp.text[:500]}"}))
                            return

                        result = resp.json()
                        for block in result.get("content", []):
                            if block.get("type") == "text":
                                text = block.get("text", "")
                                full_content += text
                            elif block.get("type") == "tool_use":
                                tool_calls.append(block)

                    # ── Build assistant message ──
                    assistant_blocks = []
                    if full_content:
                        assistant_blocks.append({"type": "text", "text": full_content})
                    for tc in tool_calls:
                        assistant_blocks.append({
                            "type": "tool_use",
                            "id": tc["id"],
                            "name": tc["name"],
                            "input": tc.get("input", {}),
                        })
                    messages.append({"role": "assistant", "content": assistant_blocks})

                    if not tool_calls:
                        # No tool calls — text reply only, conversation done
                        if full_content:
                            q.put(_emit_event("text", data=full_content))
                        return

                    # ── Tool calls exist — emit iteration header BEFORE tool events ──
                    loop_count += 1
                    q.put(_emit_event("iteration_start", details={"loop": loop_count}))

                    # If there's text before tools, it's the thinking process
                    if full_content:
                        q.put(_emit_event("thinking", data=full_content))

                    for tc in tool_calls:
                        q.put(_emit_event("tool_call", details={
                            "name": tc["name"],
                            "input": tc.get("input", {}),
                        }))

                    internal_tool_calls = []
                    for tc in tool_calls:
                        args = tc.get("input", {})
                        if isinstance(args, str):
                            try:
                                args = json.loads(args)
                            except json.JSONDecodeError:
                                args = {"_parse_error": True, "_raw_arguments": args}
                        internal_tool_calls.append({
                            "id": tc["id"],
                            "name": tc["name"],
                            "args": args,
                        })

                    tool_results = await _handle_tool_calls(internal_tool_calls)

                    for tr in tool_results:
                        q.put(_emit_event("tool_result", details={
                            "name": tr["name"],
                            "content": str(tr["result"]),
                            "is_error": tr.get("isError", False),
                        }))

                    for tr in tool_results:
                        messages.append({
                            "role": "user",
                            "content": [{
                                "type": "tool_result",
                                "tool_use_id": tr["id"],
                                "content": str(tr["result"]),
                            }],
                        })

                    # Merge consecutive user messages into a single message
                    messages = _merge_user_messageses(messages)

                    q.put(_emit_event("iteration_end"))

                    if loop_count >= max_loops:
                        q.put(_emit_event("text", data="\n[Agent] 工具调用循环次数过多，已强制终止。\n"))
                        return

            def async_runner():
                new_loop = asyncio.new_event_loop()
                asyncio.set_event_loop(new_loop)
                try:
                    new_loop.run_until_complete(run_agent())
                except Exception as e:
                    q.put(_emit_event("error", details={"message": str(e)}))
                finally:
                    new_loop.close()
                q.put(sentinel)

            t = threading.Thread(target=async_runner, daemon=True)
            t.start()

            while True:
                item = q.get()
                if item is sentinel:
                    break
                yield item

        return Response(stream_with_context(generate()), mimetype="text/event-stream")
    except Exception as e:
        return {"error": str(e)}, 500


# ─── Internal: message conversion ─────────────────────────────────────────────

def _merge_user_messageses(msgs):
    """Merge consecutive user/assistant messages with same role."""
    if not msgs:
        return msgs
    merged = [msgs[0]]
    for m in msgs[1:]:
        prev = merged[-1]
        if m["role"] == prev["role"]:
            prev_content = prev["content"]
            new_content = m["content"]
            if isinstance(prev_content, list) and isinstance(new_content, list):
                prev["content"] = prev_content + new_content
            else:
                prev["content"] = str(prev_content) + str(new_content)
        else:
            merged.append(m)
    return merged

def _to_anthropic_messages(msgs):
    """Convert internal message format to Anthropic message content blocks."""
    anthropic_msgs = []
    for m in msgs:
        role = m["role"]
        content = m.get("content", "")
        if role == "system":
            continue
        if role == "user":
            if isinstance(content, list):
                anthropic_msgs.append({"role": "user", "content": content})
            else:
                anthropic_msgs.append({"role": "user", "content": [{"type": "text", "text": str(content)}]})
        elif role == "assistant":
            if isinstance(content, list):
                # Already in Anthropic format (content blocks), pass through
                anthropic_msgs.append({"role": "assistant", "content": content})
            else:
                # OpenAI format, convert
                blocks = []
                if content:
                    blocks.append({"type": "text", "text": str(content)})
                for tc in m.get("tool_calls", []):
                    blocks.append({
                        "type": "tool_use",
                        "id": tc["id"],
                        "name": tc["function"]["name"],
                        "input": json.loads(tc["function"]["arguments"]) if isinstance(tc["function"]["arguments"], str) else tc["function"]["arguments"],
                    })
                if blocks:
                    anthropic_msgs.append({"role": "assistant", "content": blocks})
        elif role == "tool":
            tool_result = {
                "type": "tool_result",
                "tool_use_id": m.get("tool_call_id", ""),
                "content": str(content),
            }
            found = False
            for am in reversed(anthropic_msgs):
                if am["role"] == "assistant":
                    am["content"] = [{"type": "tool_result", "tool_use_id": m.get("tool_call_id", ""), "content": str(content)}] + am["content"]
                    found = True
                    break
            if not found:
                anthropic_msgs.append({"role": "user", "content": [tool_result]})
    return anthropic_msgs
