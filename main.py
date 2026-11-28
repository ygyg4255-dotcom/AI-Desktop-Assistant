"""Unified Altron Entry Point.

Launches and coordinates all Altron subsystems concurrently with full fault isolation:
- HUD Web Interface Server (Flask on 127.0.0.1, port 5000 or 5001 if busy)
- Continuous Voice Listener Loop (FastVAD + faster-whisper + TTS playback)
- Telegram Remote Control Bot Listener
"""
import json
import os
import sys
import time
import threading
from pathlib import Path

# Ensure project root is on sys.path
PROJECT_ROOT = Path(__file__).resolve().parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.config import Settings
from src.config.log import get_logger, setup_logging
from src.config.settings import get_config_value
from src.storage.db import Database
from src.services.ai_client import AIClient
from src.services.model_router import ModelRouter
from src.services.omniroute_manager import OmniRouteManager
from src.agent.executor import AgentExecutor
from src.agent.control_intents import ControlHandler
from src.agent.approvals import (
    ApprovalManager,
    CHANNEL_HUD,
    CHANNEL_TELEGRAM,
    CHANNEL_VOICE,
)
from src.services.hud_status import init_hud_status, push_hud_toast, update_hud_status
from src.services.voice_service import (
    TTSManager,
    SpeechRecognitionListener,
    is_speaking_event,
)
from src.services.wake_gate import ACTION_ACK, ACTION_IGNORE, WakeGate
from src.services.hotkey import GlobalHotkey
from src.services.telegram_service import (
    TelegramApprovalBridge,
    TelegramBotApi,
    TelegramRemoteListener,
    TypingIndicator,
    send_telegram_message_async,
)
from src.services.camera_service import get_camera_service
from src.agent.tools import run_scheduled_action
from src.services.reminder_service import format_reminder_speech, get_reminder_service
from src.services.scheduler import get_scheduler
from src.services.alarm_chime import play_alarm_chime
from src.services.desktop_notify import show_desktop_notification
from ui_server import start_hud_server, stop_hud_server

logger = get_logger("main")


def _log_step(step) -> None:
    """Internal execution steps go to the logger only, never the HUD chat feed."""
    content = getattr(step, "content", None)
    if content:
        logger.info("Step: %s", content)


def apply_ai_config(ai_client, cfg: dict) -> None:
    """Hot-reload model/key. The HUD payload only carries a masked key, so the
    real key is always loaded from Settings/.env, never taken from cfg."""
    s = Settings()  # always the Anthropic entries: cfg["model"] belongs to the active provider
    logger.info("Hot-reloading Anthropic model: %s", s.ai_model)
    ai_client.update_config(model=s.ai_model, api_key=s.ai_api_key or None)


def is_duplicate_approval_reply(res, bridge) -> bool:
    """True when a Telegram-origin 'awaiting approval' reply repeats the card the bridge already sent."""
    return bridge is not None and getattr(res, "error", None) == "awaiting approval"


def make_approval_listener(bridge_ref: list, notify_telegram=send_telegram_message_async):
    """Deliver approval events to the HUD log and (when configured) Telegram.

    bridge_ref[0] is the TelegramApprovalBridge, or None if Telegram is disabled.
    """
    def _listener(event: str, req) -> None:
        bridge = bridge_ref[0]
        if event == "created":
            update_hud_status(log_entry=f"Approval required: {req.tool_name} — {req.summary} (approve in the HUD panel)")
            # telegram origin: inline keyboard; voice origin: HUD + Telegram if configured.
            if bridge is not None and req.channel in (CHANNEL_TELEGRAM, CHANNEL_VOICE):
                bridge.send_request(req)
        elif event == "resolved":
            update_hud_status(log_entry=f"Approval {req.status}: {req.tool_name}")
            if bridge is not None:
                bridge.on_resolved(req)
        elif event == "executed":
            res = req.result
            if res is not None and getattr(res, "success", False):
                out = getattr(res, "output", None)
                detail = (out.get("message") if isinstance(out, dict) else None) or "done"
                text = f"✅ {req.tool_name}: {detail}"
                if not (isinstance(out, dict) and out.get("message")) and out:
                    # Telegram caps messages at 4096 chars; the output is already redacted-at-source.
                    text = f"✅ {req.tool_name}:\n{out if isinstance(out, str) else json.dumps(out, ensure_ascii=False, default=str)}"[:4000]
            else:
                err = getattr(res, "error", None) or req.meta.get("run_error") or "failed"
                text = f"❌ {req.tool_name} failed: {err}"
            update_hud_status(log_entry=f"Altron: {text}")
            if req.channel == CHANNEL_TELEGRAM and bridge is not None:
                notify_telegram(text)
    return _listener


def telegram_tts_enabled() -> bool:
    """Speak Telegram-origin replies locally only when ALTRON_TELEGRAM_TTS is truthy (default off)."""
    return get_config_value("ALTRON_TELEGRAM_TTS", default="false").lower() in ("1", "true", "yes", "on")


def _run_guarded(label: str, fn, *args) -> None:
    """Run one alarm step; a failure is logged and never stops the remaining steps."""
    try:
        fn(*args)
    except Exception:
        logger.exception("Reminder %s failed", label)


def make_reminder_handler(tts, notify_telegram=send_telegram_message_async,
                          chime=play_alarm_chime, notify_desktop=show_desktop_notification,
                          run_async: bool = True):
    """Callback for ReminderService: act like a PC alarm.

    On fire: log to the HUD chat and send the Telegram alert (both synchronous and cheap), then
    on a background thread show a native desktop notification, play the alarm chime and speak
    the reminder. The reminder scheduler is never blocked by audio or the toast.

    Mute (``tts.is_muted`` or TTS disabled) silences the chime and the speech; the HUD log says
    so. The desktop notification is still shown, since it is visual. Telegram is skipped
    silently when it is not configured.
    """
    def _in_background(label: str, fn, *args) -> None:
        if run_async:
            threading.Thread(target=_run_guarded, args=(label, fn, *args), daemon=True,
                             name=f"AltronReminder-{label}").start()
        else:
            _run_guarded(label, fn, *args)

    def _alarm(rem, text: str, silenced: bool) -> None:
        # The toast blocks for its display time, so it runs on its own thread while the chime plays.
        _in_background("notification", notify_desktop, "Altron reminder", rem.message)
        if silenced:
            return
        _run_guarded("chime", chime)  # blocks for the chime's length, then speech follows it
        _run_guarded("speech", tts.speak, text)

    def _fire(rem) -> None:
        text = format_reminder_speech(rem)
        silenced = (not tts.is_enabled) or tts.is_muted
        logger.info("Reminder fired: %s", text)
        log_text = f"Altron: {text}"
        if silenced:
            log_text += " (muted: alarm sound and voice skipped)"
        update_hud_status(log_entry=log_text)
        notify_telegram(f"⏰ {text}")
        _in_background("alarm", _alarm, rem, text, silenced)
    return _fire


def main() -> None:
    """Coordinate and supervise all Altron subsystems concurrently."""
    # Per-monitor DPI awareness before any screen metrics are read, so GUI clicks and screenshots
    # share physical pixel coordinates on scaled displays.
    from src.agent.screen_geometry import ensure_dpi_aware
    ensure_dpi_aware()
    log_path = setup_logging()
    logger.info("Starting Altron (log file: %s)", log_path)

    # Initialize shared HUD status
    init_hud_status()

    # Load configuration & database
    settings = Settings()
    database = Database(settings.db_path)
    try:
        database.initialize()
    except Exception:
        logger.exception("Database initialisation failed")

    # Auto-start OmniRoute proxy only if configured for local OmniRoute port
    try:
        auto_start_omniroute = database.get_setting("auto_start_omniroute", True)
        if auto_start_omniroute and ("20128" in settings.ai_base_url or "localhost" in settings.ai_base_url):
            omniroute_manager = OmniRouteManager()
            omniroute_manager.start_if_needed(settings.ai_base_url)
    except Exception as omni_err:
        logger.warning("OmniRoute auto-start skipped: %s", omni_err)

    # Initialize Universal AI client and Agent Executor
    ai_client = AIClient(
        base_url=settings.ai_base_url,
        api_key=settings.ai_api_key,
        model=settings.ai_model,
        provider=settings.ai_provider,
        max_tokens=settings.max_tokens,
        timeout=settings.request_timeout,
    )
    # Out-of-band approvals: gated tool calls never block; they wait for the
    # owner's decision in the HUD panel or a Telegram inline keyboard.
    approvals = ApprovalManager()
    router = ModelRouter(ai_client)
    executor = AgentExecutor(ai_client=router, database=database, approvals=approvals)
    tg_bridge = [None]
    approvals.add_listener(make_approval_listener(tg_bridge))

    def _on_resume_complete(res, origin):
        update_hud_status(state="Idle", log_entry=f"Altron: {res.content}")
        if origin == CHANNEL_TELEGRAM:
            send_telegram_message_async(res.content)
    executor.on_resume_complete = _on_resume_complete
    approvals.start_sweeper(interval=5.0)
    tts_manager = TTSManager(enabled=True)

    reminders = get_reminder_service()
    reminders.set_callback(make_reminder_handler(tts_manager))
    reminders.start()  # overdue reminders (missed while off) fire right away

    scheduler = get_scheduler()
    scheduler.set_callbacks(
        on_notify=lambda title, text: (push_hud_toast(text), send_telegram_message_async(f"⏰ {text}")),
        run_action=lambda name, args, approved: run_scheduled_action(executor.registry, name, args, approved))
    scheduler.start()  # silent: HUD toast + optional Telegram only, never TTS

    camera_service = get_camera_service()
    shutdown_event = threading.Event()
    active_voice_worker = [None]
    active_tg_worker = [None]

    def _pause_mic() -> None:
        worker = active_voice_worker[0]
        if worker is not None:
            worker.pause()

    def _resume_mic() -> None:
        worker = active_voice_worker[0]
        if worker is not None:
            worker.resume()

    def _request_shutdown() -> None:
        """Let the goodbye finish speaking (max ~5s), then end the supervision loop."""
        def _later():
            time.sleep(0.4)  # the reply is queued right after this returns
            deadline = time.monotonic() + 5.0
            while is_speaking_event.is_set() and time.monotonic() < deadline:
                time.sleep(0.1)
            shutdown_event.set()
        threading.Thread(target=_later, name="AltronShutdown", daemon=True).start()

    control = ControlHandler(
        clear_memory=executor.clear_history,
        request_shutdown=_request_shutdown,
        pause_listening=_pause_mic,
        resume_listening=_resume_mic,
        start_camera=camera_service.start,
        stop_camera=camera_service.stop,
        revoke_trust=lambda: executor.approvals and executor.approvals.revoke_trust(),
    )

    if settings.camera_autostart:
        try:
            if not camera_service.start():
                logger.warning("Camera autostart skipped: %s", camera_service.last_error)
        except Exception as err:
            logger.warning("Camera autostart failed: %s", err)

    wake_gate = WakeGate(
        require_wake_word=get_config_value("ALTRON_REQUIRE_WAKE_WORD", default="true").lower()
        not in ("0", "false", "no", "off")
    )

    def _on_hotkey() -> None:
        """Alt+R: listen for the next utterance directly, without the wake word."""
        if control.muted:
            logger.info("Alt+R ignored: microphone is muted")
            return
        wake_gate.arm_direct()
        tts_manager.stop()
        update_hud_status(state="Listening")
        logger.info("Alt+R pressed: listening without wake word")

    hotkey = GlobalHotkey(_on_hotkey)
    hotkey.start()

    def _reply_control(reply: str, speak: bool = True) -> None:
        logger.info("Altron: %s", reply)
        update_hud_status(state="Idle", log_entry=f"Altron: {reply}")
        send_telegram_message_async(reply)
        if speak and tts_manager.is_enabled and not tts_manager.is_muted:
            tts_manager.speak(reply)

    # -------------------------------------------------------------
    # 1. HUD Server Subsystem Thread
    # -------------------------------------------------------------
    def _on_ai_config_updated(cfg: dict):
        """Hot-reload the AI provider, model and API key at runtime."""
        apply_ai_config(ai_client, cfg)
        router.configure(cfg.get("provider"))

    def _on_hud_command(cmd_text: str):
        clean_cmd = cmd_text.strip()
        if not clean_cmd:
            return
        logger.info("HUD command received: %r", clean_cmd)
        tts_manager.stop()  # a new command interrupts any speech still playing
        control_reply = control.handle(clean_cmd)
        if control_reply:
            _reply_control(control_reply)
            return
        update_hud_status(state="Thinking", log_entry=f"User: {clean_cmd}")

        def _on_complete(res):
            if res and res.content:
                logger.info("Altron: %s", res.content)
                update_hud_status(state="Idle", log_entry=f"Altron: {res.content}")
                send_telegram_message_async(res.content)
                # Sentences were already queued for speech while streaming.
                if (tts_manager.is_enabled and not tts_manager.is_muted
                        and not getattr(res, "streamed_speech", False)):
                    tts_manager.speak(res.content)
            else:
                update_hud_status(state="Idle")

        executor.process(
            user_message=clean_cmd,
            on_sentence=tts_manager.enqueue,
            on_step=_log_step,
            on_complete=_on_complete,
            on_error=lambda err: (
                logger.error("HUD command failed: %s", err),
                update_hud_status(state="Idle")
            ),
            origin=CHANNEL_HUD,
        )

    def _run_hud():
        try:
            start_hud_server(
                host="127.0.0.1",
                port=5000,
                open_browser=True,
                command_handler=_on_hud_command,
                on_config_updated=_on_ai_config_updated,
                approval_manager=approvals,
            )
        except Exception:
            logger.exception("HUD server subsystem failed")

    hud_thread = threading.Thread(target=_run_hud, name="AltronHUDThread", daemon=True)
    hud_thread.start()

    # -------------------------------------------------------------
    # 2. Continuous Voice Listener Loop Thread (faster-whisper + FastVAD)
    # -------------------------------------------------------------
    def _run_voice():
        while not shutdown_event.is_set():
            try:
                worker = SpeechRecognitionListener()
                active_voice_worker[0] = worker
                if control.muted:
                    worker.pause()

                def _on_recognized(text: str):
                    if not text or not text.strip():
                        return
                    clean_text = text.strip()
                    decision = wake_gate.evaluate(clean_text)
                    if decision.action == ACTION_IGNORE:
                        logger.debug("Ignored speech without wake word: %r", clean_text)
                        update_hud_status(state="Idle")
                        return
                    if decision.action == ACTION_ACK:
                        update_hud_status(state="Listening")
                        tts_manager.speak("Yes?")
                        return
                    query = decision.query

                    listen_vad = getattr(worker, "last_listen_duration", 0.0)
                    stt_dur = getattr(worker, "last_stt_duration", 0.0)

                    logger.info("Voice recognized: %r", clean_text)
                    tts_manager.stop()
                    control_reply = control.handle(query)
                    if control_reply:
                        _reply_control(control_reply)
                        return
                    update_hud_status(state="Thinking", log_entry=f"Voice: {clean_text}")

                    t_agent_start = time.perf_counter()
                    tts_init_time = 0.0
                    agent_time = 0.0

                    try:
                        future = executor.process(
                            user_message=query,
                            on_sentence=tts_manager.enqueue,
                            on_step=_log_step,
                            origin=CHANNEL_VOICE,
                        )
                        response = future.result(timeout=120)
                        agent_time = time.perf_counter() - t_agent_start

                        if response and response.content:
                            logger.info("Altron: %s", response.content)
                            update_hud_status(log_entry=f"Altron: {response.content}")
                            send_telegram_message_async(response.content)

                            t_tts_start = time.perf_counter()
                            if not response.streamed_speech:  # else already spoken sentence by sentence
                                tts_manager.speak(response.content)
                            tts_init_time = time.perf_counter() - t_tts_start
                    except Exception as proc_err:
                        agent_time = time.perf_counter() - t_agent_start
                        logger.error("Agent execution failed for voice command: %s", proc_err)

                    total_time = listen_vad + stt_dur + agent_time + tts_init_time
                    logger.info(
                        "Voice timing: listen/VAD %.2fs | STT %.2fs | agent/LLM %.2fs | "
                        "TTS init %.2fs | total %.2fs",
                        listen_vad, stt_dur, agent_time, tts_init_time, total_time,
                    )
                    update_hud_status(state="Idle")

                # Plain-Python listener: callbacks run on this thread (no Qt event loop).
                worker.text_recognized.connect(_on_recognized)
                worker.error_occurred.connect(lambda err: logger.warning("Voice error: %s", err))
                worker.run()
                if not shutdown_event.is_set():
                    shutdown_event.wait(2)  # listener ended on its own: back off before restarting
            except Exception:
                logger.exception("Voice subsystem failed; restarting in 2s")
                time.sleep(2)

    voice_thread = threading.Thread(target=_run_voice, name="AltronVoiceThread", daemon=True)
    voice_thread.start()

    # -------------------------------------------------------------
    # 3. Telegram Bot Subsystem Thread
    # -------------------------------------------------------------
    # Settings already applies the canonical precedence (.env > environment > default).
    bot_token = settings.telegram_bot_token
    chat_id = settings.telegram_chat_id

    # Approval callbacks need BOTH the owner's chat id and user id. In a private
    # chat they are equal; set TELEGRAM_OWNER_USER_ID when using a group chat.
    owner_user_id = get_config_value("TELEGRAM_OWNER_USER_ID") or chat_id
    if bot_token and chat_id and str(bot_token).strip() and str(chat_id).strip():
        tg_bridge[0] = TelegramApprovalBridge(
            approvals, TelegramBotApi(str(bot_token).strip()),
            chat_id=str(chat_id).strip(), owner_user_id=str(owner_user_id).strip(),
        )

    def _run_telegram():
        if not bot_token or not chat_id or str(bot_token).strip() == "" or str(chat_id).strip() == "":
            logger.warning("Telegram bot token or chat ID is not configured; Telegram listener disabled")
            return

        while not shutdown_event.is_set():
            try:
                tg_worker = TelegramRemoteListener(
                    bot_token=bot_token,
                    allowed_chat_id=chat_id,
                    callback_handler=tg_bridge[0].handle_callback_query if tg_bridge[0] else None,
                    message_interceptor=tg_bridge[0].handle_message if tg_bridge[0] else None,
                )
                active_tg_worker[0] = tg_worker

                def _on_telegram_command(text: str):
                    if not text or not text.strip():
                        return
                    clean_cmd = text.strip()
                    logger.info("Telegram command received: %r", clean_cmd)
                    control_reply = control.handle(clean_cmd)
                    if control_reply:
                        _reply_control(control_reply, speak=telegram_tts_enabled())
                        return
                    update_hud_status(state="Thinking", log_entry=f"Telegram: {clean_cmd}")
                    typing = TypingIndicator(TelegramBotApi(str(bot_token).strip()), str(chat_id)).start()

                    def _on_complete(res):
                        typing.stop()
                        if res and res.content:
                            logger.info("Altron (Telegram reply): %s", res.content)
                            update_hud_status(state="Idle", log_entry=f"Altron: {res.content}")
                            if not is_duplicate_approval_reply(res, tg_bridge[0]):
                                send_telegram_message_async(res.content)
                            if telegram_tts_enabled() and tts_manager.is_enabled and not tts_manager.is_muted:
                                tts_manager.speak(res.content)

                    def _on_error(err):
                        typing.stop()
                        logger.error("Telegram command failed: %s", err)

                    executor.process(
                        user_message=clean_cmd,
                        on_complete=_on_complete,
                        on_error=_on_error,
                        origin=CHANNEL_TELEGRAM,
                    )

                tg_worker.remote_command_received.connect(_on_telegram_command)
                tg_worker.run()
                if not shutdown_event.is_set():
                    shutdown_event.wait(5)  # listener ended on its own: back off before restarting
            except Exception:
                logger.exception("Telegram subsystem failed; restarting in 5s")
                time.sleep(5)

    tg_thread = threading.Thread(target=_run_telegram, name="AltronTelegramThread", daemon=True)
    tg_thread.start()

    # -------------------------------------------------------------
    # 4. Online Readiness Banner & Main Thread Supervision Loop
    # -------------------------------------------------------------
    update_hud_status(state="Idle")
    logger.info("Altron is online.")

    try:
        while not shutdown_event.is_set():
            time.sleep(1)
    except KeyboardInterrupt:
        logger.info("Shutting down gracefully...")
    shutdown_event.set()
    try:
        hotkey.stop()
    except Exception as err:
        logger.warning("Global hotkey did not stop cleanly: %s", err)
    if active_voice_worker[0]:
        try:
            active_voice_worker[0].abort()
        except Exception as err:
            logger.warning("Voice listener did not stop cleanly: %s", err)
    if active_tg_worker[0]:
        try:
            active_tg_worker[0].stop()
        except Exception as err:
            logger.warning("Telegram listener did not stop cleanly: %s", err)
    try:
        executor.cancel()
        approvals.stop()
    except Exception as err:
        logger.warning("Executor/approvals did not stop cleanly: %s", err)
    try:
        scheduler.stop()
    except Exception as err:
        logger.warning("Scheduler did not stop cleanly: %s", err)
    try:
        reminders.stop()
    except Exception as err:
        logger.warning("Reminder service did not stop cleanly: %s", err)
    try:
        tts_manager.stop()
    except Exception as err:
        logger.warning("TTS did not stop cleanly: %s", err)
    try:
        camera_service.stop()
    except Exception as err:
        logger.warning("Camera service did not stop cleanly: %s", err)
    # Flag Offline, let open HUD tabs see it and close, then free port 5000.
    stop_hud_server(grace=2.0)
    logger.info("All subsystems stopped.")


if __name__ == "__main__":
    main()
