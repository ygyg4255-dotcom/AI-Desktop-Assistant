"""Automated verification tests for Step 2 UI Integration, Tools, and Settings."""
import sys
import time
import os
import shutil
import tempfile
from pathlib import Path

# Add project root to path
sys.path.insert(0, str(Path(__file__).parent))

from PySide6.QtWidgets import QApplication

from src.config import Settings
from src.storage.db import Database
from src.agent.tools import (
    create_default_registry, ToolCategory,
    handle_file_copy, handle_file_move, handle_file_open,
    handle_file_write, handle_terminal_command
)
from src.agent.permissions import PermissionGate, PermissionLevel
from src.agent.executor import AgentExecutor, PermissionRequest, AgentStep
from src.services.ai_client import AIClient
from src.ui.chat_window import ChatWindow, PermissionCard, MessageBubble, StepBubble
from src.ui.settings_window import SettingsWindow, is_windows_startup_enabled, set_windows_startup


def test_new_file_tools():
    """Test the expanded file management tools: file_copy, file_move, file_open."""
    print("\n" + "=" * 60)
    print("TEST 1: Expanded File System Tools")
    print("=" * 60)

    with tempfile.TemporaryDirectory() as tmp_dir:
        tmp_path = Path(tmp_dir)

        # 1. Test file_write first
        print("\n[1.1] Testing file_write (requires permission vs approved)...")
        test_file = tmp_path / "hello.txt"
        res_unapproved = handle_file_write({"path": str(test_file), "content": "Hello World"})
        assert res_unapproved.requires_permission is True
        assert res_unapproved.is_dangerous is True
        assert not test_file.exists(), "File should not be created without approval"
        print("    [OK] Unapproved write blocked by permission check")

        res_approved = handle_file_write({"path": str(test_file), "content": "Hello World", "_approved": True})
        assert res_approved.success is True
        assert test_file.exists()
        assert test_file.read_text(encoding="utf-8") == "Hello World"
        print("    [OK] Approved write created file successfully")

        # 2. Test file_copy
        print("\n[1.2] Testing file_copy...")
        copy_dest = tmp_path / "hello_copied.txt"
        copy_res = handle_file_copy({"source": str(test_file), "destination": str(copy_dest)})
        assert copy_res.success is True
        assert copy_dest.exists()
        assert copy_dest.read_text(encoding="utf-8") == "Hello World"
        print("    [OK] File copy succeeded")

        # 3. Test file_move
        print("\n[1.3] Testing file_move (requires permission vs approved)...")
        move_dest = tmp_path / "hello_moved.txt"
        move_unapproved = handle_file_move({"source": str(copy_dest), "destination": str(move_dest)})
        assert move_unapproved.requires_permission is True
        assert move_unapproved.is_dangerous is True
        assert copy_dest.exists(), "File should not be moved without approval"
        print("    [OK] Unapproved move blocked by permission check")

        move_approved = handle_file_move({"source": str(copy_dest), "destination": str(move_dest), "_approved": True})
        assert move_approved.success is True
        assert move_dest.exists()
        assert not copy_dest.exists(), "Original file should be moved"
        print("    [OK] Approved move executed successfully")

        # 4. Test file_open validation
        print("\n[1.4] Testing file_open parameter validation...")
        open_res = handle_file_open({"path": str(tmp_path / "nonexistent.txt")})
        assert open_res.success is False
        assert "not found" in open_res.error.lower()
        print("    [OK] File open correctly detects missing files")

        # 5. Test terminal_execute
        print("\n[1.5] Testing terminal_execute (approved execution)...")
        term_res = handle_terminal_command({"command": "echo test_execution", "_approved": True})
        assert term_res.success is True
        assert "test_execution" in term_res.output.get("stdout", "")
        print("    [OK] Approved terminal command executed and captured output")

    print("\n[RESULT] Expanded File Tools: PASSED")
    return True


def test_permission_card_and_unblocking():
    """Test the PermissionCard widget and unblocking of PermissionRequest."""
    print("\n" + "=" * 60)
    print("TEST 2: In-Chat Permission Confirmation Card")
    print("=" * 60)

    # Initialize Qt application if needed
    app = QApplication.instance() or QApplication(sys.argv)

    # Mock settings and client
    settings = Settings()
    client = AIClient(settings.anthropic_base_url, settings.anthropic_api_key)
    executor = AgentExecutor(client)

    # 1. Test Single Approval
    print("\n[2.1] Testing 'Approve Once' button...")
    req1 = PermissionRequest("terminal_execute", {"command": "dir"}, "Execute dir command", is_dangerous=True)
    card1 = PermissionCard(req1, executor)
    assert card1._approve_btn.isEnabled() is True

    # Simulate clicking approve
    card1._approve_btn.click()
    assert req1.response is True
    assert req1.session_allow is False
    assert not card1._approve_btn.isEnabled()
    print("    [OK] Single approval unblocked request (response=True, session_allow=False)")

    # 2. Test Always Allow in Session
    print("\n[2.2] Testing 'Always Allow in Session' button...")
    req2 = PermissionRequest("file_move", {"source": "a.txt", "destination": "b.txt"}, "Move a.txt", is_dangerous=True)
    card2 = PermissionCard(req2, executor)

    # Simulate clicking always allow
    card2._session_btn.click()
    assert req2.response is True
    assert req2.session_allow is True
    assert not card2._session_btn.isEnabled()
    print("    [OK] Session allow unblocked request (response=True, session_allow=True)")

    # 3. Test Reject
    print("\n[2.3] Testing 'Reject' button...")
    req3 = PermissionRequest("file_delete", {"path": "important.dat"}, "Delete important file", is_dangerous=True)
    card3 = PermissionCard(req3, executor)

    # Simulate clicking reject
    card3._reject_btn.click()
    assert req3.response is False
    assert not card3._reject_btn.isEnabled()
    print("    [OK] Rejection unblocked request (response=False)")

    print("\n[RESULT] Permission Card & Unblocking: PASSED")
    return True


def test_chat_window_and_signals():
    """Test ChatWindow initialization, message rendering, and history loading."""
    print("\n" + "=" * 60)
    print("TEST 3: ChatWindow & Signal Bridge")
    print("=" * 60)

    app = QApplication.instance() or QApplication(sys.argv)

    test_db_path = Path.home() / ".ai-desktop-assistant" / "test_chat_window.db"
    db = Database(test_db_path)
    db.initialize()

    settings = Settings()
    client = AIClient(settings.anthropic_base_url, settings.anthropic_api_key)
    executor = AgentExecutor(client, database=db)

    print("\n[3.1] Creating ChatWindow instance...")
    chat_win = ChatWindow(executor, db)
    chat_win.show()
    print("    [OK] ChatWindow created and displayed")

    print("\n[3.2] Testing conversation creation and switching...")
    conv1 = db.create_conversation("First Topic")
    db.add_message(conv1, "user", "User question 1")
    db.add_message(conv1, "assistant", "Assistant answer 1")

    conv2 = db.create_conversation("Second Topic")
    db.add_message(conv2, "user", "User question 2")
    db.add_message(conv2, "assistant", "Assistant answer 2")

    chat_win._load_conversations_list()
    assert chat_win._conv_selector.count() >= 2
    print(f"    [OK] Loaded {chat_win._conv_selector.count()} conversations into selector")

    print("\n[3.3] Testing step bubbles and permission card insertion...")
    step = AgentStep(step_type="tool_use", content="Running file_search", tool_name="file_search", tool_input={"query": "test"})
    chat_win._handle_step(step)

    perm_req = PermissionRequest("terminal_execute", {"command": "whoami"}, "Execute whoami", is_dangerous=True)
    chat_win._handle_permission(perm_req)
    perm_req.approve()

    print("    [OK] Step bubble and permission card rendered cleanly")

    chat_win.close()
    db.close()
    print("\n[RESULT] ChatWindow & Signal Bridge: PASSED")
    return True


def test_settings_dialog():
    """Test SettingsWindow creation, tab population, and saving."""
    print("\n" + "=" * 60)
    print("TEST 4: Settings Dialog & Tool Permissions")
    print("=" * 60)

    app = QApplication.instance() or QApplication(sys.argv)

    test_db_path = Path.home() / ".ai-desktop-assistant" / "test_settings.db"
    db = Database(test_db_path)
    db.initialize()

    settings = Settings()

    print("\n[4.1] Creating SettingsWindow instance...")
    settings_win = SettingsWindow(settings, db)
    assert settings_win._tabs.count() >= 3
    print(f"    [OK] SettingsWindow initialized with {settings_win._tabs.count()} tabs")

    print("\n[4.2] Checking Tool Permissions table...")
    row_count = settings_win._perm_table.rowCount()
    assert row_count >= 10, f"Expected at least 10 tools, found {row_count}"
    print(f"    [OK] Permissions table has {row_count} tools listed")

    print("\n[4.3] Modifying and saving settings...")
    settings_win._model_input.setText("serve")
    settings_win._tokens_spin.setValue(8192)

    # Change permission for terminal_execute to 'deny' in the UI
    combo = settings_win._combo_map.get("terminal_execute")
    if combo:
        combo.setCurrentIndex(2)  # Index 2 is 'deny'

    # Save
    settings_win._save_settings()

    # Verify database was updated
    perm = db.get_tool_permission("terminal_execute")
    assert perm == "deny", f"Expected permission 'deny', got {perm}"
    print("    [OK] Tool permission persisted to SQLite database")

    # Verify settings object was updated
    assert settings.anthropic_model == "serve"
    assert settings.max_tokens == 8192
    print("    [OK] In-memory settings updated")

    settings_win.close()
    db.close()
    print("\n[RESULT] Settings Dialog: PASSED")
    return True


def main():
    """Run all Step 2 verification tests."""
    print("=" * 60)
    print("AI Desktop Assistant - Step 2 Integration Tests")
    print("=" * 60)

    results = []

    try:
        results.append(("Expanded File Tools", test_new_file_tools()))
    except Exception as e:
        print(f"\n[FAIL] Expanded File Tools: {e}")
        import traceback
        traceback.print_exc()
        results.append(("Expanded File Tools", False))

    try:
        results.append(("Permission Cards & Unblocking", test_permission_card_and_unblocking()))
    except Exception as e:
        print(f"\n[FAIL] Permission Cards: {e}")
        import traceback
        traceback.print_exc()
        results.append(("Permission Cards & Unblocking", False))

    try:
        results.append(("ChatWindow & Signals", test_chat_window_and_signals()))
    except Exception as e:
        print(f"\n[FAIL] ChatWindow: {e}")
        import traceback
        traceback.print_exc()
        results.append(("ChatWindow & Signals", False))

    try:
        results.append(("Settings Dialog", test_settings_dialog()))
    except Exception as e:
        print(f"\n[FAIL] Settings Dialog: {e}")
        import traceback
        traceback.print_exc()
        results.append(("Settings Dialog", False))

    # Summary
    print("\n" + "=" * 60)
    print("TEST SUMMARY")
    print("=" * 60)
    for name, passed in results:
        status = "PASSED" if passed else "FAILED"
        print(f"  {name}: {status}")

    total = len(results)
    passed = sum(1 for _, p in results if p)
    print(f"\nTotal: {passed}/{total} tests passed")
    print("=" * 60)

    return all(p for _, p in results)


if __name__ == "__main__":
    success = main()
    sys.exit(0 if success else 1)
