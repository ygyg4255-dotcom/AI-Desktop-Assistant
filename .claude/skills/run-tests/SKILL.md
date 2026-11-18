---
name: run-tests
description: Run the Altron test suite safely (pytest without cache files). User-invoked only.
disable-model-invocation: true
---

# Run tests

Run both suites from the project root, in this order. Always pass `-p no:cacheprovider`
so no `.pytest_cache/` is written into the OneDrive-synced project folder.

```bash
python -m pytest tests test_agent.py -q -p no:cacheprovider
```

- `tests/` holds the security and behaviour suites (permission bypass, safe tools, HUD
  security, approvals, command execution, secrets, runtime paths, HUD status, Telegram).
- `test_agent.py` is a legacy root-level suite; keep it green too.
- Tests must not touch the real `~/.ai-desktop-assistant/` data: set `ALTRON_DATA_DIR`
  to a temp dir (use pytest's `tmp_path` + `monkeypatch.setenv`) when a test reads or writes
  runtime files such as `memory.json`, `status.json`, or `telegram_offset.json`.
- Never read or print `.env` while debugging a failure.

Report the final pass/fail counts and the full output of any failing test.
