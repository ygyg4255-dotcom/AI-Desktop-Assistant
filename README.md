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
| `src/agent/` | Agent executor, tool registry, permission tiers, approval manager, conversation memory. |
| `src/services/` | Claude client, voice (VAD + faster-whisper STT, TTS), Telegram, HUD status, desktop automation. |
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
Edit `.env` to set your Anthropic API key (`AI_API_KEY`), optionally `AI_MODEL`, and your Telegram bot credentials. You can also change the model, key and language later from the HUD settings panel.

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
`pytest.ini` limits collection to `tests/` and `test_agent.py` and disables the pytest cache, so nothing is written into the synced project folder. Tests use a temporary `ALTRON_DATA_DIR` and never touch your real `.env` or the network.

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
