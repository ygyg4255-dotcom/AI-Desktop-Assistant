"""The .claude PreToolUse hook blocks direct .env edits."""
import importlib.util
import json
import subprocess
import sys
from pathlib import Path

HOOK = Path(__file__).resolve().parent.parent / ".claude" / "hooks" / "block_env_edit.py"


def _run(path):
    ev = json.dumps({"tool_input": {"file_path": path}})
    return subprocess.run([sys.executable, str(HOOK)], input=ev, text=True, capture_output=True)


def test_blocks_env_variants():
    for p in (".env", r"C:\proj\.env", "/a/b/.env", ".env.local", "prod.env"):
        r = _run(p)
        assert r.returncode == 2, p
        assert "Blocked" in r.stderr


def test_allows_example_and_normal_files():
    for p in (".env.example", "src/app.py", "README.md", "environment.py"):
        assert _run(p).returncode == 0, p


def test_bad_input_does_not_block():
    r = subprocess.run([sys.executable, str(HOOK)], input="not json", text=True, capture_output=True)
    assert r.returncode == 0
