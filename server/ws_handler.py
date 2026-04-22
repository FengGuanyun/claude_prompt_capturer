"""WebSocket PTY handler — runs on port 8081, bridges browser ↔ PTY ↔ Claude/OpenCode."""

import asyncio
import json
import os
import platform
import queue
import signal
import subprocess
import tempfile
import threading
from pathlib import Path

from .config import load_claude_config

IS_WINDOWS = platform.system() == "Windows"

if IS_WINDOWS:
    from winpty import PTY

pty_procs = {}

def _cleanup_pty_locals():
    pass


def start_ws_handler(flask_app):
    """Start WebSocket handler via async websockets server."""
    import websockets
    import socket

    ws_port = 8081

    # Start the websockets server in a background thread
    def _run_server():
        async def pty_handler(websocket):
            from urllib.parse import parse_qs, urlparse
            path = websocket.request.path if hasattr(websocket, 'request') else ''
            qs = parse_qs(urlparse(path).query)
            tool = qs.get('tool', ['claude'])[0]
            proc_id = id(websocket)

            try:
                config = load_claude_config()

                if IS_WINDOWS:
                    await _handle_windows_pty(websocket, tool, config, proc_id)
                else:
                    await _handle_unix_pty(websocket, tool, config, proc_id)

            except Exception as e:
                print(f"PTY error: {e}")
                import traceback
                traceback.print_exc()
            finally:
                _cleanup_pty_locals()

        async def _start():
            server = await websockets.serve(
                pty_handler, "0.0.0.0", ws_port,
                max_size=10**7, ping_interval=30, ping_timeout=10,
            )
            await server.wait_closed()

        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        loop.run_until_complete(_start())

    t = threading.Thread(target=_run_server, daemon=True)
    t.start()
    return ws_port


async def _build_opencode_config():
    """Mirror user's opencode config but route baseURLs through proxy."""
    global_config = Path.home() / ".config" / "opencode" / "opencode.json"
    if global_config.exists():
        try:
            with open(global_config) as f:
                oc = json.load(f)
        except Exception:
            oc = {}
    else:
        oc = {}

    for prov in oc.get("provider", {}).values():
        opts = prov.get("options", {})
        if opts and "baseURL" in opts:
            opts["baseURL"] = "http://localhost:8080/apps/anthropic"
        for mod in prov.get("models", {}).values():
            mod_opts = mod.get("options", {})
            if mod_opts and "baseURL" in mod_opts:
                mod_opts["baseURL"] = "http://localhost:8080/apps/anthropic"

    oc.pop("$schema", None)

    config_fd, opencode_config_path = tempfile.mkstemp(suffix='.json', prefix='opencode_')
    os.write(config_fd, json.dumps(oc, indent=2).encode())
    os.close(config_fd)
    return opencode_config_path


def _build_claude_proxy_settings(config: dict):
    """Create temp settings file that routes Claude through proxy."""
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
    return settings_path


async def _handle_windows_pty(websocket, tool, config, proc_id):
    # Start with default size, will be resized by client
    pty_inst = PTY(rows=24, cols=80)
    cleanup_paths = []

    try:
        if tool == "opencode":
            opencode_config_path = await _build_opencode_config()
            cleanup_paths.append(opencode_config_path)
            os.environ["OPENCODE_CONFIG"] = opencode_config_path
            cmd = "opencode"
        else:
            settings_path = _build_claude_proxy_settings(config)
            cleanup_paths.append(settings_path)
            cmd = f'claude --settings "{settings_path}"'

        pty_inst.spawn("C:\\Windows\\System32\\cmd.exe", cmdline=f'cmd /c {cmd}')

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
                    # Handle resize messages from client
                    if isinstance(msg, str):
                        try:
                            data = json.loads(msg)
                            if data.get('type') == 'resize':
                                cols = data.get('cols', 80)
                                rows = data.get('rows', 24)
                                pty_inst.resize(cols, rows)
                                continue
                        except json.JSONDecodeError:
                            pass
                    pty_inst.write(msg)
            except Exception:
                pass

        await asyncio.gather(process_output(), write_input())
    finally:
        for p in cleanup_paths:
            try:
                os.unlink(p)
            except Exception:
                pass
        try:
            pty_inst.close()
        except Exception:
            pass


async def _handle_unix_pty(websocket, tool, config, proc_id):
    import pty
    import struct
    import fcntl
    import termios

    env = os.environ.copy()
    env["TERM"] = "xterm-256color"
    cleanup_paths = []

    if tool == "opencode":
        opencode_config_path = await _build_opencode_config()
        cleanup_paths.append(opencode_config_path)
        env["OPENCODE_CONFIG"] = opencode_config_path
        cmd = ["opencode"]
    else:
        settings_path = _build_claude_proxy_settings(config)
        cleanup_paths.append(settings_path)
        cmd = ["claude", "--settings", settings_path]

    master_fd, slave_fd = pty.openpty()
    proc = subprocess.Popen(
        cmd, stdin=slave_fd, stdout=slave_fd, stderr=slave_fd,
        env=env, preexec_fn=os.setsid
    )
    os.close(slave_fd)
    pty_procs[proc_id] = (master_fd, proc)

    def resize_pty(cols, rows):
        """Resize the PTY using TIOCSWINSZ ioctl."""
        try:
            winsize = struct.pack('HHHH', rows, cols, 0, 0)
            fcntl.ioctl(master_fd, termios.TIOCSWINSZ, winsize)
            # Send SIGWINCH to notify the process
            os.kill(proc.pid, signal.SIGWINCH)
        except Exception:
            pass

    try:
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
            import signal
            try:
                async for msg in websocket:
                    if master_fd is not None:
                        # Handle resize messages from client
                        if isinstance(msg, str):
                            try:
                                data = json.loads(msg)
                                if data.get('type') == 'resize':
                                    cols = data.get('cols', 80)
                                    rows = data.get('rows', 24)
                                    resize_pty(cols, rows)
                                    continue
                            except json.JSONDecodeError:
                                pass
                        os.write(master_fd, msg.encode('utf-8'))
            except Exception:
                pass

        await asyncio.gather(process_output(), write_input())
    finally:
        for p in cleanup_paths:
            try:
                os.unlink(p)
            except Exception:
                pass
        try:
            os.close(master_fd)
        except Exception:
            pass
