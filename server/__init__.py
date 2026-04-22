"""Unified Server — Terminal UI + API Proxy + WebSocket PTY + Agent Demo."""

import httpx
import os
import platform
import sys
import threading
from pathlib import Path

from flask import Flask, request, Response, send_from_directory
from flask_cors import CORS

from .config import load_claude_config
from .capture import captured_requests, read_debug_logs, DEBUG_LOG_FILE, write_debug_log
from .proxy import proxy_to_api, chat_completions
from .translate import translate_text
from .agent import agent_chat

IS_WINDOWS = platform.system() == "Windows"

app = Flask(__name__, static_folder=None)
CORS(app)

APPS_DIR = Path(__file__).parent.parent / "apps"
APPS_DIR.mkdir(exist_ok=True)

# ─── HTML Routes ──────────────────────────────────────────────────────────────

@app.route("/")
@app.route("/terminal")
@app.route("/ui")
@app.route("/capture")
def index():
    resp = Response(
        (Path(__file__).parent.parent / "terminal_ui.html").read_text(encoding="utf-8"),
        200,
        {"Content-Type": "text/html", "Cache-Control": "no-store, no-cache, must-revalidate"},
    )
    return resp


@app.route("/agent")
def agent_page():
    resp = Response(
        (Path(__file__).parent.parent / "agent_ui.html").read_text(encoding="utf-8"),
        200,
        {"Content-Type": "text/html", "Cache-Control": "no-store, no-cache, must-revalidate"},
    )
    return resp


@app.route("/apps/<path:filename>")
def serve_app(filename):
    return send_from_directory(APPS_DIR, filename)


# ─── Proxy Routes ─────────────────────────────────────────────────────────────

@app.route("/apps/anthropic/v1/messages", methods=["POST"])
def opencode_proxy():
    return proxy_to_api(base_url_path="apps/anthropic/v1/messages")


@app.route("/v1/messages", methods=["POST"])
def proxy_api():
    return proxy_to_api()


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


@app.route("/v1/chat/completions", methods=["POST"])
def chat_completions_route():
    return chat_completions()


# ─── Capture / Config API ─────────────────────────────────────────────────────

@app.route("/api_key", methods=["GET"])
def get_api_key():
    return load_claude_config()


@app.route("/tools", methods=["GET"])
def get_tools():
    return {"available": ["claude", "opencode"], "default": "claude"}


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


# ─── Translate ────────────────────────────────────────────────────────────────

@app.route("/translate", methods=["POST"])
def translate_route():
    return translate_text()


# ─── Agent ────────────────────────────────────────────────────────────────────

@app.route("/chat", methods=["POST"])
def chat_route():
    return agent_chat()


# ─── Health ───────────────────────────────────────────────────────────────────

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


# ─── WebSocket PTY ────────────────────────────────────────────────────────────

from .ws_handler import start_ws_handler as _start_ws_handler
