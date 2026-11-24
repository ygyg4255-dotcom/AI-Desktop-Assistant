# Altron AI Desktop Assistant

## Project Overview
Altron is an autonomous Windows desktop assistant: a headless Python orchestrator with a
local Flask "radar" HUD in the browser, always-on voice (wake word, VAD, faster-whisper STT,
TTS), desktop/file/app automation tools behind a permission gate, and remote control plus
approvals over Telegram. Chat and tools talk only to the Anthropic Claude API; an optional `GROQ_API_KEY` sends
recorded speech (STT only) to Groq Whisper.

## Architecture (official runtime path)
- Launch: `python main.py`. This is the only supported entry point.
- `main.py`: orchestrator. It calls `setup_logging()`, then starts the HUD server, the voice
  listener and the Telegram listener on **plain `threading.Thread`s** and supervises them.
  It must not import PySide6.
- `ui_server.py`: Flask HUD server on `127.0.0.1:5000` (falls back to 5001), serving `ui/`
  and the JSON API (`/status`, `/config`, `/test_config`, `/command`, `/clear_logs`,
  `/approvals`, `/approve`). Flask is required: there is no `http.server` fallback.
- `src/agent/`: `executor.py` (agent loop), `tools.py` (tool registry and handlers),
  `permissions.py` (tiers), `approvals.py` (out-of-band approvals), `memory.py`
  (conversation memory), `history.py` (reference-only cross-session history),
  `control_intents.py` (strict whole-utterance shutdown / mute / forget commands). Key points:
  `approvals.py` also holds `describe_action()` (plain-language previews used by HUD and Telegram,
  no raw args in Telegram) and the optional PIN flow (`pin_required`, `verify_pin_and_approve`,
  rate limit and lockout). `tools.py` has `file_list` (SAFE), Recycle Bin `file_delete`
  (`SHFileOperationW`), `file_restore` (SAFE), one shared Desktop resolver (OneDrive-aware) for
  all file tools, and `power_control` (shutdown/restart gated via `needs_approval_power_control`).
  `executor.py` runs compound requests to completion (local-intent bypass disabled for them) and
  resumes the remaining steps after an approval (resume-completion callback wired in `main.py`). Root `memory.py` only re-exports `src.agent.memory`.
- `src/services/`: `ai_client.py` (Claude Messages API), `voice_service.py`
  (`SpeechRecognitionListener`, queue-based `TTSManager`), `groq_stt.py` (optional Groq
  Whisper with local faster-whisper fallback), `sentence_chunker.py` (streamed text ->
  sentences for TTS), `audio_input.py` (mic selection that skips loopback devices, post-TTS
  echo gate, energy floor), `telegram_service.py`
  (`TelegramRemoteListener`, `TelegramApprovalBridge`, send helpers; in PIN mode the owner's next
  message is consumed as the PIN reply, never logged or echoed, and deleted from the chat), `hud_status.py`
  (in-memory HUD state, flushed atomically to `status.json`), `signals.py` (Qt-free
  `CallbackSignal`), plus desktop icon and OmniRoute helpers.
- `src/security/`: `pin.py` (PBKDF2 approval PIN hash/verify; `python -m src.security.pin`).
- `src/config/`: `settings.py` (configuration), `paths.py` (runtime dir), `log.py`
  (logging).

### Legacy (deprecated, do not extend)
`src/app.py` and `src/ui/` (PySide6 capsule UI, tray, settings window) are kept only so old
imports keep working. `SpeechRecognitionWorker` and `TelegramRemoteWorker` are thin `QThread`
wrappers over the listeners above, kept for that UI only. Start them with `.start()`; never
call `.run()` on a QThread from a Python thread. Root `test_app.py`, `test_chat_ui.py`,
`test_step3_final.py` and `test_ui_integration.py` are manual GUI scripts, not pytest suites.

## Configuration
- Use `src.config.settings.get_config_value(*keys, default=...)` (or `Settings()`, which
  uses it). Do not read `os.environ` or `.env` directly.
- Precedence, everywhere: explicit argument > `.env` (`resolve_env_path()`) > process
  environment > default.
- Default models live only in `settings.py`: `DEFAULT_MODEL = "claude-sonnet-5-5"` and
  `DEFAULT_FAST_MODEL = "claude-haiku-5-5"`.
- Never read, print, edit or commit `.env` (it is gitignored; only `.env.example` is tracked). A PreToolUse hook blocks edits to it; use `.env.example`.

## Runtime directory
Runtime state lives in `~/.ai-desktop-assistant/` (override with `ALTRON_DATA_DIR`), never in
the OneDrive-synced project folder. It holds `memory.json`, `status.json`,
`telegram_offset.json`, `conversation_history.json` (redacted, capped, reference-only),
`assistant.db` and `altron.log` (rotating: 1 MB x 3). Use
`src.config.paths.runtime_file(name)` for new files.

## Logging
- `from src.config.log import get_logger; logger = get_logger(__name__)`.
- Do not use `print()`, and do not write silent `except Exception: pass` blocks: log at
  least at DEBUG.
- Every handler formats through `RedactingFormatter` (`redact_secrets`), which removes bot
  tokens and `sk-ant-` keys. Still pass known secrets explicitly:
  `redact_secrets(err, token)`.
- `ALTRON_LOG_LEVEL` sets the level (default INFO).

## Security design (audit S1–S8): keep these invariants
- **S1 HUD auth:** a per-launch random token is injected into `index.html` and must be sent
  as `X-HUD-Token` (constant-time compare). The Host header must be
  `127.0.0.1:<port>`/`localhost:<port>`, and any Origin must match. No CORS headers.
- **S2 JSON-only, allow-listed providers:** `AIClient` stays hard-coded to `api.anthropic.com`
  (prompt caching). `ModelRouter` can switch live to openai/gemini/ollama/custom through
  `src/services/openai_compat.py`: fixed endpoints for openai/gemini, Ollama loopback only,
  custom `https://` or loopback `http://`; keys stay per provider (`PROVIDER_ENV`). State-changing endpoints require
  `Content-Type: application/json`. Config values cannot contain line breaks. The only
  other host is `api.groq.com`, hard-coded in `groq_stt.py` and used solely for audio
  transcription (never chat, never configurable).
- **S3 approval cannot be forged:** `Tool.run` strips every `_`-prefixed key from model input.
  Approval arrives only as the executor's `approved=` kwarg.
- **S4 tiers:** SAFE (auto), CONDITIONAL (per-call `needs_approval_*` check, no blanket
  allow), DANGEROUS (always ask). New tools default to DANGEROUS. See the
  `permission-gate-review` skill.
- **S5 approvals (PIN):** with `APPROVAL_PIN_HASH` set, approval needs the PIN (HUD `/config/pin`
  and `/approve`, or Telegram reply). Only the hash is stored; never log or echo the PIN.
- **S5 approvals:** non-blocking `ApprovalManager`, resolved from the HUD (token) or a
  Telegram inline keyboard (owner chat and user only). Bound to an args hash, single use,
  expiring. Voice can never approve.
- **S6 secrets:** never log or return tokens or keys (`redact_secrets`). `.env` values are
  quoted on write. A failed `.env` write raises an error and is never swallowed.
- **S7 command execution:** no `shell=True`. Resolve executables with `shutil.which` and
  pass argument lists.
- **S8 runtime files:** everything mutable goes in `~/.ai-desktop-assistant/` (see above).

## Prompt constraint
`prompts/system_prompt.txt` must stay under **6900 characters** (a test enforces it). Trim wording
before adding rules, and keep the password and compound-request rules intact (tests check them).

## Tests
- Current status: **872 passed** (`tests/` plus `test_agent.py`). `test_pin_settings.py` failed
  once in a full run and passed on rerun, so treat it as possibly order-dependent.
- Run `pytest -p no:cacheprovider` from the project root. `pytest.ini` limits collection to
  `tests/` and `test_agent.py` and disables the cache provider, so no `.pytest_cache/`
  lands in OneDrive.
- Tests must set `ALTRON_DATA_DIR` to a temp dir when they touch runtime files. They must
  never touch the real `.env`, network or data dir. Check log output with `caplog`,
  not `capsys`.

## Understand Anything Knowledge Graph
This project maintains a codebase knowledge graph at `.ua/knowledge-graph.json` generated by Understand Anything.
- For architectural queries, component boundaries, and cross-module interactions, check `.ua/knowledge-graph.json` first before performing broad raw file searches.
- To open the interactive knowledge-graph dashboard: use `/understand-dashboard` or run `npx @understand-anything/viewer .`
