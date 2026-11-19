"""Unified Altron Entry Point.

Launches and coordinates all Altron subsystems concurrently with full fault isolation:
- HUD Web Interface Server (Flask on 127.0.0.1, port 5000 or 5001 if busy)
- Continuous Voice Listener Loop (FastVAD + faster-whisper + TTS playback)
- Telegram Remote Control Bot Listener
"""
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
from src.services import AIClient, OmniRouteManager
from src.agent.executor import AgentExecutor
from src.agent.control_intents import ControlHandler
from src.agent.approvals import (
    ApprovalManager,
    CHANNEL_HUD,
    CHANNEL_TELEGRAM,
    CHANNEL_VOICE,
)
from src.services.hud_status import init_hud_status, update_hud_status
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
    send_telegram_message_async,
)
from src.services.camera_service import get_camera_service
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
    model = cfg.get("model", "")
    real_key = Settings().ai_api_key or None
    logger.info("Hot-reloading AI model: %s", model)
    ai_client.update_config(model=model, api_key=real_key)


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
            else:
                err = getattr(res, "error", None) or req.meta.get("run_error") or "failed"
                text = f"❌ {req.tool_name} failed: {err}"
            update_hud_status(log_entry=f"Altron: {text}")
            if req.channel == CHANNEL_TELEGRAM and bridge is not None:
                notify_telegram(text)
    return _listener


def main() -> None:
    """Coordinate and supervise all Altron subsystems concurrently."""
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
    executor = AgentExecutor(ai_client=ai_client, database=database, approvals=approvals)
    tg_bridge = [None]
    approvals.add_listener(make_approval_listener(tg_bridge))
    approvals.start_sweeper(interval=5.0)
    tts_manager = TTSManager(enabled=True)

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
        """Hot-reload AI model and API key at runtime."""
        apply_ai_config(ai_client, cfg)

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
                )
                active_tg_worker[0] = tg_worker

                def _on_telegram_command(text: str):
                    if not text or not text.strip():
                        return
                    clean_cmd = text.strip()
                    logger.info("Telegram command received: %r", clean_cmd)
                    control_reply = control.handle(clean_cmd)
                    if control_reply:
                        _reply_control(control_reply)
                        return
                    update_hud_status(state="Thinking", log_entry=f"Telegram: {clean_cmd}")

                    def _on_complete(res):
                        if res and res.content:
                            logger.info("Altron (Telegram reply): %s", res.content)
                            update_hud_status(state="Idle", log_entry=f"Altron: {res.content}")
                            send_telegram_message_async(res.content)
                            if tts_manager.is_enabled and not tts_manager.is_muted:
                                tts_manager.speak(res.content)

                    executor.process(
                        user_message=clean_cmd,
                        on_complete=_on_complete,
                        on_error=lambda err: logger.error("Telegram command failed: %s", err),
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
