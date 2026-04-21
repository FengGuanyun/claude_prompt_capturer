"""
Unified Server — Terminal UI + API Proxy + WebSocket PTY
No agent chat — agent code is in server.py (local demo)
"""

import json
import os
import re
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
                        # Create temp opencode config for proxy routing
                        config_fd, opencode_config_path = tempfile.mkstemp(
                            suffix='.json', prefix='opencode_'
                        )
                        opencode_config = {
                            "provider": {
                                "anthropic": {
                                    "options": {
                                        "apiKey": config["api_key"],
                                        "baseURL": "http://localhost:8080/v1"
                                    }
                                }
                            },
                            "model": f"anthropic/{config['model']}"
                        }
                        os.write(config_fd, json.dumps(opencode_config, indent=2).encode())
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
                        config_fd, opencode_config_path = tempfile.mkstemp(
                            suffix='.json', prefix='opencode_'
                        )
                        opencode_config = {
                            "provider": {
                                "anthropic": {
                                    "options": {
                                        "apiKey": config["api_key"],
                                        "baseURL": "http://localhost:8080/v1"
                                    }
                                }
                            },
                            "model": f"anthropic/{config['model']}"
                        }
                        os.write(config_fd, json.dumps(opencode_config, indent=2).encode())
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
