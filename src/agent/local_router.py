"""Local (zero-API) intent router: the legacy rule chain first, then the ROUTES table."""
import difflib
import re
import shutil
import time
from typing import Any, Dict, Optional


def _match_legacy_intent(prompt: str) -> Optional[Dict[str, Any]]:
    """Match direct system control and YouTube commands to bypass LLM latency."""
    if not prompt:
        return None

    p = prompt.strip().lower()
    clean_p = re.sub(r'[\.\,\!\?؟]+$', '', p).strip()
    is_arabic = any('؀' <= char <= 'ۿ' for char in clean_p)

    # Normalize Arabic/Eastern digits to standard 0-9
    clean_p_norm = clean_p
    for ar_digit, en_digit in zip("٠١٢٣٤٥٦٧٨٩", "0123456789"):
        clean_p_norm = clean_p_norm.replace(ar_digit, en_digit)

    # Arabic text normalization for fuzzy matching
    def _normalize_arabic(text: str) -> str:
        text = re.sub(r'[ً-ٰٟ]', '', text)  # Tashkeel / diacritics
        text = re.sub(r'[إأآا]', 'ا', text)               # Normalize Alef variants
        text = re.sub(r'ة', 'ه', text)                     # Normalize Teh Marbuta
        text = re.sub(r'ى', 'ي', text)                     # Normalize Alef Maksura
        return text

    norm_ar = _normalize_arabic(clean_p_norm)
    tokens = clean_p_norm.split()

    def _fuzzy_token(token: str, target: str, min_ratio: float = 0.72) -> bool:
        if token == target:
            return True
        return difflib.SequenceMatcher(None, token, target).ratio() >= min_ratio

    # 0a. Application Window Movement Intent (STRICT PRIORITY when window/browser/app is mentioned)
    window_markers_ar = ("نافذة", "نافذه", "متصفح", "المتصفح", "برنامج", "تطبيق", "كروم", "بريف", "ايدج", "موقع")
    window_markers_en = ("window", "browser", "app", "application", "chrome", "brave", "edge", "vscode", "omniroute")
    has_window_marker = any(w in clean_p_norm for w in window_markers_ar) or any(w in clean_p_norm for w in window_markers_en)

    ar_pos_map = {
        "وسط": "center", "الوسط": "center", "للوسط": "center", "منتصف": "center", "المنتصف": "center", "للمنتصف": "center",
        "مركز": "center", "المركز": "center", "للمركز": "center",
        "يمين": "right", "اليمين": "right", "لليمين": "right", "في يمين": "right", "على يمين": "right",
        "يسار": "left", "اليسار": "left", "لليسار": "left", "شمال": "left", "الشمال": "left", "للشمال": "left", "في يسار": "left", "على يسار": "left",
        "اعلى": "top", "أعلى": "top", "الاعلى": "top", "الأعلى": "top", "للاعلى": "top", "للأعلى": "top", "فوق": "top", "لفوق": "top",
        "اسفل": "bottom", "أسفل": "bottom", "الاسفل": "bottom", "الأسفل": "bottom", "للاسفل": "bottom", "للأسفل": "bottom", "تحت": "bottom", "لتحت": "bottom",
        "اعلى اليمين": "top_right", "أعلى اليمين": "top_right", "فوق يمين": "top_right", "لأعلى اليمين": "top_right",
        "اعلى اليسار": "top_left", "أعلى اليسار": "top_left", "فوق يسار": "top_left", "لأعلى اليسار": "top_left",
        "اسفل اليمين": "bottom_right", "أسفل اليمين": "bottom_right", "تحت يمين": "bottom_right", "لأسفل اليمين": "bottom_right",
        "اسفل اليسار": "bottom_left", "أسفل اليسار": "bottom_left", "تحت يسار": "bottom_left", "لأسفل اليسار": "bottom_left",
        "تكبير": "maximize", "كبر": "maximize", "ملء الشاشة": "maximize", "كامل الشاشة": "maximize",
        "تصغير": "minimize", "صغر": "minimize", "اخفاء": "minimize"
    }
    pos_keys = "|".join(sorted(ar_pos_map.keys(), key=len, reverse=True))

    if has_window_marker:
        ar_win_pat = re.compile(
            rf'^(?:حرك|انقل|ضع|ودي|جيب|خلي|سوي|اعمل)?\s*(.+?)\s+(?:إلى|الى|في|لـ|ل|على)?\s*({pos_keys})(?:\s+(?:من\s+)?الشاشة)?$',
            re.IGNORECASE
        )
        en_win_pat = re.compile(
            r'^(?:move|position|place|relocate|put|set|snap)\s+(.+?)\s+(?:to\s+(?:the\s+)?|at\s+(?:the\s+)?|in\s+(?:the\s+)?)(center|middle|centre|top|bottom|left|right|top[ -_]left|top[ -_]right|bottom[ -_]left|bottom[ -_]right|maximize|minimize)(?:\s+(?:of\s+)?(?:the\s+)?(?:screen))?$',
            re.IGNORECASE
        )
        m_ar_win = ar_win_pat.match(clean_p)
        m_en_win = en_win_pat.match(clean_p)

        if m_ar_win:
            raw_target = m_ar_win.group(1).strip()
            raw_pos = m_ar_win.group(2).strip()
            win_pos = ar_pos_map.get(raw_pos, "right")
            pos_ar_desc = {
                "center": "وسط الشاشة",
                "top": "أعلى الشاشة",
                "bottom": "أسفل الشاشة",
                "left": "يسار الشاشة",
                "right": "يمين الشاشة",
                "top_right": "أعلى يمين الشاشة",
                "top_left": "أعلى يسار الشاشة",
                "bottom_right": "أسفل يمين الشاشة",
                "bottom_left": "أسفل يسار الشاشة",
                "maximize": "كامل الشاشة",
                "minimize": "شريط المهام"
            }.get(win_pos, "يمين الشاشة")

            return {
                "tool_name": "move_application_window",
                "tool_input": {"window_query": raw_target or clean_p, "position": win_pos},
                "confirmation": f"✅ تم ضبط وتحريك النافذة إلى {pos_ar_desc} بنجاح."
            }

        elif m_en_win:
            raw_target = m_en_win.group(1).strip()
            raw_pos = m_en_win.group(2).strip().lower().replace(" ", "_").replace("-", "_")
            if raw_pos in ("middle", "centre"):
                raw_pos = "center"
            return {
                "tool_name": "move_application_window",
                "tool_input": {"window_query": raw_target or clean_p, "position": raw_pos},
                "confirmation": f"✅ Successfully moved window to {raw_pos.replace('_', ' ')}."
            }
        else:
            # Fallback for general window movement with position
            found_pos = None
            for pkey, pval in ar_pos_map.items():
                if pkey in clean_p:
                    found_pos = pval
                    break
            if not found_pos:
                for pword in ("right", "left", "center", "top", "bottom", "maximize", "minimize"):
                    if pword in clean_p.lower():
                        found_pos = pword
                        break

            if found_pos:
                return {
                    "tool_name": "move_application_window",
                    "tool_input": {"window_query": clean_p, "position": found_pos},
                    "confirmation": f"✅ تم تحريك وضبط النافذة بنجاح." if is_arabic else f"✅ Successfully adjusted window position."
                }

    # 0b. Desktop Icon Positioning Intent (STRICTLY ONLY when NO window/browser mentioned)
    if not has_window_marker:
        ar_desktop_pat = re.compile(
            rf'^(?:حرك|انقل|ضع|ودي|جيب)\s+(?:ملف|ايقونة|أيقونة|عنصر)\s*(.+?)\s+(?:إلى|الى|في|لـ|ل)?\s*({pos_keys})(?:\s+(?:من\s+)?الشاشة|\s+(?:من\s+)?سطح المكتب)?$',
            re.IGNORECASE
        )
        en_desktop_pat = re.compile(
            r'^(?:move|position|place|relocate|put|set)\s+(?:desktop\s+)?(?:icon|file|item)\s+(.+?)\s+(?:to\s+(?:the\s+)?|at\s+(?:the\s+)?|in\s+(?:the\s+)?)(center|middle|centre|top|bottom|left|right|top[ -_]left|top[ -_]right|bottom[ -_]left|bottom[ -_]right)(?:\s+(?:of\s+)?(?:the\s+)?(?:screen|desktop))?$',
            re.IGNORECASE
        )

        m_en_dt = en_desktop_pat.match(clean_p)
        m_ar_dt = ar_desktop_pat.match(clean_p)
        if m_en_dt:
            dt_target = m_en_dt.group(1).strip().strip('"\'')
            dt_pos = m_en_dt.group(2).strip().lower().replace(" ", "_").replace("-", "_")
            if dt_pos in ("middle", "centre"):
                dt_pos = "center"
            return {
                "tool_name": "desktop_icon_position",
                "tool_input": {"target": dt_target, "position": dt_pos},
                "confirmation": f"✅ Successfully moved '{dt_target}' icon to {dt_pos.replace('_', ' ')}."
            }
        elif m_ar_dt:
            dt_target = m_ar_dt.group(1).strip().strip('"\'')
            dt_pos_raw = m_ar_dt.group(2).strip()
            dt_pos = ar_pos_map.get(dt_pos_raw, "center")
            pos_ar_desc = {
                "center": "وسط الشاشة",
                "top": "أعلى الشاشة",
                "bottom": "أسفل الشاشة",
                "left": "يسار الشاشة",
                "right": "يمين الشاشة",
                "top_right": "أعلى يمين الشاشة",
                "top_left": "أعلى يسار الشاشة",
                "bottom_right": "أسفل يمين الشاشة",
                "bottom_left": "أسفل يسار الشاشة"
            }.get(dt_pos, "وسط الشاشة")
            return {
                "tool_name": "desktop_icon_position",
                "tool_input": {"target": dt_target, "position": dt_pos},
                "confirmation": f"✅ تم تحريك أيقونة '{dt_target}' إلى {pos_ar_desc} بنجاح."
            }

    # 1. YouTube Play (Priority if explicit YouTube search/play query, but NOT when asking to summarize)
    is_summarize_query = any(w in clean_p for w in ("لخص", "تلخيص", "ملخص", "شرح", "عن ماذا", "محتوى", "تفاصيل", "اعطني ملخص", "اعطني تفاصيل", "summarize", "summary", "tldr", "tl;dr", "explain", "what is it about"))
    if not is_summarize_query:
        yt_patterns = [
            # Arabic specific
            r'^(?:افتح\s+اليوتيوب\s+وشغل|افتح\s+يوتيوب\s+وشغل)\s+(.+)$',
            r'^(?:شغل|افتح|ابحث عن)\s+(.+?)\s+(?:على\s+اليوتيوب|على\s+يوتيوب|في\s+اليوتيوب|في\s+يوتيوب|يوتيوب)$',
            r'^(?:شغل|افتح)\s+(?:على\s+اليوتيوب|على\s+يوتيوب|في\s+اليوتيوب|في\s+يوتيوب|يوتيوب)\s+(.+)$',
            r'^(?:شغل\s+اغنية|شغل\s+أغنية|شغل\s+فيديو|شغل\s+كليب|شغل\s+موسيقى)\s+(.+)$',
            # English specific
            r'^(?:open\s+youtube\s+and\s+play)\s+(.+)$',
            r'^(?:play|open)\s+(.+?)\s+(?:on\s+youtube|in\s+youtube|youtube)$',
            r'^(?:youtube\s+play|play\s+on\s+youtube|play\s+in\s+youtube)\s+(.+)$',
            r'^(?:play\s+song|play\s+video|play\s+music)\s+(.+)$',
        ]
        for pattern in yt_patterns:
            match = re.match(pattern, clean_p, re.IGNORECASE)
            if match:
                query = match.group(1).strip().strip('"\'')
                if query:
                    msg = f"✅ تم تشغيل '{query}' على يوتيوب." if is_arabic else f"✅ Playing '{query}' on YouTube."
                    return {
                        "tool_name": "youtube_play",
                        "tool_input": {"query": query},
                        "confirmation": msg
                    }

    # 2. Windows Settings with Typo Tolerance (e.g. "open srtting", "open seting", "settings", "افتح الاعدادات")
    is_settings = False
    open_verbs = {"open", "launch", "show", "goto", "start", "opne", "oepn", "opn"}
    settings_words = {"settings", "setting", "seting", "setings", "srtting", "srttings", "settngs", "sttings", "configuration", "config"}

    if clean_p_norm in settings_words or any(_fuzzy_token(clean_p_norm, target, 0.72) for target in ["settings", "setting"]):
        is_settings = True
    elif len(tokens) >= 2:
        has_open = any(t in open_verbs or _fuzzy_token(t, "open", 0.75) for t in tokens)
        has_setting = any(t in settings_words or _fuzzy_token(t, "settings", 0.72) or _fuzzy_token(t, "setting", 0.75) for t in tokens)
        if has_open and has_setting:
            is_settings = True

    # Arabic Settings
    if not is_settings:
        ar_settings_roots = {"اعدادات", "الاعدادات", "ضبط", "الضبط"}
        if norm_ar in ar_settings_roots or any(r in norm_ar for r in ["اعدادات الويندوز", "اعدادات النظام", "اعدادات الجهاز"]):
            is_settings = True
        elif any(root in norm_ar for root in ["اعدادات", "ضبط"]):
            ar_verbs = ["افتح", "فتح", "شغل", "عرض", "وريني", "ادخل"]
            if any(v in norm_ar for v in ar_verbs) or len(norm_ar.split()) <= 2:
                is_settings = True

    # Close/hide verbs must never open Settings (they fall through to app_close / the LLM).
    if is_settings and (set(clean_p_norm.split()) | set(norm_ar.split())) & {"close", "quit", "exit", "hide", "minimize", "اغلق", "أغلق", "اقفل", "اخف", "اخفي"}:
        is_settings = False

    if is_settings:
        msg = "✅ تم فتح إعدادات النظام." if is_arabic else "✅ Opened Windows Settings."
        return {
            "tool_name": "system_control",
            "tool_input": {"action": "open_settings"},
            "confirmation": msg
        }

    # 3. Numeric Volume & Brightness Settings with Fuzzy Matching (e.g. "volum 33", "صوت 33", "brightness 50")
    if not ("youtube" in clean_p_norm or "يوتيوب" in clean_p_norm):
        m_num = re.search(r'(\d{1,3})', clean_p_norm)
        if m_num:
            val = max(0, min(100, int(m_num.group(1))))

            # Check for brightness first
            is_bright = (
                any(t in {"brightness", "brightnes", "screen", "display"} for t in tokens) or
                any(_fuzzy_token(t, "brightness", 0.75) for t in tokens) or
                any(w in norm_ar for w in {"سطوع", "السطوع", "اضاءة", "الاضاءة", "اضاءه", "الاضاءه", "نور"})
            )
            if is_bright:
                msg = f"✅ تم ضبط السطوع على {val}%." if is_arabic else f"✅ Brightness set to {val}%."
                return {
                    "tool_name": "system_control",
                    "tool_input": {"action": "set_brightness", "value": val},
                    "confirmation": msg
                }

            # Check for volume (e.g. "volum 33", "volume 33", "vol 33", "sound 33", "صوت 33", "الصوت 33")
            is_vol = (
                any(t in {"vol", "volume", "volum", "voulme", "volue", "volumn", "sound", "audio"} for t in tokens) or
                any(_fuzzy_token(t, "volume", 0.72) for t in tokens) or
                any(_fuzzy_token(t, "sound", 0.75) for t in tokens) or
                ("صوت" in norm_ar)
            )
            if is_vol:
                msg = f"✅ تم ضبط مستوى الصوت على {val}%." if is_arabic else f"✅ Volume set to {val}%."
                return {
                    "tool_name": "system_control",
                    "tool_input": {"action": "set_volume", "value": val},
                    "confirmation": msg
                }

    # 4. Volume Up
    vol_up_en = {"volume up", "increase volume", "raise volume", "turn up volume", "louder", "sound up", "volume +", "vol up", "volum up"}
    vol_up_tokens = {"up", "+"}
    is_vol_up = (
        clean_p_norm in vol_up_en or
        (any(t in vol_up_tokens for t in tokens) and any(t in {"vol", "volume", "volum", "sound", "audio"} or _fuzzy_token(t, "volume", 0.72) for t in tokens)) or
        ("صوت" in norm_ar and any(w in norm_ar for w in {"ارفع", "علي", "رفع", "زود", "تعلي", "اعلى", "زياده", "زيادة"}))
    )
    if is_vol_up:
        msg = "✅ تم رفع مستوى الصوت." if is_arabic else "✅ Volume increased."
        return {
            "tool_name": "system_control",
            "tool_input": {"action": "volume_up"},
            "confirmation": msg
        }

    # 5. Volume Down
    vol_down_en = {"volume down", "decrease volume", "lower volume", "turn down volume", "quieter", "sound down", "volume -", "vol down", "volum down"}
    vol_down_tokens = {"down", "-"}
    is_vol_down = (
        clean_p_norm in vol_down_en or
        (any(t in vol_down_tokens for t in tokens) and any(t in {"vol", "volume", "volum", "sound", "audio"} or _fuzzy_token(t, "volume", 0.72) for t in tokens)) or
        ("صوت" in norm_ar and any(w in norm_ar for w in {"خفض", "اخفض", "وطي", "نزل", "قلل", "توطيه", "توطية", "اقل"}))
    )
    if is_vol_down:
        msg = "✅ تم خفض مستوى الصوت." if is_arabic else "✅ Volume decreased."
        return {
            "tool_name": "system_control",
            "tool_input": {"action": "volume_down"},
            "confirmation": msg
        }

    # 6. Volume Mute
    vol_mute_en = {"mute", "unmute", "mute volume", "mute sound", "mute audio", "silence"}
    is_vol_mute = (
        clean_p_norm in vol_mute_en or
        any(t in ("mute", "unmute") for t in tokens) or  # exact: fuzzy 0.75 matched "minute"
        any(w in norm_ar for w in {"كتم", "صامت", "اخرس", "الغاء الكتم", "فك الكتم"})
    )
    if is_vol_mute:
        msg = "✅ تم كتم/إلغاء كتم الصوت." if is_arabic else "✅ Volume muted/unmuted."
        return {
            "tool_name": "system_control",
            "tool_input": {"action": "volume_mute"},
            "confirmation": msg
        }

    # 7. Brightness Up
    bright_up_en = {"brightness up", "increase brightness", "raise brightness", "brighter", "screen brighter", "brightness +"}
    is_bright_up = (
        clean_p_norm in bright_up_en or
        ("up" in tokens and any(t in {"brightness", "brightnes", "screen"} or _fuzzy_token(t, "brightness", 0.75) for t in tokens)) or
        (("سطوع" in norm_ar or "اضاءة" in norm_ar or "اضاءه" in norm_ar) and any(w in norm_ar for w in {"ارفع", "علي", "رفع", "زود", "اعلى", "زياده", "زيادة"}))
    )
    if is_bright_up:
        msg = "✅ تم رفع السطوع." if is_arabic else "✅ Brightness increased."
        return {
            "tool_name": "system_control",
            "tool_input": {"action": "brightness_up"},
            "confirmation": msg
        }

    # 8. Brightness Down
    bright_down_en = {"brightness down", "decrease brightness", "lower brightness", "dimmer", "dim screen", "brightness -"}
    is_bright_down = (
        clean_p_norm in bright_down_en or
        ("down" in tokens and any(t in {"brightness", "brightnes", "screen"} or _fuzzy_token(t, "brightness", 0.75) for t in tokens)) or
        (("سطوع" in norm_ar or "اضاءة" in norm_ar or "اضاءه" in norm_ar) and any(w in norm_ar for w in {"خفض", "اخفض", "وطي", "نزل", "قلل", "اقل"}))
    )
    if is_bright_down:
        msg = "✅ تم خفض السطوع." if is_arabic else "✅ Brightness decreased."
        return {
            "tool_name": "system_control",
            "tool_input": {"action": "brightness_down"},
            "confirmation": msg
        }

    # 9. Screen Capture / Vision Intent
    # Only explicit "take/send a screenshot" requests skip the LLM. Questions about what is
    # ON the screen ("can you see my screen", "what's on my screen", "read this error") go
    # to the model, which calls capture_and_inspect_screen and looks at the attached image.
    screen_cap_en = {
        "screenshot", "take a screenshot", "take screenshot", "capture screen",
        "capture the screen", "screen capture", "send screenshot"
    }
    is_screen_cap = (
        clean_p_norm in screen_cap_en or
        ("screenshot" in clean_p_norm) or
        ("screen" in tokens and any(t in tokens for t in ("capture", "take", "grab"))) or
        any(w in norm_ar for w in {"صور الشاشه", "صور الشاشة", "لقطه شاشه", "لقطة شاشة", "صوره للشاشه", "صورة للشاشة", "صور شاشتي", "سكرين شوت", "سكرينشوت"})
    )
    if is_screen_cap:
        msg = "✅ جاري فحص الشاشة والتقاطها وإرسالها إلى تيليجرام..." if is_arabic else "✅ Capturing desktop screen and delivering to Telegram..."
        return {
            "tool_name": "capture_and_inspect_screen",
            "tool_input": {"send_to_telegram": True},
            "confirmation": msg
        }

    # 10. WhatsApp Messaging & Reading Intent (Strict priority for WhatsApp requests and follow-up reading commands)
    has_wa_keyword = any(w in clean_p for w in ("واتساب", "واتس", "whatsapp"))
    has_meta_ai = any(w in clean_p for w in ("meta ai", "metaai", "ميتا ai", "ميتا اي", "ميتا"))

    is_reply_forward_or_read = (
        any(w in clean_p for w in (
            "ارسل اجابة", "ارسل اجابه", "ارسل رد", "ارسل جواب", "ابعث رد", "ابعث اجابة", "ابعث اجابه",
            "اقرأ رد", "اقرا رد", "اقرأ اجابة", "اقرا اجابة", "اقرأ جواب", "اقرا جواب",
            "ماذا رد", "ماذا ردت", "ماذا قالت", "ماذا قال", "ماذا اجاب", "ماذا اجابت",
            "شو رد", "شو ردت", "ايش رد", "ايش ردت",
            "ارسلها", "أرسلها", "ارسلته", "أرسلته",
            "اقرأ الرسالة", "اقرا الرسالة", "اقرأ رسالة", "اقرا رسالة", "اقرأ رسائل", "اقرا رسائل",
            "اقرأ محادثة", "اقرا محادثة",
            "ماذا ارسل", "ماذا أرسل", "ماذا كتب", "ماذا بعث"
        ))
        or (any(w in clean_p for w in ("ارسل", "ابعث", "send", "forward")) and any(w in clean_p for w in ("اجابة", "إجابة", "رد", "جواب", "رسالة", "الرسالة", "صورة", "صوره", "reply", "answer", "response", "screenshot")) and any(w in clean_p for w in ("تلغرام", "التلغرام", "تيليجرام", "telegram", "telgram")))
        or ("read" in clean_p and any(w in clean_p for w in ("reply", "answer", "response", "message", "chat")))
        or (any(w in clean_p for w in ("what did", "what has")) and any(w in clean_p for w in ("reply", "send", "say", "write")))
        or ((has_wa_keyword or has_meta_ai) and any(w in clean_p for w in ("اقرأ", "اقرا", "رسائل", "read", "messages", "fetch", "check")))
    )

    # Email requests always go to the LLM (Gmail tools), never the WhatsApp reader.
    if any(w in clean_p for w in ("mail", "inbox", "gmail", "بريد", "إيميل", "ايميل", "جيميل")) and not has_wa_keyword:
        is_reply_forward_or_read = False

    if is_reply_forward_or_read:
        chat_target = ""

        # Pattern 1: "ارسل اجابة/رد/رسالة [الاسم] الى التلغرام"
        m_forward = re.search(
            r'^(?:ارسل|ابعث|حول)\s+(?:صورة|صوره|لقطة|لقطه)?\s*(?:اجابة|إجابة|رد|جواب|رسالة|الرسالة)?\s*(.+?)\s+(?:الى|إلى|لـ|ل|على)?\s*(?:التلغرام|تلغرام|تيليجرام|telegram)$',
            clean_p,
            re.IGNORECASE
        )
        if m_forward:
            chat_target = m_forward.group(1).strip()

        # Pattern 2: "اقرأ رد/اجابة/رسالة [الاسم]" or "اقرأ الرسالة التي ارسلها [الاسم]"
        if not chat_target:
            m_read_reply = re.search(
                r'^(?:اقرأ|اقرا|اعرض|وريني|جيب|شوف)\s+(?:الرسالة|الرسائل|رسائل|رسالة|رد|اجابة|إجابة|جواب)?\s*(?:التي\s+(?:ارسلها|أرسلها|ارسلته|أرسلته))?\s*(?:في\s+واتساب|على\s+واتساب|واتساب|واتس)?\s*(?:من|مع|لـ|ل)?\s*([^\:\,\n\?؟]+?)(?:\s+(?:في\s+واتساب|على\s+واتساب|واتساب|واتس))?$',
                clean_p,
                re.IGNORECASE
            )
            if m_read_reply:
                chat_target = m_read_reply.group(1).strip()

        # Pattern 3: "ماذا ارسل [الاسم] في واتساب" or "ماذا رد [الاسم]"
        if not chat_target:
            m_what = re.search(
                r'^(?:ماذا|شو|ايش)\s+(?:أرسل|ارسل|كتب|بعث|رد|ردت|قال|قالت|اجاب|اجابت)\s+(?:لي\s+)?([^\:\,\n\?؟]+?)(?:\s+(?:في\s+واتساب|على\s+واتساب|واتساب|واتس))?$',
                clean_p,
                re.IGNORECASE
            )
            if m_what:
                chat_target = m_what.group(1).strip()

        # Pattern 4: English forward: "send [name]'s reply to telegram" or "send reply of [name] to telegram"
        if not chat_target:
            m_en_fwd = re.search(
                r'^(?:send|forward)\s+(?:the\s+)?(?:reply|answer|response|message)\s+(?:of|from)?\s*(.+?)\s+(?:to\s+telegram)$',
                clean_p,
                re.IGNORECASE
            )
            if m_en_fwd:
                chat_target = m_en_fwd.group(1).strip()

        # Pattern 5: English read: "read reply from [name]" or "read whatsapp messages from [name]"
        if not chat_target:
            m_en_read = re.search(
                r'^(?:read|show|get|fetch)\s+(?:the\s+)?(?:reply|answer|response|whatsapp\s+messages?|messages?|chat)\s+(?:from|with|of)?\s*(.+?)(?:\s+(?:on\s+whatsapp|in\s+whatsapp))?$',
                clean_p,
                re.IGNORECASE
            )
            if m_en_read:
                chat_target = m_en_read.group(1).strip()

        # Pattern 6: English what: "what did [name] send/reply on whatsapp"
        if not chat_target:
            m_en_what = re.search(
                r'^what\s+did\s+(.+?)\s+(?:send|reply|write|say)(?:\s+(?:to\s+me\s+)?(?:on\s+whatsapp|in\s+whatsapp))?$',
                clean_p,
                re.IGNORECASE
            )
            if m_en_what:
                chat_target = m_en_what.group(1).strip()

        # Clean filler words from chat_target
        if chat_target:
            chat_target = chat_target.strip(' "\'').strip()
            chat_target = re.sub(r'^(?:من|مع|المدعو|صديقي|لي|رد|اجابة|إجابة|رسالة|الرسالة|from|with|of)\s+', '', chat_target).strip()
            chat_target = re.sub(r'\s+(?:في|على)\s+(?:واتساب|الواتساب|واتس)$', '', chat_target).strip()
            chat_target = re.sub(r'\s+(?:on|in)\s+whatsapp$', '', chat_target, flags=re.IGNORECASE).strip()

        if not chat_target and has_meta_ai:
            chat_target = "Meta AI"
        elif chat_target and chat_target.lower() in ("meta ai", "metaai", "ميتا ai", "ميتا اي", "ميتا"):
            chat_target = "Meta AI"
        elif chat_target in ("واتساب", "واتس", "whatsapp", "الرسائل", "رسائل", "الرسالة", "رسالة", "messages", "message"):
            chat_target = ""

        confirm_msg = (
            f"✅ جاري استخراج رد '{chat_target}' وإرساله إلى تيليجرام..."
            if ("تلغرام" in clean_p or "telegram" in clean_p)
            else (f"✅ جاري قراءة رد '{chat_target}'..." if chat_target else "✅ جاري قراءة أحدث رسائل واتساب...")
        )
        if not is_arabic:
            confirm_msg = (
                f"✅ Fetching '{chat_target}' reply and delivering to Telegram..."
                if ("telegram" in clean_p or "telgram" in clean_p)
                else (f"✅ Reading '{chat_target}' reply..." if chat_target else "✅ Reading latest WhatsApp messages...")
            )

        want_screenshot = any(w in clean_p for w in ("صورة", "صوره", "لقطة", "لقطه", "screenshot", "image", "photo"))
        return {
            "tool_name": "whatsapp_read",
            "tool_input": {"contact_or_chat": chat_target, "count": 10, "send_to_telegram": True, "send_screenshot": want_screenshot},
            "confirmation": confirm_msg
        }

    elif has_wa_keyword:
        ar_wa_pat = re.compile(
            r'^(?:ارسل|ابعث|سوي|اكتب)?\s*(?:رسالة\s+)?(?:على\s+)?(?:في\s+)?(?:واتساب|الواتساب|واتس|الواتس)\s*(?:الى|إلى|لـ|ل)?\s*([^\:\,\n]+?)(?:\s*(?:\:|\-|\،|\,)\s*|\s+(?:نصها|رسالة|وقل له|وقل لها|يقول|نصها هو)\s+|\s+)(.+)$',
            re.IGNORECASE
        )
        en_wa_pat = re.compile(
            r'^(?:send\s+)?(?:a\s+)?whatsapp(?:\s+message)?\s+(?:to\s+)?([^\:\,\n]+?)(?:\s*(?:\:|\-|\,)\s*|\s+(?:saying|text|message)\s+|\s+)(.+)$',
            re.IGNORECASE
        )
        m_wa_ar = ar_wa_pat.match(clean_p)
        m_wa_en = en_wa_pat.match(clean_p)

        if m_wa_ar:
            rec = m_wa_ar.group(1).strip().strip('"\'')
            msg_text = m_wa_ar.group(2).strip().strip('"\'')
            if rec and msg_text:
                return {
                    "tool_name": "whatsapp_send",
                    "tool_input": {"recipient": rec, "message": msg_text, "auto_send": True},
                    "confirmation": f"✅ تم إرسال رسالة واتساب إلى '{rec}' بنجاح."
                }
        elif m_wa_en:
            rec = m_wa_en.group(1).strip().strip('"\'')
            msg_text = m_wa_en.group(2).strip().strip('"\'')
            if rec and msg_text:
                return {
                    "tool_name": "whatsapp_send",
                    "tool_input": {"recipient": rec, "message": msg_text, "auto_send": True},
                    "confirmation": f"✅ Successfully sent WhatsApp message to '{rec}'."
                }

    # 11. Clipboard Management Intent
    if any(w in clean_p for w in ("سجل الحافظة", "تاريخ الحافظة", "سجل الحافظه", "clipboard history", "show clipboard history")):
        return {
            "tool_name": "clipboard_manager",
            "tool_input": {"action": "history"},
            "confirmation": "📋 جاري استرجاع سجل الحافظة..."
        }

    if any(w in clean_p for w in ("امسح الحافظة", "امسح الحافظه", "تفريغ الحافظة", "تفريغ الحافظه", "مسح الحافظة", "مسح الحافظه", "clear clipboard", "empty clipboard")):
        return {
            "tool_name": "clipboard_manager",
            "tool_input": {"action": "clear"},
            "confirmation": "✅ تم تفريغ الحافظة بنجاح."
        }

    if any(w in clean_p for w in ("النص المنسوخ", "محتوى الحافظة", "محتوى الحافظه", "ما في الحافظة", "ما في الحافظه", "اقرأ الحافظة", "اقرا الحافظة", "اقرأ الحافظه", "read clipboard", "get clipboard", "what is on my clipboard", "clipboard content")):
        return {
            "tool_name": "clipboard_manager",
            "tool_input": {"action": "get"},
            "confirmation": "📋 جاري قراءة محتوى الحافظة..."
        }

    if any(w in clean_p for w in ("انسخ المحدد", "انسخ النص المحدد", "نسخ المحدد", "copy selected", "copy selected text")):
        return {
            "tool_name": "clipboard_manager",
            "tool_input": {"action": "copy_selected"},
            "confirmation": "✅ جاري نسخ النص المحدد..."
        }

    if clean_p in ("الصق", "لصق", "الصق النص", "لصق النص", "paste", "paste text"):
        return {
            "tool_name": "clipboard_manager",
            "tool_input": {"action": "paste"},
            "confirmation": "✅ تم لصق النص في النافذة النشطة."
        }

    copy_text_match = re.match(r'^(?:انسخ|نسخ|ضع في الحافظة|ضع في الحافظه)\s+(?:هذا\s+)?(?:النص\s*)?(?:\:|\-|\s)\s*(.+)$', clean_p, re.IGNORECASE)
    if copy_text_match:
        text_to_copy = copy_text_match.group(1).strip()
        if text_to_copy:
            return {
                "tool_name": "clipboard_manager",
                "tool_input": {"action": "set", "text": text_to_copy},
                "confirmation": "✅ تم نسخ النص إلى الحافظة بنجاح."
            }

    return None


# ---------------------------------------------------------------------------
# Table-driven routes for simple single-step commands (EN/AR), consulted after the legacy chain.
# Patterns match the WHOLE normalized utterance. Gated tools (app_close, power_control) are still
# gated by their handlers, so the executor's requires_permission path (S3/S4) applies unchanged.
# A route builder returns the intent dict, or None to decline (the request then goes to the LLM).
# ---------------------------------------------------------------------------
_AR_RANGE = re.compile("[؀-ۿ]")
_WEEKDAYS_AR = ("الاثنين", "الثلاثاء", "الأربعاء", "الخميس", "الجمعة", "السبت", "الأحد")
_MONTHS_AR = ("يناير", "فبراير", "مارس", "أبريل", "مايو", "يونيو", "يوليو", "أغسطس", "سبتمبر", "أكتوبر", "نوفمبر", "ديسمبر")
_DIRS = {
    "desktop": "desktop", "documents": "~/Documents", "downloads": "~/Downloads", "pictures": "~/Pictures",
    "music": "~/Music", "videos": "~/Videos", "home": "~",
    "سطح المكتب": "desktop", "المستندات": "~/Documents", "التنزيلات": "~/Downloads",
    "التحميلات": "~/Downloads", "الصور": "~/Pictures", "الموسيقي": "~/Music", "الفيديوهات": "~/Videos",
}
_DIR_RE = "|".join(sorted(_DIRS, key=len, reverse=True))
_AR_APPS = {"كروم": "chrome", "بريف": "brave", "ايدج": "msedge", "فايرفوكس": "firefox", "نوتباد": "notepad",
            "المفكره": "notepad", "الحاسبه": "calculator", "واتساب": "whatsapp", "سبوتيفاي": "spotify",
            "الكاميرا": "camera", "الساعه": "clock", "الخرائط": "maps"}
_APP_STOP = {"it", "this", "that", "file", "folder", "url", "site", "website", "link", "a", "an", "my", "me",
             "youtube", "music", "video", "song", "door", "tab", "window"}


def _norm(prompt: str) -> str:
    t = re.sub(r"[\.\,\!\?؟]+$", "", prompt.strip().lower()).strip()
    t = re.sub(r"[ً-ٰٟ]", "", t)
    t = re.sub(r"[إأآ]", "ا", t).replace("ة", "ه").replace("ى", "ي")
    return re.sub(r"\s+", " ", t)


def _fmt_time(now: time.struct_time) -> str:
    return time.strftime("%I:%M %p", now).lstrip("0")


def _r_time(m, ar):
    now = time.localtime()
    msg = f"الساعة الآن {time.strftime('%H:%M', now)}." if ar else f"It's {_fmt_time(now)}."
    return {"tool_name": None, "tool_input": {}, "confirmation": msg}


def _r_date(m, ar):
    now = time.localtime()
    if ar:
        msg = f"اليوم {_WEEKDAYS_AR[now.tm_wday]}، {now.tm_mday} {_MONTHS_AR[now.tm_mon - 1]} {now.tm_year}."
    else:
        msg = "Today is " + time.strftime("%A, %B %d, %Y", now).replace(" 0", " ") + "."
    return {"tool_name": None, "tool_input": {}, "confirmation": msg}


def _g(m, key: str) -> Optional[str]:
    """Group ``key`` from the English or the Arabic (``key2``) alternative of a route pattern."""
    d = m.groupdict()
    return d.get(key) or d.get(key + "2")


def _app_name(raw: str) -> Optional[str]:
    raw = raw.strip()
    if not raw or len(raw.split()) > 3 or re.search(r"[\\/:.]", raw) or _APP_STOP & set(raw.split()):
        return None
    if _AR_RANGE.search(raw):
        return _AR_APPS.get(raw)
    return raw


# App Paths apps that startfile resolves but shutil.which cannot see.
_KNOWN_APPS = {"chrome", "firefox", "brave", "msedge", "edge", "word", "excel", "powerpoint", "outlook",
               "teams", "discord", "steam", "explorer", "notepad", "paint", "terminal"}


def _is_known_app(name: str) -> bool:
    """True only for names the app_open tool can resolve; anything else (him, Clipboard, 'x in vscode') goes to the LLM."""
    from src.agent.tools import APP_NAME_ALIASES, APP_URI_MAP  # lazy: tools imports heavy modules
    n = name.strip().lower()
    return n in _KNOWN_APPS or n in APP_NAME_ALIASES or n in APP_URI_MAP or bool(shutil.which(n))


def _r_app(tool: str, ok_en: str, ok_ar: str):
    def build(m, ar):
        app = _app_name(_g(m, "app") or "")
        if not app:
            return None
        if tool in ("app_open", "app_close") and not _is_known_app(app):
            return None
        # "close new desktop / window x / tab" are not app names; let the LLM handle them.
        if tool == "app_close" and re.match(r"^(?:(?:new|virtual|this|current|my|the|ال)\s+)*(?:desktops?|windows?|tabs?|سطح المكتب|نافذه|نافذة|تبويب)\b", app, re.I):
            return None
        return {"tool_name": tool, "tool_input": {"app_name": app},
                "confirmation": (ok_ar if ar else ok_en).format(app=app)}
    return build


def _r_file_search(m, ar):
    q = (_g(m, "q") or "").strip().strip("\"'")
    if not q:
        return None
    d = _DIRS.get(_g(m, "d") or "")
    args = {"query": q, **({"directory": d} if d else {})}
    return {"tool_name": "file_search", "tool_input": args, "confirmation": "",
            "render": lambda o: _render_files(o, ar, q)}


def _r_file_list(m, ar):
    d = _DIRS[_g(m, "d")]
    return {"tool_name": "file_list", "tool_input": {"directory": d}, "confirmation": "",
            "render": lambda o: _render_dir(o, ar)}


def _r_media(action: str, en: str, ar_msg: str):
    return lambda m, ar: {"tool_name": "media_control", "tool_input": {"action": action},
                          "confirmation": ar_msg if ar else en}


def _r_play_pause(m, ar):
    action = "stop" if m.string.startswith(("stop", "وقف", "اوقف", "ايقاف")) else "play_pause"
    return {"tool_name": "media_control", "tool_input": {"action": action}, "confirmation": "✅ تم." if ar else "✅ Done."}


def _r_now_playing(m, ar):
    return {"tool_name": "media_control", "tool_input": {"action": "now_playing"}, "confirmation": ""}


def _r_power(action: str, en: str, ar_msg: str):
    return lambda m, ar: {"tool_name": "power_control", "tool_input": {"action": action},
                          "confirmation": ar_msg if ar else en}


def _r_sysinfo(m, ar):
    return {"tool_name": "system_info", "tool_input": {}, "confirmation": "",
            "render": lambda o: _render_sysinfo(o, ar)}


def _r_listing(tool: str, key: str, empty_en: str, empty_ar: str, line):
    def build(m, ar):
        return {"tool_name": tool, "tool_input": {}, "confirmation": "",
                "render": lambda o: "\n".join(line(i) for i in o.get(key, [])) or (empty_ar if ar else empty_en)}
    return build


def _r_note_read(m, ar):
    return {"tool_name": "note_read", "tool_input": {"title_or_id": (_g(m, "t") or "").strip()}, "confirmation": "",
            "render": lambda o: f"{o['note'].get('title', '')}\n{o['note'].get('content', '')}".strip()}


def _r_recall(m, ar):
    from src.agent import memory_store
    q = (_g(m, "q") or "").strip()
    found = [x for x in memory_store.get_store().search(q, k=5, max_chars=900) if x["kind"] != "outcome"]
    if not found:
        msg = f"لا أتذكر شيئا عن {q}." if ar else f"I don't remember anything about {q}."
    else:
        msg = ("أتذكر:\n" if ar else "I remember:\n") + "\n".join(f"- {x['text']}" for x in found)
    return {"tool_name": None, "tool_input": {}, "confirmation": msg}  # lookup happens here: pure local answer


def _r_forget_topic(m, ar):
    from src.agent import memory_store
    q = (_g(m, "q") or "").strip()
    n = memory_store.get_store().delete_matching(q)
    msg = (f"تم حذف {n} من الذكريات." if ar else f"Forgot {n} memor{'y' if n == 1 else 'ies'}.") if n else (
        f"لم أجد شيئا عن {q}." if ar else f"I had nothing saved about {q}.")
    return {"tool_name": None, "tool_input": {}, "confirmation": msg}


def _render_files(o, ar, q):
    files = o.get("files") or []
    if not files:
        return f"لم أجد ملفات تطابق '{q}'." if ar else f"No files matching '{q}'."
    head = f"وجدت {len(files)} ملف:" if ar else f"Found {len(files)} file(s):"
    return head + "\n" + "\n".join(files)


def _render_dir(o, ar):
    entries = o.get("entries") or []
    if not entries:
        return "المجلد فارغ." if ar else "The folder is empty."
    names = [e["name"] + ("/" if e["type"] == "folder" else "") for e in entries]
    return (f"{len(names)} عنصر:" if ar else f"{len(names)} item(s):") + "\n" + "\n".join(names)


def _render_sysinfo(o, ar):
    cpu, ram, disk = o.get("cpu_percent"), o.get("memory_percent"), o.get("disk_percent")
    free = o.get("memory_available_gb", 0)
    if ar:
        return f"المعالج {cpu}%، الذاكرة {ram}% (المتاح {free:.1f} جيجا)، القرص {disk}%."
    return f"CPU {cpu}%, RAM {ram}% ({free:.1f} GB free), disk {disk}% used."


_LEAD = r"(?:(?:show|list|read|get|give) (?:me )?|what(?:'s| is| are) (?:on )?)?"
_AR_LEAD = r"(?:(?:اعرض|اقرا|وريني|اظهر|اسرد) (?:لي )?|ما هي |ايش |شو )?"
_OPEN_EN = r"^(?:open|launch|start|run) (?:the )?(?P<app>[^\s].*?)(?: app| application| program)?$"
_CLOSE_EN = r"^(?:close|quit|exit|kill|terminate) (?:the )?(?P<app>[^\s].*?)(?: app| application| program)?$"

# (regex over the normalized utterance, builder) - first match wins; order matters.
ROUTES = [
    (r"^(?:what(?:'s| is)? (?:the )?(?:current )?time(?: is it)?(?: now| right now)?|what time is it(?: now| right now)?|current time|tell me the time|time(?: now)?|كم الساعه(?: الان)?|الساعه كم(?: الان)?|الساعه(?: الان)?|كم الوقت(?: الان)?|ما(?: هو)? الوقت(?: الان)?|الوقت(?: الان)?)$", _r_time),
    (r"^(?:what(?:'s| is)? (?:the |today'?s )?date(?: today)?|what day is (?:it|today)|what(?:'s| is) today|today'?s date|date(?: today)?|ما(?: هو)? تاريخ اليوم|تاريخ اليوم|اي يوم اليوم|ما اليوم|ايش تاريخ اليوم)$", _r_date),
    (r"^(?:lock(?: my| the)?(?: pc| computer| screen| laptop| workstation)?|(?:اقفل|قفل|اغلق) (?:ال)?(?:شاشه|جهاز|كمبيوتر|حاسوب|لابتوب)(?:ي|ك)?)$", _r_power("lock", "Locking the PC.", "تم قفل الجهاز.")),
    (r"^(?:go to sleep|sleep|sleep mode|put (?:the |my )?(?:pc|computer|laptop) (?:to )?sleep|sleep (?:pc|computer|laptop)|نيم (?:ال)?(?:جهاز|كمبيوتر|حاسوب)|وضع النوم|نوم)$", _r_power("sleep", "Putting the PC to sleep.", "جاري وضع الجهاز في وضع النوم.")),
    (r"^(?:what(?:'s| is) (?:playing|this song)(?: now)?|now playing|ايش (?:الاغنيه )?(?:الشغاله|اللي شغاله)|ما الاغنيه الحاليه)$", _r_now_playing),
    (r"^(?:pause|stop)(?: the)?(?: music| song| media| video| playback)?$|^(?:play|resume)(?: the)?(?: music| song| media| video| playback)$|^(?:resume|continue)$|^(?:وقف|اوقف|ايقاف|بوز|كمل|استئناف|شغل)(?: ال)?(?:موسيقي|اغنيه|ميديا|فيديو)$", _r_play_pause),
    (r"^(?:next|skip)(?: the)?(?: song| track| music)?$|^(?:التالي|التاليه|الاغنيه التاليه|الاغنيه الجايه|تخطي)$", _r_media("next", "✅ Next track.", "✅ الأغنية التالية.")),
    (r"^(?:previous|prev|last)(?: the)?(?: song| track)?$|^(?:السابق|السابقه|الاغنيه السابقه)$", _r_media("previous", "✅ Previous track.", "✅ الأغنية السابقة.")),
    (r"^(?:(?:system|pc|computer) (?:info|information|specs|status|stats)|(?:show |get |give me )?(?:my )?(?:system|pc|computer) (?:info|information|specs|status|stats)|how(?:'s| is) my (?:pc|computer|system)(?: doing)?|(?:cpu|ram|memory|disk)(?: usage| status)?|(?:معلومات|حاله|مواصفات) (?:ال)?(?:نظام|جهاز|كمبيوتر|حاسوب)|استهلاك (?:ال)?(?:رام|معالج|ذاكره))$", _r_sysinfo),
    (rf"^{_LEAD}(?:my |the )?(?:to-?do(?:s)?(?: list)?|tasks(?: list)?)$|^{_AR_LEAD}(?:قائمه |قايمه )?(?:المهام|مهامي|الواجبات)$", _r_listing("todo_list", "todos", "Nothing on the to-do list.", "لا توجد مهام.", lambda i: f"- {i.get('task', '')}" + (f" (due {i['due']})" if i.get("due") else ""))),
    (rf"^{_LEAD}(?:my |the |all )?(?:saved )?notes$|^{_AR_LEAD}(?:ملاحظاتي|الملاحظات)$", _r_listing("note_list", "notes", "No notes saved.", "لا توجد ملاحظات.", lambda i: f"- {i.get('title', '')}")),
    (r"^(?:read|open|show) (?:my )?note (?:called |named |titled )?(?P<t>.+)$|^(?:اقرا|افتح|اعرض) (?:لي )?ملاحظه (?P<t2>.+)$", _r_note_read),
    (rf"^{_LEAD}(?:my |the |all )?(?:pending |upcoming )?reminders$|^{_AR_LEAD}(?:تذكيراتي|التذكيرات)$", _r_listing("list_reminders", "reminders", "No pending reminders.", "لا توجد تذكيرات.", lambda i: f"- {i.get('message', '')} ({i.get('due', '')})")),
    (rf"^(?:find|search for|search|look for|locate) (?:my |the |a )?files? (?:named |called |with )?(?P<q>[^\s].*?)(?: (?:in|on|under) (?:my |the )?(?P<d>{_DIR_RE}))?$|^(?:ابحث عن|دور على|جد) (?:ال)?ملف(?:ات)? (?P<q2>[^\s].*?)(?: (?:في|على) (?P<d2>{_DIR_RE}))?$", _r_file_search),
    (rf"^(?:(?:list|show|ls|open) (?:me )?(?:the )?(?:files |folders |contents |items )?(?:in |on |of )?|what(?:'s| is| are) (?:the )?(?:files )?(?:in|on) )(?:my |the )?(?P<d>{_DIR_RE})(?: folder| directory)?$|^(?:(?:اعرض|اظهر|وريني|اسرد) (?:لي )?(?:ملفات |محتويات |محتوي )|(?:ماذا|شو|ايش) (?:يوجد )?(?:في|على) )(?P<d2>{_DIR_RE})$", _r_file_list),
    (r"^what do you (?:remember|know) about (?P<q>.+)$|^ماذا تتذكر عن (?P<q2>.+)$", _r_recall),
    (r"^forget (?:about |that )?(?P<q>.+)$|^(?:انس|انسي) (?:ما يخص |عن )?(?P<q2>.+)$", _r_forget_topic),
    (_CLOSE_EN + r"|^(?:اغلق|سكر|اقفل) (?:تطبيق |برنامج )?(?P<app2>.+)$", _r_app("app_close", "Closing {app}.", "جاري إغلاق {app}.")),
    (_OPEN_EN + r"|^(?:افتح|فتح|شغل (?:تطبيق|برنامج)) (?:تطبيق |برنامج )?(?P<app2>.+)$", _r_app("app_open", "✅ Opened {app}.", "✅ تم تشغيل {app}.")),
]

_COMPILED = [(re.compile(rx), build) for rx, build in ROUTES]


def match_local_intent(prompt: str) -> Optional[Dict[str, Any]]:
    """Legacy rule chain first, then the ROUTES table. Returns {tool_name, tool_input, confirmation[, render]}.

    ``tool_name`` None means a pure local answer (time/date) that needs no tool at all.
    """
    if not prompt:
        return None
    intent = _match_legacy_intent(prompt)
    if intent:
        return intent
    text = _norm(prompt)
    ar = bool(_AR_RANGE.search(text))
    for rx, build in _COMPILED:
        m = rx.match(text)
        if m:
            intent = build(m, ar)
            if intent:
                return intent
    return None
