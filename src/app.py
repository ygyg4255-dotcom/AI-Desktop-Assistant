"""Main application entry point."""
import sys
from typing import Optional

from PySide6.QtWidgets import QApplication
from PySide6.QtCore import QObject

from src.config import Settings
from src.services import AIClient, OmniRouteManager
from src.storage.db import Database
from src.agent.executor import AgentExecutor
from src.ui import SystemTrayIcon, ChatWindow, SettingsWindow


class Application(QObject):
    """Main application class managing the application lifecycle."""

    def __init__(self):
        super().__init__()
        self._settings = Settings()
        self._app: Optional[QApplication] = None
        self._tray_icon: Optional[SystemTrayIcon] = None
        self._database: Optional[Database] = None
        self._ai_client: Optional[AIClient] = None
        self._executor: Optional[AgentExecutor] = None
        self._chat_window: Optional[ChatWindow] = None
        self._settings_window: Optional[SettingsWindow] = None
        self._omniroute_manager: Optional[OmniRouteManager] = None

    def initialize(self) -> bool:
        """Initialize the application components."""
        # Create Qt application
        self._app = QApplication(sys.argv)
        self._app.setApplicationName(self._settings.app_name)
        self._app.setApplicationVersion(self._settings.app_version)
        self._app.setQuitOnLastWindowClosed(False)

        # Initialize SQLite database
        self._database = Database(self._settings.db_path)
        self._database.initialize()

        # Auto-start OmniRoute if enabled
        auto_start_omniroute = self._database.get_setting("auto_start_omniroute", True)
        if auto_start_omniroute:
            self._omniroute_manager = OmniRouteManager()
            self._omniroute_manager.start_if_needed(self._settings.anthropic_base_url)

        # Initialize AI client
        self._ai_client = AIClient(
            base_url=self._settings.anthropic_base_url,
            api_key=self._settings.anthropic_api_key,
            model=self._settings.anthropic_model,
            max_tokens=self._settings.max_tokens,
            timeout=self._settings.request_timeout
        )

        # Initialize Agent Executor
        self._executor = AgentExecutor(
            ai_client=self._ai_client,
            database=self._database
        )

        # Initialize system tray
        self._tray_icon = SystemTrayIcon(self._settings.app_name)
        self._tray_icon.exit_requested.connect(self._on_exit_requested)
        self._tray_icon.settings_requested.connect(self._on_settings_requested)
        self._tray_icon.about_requested.connect(self._on_about_requested)
        self._tray_icon.chat_requested.connect(self._on_chat_requested)
        self._tray_icon.initialize()

        # Create chat window and show immediately
        self._chat_window = ChatWindow(self._executor, self._database)
        self._chat_window.exit_requested.connect(self._on_exit_requested)
        self._chat_window.settings_requested.connect(self._on_settings_requested)
        self._chat_window.show()
        self._chat_window.raise_()
        self._chat_window.activateWindow()

        return True

    def run(self) -> int:
        """Run the application main loop."""
        if not self._app:
            return 1
        return self._app.exec()

    def shutdown(self) -> None:
        """Clean shutdown of all components."""
        if self._executor:
            self._executor.cancel()

        if self._ai_client:
            self._ai_client.cancel()

        if self._settings_window:
            self._settings_window.close()

        if self._chat_window:
            self._chat_window.close()

        if self._tray_icon:
            self._tray_icon.hide()

        # Stop OmniRoute if we spawned it
        if self._omniroute_manager:
            self._omniroute_manager.stop()

        if self._database:
            self._database.close()

    def _on_exit_requested(self) -> None:
        """Handle exit request from tray menu."""
        self.shutdown()
        if self._app:
            self._app.quit()

    def _on_settings_requested(self) -> None:
        """Handle settings request from tray menu or chat window."""
        if not self._settings_window:
            self._settings_window = SettingsWindow(self._settings, self._database)
            self._settings_window.settings_saved.connect(self._on_settings_saved)

        self._settings_window.show()
        self._settings_window.raise_()
        self._settings_window.activateWindow()

    def _on_settings_saved(self, new_cfg: dict) -> None:
        """Apply newly saved settings across components."""
        if self._ai_client:
            self._ai_client._base_url = new_cfg.get("base_url", self._ai_client._base_url)
            self._ai_client._api_key = new_cfg.get("api_key", self._ai_client._api_key)
            self._ai_client._model = new_cfg.get("model", self._ai_client._model)
            self._ai_client._max_tokens = new_cfg.get("max_tokens", self._ai_client._max_tokens)
            self._ai_client._timeout = new_cfg.get("timeout", self._ai_client._timeout)

        if self._chat_window and self._ai_client:
            self._chat_window.update_model_display(self._ai_client.model)
            self._chat_window.update_settings(new_cfg)

    def _on_about_requested(self) -> None:
        """Handle about request from tray menu."""
        if self._tray_icon:
            self._tray_icon.show_message(
                f"{self._settings.app_name} v{self._settings.app_version}",
                "A lightweight AI Desktop Assistant for Windows."
            )

    def _on_chat_requested(self) -> None:
        """Handle chat window request from tray menu or tray icon click."""
        if self._chat_window:
            if self._chat_window.isVisible():
                if self._chat_window.isActiveWindow():
                    self._chat_window.hide()
                else:
                    self._chat_window.raise_()
                    self._chat_window.activateWindow()
            else:
                self._chat_window.show()
                self._chat_window.raise_()
                self._chat_window.activateWindow()


def main() -> int:
    """Application entry point."""
    app = Application()
    if not app.initialize():
        return 1

    result = app.run()
    app.shutdown()
    return result


if __name__ == "__main__":
    sys.exit(main())
