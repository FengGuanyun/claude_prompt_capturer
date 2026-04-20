"""
Unified Server — Terminal UI + API Proxy + WebSocket PTY
No agent chat — agent code is in server.py (local demo)
"""

import json
import os
import re
import pty
import subprocess
import asyncio
import threading
from pathlib import Path
from flask import Flask, request, Response, stream_with_context, send_from_directory
from flask_cors import CORS
from datetime import datetime
import httpx

app = Flask(__name__, static_folder=None)
CORS(app)

APPS_DIR = Path(__file__).parent / "apps"
APPS_DIR.mkdir(exist_ok=True)

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
    return (Path(__file__).parent / "terminal_ui.html").read_text(), 200, {"Content-Type": "text/html"}


@app.route("/terminal")
def terminal_page():
    return (Path(__file__).parent / "terminal_ui.html").read_text(), 200, {"Content-Type": "text/html"}


@app.route("/ui")
def ui():
    return (Path(__file__).parent / "terminal_ui.html").read_text(), 200, {"Content-Type": "text/html"}


@app.route("/capture")
def capture():
    return (Path(__file__).parent / "terminal_ui.html").read_text(), 200, {"Content-Type": "text/html"}


@app.route("/apps/<path:filename>")
def serve_app(filename):
    return send_from_directory(APPS_DIR, filename)


# ─── Info API ───

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
    try:
        req_data = request.get_json()
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
        }
        write_debug_log(debug_entry)

        config = load_claude_config()
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


# ─── WebSocket PTY ───

pty_procs = {}

def run_ws():
    import websockets

    async def pty_handler(websocket):
        proc_id = id(websocket)
        master_fd = None
        proc = None
        settings_path = None

        try:
            master_fd, slave_fd = pty.openpty()
            env = os.environ.copy()
            env["TERM"] = "xterm-256color"

            config = load_claude_config()
            proxy_settings = {
                "env": {
                    "ANTHROPIC_AUTH_TOKEN": config["api_key"],
                    "ANTHROPIC_BASE_URL": "http://localhost:8080",
                    "ANTHROPIC_MODEL": config["model"]
                }
            }
            import tempfile
            settings_fd, settings_path = tempfile.mkstemp(suffix='.json')
            os.write(settings_fd, json.dumps(proxy_settings, indent=2).encode())
            os.close(settings_fd)

            proc = subprocess.Popen(
                ["claude", "--settings", settings_path],
                stdin=slave_fd,
                stdout=slave_fd,
                stderr=slave_fd,
                env=env,
                preexec_fn=os.setsid
            )

            os.close(slave_fd)
            pty_procs[proc_id] = (master_fd, proc)
            loop = asyncio.get_event_loop()
            q = asyncio.Queue()

            def reader_callback():
                try:
                    data = os.read(master_fd, 1024)
                    q.put_nowait(data if data else None)
                except Exception:
                    q.put_nowait(None)

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
        finally:
            if settings_path:
                try:
                    os.unlink(settings_path)
                except Exception:
                    pass
            if proc:
                try:
                    proc.terminate()
                except Exception:
                    pass
            if master_fd is not None:
                try:
                    loop.remove_reader(master_fd)
                except Exception:
                    pass
                try:
                    os.close(master_fd)
                except Exception:
                    pass
            pty_procs.pop(proc_id, None)

    async def ws_server():
        async with websockets.serve(pty_handler, "0.0.0.0", 8081):
            await asyncio.Future()

    asyncio.run(ws_server())


# ─── Main ───

if __name__ == "__main__":
    ws_thread = threading.Thread(target=run_ws, daemon=True)
    ws_thread.start()

    print("=" * 50)
    print("Unified Server — Port 8080")
    print("=" * 50)
    print()
    print("Home (Terminal):  http://localhost:8080")
    print("WebSocket PTY:    ws://localhost:8081")
    print()
    print("=" * 50)

    app.run(host="0.0.0.0", port=8080, debug=False, threaded=True)
