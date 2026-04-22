"""Proxy routes — forward requests to upstream API with capture."""

import json
import re
from datetime import datetime

import httpx
from flask import request, Response

from .config import load_claude_config
from .capture import captured_requests, write_debug_log


def _filter_haiku_lines(text: str) -> str:
    lines = text.split('\n')
    return '\n'.join(line for line in lines if 'claude-haiku-4-5-20251001' not in line)


def _estimate_tokens(text: str) -> int:
    if not text:
        return 0
    chinese = sum(1 for c in text if '一' <= c <= '鿿')
    other = len(text) - chinese
    return chinese // 2 + other // 4


def proxy_to_api(base_url_path: str | None = None):
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
                                    reminder_content = _filter_haiku_lines(m.group(1).strip())
                                    system_reminders.append(reminder_content)
                                    content_array.append({"type": "system-reminder", "text": reminder_content})
                                    text_parts.append(f"[SYSTEM REMINDER] {reminder_content}")
                                outer_text = _filter_haiku_lines(re.sub(r'<system-reminder>.*?</system-reminder>', '', text, flags=re.DOTALL).strip())
                                if outer_text:
                                    text_parts.append(outer_text)
                                continue
                            filtered_text = _filter_haiku_lines(text)
                            text_parts.append(filtered_text)
                            content_array.append({"type": "text", "text": filtered_text})
                        elif b.get("type") == "thinking":
                            thinking_text = _filter_haiku_lines(b.get("text", ""))
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
                        filtered = _filter_haiku_lines(str(b))
                        text_parts.append(filtered)
                        content_array.append({"type": "text", "text": filtered})
                content = "\n".join(text_parts)
            elif isinstance(content, dict):
                filtered = _filter_haiku_lines(content.get("text", json.dumps(content, ensure_ascii=False)))
                content_array.append({"type": "text", "text": filtered})
                content = filtered
            else:
                filtered = _filter_haiku_lines(str(content))
                content_array.append({"type": "text", "text": filtered})
                content = filtered
            parsed_messages.append({
                "role": role,
                "content": str(content),
                "content_preview": str(content)[:500] if content else '',
                "content_array": content_array,
                "tokens": _estimate_tokens(str(content))
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

        system_text = _filter_haiku_lines(system_text)

        entry = {
            "timestamp": datetime.now().strftime("%H:%M:%S.%f")[:-3],
            "model": req_data.get("model", "unknown"),
            "system_prompt": system_text,
            "system_tokens": _estimate_tokens(system_text),
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


def chat_completions():
    """Accept OpenAI-format requests, convert to Anthropic, and stream back."""
    try:
        req_data = request.get_json()
        config = load_claude_config()

        # ── Capture prompt for display in capture UI ──
        raw_messages = req_data.get("messages", [])
        raw_tools = req_data.get("tools", [])
        raw_system = [m.get("content", "") for m in raw_messages if m.get("role") == "system"]
        parsed_msgs = []
        for msg in raw_messages:
            content = msg.get("content", "")
            parsed_msgs.append({
                "role": msg["role"],
                "content": str(content),
                "content_preview": str(content)[:500] if content else "",
                "tokens": 0,
            })
        capture_entry = {
            "timestamp": datetime.now().strftime("%H:%M:%S.%f")[:-3],
            "model": config.get("model", "unknown"),
            "system_prompt": "\n".join(raw_system) if raw_system else "",
            "system_tokens": 0,
            "system_reminders": [],
            "messages": parsed_msgs,
            "tools_count": len(raw_tools),
            "tools": raw_tools[:20],
        }
        captured_requests.append(capture_entry)
        if len(captured_requests) > 500:
            captured_requests.pop(0)

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

        # DashScope Anthropic endpoint doesn't support real streaming.
        # Always use non-streaming, then convert to requested format.
        anthropic_req["stream"] = False
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

        openai_resp = {
            "id": result.get("id", "msg_0"),
            "object": "chat.completion",
            "created": int(datetime.now().timestamp()),
            "model": config.get("model", "unknown"),
            "choices": [{"message": message, "finish_reason": result.get("stop_reason", "stop"), "index": 0}],
        }
        if "usage" in result:
            openai_resp["usage"] = result["usage"]

        # If client requested streaming, return as SSE
        if req_data.get("stream", False):
            def convert_to_sse():
                # Start: assistant role
                yield f"data: {json.dumps({'id': openai_resp['id'], 'object': 'chat.completion.chunk', 'model': openai_resp['model'], 'choices': [{'delta': {'role': 'assistant'}, 'index': 0, 'finish_reason': None}]})}\n\n"
                # Content
                if content:
                    yield f"data: {json.dumps({'id': openai_resp['id'], 'object': 'chat.completion.chunk', 'model': openai_resp['model'], 'choices': [{'delta': {'content': content}, 'index': 0, 'finish_reason': None}]})}\n\n"
                # Tool calls
                if tool_calls:
                    for i, tc in enumerate(tool_calls):
                        yield f"data: {json.dumps({'id': openai_resp['id'], 'object': 'chat.completion.chunk', 'model': openai_resp['model'], 'choices': [{'delta': {'tool_calls': [{'index': i, 'id': tc['id'], 'type': 'function', 'function': {'name': tc['function']['name'], 'arguments': tc['function']['arguments']}}]}, 'index': 0, 'finish_reason': None}]})}\n\n"
                # Done
                finish = openai_resp['choices'][0]['finish_reason']
                yield f"data: {json.dumps({'id': openai_resp['id'], 'object': 'chat.completion.chunk', 'model': openai_resp['model'], 'choices': [{'delta': {}, 'index': 0, 'finish_reason': finish}]})}\n\n"
                yield "data: [DONE]\n\n"

            return Response(convert_to_sse(), mimetype="text/event-stream")
        else:
            return openai_resp
    except Exception as e:
        return {"error": str(e)}, 500
