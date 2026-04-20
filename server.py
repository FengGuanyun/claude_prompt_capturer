"""
Multi-User Session Server — Port 8080
Each user gets an isolated workspace + Docker container.
Cookie-based session management.
"""

import json
import os
import pty
import subprocess
import asyncio
import threading
import uuid
import re
import time
from urllib.parse import parse_qs, urlparse
from pathlib import Path
from flask import Flask, request, Response, stream_with_context, send_from_directory, make_response
from flask_cors import CORS
from datetime import datetime
import httpx

from agent.engine import AgentEngine
from agent.tools_registry import get_all_tools
from agent.engine import SkillTool
from session_manager import get_session_manager, SessionManager

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


def filter_haiku_lines(text):
    lines = text.split('\n')
    return '\n'.join(line for line in lines if 'claude-haiku-4-5-20251001' not in line)


# ─── Session Middleware ───

SYSTEM_PROMPT = """You are an expert full-stack developer agent with a React tool loop.

## Capabilities
- **React Tool Loop**: Think → Act → Observe cycles until the task is complete
- **Tool Use**: Read, Write, Edit, Bash, Glob, Grep, WebSearch, WebFetch, TaskCreate/Update/List, Skill, ToolSearch
- **Memory**: You have a 3-layer memory system (short-term recent messages, mid-term summaries, long-term project context)
- **Compression**: Long sessions are automatically summarized
- **Agent Team**: You can delegate subtasks by using `Skill` with `$frontend-dev`, `$backend-dev`, or `$tester`

## Web App Creation Rules
When asked to create a web app:
1. Plan the structure first
2. Write COMPLETE, working files using the Write tool
3. Save to `apps/` directory under a subdirectory named after the app
4. Use proper file extensions (.html, .css, .js, .py)
5. Verify files exist with Bash (ls, cat)
6. Tell the user the URL: `http://localhost/apps/{app_name}/`

## Important
- Write COMPLETE code — no truncation, no placeholders
- Do NOT describe what you will do — just create the files
- After creating files, verify them"""

# Per-session state
_engines: dict[str, AgentEngine] = {}
_event_logs: dict[str, list[dict]] = {}
_captured: dict[str, list[dict]] = {}

MAX_LOGS = 200
MAX_CAPTURED = 500


def _session_id_from_request() -> str | None:
    return request.cookies.get("session_id")


def _ensure_session() -> str:
    sm = get_session_manager()
    sid = _session_id_from_request()
    sid = sm.get_or_create_session(sid)
    if sid not in _event_logs:
        _event_logs[sid] = []
    if sid not in _captured:
        _captured[sid] = []
    return sid


def _set_session_cookie(response, session_id: str):
    response.set_cookie("session_id", session_id, max_age=86400 * 7, httponly=False, samesite="Lax")


def _log_event(session_id: str, event):
    if session_id not in _event_logs:
        _event_logs[session_id] = []
    _event_logs[session_id].append(event.to_dict())
    if len(_event_logs[session_id]) > MAX_LOGS:
        _event_logs[session_id].pop(0)


def _get_engine(session_id: str) -> AgentEngine:
    if session_id not in _engines:
        config = load_claude_config()
        _engines[session_id] = AgentEngine(
            system_prompt=SYSTEM_PROMPT,
            model=config["model"],
            api_key=config["api_key"],
            base_url=config["base_url"],
            max_tokens_before_compact=6000,
        )
    return _engines[session_id]


# ─── Debug Logs (shared) ───

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


# ─── HTML Routes ───

@app.route("/")
def index():
    sid = _ensure_session()
    resp = make_response((Path(__file__).parent / "terminal_ui.html").read_text())
    _set_session_cookie(resp, sid)
    return resp, 200, {"Content-Type": "text/html"}


@app.route("/terminal")
def terminal_page():
    sid = _ensure_session()
    resp = make_response((Path(__file__).parent / "terminal_ui.html").read_text())
    _set_session_cookie(resp, sid)
    return resp, 200, {"Content-Type": "text/html"}


@app.route("/agent")
def agent_page():
    sid = _ensure_session()
    resp = make_response((Path(__file__).parent / "agent_ui.html").read_text())
    _set_session_cookie(resp, sid)
    return resp, 200, {"Content-Type": "text/html"}


@app.route("/apps/<path:filename>")
def serve_app(filename):
    return send_from_directory(APPS_DIR, filename)


# ─── Session Info API ───

@app.route("/session/info", methods=["GET"])
def session_info():
    sid = _ensure_session()
    sm = get_session_manager()
    info = sm.get_session_info(sid)
    if info:
        resp = make_response(json.dumps(info))
        _set_session_cookie(resp, sid)
        return resp
    return {"error": "No session"}, 404


@app.route("/session/list", methods=["GET"])
def list_sessions():
    return {"sessions": get_session_manager().list_sessions()}


@app.route("/session/destroy", methods=["POST"])
def destroy_session():
    sid = _session_id_from_request()
    if sid:
        get_session_manager().destroy_session(sid)
        _engines.pop(sid, None)
        _event_logs.pop(sid, None)
        _captured.pop(sid, None)
        resp = make_response(json.dumps({"status": "ok"}))
        resp.delete_cookie("session_id")
        return resp
    return {"error": "No session"}, 400


# ─── Agent Chat API ───

@app.route("/chat", methods=["POST"])
def chat():
    sid = _ensure_session()
    data = request.json
    messages = data.get("messages", [])
    user_input = messages[-1].get("content", "") if messages else data.get("message", "")

    engine = _get_engine(sid)

    def generate():
        for event in engine.run(user_input):
            _log_event(sid, event)
            yield f"event: {event.type}\ndata: {json.dumps(event.to_dict(), ensure_ascii=False)}\n\n"

    resp = Response(
        stream_with_context(generate()),
        mimetype="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
    _set_session_cookie(resp, sid)
    return resp


@app.route("/reset", methods=["POST"])
def reset_engine():
    sid = _ensure_session()
    _engines.pop(sid, None)
    if sid in _event_logs:
        _event_logs[sid].clear()
    return {"status": "ok"}


@app.route("/logs", methods=["GET"])
def get_logs():
    sid = _ensure_session()
    return {"logs": _event_logs.get(sid, []), "count": len(_event_logs.get(sid, []))}


@app.route("/clear_logs", methods=["POST"])
def clear_logs_route():
    sid = _ensure_session()
    if sid in _event_logs:
        _event_logs[sid].clear()
    return {"status": "ok"}


# ─── Info API ───

@app.route("/health", methods=["GET"])
def health():
    sm = get_session_manager()
    return {
        "status": "ok",
        "active_sessions": len(sm.sessions),
    }


@app.route("/skills", methods=["GET"])
def list_skills():
    tool = SkillTool()
    skills = tool._discover_skills()
    return {"skills": skills, "count": len(skills)}


@app.route("/tools", methods=["GET"])
def list_tools():
    tools = get_all_tools()
    return {
        "tools": [{"name": t.name, "description": t.description, "read_only": t.is_read_only()} for t in tools.values()],
        "count": len(tools),
    }


@app.route("/team", methods=["GET"])
def team_status():
    return get_team().get_team_status()


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


# ─── Proxy / Capture API (per-session) ───

@app.route("/api_key", methods=["GET"])
def get_api_key():
    return load_claude_config()


@app.route("/captured", methods=["GET"])
def get_captured():
    sid = _ensure_session()
    return {"requests": _captured.get(sid, []), "count": len(_captured.get(sid, []))}


@app.route("/captured/<int:index>", methods=["GET"])
def get_captured_detail(index: int):
    sid = _ensure_session()
    captured = _captured.get(sid, [])
    if 0 <= index < len(captured):
        return captured[index]
    return {"error": "Not found"}, 404


@app.route("/captured", methods=["DELETE"])
def clear_captured():
    sid = _ensure_session()
    if sid in _captured:
        _captured[sid].clear()
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
        sid = _ensure_session()
        captured = _captured.setdefault(sid, [])

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
        captured.append(entry)
        if len(captured) > MAX_CAPTURED:
            captured.pop(0)

        debug_entry = {
            "timestamp": datetime.now().strftime("%H:%M:%S.%f")[:-3],
            "session_id": sid,
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


# ─── WebSocket PTY (per-session) ───

def run_ws():
    import websockets

    async def pty_handler(websocket, path=None):
        path_str = path if path else (websocket.path if hasattr(websocket, 'path') else '')
        parsed = urlparse(path_str)
        qs = parse_qs(parsed.query)
        session_id = qs.get("session_id", [None])[0]

        if not session_id:
            sm = get_session_manager()
            sessions = sm.list_sessions()
            if sessions:
                session_id = sessions[0]["session_id"]
            else:
                session_id = sm.get_or_create_session()

        sm = get_session_manager()
        sm.get_or_create_session(session_id)

        master_fd = None
        loop = None

        try:
            master_fd, pid = sm.start_session_pty(session_id)
            sm.sessions[session_id]["last_active"] = time.time()

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
                        continue
                    except websockets.exceptions.ConnectionClosed:
                        break

            async def write_input():
                try:
                    async for msg in websocket:
                        if master_fd is not None:
                            os.write(master_fd, msg.encode('utf-8'))
                except Exception:
                    pass

            await asyncio.gather(process_output(), write_input())

        except Exception as e:
            print(f"[ws] PTY error: {e}")
        finally:
            if master_fd is not None:
                if loop:
                    try:
                        loop.remove_reader(master_fd)
                    except Exception:
                        pass
                try:
                    os.close(master_fd)
                except Exception:
                    pass

    async def ws_server():
        async with websockets.serve(pty_handler, "0.0.0.0", 8081):
            await asyncio.Future()

    asyncio.run(ws_server())


# ─── Main ───

if __name__ == "__main__":
    ws_thread = threading.Thread(target=run_ws, daemon=True)
    ws_thread.start()

    # Try building session image (skip if Docker unavailable)
    sm = get_session_manager()
    try:
        sm.build_session_image()
    except Exception as e:
        print(f"[session] Docker image build skipped: {e}")

    print("=" * 50)
    print("Multi-User Session Server — Port 8080")
    print("=" * 50)
    print()
    print("Home (Terminal):  http://localhost:8080")
    print("Agent UI:         http://localhost:8080/agent")
    print("WebSocket PTY:    ws://localhost:8081")
    print("Session Info:     http://localhost:8080/session/info")
    print()
    print("=" * 50)

    app.run(host="0.0.0.0", port=8080, debug=False, threaded=True)
