"""
Agent Engine - 完整实现
支持 React Tool Loop + Tool + MCP + Skill + 3层记忆 + 压缩 + Agent Team
"""

import json
import os
import re
import queue
import threading
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Iterator

from .tools_registry import get_all_tools
from .team import get_team, create_specialized_team, AgentTeam


# ── Events ────────────────────────────────────────────────────────────

class EventType:
    TEXT = "text"
    TOOL_CALL = "tool_call"
    TOOL_RESULT = "tool_result"
    WAITING = "waiting"
    ERROR = "error"
    ITERATION_START = "iteration_start"
    ITERATION_END = "iteration_end"
    PROMPT_ASSEMBLED = "prompt_assembled"
    TEAM_DELEGATE = "team_delegate"
    COMPACTION = "compaction"
    MCP_CALL = "mcp_call"


def get_timestamp() -> str:
    return datetime.now().strftime("%H:%M:%S.%f")[:-3]


@dataclass
class Event:
    type: str
    data: Any = None
    timestamp: str = ""
    details: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "type": self.type,
            "data": self.data,
            "timestamp": self.timestamp,
            "details": self.details,
        }


# ── Memory Compaction (3-layer) ────────────────────────────────────────

class MemoryManager:
    """3层记忆: 短期(最近N条), 中期(压缩摘要), 长期(全局摘要)"""

    def __init__(self, short_window: int = 6):
        self.messages: list[dict] = []
        self.mid_summary: str = ""
        self.long_summary: str = ""
        self.short_window = short_window
        self.compact_count = 0

    def add(self, role: str, content):
        self.messages.append({"role": role, "content": content})

    def should_compact(self, threshold: int = 12) -> bool:
        return len(self.messages) > threshold

    def _summarize_with_llm(self, msgs: list[dict], api_key: str, base_url: str, model: str) -> str:
        import httpx
        text = "\n".join(f'{m["role"]}: {m.get("content", "")[:300]}' for m in msgs)
        prompt = f"Summarize this conversation in 2-3 sentences, keeping only key facts and decisions:\n\n{text}\n\nSummary:"
        try:
            resp = httpx.post(
                f"{base_url}/v1/messages",
                json={"model": model, "max_tokens": 256, "messages": [{"role": "user", "content": prompt}]},
                headers={"Content-Type": "application/json", "x-api-key": api_key, "anthropic-version": "2023-06-01"},
                timeout=30,
            )
            if resp.status_code == 200:
                body = resp.json()
                for block in body.get("content", []):
                    if isinstance(block, dict) and block.get("type") == "text":
                        return block["text"].strip()
        except Exception:
            pass
        return f"Conversation covered {len(msgs)} turns."

    def compact(self, api_key: str, base_url: str, model: str) -> Event:
        recent = self.messages[-self.short_window:]
        middle = self.messages[:-self.short_window]

        mid = self._summarize_with_llm(middle, api_key, base_url, model) if middle else ""
        self.mid_summary = mid
        self.long_summary += (("\n" if self.long_summary else "") + f"[Compact #{self.compact_count + 1}] {mid}")
        self.messages = [
            {"role": "system", "content": f"[Session Summary]\n{self.long_summary}"},
            {"role": "system", "content": f"[Recent Summary]\n{mid}"},
        ] + recent
        self.compact_count += 1

        return Event(
            type=EventType.COMPACTION,
            data=f"Session compacted (kept {self.short_window} recent, summarized {len(middle)} older)",
            timestamp=get_timestamp(),
            details={"total_compactions": self.compact_count, "long_summary_len": len(self.long_summary)},
        )

    def get_messages(self) -> list[dict]:
        return list(self.messages)


# ── MCP Client (stub, registers as tools) ─────────────────────────────

_mcp_servers: dict[str, dict] = {}


def register_mcp_server(name: str, command: list[str]):
    _mcp_servers[name] = {"command": command}


class MCPTool:
    """Wrapper that calls an MCP server via stdio"""

    def __init__(self, name: str, mcp_name: str, description: str, input_schema: dict):
        self.name = f"mcp_{mcp_name}_{name}"
        self._mcp_name = mcp_name
        self._tool_name = name
        self.description = f"[MCP:{mcp_name}] {description}"
        self.input_schema = input_schema

    def execute(self, **kwargs) -> "ToolResult":
        import subprocess, json
        server = _mcp_servers.get(self._mcp_name)
        if not server:
            return ToolResult(content=f"MCP server '{self._mcp_name}' not registered", is_error=True)
        request = {
            "jsonrpc": "2.0", "id": 1,
            "method": "tools/call",
            "params": {"name": self._tool_name, "arguments": kwargs},
        }
        try:
            proc = subprocess.Popen(
                server["command"], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                text=True, timeout=30,
            )
            stdout, _ = proc.communicate(json.dumps(request) + "\n")
            result = json.loads(stdout.split("\n")[0])
            content = result.get("result", {}).get("content", [{}])
            texts = [c.get("text", str(c)) for c in content if isinstance(c, dict)]
            return ToolResult(content="\n".join(texts) if texts else str(result))
        except Exception as e:
            return ToolResult(content=f"MCP error: {e}", is_error=True)


# ── Tools ──────────────────────────────────────────────────────────────

@dataclass
class ToolResult:
    content: str
    is_error: bool = False


# ── Tool Classes ───────────────────────────────────────────────────────

class Tool:
    @property
    def name(self) -> str:
        raise NotImplementedError
    @property
    def description(self) -> str:
        raise NotImplementedError
    @property
    def input_schema(self) -> dict:
        raise NotImplementedError
    def execute(self, **kwargs) -> ToolResult:
        raise NotImplementedError
    def is_read_only(self) -> bool:
        return True
    def to_api_schema(self) -> dict:
        return {"name": self.name, "description": self.description, "input_schema": self.input_schema}


class ReadTool(Tool):
    name = "Read"
    description = "Reads a file from the local filesystem."
    input_schema = {"type": "object", "properties": {"file_path": {"type": "string", "description": "Path to file to read"}}, "required": ["file_path"]}
    def execute(self, file_path: str) -> ToolResult:
        try:
            from pathlib import Path
            content = Path(file_path).read_text()
            if len(content) > 5000: content = content[:5000] + f"\n... (truncated, total {len(content)} chars)"
            return ToolResult(content=content)
        except FileNotFoundError:
            return ToolResult(content=f"File not found: {file_path}", is_error=True)
        except Exception as e:
            return ToolResult(content=str(e), is_error=True)


class WriteTool(Tool):
    name = "Write"
    description = "Creates or overwrites a file with content."
    input_schema = {"type": "object", "properties": {"file_path": {"type": "string"}, "content": {"type": "string"}}, "required": ["file_path", "content"]}
    def is_read_only(self) -> bool: return False
    def execute(self, file_path: str, content: str) -> ToolResult:
        try:
            from pathlib import Path
            Path(file_path).parent.mkdir(parents=True, exist_ok=True)
            Path(file_path).write_text(content)
            return ToolResult(content=f"Written to {file_path}")
        except Exception as e:
            return ToolResult(content=str(e), is_error=True)


class EditTool(Tool):
    name = "Edit"
    description = "Edits specific content in a file."
    input_schema = {"type": "object", "properties": {"file_path": {"type": "string"}, "old_string": {"type": "string"}, "new_string": {"type": "string"}}, "required": ["file_path", "old_string", "new_string"]}
    def is_read_only(self) -> bool: return False
    def execute(self, file_path: str, old_string: str, new_string: str) -> ToolResult:
        try:
            from pathlib import Path
            content = Path(file_path).read_text()
            if old_string not in content:
                return ToolResult(content=f"Could not find old_string in file: {file_path}", is_error=True)
            Path(file_path).write_text(content.replace(old_string, new_string, 1))
            return ToolResult(content=f"Edited {file_path}")
        except FileNotFoundError:
            return ToolResult(content=f"File not found: {file_path}", is_error=True)
        except Exception as e:
            return ToolResult(content=str(e), is_error=True)


class BashTool(Tool):
    name = "Bash"
    description = "Executes a shell command."
    input_schema = {"type": "object", "properties": {"command": {"type": "string"}}, "required": ["command"]}
    def is_read_only(self) -> bool: return False
    def execute(self, command: str) -> ToolResult:
        import subprocess
        try:
            result = subprocess.run(command, shell=True, capture_output=True, text=True, timeout=60)
            output = result.stdout if result.stdout else result.stderr
            if len(output) > 3000: output = output[:3000] + f"\n... (truncated)"
            return ToolResult(content=output or "(no output)")
        except subprocess.TimeoutExpired:
            return ToolResult(content="Command timed out after 60s", is_error=True)
        except Exception as e:
            return ToolResult(content=str(e), is_error=True)


class GlobTool(Tool):
    name = "Glob"
    description = "Finds files matching a glob pattern."
    input_schema = {"type": "object", "properties": {"pattern": {"type": "string"}}, "required": ["pattern"]}
    def execute(self, pattern: str) -> ToolResult:
        from pathlib import Path
        try:
            matches = list(Path(".").glob(pattern))
            if not matches: return ToolResult(content="No matches found")
            return ToolResult(content="\n".join(str(m) for m in matches[:50]))
        except Exception as e:
            return ToolResult(content=str(e), is_error=True)


class GrepTool(Tool):
    name = "Grep"
    description = "Searches for text in files."
    input_schema = {"type": "object", "properties": {"pattern": {"type": "string"}, "path": {"type": "string", "default": "."}}, "required": ["pattern"]}
    def execute(self, pattern: str, path: str = ".") -> ToolResult:
        import re
        from pathlib import Path
        try:
            matches = []
            for p in Path(path).rglob("*"):
                if not p.is_file(): continue
                try:
                    for i, line in enumerate(p.read_text().splitlines(), 1):
                        if re.search(pattern, line):
                            matches.append(f"{p}:{i}: {line[:100]}")
                except Exception: pass
            if not matches: return ToolResult(content="No matches found")
            return ToolResult(content="\n".join(matches[:50]))
        except Exception as e:
            return ToolResult(content=str(e), is_error=True)


class WebSearchTool(Tool):
    name = "WebSearch"
    description = "Searches the web for information."
    input_schema = {"type": "object", "properties": {"query": {"type": "string"}}, "required": ["query"]}
    def execute(self, query: str) -> ToolResult:
        try:
            import urllib.request, urllib.parse, re
            url = f"https://html.duckduckgo.com/html/?q={urllib.parse.quote(query)}"
            req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0'})
            with urllib.request.urlopen(req, timeout=10) as resp:
                html = resp.read().decode('utf-8', errors='ignore')
            results = re.findall(r'<a class="result__a" href="([^"]+)">([^<]+)</a>', html)
            if not results: return ToolResult(content=f"No search results for: {query}")
            return ToolResult(content="\n\n".join(f"{i+1}. {t}\n   URL: {l}" for i, (l, t) in enumerate(results[:5])))
        except Exception as e:
            return ToolResult(content=f"Search failed: {e}", is_error=True)


class WebFetchTool(Tool):
    name = "WebFetch"
    description = "Fetches the content of a webpage."
    input_schema = {"type": "object", "properties": {"url": {"type": "string"}}, "required": ["url"]}
    def execute(self, url: str) -> ToolResult:
        try:
            import urllib.request, re
            req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0'})
            with urllib.request.urlopen(req, timeout=15) as resp:
                content = resp.read().decode('utf-8', errors='ignore')
            content = re.sub(r'<script[^>]*>.*?</script>', '', content, flags=re.DOTALL)
            content = re.sub(r'<style[^>]*>.*?</style>', '', content, flags=re.DOTALL)
            content = re.sub(r'<[^>]+>', '', content)
            content = re.sub(r'\n\s*\n', '\n\n', content).strip()
            if len(content) > 5000: content = content[:5000] + "\n... (truncated)"
            return ToolResult(content=content)
        except Exception as e:
            return ToolResult(content=f"Failed to fetch {url}: {e}", is_error=True)


class TaskCreateTool(Tool):
    name = "TaskCreate"
    description = "Creates a task to track progress."
    input_schema = {"type": "object", "properties": {"subject": {"type": "string"}, "description": {"type": "string"}}, "required": ["subject", "description"]}
    def execute(self, subject: str, description: str) -> ToolResult:
        try:
            from .task_manager import TaskManager
            task_id = TaskManager().create_task(subject, description)
            return ToolResult(content=f"Task created: #{task_id} - {subject}")
        except Exception as e:
            return ToolResult(content=str(e), is_error=True)


class TaskUpdateTool(Tool):
    name = "TaskUpdate"
    description = "Updates a task status."
    input_schema = {"type": "object", "properties": {"task_id": {"type": "string"}, "status": {"type": "string"}}, "required": ["task_id", "status"]}
    def execute(self, task_id: str, status: str) -> ToolResult:
        try:
            from .task_manager import TaskManager
            TaskManager().update_task(task_id, status)
            return ToolResult(content=f"Task #{task_id} updated to {status}")
        except Exception as e:
            return ToolResult(content=str(e), is_error=True)


class TaskListTool(Tool):
    name = "TaskList"
    description = "Lists all tasks."
    input_schema = {"type": "object", "properties": {}}
    def execute(self) -> ToolResult:
        try:
            from .task_manager import TaskManager
            tasks = TaskManager().list_tasks()
            return ToolResult(content="Tasks:\n" + "\n".join(tasks) if tasks else "No tasks")
        except Exception as e:
            return ToolResult(content=str(e), is_error=True)


class SkillTool(Tool):
    name = "Skill"
    description = "Load a local skill definition."
    input_schema = {"type": "object", "properties": {"skill": {"type": "string"}}, "required": ["skill"]}
    def _discover_skill_roots(self, cwd: str = ".") -> list[tuple[str, str]]:
        import os
        roots = []
        for ancestor in self._get_ancestors(cwd):
            for subdir in [".claw/skills", ".claw/commands", ".codex/skills", ".codex/commands"]:
                path = os.path.join(ancestor, subdir)
                if os.path.isdir(path): roots.append(("Project", path))
        home = os.path.expanduser("~")
        for subdir in [".claw/skills", ".codex/skills"]:
            path = os.path.join(home, subdir)
            if os.path.isdir(path): roots.append(("User", path))
        return roots
    def _get_ancestors(self, path: str) -> list[str]:
        import os
        ancestors, current = [], os.path.abspath(path)
        while True:
            parent = os.path.dirname(current)
            if parent == current: break
            ancestors.append(current)
            current = parent
        return ancestors
    def _discover_skills(self) -> list[dict]:
        import os, re
        skills = []
        for source, root in self._discover_skill_roots():
            try:
                for entry in os.scandir(root):
                    if not entry.is_dir(): continue
                    skill_md = os.path.join(entry.path, "SKILL.md")
                    if not os.path.isfile(skill_md): continue
                    try:
                        with open(skill_md) as f: content = f.read(200)
                        match = re.search(r'description:\s*"?([^"\n]+)"?', content, re.IGNORECASE)
                        description = match.group(1).strip() if match else "No description"
                    except: description = "No description"
                    skills.append({"name": entry.name, "path": skill_md, "source": source, "description": description})
            except PermissionError: continue
        return skills
    def execute(self, skill: str) -> ToolResult:
        import os
        try:
            skill_name = skill.strip().lstrip('/').lstrip('$')
            if not skill_name: return ToolResult(content="Skill name cannot be empty", is_error=True)
            roots = self._discover_skill_roots()
            for source, root in roots:
                skill_path = os.path.join(root, skill_name, "SKILL.md")
                if os.path.isfile(skill_path):
                    return ToolResult(content=open(skill_path).read())
            available = self._discover_skills()
            available_list = "\n".join(f"  - {s['name']} ({s['source']}): {s['description']}" for s in available)
            return ToolResult(content=f"Skill '{skill_name}' not found.\n\nAvailable skills:\n{available_list or '  (none found)'}", is_error=True)
        except Exception as e:
            return ToolResult(content=str(e), is_error=True)


class ToolSearchTool(Tool):
    name = "ToolSearch"
    description = "Search for available tools by keyword."
    input_schema = {"type": "object", "properties": {"query": {"type": "string"}}, "required": ["query"]}
    def execute(self, query: str) -> ToolResult:
        from .tools_registry import get_all_tools
        all_tools = get_all_tools()
        query_lower = query.lower()
        matches = [{"name": t.name, "description": t.description, "read_only": t.is_read_only()} for t in all_tools.values() if query_lower in t.name.lower() or query_lower in t.description.lower()]
        if not matches: return ToolResult(content=f"No tools found matching '{query}'")
        return ToolResult(content="\n".join(f"- {m['name']} {'(read-only)' if m['read_only'] else ''}: {m['description']}" for m in matches))


DEFAULT_TOOLS: dict[str, Tool] = {
    "Read": ReadTool(),
    "Write": WriteTool(),
    "Edit": EditTool(),
    "Bash": BashTool(),
    "Glob": GlobTool(),
    "Grep": GrepTool(),
    "WebSearch": WebSearchTool(),
    "WebFetch": WebFetchTool(),
    "TaskCreate": TaskCreateTool(),
    "TaskUpdate": TaskUpdateTool(),
    "TaskList": TaskListTool(),
    "Skill": SkillTool(),
    "ToolSearch": ToolSearchTool(),
}


# ── Main Engine ────────────────────────────────────────────────────────

class AgentEngine:
    def __init__(
        self,
        system_prompt: str = "You are a helpful coding assistant.",
        provider: str = "anthropic",
        model: str = "claude-opus-4-6",
        api_key: str | None = None,
        base_url: str | None = None,
        max_tokens_before_compact: int = 6000,
    ):
        self._system_prompt = system_prompt
        self._model = model
        self._api_key = api_key or os.environ.get("ANTHROPIC_AUTH_TOKEN", "")
        self._base_url = base_url or os.environ.get("ANTHROPIC_BASE_URL", "https://api.anthropic.com")
        self._max_tokens = max_tokens_before_compact
        self._memory = MemoryManager(short_window=6)
        self._tools = dict(DEFAULT_TOOLS)
        self._team: AgentTeam | None = None

    # ── helpers ──

    def _call_llm_stream(self, event_queue: queue.Queue) -> dict:
        import httpx

        target_url = f"{self._base_url}/v1/messages"
        headers = {
            "Content-Type": "application/json",
            "x-api-key": self._api_key,
            "anthropic-version": "2023-06-01",
        }
        system = self._system_prompt

        # Inject available MCP tools into system prompt
        mcp_names = []
        for t in self._tools.values():
            name = getattr(t, "name", "?")
            if name.startswith("mcp_"):
                mcp_names.append(name)
        if mcp_names:
            system += f"\n\n## MCP Tools\nYou also have these MCP tools: {', '.join(mcp_names)}"

        req_body = {
            "model": self._model,
            "max_tokens": 8192,
            "system": system,
            "messages": self._memory.get_messages(),
            "tools": [t.to_api_schema() for t in self._tools.values() if not getattr(t, "name", "").startswith("mcp_")],
            "stream": True,
        }

        tool_calls: list[dict] = []
        text_content = ""
        thinking_content = ""
        tool_blocks: dict[int, dict] = {}  # index -> {name, input_text}

        with httpx.stream("POST", target_url, json=req_body, headers=headers, timeout=180) as resp:
            if resp.status_code != 200:
                error_body = resp.text
                return {"content": f"API error {resp.status_code}: {error_body}", "tool_calls": [], "text_content": ""}

            for line in resp.iter_lines():
                line = line.strip()
                if not line or not line.startswith("data:"):
                    continue
                data_str = line[5:].strip()
                if not data_str:
                    continue
                try:
                    chunk = json.loads(data_str)
                except json.JSONDecodeError:
                    continue

                chunk_type = chunk.get("type", "")

                if chunk_type == "content_block_start":
                    block = chunk.get("content_block", {})
                    if block.get("type") == "tool_use":
                        idx = chunk.get("index", 0)
                        tool_blocks[idx] = {"name": block.get("name", ""), "input_text": "", "id": block.get("id", "")}
                    elif block.get("type") == "thinking":
                        idx = chunk.get("index", 0)
                        # Thinking blocks will be handled via thinking_delta

                elif chunk_type == "content_block_delta":
                    idx = chunk.get("index", 0)
                    delta = chunk.get("delta", {})
                    if not isinstance(delta, dict):
                        continue
                    delta_type = delta.get("type", "")

                    if delta_type == "text_delta":
                        txt = delta.get("text", "")
                        text_content += txt
                        event_queue.put(("text", txt))

                    elif delta_type == "thinking_delta":
                        txt = delta.get("thinking", "")
                        thinking_content += txt
                        text_content += f"\n<thinking>\n{txt}\n</thinking>\n"
                        event_queue.put(("text", f"[thinking] {txt} [/thinking]"))

                    elif delta_type == "tool_use_delta":
                        tb = tool_blocks.get(idx)
                        if tb is None:
                            tool_blocks[idx] = {"name": "", "input_text": "", "id": ""}
                            tb = tool_blocks[idx]
                        td = delta.get("tool_use", {})
                        if not isinstance(td, dict):
                            td = {}
                        if td.get("name"):
                            tb["name"] = td["name"]
                        if td.get("input"):
                            tb["input_text"] += td["input"]

                elif chunk_type == "message_delta":
                    pass

        # Finalize tool calls
        for idx, tb in sorted(tool_blocks.items()):
            if tb["name"]:
                try:
                    inp = json.loads(tb["input_text"]) if tb["input_text"] else {}
                except json.JSONDecodeError:
                    inp = {}
                tool_calls.append({"name": tb["name"], "input": inp, "id": tb["id"]})

        return {"content": text_content, "tool_calls": tool_calls, "thinking": thinking_content}

    def _build_prompt_details(self) -> dict:
        system_reminders = []
        parsed = []
        for msg in self._memory.get_messages():
            role = msg.get("role", "unknown")
            content = msg.get("content", "")
            content_array = []

            if isinstance(content, str):
                if "<system-reminder>" in content:
                    matches = re.findall(r"<system-reminder>(.*?)</system-reminder>", content, re.DOTALL)
                    system_reminders.extend(m.strip() for m in matches)
                    content_array.append({"type": "system-reminder", "text": content})
                else:
                    content_array.append({"type": "text", "text": content})
            elif isinstance(content, list):
                for item in content:
                    if isinstance(item, dict):
                        t = item.get("type", "")
                        if t == "text":
                            txt = item.get("text", "")
                            if "<system-reminder>" in txt:
                                matches = re.findall(r"<system-reminder>(.*?)</system-reminder>", txt, re.DOTALL)
                                system_reminders.extend(m.strip() for m in matches)
                                content_array.append({"type": "system-reminder", "text": txt})
                            else:
                                content_array.append({"type": "text", "text": txt})
                        elif t == "tool_use":
                            content_array.append({"type": "tool_use", "id": item.get("id", ""), "name": item.get("name", ""), "input": item.get("input", {})})
                        elif t == "tool_result":
                            content_array.append({"type": "tool_result", "tool_use_id": item.get("tool_use_id", ""), "content": item.get("content", ""), "is_error": item.get("is_error", False)})
                        else:
                            content_array.append(item)

            parsed.append({
                "role": role,
                "content": str(content)[:500] if isinstance(content, str) else str(content)[:500],
                "content_preview": str(content)[:500],
                "content_array": content_array,
                "tokens": len(str(content)) // 4,
            })

        return {
            "system_prompt": self._system_prompt,
            "messages_count": len(parsed),
            "messages": parsed[-3:] if len(parsed) > 3 else parsed,
            "tools": [t.to_api_schema() for t in self._tools.values()],
            "system_reminders": system_reminders,
            "memory_compactions": self._memory.compact_count,
            "long_summary": self._memory.long_summary,
        }

    # ── main loop ──

    def run(self, user_input: str, max_iterations: int = 20) -> Iterator[Event]:
        self._memory.add("user", user_input)

        if self._memory.should_compact(threshold=12):
            yield self._memory.compact(self._api_key, self._base_url, self._model)

        iteration = 0
        while iteration < max_iterations:
            iteration += 1
            ts = get_timestamp()

            yield Event(type=EventType.ITERATION_START, data=f"Iteration {iteration}", timestamp=ts, details={"iteration": iteration})

            prompt_details = self._build_prompt_details()
            yield Event(type=EventType.PROMPT_ASSEMBLED, data="Prompt assembled", timestamp=get_timestamp(), details=prompt_details)

            event_queue: queue.Queue = queue.Queue()
            result_thread = threading.Thread(target=self._llm_wrapper, args=(event_queue,), daemon=True)
            result_thread.start()

            tool_calls = []
            text_content = ""
            thinking_content = ""

            while result_thread.is_alive():
                try:
                    ev_type, ev_data = event_queue.get(timeout=0.05)
                    if ev_type == "text":
                        yield Event(type=EventType.TEXT, data=ev_data, timestamp=get_timestamp())
                    elif ev_type == "tool_calls":
                        tool_calls = ev_data
                    elif ev_type == "text_content":
                        text_content = ev_data
                    elif ev_type == "thinking_content":
                        thinking_content = ev_data
                except queue.Empty:
                    pass

            while not event_queue.empty():
                try:
                    ev_type, ev_data = event_queue.get_nowait()
                    if ev_type == "text":
                        yield Event(type=EventType.TEXT, data=ev_data, timestamp=get_timestamp())
                    elif ev_type == "tool_calls":
                        tool_calls = ev_data
                    elif ev_type == "text_content":
                        text_content = ev_data
                    elif ev_type == "thinking_content":
                        thinking_content = ev_data
                except queue.Empty:
                    break

            result_thread.join()

            if not tool_calls:
                if text_content and text_content.startswith("API error"):
                    yield Event(type=EventType.ERROR, data=text_content, timestamp=get_timestamp(), details={"message": text_content})
                    yield Event(type=EventType.ITERATION_END, data="Failed", timestamp=get_timestamp(), details={"iteration": iteration, "reason": "api_error"})
                    break
                if text_content:
                    self._memory.add("assistant", [{"type": "text", "text": text_content}])
                yield Event(type=EventType.ITERATION_END, data="Completed", timestamp=get_timestamp(), details={"iteration": iteration, "reason": "no_tool_calls"})
                break

            tool_results = []
            for tc in tool_calls:
                tool_name = tc.get("name", "")
                tool_input = tc.get("input", {})
                tool_id = tc.get("id", "")

                yield Event(type=EventType.TOOL_CALL, data=f"{tool_name}({json.dumps(tool_input, ensure_ascii=False)})", timestamp=get_timestamp(), details={"name": tool_name, "input": tool_input, "id": tool_id})

                # Check if it's a team delegation
                if tool_name == "Skill" and isinstance(tool_input, dict):
                    skill_name = tool_input.get("skill", "")
                    if skill_name.startswith("$"):
                        yield Event(type=EventType.TEAM_DELEGATE, data=f"Delegating to team: {skill_name}", timestamp=get_timestamp(), details={"agent": skill_name})
                        if self._team is None:
                            self._team = create_specialized_team()

                result = self._execute_tool(tool_name, tool_input)

                yield Event(
                    type=EventType.TOOL_RESULT,
                    data=result.content,
                    timestamp=get_timestamp(),
                    details={"name": tool_name, "input": tool_input, "is_error": result.is_error, "content_preview": result.content[:1000]},
                )

                tool_results.append({"type": "tool_result", "tool_use_id": tool_id, "content": result.content, "is_error": result.is_error})

            # Save assistant message + tool results
            assistant_content = []
            if text_content:
                assistant_content.append({"type": "text", "text": text_content})
            for tc in tool_calls:
                assistant_content.append({"type": "tool_use", "id": tc.get("id", ""), "name": tc.get("name", ""), "input": tc.get("input", {})})

            self._memory.add("assistant", assistant_content)
            self._memory.add("user", tool_results)

            yield Event(type=EventType.ITERATION_END, data=f"Iteration {iteration} completed", timestamp=get_timestamp(), details={"iteration": iteration, "reason": "tool_executed", "tool_count": len(tool_calls)})

            # Check compact
            if self._memory.should_compact(threshold=12):
                yield self._memory.compact(self._api_key, self._base_url, self._model)

        if iteration >= max_iterations:
            yield Event(type=EventType.ERROR, data="Max iterations reached", timestamp=get_timestamp())

    def _llm_wrapper(self, event_queue: queue.Queue):
        """Runs in thread, pushes results to queue"""
        result = self._call_llm_stream(event_queue)
        event_queue.put(("tool_calls", result.get("tool_calls", [])))
        event_queue.put(("text_content", result.get("content", "")))
        event_queue.put(("thinking_content", result.get("thinking", "")))

    def _execute_tool(self, name: str, input_data: dict) -> ToolResult:
        tool = self._tools.get(name)
        if not tool:
            # Try MCP tools
            if name.startswith("mcp_"):
                parts = name.split("_", 2)
                if len(parts) == 3:
                    _, mcp_name, tool_name = parts
                    return MCPTool(tool_name, mcp_name, "", {}).execute(**input_data)
            return ToolResult(content=f"Unknown tool: {name}", is_error=True)
        return tool.execute(**input_data)
