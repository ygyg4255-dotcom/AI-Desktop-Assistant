"""Final Verification Test Suite for Step 3:
- Advanced Tools: window_control, code_edit, web_fetch
- Task Audit Log Viewer in SettingsWindow & SQLite db
- System Tray Toggle
- Frozen path resolution
"""
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

# Add project root to path
sys.path.insert(0, str(Path(__file__).parent))

from PySide6.QtWidgets import QApplication

from src.config import Settings
from src.storage.db import Database
from src.agent.tools import (
    create_default_registry, ToolCategory,
    handle_window_control, handle_code_edit, handle_web_fetch
)
from src.agent.permissions import PermissionGate, PermissionLevel
from src.ui.settings_window import SettingsWindow


def test_window_control_tool():
    """Test window_control tool listing and parameter handling."""
    print("\n" + "=" * 60)
    print("TEST 1: Window Control Tool")
    print("=" * 60)

    registry = create_default_registry()
    tool = registry.get("window_control")
    assert tool is not None, "window_control tool not registered"
    assert tool.category == ToolCategory.SYSTEM
    assert tool.requires_permission is False
    assert tool.is_dangerous is False
    print("    [OK] Tool registered with correct metadata (Safe, System)")

    # 1. Test listing open windows
    print("\n[1.1] Testing window listing...")
    res = handle_window_control({"action": "list"})
    assert res.success is True, f"Failed to list windows: {res.error}"
    assert isinstance(res.output, dict)
    windows = res.output.get("windows", [])
    count = res.output.get("count", 0)
    print(f"    [OK] Found {count} open windows via Windows ctypes user32.dll")
    if windows:
        print(f"    Sample window: {windows[0]}")

    # 2. Test filtered listing
    print("\n[1.2] Testing filtered window listing...")
    res_filtered = handle_window_control({"action": "list", "title": "explorer"})
    assert res_filtered.success is True
    print(f"    [OK] Filtered listing succeeded (count: {res_filtered.output.get('count')})")

    # 3. Test invalid action error
    print("\n[1.3] Testing invalid action handling...")
    res_invalid = handle_window_control({"action": "invalid_action"})
    assert res_invalid.success is False
    assert "unsupported action" in res_invalid.error.lower()
    print("    [OK] Correctly rejected unsupported action")

    print("\n[RESULT] Window Control Tool: PASSED")
    return True


def test_code_edit_tool():
    """Test targeted code editing with search-and-replace and permission gating."""
    print("\n" + "=" * 60)
    print("TEST 2: Targeted Code Edit Tool")
    print("=" * 60)

    registry = create_default_registry()
    tool = registry.get("code_edit")
    assert tool is not None, "code_edit tool not registered"
    assert tool.category == ToolCategory.CODING
    assert tool.requires_permission is True
    assert tool.is_dangerous is True
    print("    [OK] Tool registered with correct metadata (Dangerous, Coding)")

    with tempfile.TemporaryDirectory() as tmp_dir:
        test_file = Path(tmp_dir) / "sample_code.py"
        test_file.write_text(
            "def calculate(x):\n    result = x * 2\n    return result\n\ndef helper():\n    return 42\n",
            encoding="utf-8"
        )

        # 1. Unapproved call -> must require permission
        print("\n[2.1] Testing permission gating for code_edit...")
        res_unapproved = handle_code_edit({
            "path": str(test_file),
            "old_string": "result = x * 2",
            "new_string": "result = x * 10"
        })
        assert res_unapproved.requires_permission is True
        assert res_unapproved.is_dangerous is True
        assert "result = x * 2" in test_file.read_text(encoding="utf-8"), "File should not be modified without approval"
        print("    [OK] Unapproved code edit blocked by permission check")

        # 2. Approved call -> performs exact replacement
        print("\n[2.2] Testing approved targeted code edit...")
        res_approved = handle_code_edit({
            "path": str(test_file),
            "old_string": "result = x * 2",
            "new_string": "result = x * 10"
        }, approved=True)
        assert res_approved.success is True
        assert res_approved.output.get("occurrences_replaced") == 1
        updated = test_file.read_text(encoding="utf-8")
        assert "result = x * 10" in updated
        assert "result = x * 2" not in updated
        assert "def helper():" in updated, "Remaining file content preserved"
        print("    [OK] Targeted search and replace succeeded without rewriting entire file")

        # 3. Target text not found -> returns clean error
        print("\n[2.3] Testing missing text detection...")
        res_missing = handle_code_edit({
            "path": str(test_file),
            "old_string": "nonexistent_code()",
            "new_string": "new_code()"
        }, approved=True)
        assert res_missing.success is False
        assert "not found" in res_missing.error.lower()
        print("    [OK] Correctly reported missing target text")

        # 4. Multiple occurrences without replace_all -> returns ambiguity error
        print("\n[2.4] Testing multiple occurrence safety check...")
        multi_file = Path(tmp_dir) / "multi.txt"
        multi_file.write_text("foo bar foo baz foo", encoding="utf-8")

        res_ambiguous = handle_code_edit({
            "path": str(multi_file),
            "old_string": "foo",
            "new_string": "replacement",
            "replace_all": False
        }, approved=True)
        assert res_ambiguous.success is False
        assert "occurs 3 times" in res_ambiguous.error
        print("    [OK] Ambiguous replacement blocked (requires replace_all=True)")

        # 5. Multiple occurrences with replace_all=True
        print("\n[2.5] Testing replace_all=True...")
        res_all = handle_code_edit({
            "path": str(multi_file),
            "old_string": "foo",
            "new_string": "replacement",
            "replace_all": True
        }, approved=True)
        assert res_all.success is True
        assert res_all.output.get("occurrences_replaced") == 3
        assert multi_file.read_text(encoding="utf-8") == "replacement bar replacement baz replacement"
        print("    [OK] replace_all=True replaced all occurrences successfully")

    print("\n[RESULT] Targeted Code Edit Tool: PASSED")
    return True


def test_web_fetch_tool():
    """Test web_fetch tool with clean HTML extraction."""
    print("\n" + "=" * 60)
    print("TEST 3: Web Fetch Tool")
    print("=" * 60)

    registry = create_default_registry()
    tool = registry.get("web_fetch")
    assert tool is not None, "web_fetch tool not registered"
    assert tool.category == ToolCategory.BROWSER
    assert tool.requires_permission is False
    assert tool.is_dangerous is False
    print("    [OK] Tool registered with correct metadata (Safe, Browser)")

    # 1. Missing URL parameter
    print("\n[3.1] Testing missing URL validation...")
    res_no_url = handle_web_fetch({})
    assert res_no_url.success is False
    assert "required" in res_no_url.error.lower()
    print("    [OK] Missing URL detected")

    # 2. Test extraction from mock HTML via patch
    print("\n[3.2] Testing HTML cleanup and text extraction...")
    sample_html = b"""
    <!DOCTYPE html>
    <html>
    <head><title>Test Title</title><style>body { color: red; }</style></head>
    <body>
        <script>console.log("bad script");</script>
        <h1>Welcome to AI Assistant</h1>
        <p>This is a paragraph with <b>bold text</b> and &amp; entities.</p>
        <div>
            <ul>
                <li>Item 1</li>
                <li>Item 2</li>
            </ul>
        </div>
    </body>
    </html>
    """

    class MockResponse:
        def __init__(self, data):
            self.data = data
            self.headers = {"Content-Type": "text/html; charset=utf-8"}
        def read(self):
            return self.data
        def __enter__(self):
            return self
        def __exit__(self, *args):
            pass

    with patch("urllib.request.urlopen", return_value=MockResponse(sample_html)):
        res = handle_web_fetch({"url": "https://example.com/test", "max_chars": 2000})
        assert res.success is True
        extracted_text = res.output.get("text", "")
        print(f"    Extracted Text Snippet:\n    {extracted_text[:120]}...")
        assert "Welcome to AI Assistant" in extracted_text
        assert "bold text and & entities." in extracted_text
        assert "console.log" not in extracted_text, "Scripts should be stripped"
        assert "color: red" not in extracted_text, "Styles should be stripped"
        assert "<p>" not in extracted_text, "HTML tags should be stripped"
        print("    [OK] Scripts, styles, and HTML tags stripped cleanly; HTML entities unescaped")

    print("\n[RESULT] Web Fetch Tool: PASSED")
    return True


def test_audit_log_tab():
    """Test Task Audit Log tab in SettingsWindow and SQLite database."""
    print("\n" + "=" * 60)
    print("TEST 4: Task Audit Log Viewer")
    print("=" * 60)

    app = QApplication.instance() or QApplication(sys.argv)

    test_db_path = Path.home() / ".ai-desktop-assistant" / "test_audit_log.db"
    db = Database(test_db_path)
    db.initialize()

    # Clear any old data
    db.clear_tool_executions()

    print("\n[4.1] Logging test tool executions to SQLite...")
    conv_id = db.create_conversation("Audit Log Test Conv")
    ex1 = db.log_tool_execution(conv_id, "window_control", {"action": "list"}, "success")
    db.update_tool_execution(ex1, {"windows_found": 5}, "success")

    ex2 = db.log_tool_execution(conv_id, "code_edit", {"path": "main.py", "old_string": "x"}, "approved")
    db.update_tool_execution(ex2, {"replaced": 1}, "success")

    ex3 = db.log_tool_execution(conv_id, "file_delete", {"path": "bad.txt"}, "rejected")

    all_logs = db.get_all_tool_executions(limit=10)
    assert len(all_logs) == 3, f"Expected 3 logs, found {len(all_logs)}"
    print(f"    [OK] Successfully retrieved {len(all_logs)} logs from SQLite")

    print("\n[4.2] Initializing SettingsWindow with Task Audit Log tab...")
    settings = Settings()
    settings_win = SettingsWindow(settings, db)
    assert settings_win._tabs.count() == 4, f"Expected 4 tabs, got {settings_win._tabs.count()}"
    assert settings_win._tabs.tabText(3) == "Task Audit Log"
    print("    [OK] Tab 4 'Task Audit Log' present in SettingsWindow")

    print("\n[4.3] Verifying audit table population...")
    assert settings_win._audit_table.rowCount() == 3
    # Check tool names in rows
    tools_in_table = [settings_win._audit_table.item(r, 1).text() for r in range(3)]
    assert "file_delete" in tools_in_table
    assert "code_edit" in tools_in_table
    assert "window_control" in tools_in_table
    print(f"    [OK] Table populated with tool execution records: {tools_in_table}")

    print("\n[4.4] Testing clear logs button in UI...")
    with patch("PySide6.QtWidgets.QMessageBox.question", return_value=16384), \
         patch("PySide6.QtWidgets.QMessageBox.information"):  # QMessageBox.Yes and suppress popup
        settings_win._clear_audit_btn.click()

    assert settings_win._audit_table.rowCount() == 0
    assert len(db.get_all_tool_executions()) == 0
    print("    [OK] Clear audit logs button cleared table and SQLite database")

    settings_win.close()
    db.close()
    print("\n[RESULT] Task Audit Log Viewer: PASSED")
    return True


def test_frozen_path_resolution():
    """Test settings path resolution when running under frozen executable."""
    print("\n" + "=" * 60)
    print("TEST 5: Frozen Executable Path Resolution")
    print("=" * 60)

    with tempfile.TemporaryDirectory() as tmp_dir:
        fake_exe = Path(tmp_dir) / "dist" / "AIDesktopAssistant" / "AIDesktopAssistant.exe"
        fake_exe.parent.mkdir(parents=True, exist_ok=True)
        fake_exe.touch()

        fake_env = fake_exe.parent / ".env"
        fake_env.write_text(
            "ANTHROPIC_BASE_URL=http://localhost:20128\nANTHROPIC_MODEL=serve\nANTHROPIC_API_KEY=sk-test-key\n",
            encoding="utf-8"
        )

        with patch("sys.frozen", True, create=True), patch("sys.executable", str(fake_exe)):
            settings = Settings()
            assert settings.env_path == fake_env
            assert settings.anthropic_api_key == "sk-test-key"
            assert settings.anthropic_model == "serve"
            print(f"    [OK] Resolved .env from executable parent: {settings.env_path}")
            print(f"    [OK] Loaded credentials under frozen environment")

    print("\n[RESULT] Frozen Path Resolution: PASSED")
    return True


def main():
    """Run all Step 3 verification tests."""
    print("=" * 60)
    print("AI Desktop Assistant - Step 3 Final Verification Tests")
    print("=" * 60)

    results = []

    try:
        results.append(("Window Control Tool", test_window_control_tool()))
    except Exception as e:
        print(f"\n[FAIL] Window Control Tool: {e}")
        import traceback
        traceback.print_exc()
        results.append(("Window Control Tool", False))

    try:
        results.append(("Targeted Code Edit Tool", test_code_edit_tool()))
    except Exception as e:
        print(f"\n[FAIL] Targeted Code Edit Tool: {e}")
        import traceback
        traceback.print_exc()
        results.append(("Targeted Code Edit Tool", False))

    try:
        results.append(("Web Fetch Tool", test_web_fetch_tool()))
    except Exception as e:
        print(f"\n[FAIL] Web Fetch Tool: {e}")
        import traceback
        traceback.print_exc()
        results.append(("Web Fetch Tool", False))

    try:
        results.append(("Task Audit Log Viewer", test_audit_log_tab()))
    except Exception as e:
        print(f"\n[FAIL] Task Audit Log Viewer: {e}")
        import traceback
        traceback.print_exc()
        results.append(("Task Audit Log Viewer", False))

    try:
        results.append(("Frozen Path Resolution", test_frozen_path_resolution()))
    except Exception as e:
        print(f"\n[FAIL] Frozen Path Resolution: {e}")
        import traceback
        traceback.print_exc()
        results.append(("Frozen Path Resolution", False))

    # Summary
    print("\n" + "=" * 60)
    print("STEP 3 TEST SUMMARY")
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
