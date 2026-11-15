# Altron — AI Desktop Assistant
# مساعد سطح المكتب الذكي (ألترون)

[![Python](https://img.shields.io/badge/Python-3.10%2B-blue.svg)](https://www.python.org/)
[![UI](https://img.shields.io/badge/UI-PySide6%20%2F%20Qt6-brightgreen.svg)](https://www.qt.io/)
[![Platform](https://img.shields.io/badge/Platform-Windows%2010%20%2F%2011-0078D6.svg)](https://www.microsoft.com/windows)
[![License](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

---

## English

### Overview
**Altron** is an intelligent, autonomous Windows desktop assistant inspired by advanced AI agent workflows. It features a modern floating dynamic capsule UI (similar to a Dynamic Island) built with PySide6, continuous voice wake-word detection, conditional speech synthesis (TTS), Win32-level desktop icon manipulation, file management, and remote smartphone control via Telegram.

### Key Capabilities
- **Floating Capsule UI**: Sleek, borderless, floating dynamic island that expands smoothly for interaction and collapses when idle.
- **Voice Wake-Word & Continuous Listening**: Always-on ambient listener triggered by saying *"Altron"* (or *"ألترون"* in Arabic), with automatic pause during speech playback to avoid audio feedback.
- **Conditional Text-to-Speech (TTS)**: Context-aware voice responses using Windows SAPI / COM with automatic Markdown and code-block stripping.
- **Desktop Icon Control**: Direct Win32 manipulation (`SysListView32`) to inspect, organize, and visually move desktop icons (center, corners, patterns, or precise coordinates).
- **PDF Generation & Reporting**: Creates beautifully formatted, styled PDF reports supporting RTL Arabic and LTR English layouts.
- **Remote Telegram Control**: Send commands and receive responses or generated documents directly on your phone via Telegram long-polling.
- **OmniRoute / Anthropic Backend**: Compatible with local LLM proxies (OmniRoute) or direct Anthropic Claude API endpoints.

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
Edit `.env` to configure your LLM endpoint (OmniRoute default `http://localhost:20128` or your Anthropic API key) and your Telegram bot credentials.

#### 6. ⚠️ Important Windows Desktop Setting
To allow Altron to move and position desktop icons:
- Right-click anywhere on your Windows Desktop.
- Hover over **View**.
- **Uncheck "Auto arrange icons"** (and optionally uncheck "Align icons to grid" for smooth custom coordinates).

#### 7. Launch the Application
```bash
python -m src.app
```
Alternatively, launch from tray or run standalone tests:
```bash
python test_ui_integration.py
```

---

## العربية

### نظرة عامة
**ألترون (Altron)** هو مساعد سطح مكتب ذكي ومستقل لنظام ويندوز، مستوحى من أحدث تقنيات عملاء الذكاء الاصطناعي. يتميز بواجهة كبسولة عائمة عصرية (شبيهة بـ Dynamic Island) مبنية باستخدام مكتبة PySide6، ونظام استماع صوتي مستمر لكلمة التنبيه ("ألترون")، ونطق صوتي ذكي (TTS)، وإمكانية تحريك وترتيب أيقونات سطح المكتب عبر Win32، وإدارة الملفات وإنشاء التقارير، والتحكم الكامل عن بُعد عبر تطبيق تيليجرام.

### أبرز المزايا
- **كبسولة تفاعلية عائمة**: واجهة ديناميكية بدون إطارات تتوسع بسلاسة للمحادثة وتتقلص عند الخمول لتوفير مساحة الشاشة.
- **استماع صوتي مستمر لكلمة التنبيه**: تعرف دائم على الصوت عند نداء *"ألترون"* أو *"Altron"*، مع إيقاف مؤقت ذكي أثناء النطق لمنع ارتداد الصوت.
- **نطق صوتي ذكي (TTS)**: قراءة ملخصة للردود باستخدام محرك Windows SAPI الصوتي مع إزالة الأكواد وعلامات الماركداون تلقائياً.
- **تحريك أيقونات سطح المكتب**: تواصل مباشر مع نافذة `SysListView32` في نظام ويندوز للبحث عن الأيقونات ونقلها لأي مكان (الوسط، الأركان، أو إحداثيات محددة).
- **إنشاء مستندات PDF احترافية**: توليد تقارير منسقة تدعم اللغة العربية (من اليمين لليسار RTL) والإنجليزية.
- **تحكم عن بُعد عبر تيليجرام**: إرسال الأوامر واستقبال الردود والمستندات المنشأة مباشرة على هاتفك أينما كنت.
- **دعم نماذج الذكاء الاصطناعي**: متوافق تماماً مع وسيط OmniRoute المحلي أو واجهة Anthropic Claude API مباشرة.

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
python -m src.app
```

---

## License
This project is licensed under the MIT License - see the [LICENSE](LICENSE) file for details.
