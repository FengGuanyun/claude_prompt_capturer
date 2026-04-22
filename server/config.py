"""Server config — loads API key, base URL, model from ~/.claude/settings.json."""

import json
from pathlib import Path


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
