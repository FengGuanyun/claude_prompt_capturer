"""Translate endpoint — uses upstream API for EN→ZH translation."""

import httpx
from flask import request

from .config import load_claude_config


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
