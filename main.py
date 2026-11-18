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
from src.config.settings import get_config_value
from src.storage.db import Database
from src.services import AIClient, OmniRouteManager
from src.agent.executor import AgentExecutor
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
    parse_wake_word,
)
from src.services.telegram_service import (
    TelegramApprovalBridge,
    TelegramBotApi,
    TelegramRemoteListener,
    send_telegram_message_async,
)
from ui_server import start_hud_server


def apply_ai_config(ai_client, cfg: dict) -> None:
    """Hot-reload model/key. The HUD payload only carries a masked key, so the
    real key is always loaded from Settings/.env, never taken from cfg."""
    model = cfg.get("model", "")
    real_key = Settings().ai_api_key or None
    print(f"[AI Config] Hot-reloading AI model: {model}")
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
    # Initialize shared HUD status
    init_hud_status()

    # Load configuration & database
    settings = Settings()
    database = Database(settings.db_path)
    try:
        database.initialize()
    except Exception as db_err:
        print(f"[Database Error] {db_err}")

    # Auto-start OmniRoute proxy only if configured for local OmniRoute port
    try:
        auto_start_omniroute = database.get_setting("auto_start_omniroute", True)
        if auto_start_omniroute and ("20128" in settings.ai_base_url or "localhost" in settings.ai_base_url):
            omniroute_manager = OmniRouteManager()
            omniroute_manager.start_if_needed(settings.ai_base_url)
    except Exception as omni_err:
        print(f"[OmniRoute Warning] {omni_err}")

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

    shutdown_event = threading.Event()
    active_voice_worker = [None]
    active_tg_worker = [None]

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
        print(f"[HUD Command] Received: \"{clean_cmd}\"")
        update_hud_status(state="Thinking", log_entry=f"User: {clean_cmd}")

        def _on_complete(res):
            if res and res.content:
                print(f"[Altron] {res.content}")
                update_hud_status(state="Idle", log_entry=f"Altron: {res.content}")
                send_telegram_message_async(res.content)
                if tts_manager.is_enabled and not tts_manager.is_muted:
                    tts_manager.speak(res.content)
            else:
                update_hud_status(state="Idle")

        executor.process(
            user_message=clean_cmd,
            on_step=lambda step: update_hud_status(
                log_entry=f"Step: {step.content}"
            ) if getattr(step, "content", None) else None,
            on_complete=_on_complete,
            on_error=lambda err: (
                print(f"[HUD Command Error] {err}"),
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
        except Exception as hud_err:
            print(f"[HUD Server Subsystem Error] {hud_err}")

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

                def _on_recognized(text: str):
                    if not text or not text.strip():
                        return
                    clean_text = text.strip()
                    has_wake_word, remainder = parse_wake_word(clean_text)
                    if has_wake_word and remainder:
                        query = remainder
                    elif has_wake_word and not remainder:
                        tts_manager.speak("Yes, sir? How may I assist you?")
                        return
                    else:
                        query = clean_text

                    listen_vad = getattr(worker, "last_listen_duration", 0.0)
                    stt_dur = getattr(worker, "last_stt_duration", 0.0)

                    print(f"[Voice] Recognized: \"{clean_text}\"")
                    update_hud_status(state="Thinking", log_entry=f"Voice: {clean_text}")

                    t_agent_start = time.perf_counter()
                    tts_init_time = 0.0
                    agent_time = 0.0

                    try:
                        future = executor.process(
                            user_message=query,
                            on_step=lambda step: update_hud_status(
                                log_entry=f"Step: {step.content}"
                            ) if getattr(step, "content", None) else None,
                            origin=CHANNEL_VOICE,
                        )
                        response = future.result(timeout=120)
                        agent_time = time.perf_counter() - t_agent_start

                        if response and response.content:
                            print(f"[Altron] {response.content}")
                            update_hud_status(log_entry=f"Altron: {response.content}")
                            send_telegram_message_async(response.content)

                            t_tts_start = time.perf_counter()
                            tts_manager.speak(response.content)
                            tts_init_time = time.perf_counter() - t_tts_start
                    except Exception as proc_err:
                        agent_time = time.perf_counter() - t_agent_start
                        print(f"[Agent Execution Error] {proc_err}")

                    total_time = listen_vad + stt_dur + agent_time + tts_init_time
                    print(
                        f"[Voice Timing] Listen/VAD: {listen_vad:.2f}s | "
                        f"STT: {stt_dur:.2f}s | "
                        f"Agent/LLM: {agent_time:.2f}s | "
                        f"TTS Init: {tts_init_time:.2f}s | "
                        f"Total: {total_time:.2f}s"
                    )
                    update_hud_status(state="Idle")

                # Plain-Python listener: callbacks run on this thread (no Qt event loop).
                worker.text_recognized.connect(_on_recognized)
                worker.error_occurred.connect(lambda err: print(f"[Voice Error] {err}"))
                worker.run()
                if not shutdown_event.is_set():
                    shutdown_event.wait(2)  # listener ended on its own: back off before restarting
            except Exception as voice_err:
                print(f"[Voice Subsystem Error] {voice_err}")
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
            print("[Telegram] Warning: Bot token or chat ID is not configured. Telegram listener disabled.")
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
                    print(f"[Telegram Worker] Incoming remote command: \"{clean_cmd}\"")
                    update_hud_status(state="Thinking", log_entry=f"Telegram: {clean_cmd}")

                    def _on_complete(res):
                        if res and res.content:
                            print(f"[Telegram Response] {res.content}")
                            update_hud_status(state="Idle", log_entry=f"Altron: {res.content}")
                            send_telegram_message_async(res.content)
                            if tts_manager.is_enabled and not tts_manager.is_muted:
                                tts_manager.speak(res.content)

                    executor.process(
                        user_message=clean_cmd,
                        on_complete=_on_complete,
                        on_error=lambda err: print(f"[Telegram Error] {err}"),
                        origin=CHANNEL_TELEGRAM,
                    )

                tg_worker.remote_command_received.connect(_on_telegram_command)
                tg_worker.run()
                if not shutdown_event.is_set():
                    shutdown_event.wait(5)  # listener ended on its own: back off before restarting
            except Exception as tg_err:
                print(f"[Telegram Subsystem Error] {tg_err}")
                time.sleep(5)

    tg_thread = threading.Thread(target=_run_telegram, name="AltronTelegramThread", daemon=True)
    tg_thread.start()

    # -------------------------------------------------------------
    # 4. Online Readiness Banner & Main Thread Supervision Loop
    # -------------------------------------------------------------
    update_hud_status(state="Idle")
    print("Altron is online.", flush=True)

    try:
        while not shutdown_event.is_set():
            time.sleep(1)
    except KeyboardInterrupt:
        print("\n[Altron] Shutting down gracefully...")
        shutdown_event.set()
        if active_voice_worker[0]:
            try:
                active_voice_worker[0].abort()
            except Exception:
                pass
        if active_tg_worker[0]:
            try:
                active_tg_worker[0].stop()
            except Exception:
                pass
        try:
            executor.cancel()
            approvals.stop()
        except Exception:
            pass
        try:
            tts_manager.stop()
        except Exception:
            pass
        update_hud_status(state="Offline")
        print("[Altron] All subsystems stopped cleanly.")


if __name__ == "__main__":
    main()
