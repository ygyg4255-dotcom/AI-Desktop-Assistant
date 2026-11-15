"""Chat interface window with NotchPixel dynamic island UI, Always-On Voice, and Agent execution."""
import json
import ctypes
import sys
import time
from typing import Optional, Union, Callable

from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout,
    QLineEdit, QPushButton, QLabel, QScrollArea, QFrame,
    QApplication
)
from PySide6.QtCore import Qt, Signal, QObject, Slot, QPropertyAnimation, QRect, QEasingCurve, QTimer
from PySide6.QtGui import QFont, QColor, QGuiApplication, QCursor, QPainter, QPainterPath, QPen

from src.storage.db import Database
from src.agent.executor import AgentExecutor, AgentStep, AgentResponse, PermissionRequest, AgentState
from src.services.ai_client import AIClient
from src.services.voice_service import TTSManager, SpeechRecognitionWorker, parse_wake_word
from src.services.telegram_service import TelegramRemoteWorker, send_telegram_message_async


class AgentBridge(QObject):
    """Qt Signal bridge to safely dispatch background agent callbacks to the main GUI thread."""

    step_received = Signal(object)        # AgentStep
    permission_requested = Signal(object) # PermissionRequest
    response_received = Signal(object)    # AgentResponse
    error_occurred = Signal(str)          # Error message string


class MessageBubble(QFrame):
    """A sleek NotchPixel message bubble in the chat view."""

    def __init__(self, role: str, content: str, parent=None):
        super().__init__(parent)
        self._role = role
        self._setup_ui(content)

    def _setup_ui(self, content: str):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 8, 12, 8)
        layout.setSpacing(3)

        is_user = (self._role == "user")
        role_label = QLabel("You" if is_user else "Assistant")
        role_label.setFont(QFont("Segoe UI", 8, QFont.Bold))
        role_label.setStyleSheet("color: #A78BFA;" if is_user else "color: #9CA3AF;")
        layout.addWidget(role_label)

        content_label = QLabel(content)
        content_label.setWordWrap(True)
        content_label.setTextInteractionFlags(Qt.TextSelectableByMouse)
        content_label.setFont(QFont("Segoe UI", 9))
        content_label.setStyleSheet(f"color: {'#F9FAFB' if is_user else '#E5E7EB'}; background: transparent; border: none;")
        layout.addWidget(content_label)

        if is_user:
            self.setStyleSheet("""
                QFrame {
                    background-color: #181824;
                    border: 1px solid rgba(139, 92, 246, 0.35);
                    border-radius: 12px;
                    margin-left: 45px;
                    margin-right: 4px;
                }
            """)
        else:
            self.setStyleSheet("""
                QFrame {
                    background-color: #101016;
                    border: 1px solid rgba(255, 255, 255, 0.07);
                    border-radius: 12px;
                    margin-left: 4px;
                    margin-right: 45px;
                }
            """)


class StepBubble(QFrame):
    """A compact card displaying an agent reasoning step or tool execution."""

    def __init__(self, step: AgentStep, parent=None):
        super().__init__(parent)
        self._setup_ui(step)

    def _setup_ui(self, step: AgentStep):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(10, 6, 10, 6)
        layout.setSpacing(3)

        if step.step_type == "tool_use":
            header = QLabel(f"⚙️ Tool: {step.tool_name}")
            header.setFont(QFont("Segoe UI", 8, QFont.Bold))
            header.setStyleSheet("color: #8B5CF6; background: transparent; border: none;")
            layout.addWidget(header)

            input_summary = json.dumps(step.tool_input, ensure_ascii=False)
            if len(input_summary) > 120:
                input_summary = input_summary[:120] + "..."
            detail = QLabel(f"Input: {input_summary}")
            detail.setFont(QFont("Consolas", 8))
            detail.setStyleSheet("color: #94A3B8; background: transparent; border: none;")
            detail.setWordWrap(True)
            layout.addWidget(detail)

            self.setStyleSheet("""
                QFrame {
                    background-color: #12121A;
                    border: 1px dashed rgba(139, 92, 246, 0.3);
                    border-radius: 10px;
                    margin-left: 15px;
                    margin-right: 15px;
                }
            """)

        elif step.step_type == "tool_result":
            is_success = step.tool_result.success if step.tool_result else True
            icon = "✓" if is_success else "✗"
            color = "#10B981" if is_success else "#EF4444"

            header = QLabel(f"{icon} Result: {step.tool_name}")
            header.setFont(QFont("Segoe UI", 8, QFont.Bold))
            header.setStyleSheet(f"color: {color}; background: transparent; border: none;")
            layout.addWidget(header)

            out_text = step.content
            if len(out_text) > 140:
                out_text = out_text[:140] + "..."
            detail = QLabel(out_text)
            detail.setFont(QFont("Consolas", 8))
            detail.setStyleSheet("color: #9CA3AF; background: transparent; border: none;")
            detail.setWordWrap(True)
            layout.addWidget(detail)

            self.setStyleSheet("""
                QFrame {
                    background-color: #101016;
                    border: 1px solid rgba(255, 255, 255, 0.08);
                    border-radius: 10px;
                    margin-left: 15px;
                    margin-right: 15px;
                }
            """)

        else:
            label = QLabel(f"💭 {step.content}")
            label.setFont(QFont("Segoe UI", 8))
            label.setStyleSheet("color: #6B7280; font-style: italic; background: transparent; border: none;")
            layout.addWidget(label)
            self.setStyleSheet("""
                QFrame {
                    background-color: transparent;
                    border: none;
                    margin-left: 15px;
                }
            """)


class PermissionCard(QFrame):
    """Interactive permission card prompting user for sensitive tool approval."""

    def __init__(self, request: PermissionRequest, executor: AgentExecutor, on_resolved: Optional[Callable[[], None]] = None, parent=None):
        super().__init__(parent)
        self._request = request
        self._executor = executor
        self._on_resolved = on_resolved
        self._setup_ui()

    def _setup_ui(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 10, 12, 10)
        layout.setSpacing(6)

        header_layout = QHBoxLayout()
        header_label = QLabel(f"⚠️ Permission Request: {self._request.tool_name}")
        header_label.setFont(QFont("Segoe UI", 8, QFont.Bold))
        header_label.setStyleSheet("color: #F59E0B; border: none; background: transparent;")
        header_layout.addWidget(header_label)

        if self._request.is_dangerous:
            danger_badge = QLabel("DANGEROUS")
            danger_badge.setFont(QFont("Segoe UI", 7, QFont.Bold))
            danger_badge.setStyleSheet("""
                background-color: #EF4444;
                color: white;
                border-radius: 3px;
                padding: 1px 5px;
            """)
            header_layout.addWidget(danger_badge)

        header_layout.addStretch()
        layout.addLayout(header_layout)

        msg_label = QLabel(self._request.message)
        msg_label.setFont(QFont("Segoe UI", 8))
        msg_label.setWordWrap(True)
        msg_label.setStyleSheet("color: #E5E7EB; border: none; background: transparent;")
        layout.addWidget(msg_label)

        btn_layout = QHBoxLayout()
        btn_layout.setSpacing(6)

        self._approve_btn = QPushButton("Approve Once")
        self._approve_btn.setFont(QFont("Segoe UI", 8, QFont.Bold))
        self._approve_btn.setStyleSheet("""
            QPushButton {
                background-color: #10B981;
                color: white;
                border: none;
                border-radius: 6px;
                padding: 4px 10px;
            }
            QPushButton:hover {
                background-color: #059669;
            }
        """)
        self._approve_btn.clicked.connect(self._on_approve)
        btn_layout.addWidget(self._approve_btn)

        self._session_btn = QPushButton("Always Allow")
        self._session_btn.setFont(QFont("Segoe UI", 8))
        self._session_btn.setStyleSheet("""
            QPushButton {
                background-color: #6366F1;
                color: white;
                border: none;
                border-radius: 6px;
                padding: 4px 10px;
            }
            QPushButton:hover {
                background-color: #4F46E5;
            }
        """)
        self._session_btn.clicked.connect(self._on_allow_session)
        btn_layout.addWidget(self._session_btn)

        self._reject_btn = QPushButton("Reject")
        self._reject_btn.setFont(QFont("Segoe UI", 8))
        self._reject_btn.setStyleSheet("""
            QPushButton {
                background-color: transparent;
                color: #EF4444;
                border: 1px solid #EF4444;
                border-radius: 6px;
                padding: 4px 10px;
            }
            QPushButton:hover {
                background-color: rgba(239, 68, 68, 0.15);
            }
        """)
        self._reject_btn.clicked.connect(self._on_reject)
        btn_layout.addWidget(self._reject_btn)

        btn_layout.addStretch()
        layout.addLayout(btn_layout)

        self._status_label = QLabel("")
        self._status_label.setFont(QFont("Segoe UI", 8, QFont.Bold))
        self._status_label.setStyleSheet("background: transparent;")
        layout.addWidget(self._status_label)

        self.setStyleSheet("""
            QFrame {
                background-color: #181510;
                border: 1px solid rgba(245, 158, 11, 0.4);
                border-radius: 10px;
                margin-left: 15px;
                margin-right: 15px;
            }
        """)

    def _disable_buttons(self):
        self._approve_btn.setEnabled(False)
        self._session_btn.setEnabled(False)
        self._reject_btn.setEnabled(False)

    def _on_approve(self):
        self._disable_buttons()
        self._status_label.setText("✅ Approved")
        self._status_label.setStyleSheet("color: #10B981; background: transparent;")
        self._request.approve(session_allow=False)
        if self._on_resolved:
            self._on_resolved()

    def _on_allow_session(self):
        self._disable_buttons()
        self._status_label.setText("✅ Session allowed")
        self._status_label.setStyleSheet("color: #6366F1; background: transparent;")
        self._request.approve(session_allow=True)
        if self._on_resolved:
            self._on_resolved()

    def _on_reject(self):
        self._disable_buttons()
        self._status_label.setText("❌ Rejected")
        self._status_label.setStyleSheet("color: #EF4444; background: transparent;")
        self._request.reject()
        if self._on_resolved:
            self._on_resolved()


class ChatWindow(QWidget):
    """Floating Dynamic Island Command Center with NotchPixel aesthetic and Always-On Voice."""

    exit_requested = Signal()
    hidden = Signal()
    settings_requested = Signal()

    COLLAPSED_WIDTH_APP = 200
    COLLAPSED_HEIGHT_APP = 22
    COLLAPSED_Y_APP = 0

    COLLAPSED_WIDTH_DESKTOP = 260
    COLLAPSED_HEIGHT_DESKTOP = 38
    COLLAPSED_Y_DESKTOP = 0

    COLLAPSED_WIDTH = 260
    COLLAPSED_HEIGHT = 38

    EXPANDED_WIDTH = 420
    EXPANDED_HEIGHT = 260
    EXPANDED_Y = 0

    @property
    def collapsed_width(self) -> int:
        return self.COLLAPSED_WIDTH_APP if getattr(self, "_foreground_app_active", False) else self.COLLAPSED_WIDTH_DESKTOP

    @property
    def collapsed_height(self) -> int:
        return self.COLLAPSED_HEIGHT_APP if getattr(self, "_foreground_app_active", False) else self.COLLAPSED_HEIGHT_DESKTOP

    @property
    def collapsed_y(self) -> int:
        return 0

    def __init__(
        self,
        executor_or_client: Union[AgentExecutor, AIClient],
        database: Optional[Database] = None,
        parent=None
    ):
        super().__init__(parent)
        self._database = database

        if isinstance(executor_or_client, AgentExecutor):
            self._executor = executor_or_client
        else:
            self._executor = AgentExecutor(executor_or_client, database=database)

        self._current_future = None
        self._is_expanded = False
        self._is_pinned = False  # Always False for seamless auto-collapse
        self._foreground_app_active = False
        self._active_permission_request: Optional[PermissionRequest] = None
        self._status_state = "idle"  # "idle", "listening", "thinking", "speaking"

        # Load initial settings
        self._dynamic_notch_enabled = self._database.get_setting("dynamic_notch", True) if self._database else True
        enable_tts = self._database.get_setting("enable_tts", True) if self._database else True
        self._tts_manager = TTSManager(enabled=enable_tts, parent=self)
        self._tts_manager.speech_started.connect(self._on_tts_started)
        self._tts_manager.speech_finished.connect(self._on_tts_finished)

        # Thread bridge for agent execution
        self._bridge = AgentBridge()
        self._bridge.step_received.connect(self._handle_step)
        self._bridge.permission_requested.connect(self._handle_permission)
        self._bridge.response_received.connect(self._handle_response)
        self._bridge.error_occurred.connect(self._handle_error)

        # Collapse debounce timer
        self._collapse_timer = QTimer(self)
        self._collapse_timer.setSingleShot(True)
        self._collapse_timer.timeout.connect(self._do_auto_collapse)

        # Adaptive notch timer: poll foreground window every 800ms when collapsed
        self._adaptive_notch_timer = QTimer(self)
        self._adaptive_notch_timer.setInterval(800)
        self._adaptive_notch_timer.timeout.connect(self._poll_foreground_window)

        # Wake Word State & Timer ("altron")
        self._wake_active_until = 0.0
        self._wake_timer = QTimer(self)
        self._wake_timer.setSingleShot(True)
        self._wake_timer.timeout.connect(self._on_wake_timeout)

        # Command input source tracker ("voice", "text", "telegram")
        self._current_command_source = "text"

        # Always-On Continuous Voice Recognition Worker
        self._voice_worker: Optional[SpeechRecognitionWorker] = None

        self._setup_window_properties()
        self._setup_ui()

        # Start in compact notch mode with initial adaptive detection flush with y=0
        self._foreground_app_active = self._check_foreground_app()
        self._apply_geometry(expanded=False, animated=False)

        # Start Always-On Continuous Background Listening
        self._start_always_on_voice()

        # Telegram Remote Control Worker
        self._is_remote_executing = False
        self.telegram_worker = TelegramRemoteWorker()
        self.telegram_worker.remote_command_received.connect(self._on_telegram_command)
        self.telegram_worker.start()

    def _setup_window_properties(self):
        """Configure sleek frameless floating dynamic island flags."""
        self.setWindowFlags(
            Qt.FramelessWindowHint |
            Qt.WindowStaysOnTopHint |
            Qt.SubWindow
        )
        self.setAttribute(Qt.WA_TranslucentBackground, True)

        self._anim = QPropertyAnimation(self, b"geometry")
        self._anim.setEasingCurve(QEasingCurve.OutQuart)
        self._anim.finished.connect(self._on_animation_finished)

    def paintEvent(self, event):
        """Paint seamless Pitch-Black Obsidian body with smooth rounded bottom corners and status rim glow."""
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)

        w = self.width()
        h = self.height()

        # Determine bottom corner radius based on mode
        if self._is_expanded:
            bottom_radius = 22.0
        elif getattr(self, "_foreground_app_active", False):
            bottom_radius = 11.0
        else:
            bottom_radius = 18.0

        # Create smooth path: square top flush with screen edge, rounded bottom corners
        path = QPainterPath()
        path.moveTo(0, 0)
        path.lineTo(w, 0)
        path.lineTo(w, h - bottom_radius)
        path.quadTo(w, h, w - bottom_radius, h)
        path.lineTo(bottom_radius, h)
        path.quadTo(0, h, 0, h - bottom_radius)
        path.closeSubpath()

        # Fill background with pitch-black obsidian (#050508)
        painter.fillPath(path, QColor("#050508"))

        # Determine subtle status rim glow color
        if self._status_state == "thinking":
            glow_color = QColor("#8B5CF6")
        elif self._status_state == "speaking":
            glow_color = QColor("#10B981")
        else:
            # idle / listening
            glow_color = QColor(255, 255, 255, 20)

        # Draw 1.2px status rim stroke
        pen = QPen(glow_color, 1.2)
        painter.setPen(pen)
        painter.drawPath(path)

    def resizeEvent(self, event):
        """Keep dynamic island centered horizontally at top of screen."""
        super().resizeEvent(event)
        screen = QGuiApplication.primaryScreen()
        if screen and not getattr(self, "_repositioning", False):
            self._repositioning = True
            try:
                geo = screen.geometry()
                target_x = geo.x() + (geo.width() - self.width()) // 2
                if abs(self.x() - target_x) > 1:
                    self.move(target_x, self.y())
            finally:
                self._repositioning = False

    def _check_foreground_app(self) -> bool:
        """Return True if an active application window (not desktop/taskbar) is in foreground."""
        try:
            if not hasattr(ctypes, "windll") or not hasattr(ctypes.windll, "user32"):
                return False
            user32 = ctypes.windll.user32
            hwnd = user32.GetForegroundWindow()
            if not hwnd:
                return False
            try:
                our_hwnd = int(self.winId())
                if hwnd == our_hwnd:
                    return getattr(self, "_foreground_app_active", False)
            except Exception:
                pass
            buf = ctypes.create_unicode_buffer(256)
            user32.GetClassNameW(hwnd, buf, 256)
            cls_name = buf.value
            desktop_classes = {"Progman", "WorkerW", "Shell_TrayWnd"}
            return bool(cls_name and cls_name not in desktop_classes)
        except Exception:
            return False

    def _poll_foreground_window(self):
        """Poll foreground window every 800ms when collapsed to adapt strip thickness."""
        if self._is_expanded:
            return
        is_app = self._check_foreground_app()
        if is_app != getattr(self, "_foreground_app_active", None):
            self._foreground_app_active = is_app
            target = self._get_target_geometry(expanded=False)
            self._update_notch_appearance(expanded=False)
            self._anim.stop()
            self._anim.setDuration(180)
            self._anim.setStartValue(self.geometry())
            self._anim.setEndValue(target)
            self._anim.start()

    def _is_permission_waiting(self) -> bool:
        """Check whether a permission dialog/card is actively awaiting user interaction."""
        req = getattr(self, "_active_permission_request", None)
        if req is not None:
            if req.response is None:
                return True
            self._active_permission_request = None
        if hasattr(self, "_executor") and self._executor.pending_permission is not None:
            if self._executor.pending_permission.response is None:
                return True
        return False

    def _on_permission_resolved(self):
        """Callback when user approves or rejects permission card."""
        self._active_permission_request = None
        if not self.geometry().contains(QCursor.pos()):
            self._schedule_collapse(1000)

    def _get_target_geometry(self, expanded: bool) -> QRect:
        """Calculate top-centered position flush with top screen edge (y = 0)."""
        screen = QGuiApplication.primaryScreen()
        geo = screen.geometry() if screen else QRect(0, 0, 1920, 1080)
        if expanded:
            w = self.EXPANDED_WIDTH
            h = self.EXPANDED_HEIGHT
            x = geo.x() + (geo.width() - w) // 2
            y = geo.y() + self.EXPANDED_Y
        else:
            w = self.collapsed_width
            h = self.collapsed_height
            x = geo.x() + (geo.width() - w) // 2
            y = geo.y() + self.collapsed_y
        return QRect(x, y, w, h)

    def _collapse_notch(self, animated: bool = True):
        """Collapse expanded panel down to sleek dynamic notch bar."""
        if not self._is_expanded:
            return
        if self._is_permission_waiting():
            return
        self._foreground_app_active = self._check_foreground_app()
        self._apply_geometry(expanded=False, animated=animated)

    def _expand_notch(self, animated: bool = True):
        """Expand dynamic notch to full command panel."""
        if self._is_expanded:
            return
        self._apply_geometry(expanded=True, animated=animated)

    def _apply_geometry(self, expanded: bool, animated: bool = True):
        """Transition between collapsed notch and expanded command panel."""
        self._is_expanded = expanded
        target = self._get_target_geometry(expanded)

        if not expanded:
            self._adaptive_notch_timer.start()
            # Collapse: immediately hide body widget and set minimum size to (0, 0) before animating
            self._body_widget.setVisible(False)
            self.setMinimumSize(0, 0)
            self.setMaximumSize(16777215, 16777215)
            self._update_notch_appearance(expanded=False)

            if animated:
                self._anim.stop()
                self._anim.setDuration(180)
                self._anim.setStartValue(self.geometry())
                self._anim.setEndValue(target)
                self._anim.start()
            else:
                self.setGeometry(target)
                self._on_animation_finished()
        else:
            self._adaptive_notch_timer.stop()
            # Expand: keep body hidden during geometry animation to prevent word-wrap stutter
            self._body_widget.setVisible(False)
            self.setMinimumSize(0, 0)
            self.setMaximumSize(16777215, 16777215)
            self._update_notch_appearance(expanded=True)

            if animated:
                self._anim.stop()
                self._anim.setDuration(200)
                self._anim.setStartValue(self.geometry())
                self._anim.setEndValue(target)
                self._anim.start()
            else:
                self.setGeometry(target)
                self._on_animation_finished()

    def _on_animation_finished(self):
        """Handle completion of expand or collapse animation."""
        if not self._is_expanded:
            self._body_widget.setVisible(False)
            target = self._get_target_geometry(False)
            self.setGeometry(target)
        else:
            # Show body widget only when geometry animation finishes
            self._body_widget.setVisible(True)
            target = self._get_target_geometry(True)
            self.setGeometry(target)
            self._input_field.setFocus()
        self.update()

    def _update_notch_appearance(self, expanded: bool):
        """Update island controls visibility and appearance."""
        if expanded:
            if hasattr(self, "_ai_icon"):
                self._ai_icon.setFont(QFont("Segoe UI", 11, QFont.Bold))
            if hasattr(self, "_compact_label"):
                self._compact_label.setVisible(False)
            if hasattr(self, "_model_badge"):
                self._model_badge.setVisible(True)
        else:
            is_app = getattr(self, "_foreground_app_active", False)
            if is_app:
                if hasattr(self, "_ai_icon"):
                    self._ai_icon.setFont(QFont("Segoe UI", 8, QFont.Bold))
                if hasattr(self, "_compact_label"):
                    self._compact_label.setFont(QFont("Segoe UI", 8, QFont.Bold))
            else:
                if hasattr(self, "_ai_icon"):
                    self._ai_icon.setFont(QFont("Segoe UI", 10, QFont.Bold))
                if hasattr(self, "_compact_label"):
                    self._compact_label.setFont(QFont("Segoe UI", 9, QFont.Bold))

            if hasattr(self, "_compact_label"):
                self._compact_label.setVisible(True)
            if hasattr(self, "_model_badge"):
                self._model_badge.setVisible(False)
        self.update()

    def _setup_ui(self):
        """Construct the decluttered NotchPixel dynamic island interface."""
        root_layout = QVBoxLayout(self)
        root_layout.setContentsMargins(10, 2, 10, 6)
        root_layout.setSpacing(4)

        # --- Top Header Bar (Always visible) ---
        self._header_bar = QWidget()
        self._header_bar.setStyleSheet("background: transparent;")
        header_layout = QHBoxLayout(self._header_bar)
        header_layout.setContentsMargins(4, 2, 4, 2)
        header_layout.setSpacing(6)

        # AI indicator icon
        self._ai_icon = QLabel("✦")
        self._ai_icon.setFont(QFont("Segoe UI", 10, QFont.Bold))
        self._ai_icon.setStyleSheet("color: #8B5CF6; border: none; background: transparent;")
        header_layout.addWidget(self._ai_icon)

        # Compact label (visible when collapsed or expanded)
        self._compact_label = QLabel("AI Assistant")
        self._compact_label.setFont(QFont("Segoe UI", 9, QFont.Bold))
        self._compact_label.setStyleSheet("color: #F8FAFC; border: none; background: transparent;")
        header_layout.addWidget(self._compact_label)

        # Model badge (visible when expanded)
        model_name = self._executor._ai_client.model
        short_model = model_name[:10] + ".." if len(model_name) > 12 else model_name
        self._model_badge = QLabel(f"[{short_model}]")
        self._model_badge.setFont(QFont("Segoe UI", 8))
        self._model_badge.setStyleSheet("color: #6B7280; border: none; background: transparent;")
        self._model_badge.setToolTip(f"Active Model: {model_name}")
        self._model_badge.setVisible(False)
        header_layout.addWidget(self._model_badge)

        header_layout.addStretch()

        # Right header controls: ONLY Settings (⚙) and Permanent Close (✕)
        self._settings_btn = QPushButton("⚙")
        self._settings_btn.setToolTip("Open Settings")
        self._settings_btn.setFixedSize(24, 22)
        self._settings_btn.setStyleSheet("""
            QPushButton {
                background-color: transparent;
                color: #9CA3AF;
                border: none;
                font-size: 13px;
                border-radius: 4px;
            }
            QPushButton:hover {
                color: #FFFFFF;
                background-color: rgba(255, 255, 255, 0.12);
            }
        """)
        self._settings_btn.clicked.connect(self.settings_requested.emit)
        header_layout.addWidget(self._settings_btn)

        self._close_btn = QPushButton("✕")
        self._close_btn.setToolTip("Exit Application Permanently")
        self._close_btn.setFixedSize(24, 22)
        self._close_btn.setStyleSheet("""
            QPushButton {
                background-color: transparent;
                color: #9CA3AF;
                border: none;
                font-size: 13px;
                border-radius: 4px;
            }
            QPushButton:hover {
                color: #EF4444;
                background-color: rgba(239, 68, 68, 0.18);
            }
        """)
        self._close_btn.clicked.connect(self._permanent_quit)
        header_layout.addWidget(self._close_btn)

        root_layout.addWidget(self._header_bar)

        # --- Expanded Body Container (Hidden when collapsed) ---
        self._body_widget = QWidget()
        self._body_widget.setStyleSheet("background: transparent;")
        exp_layout = QVBoxLayout(self._body_widget)
        exp_layout.setContentsMargins(0, 0, 0, 0)
        exp_layout.setSpacing(6)

        # Scroll Area for chat messages (Strictly transparent with zero white background)
        self._scroll_area = QScrollArea()
        self._scroll_area.setWidgetResizable(True)
        self._scroll_area.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self._scroll_area.setVerticalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        self._scroll_area.setStyleSheet("""
            QScrollArea {
                background: transparent;
                border: none;
            }
            QScrollArea > QWidget > QWidget {
                background: transparent;
            }
            QScrollBar:vertical {
                width: 0px;
                background: transparent;
            }
        """)
        self._scroll_area.viewport().setStyleSheet("background: transparent; border: none;")

        self._chat_container = QWidget()
        self._chat_container.setStyleSheet("background: transparent;")
        self._chat_layout = QVBoxLayout(self._chat_container)
        self._chat_layout.setContentsMargins(4, 4, 4, 4)
        self._chat_layout.setSpacing(8)
        self._chat_layout.addStretch()
        self._scroll_area.setWidget(self._chat_container)
        exp_layout.addWidget(self._scroll_area, 1)

        # Status Bar
        self._status_bar = QWidget()
        self._status_bar.setFixedHeight(20)
        self._status_bar.setStyleSheet("background: transparent;")
        status_layout = QHBoxLayout(self._status_bar)
        status_layout.setContentsMargins(6, 0, 6, 0)

        self._status_label = QLabel("Listening...")
        self._status_label.setFont(QFont("Segoe UI", 8))
        self._status_label.setStyleSheet("color: #6B7280; background: transparent;")
        status_layout.addWidget(self._status_label)
        status_layout.addStretch()
        exp_layout.addWidget(self._status_bar)

        # Input Bar (Obsidian Glass + Send Pill Button, Mic button completely removed)
        input_area = QWidget()
        input_area.setStyleSheet("background: transparent;")
        input_layout = QHBoxLayout(input_area)
        input_layout.setContentsMargins(4, 0, 4, 4)
        input_layout.setSpacing(8)

        self._input_field = QLineEdit()
        self._input_field.setPlaceholderText("Type a message or speak naturally...")
        self._input_field.setFont(QFont("Segoe UI", 9))
        self._input_field.setStyleSheet("""
            QLineEdit {
                background-color: #12121A;
                color: #FFFFFF;
                border: 1px solid rgba(255, 255, 255, 0.1);
                border-radius: 12px;
                padding: 6px 12px;
            }
            QLineEdit:focus {
                border: 1px solid rgba(139, 92, 246, 0.5);
            }
            QLineEdit:disabled {
                background-color: #0A0A10;
                color: #4B5563;
            }
        """)
        self._input_field.returnPressed.connect(self._send_message)
        input_layout.addWidget(self._input_field, 1)

        # Sleek Pill Send Button (#6366F1, border-radius: 12px)
        self._send_button = QPushButton("Send")
        self._send_button.setFont(QFont("Segoe UI", 9, QFont.Bold))
        self._send_button.setFixedSize(68, 32)
        self._send_button.setStyleSheet("""
            QPushButton {
                background-color: #6366F1;
                color: #FFFFFF;
                border: none;
                border-radius: 12px;
            }
            QPushButton:hover {
                background-color: #4F46E5;
            }
            QPushButton:disabled {
                background-color: #2D2D3E;
                color: #6B7280;
            }
        """)
        self._send_button.clicked.connect(self._send_message)
        input_layout.addWidget(self._send_button)

        # Stop Button
        self._cancel_btn = QPushButton("Stop")
        self._cancel_btn.setFont(QFont("Segoe UI", 9))
        self._cancel_btn.setFixedSize(65, 32)
        self._cancel_btn.setStyleSheet("""
            QPushButton {
                background-color: #EF4444;
                color: white;
                border: none;
                border-radius: 12px;
            }
            QPushButton:hover {
                background-color: #DC2626;
            }
        """)
        self._cancel_btn.setVisible(False)
        self._cancel_btn.clicked.connect(self._cancel_request)
        input_layout.addWidget(self._cancel_btn)

        exp_layout.addWidget(input_area)
        root_layout.addWidget(self._body_widget)

    # --- Mouse Hover Dynamic Notch Behaviors ---

    def enterEvent(self, event):
        """Expand dynamic island when mouse hovers over it."""
        self._collapse_timer.stop()
        if not self._is_expanded and self._dynamic_notch_enabled:
            self._expand_notch(animated=True)
        super().enterEvent(event)

    def leaveEvent(self, event):
        """Schedule smooth collapse when mouse leaves island."""
        if self._dynamic_notch_enabled:
            self._schedule_collapse(380)
        super().leaveEvent(event)

    def mousePressEvent(self, event):
        """Clicking on collapsed notch immediately expands it."""
        if not self._is_expanded:
            self._expand_notch(animated=True)
        super().mousePressEvent(event)

    def _schedule_collapse(self, delay_ms: int = 380):
        """Schedule smooth collapse if dynamic notch is active and not waiting for permission."""
        if not self._dynamic_notch_enabled:
            return
        if self._is_permission_waiting():
            return
        self._collapse_timer.start(delay_ms)

    def _do_auto_collapse(self):
        """Execute smooth collapse on mouse leave."""
        if not self._dynamic_notch_enabled:
            return
        if self._is_permission_waiting():
            return
        if self.geometry().contains(QCursor.pos()):
            return
        self._collapse_notch(animated=True)

    def _check_collapse_after_submit(self):
        """1 second after command submission, collapse cleanly if cursor is outside."""
        if not self._dynamic_notch_enabled:
            return
        if self._is_permission_waiting():
            return
        if not self.geometry().contains(QCursor.pos()):
            self._collapse_notch(animated=True)

    # --- Always-On Continuous Voice Listening ---

    def _start_always_on_voice(self):
        """Start the background continuous speech recognition loop."""
        self._voice_worker = SpeechRecognitionWorker(parent=self)
        self._voice_worker.started_listening.connect(self._on_voice_started)
        self._voice_worker.status_updated.connect(self._on_voice_status)
        self._voice_worker.text_recognized.connect(self._on_voice_recognized)
        self._voice_worker.error_occurred.connect(self._on_voice_error)
        self._voice_worker.start()

    def _on_wake_timeout(self):
        """Return to wake-word standby mode when 8-second window expires."""
        self._wake_active_until = 0.0
        if self._status_state == "listening" and self._executor.state == AgentState.IDLE:
            self._set_notch_status("AI Assistant")
            self._status_label.setText("Standby (say 'Altron')")
            self.update()

    def _on_voice_started(self):
        self._status_state = "listening"
        self._set_notch_status("AI Assistant")
        self._status_label.setText("Standby (say 'Altron')")
        self.update()

    def _on_voice_status(self, msg: str):
        if self._status_state == "idle":
            self._status_state = "listening"
            self.update()
        if time.time() < getattr(self, "_wake_active_until", 0.0):
            self._status_label.setText(f"{msg} (Altron Awake)")
        else:
            self._status_label.setText(msg)

    def _on_voice_recognized(self, text: str):
        """Process recognized speech with 'altron' wake-word detection."""
        if not text or not text.strip():
            return

        clean_text = text.strip()
        has_wake_word, remainder = parse_wake_word(clean_text)
        is_awake = time.time() < getattr(self, "_wake_active_until", 0.0)

        if has_wake_word:
            if remainder:
                # Pattern a: Single phrase "altron <command>"
                self._wake_active_until = 0.0
                self._wake_timer.stop()
                self._set_notch_status("⚡ Thinking...")
                self._status_label.setText(f"Voice: \"{remainder}\"")
                self._execute_command(remainder, source="voice")
            else:
                # Pattern b: Two-step wake - user said ONLY "altron"
                self._wake_active_until = time.time() + 8.0
                self._wake_timer.start(8000)
                self._status_state = "listening"
                self._set_notch_status("👂 Listening...")
                self._status_label.setText("Altron awake! Listening for command...")
                self.update()
        elif is_awake:
            # Pattern b (step 2): Active 8-second window, execute spoken phrase directly
            self._wake_active_until = 0.0
            self._wake_timer.stop()
            self._set_notch_status("⚡ Thinking...")
            self._status_label.setText(f"Voice: \"{clean_text}\"")
            self._execute_command(clean_text, source="voice")
        else:
            # Any speech detected before hearing "altron" (not in 8-second window) is ignored
            print(f"[Voice] Ignored speech without wake word: '{clean_text}'")

    def _on_voice_error(self, err_msg: str):
        self._status_label.setText(f"Voice: {err_msg}")

    def _on_tts_started(self, text: str = ""):
        """Echo Prevention: Automatically pause mic capture when TTS speaks."""
        if self._voice_worker:
            self._voice_worker.pause()
        self._status_state = "speaking"
        self._set_notch_status("🔊 Speaking...")
        self._status_label.setText("Speaking...")
        self.update()

    def _on_tts_finished(self):
        """Echo Prevention: Resume continuous listening immediately after TTS finishes."""
        if self._voice_worker:
            self._voice_worker.resume()
        self._status_state = "listening"
        if self._executor.state == AgentState.IDLE:
            self._set_notch_status("AI Assistant")
            self._status_label.setText("Standby (say 'Altron')")
        self.update()

    def _set_notch_status(self, text: str):
        """Update concise status text displayed on the notch bar."""
        self._compact_label.setText(text)

    # --- Agent & Message Handling ---

    def _clear_chat_widgets(self):
        while self._chat_layout.count() > 0:
            item = self._chat_layout.takeAt(0)
            if item.widget():
                item.widget().deleteLater()
        self._chat_layout.addStretch()

    def _add_message_bubble(self, role: str, content: str):
        if self._chat_layout.count() > 0:
            self._chat_layout.takeAt(self._chat_layout.count() - 1)

        bubble = MessageBubble(role, content)
        self._chat_layout.addWidget(bubble)
        self._chat_layout.addStretch()
        self._scroll_to_bottom()

    def _add_step_bubble(self, step: AgentStep):
        if self._chat_layout.count() > 0:
            self._chat_layout.takeAt(self._chat_layout.count() - 1)

        step_bubble = StepBubble(step)
        self._chat_layout.addWidget(step_bubble)
        self._chat_layout.addStretch()
        self._scroll_to_bottom()

    def _add_permission_card(self, request: PermissionRequest):
        if self._chat_layout.count() > 0:
            self._chat_layout.takeAt(self._chat_layout.count() - 1)

        card = PermissionCard(request, self._executor, on_resolved=self._on_permission_resolved)
        self._chat_layout.addWidget(card)
        self._chat_layout.addStretch()
        self._scroll_to_bottom()

    def _scroll_to_bottom(self):
        sb = self._scroll_area.verticalScrollBar()
        sb.setValue(sb.maximum())

    def _on_telegram_command(self, text: str):
        """Handle incoming command from Telegram remote control silently in background."""
        clean_text = text.strip() if text else ""
        if not clean_text:
            return

        # Keep floating UI collapsed (silent mode) or collapse immediately if currently expanded
        if self._is_expanded:
            self._collapse_notch(animated=False)

        self._is_remote_executing = True
        self._execute_command(clean_text, source="telegram")

    def _execute_command(self, text: str, source: str = "text", is_voice: bool = False, is_telegram: bool = False):
        """Dispatch text command directly into the agent execution pipeline."""
        clean_text = text.strip()
        if not clean_text or self._executor.state != AgentState.IDLE:
            return

        if is_voice:
            source = "voice"
        elif is_telegram:
            source = "telegram"

        self._current_command_source = source
        self._tts_manager.stop()

        if source == "telegram":
            bubble_content = f"📱 \"{clean_text}\""
        elif source == "voice":
            bubble_content = f"🎤 \"{clean_text}\""
        else:
            bubble_content = clean_text

        self._add_message_bubble("user", bubble_content)
        self._input_field.clear()

        self._status_state = "thinking"
        self._set_processing_state(True)
        self._set_notch_status("⚡ Thinking...")
        self._status_label.setText("Analyzing request...")
        self.update()

        # 1 second after submitting command, if cursor is outside, collapse cleanly
        if source != "telegram":
            QTimer.singleShot(1000, self._check_collapse_after_submit)

        self._current_future = self._executor.process(
            user_message=clean_text,
            on_step=self._bridge.step_received.emit,
            on_permission=self._bridge.permission_requested.emit,
            on_complete=self._bridge.response_received.emit,
            on_error=lambda err: self._bridge.error_occurred.emit(str(err))
        )

    def _send_message(self):
        """Send message from input field to AgentExecutor."""
        text = self._input_field.text().strip()
        if text:
            self._execute_command(text, source="text")

    def _cancel_request(self):
        self._active_permission_request = None
        self._executor.cancel()
        self._tts_manager.stop()
        self._status_state = "listening"
        self._set_notch_status("AI Assistant")
        self._status_label.setText("Cancelled by user")
        self._is_remote_executing = False
        self._current_command_source = "text"
        self._set_processing_state(False)
        self.update()

    def _set_processing_state(self, running: bool):
        self._input_field.setEnabled(not running)
        self._send_button.setVisible(not running)
        self._send_button.setEnabled(not running)
        self._cancel_btn.setVisible(running)
        self._cancel_btn.setEnabled(running)
        if not running and self._is_expanded:
            self._input_field.setFocus()

    @Slot(object)
    def _handle_step(self, step: AgentStep):
        self._status_state = "thinking"
        if step.step_type == "tool_use":
            tool_display = step.tool_name.replace("_", " ").title() if step.tool_name else "Tool"
            self._status_label.setText(f"⚙️ Running {tool_display}...")
            self._set_notch_status("⚡ Thinking...")
        elif step.step_type == "tool_result":
            self._status_label.setText("Processing output...")
            self._set_notch_status("⚡ Thinking...")
        elif step.step_type == "thinking":
            self._status_label.setText("Thinking...")
            self._set_notch_status("⚡ Thinking...")
        self.update()

    @Slot(object)
    def _handle_permission(self, request: PermissionRequest):
        self._active_permission_request = request
        self._set_notch_status("⚠️ Confirmation required")
        self._status_label.setText(f"⚠️ Awaiting confirmation: {request.tool_name}")
        self._add_permission_card(request)
        if not self._is_expanded and not getattr(self, "_is_remote_executing", False):
            self._expand_notch(animated=True)

    @Slot(object)
    def _handle_response(self, response: AgentResponse):
        """Slot when agent completes execution: render response and read aloud."""
        try:
            self._set_processing_state(False)
            self._active_permission_request = None

            if response.content:
                self._add_message_bubble("assistant", response.content)
                send_telegram_message_async(response.content)

                is_voice_source = (getattr(self, "_current_command_source", "text") == "voice")
                if is_voice_source and self._tts_manager.is_enabled and not self._tts_manager.is_muted:
                    self._status_state = "speaking"
                    self._set_notch_status("🔊 Speaking...")
                    self._status_label.setText("Speaking response...")
                    try:
                        self._tts_manager.speak(response.content)
                    except Exception:
                        self._status_state = "listening"
                        self._set_notch_status("AI Assistant")
                        self._status_label.setText("Standby (say 'Altron')")
                else:
                    self._status_state = "listening"
                    self._set_notch_status("AI Assistant")
                    self._status_label.setText("Standby (say 'Altron')")
            else:
                self._status_state = "listening"
                self._set_notch_status("AI Assistant")
                self._status_label.setText("Standby (say 'Altron')")
        finally:
            self._is_remote_executing = False
            self._current_command_source = "text"
            self._set_processing_state(False)
            self.update()

    @Slot(str)
    def _handle_error(self, error_str: str):
        try:
            self._set_processing_state(False)
            self._active_permission_request = None
            self._status_state = "listening"
            self._set_notch_status("AI Assistant")
            self._status_label.setText("Error occurred")
            err_msg = f"⚠️ Error: {error_str}"
            self._add_message_bubble("assistant", err_msg)
            send_telegram_message_async(err_msg)
        finally:
            self._is_remote_executing = False
            self._current_command_source = "text"
            self._set_processing_state(False)
            self.update()

    def update_model_display(self, model_name: str):
        short_model = model_name[:10] + ".." if len(model_name) > 12 else model_name
        self._model_badge.setText(f"[{short_model}]")
        self._model_badge.setToolTip(f"Active Model: {model_name}")

    def update_settings(self, cfg: dict):
        """Refresh dynamic notch and TTS settings from settings dialog."""
        if "dynamic_notch" in cfg:
            self._dynamic_notch_enabled = bool(cfg["dynamic_notch"])
        if "enable_tts" in cfg:
            self._tts_manager.is_enabled = bool(cfg["enable_tts"])

    def _permanent_quit(self):
        """Cleanly stop background workers/voice threads and permanently terminate the application."""
        if self._voice_worker:
            self._voice_worker.abort()
            self._voice_worker.wait(1000)

        if getattr(self, "telegram_worker", None):
            self.telegram_worker.stop()
            self.telegram_worker.wait(1000)

        if self._tts_manager:
            self._tts_manager.stop()

        if self._executor:
            self._executor.cancel()

        self.exit_requested.emit()
        app = QApplication.instance()
        if app:
            app.quit()
        sys.exit(0)

    def closeEvent(self, event):
        event.ignore()
        self.hide()
        self.hidden.emit()
