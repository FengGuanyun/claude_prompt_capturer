"""
Session Manager — Docker-based user isolation.
Each user session runs in a dedicated container with a mounted workspace.
"""

import json
import os
import pty
import subprocess
import threading
import time
import uuid
from pathlib import Path

WORKSPACE_ROOT = Path(__file__).parent / "workspaces"
WORKSPACE_ROOT.mkdir(exist_ok=True)

# Container lifecycle:
# - Created on first access (cookie session_id)
# - Destroyed after 30 minutes of inactivity
# - Workspace persists on host filesystem

SESSION_TIMEOUT = 30 * 60  # 30 minutes


class SessionManager:
    """Manages per-user Docker containers and workspace directories."""

    def __init__(self, image_name: str = "claude-session"):
        self.sessions: dict[str, dict] = {}  # session_id -> {container_id, workspace, last_active, pid, master_fd}
        self.image_name = image_name
        self._lock = threading.Lock()
        # Start cleanup thread
        threading.Thread(target=self._cleanup_loop, daemon=True).start()

    def get_or_create_session(self, session_id: str | None = None) -> str:
        """Return existing session_id or create a new one."""
        if session_id and session_id in self.sessions:
            self.sessions[session_id]["last_active"] = time.time()
            return session_id

        new_id = session_id or uuid.uuid4().hex[:16]
        workspace = WORKSPACE_ROOT / new_id
        workspace.mkdir(exist_ok=True)

        with self._lock:
            self.sessions[new_id] = {
                "workspace": str(workspace),
                "last_active": time.time(),
                "created_at": time.time(),
                "container_id": None,
                "pid": None,
                "master_fd": None,
            }
        print(f"[session] created {new_id} -> {workspace}")
        return new_id

    def start_session_pty(self, session_id: str) -> tuple[int, int]:
        """Start a PTY process for the session.
        Returns (master_fd, pid).
        The process cwd is locked to the session workspace.
        """
        session = self.sessions.get(session_id)
        if not session:
            raise ValueError(f"Unknown session: {session_id}")

        workspace = session["workspace"]
        session["last_active"] = time.time()

        # Close previous PTY if exists
        if session.get("pid") is not None:
            try:
                os.kill(session["pid"], 9)
            except OSError:
                pass
            if session.get("master_fd"):
                try:
                    os.close(session["master_fd"])
                except OSError:
                    pass

        master_fd, slave_fd = pty.openpty()
        env = os.environ.copy()
        env["TERM"] = "xterm-256color"
        env["HOME"] = workspace
        env["WORKSPACE"] = workspace

        proc = subprocess.Popen(
            ["bash", "--norc", "--noprofile"],
            stdin=slave_fd,
            stdout=slave_fd,
            stderr=slave_fd,
            cwd=workspace,
            env=env,
            preexec_fn=os.setsid,
        )
        os.close(slave_fd)

        with self._lock:
            session["pid"] = proc.pid
            session["master_fd"] = master_fd

        return master_fd, proc.pid

    def get_pty_fds(self, session_id: str) -> tuple[int, int] | None:
        """Get existing PTY file descriptors for a session."""
        session = self.sessions.get(session_id)
        if not session or session.get("master_fd") is None:
            return None
        session["last_active"] = time.time()
        return session["master_fd"], session["pid"]

    def ensure_workspace(self, session_id: str) -> Path:
        """Return the workspace directory for a session, creating if needed."""
        session = self.sessions.get(session_id)
        if session:
            return Path(session["workspace"])
        workspace = WORKSPACE_ROOT / session_id
        workspace.mkdir(exist_ok=True)
        return workspace

    def destroy_session(self, session_id: str):
        """Clean up a session: kill PTY, remove container, keep workspace."""
        with self._lock:
            session = self.sessions.pop(session_id, None)
        if not session:
            return

        # Kill PTY process
        if session.get("pid"):
            try:
                os.kill(session["pid"], 9)
            except OSError:
                pass
        if session.get("master_fd"):
            try:
                os.close(session["master_fd"])
            except OSError:
                pass

        # Stop Docker container if running
        if session.get("container_id"):
            try:
                subprocess.run(
                    ["docker", "stop", "-t", "2", session["container_id"]],
                    capture_output=True, timeout=10
                )
                subprocess.run(
                    ["docker", "rm", session["container_id"]],
                    capture_output=True, timeout=10
                )
            except Exception as e:
                print(f"[session] cleanup error for {session_id}: {e}")

        print(f"[session] destroyed {session_id}")

    def build_session_image(self) -> bool:
        """Build the session Docker image. Run once at startup."""
        dockerfile = Path(__file__).parent / "Dockerfile.session"
        if not dockerfile.exists():
            print("[session] Dockerfile.session not found, skipping image build")
            return False

        result = subprocess.run(
            ["docker", "build", "-t", self.image_name, "-f", str(dockerfile), str(Path(__file__).parent)],
            capture_output=True, text=True, timeout=300
        )
        if result.returncode == 0:
            print(f"[session] built image {self.image_name}")
            return True
        else:
            print(f"[session] build failed: {result.stderr}")
            return False

    def start_docker_session(self, session_id: str) -> str | None:
        """Start a Docker container for the session. Returns container_id or None."""
        session = self.sessions.get(session_id)
        if not session:
            return None

        workspace = session["workspace"]

        try:
            result = subprocess.run(
                [
                    "docker", "run", "-d",
                    "--name", f"claude-{session_id}",
                    "--memory", "512m",
                    "--cpus", "1.0",
                    "--network", "session-net",
                    "--security-opt", "no-new-privileges=true",
                    "--read-only",
                    "--tmpfs", "/tmp:noexec,nosuid,size=64m",
                    "--tmpfs", "/run:noexec,nosuid,size=8m",
                    "-v", f"{workspace}:/workspace:rw",
                    "-w", "/workspace",
                    self.image_name,
                    "tail", "-f", "/dev/null",
                ],
                capture_output=True, text=True, timeout=30
            )
            if result.returncode == 0:
                container_id = result.stdout.strip()
                with self._lock:
                    session["container_id"] = container_id
                print(f"[session] docker container {container_id} for {session_id}")
                return container_id
            else:
                print(f"[session] docker run failed: {result.stderr}")
                return None
        except Exception as e:
            print(f"[session] docker error: {e}")
            return None

    def exec_in_container(self, session_id: str, command: list[str]) -> str | None:
        """Execute a command inside the session's container."""
        session = self.sessions.get(session_id)
        if not session or not session.get("container_id"):
            return None

        try:
            result = subprocess.run(
                ["docker", "exec", "-i", session["container_id"]] + command,
                capture_output=True, text=True, timeout=30
            )
            return result.stdout
        except Exception:
            return None

    def _cleanup_loop(self):
        """Periodically destroy inactive sessions."""
        while True:
            time.sleep(60)
            now = time.time()
            stale = []
            with self._lock:
                for sid, sess in self.sessions.items():
                    if now - sess["last_active"] > SESSION_TIMEOUT:
                        stale.append(sid)
            for sid in stale:
                print(f"[session] timeout: {sid}")
                self.destroy_session(sid)

    def get_session_info(self, session_id: str) -> dict | None:
        """Get session metadata."""
        session = self.sessions.get(session_id)
        if not session:
            return None
        return {
            "session_id": session_id,
            "workspace": session["workspace"],
            "created_at": session["created_at"],
            "last_active": session["last_active"],
            "container_id": session.get("container_id"),
            "has_pty": session.get("master_fd") is not None,
        }

    def list_sessions(self) -> list[dict]:
        """List all active sessions."""
        return [self.get_session_info(sid) for sid in list(self.sessions.keys())]


# Global instance
_session_manager: SessionManager | None = None


def get_session_manager() -> SessionManager:
    global _session_manager
    if _session_manager is None:
        _session_manager = SessionManager()
    return _session_manager
