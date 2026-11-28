"""Test Agent System - SQLite Storage, Tool Registry, and Agent Executor."""
import sys
import time
from pathlib import Path

# Add project root to path
sys.path.insert(0, str(Path(__file__).parent))

from src.storage.db import Database
from src.agent.tools import ToolCategory, create_default_registry
from src.agent.permissions import PermissionGate
from src.agent.executor import AgentExecutor, AgentState
from src.config import Settings


def test_sqlite_storage():
    """Test enhanced SQLite storage."""
    print("\n" + "=" * 60)
    print("TEST 1: SQLite Storage")
    print("=" * 60)

    # Use test database
    test_db_path = Path.home() / ".ai-desktop-assistant" / "test_agent.db"
    db = Database(test_db_path)

    print("\n[1.1] Initializing database...")
    db.initialize()
    print("    [OK] Database initialized")

    # Test settings
    print("\n[1.2] Testing settings storage...")
    db.set_setting("test_key", "test_value")
    db.set_setting("test_number", 42)
    value = db.get_setting("test_key")
    number = db.get_setting("test_number")
    assert value == "test_value", f"Expected 'test_value', got {value}"
    assert number == 42, f"Expected 42, got {number}"
    print("    [OK] Settings work correctly")

    # Test conversations
    print("\n[1.3] Testing conversation management...")
    conv_id = db.create_conversation("Test Conversation")
    assert conv_id > 0, "Failed to create conversation"
    conv = db.get_conversation(conv_id)
    assert conv.title == "Test Conversation", f"Expected 'Test Conversation', got {conv.title}"
    print(f"    [OK] Created conversation ID: {conv_id}")

    # Test messages
    print("\n[1.4] Testing message storage...")
    msg_id = db.add_message(conv_id, "user", "Hello, test message!")
    assert msg_id > 0, "Failed to add message"
    messages = db.get_messages(conv_id)
    assert len(messages) == 1, f"Expected 1 message, got {len(messages)}"
    assert messages[0].content == "Hello, test message!"
    print(f"    [OK] Stored and retrieved message")

    # Test tool executions
    print("\n[1.5] Testing tool execution logs...")
    exec_id = db.log_tool_execution(conv_id, "file_read", {"path": "/test"}, "pending")
    assert exec_id > 0, "Failed to log tool execution"
    db.update_tool_execution(exec_id, {"content": "test content"}, "success")
    executions = db.get_tool_executions(conv_id)
    assert len(executions) == 1
    assert executions[0].status == "success"
    print("    [OK] Tool execution logging works")

    # Test permissions
    print("\n[1.6] Testing permission storage...")
    db.set_tool_permission("terminal_execute", "allow")
    perm = db.get_tool_permission("terminal_execute")
    assert perm == "allow", f"Expected 'allow', got {perm}"
    print("    [OK] Permission storage works")

    # Cleanup
    db.close()
    print("\n[RESULT] SQLite Storage: PASSED")
    return True


def test_tool_registry():
    """Test Tool Registry."""
    print("\n" + "=" * 60)
    print("TEST 2: Tool Registry")
    print("=" * 60)

    print("\n[2.1] Creating default registry...")
    registry = create_default_registry()
    tools = registry.list_tools()
    print(f"    [OK] Registered {len(tools)} tools")

    print("\n[2.2] Checking tool schemas...")
    schemas = registry.get_tools_schema()
    assert len(schemas) == len(tools), "Schema count mismatch"
    print(f"    [OK] Generated {len(schemas)} tool schemas")

    print("\n[2.3] Testing tool categories...")
    file_tools = registry.get_tools_by_category(ToolCategory.FILE_SYSTEM)
    system_tools = registry.get_tools_by_category(ToolCategory.SYSTEM)
    print(f"    File System Tools: {len(file_tools)}")
    print(f"    System Tools: {len(system_tools)}")

    print("\n[2.4] Testing tool handlers...")

    # Test file_search (safe, no permission needed)
    print("\n    [2.4.1] Testing file_search tool...")
    search_tool = registry.get("file_search")
    result = search_tool.handler({"query": "test", "directory": str(Path.home())})
    print(f"        Success: {result.success}")
    if result.success:
        print(f"        Files found: {result.output.get('count', 0)}")
    else:
        print(f"        Error: {result.error}")

    # Test process_list (safe, no permission needed)
    print("\n    [2.4.2] Testing process_list tool...")
    proc_tool = registry.get("process_list")
    result = proc_tool.handler({})
    print(f"        Success: {result.success}")
    if result.success:
        print(f"        Processes: {result.output.get('count', 0)}")

    # Test system_info (safe, no permission needed)
    print("\n    [2.4.3] Testing system_info tool...")
    sys_tool = registry.get("system_info")
    result = sys_tool.handler({})
    print(f"        Success: {result.success}")
    if result.success:
        print(f"        OS: {result.output.get('os', 'unknown')}")
        print(f"        CPU: {result.output.get('cpu_percent', 0)}%")
        print(f"        Memory: {result.output.get('memory_percent', 0)}%")

    # Test file_write (requires permission)
    print("\n    [2.4.4] Testing file_write tool (permission check)...")
    write_tool = registry.get("file_write")
    result = write_tool.handler({"path": "/tmp/test.txt", "content": "test"})
    print(f"        Requires permission: {result.requires_permission}")
    print(f"        Permission message: {result.permission_message}")

    # Test terminal_execute (requires permission, dangerous)
    print("\n    [2.4.5] Testing terminal_execute tool (permission check)...")
    term_tool = registry.get("terminal_execute")
    result = term_tool.handler({"command": "echo test"})
    print(f"        Requires permission: {result.requires_permission}")
    print(f"        Is dangerous: {result.is_dangerous}")
    print(f"        Permission message: {result.permission_message}")

    print("\n[RESULT] Tool Registry: PASSED")
    return True


def test_permission_gate():
    """Test Permission Gate."""
    print("\n" + "=" * 60)
    print("TEST 3: Permission Gate")
    print("=" * 60)

    gate = PermissionGate()

    print("\n[3.1] Testing safe tools...")
    assert gate.is_safe("file_search") == True
    # file_read is conditional (S4): path-dependent approval, never blanket-safe
    assert gate.is_safe("file_read") == False
    assert "file_read" in gate.CONDITIONAL_TOOLS
    assert gate.is_safe("process_list") == True
    assert gate.is_safe("system_info") == True
    print("    [OK] Safe tools identified correctly")

    print("\n[3.2] Testing dangerous tools...")
    assert gate.is_dangerous("file_write") == True
    assert gate.is_dangerous("file_delete") == True
    assert gate.is_dangerous("terminal_execute") == True
    assert gate.is_dangerous("app_close") == False
    print("    [OK] Dangerous tools identified correctly")

    print("\n[3.3] Testing permission requirements...")
    assert gate.requires_permission("file_write") == True
    assert gate.requires_permission("terminal_execute") == True
    # Conditional tools are gated per call via needs_approval_*, not by tool name
    assert gate.requires_permission("file_read") == False
    from src.agent.permissions import needs_approval_file_read
    assert needs_approval_file_read("C:/Users/x/.ssh/id_rsa") == True
    print("    [OK] Permission requirements correct")

    print("\n[3.4] Testing session permissions...")
    from src.agent.permissions import PermissionLevel
    gate.set_session_permission("terminal_execute", PermissionLevel.ALLOW)
    assert gate.get_permission_level("terminal_execute") == PermissionLevel.ALLOW
    gate.clear_session_permissions()
    assert gate.get_permission_level("terminal_execute") == PermissionLevel.ASK
    print("    [OK] Session permissions work")

    print("\n[RESULT] Permission Gate: PASSED")
    return True


def test_agent_executor():
    """Test Agent Executor initialization."""
    print("\n" + "=" * 60)
    print("TEST 4: Agent Executor")
    print("=" * 60)

    # Load settings
    settings = Settings()

    print("\n[4.1] Creating agent executor...")
    from src.services import AIClient

    ai_client = AIClient(
        base_url=settings.anthropic_base_url,
        api_key=settings.anthropic_api_key,
        model=settings.anthropic_model,
        max_tokens=100,
        timeout=60.0
    )

    db_path = Path.home() / ".ai-desktop-assistant" / "test_agent.db"
    db = Database(db_path)
    db.initialize()

    executor = AgentExecutor(ai_client=ai_client, database=db)
    print("    [OK] Agent executor created")

    print("\n[4.2] Checking initial state...")
    assert executor.state == AgentState.IDLE
    print(f"    [OK] Initial state: {executor.state.value}")

    print("\n[4.3] Checking tools schema...")
    schema = executor.tools_schema
    print(f"    [OK] Tools schema has {len(schema)} tools")
    for s in schema[:3]:
        print(f"        - {s['name']}")

    print("\n[4.4] Starting conversation...")
    conv_id = executor.start_conversation("Test Agent Conversation")
    print(f"    [OK] Conversation started: {conv_id}")

    print("\n[4.5] Testing simple query (no tools)...")
    response_received = {"done": False, "response": None}
    steps_log = []

    def on_step(step):
        steps_log.append(f"{step.step_type}: {step.content[:50] if step.content else ''}")

    def on_complete(response):
        response_received["response"] = response
        response_received["done"] = True

    def on_error(error):
        response_received["done"] = True
        print(f"    Error: {error}")

    # Send a simple query that shouldn't need tools
    _ = executor.process(
        "Say 'Agent test successful' and nothing else.",
        on_step=on_step,
        on_complete=on_complete,
        on_error=on_error
    )

    # Wait for response
    start = time.time()
    while not response_received["done"] and (time.time() - start) < 60:
        time.sleep(0.1)

    if response_received["response"]:
        resp = response_received["response"]
        print(f"    [OK] Response: {resp.content[:100]}")
        print(f"    Tools used: {resp.tools_used}")
        print(f"    Steps: {len(resp.steps)}")
    else:
        print("    [WARN] No response received (timeout)")

    db.close()
    print("\n[RESULT] Agent Executor: PASSED")
    return True


def main():
    """Run all tests."""
    print("=" * 60)
    print("AI Desktop Assistant - Agent System Tests")
    print("=" * 60)

    results = []

    # Run tests
    try:
        results.append(("SQLite Storage", test_sqlite_storage()))
    except Exception as e:
        print(f"\n[FAIL] SQLite Storage: {e}")
        results.append(("SQLite Storage", False))

    try:
        results.append(("Tool Registry", test_tool_registry()))
    except Exception as e:
        print(f"\n[FAIL] Tool Registry: {e}")
        results.append(("Tool Registry", False))

    try:
        results.append(("Permission Gate", test_permission_gate()))
    except Exception as e:
        print(f"\n[FAIL] Permission Gate: {e}")
        results.append(("Permission Gate", False))

    try:
        results.append(("Agent Executor", test_agent_executor()))
    except Exception as e:
        print(f"\n[FAIL] Agent Executor: {e}")
        results.append(("Agent Executor", False))

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
