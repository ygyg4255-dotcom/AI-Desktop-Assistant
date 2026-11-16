# Graph Report - AI Desktop Assistant  (2026-11-16)

## Corpus Check
- cluster-only mode — file stats not available

## Summary
- 707 nodes · 1494 edges · 43 communities (15 shown, 28 thin omitted)
- Extraction: 89% EXTRACTED · 11% INFERRED · 0% AMBIGUOUS · INFERRED: 169 edges (avg confidence: 0.87)
- Token cost: 5,320 input · 2,686 output

## Graph Freshness
- Built from commit: `4a6912e1`
- Run `git rev-parse HEAD` and compare to check if the graph is stale.
- Run `graphify update .` after code changes (no API cost).

## Community Hubs (Navigation)
- Packaging and Build Scripts
- Configuration and AI Client
- Permission Request Card UI
- Application Lifecycle Management
- Desktop and System Tools
- Media and Messaging Tools
- Code and Window Management
- Agent Execution Flow
- Tool Permission Management
- Settings Configuration Dialog
- Desktop Icon Automation
- Core Agent Loop
- Conversation and Permission Persistence
- Tool Registry System
- SQLite Database Storage
- Agent Lifecycle Testing
- Tool Execution Audit
- Chat Interface View
- Text To Speech Manager
- Desktop Assistant Application
- Continuous Speech Recognition
- Database Schema Setup
- Permission Data Models
- OmniRoute Server Management
- File Management Tools
- Telegram Bot Worker
- Conversation Session Management
- Dynamic Island UI Bridge
- Desktop Window Control
- Chat Message Storage
- Windows System Control
- Claude Graphify Guidelines
- psutil
- python-dotenv
- youtube-transcript-api
- yt-dlp

## God Nodes (most connected - your core abstractions)
1. `ChatWindow` - 71 edges
2. `Database` - 47 edges
3. `create_default_registry()` - 45 edges
4. `AgentExecutor` - 44 edges
5. `ToolResult` - 39 edges
6. `AIClient` - 33 edges
7. `SettingsWindow` - 26 edges
8. `PermissionRequest` - 22 edges
9. `PermissionGate` - 19 edges
10. `test_agent_executor()` - 18 edges

## Surprising Connections (you probably didn't know these)
- `test_permission_gate()` --uses--> `PermissionLevel`  [INFERRED]
  test_agent.py → src/agent/permissions.py
- `test_tool_registry()` --uses--> `ToolCategory`  [INFERRED]
  test_agent.py → src/agent/tools.py
- `test_agent_executor()` --calls--> `Settings`  [EXTRACTED]
  test_agent.py → src/config/settings.py
- `test_audit_log_tab()` --calls--> `Settings`  [EXTRACTED]
  test_step3_final.py → src/config/settings.py
- `test_frozen_path_resolution()` --calls--> `Settings`  [EXTRACTED]
  test_step3_final.py → src/config/settings.py

## Import Cycles
- None detected.

## Hyperedges (group relationships)
- **Altron Core Capabilities** — readme_capsule_ui, readme_wake_word, readme_tts, readme_desktop_icon_control, readme_telegram_control [EXTRACTED 1.00]

## Communities (43 total, 28 thin omitted)

### Community 0 - "Packaging and Build Scripts"
Cohesion: 0.06
Nodes (12): build(), ConnectionStatus, chunk_message(), send_telegram_document(), send_telegram_message(), send_telegram_message_async(), send_telegram_photo(), send_telegram_photo_async() (+4 more)

### Community 1 - "Configuration and AI Client"
Cohesion: 0.06
Nodes (8): load_env_file(), Settings, AIClient, _worker(), AIResponse, Message, test_chat_ui(), test_omniroute()

### Community 2 - "Permission Request Card UI"
Cohesion: 0.07
Nodes (8): AgentStep, PermissionRequest, MessageBubble, PermissionCard, StepBubble, main(), test_chat_window_and_signals(), test_permission_card_and_unblocking()

### Community 3 - "Application Lifecycle Management"
Cohesion: 0.06
Nodes (4): Application, main(), SystemTrayIcon, test_application()

### Community 4 - "Desktop and System Tools"
Cohesion: 0.11
Nodes (15): create_default_registry(), handle_app_close(), handle_app_open(), handle_browser_open(), handle_capture_and_inspect_screen(), handle_create_and_send_pdf(), handle_file_copy(), handle_file_delete() (+7 more)

### Community 5 - "Media and Messaging Tools"
Cohesion: 0.10
Nodes (13): extract_youtube_video_id(), fetch_youtube_transcript_text(), _get_clipboard_unicode(), handle_clipboard_manager(), handle_whatsapp_read(), handle_whatsapp_send(), handle_youtube_play_and_summarize(), _is_phone_number() (+5 more)

### Community 6 - "Code and Window Management"
Cohesion: 0.10
Nodes (9): handle_code_edit(), handle_web_fetch(), handle_window_control(), ToolCategory, main(), test_code_edit_tool(), test_frozen_path_resolution(), test_web_fetch_tool() (+1 more)

### Community 8 - "Tool Permission Management"
Cohesion: 0.13
Nodes (4): PermissionGate, main(), test_permission_gate(), test_tool_registry()

### Community 10 - "Desktop Icon Automation"
Cohesion: 0.19
Nodes (6): calculate_screen_coordinates(), get_desktop_icons(), get_desktop_listview_hwnd(), get_screen_dimensions(), LVITEMW, position_desktop_icon()

### Community 11 - "Core Agent Loop"
Cohesion: 0.16
Nodes (4): clean_tool_confirmation(), intercept_raw_code_or_payload(), match_local_intent(), sanitize_final_reply()

### Community 15 - "Agent Lifecycle Testing"
Cohesion: 0.13
Nodes (4): _worker(), AgentState, test_agent_executor(), on_step()

### Community 21 - "Desktop Assistant Application"
Cohesion: 0.15
Nodes (13): src.app, Altron AI Desktop Assistant, Floating Capsule UI, Desktop Icon Control (SysListView32), Remote Telegram Control, Conditional Text-to-Speech (TTS), Voice Wake-Word & Continuous Listening, comtypes (+5 more)

### Community 24 - "Permission Data Models"
Cohesion: 0.22
Nodes (3): AgentResponse, PermissionLevel, PermissionRule

### Community 26 - "File Management Tools"
Cohesion: 0.20
Nodes (5): handle_file_move(), handle_file_open(), handle_file_write(), handle_terminal_command(), test_new_file_tools()

### Community 32 - "Desktop Window Control"
Cohesion: 0.29
Nodes (3): find_matching_window(), handle_desktop_icon_position(), handle_move_application_window()

## Knowledge Gaps
- **13 isolated node(s):** `src.app`, `comtypes`, `PyAudio`, `PySide6`, `pyttsx3` (+8 more)
  These have ≤1 connection - possible missing edges or undocumented components. (Counts symbols only; 348 node(s) total have ≤1 connection when file, concept and rationale nodes are included.)
- **28 thin communities (<3 nodes) omitted from report** — run `graphify query` to explore isolated nodes.

## Suggested Questions
_Questions this graph is uniquely positioned to answer:_

- **Why does `ChatWindow` connect `Chat Interface View` to `Packaging and Build Scripts`, `Configuration and AI Client`, `Permission Request Card UI`, `Application Lifecycle Management`, `Notch Expansion Handler`, `Permission Resolution Handler`, `Agent Execution Flow`, `SQLite Database Storage`, `Agent Lifecycle Testing`, `Notch Window Adaptation`, `Voice Interaction Controller`, `Text To Speech Manager`, `Continuous Speech Recognition`, `Permission Data Models`, `Telegram Bot Worker`, `Dynamic Island UI Bridge`, `UI Collapse Management`, `Command Execution Pipeline`?**
  _High betweenness centrality (0.231) - this node is a cross-community bridge._
- **Are the 10 inferred relationships involving `ChatWindow` (e.g. with `AgentExecutor` and `AgentResponse`) actually correct?**
  _`ChatWindow` has 10 INFERRED edges - model-reasoned connections that need verification._
- **What connects `src.app`, `comtypes`, `PyAudio` to the rest of the system?**
  _13 weakly-connected nodes found - possible documentation gaps or missing edges._
- **Should `Packaging and Build Scripts` be split into smaller, more focused modules?**
  _Cohesion score 0.05727605727605728 - nodes in this community are weakly interconnected._
- **Why does `AgentExecutor` connect `Agent Execution Flow` to `Packaging and Build Scripts`, `Chat Message Storage`, `Permission Request Card UI`, `Configuration and AI Client`, `Desktop and System Tools`, `Application Lifecycle Management`, `Tool Permission Management`, `Core Agent Loop`, `Conversation and Permission Persistence`, `Tool Registry System`, `SQLite Database Storage`, `Agent Lifecycle Testing`, `Chat Interface View`, `Permission Data Models`, `Dynamic Island UI Bridge`?**
  _High betweenness centrality (0.146) - this node is a cross-community bridge._
- **Are the 4 inferred relationships involving `Database` (e.g. with `AgentExecutor` and `Application`) actually correct?**
  _`Database` has 4 INFERRED edges - model-reasoned connections that need verification._
- **Should `Configuration and AI Client` be split into smaller, more focused modules?**
  _Cohesion score 0.060408163265306125 - nodes in this community are weakly interconnected._