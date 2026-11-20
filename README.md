# Altron — AI Desktop Assistant
# مساعد سطح المكتب الذكي (ألترون)

[![Python](https://img.shields.io/badge/Python-3.10%2B-blue.svg)](https://www.python.org/)
[![UI](https://img.shields.io/badge/UI-Flask%20HUD-brightgreen.svg)](https://flask.palletsprojects.com/)
[![Platform](https://img.shields.io/badge/Platform-Windows%2010%20%2F%2011-0078D6.svg)](https://www.microsoft.com/windows)
[![License](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

---

## English

### Overview
**Altron** is an intelligent, autonomous Windows desktop assistant inspired by advanced AI agent workflows. A headless Python orchestrator (`main.py`) drives a local sci-fi "radar" HUD in your browser (Flask, `ui_server.py`), continuous voice wake-word detection, conditional speech synthesis (TTS), Win32-level desktop icon manipulation, file management, and remote smartphone control via Telegram.

### Key Capabilities
- **Radar HUD**: Local web dashboard (opened in app mode in Chromium browsers) showing live status, conversation log, hardware metrics, AI settings, and pending approval requests.
- **Voice Wake-Word & Continuous Listening**: Always-on ambient listener triggered by saying *"Altron"* (or *"ألترون"* in Arabic), with automatic pause during speech playback to avoid audio feedback.
- **Conditional Text-to-Speech (TTS)**: Context-aware voice responses with automatic Markdown and code-block stripping. Local Piper voices are preferred (fast, offline); edge-tts and Windows SAPI are fallbacks. See *Local voice (Piper)* below.
- **Desktop Icon Control**: Direct Win32 manipulation (`SysListView32`) to inspect, organize, and visually move desktop icons (center, corners, patterns, or precise coordinates).
- **PDF Generation & Reporting**: Creates beautifully formatted, styled PDF reports supporting RTL Arabic and LTR English layouts.
- **Remote Telegram Control**: Send commands and receive responses or generated documents directly on your phone via Telegram long-polling.
- **Anthropic Claude Backend**: Uses the Claude Messages API directly (default model `claude-sonnet-5-5`, fast model `claude-haiku-5-5`). The endpoint is fixed to `api.anthropic.com` by design.
- **Out-of-band Approvals**: Dangerous actions (file writes and deletes, terminal commands, closing apps, ...) never run unprompted. You approve or deny each request in the HUD or with Telegram inline buttons.
- **Autonomous Multi-Step Execution**: Give one request and Altron chains tool calls until the task is done (up to 20 tool iterations per turn) instead of stopping after the first step or asking "shall I continue?". It only pauses for an approval or when something genuinely blocks it (for example a login or credential prompt).

### Integrations

#### Artcraft Suite (native CLI integration)
Altron reads and writes the Artcraft apps directly from Python, with no GUI clicking. Writes are staged and verified before an existing file is replaced.

| App | Tools |
|---|---|
| DeckCraft (slides) | `deckcraft_create`, `deckcraft_read`, `deckcraft_edit` |
| WordCraft (documents) | `wordcraft_create`, `wordcraft_read`, `wordcraft_edit` |
| GridCraft (spreadsheets) | `gridcraft_create`, `gridcraft_read`, `gridcraft_edit` |
| PdfCraft (PDF) | `pdfcraft_read`, `pdfcraft_merge`, `pdfcraft_split` |

Relative paths resolve against your Desktop.

#### Steam
- `steam_list_installed`: scans the local library for installed games.
- `steam_launch_game`: launches a game by validated numeric appid.
- `steam_search_store`: searches the public Steam store.
- `steam_open_store_page`: opens a store page in the Steam client.
- `steam_add_to_cart`: one atomic call that searches, opens the page, scrolls, locates the "Add to Cart" button by vision and clicks it (bounded to five attempts).
- **Safety guardrail:** Altron never checks out. Buttons labelled Checkout, Purchase or "Add to Cart and Pay" are refused, so you always complete payment yourself. Steam URIs are built only from a numeric appid, never from free text.

#### Telegram File Sharing
`telegram_send_file` sends any local file (`.deckcraft`, `.docx`, `.xlsx`, `.pdf`, images, ...) to your Telegram chat through the Bot API `sendDocument` endpoint. It falls back to your Desktop for relative paths and enforces Telegram's 50 MB limit.

#### Wi-Fi Vault
`network_control` manages Wi-Fi and Bluetooth, and keeps saved Wi-Fi passwords in a local vault (`wifi_vault.json` in the runtime directory):
- `wifi_get_password`: reads a saved network's password and stores it in the vault.
- `wifi_forget`: backs the password up to the vault, then deletes the Windows profile.
- `wifi_connect_new`: adds and joins a network with a password, or reuses the vault's.

The vault file is local only and is never logged or committed.

### Approval Policy
| Tier | Behaviour | Examples |
|---|---|---|
| Safe | Runs automatically | Artcraft tools, Steam tools (including `steam_add_to_cart`), `telegram_send_file`, `network_control`, media and power control, notes and to-dos, reminders, `web_search`, `open_url`, `gui_click`, `gui_scroll`, camera, PDF creation |
| Conditional | Auto-runs for low-risk input, asks otherwise (never a blanket "allow") | `file_read`, `file_open`, `app_open`, `send_to_telegram` (sensitive paths ask) |
| Dangerous | Always asks, per call | `file_write`, `file_delete`, `file_move`, `file_copy`, `terminal_execute`, `app_close`, `code_edit`, `gui_type`, `gui_hotkey` |

Approvals are given in the HUD or with Telegram inline buttons. Voice can never approve. New tools default to Dangerous.

### Local voice (Piper)
Piper runs offline and is the preferred voice. Altron uses `en_US-joe-medium` for English and `ar_JO-kareem-medium` for Arabic, and applies the `ALTRON_VOICE_FX=ultron` chain to both Piper and edge-tts. Install the package and download the two voices once (Windows, from the project folder):

```powershell
pip install "piper-tts>=1.3.0"
python -m piper.download_voices en_US-joe-medium ar_JO-kareem-medium --download-dir "$env:USERPROFILE\.ai-desktop-assistant\voices"
```

Altron looks for the models in that folder automatically. To use other files, set `PIPER_MODEL_PATH` (English), `PIPER_MODEL_PATH_AR` (Arabic) or `PIPER_LENGTH_SCALE` (default `1.06`) in `.env`. If Piper or a model is missing, Altron falls back to edge-tts, so nothing breaks.

### Architecture
| Component | Role |
|---|---|
| `main.py` | **Official entry point.** Starts and supervises the HUD server, the voice listener and the Telegram listener on plain Python threads. |
| `ui_server.py` + `ui/` | Flask HUD server on `127.0.0.1:5000` (5001 if busy) and its web front end. |
| `src/agent/` | Agent executor (autonomous tool loop), tool registry, permission tiers, approval manager, conversation memory, vision and screen-coordinate helpers. |
| `src/services/` | Claude client, voice (VAD + faster-whisper or Groq STT, TTS), Telegram, HUD status, desktop icons, reminders, camera, media, network and Wi-Fi vault, notes, web search, `artcraft_service.py`, `steam_service.py`. |
| `tests/` | pytest suite (see *Run the Tests*). |
| `src/config/` | Settings (single config precedence), runtime paths, central logging. |
| `src/app.py`, `src/ui/` | **Legacy, deprecated** PySide6 capsule UI. These are kept so old imports still work. They are no longer the supported way to run Altron. |

**Configuration precedence** (the same everywhere): explicit argument > `.env` > process environment > built-in default.

### Runtime Directory
Altron keeps all mutable state out of the project folder, which matters because the project folder may be synced by OneDrive. The state lives in `%USERPROFILE%\.ai-desktop-assistant\` (override with `ALTRON_DATA_DIR`):

| File | Purpose |
|---|---|
| `memory.json` | Short-term conversation memory |
| `status.json` | Snapshot of the HUD state |
| `telegram_offset.json` | Last processed Telegram update (prevents replaying commands after a restart) |
| `conversation_history.json` | Redacted, capped, reference-only cross-session history |
| `wifi_vault.json` | Saved Wi-Fi passwords (local only) |
| `assistant.db` | Settings and history database |
| `altron.log` | Rotating log, 1 MB × 3 backups, with secrets redacted. Set `ALTRON_LOG_LEVEL=DEBUG` for more detail |

### Security Design
Altron runs tools on your desktop, so its HUD, configuration and tools are hardened (audit items S1–S8):

| ID | Protection |
|---|---|
| S1 | HUD API requires a random per-launch token (`X-HUD-Token`), a `127.0.0.1`/`localhost` Host header and a same-origin Origin. No CORS. |
| S2 | API key is only ever sent to `api.anthropic.com`; `base_url`/`provider` cannot be configured. State-changing endpoints accept JSON only. |
| S3 | The model cannot forge approval: `_`-prefixed tool input keys are stripped before dispatch. |
| S4 | Tools are tiered as safe, conditional (checked per call, e.g. sensitive paths) or dangerous (always ask). |
| S5 | Approvals are non-blocking, bound to a hash of the arguments, single-use and expiring. They are accepted from the HUD or from the owner's Telegram account. Voice can never approve. |
| S6 | Bot tokens and API keys are redacted from logs and errors. `.env` values are written safely. |
| S7 | No `shell=True`. Executables are resolved and launched with argument lists. |
| S8 | Runtime files live in `~/.ai-desktop-assistant/`, not in the project folder. |

---

### ⚠️ Mandatory Telegram Bot Setup
To enable remote control from your phone and receive output messages/documents:
1. **Create your Bot**:
   - Open Telegram and search for [`@BotFather`](https://t.me/BotFather).
   - Send `/newbot` and follow instructions to choose a name and username.
   - Copy the HTTP API token (`TELEGRAM_BOT_TOKEN`).
2. **Find your Numeric Chat ID**:
   - In Telegram, message [`@userinfobot`](https://t.me/userinfobot) or [`@raw_data_bot`](https://t.me/raw_data_bot).
   - Copy your numeric ID (e.g. `123456789`).
3. **Configure `.env`**:
   - Copy `.env.example` to `.env`:
     ```bash
     copy .env.example .env
     ```
   - Open `.env` and fill in your values:
     ```env
     TELEGRAM_BOT_TOKEN=your_bot_token_here
     TELEGRAM_CHAT_ID=your_chat_id_here
     ```

---

### Installation & Getting Started

#### 1. Prerequisites
- Windows 10 or Windows 11
- Python 3.10 to 3.13
- A working microphone (for voice commands)

#### 2. Clone the Repository
```bash
git clone https://github.com/<your-username>/AI-Desktop-Assistant.git
cd AI-Desktop-Assistant
```

#### 3. Create a Virtual Environment (Recommended)
```bash
python -m venv venv
venv\Scripts\activate
```

#### 4. Install Dependencies
```bash
pip install -r requirements.txt
```

#### 5. Configure Environment Variables
Copy `.env.example` to `.env`:
```bash
copy .env.example .env
```
Edit `.env` (see `.env.example`) to set:

| Variable | Purpose |
|---|---|
| `ANTHROPIC_API_KEY` | Claude API key (required) |
| `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID` | Telegram remote control, approvals and file sending |
| `GROQ_API_KEY` | Optional. Sends recorded speech (STT only) to Groq Whisper; local faster-whisper is the fallback |
| `ALTRON_MIC_DEVICE`, `ALTRON_REQUIRE_WAKE_WORD` | Optional microphone and wake-word behaviour |
| `ALTRON_CAMERA_AUTOSTART` | Optional webcam warm-up (frames stay in RAM) |
| `ALTRON_VOICE_FX`, `PIPER_MODEL_PATH`, `PIPER_MODEL_PATH_AR`, `PIPER_LENGTH_SCALE` | Optional voice settings |
| `ALTRON_DATA_DIR`, `ALTRON_LOG_LEVEL` | Optional runtime directory and log level |

You can also change the model, key and language later from the HUD settings panel. Steam tools need the Steam client installed; Artcraft tools need the Artcraft apps.

#### 6. ⚠️ Important Windows Desktop Setting
To allow Altron to move and position desktop icons:
- Right-click anywhere on your Windows Desktop.
- Hover over **View**.
- **Uncheck "Auto arrange icons"** (and optionally uncheck "Align icons to grid" for smooth custom coordinates).

#### 7. Launch the Application
```bash
python main.py
```
The HUD opens in your browser at `http://127.0.0.1:5000`, and the console prints `Altron is online.` Press `Ctrl+C` to stop.

> `python -m src.app` (the PySide6 capsule UI) is deprecated and no longer maintained.

#### 8. Run the Tests
```bash
pip install pytest
pytest -p no:cacheprovider
```
`pytest.ini` limits collection to `tests/` and `test_agent.py` and disables the pytest cache, so nothing is written into the synced project folder. Tests use a temporary `ALTRON_DATA_DIR` and never touch your real `.env` or the network. Current status: **782 passed**. The root `test_app.py`, `test_chat_ui.py`, `test_step3_final.py` and `test_ui_integration.py` are manual GUI scripts for the legacy UI, not part of the suite.

---

## العربية

### نظرة عامة
**ألترون (Altron)** هو مساعد سطح مكتب ذكي ومستقل لنظام ويندوز، مستوحى من أحدث تقنيات عملاء الذكاء الاصطناعي. يعمل عبر منسق رئيسي (`main.py`) مع لوحة HUD محلية في المتصفح مبنية بـ Flask (`ui_server.py`)، ونظام استماع صوتي مستمر لكلمة التنبيه ("ألترون")، ونطق صوتي ذكي (TTS)، وإمكانية تحريك وترتيب أيقونات سطح المكتب عبر Win32، وإدارة الملفات وإنشاء التقارير، والتحكم الكامل عن بُعد عبر تطبيق تيليجرام.

### أبرز المزايا
- **لوحة HUD تفاعلية**: لوحة ويب محلية تعرض الحالة وسجل المحادثة ومقاييس الجهاز والإعدادات وطلبات الموافقة على الإجراءات الخطيرة.
- **استماع صوتي مستمر لكلمة التنبيه**: تعرف دائم على الصوت عند نداء *"ألترون"* أو *"Altron"*، مع إيقاف مؤقت ذكي أثناء النطق لمنع ارتداد الصوت.
- **نطق صوتي ذكي (TTS)**: قراءة ملخصة للردود باستخدام محرك Windows SAPI الصوتي مع إزالة الأكواد وعلامات الماركداون تلقائياً.
- **تحريك أيقونات سطح المكتب**: تواصل مباشر مع نافذة `SysListView32` في نظام ويندوز للبحث عن الأيقونات ونقلها لأي مكان (الوسط، الأركان، أو إحداثيات محددة).
- **إنشاء مستندات PDF احترافية**: توليد تقارير منسقة تدعم اللغة العربية (من اليمين لليسار RTL) والإنجليزية.
- **تحكم عن بُعد عبر تيليجرام**: إرسال الأوامر واستقبال الردود والمستندات المنشأة مباشرة على هاتفك أينما كنت.
- **نماذج Claude من Anthropic**: يتصل مباشرة بواجهة Anthropic Claude API فقط (النموذج الافتراضي `claude-sonnet-5-5`).
- **حزمة Artcraft**: إنشاء وقراءة وتعديل ملفات DeckCraft وWordCraft وGridCraft ودمج وتقسيم PdfCraft مباشرة عبر بايثون دون نقرات واجهة.
- **تكامل Steam**: فحص المكتبة المثبتة وتشغيل الألعاب والبحث في المتجر وفتح صفحات المتجر، والأداة `steam_add_to_cart` تضيف اللعبة إلى السلة فقط ولا تُكمل الدفع أبداً.
- **مشاركة الملفات عبر تيليجرام**: الأداة `telegram_send_file` ترسل أي ملف محلي إلى محادثتك (حد أقصى 50 ميغابايت).
- **خزنة Wi-Fi**: قراءة كلمات مرور الشبكات المحفوظة (`wifi_get_password`) ونسيان الشبكات مع نسخة احتياطية (`wifi_forget`) والاتصال بشبكة جديدة (`wifi_connect_new`).
- **تنفيذ مستقل متعدد الخطوات**: يُكمل المهمة بسلسلة من الأدوات (حتى 20 خطوة في الدور الواحد) دون توقف بعد أول خطوة.
- **سياسة الموافقات**: الأدوات الآمنة تعمل تلقائياً، أما الكتابة والحذف والنقل والنسخ وأوامر الطرفية وإغلاق التطبيقات والكتابة بلوحة المفاتيح فتتطلب موافقتك دائماً.
- **ملفات التشغيل**: تُحفظ الذاكرة والحالة والسجل (`altron.log`) في المجلد `‎%USERPROFILE%\.ai-desktop-assistant\` خارج مجلد المشروع.

---

### ⚠️ إعداد بوت تيليجرام (إلزامي للتحكم عن بُعد)
لتفعيل ميزة التحكم عن بُعد واستلام المستندات والرسائل على هاتفك:
1. **إنشاء البوت**:
   - افتح تطبيق تيليجرام وابحث عن [`@BotFather`](https://t.me/BotFather).
   - أرسل الأمر `/newbot` واختر اسماً ومعرفاً للبوت.
   - انسخ الرمز السري الخاص بالبوت (`TELEGRAM_BOT_TOKEN`).
2. **معرفة معرف حسابك (Chat ID)**:
   - راسل البوت [`@userinfobot`](https://t.me/userinfobot) أو [`@raw_data_bot`](https://t.me/raw_data_bot).
   - انسخ المعرف الرقمي الخاص بك (مثل `123456789`).
3. **تحديث ملف `.env`**:
   - انسخ ملف `.env.example` إلى `.env`:
     ```bash
     copy .env.example .env
     ```
   - أضف بيانات البوت في ملف `.env`:
     ```env
     TELEGRAM_BOT_TOKEN=your_bot_token_here
     TELEGRAM_CHAT_ID=your_chat_id_here
     ```

---

### خطوات التثبيت والتشغيل

#### 1. المتطلبات
- نظام تشغيل Windows 10 أو Windows 11
- بايثون بالإصدار 3.10 إلى 3.13
- ميكروفون متصل لتفعيل الأوامر الصوتية

#### 2. استنساخ المشروع
```bash
git clone https://github.com/<your-username>/AI-Desktop-Assistant.git
cd AI-Desktop-Assistant
```

#### 3. إنشاء بيئة افتراضية (موصى به)
```bash
python -m venv venv
venv\Scripts\activate
```

#### 4. تثبيت المكتبات المطلوبة
```bash
pip install -r requirements.txt
```

#### 5. إعداد المتغيرات البيئية
انسخ ملف الإعدادات:
```bash
copy .env.example .env
```
قم بملء المتغيرات الخاصة بالذكاء الاصطناعي وبوت التيليجرام داخل ملف `.env`.

#### 6. ⚠️ خطوة هامة في سطح المكتب لويندوز
لكي يتمكن المساعد من نقل أيقونات سطح المكتب وترتيبها:
- انقر بزر الفأرة الأيمن على مساحة فارغة في **سطح المكتب**.
- اختر **عرض (View)**.
- **ألغِ تفعيل خيار "ترتيب الرموز تلقائياً" (Auto arrange icons)**.

#### 7. تشغيل البرنامج
```bash
python main.py
```
تُفتح لوحة HUD في المتصفح على العنوان `http://127.0.0.1:5000`. (واجهة PySide6 القديمة `python -m src.app` لم تعد مدعومة.)

#### 8. تشغيل الاختبارات
```bash
pytest -p no:cacheprovider
```

---

## License
This project is licensed under the MIT License - see the [LICENSE](LICENSE) file for details.
