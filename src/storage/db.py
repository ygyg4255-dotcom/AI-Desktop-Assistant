"""Enhanced SQLite database for AI Desktop Assistant."""
import sqlite3
import json
from pathlib import Path
from typing import Optional, List, Dict, Any
from datetime import datetime
from dataclasses import dataclass, field


@dataclass
class Conversation:
    """Represents a conversation session."""
    id: Optional[int] = None
    title: str = ""
    created_at: str = ""
    updated_at: str = ""


@dataclass
class ChatMessage:
    """Represents a chat message."""
    id: Optional[int] = None
    conversation_id: int = 0
    role: str = ""
    content: str = ""
    timestamp: str = ""
    metadata: Dict[str, Any] = field(default_factory=dict)


@dataclass
class ToolExecution:
    """Represents a tool execution log."""
    id: Optional[int] = None
    conversation_id: int = 0
    tool_name: str = ""
    tool_input: Dict[str, Any] = field(default_factory=dict)
    tool_output: Dict[str, Any] = field(default_factory=dict)
    status: str = ""  # "pending", "approved", "rejected", "success", "failed"
    created_at: str = ""


class Database:
    """Enhanced SQLite database manager."""

    def __init__(self, db_path: Path):
        self.db_path = db_path
        self._connection: Optional[sqlite3.Connection] = None

    def initialize(self) -> None:
        """Initialize database connection and create tables."""
        self._connection = sqlite3.connect(str(self.db_path), check_same_thread=False)
        self._connection.row_factory = sqlite3.Row
        self._create_tables()
        self._migrate()

    def _create_tables(self) -> None:
        """Create all required tables."""
        cursor = self._connection.cursor()

        # Settings table
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS settings (
                key TEXT PRIMARY KEY,
                value TEXT
            )
        """)

        # Conversations table
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS conversations (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                title TEXT DEFAULT '',
                created_at TEXT DEFAULT CURRENT_TIMESTAMP,
                updated_at TEXT DEFAULT CURRENT_TIMESTAMP
            )
        """)

        # Messages table
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                conversation_id INTEGER NOT NULL,
                role TEXT NOT NULL,
                content TEXT NOT NULL,
                timestamp TEXT DEFAULT CURRENT_TIMESTAMP,
                metadata TEXT DEFAULT '{}',
                FOREIGN KEY (conversation_id) REFERENCES conversations(id)
            )
        """)

        # Tool executions table
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS tool_executions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                conversation_id INTEGER NOT NULL,
                tool_name TEXT NOT NULL,
                tool_input TEXT DEFAULT '{}',
                tool_output TEXT DEFAULT '{}',
                status TEXT DEFAULT 'pending',
                created_at TEXT DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (conversation_id) REFERENCES conversations(id)
            )
        """)

        # Permissions table
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS permissions (
                tool_name TEXT PRIMARY KEY,
                permission_mode TEXT DEFAULT 'ask',
                -- 'ask' = prompt user each time
                -- 'allow' = auto-allow for session
                -- 'deny' = auto-deny
                updated_at TEXT DEFAULT CURRENT_TIMESTAMP
            )
        """)

        self._connection.commit()

    def _migrate(self) -> None:
        """Run any necessary migrations."""
        # Future migrations go here
        pass

    # === Settings ===

    def get_setting(self, key: str, default: Any = None) -> Any:
        """Get a setting value."""
        cursor = self._connection.cursor()
        cursor.execute("SELECT value FROM settings WHERE key = ?", (key,))
        row = cursor.fetchone()
        if row:
            try:
                return json.loads(row["value"])
            except (json.JSONDecodeError, TypeError):
                return row["value"]
        return default

    def set_setting(self, key: str, value: Any) -> None:
        """Set a setting value."""
        cursor = self._connection.cursor()
        value_str = json.dumps(value) if not isinstance(value, str) else value
        cursor.execute(
            "INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)",
            (key, value_str)
        )
        self._connection.commit()

    # === Conversations ===

    def create_conversation(self, title: str = "") -> int:
        """Create a new conversation and return its ID."""
        cursor = self._connection.cursor()
        now = datetime.now().isoformat()
        cursor.execute(
            "INSERT INTO conversations (title, created_at, updated_at) VALUES (?, ?, ?)",
            (title or "New Conversation", now, now)
        )
        self._connection.commit()
        return cursor.lastrowid

    def get_conversation(self, conversation_id: int) -> Optional[Conversation]:
        """Get a conversation by ID."""
        cursor = self._connection.cursor()
        cursor.execute("SELECT * FROM conversations WHERE id = ?", (conversation_id,))
        row = cursor.fetchone()
        if row:
            return Conversation(
                id=row["id"],
                title=row["title"],
                created_at=row["created_at"],
                updated_at=row["updated_at"]
            )
        return None

    def list_conversations(self, limit: int = 50) -> List[Conversation]:
        """List recent conversations."""
        cursor = self._connection.cursor()
        cursor.execute(
            "SELECT * FROM conversations ORDER BY updated_at DESC LIMIT ?",
            (limit,)
        )
        return [
            Conversation(
                id=row["id"],
                title=row["title"],
                created_at=row["created_at"],
                updated_at=row["updated_at"]
            )
            for row in cursor.fetchall()
        ]

    def update_conversation(self, conversation_id: int, title: str = None) -> None:
        """Update a conversation."""
        cursor = self._connection.cursor()
        now = datetime.now().isoformat()
        if title:
            cursor.execute(
                "UPDATE conversations SET title = ?, updated_at = ? WHERE id = ?",
                (title, now, conversation_id)
            )
        else:
            cursor.execute(
                "UPDATE conversations SET updated_at = ? WHERE id = ?",
                (now, conversation_id)
            )
        self._connection.commit()

    def delete_conversation(self, conversation_id: int) -> None:
        """Delete a conversation and its messages."""
        cursor = self._connection.cursor()
        cursor.execute("DELETE FROM messages WHERE conversation_id = ?", (conversation_id,))
        cursor.execute("DELETE FROM tool_executions WHERE conversation_id = ?", (conversation_id,))
        cursor.execute("DELETE FROM conversations WHERE id = ?", (conversation_id,))
        self._connection.commit()

    # === Messages ===

    def add_message(
        self,
        conversation_id: int,
        role: str,
        content: str,
        metadata: Dict[str, Any] = None
    ) -> int:
        """Add a message to a conversation."""
        cursor = self._connection.cursor()
        now = datetime.now().isoformat()
        cursor.execute(
            """INSERT INTO messages
               (conversation_id, role, content, timestamp, metadata)
               VALUES (?, ?, ?, ?, ?)""",
            (conversation_id, role, content, now, json.dumps(metadata or {}))
        )
        self._connection.commit()
        self.update_conversation(conversation_id)
        return cursor.lastrowid

    def get_messages(self, conversation_id: int, limit: int = 100) -> List[ChatMessage]:
        """Get messages for a conversation."""
        cursor = self._connection.cursor()
        cursor.execute(
            """SELECT * FROM messages
               WHERE conversation_id = ?
               ORDER BY timestamp ASC
               LIMIT ?""",
            (conversation_id, limit)
        )
        messages = []
        for row in cursor.fetchall():
            metadata = {}
            try:
                metadata = json.loads(row["metadata"])
            except (json.JSONDecodeError, TypeError):
                pass
            messages.append(ChatMessage(
                id=row["id"],
                conversation_id=row["conversation_id"],
                role=row["role"],
                content=row["content"],
                timestamp=row["timestamp"],
                metadata=metadata
            ))
        return messages

    # === Tool Executions ===

    def log_tool_execution(
        self,
        conversation_id: int,
        tool_name: str,
        tool_input: Dict[str, Any],
        status: str = "pending"
    ) -> int:
        """Log a tool execution."""
        cursor = self._connection.cursor()
        now = datetime.now().isoformat()
        cursor.execute(
            """INSERT INTO tool_executions
               (conversation_id, tool_name, tool_input, status, created_at)
               VALUES (?, ?, ?, ?, ?)""",
            (conversation_id, tool_name, json.dumps(tool_input), status, now)
        )
        self._connection.commit()
        return cursor.lastrowid

    def update_tool_execution(
        self,
        execution_id: int,
        tool_output: Dict[str, Any],
        status: str
    ) -> None:
        """Update a tool execution with results."""
        cursor = self._connection.cursor()
        cursor.execute(
            """UPDATE tool_executions
               SET tool_output = ?, status = ?
               WHERE id = ?""",
            (json.dumps(tool_output), status, execution_id)
        )
        self._connection.commit()

    def get_tool_executions(self, conversation_id: int) -> List[ToolExecution]:
        """Get tool executions for a conversation."""
        cursor = self._connection.cursor()
        cursor.execute(
            "SELECT * FROM tool_executions WHERE conversation_id = ? ORDER BY created_at",
            (conversation_id,)
        )
        executions = []
        for row in cursor.fetchall():
            tool_input = {}
            tool_output = {}
            try:
                tool_input = json.loads(row["tool_input"])
            except (json.JSONDecodeError, TypeError):
                pass
            try:
                tool_output = json.loads(row["tool_output"])
            except (json.JSONDecodeError, TypeError):
                pass
            executions.append(ToolExecution(
                id=row["id"],
                conversation_id=row["conversation_id"],
                tool_name=row["tool_name"],
                tool_input=tool_input,
                tool_output=tool_output,
                status=row["status"],
                created_at=row["created_at"]
            ))
        return executions

    def get_all_tool_executions(self, limit: int = 150) -> List[ToolExecution]:
        """Get all tool executions across all conversations, newest first."""
        cursor = self._connection.cursor()
        cursor.execute(
            "SELECT * FROM tool_executions ORDER BY id DESC LIMIT ?",
            (limit,)
        )
        executions = []
        for row in cursor.fetchall():
            tool_input = {}
            tool_output = {}
            try:
                tool_input = json.loads(row["tool_input"])
            except (json.JSONDecodeError, TypeError):
                pass
            try:
                tool_output = json.loads(row["tool_output"])
            except (json.JSONDecodeError, TypeError):
                pass
            executions.append(ToolExecution(
                id=row["id"],
                conversation_id=row["conversation_id"],
                tool_name=row["tool_name"],
                tool_input=tool_input,
                tool_output=tool_output,
                status=row["status"],
                created_at=row["created_at"]
            ))
        return executions

    def clear_tool_executions(self) -> None:
        """Clear all tool execution logs."""
        cursor = self._connection.cursor()
        cursor.execute("DELETE FROM tool_executions")
        self._connection.commit()

    # === Permissions ===

    def get_tool_permission(self, tool_name: str) -> str:
        """Get permission mode for a tool. Default is 'ask'."""
        cursor = self._connection.cursor()
        cursor.execute("SELECT permission_mode FROM permissions WHERE tool_name = ?", (tool_name,))
        row = cursor.fetchone()
        return row["permission_mode"] if row else "ask"

    def set_tool_permission(self, tool_name: str, mode: str) -> None:
        """Set permission mode for a tool."""
        if mode not in ("ask", "allow", "deny"):
            raise ValueError(f"Invalid permission mode: {mode}")
        cursor = self._connection.cursor()
        now = datetime.now().isoformat()
        cursor.execute(
            """INSERT OR REPLACE INTO permissions (tool_name, permission_mode, updated_at)
               VALUES (?, ?, ?)""",
            (tool_name, mode, now)
        )
        self._connection.commit()

    def get_all_permissions(self) -> Dict[str, str]:
        """Get all tool permissions."""
        cursor = self._connection.cursor()
        cursor.execute("SELECT tool_name, permission_mode FROM permissions")
        return {row["tool_name"]: row["permission_mode"] for row in cursor.fetchall()}

    def close(self) -> None:
        """Close the database connection."""
        if self._connection:
            self._connection.close()
            self._connection = None
