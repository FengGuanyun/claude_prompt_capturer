"""
Unified Server — Terminal UI + API Proxy + WebSocket PTY
No agent chat — agent code is in server.py (local demo)
"""

import json
import os
import re
import sys
import subprocess
import asyncio
import tempfile
import threading
import platform
from pathlib import Path
from flask import Flask, request, Response, stream_with_context, send_from_directory
from flask_cors import CORS
from datetime import datetime
import httpx

IS_WINDOWS = platform.system() == "Windows"

if not IS_WINDOWS:
    import pty

app = Flask(__name__, static_folder=None)
CORS(app)

APPS_DIR = Path(__file__).parent / "apps"
APPS_DIR.mkdir(exist_ok=True)

# ─── WebSocket handler ───
ws_handler = None

# ─── Config ───

def load_claude_config() -> dict:
    settings_path = Path.home() / ".claude" / "settings.json"
    if settings_path.exists():
        try:
            with open(settings_path) as f:
                settings = json.load(f)
                env = settings.get("env", {})
                return {
                    "api_key": env.get("ANTHROPIC_AUTH_TOKEN", ""),
                    "base_url": env.get("ANTHROPIC_BASE_URL", "https://api.anthropic.com"),
                    "model": env.get("ANTHROPIC_MODEL", "claude-sonnet-4-6"),
                }
        except Exception:
            pass
    return {"api_key": "", "base_url": "https://api.anthropic.com", "model": "claude-sonnet-4-6"}


def estimate_tokens(text: str) -> int:
    if not text:
        return 0
    chinese = sum(1 for c in text if '\u4e00' <= c <= '\u9fff')
    other = len(text) - chinese
    return chinese // 2 + other // 4


# ─── Captured Prompts ───

captured_requests: list[dict] = []

# ─── Debug Logs ───

DEBUG_LOG_FILE = Path(__file__).parent / "proxy_debug_logs.jsonl"

def write_debug_log(entry: dict):
    with open(DEBUG_LOG_FILE, "a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False, default=str) + "\n")

def read_debug_logs(limit: int = 50) -> list:
    if not DEBUG_LOG_FILE.exists():
        return []
    lines = []
    with open(DEBUG_LOG_FILE, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                try:
                    lines.append(json.loads(line))
                except json.JSONDecodeError:
                    pass
    return lines[-limit:]


def filter_haiku_lines(text):
    lines = text.split('\n')
    return '\n'.join(line for line in lines if 'claude-haiku-4-5-20251001' not in line)


# ─── HTML Routes ───

@app.route("/")
def index():
    resp = Response((Path(__file__).parent / "terminal_ui.html").read_text(encoding="utf-8"), 200, {"Content-Type": "text/html", "Cache-Control": "no-store, no-cache, must-revalidate"})
    return resp


@app.route("/terminal")
def terminal_page():
    resp = Response((Path(__file__).parent / "terminal_ui.html").read_text(encoding="utf-8"), 200, {"Content-Type": "text/html", "Cache-Control": "no-store, no-cache, must-revalidate"})
    return resp


@app.route("/ui")
def ui():
    resp = Response((Path(__file__).parent / "terminal_ui.html").read_text(encoding="utf-8"), 200, {"Content-Type": "text/html", "Cache-Control": "no-store, no-cache, must-revalidate"})
    return resp


@app.route("/capture")
def capture():
    resp = Response((Path(__file__).parent / "terminal_ui.html").read_text(encoding="utf-8"), 200, {"Content-Type": "text/html", "Cache-Control": "no-store, no-cache, must-revalidate"})
    return resp


@app.route("/agent")
def agent_page():
    resp = Response((Path(__file__).parent / "agent_ui.html").read_text(encoding="utf-8"), 200, {"Content-Type": "text/html", "Cache-Control": "no-store, no-cache, must-revalidate"})
    return resp


@app.route("/apps/<path:filename>")
def serve_app(filename):
    return send_from_directory(APPS_DIR, filename)


# ─── OpenCode Proxy ───

@app.route("/apps/anthropic/v1/messages", methods=["POST"])
def opencode_proxy():
    """Proxy for opencode (uses Anthropic-compatible API via DashScope)."""
    return _proxy_to_api(base_url_path="apps/anthropic/v1/messages")


def _proxy_to_api(base_url_path=None):
    """Core proxy logic used by both Claude and opencode."""
    try:
        req_data = request.get_json()

        # Strip provider prefix from model name (opencode sends "anthropic/claude-xxx")
        model = req_data.get("model", "")
        if "/" in model:
            model = model.split("/", 1)[1]
            req_data["model"] = model

        body = json.dumps(req_data)

        messages = req_data.get("messages", [])
        system = req_data.get("system", [])
        tools = req_data.get("tools", [])

        parsed_messages = []
        system_reminders = []
        for msg in messages:
            role = msg.get("role", "unknown")
            content = msg.get("content", "")
            content_array = []
            if isinstance(content, list):
                text_parts = []
                for b in content:
                    if isinstance(b, dict):
                        if b.get("type") == "text":
                            text = b.get("text", "")
                            if text and "<system-reminder>" in text:
                                for m in re.finditer(r'<system-reminder>(.*?)</system-reminder>', text, re.DOTALL):
                                    reminder_content = filter_haiku_lines(m.group(1).strip())
                                    system_reminders.append(reminder_content)
                                    content_array.append({"type": "system-reminder", "text": reminder_content})
                                    text_parts.append(f"[SYSTEM REMINDER] {reminder_content}")
                                outer_text = filter_haiku_lines(re.sub(r'<system-reminder>.*?</system-reminder>', '', text, flags=re.DOTALL).strip())
                                if outer_text:
                                    text_parts.append(outer_text)
                                continue
                            filtered_text = filter_haiku_lines(text)
                            text_parts.append(filtered_text)
                            content_array.append({"type": "text", "text": filtered_text})
                        elif b.get("type") == "thinking":
                            thinking_text = filter_haiku_lines(b.get("text", ""))
                            if thinking_text:
                                text_parts.append(f"<thinking>{thinking_text}</thinking>")
                                content_array.append({"type": "thinking", "text": thinking_text})
                        elif b.get("type") == "tool_use":
                            tool_name = b.get('name', b.get('id', '?'))
                            tool_input = b.get('input', {})
                            text_parts.append(f"[Tool: {tool_name}]")
                            content_array.append({
                                "type": "tool_use",
                                "id": b.get('id', ''),
                                "name": tool_name,
                                "input": tool_input
                            })
                        elif b.get("type") == "tool_result":
                            tool_use_id = b.get('tool_use_id', '')
                            result_content = b.get('content', '')
                            is_error = b.get('is_error', False)
                            text_parts.append("[Tool Result]")
                            content_array.append({
                                "type": "tool_result",
                                "tool_use_id": tool_use_id,
                                "content": result_content,
                                "is_error": is_error,
                                "role": "tool"
                            })
                        else:
                            text_parts.append(json.dumps(b, ensure_ascii=False))
                            content_array.append(b)
                    else:
                        filtered = filter_haiku_lines(str(b))
                        text_parts.append(filtered)
                        content_array.append({"type": "text", "text": filtered})
                content = "\n".join(text_parts)
            elif isinstance(content, dict):
                filtered = filter_haiku_lines(content.get("text", json.dumps(content, ensure_ascii=False)))
                content_array.append({"type": "text", "text": filtered})
                content = filtered
            else:
                filtered = filter_haiku_lines(str(content))
                content_array.append({"type": "text", "text": filtered})
                content = filtered
            parsed_messages.append({
                "role": role,
                "content": str(content),
                "content_preview": str(content)[:500] if content else '',
                "content_array": content_array,
                "tokens": estimate_tokens(str(content))
            })

        system_text = ""
        if isinstance(system, list):
            parts = []
            for s in system:
                if isinstance(s, str):
                    parts.append(s)
                elif isinstance(s, dict):
                    parts.append(s.get("text", json.dumps(s, ensure_ascii=False)))
                elif s:
                    parts.append(str(s))
            system_text = "\n".join(parts)
        elif isinstance(system, str):
            system_text = system
        elif isinstance(system, dict):
            system_text = system.get("text", json.dumps(system, ensure_ascii=False))

        system_text = filter_haiku_lines(system_text)

        entry = {
            "timestamp": datetime.now().strftime("%H:%M:%S.%f")[:-3],
            "model": req_data.get("model", "unknown"),
            "system_prompt": system_text,
            "system_tokens": estimate_tokens(system_text),
            "system_reminders": system_reminders,
            "messages": parsed_messages,
            "tools_count": len(tools),
            "tools": tools[:20]
        }
        captured_requests.append(entry)
        if len(captured_requests) > 500:
            captured_requests.pop(0)

        debug_entry = {
            "timestamp": datetime.now().strftime("%H:%M:%S.%f")[:-3],
            "raw_request": req_data,
            "parsed_entry": entry,
            "proxy": "opencode" if base_url_path else "claude",
        }
        write_debug_log(debug_entry)

        config = load_claude_config()
        if base_url_path:
            # opencode: forward to dashscope directly
            target_url = f"https://coding.dashscope.aliyuncs.com/{base_url_path}"
        else:
            target_url = f"{config['base_url']}/v1/messages"
        api_key = config["api_key"]

        headers = {
            "Content-Type": "application/json",
            "x-api-key": api_key,
            "anthropic-version": "2023-06-01",
            "anthropic-dangerous-direct-browser-access": "true",
        }
        for k, v in request.headers:
            if k in ('authorization', 'user-agent', 'x-app',
                     'x-claude-code-session-id', 'anthropic-beta'):
                headers[k] = v

        try:
            resp = httpx.post(target_url, content=body, headers=headers, timeout=120)
            ct = resp.headers.get("content-type", "")
            content_data = resp.content
            resp.close()
            debug_entry["response_status"] = resp.status_code
            debug_entry["response_content_type"] = ct
            debug_entry["response_preview"] = content_data[:500].decode("utf-8", errors="replace") if not b"text/event-stream" in ct.encode() else "(streaming)"
            write_debug_log(debug_entry)

            if "text/event-stream" in ct:
                return Response(content_data, mimetype="text/event-stream")
            else:
                return Response(f"data: {content_data.decode('utf-8')}\n\n",
                               mimetype="text/event-stream")
        except Exception as e:
            debug_entry["error"] = str(e)
            write_debug_log(debug_entry)
            return {"error": str(e)}, 500

    except Exception as e:
        return {"error": str(e)}, 500

@app.route("/health", methods=["GET"])
def health():
    return {"status": "ok"}


@app.route("/apps", methods=["GET"])
def list_apps():
    apps = []
    if APPS_DIR.exists():
        for item in sorted(APPS_DIR.iterdir(), key=lambda x: x.stat().st_mtime, reverse=True):
            if item.is_dir():
                apps.append({"name": item.name, "path": f"/apps/{item.name}/", "files": [f.name for f in item.iterdir() if f.is_file()]})
            elif item.is_file() and item.suffix == ".html":
                apps.append({"name": item.stem, "path": f"/apps/{item.name}", "files": [item.name]})
    return {"apps": apps, "count": len(apps)}


# ─── Proxy / Capture API ───

@app.route("/api_key", methods=["GET"])
def get_api_key():
    return load_claude_config()


@app.route("/tools", methods=["GET"])
def get_tools():
    return {
        "available": ["claude", "opencode"],
        "default": "claude"
    }


@app.route("/captured", methods=["GET"])
def get_captured():
    return {"requests": captured_requests, "count": len(captured_requests)}


@app.route("/captured/<int:index>", methods=["GET"])
def get_captured_detail(index: int):
    if 0 <= index < len(captured_requests):
        return captured_requests[index]
    return {"error": "Not found"}, 404


@app.route("/captured", methods=["DELETE"])
def clear_captured():
    captured_requests.clear()
    return {"status": "ok"}


@app.route("/debug_logs", methods=["GET"])
def get_debug_logs():
    limit = request.args.get("limit", 50, type=int)
    logs = read_debug_logs(limit)
    return {"logs": logs, "count": len(logs), "log_file": str(DEBUG_LOG_FILE)}


@app.route("/debug_logs", methods=["DELETE"])
def clear_debug_logs():
    if DEBUG_LOG_FILE.exists():
        DEBUG_LOG_FILE.unlink()
    return {"status": "ok"}


@app.route("/translate", methods=["POST"])
def translate_text():
    try:
        data = request.get_json()
        text = data.get("text", "").strip()
        if not text:
            return {"error": "No text provided"}, 400

        config = load_claude_config()
        target_url = f"{config['base_url']}/v1/messages"

        prompt = f"""Translate the following English text to Chinese.

RULES:
- Translate natural language sentences into natural Chinese
- Keep all tool names, function names, variable names, code snippets, file paths, URLs, flags, and technical terms in English
- Keep placeholder patterns like {{...}}, [...], <...> in their original form
- Keep ALL uppercase English words (abbreviations, proper nouns) as-is
- Do NOT translate anything that looks like a technical identifier

Original text:
{text}

Chinese translation:"""

        req_body = {
            "model": config["model"],
            "max_tokens": 4096,
            "messages": [{"role": "user", "content": prompt}]
        }

        headers = {
            "Content-Type": "application/json",
            "x-api-key": config["api_key"],
            "anthropic-version": "2023-06-01",
        }

        resp = httpx.post(target_url, json=req_body, headers=headers, timeout=30)
        if resp.status_code != 200:
            return {"error": f"API error: {resp.status_code}"}, resp.status_code

        result = resp.json()
        content = result.get("content", [])
        translation = ""
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text":
                translation = block.get("text", "")
                break

        translation = translation.replace("Chinese translation:", "").strip()
        return {"translation": translation}
    except Exception as e:
        return {"error": str(e)}, 500


@app.route("/v1/messages", methods=["POST"])
def proxy_api():
    return _proxy_to_api()


@app.route("/v1/chat/completions", methods=["POST"])
def chat_completions():
    """Accept OpenAI-format requests, convert to Anthropic, and stream back."""
    try:
        req_data = request.get_json()
        config = load_claude_config()

        # Convert OpenAI messages to Anthropic format
        messages = []
        system_messages = []
        for msg in req_data.get("messages", []):
            if msg["role"] == "system":
                system_messages.append({"type": "text", "text": msg.get("content", "")})
            elif msg["role"] == "tool":
                messages.append({
                    "role": "user",
                    "content": [{
                        "type": "tool_result",
                        "tool_use_id": msg.get("tool_call_id", ""),
                        "content": msg.get("content", ""),
                    }],
                })
            elif msg["role"] == "assistant":
                assistant_content = []
                if msg.get("content"):
                    assistant_content.append({"type": "text", "text": msg["content"]})
                if msg.get("tool_calls"):
                    for tc in msg["tool_calls"]:
                        func = tc.get("function", {})
                        try:
                            input_json = json.loads(func.get("arguments", "{}"))
                        except Exception:
                            input_json = {}
                        assistant_content.append({
                            "type": "tool_use",
                            "id": tc.get("id", "call_unknown"),
                            "name": func.get("name", "unknown"),
                            "input": input_json,
                        })
                messages.append({"role": "assistant", "content": assistant_content})
            else:
                messages.append({"role": msg["role"], "content": msg.get("content", "")})

        # Convert tools to Anthropic format
        tools = []
        for t in req_data.get("tools", []):
            func = t.get("function", {})
            tools.append({
                "name": func.get("name", ""),
                "description": func.get("description", ""),
                "input_schema": func.get("parameters", {"type": "object"}),
            })

        # Build Anthropic request
        anthropic_req = {
            "model": config["model"],
            "max_tokens": 8192,
            "system": system_messages if system_messages else None,
            "messages": messages,
            "tools": tools if tools else None,
            "stream": req_data.get("stream", False),
            "temperature": req_data.get("temperature", 0.7),
        }

        headers = {
            "Content-Type": "application/json",
            "x-api-key": config["api_key"],
            "anthropic-version": "2023-06-01",
        }

        target_url = f"{config['base_url']}/v1/messages"

        if req_data.get("stream", False):
            resp = httpx.post(target_url, json=anthropic_req, headers=headers, timeout=120)
            ct = resp.headers.get("content-type", "")
            content_data = resp.content
            resp.close()

            if "text/event-stream" in ct:
                # Convert Anthropic SSE to OpenAI SSE format
                def convert_stream():
                    buf = ""
                    for line in content_data.decode("utf-8", errors="replace").split("\n"):
                        if not line.startswith("data: "):
                            continue
                        raw = line[6:].strip()
                        if not raw or raw == "[DONE]":
                            continue
                        try:
                            evt = json.loads(raw)
                            evt_type = evt.get("type", "")
                            if evt_type == "content_block_delta":
                                delta_text = evt.get("delta", {}).get("text", "")
                                if delta_text:
                                    yield f"data: {json.dumps({'choices': [{'delta': {'content': delta_text}, 'index': 0}]})}\n\n"
                            elif evt_type == "content_block_start":
                                content_block = evt.get("content_block", {})
                                if content_block.get("type") == "tool_use":
                                    tool_name = content_block.get("name", "")
                                    tool_id = content_block.get("id", "")
                                    yield f"data: {json.dumps({'choices': [{'delta': {'tool_calls': [{'index': 0, 'id': tool_id, 'type': 'function', 'function': {'name': tool_name, 'arguments': ''}}]}, 'index': 0}]})}\n\n"
                            elif evt_type == "message_stop":
                                yield "data: [DONE]\n\n"
                        except Exception:
                            pass

                return Response(stream_with_context(convert_stream()), mimetype="text/event-stream")
            else:
                return Response(content_data, mimetype="text/event-stream")
        else:
            resp = httpx.post(target_url, json=anthropic_req, headers=headers, timeout=120)
            result = resp.json()
            resp.close()

            # Convert Anthropic response to OpenAI format
            content = ""
            tool_calls = []
            for block in result.get("content", []):
                if block.get("type") == "text":
                    content = block["text"]
                elif block.get("type") == "tool_use":
                    tool_calls.append({
                        "id": block["id"],
                        "type": "function",
                        "function": {"name": block["name"], "arguments": json.dumps(block.get("input", {}))},
                    })

            message = {"role": "assistant", "content": content or None}
            if tool_calls:
                message["tool_calls"] = tool_calls

            return {"choices": [{"message": message, "finish_reason": result.get("stop_reason", "stop"), "index": 0}]}
    except Exception as e:
        return {"error": str(e)}, 500


@app.route("/v1/messages", methods=["GET"])
def proxy_api_get():
    config = load_claude_config()
    target_url = f"{config['base_url']}/v1/messages"
    headers = {
        "x-api-key": config["api_key"],
        "anthropic-version": "2023-06-01",
    }
    try:
        resp = httpx.get(target_url, headers=headers, timeout=30)
        return Response(resp.text, status=resp.status_code, mimetype="application/json")
    except Exception as e:
        return {"error": str(e)}, 500


# ─── Agent Demo (mini-cc based) ───

import os as _os
import asyncio as _asyncio

# ── mini-cc tools ──
sys.path.insert(0, str(Path(__file__).parent / "mini-cc" / "src"))

async def _execute_bash(args: dict) -> str:
    command = args.get("command")
    if not command:
        return "执行命令时出错: command 不能为空"
    DANGEROUS = [
        re.compile(r'rm\s+-r[fF]?\s+/'), re.compile(r'mkfs\.'),
        re.compile(r'dd\s+if=.*of=/dev/sda'), re.compile(r'>\s*/dev/sd[a-z]'),
        re.compile(r'\$\([^)]+\)'), re.compile(r'`[^`]+`'),
    ]
    for pattern in DANGEROUS:
        if pattern.search(command):
            return f"命令执行被安全沙盒拒绝：包含高危指令模式 ({pattern.pattern})"
    try:
        process = await _asyncio.create_subprocess_shell(
            command, stdout=_asyncio.subprocess.PIPE,
            stderr=_asyncio.subprocess.PIPE, cwd=_os.getcwd()
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


def _emit_event(event_type, data=None, details=None):
    entry = {"timestamp": datetime.now().strftime("%H:%M:%S.%f")[:-3], "type": event_type}
    if data is not None:
        entry["data"] = data
    if details is not None:
        entry["details"] = details
    return f"data: {json.dumps(entry, ensure_ascii=False)}\n\n"


def _tool_definitions():
    return [{
        "type": "function",
        "function": {
            "name": t["name"],
            "description": t["description"],
            "parameters": t["inputSchema"],
        }
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
                result = result[:8000] + '\n\n...[由于内容过长，已被系统 microcompact 机制截断]...'
            results.append({"id": call["id"], "name": call["name"], "result": result, "isError": False})
        except Exception as e:
            results.append({"id": call["id"], "name": call["name"], "result": f"执行工具 {call['name']} 时出错: {str(e)}", "isError": True})
    return results


def _estimate_tokens(text: str) -> int:
    if not text:
        return 0
    chinese = sum(1 for c in text if '一' <= c <= '鿿')
    other = len(text) - chinese
    return chinese // 2 + other // 4


@app.route("/chat", methods=["POST"])
def agent_chat():
    """Real mini-cc agent loop — streams events with full execution flow."""
    try:
        data = request.get_json()
        user_message = data.get("message", "")

        def generate():
            config = load_claude_config()
            api_key = config["api_key"]
            base_url = config["base_url"]
            model = config["model"]

            # --- Anthropic format helpers ---

            def _to_anthropic_messages(msgs):
                """Convert OpenAI-style messages to Anthropic message content blocks."""
                anthropic_msgs = []
                for m in msgs:
                    role = m["role"]
                    content = m.get("content", "")
                    if role == "system":
                        continue  # system handled separately
                    if role == "user":
                        if isinstance(content, list):
                            anthropic_msgs.append({"role": "user", "content": content})
                        else:
                            anthropic_msgs.append({"role": "user", "content": [{"type": "text", "text": str(content)}]})
                    elif role == "assistant":
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
                        # Find the preceding assistant message and attach tool_result
                        tool_result = {
                            "type": "tool_result",
                            "tool_use_id": m.get("tool_call_id", ""),
                            "content": str(content),
                        }
                        # Find last assistant message to prepend this tool_result
                        found = False
                        for am in reversed(anthropic_msgs):
                            if am["role"] == "assistant":
                                # Prepend tool_result content blocks
                                am["content"] = [{"type": "tool_result", "tool_use_id": m.get("tool_call_id", ""), "content": str(content)}] + am["content"]
                                found = True
                                break
                        if not found:
                            # Fallback: add as user message
                            anthropic_msgs.append({"role": "user", "content": [tool_result]})
                return anthropic_msgs

            def _to_anthropic_tools(tools):
                """Convert OpenAI function tools to Anthropic tool format."""
                return [{
                    "name": t["name"],
                    "description": t["description"],
                    "input_schema": t["inputSchema"],
                } for t in AGENT_TOOLS]

            # Use a queue to bridge async generator -> sync Flask stream
            import queue as _queue
            q = _queue.Queue()
            sentinel = object()

            async def run_agent():
                import httpx as _httpx

                max_loops = 100
                loop_count = 0
                system_prompt = "你是一名agent编程助手。你拥有读取文件、写入文件和执行终端命令的权限。你的目标是帮助用户解决复杂的软件工程问题。"

                messages = [{"role": "user", "content": user_message}]

                # First call: always make the request
                make_request = True

                while make_request:
                    # Build prompt details for display
                    prompt_msgs = []
                    for m in messages:
                        raw = m.get("content", "")
                        if isinstance(raw, list):
                            # Anthropic-style content blocks
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
                        content_preview = content[:200]
                        prompt_msgs.append({
                            "role": m["role"],
                            "content": content,
                            "tokens": _estimate_tokens(content),
                            "content_preview": content_preview,
                        })

                    prompt_details = {
                        "system_prompt": system_prompt,
                        "messages": prompt_msgs[-10:],
                        "tools": [{"name": t["name"], "description": t["description"]} for t in AGENT_TOOLS],
                    }
                    q.put(_emit_event("prompt_assembled", details=prompt_details))
                    q.put(_emit_event("waiting", details={"message": "等待模型响应..."}))

                    # Build Anthropic request
                    anthropic_request = {
                        "model": model,
                        "system": system_prompt,
                        "messages": _to_anthropic_messages(messages),
                        "tools": _to_anthropic_tools(AGENT_TOOLS),
                        "max_tokens": 8192,
                        "stream": True,
                    }

                    # Call Anthropic API via httpx
                    headers = {
                        "x-api-key": api_key,
                        "anthropic-version": "2023-06-01",
                        "content-type": "application/json",
                    }

                    full_content = ''
                    tool_calls = []

                    async with _httpx.AsyncClient(timeout=120.0) as client:
                        resp = await client.post(
                            base_url + "/v1/messages",
                            json={**anthropic_request, "stream": False},
                            headers=headers,
                        )
                        if resp.status_code != 200:
                            q.put(_emit_event("error", details={"message": f"API 请求失败 (HTTP {resp.status_code}): {resp.text[:500]}"}))
                            return

                        result = resp.json()

                        # Parse response content blocks
                        for block in result.get("content", []):
                            if block.get("type") == "text":
                                text = block.get("text", "")
                                full_content += text
                                q.put(_emit_event("text", data=text))
                            elif block.get("type") == "tool_use":
                                tool_calls.append(block)
                            elif block.get("type") == "thinking":
                                pass

                    # Build assistant message content blocks for next round
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
                        # No tool calls — just a text reply, done
                        make_request = False
                        continue

                    # ── Tool calls exist — enter iteration loop ──

                    # Emit tool call events
                    for tc in tool_calls:
                        q.put(_emit_event("tool_call", details={
                            "name": tc["name"],
                            "input": tc.get("input", {}),
                        }))

                    # Execute tools — convert Anthropic tool_call format to internal format
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

                    # Emit iteration_start before tool results
                    loop_count += 1
                    q.put(_emit_event("iteration_start"))

                    for tr in tool_results:
                        q.put(_emit_event("tool_result", details={
                            "name": tr["name"],
                            "content": str(tr["result"]),
                            "is_error": tr.get("isError", False),
                        }))

                    # Add tool results to messages in Anthropic format
                    for tr in tool_results:
                        messages.append({
                            "role": "user",
                            "content": [{
                                "type": "tool_result",
                                "tool_use_id": tr["id"],
                                "content": str(tr["result"]),
                            }],
                        })

                    q.put(_emit_event("iteration_end"))

                    if loop_count >= max_loops:
                        q.put(_emit_event("text", data="\n[Agent] 工具调用循环次数过多，已强制终止。\n"))
                        return

            def async_runner():
                new_loop = _asyncio.new_event_loop()
                _asyncio.set_event_loop(new_loop)
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


# ─── WebSocket PTY (integrated on same port) ───

pty_procs = {}

if IS_WINDOWS:
    from winpty import PTY

def start_ws_handler(flask_app):
    """Start WebSocket handler via async websockets server."""
    import websockets
    import socket

    ws_port = 8081

    def ws_thread_func():
        async def pty_handler(websocket):
            # Parse tool from query string: ?tool=claude or ?tool=opencode
            from urllib.parse import parse_qs, urlparse
            path = websocket.request.path if hasattr(websocket, 'request') else ''
            qs = parse_qs(urlparse(path).query)
            tool = qs.get('tool', ['claude'])[0]

            proc_id = id(websocket)

            try:
                config = load_claude_config()

                if IS_WINDOWS:
                    pty_inst = PTY(rows=40, cols=120)

                    if tool == "opencode":
                        # Mirror user's opencode config but route baseURL through proxy
                        global_config = Path.home() / ".config" / "opencode" / "opencode.json"
                        if global_config.exists():
                            try:
                                with open(global_config) as f:
                                    oc = json.load(f)
                            except Exception:
                                oc = {}
                        else:
                            oc = {}

                        # Rewrite all provider baseURLs to go through our proxy
                        for prov in oc.get("provider", {}).values():
                            opts = prov.get("options", {})
                            if opts and "baseURL" in opts:
                                opts["baseURL"] = "http://localhost:8080/v1"
                            for mod in prov.get("models", {}).values():
                                mod_opts = mod.get("options", {})
                                if mod_opts and "baseURL" in mod_opts:
                                    mod_opts["baseURL"] = "http://localhost:8080/v1"

                        # Remove $schema to skip validation
                        oc.pop("$schema", None)

                        config_fd, opencode_config_path = tempfile.mkstemp(
                            suffix='.json', prefix='opencode_'
                        )
                        os.write(config_fd, json.dumps(oc, indent=2).encode())
                        os.close(config_fd)
                        os.environ["OPENCODE_CONFIG"] = opencode_config_path
                        cmd = "opencode"
                    else:
                        # claude: use temp settings file with proxy config
                        proxy_settings = {
                            "env": {
                                "ANTHROPIC_AUTH_TOKEN": config["api_key"],
                                "ANTHROPIC_BASE_URL": "http://localhost:8080",
                                "ANTHROPIC_MODEL": config["model"]
                            }
                        }
                        settings_fd, settings_path = tempfile.mkstemp(suffix='.json')
                        os.write(settings_fd, json.dumps(proxy_settings, indent=2).encode())
                        os.close(settings_fd)
                        cmd = f'claude --settings "{settings_path}"'

                    pty_inst.spawn("C:\\Windows\\System32\\cmd.exe", cmdline=f'cmd /c {cmd}')

                    import queue
                    q = queue.Queue()

                    def read_pty():
                        while True:
                            try:
                                data = pty_inst.read()
                                if data:
                                    q.put(data)
                                else:
                                    import time
                                    time.sleep(0.05)
                            except Exception:
                                q.put(None)
                                break

                    read_thread = threading.Thread(target=read_pty, daemon=True)
                    read_thread.start()

                    async def process_output():
                        loop = asyncio.get_event_loop()
                        while True:
                            data = await loop.run_in_executor(None, q.get)
                            if data is None:
                                break
                            try:
                                await websocket.send(data)
                            except Exception:
                                break

                    async def write_input():
                        try:
                            async for msg in websocket:
                                pty_inst.write(msg)
                        except Exception:
                            pass

                    await asyncio.gather(process_output(), write_input())
                else:
                    master_fd, slave_fd = pty.openpty()
                    env = os.environ.copy()
                    env["TERM"] = "xterm-256color"

                    if tool == "opencode":
                        # Mirror user's opencode config but route baseURL through proxy
                        global_config = Path.home() / ".config" / "opencode" / "opencode.json"
                        if global_config.exists():
                            try:
                                with open(global_config) as f:
                                    oc = json.load(f)
                            except Exception:
                                oc = {}
                        else:
                            oc = {}

                        # Rewrite all provider baseURLs to go through our proxy
                        for prov in oc.get("provider", {}).values():
                            opts = prov.get("options", {})
                            if opts:
                                opts["baseURL"] = "http://localhost:8080/v1"
                            for mod in prov.get("models", {}).values():
                                mod_opts = mod.get("options", {})
                                if mod_opts:
                                    mod_opts["baseURL"] = "http://localhost:8080/v1"

                        config_fd, opencode_config_path = tempfile.mkstemp(
                            suffix='.json', prefix='opencode_'
                        )
                        os.write(config_fd, json.dumps(oc, indent=2).encode())
                        os.close(config_fd)
                        env["OPENCODE_CONFIG"] = opencode_config_path
                        cmd = ["opencode"]
                        settings_path = opencode_config_path
                    else:
                        proxy_settings = {
                            "env": {
                                "ANTHROPIC_AUTH_TOKEN": config["api_key"],
                                "ANTHROPIC_BASE_URL": "http://localhost:8080",
                                "ANTHROPIC_MODEL": config["model"]
                            }
                        }
                        settings_fd, settings_path = tempfile.mkstemp(suffix='.json')
                        os.write(settings_fd, json.dumps(proxy_settings, indent=2).encode())
                        os.close(settings_fd)
                        cmd = ["claude", "--settings", settings_path]

                    proc = subprocess.Popen(
                        cmd,
                        stdin=slave_fd,
                        stdout=slave_fd,
                        stderr=slave_fd,
                        env=env,
                        preexec_fn=os.setsid
                    )

                    os.close(slave_fd)
                    pty_procs[proc_id] = (master_fd, proc)

                    q = asyncio.Queue()

                    def reader_callback():
                        try:
                            data = os.read(master_fd, 1024)
                            q.put_nowait(data if data else None)
                        except Exception:
                            q.put_nowait(None)

                    loop = asyncio.get_event_loop()
                    loop.add_reader(master_fd, reader_callback)

                    async def process_output():
                        while True:
                            try:
                                data = await asyncio.wait_for(q.get(), timeout=0.1)
                                if data is None:
                                    break
                                await websocket.send(data.decode('utf-8', errors='replace'))
                            except asyncio.TimeoutError:
                                if proc.poll() is not None:
                                    break
                                continue

                    async def write_input():
                        try:
                            async for msg in websocket:
                                if master_fd is not None:
                                    os.write(master_fd, msg.encode('utf-8'))
                        except Exception:
                            pass

                    await asyncio.gather(process_output(), write_input())

            except Exception as e:
                print(f"PTY error: {e}")
                import traceback
                traceback.print_exc()
            finally:
                if 'opencode_config_path' in locals():
                    try:
                        os.unlink(opencode_config_path)
                    except Exception:
                        pass
                if 'settings_path' in locals():
                    try:
                        os.unlink(settings_path)
                    except Exception:
                        pass
                if IS_WINDOWS and 'pty_inst' in locals():
                    try:
                        pty_inst.close()
                    except Exception:
                        pass
                elif not IS_WINDOWS:
                    if proc:
                        try:
                            proc.terminate()
                        except Exception:
                            pass
                    if master_fd is not None:
                        try:
                            os.close(master_fd)
                        except Exception:
                            pass
                pty_procs.pop(proc_id, None)

        async def main_server():
            async with websockets.serve(pty_handler, "0.0.0.0", ws_port):
                await asyncio.Future()

        asyncio.run(main_server())

    t = threading.Thread(target=ws_thread_func, daemon=True)
    t.start()
    return ws_port


# ─── Main ───

if __name__ == "__main__":
    ws_port = start_ws_handler(app)

    print("=" * 50)
    print("Unified Server — Port 8080")
    print("=" * 50)
    print()
    print("Home (Terminal):  http://localhost:8080")
    print(f"WebSocket PTY:    ws://localhost:{ws_port}")
    print()
    print("=" * 50)

    app.run(host="0.0.0.0", port=8080, debug=False, threaded=True)
