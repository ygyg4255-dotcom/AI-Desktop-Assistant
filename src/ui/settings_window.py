"""Settings dialog for AI Desktop Assistant."""
import os
import sys
import json
import platform
from pathlib import Path
from typing import Optional, Dict

from PySide6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QLabel, QLineEdit,
    QPushButton, QTabWidget, QWidget, QSpinBox, QCheckBox,
    QTableWidget, QTableWidgetItem, QHeaderView, QComboBox,
    QMessageBox, QFrame
)
from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QFont, QColor

from src.config import Settings
from src.config.settings import load_env_file
from src.storage.db import Database
from src.agent.tools import create_default_registry, ToolCategory
from src.services.ai_client import AIClient

# Windows Registry settings for auto-start
RUN_REG_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
REG_APP_NAME = "AIDesktopAssistant"


def is_windows_startup_enabled() -> bool:
    """Check if auto-start with Windows is enabled."""
    if platform.system() != "Windows":
        return False
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_REG_KEY, 0, winreg.KEY_READ) as key:
            winreg.QueryValueEx(key, REG_APP_NAME)
            return True
    except (FileNotFoundError, OSError, ImportError):
        return False


def set_windows_startup(enabled: bool) -> bool:
    """Enable or disable auto-start with Windows."""
    if platform.system() != "Windows":
        return False
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_REG_KEY, 0, winreg.KEY_SET_VALUE) as key:
            if enabled:
                # Point to executable if frozen or app.py
                if getattr(sys, "frozen", False):
                    cmd = f'"{sys.executable}"'
                else:
                    app_path = Path(__file__).resolve().parent.parent / "app.py"
                    cmd = f'"{sys.executable}" "{app_path}"'
                winreg.SetValueEx(key, REG_APP_NAME, 0, winreg.REG_SZ, cmd)
            else:
                try:
                    winreg.DeleteValue(key, REG_APP_NAME)
                except FileNotFoundError:
                    pass
        return True
    except Exception:
        return False


class SettingsWindow(QDialog):
    """Complete Settings Dialog for AI Desktop Assistant."""

    settings_saved = Signal(dict)

    def __init__(self, settings: Settings, database: Optional[Database] = None, parent=None):
        super().__init__(parent)
        self._settings = settings
        self._database = database
        self._tool_registry = create_default_registry()
        self._setup_ui()
        self._load_current_values()

    def _setup_ui(self):
        self.setWindowTitle("Settings - AI Desktop Assistant")
        self.setMinimumSize(560, 520)
        self.resize(620, 560)

        main_layout = QVBoxLayout(self)
        main_layout.setContentsMargins(15, 15, 15, 15)
        main_layout.setSpacing(12)

        # Header Title
        title_label = QLabel("Settings & Configuration")
        title_label.setFont(QFont("Segoe UI", 13, QFont.Bold))
        main_layout.addWidget(title_label)

        # Tab Widget
        self._tabs = QTabWidget()
        self._tabs.setFont(QFont("Segoe UI", 9))

        # Tab 1: AI Gateway & Models
        tab_gateway = self._create_gateway_tab()
        self._tabs.addTab(tab_gateway, "AI Gateway & Models")

        # Tab 2: Tool Permissions
        tab_permissions = self._create_permissions_tab()
        self._tabs.addTab(tab_permissions, "Tool Permissions")

        # Tab 3: System Integration
        tab_system = self._create_system_tab()
        self._tabs.addTab(tab_system, "System Integration")

        # Tab 4: Task Audit Log
        tab_audit = self._create_audit_tab()
        self._tabs.addTab(tab_audit, "Task Audit Log")

        main_layout.addWidget(self._tabs)

        # Bottom Buttons
        button_layout = QHBoxLayout()
        button_layout.addStretch()

        self._save_button = QPushButton("Save Settings")
        self._save_button.setFont(QFont("Segoe UI", 9, QFont.Bold))
        self._save_button.setStyleSheet("""
            QPushButton {
                background-color: #0078d4;
                color: white;
                border: none;
                border-radius: 4px;
                padding: 7px 18px;
            }
            QPushButton:hover {
                background-color: #106ebe;
            }
        """)
        self._save_button.clicked.connect(self._save_settings)
        button_layout.addWidget(self._save_button)

        self._cancel_button = QPushButton("Cancel")
        self._cancel_button.setFont(QFont("Segoe UI", 9))
        self._cancel_button.setStyleSheet("""
            QPushButton {
                background-color: #f3f3f3;
                border: 1px solid #ccc;
                border-radius: 4px;
                padding: 7px 15px;
            }
            QPushButton:hover {
                background-color: #e5e5e5;
            }
        """)
        self._cancel_button.clicked.connect(self.reject)
        button_layout.addWidget(self._cancel_button)

        main_layout.addLayout(button_layout)

    def _create_gateway_tab(self) -> QWidget:
        """Create the AI Gateway & Models configuration tab."""
        widget = QWidget()
        layout = QVBoxLayout(widget)
        layout.setContentsMargins(15, 15, 15, 15)
        layout.setSpacing(12)

        # Notice Card
        notice = QFrame()
        notice.setStyleSheet("background-color: #f0f7ff; border: 1px solid #cce4ff; border-radius: 6px; padding: 8px;")
        notice_layout = QVBoxLayout(notice)
        notice_layout.setContentsMargins(10, 8, 10, 8)
        notice_title = QLabel("OmniRoute Gateway Integration")
        notice_title.setFont(QFont("Segoe UI", 9, QFont.Bold))
        notice_title.setStyleSheet("color: #005a9e; border: none;")
        notice_text = QLabel(
            "All model calls route through your local OmniRoute instance. "
            "Use the 'serve' combo model identifier for automatic Round-Robin and fallback."
        )
        notice_text.setWordWrap(True)
        notice_text.setStyleSheet("color: #333; border: none; font-size: 8.5pt;")
        notice_layout.addWidget(notice_title)
        notice_layout.addWidget(notice_text)
        layout.addWidget(notice)

        # Base URL
        layout.addWidget(QLabel("OmniRoute Base URL:"))
        self._base_url_input = QLineEdit()
        self._base_url_input.setPlaceholderText("http://localhost:20128")
        self._base_url_input.setStyleSheet("""
            QLineEdit {
                border: 1px solid #ced4da;
                border-radius: 4px;
                padding: 6px 10px;
                background-color: #ffffff;
                color: #111827;
            }
            QLineEdit:focus {
                border-color: #0078d4;
            }
        """)
        layout.addWidget(self._base_url_input)

        # API Key
        layout.addWidget(QLabel("API Key:"))
        key_layout = QHBoxLayout()
        self._api_key_input = QLineEdit()
        self._api_key_input.setEchoMode(QLineEdit.Password)
        self._api_key_input.setPlaceholderText("sk-...")
        self._api_key_input.setStyleSheet("""
            QLineEdit {
                border: 1px solid #ced4da;
                border-radius: 4px;
                padding: 6px 10px;
                background-color: #ffffff;
                color: #111827;
            }
            QLineEdit:focus {
                border-color: #0078d4;
            }
        """)
        key_layout.addWidget(self._api_key_input)

        self._show_key_check = QCheckBox("Show")
        self._show_key_check.toggled.connect(
            lambda checked: self._api_key_input.setEchoMode(QLineEdit.Normal if checked else QLineEdit.Password)
        )
        key_layout.addWidget(self._show_key_check)
        layout.addLayout(key_layout)

        # Model Selector
        layout.addWidget(QLabel("Active Model Identifier:"))
        model_layout = QHBoxLayout()
        self._model_input = QLineEdit()
        self._model_input.setPlaceholderText("serve")
        self._model_input.setStyleSheet("""
            QLineEdit {
                border: 1px solid #ced4da;
                border-radius: 4px;
                padding: 6px 10px;
                background-color: #ffffff;
                color: #111827;
            }
            QLineEdit:focus {
                border-color: #0078d4;
            }
        """)
        model_layout.addWidget(self._model_input)

        self._reset_model_button = QPushButton("Use 'serve'")
        self._reset_model_button.clicked.connect(lambda: self._model_input.setText("serve"))
        model_layout.addWidget(self._reset_model_button)
        layout.addLayout(model_layout)

        # Max Tokens & Timeout
        num_layout = QHBoxLayout()
        vbox_tokens = QVBoxLayout()
        vbox_tokens.addWidget(QLabel("Max Output Tokens:"))
        self._tokens_spin = QSpinBox()
        self._tokens_spin.setRange(256, 32768)
        self._tokens_spin.setSingleStep(512)
        vbox_tokens.addWidget(self._tokens_spin)
        num_layout.addLayout(vbox_tokens)

        vbox_timeout = QVBoxLayout()
        vbox_timeout.addWidget(QLabel("Request Timeout (seconds):"))
        self._timeout_spin = QSpinBox()
        self._timeout_spin.setRange(10, 600)
        self._timeout_spin.setSingleStep(10)
        vbox_timeout.addWidget(self._timeout_spin)
        num_layout.addLayout(vbox_timeout)
        layout.addLayout(num_layout)

        # Test Connection Button
        test_layout = QHBoxLayout()
        self._test_button = QPushButton("Test Gateway Connection")
        self._test_button.setStyleSheet("padding: 6px 12px;")
        self._test_button.clicked.connect(self._test_connection)
        test_layout.addWidget(self._test_button)

        self._test_status_label = QLabel("")
        self._test_status_label.setFont(QFont("Segoe UI", 9))
        test_layout.addWidget(self._test_status_label)
        test_layout.addStretch()
        layout.addLayout(test_layout)

        # Auto-start OmniRoute checkbox
        self._auto_start_omniroute_check = QCheckBox("Auto-start OmniRoute server on launch")
        self._auto_start_omniroute_check.setChecked(True)
        layout.addWidget(self._auto_start_omniroute_check)

        layout.addStretch()
        return widget

    def _create_permissions_tab(self) -> QWidget:
        """Create the Tool Permissions policy configuration tab."""
        widget = QWidget()
        layout = QVBoxLayout(widget)
        layout.setContentsMargins(15, 15, 15, 15)
        layout.setSpacing(10)

        desc = QLabel(
            "Configure default permission behavior for each registered agent tool. "
            "'Ask' always prompts before running. 'Allow' executes automatically."
        )
        desc.setWordWrap(True)
        desc.setStyleSheet("color: #666; font-size: 8.5pt;")
        layout.addWidget(desc)

        # Permissions Table
        self._perm_table = QTableWidget()
        self._perm_table.setColumnCount(4)
        self._perm_table.setHorizontalHeaderLabels(["Tool Name", "Category", "Risk Level", "Permission Policy"])
        self._perm_table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeToContents)
        self._perm_table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeToContents)
        self._perm_table.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeToContents)
        self._perm_table.horizontalHeader().setSectionResizeMode(3, QHeaderView.Stretch)
        self._perm_table.setAlternatingRowColors(True)

        self._combo_map: Dict[str, QComboBox] = {}
        self._populate_permissions_table()
        layout.addWidget(self._perm_table)

        # Reset button
        btn_layout = QHBoxLayout()
        self._reset_perm_button = QPushButton("Reset All to Default")
        self._reset_perm_button.clicked.connect(self._reset_permissions_to_default)
        btn_layout.addWidget(self._reset_perm_button)
        btn_layout.addStretch()
        layout.addLayout(btn_layout)

        return widget

    def _populate_permissions_table(self):
        """Populate permissions table with tools."""
        tools = self._tool_registry.list_tools()
        self._perm_table.setRowCount(len(tools))

        # Load existing permissions from DB
        db_perms = self._database.get_all_permissions() if self._database else {}

        for row, tool in enumerate(tools):
            # Tool Name
            name_item = QTableWidgetItem(tool.name)
            name_item.setFlags(name_item.flags() & ~Qt.ItemIsEditable)
            self._perm_table.setItem(row, 0, name_item)

            # Category
            cat_name = tool.category.value.replace("_", " ").title()
            cat_item = QTableWidgetItem(cat_name)
            cat_item.setFlags(cat_item.flags() & ~Qt.ItemIsEditable)
            self._perm_table.setItem(row, 1, cat_item)

            # Risk Level
            risk_text = "Dangerous" if tool.is_dangerous else "Safe"
            risk_item = QTableWidgetItem(risk_text)
            risk_item.setFlags(risk_item.flags() & ~Qt.ItemIsEditable)
            if tool.is_dangerous:
                risk_item.setForeground(QColor("#d83b01"))
            else:
                risk_item.setForeground(QColor("#107c10"))
            self._perm_table.setItem(row, 2, risk_item)

            # Policy Selector
            combo = QComboBox()
            combo.addItem("Ask Every Time", "ask")
            combo.addItem("Always Allow", "allow")
            combo.addItem("Always Deny", "deny")
            combo.setStyleSheet("""
                QComboBox {
                    background-color: #ffffff;
                    color: #111827;
                    border: 1px solid #ced4da;
                    border-radius: 4px;
                    padding: 4px 8px;
                }
                QComboBox::drop-down {
                    border: none;
                }
                QComboBox QAbstractItemView {
                    background-color: white;
                    color: #111827;
                    selection-background-color: #0078d4;
                    selection-color: white;
                }
            """)

            # Determine selected index
            saved_mode = db_perms.get(tool.name)
            if saved_mode == "allow":
                combo.setCurrentIndex(1)
            elif saved_mode == "deny":
                combo.setCurrentIndex(2)
            else:
                # Default: safe -> allow or ask? Ask for dangerous, Allow for safe
                if not tool.requires_permission:
                    combo.setCurrentIndex(1)
                else:
                    combo.setCurrentIndex(0)

            self._combo_map[tool.name] = combo
            self._perm_table.setCellWidget(row, 3, combo)

    def _reset_permissions_to_default(self):
        """Reset permissions to tool defaults."""
        for tool in self._tool_registry.list_tools():
            combo = self._combo_map.get(tool.name)
            if combo:
                if not tool.requires_permission:
                    combo.setCurrentIndex(1)  # Allow
                else:
                    combo.setCurrentIndex(0)  # Ask

    def _create_system_tab(self) -> QWidget:
        """Create the System Integration tab."""
        widget = QWidget()
        layout = QVBoxLayout(widget)
        layout.setContentsMargins(15, 15, 15, 15)
        layout.setSpacing(15)

        group_title = QLabel("Windows Startup & Lifecycle")
        group_title.setFont(QFont("Segoe UI", 10, QFont.Bold))
        layout.addWidget(group_title)

        self._startup_check = QCheckBox("Start AI Desktop Assistant automatically with Windows")
        self._startup_check.setChecked(is_windows_startup_enabled())
        layout.addWidget(self._startup_check)

        startup_note = QLabel(
            "When enabled, the assistant starts minimized to the Windows System Tray at login. "
            "It consumes ~35MB RAM while idle and does not initiate API calls until opened."
        )
        startup_note.setWordWrap(True)
        startup_note.setStyleSheet("color: #666; font-size: 8.5pt;")
        layout.addWidget(startup_note)

        layout.addSpacing(10)

        # Voice & UI Behavior Section
        ui_title = QLabel("Voice & Dynamic Island UI")
        ui_title.setFont(QFont("Segoe UI", 10, QFont.Bold))
        layout.addWidget(ui_title)

        self._enable_tts_check = QCheckBox("Enable Voice Output (TTS)")
        self._enable_tts_check.setToolTip("Read aloud the assistant's final response using local text-to-speech.")
        layout.addWidget(self._enable_tts_check)

        self._dynamic_notch_check = QCheckBox("Dynamic Notch Behavior (Auto-collapse on mouse leave)")
        self._dynamic_notch_check.setToolTip("Smoothly collapse the floating command panel into a compact notch when idle.")
        layout.addWidget(self._dynamic_notch_check)

        layout.addSpacing(10)

        # Storage & Database section
        db_title = QLabel("Database & Local Storage")
        db_title.setFont(QFont("Segoe UI", 10, QFont.Bold))
        layout.addWidget(db_title)

        db_path_label = QLabel(f"Database Path: {self._settings.db_path}")
        db_path_label.setStyleSheet("color: #444; font-size: 8.5pt;")
        layout.addWidget(db_path_label)

        self._clear_history_btn = QPushButton("Clear All Conversation History")
        self._clear_history_btn.setFixedWidth(230)
        self._clear_history_btn.clicked.connect(self._clear_all_history)
        layout.addWidget(self._clear_history_btn)

        layout.addStretch()
        return widget

    def _create_audit_tab(self) -> QWidget:
        """Create the Task Audit Log viewer tab."""
        widget = QWidget()
        layout = QVBoxLayout(widget)
        layout.setContentsMargins(15, 15, 15, 15)
        layout.setSpacing(10)

        desc = QLabel(
            "Audit trail of tool executions initiated by the agent. "
            "Inspect parameters, execution status, and results."
        )
        desc.setWordWrap(True)
        desc.setStyleSheet("color: #666; font-size: 8.5pt;")
        layout.addWidget(desc)

        # Audit Table
        self._audit_table = QTableWidget()
        self._audit_table.setColumnCount(5)
        self._audit_table.setHorizontalHeaderLabels(["Timestamp", "Tool Name", "Input Summary", "Status", "Output Snippet"])
        self._audit_table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeToContents)
        self._audit_table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeToContents)
        self._audit_table.horizontalHeader().setSectionResizeMode(2, QHeaderView.Interactive)
        self._audit_table.horizontalHeader().setSectionResizeMode(3, QHeaderView.ResizeToContents)
        self._audit_table.horizontalHeader().setSectionResizeMode(4, QHeaderView.Stretch)
        self._audit_table.setAlternatingRowColors(True)
        layout.addWidget(self._audit_table)

        # Buttons
        btn_layout = QHBoxLayout()
        self._refresh_audit_btn = QPushButton("Refresh Logs")
        self._refresh_audit_btn.clicked.connect(self._load_audit_logs)
        btn_layout.addWidget(self._refresh_audit_btn)

        self._clear_audit_btn = QPushButton("Clear Audit Logs")
        self._clear_audit_btn.clicked.connect(self._clear_audit_logs)
        btn_layout.addWidget(self._clear_audit_btn)

        btn_layout.addStretch()
        layout.addLayout(btn_layout)

        # Initial load
        self._load_audit_logs()

        return widget

    def _load_audit_logs(self):
        """Load tool execution history from SQLite into the audit table."""
        if not self._database:
            return

        executions = self._database.get_all_tool_executions(limit=150)
        self._audit_table.setRowCount(len(executions))

        for row, ex in enumerate(executions):
            # 0. Timestamp
            ts_str = ex.created_at[:19].replace("T", " ") if ex.created_at else ""
            item_time = QTableWidgetItem(ts_str)
            item_time.setFlags(item_time.flags() & ~Qt.ItemIsEditable)
            self._audit_table.setItem(row, 0, item_time)

            # 1. Tool Name
            item_tool = QTableWidgetItem(ex.tool_name)
            item_tool.setFlags(item_tool.flags() & ~Qt.ItemIsEditable)
            self._audit_table.setItem(row, 1, item_tool)

            # 2. Input Summary
            if isinstance(ex.tool_input, dict):
                input_parts = [f"{k}={v}" for k, v in ex.tool_input.items() if not k.startswith("_")]
                input_summary = ", ".join(input_parts)
            else:
                input_summary = str(ex.tool_input)
            item_input = QTableWidgetItem(input_summary[:120])
            item_input.setToolTip(input_summary)
            item_input.setFlags(item_input.flags() & ~Qt.ItemIsEditable)
            self._audit_table.setItem(row, 2, item_input)

            # 3. Status
            status_text = ex.status or "unknown"
            item_status = QTableWidgetItem(status_text.capitalize())
            item_status.setFlags(item_status.flags() & ~Qt.ItemIsEditable)
            if status_text in ("success", "approved"):
                item_status.setForeground(QColor("#107c10"))
            elif status_text in ("failed", "rejected"):
                item_status.setForeground(QColor("#d83b01"))
            elif status_text == "pending":
                item_status.setForeground(QColor("#b27b00"))
            self._audit_table.setItem(row, 3, item_status)

            # 4. Output Snippet
            if isinstance(ex.tool_output, dict):
                out_msg = ex.tool_output.get("output") or ex.tool_output.get("error") or ex.tool_output.get("message")
                if out_msg is None:
                    out_snippet = json.dumps(ex.tool_output)
                else:
                    out_snippet = str(out_msg)
            else:
                out_snippet = str(ex.tool_output)
            out_snippet = out_snippet.replace("\n", " ").strip()
            item_output = QTableWidgetItem(out_snippet[:150])
            item_output.setToolTip(out_snippet)
            item_output.setFlags(item_output.flags() & ~Qt.ItemIsEditable)
            self._audit_table.setItem(row, 4, item_output)

    def _clear_audit_logs(self):
        """Clear all tool execution audit logs."""
        if not self._database:
            return

        reply = QMessageBox.question(
            self,
            "Confirm Clear Logs",
            "Are you sure you want to delete all tool execution audit logs?",
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.No
        )
        if reply == QMessageBox.Yes:
            self._database.clear_tool_executions()
            self._load_audit_logs()
            QMessageBox.information(self, "Cleared", "Tool execution audit logs have been cleared.")

    def _load_current_values(self):
        """Load current settings into the UI."""
        self._base_url_input.setText(self._settings.anthropic_base_url)
        self._api_key_input.setText(self._settings.anthropic_api_key)
        self._model_input.setText(self._settings.anthropic_model)
        self._tokens_spin.setValue(self._settings.max_tokens)
        self._timeout_spin.setValue(int(self._settings.request_timeout))
        # Load auto-start OmniRoute setting (default True)
        auto_start = self._database.get_setting("auto_start_omniroute", True) if self._database else True
        self._auto_start_omniroute_check.setChecked(bool(auto_start))

        # Load voice & dynamic notch settings
        enable_tts = self._database.get_setting("enable_tts", self._settings.enable_tts) if self._database else self._settings.enable_tts
        self._enable_tts_check.setChecked(bool(enable_tts))

        dynamic_notch = self._database.get_setting("dynamic_notch", self._settings.dynamic_notch) if self._database else self._settings.dynamic_notch
        self._dynamic_notch_check.setChecked(bool(dynamic_notch))

    def _test_connection(self):
        """Test the connection to OmniRoute."""
        self._test_status_label.setText("Testing connection...")
        self._test_status_label.setStyleSheet("color: #666;")
        self._test_button.setEnabled(False)

        base_url = self._base_url_input.text().strip() or "http://localhost:20128"
        api_key = self._api_key_input.text().strip()
        model = self._model_input.text().strip() or "serve"

        client = AIClient(
            base_url=base_url,
            api_key=api_key,
            model=model,
            timeout=10.0
        )

        try:
            connected = client.test_connection()
            if connected:
                self._test_status_label.setText("✅ Connected successfully!")
                self._test_status_label.setStyleSheet("color: #107c10; font-weight: bold;")
            else:
                self._test_status_label.setText("❌ Connection failed (check OmniRoute)")
                self._test_status_label.setStyleSheet("color: #d83b01; font-weight: bold;")
        except Exception as e:
            self._test_status_label.setText(f"❌ Error: {str(e)[:40]}")
            self._test_status_label.setStyleSheet("color: #d83b01;")
        finally:
            self._test_button.setEnabled(True)

    def _clear_all_history(self):
        """Clear all conversation history from database."""
        if not self._database:
            return
        reply = QMessageBox.question(
            self,
            "Confirm Delete",
            "Are you sure you want to delete all saved conversations and history?",
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.No
        )
        if reply == QMessageBox.Yes:
            conversations = self._database.list_conversations(limit=500)
            for conv in conversations:
                if conv.id:
                    self._database.delete_conversation(conv.id)
            QMessageBox.information(self, "Cleared", "All conversation history has been cleared.")

    def _save_settings(self):
        """Save settings and apply changes."""
        base_url = self._base_url_input.text().strip() or "http://localhost:20128"
        api_key = self._api_key_input.text().strip()
        model = self._model_input.text().strip() or "serve"
        max_tokens = self._tokens_spin.value()
        timeout = float(self._timeout_spin.value())

        # Update in-memory settings
        self._settings.anthropic_base_url = base_url
        self._settings.anthropic_api_key = api_key
        self._settings.anthropic_model = model
        self._settings.max_tokens = max_tokens
        self._settings.request_timeout = timeout

        # Save to .env file preserving other existing keys
        env_path = self._settings.env_path
        try:
            env_vars = load_env_file(env_path) if env_path.exists() else {}
            env_vars["ANTHROPIC_BASE_URL"] = base_url
            env_vars["ANTHROPIC_API_KEY"] = api_key
            env_vars["ANTHROPIC_MODEL"] = model
            with open(env_path, "w", encoding="utf-8") as f:
                for k, v in env_vars.items():
                    f.write(f"{k}={v}\n")
        except Exception as e:
            QMessageBox.warning(self, "Warning", f"Could not update .env file: {e}")

        # Save tool permissions to SQLite
        if self._database:
            for tool_name, combo in self._combo_map.items():
                mode = combo.currentData()
                self._database.set_tool_permission(tool_name, mode)

        # Apply Windows startup setting
        set_windows_startup(self._startup_check.isChecked())

        # Save auto-start OmniRoute and voice settings to SQLite
        if self._database:
            self._database.set_setting("auto_start_omniroute", self._auto_start_omniroute_check.isChecked())
            self._database.set_setting("enable_tts", self._enable_tts_check.isChecked())
            self._database.set_setting("dynamic_notch", self._dynamic_notch_check.isChecked())

        self._settings.enable_tts = self._enable_tts_check.isChecked()
        self._settings.dynamic_notch = self._dynamic_notch_check.isChecked()

        self.settings_saved.emit({
            "base_url": base_url,
            "api_key": api_key,
            "model": model,
            "max_tokens": max_tokens,
            "timeout": timeout,
            "startup": self._startup_check.isChecked(),
            "enable_tts": self._enable_tts_check.isChecked(),
            "dynamic_notch": self._dynamic_notch_check.isChecked()
        })

        self.accept()
