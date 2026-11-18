"""PreToolUse hook: block direct Edit/Write of .env files (secrets live there).

Reads the hook JSON from stdin. Exit code 2 blocks the tool call and shows stderr to Claude.
Template files such as .env.example are allowed.
"""
import json
import os
import sys


def is_protected(path: str) -> bool:
    name = os.path.basename(path.replace("\\", "/")).lower()
    if name in (".env.example", ".env.sample", ".env.template"):
        return False
    return name == ".env" or name.startswith(".env.") or name.endswith(".env")


def main() -> int:
    try:
        event = json.load(sys.stdin)
    except Exception:
        return 0
    tool_input = event.get("tool_input") or {}
    path = tool_input.get("file_path") or tool_input.get("path") or ""
    if path and is_protected(str(path)):
        print(
            "Blocked: direct edits to .env are not allowed (it holds secrets). "
            "Ask the user to change it, or use save_ai_config in the app.",
            file=sys.stderr,
        )
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
