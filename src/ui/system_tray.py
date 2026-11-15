"""System tray icon and menu management."""
from typing import Optional
from PySide6.QtWidgets import QSystemTrayIcon, QMenu
from PySide6.QtGui import QIcon, QAction, QPixmap, QPainter, QColor
from PySide6.QtCore import Signal, QObject


class SystemTrayIcon(QObject):
    """Manages the system tray icon and menu."""

    exit_requested = Signal()
    settings_requested = Signal()
    about_requested = Signal()
    chat_requested = Signal()

    def __init__(self, app_name: str):
        super().__init__()
        self._app_name = app_name
        self._tray_icon: Optional[QSystemTrayIcon] = None
        self._menu: Optional[QMenu] = None

    def initialize(self) -> None:
        """Create and configure the system tray icon."""
        self._create_icon()
        self._create_menu()
        self._tray_icon.show()

    def _create_icon(self) -> None:
        """Create the tray icon with a simple indicator."""
        self._tray_icon = QSystemTrayIcon()
        self._tray_icon.setToolTip(self._app_name)
        self._tray_icon.activated.connect(self._on_activated)

        # Create a simple icon programmatically
        pixmap = QPixmap(64, 64)
        pixmap.fill(QColor(0, 120, 215))  # Windows blue
        painter = QPainter(pixmap)
        painter.setPen(QColor(255, 255, 255))
        painter.drawText(pixmap.rect(), 0x0084, "AI")  # AlignCenter
        painter.end()
        self._tray_icon.setIcon(QIcon(pixmap))

    def _on_activated(self, reason: QSystemTrayIcon.ActivationReason) -> None:
        """Handle tray icon click activation."""
        if reason in (QSystemTrayIcon.Trigger, QSystemTrayIcon.DoubleClick):
            self.chat_requested.emit()

    def _create_menu(self) -> None:
        """Create the context menu for the tray icon."""
        self._menu = QMenu()

        chat_action = QAction("Open Chat", self._menu)
        chat_action.triggered.connect(self.chat_requested.emit)
        self._menu.addAction(chat_action)

        self._menu.addSeparator()

        about_action = QAction("About", self._menu)
        about_action.triggered.connect(self.about_requested.emit)
        self._menu.addAction(about_action)

        settings_action = QAction("Settings", self._menu)
        settings_action.triggered.connect(self.settings_requested.emit)
        self._menu.addAction(settings_action)

        self._menu.addSeparator()

        exit_action = QAction("Exit", self._menu)
        exit_action.triggered.connect(self.exit_requested.emit)
        self._menu.addAction(exit_action)

        self._tray_icon.setContextMenu(self._menu)

    def show_message(self, title: str, message: str) -> None:
        """Show a notification balloon from the tray icon."""
        self._tray_icon.showMessage(title, message, QSystemTrayIcon.Information, 3000)

    def hide(self) -> None:
        """Hide the tray icon."""
        if self._tray_icon:
            self._tray_icon.hide()
