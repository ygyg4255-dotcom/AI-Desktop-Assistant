# Graph Report - AI Desktop Assistant  (2026-11-16)

## Corpus Check
- 30 files · ~34,980 words
- Verdict: corpus is large enough that graph structure adds value.
- Unclassified: 3 file(s) not represented in the graph (top: (none) 2, .example 1)

## Summary
- 717 nodes · 1504 edges · 46 communities (17 shown, 29 thin omitted)
- Extraction: 89% EXTRACTED · 11% INFERRED · 0% AMBIGUOUS · INFERRED: 164 edges (avg confidence: 0.87)
- Token cost: 4,028 input · 1,927 output

## Graph Freshness
- Built from commit: `4a6912e1`
- Run `git rev-parse HEAD` and compare to check if the graph is stale.
- Run `graphify update .` after code changes (no API cost).

## Community Hubs (Navigation)
- test_ui_integration.py
- Configuration and AI Client
- AgentStep
- Application Lifecycle Management
- Desktop and System Tools
- Media and Messaging Tools
- Code and Window Management
- executor.py
- Settings Configuration Dialog
- Desktop Icon Automation
- Core Agent Loop
- Conversation and Permission Persistence
- Tool Registry System
- chat_window.py
- .process
- Tool Execution Audit
- ChatWindow
- AgentExecutor
- Text To Speech Manager
- Desktop Assistant Application
- Continuous Speech Recognition
- test_agent_executor
- PermissionRequest
- PermissionCard
- Settings
- .__init__
- Conversation Session Management
- send_telegram_message_async
- Desktop Window Control
- send_telegram_message
- build_exe.py
- Windows System Control
- Claude Graphify Guidelines
- psutil
- python-dotenv
- youtube-transcript-api
- yt-dlp
- handle_youtube_play_and_summarize

## God Nodes (most connected - your core abstractions)
1. `ChatWindow` - 71 edges
2. `Database` - 46 edges
3. `create_default_registry()` - 45 edges
4. `AgentExecutor` - 39 edges
5. `ToolResult` - 39 edges
6. `AIClient` - 32 edges
7. `SettingsWindow` - 26 edges
8. `PermissionRequest` - 22 edges
9. `PermissionGate` - 18 edges
10. `test_agent_executor()` - 18 edges

## Surprising Connections (you probably didn't know these)
- `test_agent_executor()` --uses--> `AgentState`  [INFERRED]
  test_agent.py → src/agent/executor.py
- `test_tool_registry()` --uses--> `ToolCategory`  [INFERRED]
  test_agent.py → src/agent/tools.py
- `test_chat_window_and_signals()` --calls--> `AgentStep`  [EXTRACTED]
  test_ui_integration.py → src/agent/executor.py
- `test_chat_window_and_signals()` --calls--> `PermissionRequest`  [EXTRACTED]
  test_ui_integration.py → src/agent/executor.py
- `test_agent_executor()` --calls--> `AgentExecutor`  [EXTRACTED]
  test_agent.py → src/agent/executor.py

## Import Cycles
- None detected.

## Hyperedges (group relationships)
- **Altron Core Capabilities** — readme_capsule_ui, readme_wake_word, readme_tts, readme_desktop_icon_control, readme_telegram_control [EXTRACTED 1.00]

## Communities (46 total, 29 thin omitted)

### Community 0 - "test_ui_integration.py"
Cohesion: 0.13
Nodes (3): send_telegram_photo(), is_windows_startup_enabled(), set_windows_startup()

### Community 1 - "Configuration and AI Client"
Cohesion: 0.07
Nodes (6): AIClient, _worker(), AIResponse, Message, test_chat_ui(), test_omniroute()

### Community 2 - "AgentStep"
Cohesion: 0.21
Nodes (3): AgentStep, MessageBubble, StepBubble

### Community 3 - "Application Lifecycle Management"
Cohesion: 0.07
Nodes (4): Application, main(), OmniRouteManager, test_application()

### Community 4 - "Desktop and System Tools"
Cohesion: 0.09
Nodes (16): handle_app_close(), handle_app_open(), handle_browser_open(), handle_file_copy(), handle_file_delete(), handle_file_move(), handle_file_open(), handle_file_read() (+8 more)

### Community 5 - "Media and Messaging Tools"
Cohesion: 0.12
Nodes (12): _find_contact_element_uia(), _focus_whatsapp_window(), _get_clipboard_unicode(), handle_clipboard_manager(), handle_whatsapp_send(), _is_phone_number(), _mouse_click(), _normalize_moroccan_phone() (+4 more)

### Community 6 - "Code and Window Management"
Cohesion: 0.09
Nodes (11): create_default_registry(), handle_code_edit(), handle_web_fetch(), handle_window_control(), ToolCategory, ToolParameter, main(), test_code_edit_tool() (+3 more)

### Community 8 - "executor.py"
Cohesion: 0.06
Nodes (9): AgentResponse, AgentState, PermissionGate, PermissionLevel, PermissionRule, ConnectionStatus, main(), test_permission_gate() (+1 more)

### Community 10 - "Desktop Icon Automation"
Cohesion: 0.12
Nodes (7): DatabaseService, calculate_screen_coordinates(), get_desktop_icons(), get_desktop_listview_hwnd(), get_screen_dimensions(), LVITEMW, position_desktop_icon()

### Community 11 - "Core Agent Loop"
Cohesion: 0.14
Nodes (4): clean_tool_confirmation(), intercept_raw_code_or_payload(), match_local_intent(), sanitize_final_reply()

### Community 14 - "chat_window.py"
Cohesion: 0.07
Nodes (3): parse_wake_word(), ChatMessage, SystemTrayIcon

### Community 21 - "Desktop Assistant Application"
Cohesion: 0.15
Nodes (13): src.app, Altron AI Desktop Assistant, Floating Capsule UI, Desktop Icon Control (SysListView32), Remote Telegram Control, Conditional Text-to-Speech (TTS), Voice Wake-Word & Continuous Listening, comtypes (+5 more)

### Community 23 - "test_agent_executor"
Cohesion: 0.11
Nodes (4): test_agent_executor(), main(), test_chat_window_and_signals(), test_settings_dialog()

### Community 29 - "send_telegram_message_async"
Cohesion: 0.22
Nodes (4): handle_capture_and_inspect_screen(), handle_whatsapp_read(), send_telegram_message_async(), send_telegram_photo_async()

### Community 32 - "Desktop Window Control"
Cohesion: 0.29
Nodes (3): find_matching_window(), handle_desktop_icon_position(), handle_move_application_window()

### Community 33 - "send_telegram_message"
Cohesion: 0.20
Nodes (5): handle_create_and_send_pdf(), handle_send_to_telegram(), chunk_message(), send_telegram_document(), send_telegram_message()

### Community 43 - "handle_youtube_play_and_summarize"
Cohesion: 0.33
Nodes (3): extract_youtube_video_id(), fetch_youtube_transcript_text(), handle_youtube_play_and_summarize()

## Knowledge Gaps
- **13 isolated node(s):** `src.app`, `comtypes`, `PyAudio`, `PySide6`, `pyttsx3` (+8 more)
  These have ≤1 connection - possible missing edges or undocumented components. (Counts symbols only; 354 node(s) total have ≤1 connection when file, concept and rationale nodes are included.)
- **29 thin communities (<3 nodes) omitted from report** — run `graphify query` to explore isolated nodes.

## Suggested Questions
_Questions this graph is uniquely positioned to answer:_

- **Why does `ChatWindow` connect `ChatWindow` to `test_ui_integration.py`, `Configuration and AI Client`, `AgentStep`, `Application Lifecycle Management`, `Notch Expansion Handler`, `executor.py`, `Conversation and Permission Persistence`, `chat_window.py`, `Notch Window Adaptation`, `AgentExecutor`, `Text To Speech Manager`, `Continuous Speech Recognition`, `test_agent_executor`, `PermissionRequest`, `.__init__`, `UI Collapse Management`, `Command Execution Pipeline`?**
  _High betweenness centrality (0.228) - this node is a cross-community bridge._
- **Are the 10 inferred relationships involving `ChatWindow` (e.g. with `AgentExecutor` and `AgentResponse`) actually correct?**
  _`ChatWindow` has 10 INFERRED edges - model-reasoned connections that need verification._
- **What connects `src.app`, `comtypes`, `PyAudio` to the rest of the system?**
  _13 weakly-connected nodes found - possible documentation gaps or missing edges._
- **Should `test_ui_integration.py` be split into smaller, more focused modules?**
  _Cohesion score 0.12962962962962962 - nodes in this community are weakly interconnected._
- **Why does `Database` connect `Conversation and Permission Persistence` to `test_ui_integration.py`, `Application Lifecycle Management`, `Code and Window Management`, `executor.py`, `Settings Configuration Dialog`, `Desktop Icon Automation`, `Tool Registry System`, `chat_window.py`, `Tool Execution Audit`, `ChatWindow`, `test_agent_executor`, `Settings`, `.__init__`, `Conversation Session Management`?**
  _High betweenness centrality (0.136) - this node is a cross-community bridge._
- **Are the 3 inferred relationships involving `Database` (e.g. with `Application` and `ChatWindow`) actually correct?**
  _`Database` has 3 INFERRED edges - model-reasoned connections that need verification._
- **Should `Configuration and AI Client` be split into smaller, more focused modules?**
  _Cohesion score 0.07087486157253599 - nodes in this community are weakly interconnected._