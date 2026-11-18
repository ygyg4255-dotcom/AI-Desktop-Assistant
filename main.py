"""Unified Altron Entry Point.

Launches and coordinates all Altron subsystems concurrently with full fault isolation:
- HUD Web Interface Server (Flask / HTTP fallback on port 5000 / 5001)
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

from PySide6.QtCore import QCoreApplication, Qt

from src.config import Settings
from src.storage.db import Database
from src.services import AIClient, OmniRouteManager
from src.agent.executor import AgentExecutor
from src.services.hud_status import init_hud_status, update_hud_status
from src.services.voice_service import (
    TTSManager,
    SpeechRecognitionWorker,
    is_speaking_event,
    parse_wake_word,
)
from src.services.telegram_service import (
    TelegramRemoteWorker,
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


def main() -> None:
    """Coordinate and supervise all Altron subsystems concurrently."""
    # Ensure headless Qt core application is initialized for signals/events
    _qt_app = QCoreApplication.instance() or QCoreApplication(sys.argv)

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
    executor = AgentExecutor(ai_client=ai_client, database=database)
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
        )

    def _run_hud():
        try:
            start_hud_server(
                host="127.0.0.1",
                port=5000,
                open_browser=True,
                command_handler=_on_hud_command,
                on_config_updated=_on_ai_config_updated,
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
                worker = SpeechRecognitionWorker()
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

                worker.text_recognized.connect(_on_recognized, Qt.ConnectionType.DirectConnection)
                worker.error_occurred.connect(
                    lambda err: print(f"[Voice Error] {err}"),
                    Qt.ConnectionType.DirectConnection,
                )
                worker.run()
            except Exception as voice_err:
                print(f"[Voice Subsystem Error] {voice_err}")
                time.sleep(2)

    voice_thread = threading.Thread(target=_run_voice, name="AltronVoiceThread", daemon=True)
    voice_thread.start()

    # -------------------------------------------------------------
    # 3. Telegram Bot Subsystem Thread
    # -------------------------------------------------------------
    bot_token = os.environ.get("TELEGRAM_BOT_TOKEN") or getattr(settings, "telegram_bot_token", None)
    chat_id = os.environ.get("TELEGRAM_CHAT_ID") or getattr(settings, "telegram_chat_id", None)
    if not bot_token or not chat_id:
        try:
            from src.config.settings import load_env_file
            env_file = PROJECT_ROOT / ".env"
            if env_file.exists():
                vars_ = load_env_file(env_file)
                bot_token = bot_token or vars_.get("TELEGRAM_BOT_TOKEN")
                chat_id = chat_id or vars_.get("TELEGRAM_CHAT_ID")
        except Exception:
            pass

    def _run_telegram():
        if not bot_token or not chat_id or str(bot_token).strip() == "" or str(chat_id).strip() == "":
            print("[Telegram] Warning: Bot token or chat ID is not configured. Telegram listener disabled.")
            return

        while not shutdown_event.is_set():
            try:
                tg_worker = TelegramRemoteWorker(bot_token=bot_token, allowed_chat_id=chat_id)
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
                    )

                tg_worker.remote_command_received.connect(
                    _on_telegram_command, Qt.ConnectionType.DirectConnection
                )
                tg_worker.run()
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
