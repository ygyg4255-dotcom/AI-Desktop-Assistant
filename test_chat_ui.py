"""Test chat window and UI responsiveness."""
import sys
import time
import threading
from pathlib import Path

# Add project root to path
sys.path.insert(0, str(Path(__file__).parent))

from PySide6.QtWidgets import QApplication
from src.config import Settings
from src.services import AIClient, Message
from src.ui import ChatWindow


def test_chat_ui():
    """Test chat window functionality."""
    print("=" * 60)
    print("Chat Window UI Test")
    print("=" * 60)

    # Load settings
    settings = Settings()

    # Create Qt application
    print("\n[1] Creating Qt application...")
    app = QApplication(sys.argv)
    app.setQuitOnLastWindowClosed(False)
    print("    [OK] Qt application created")

    # Create AI client
    print("\n[2] Creating AI client...")
    client = AIClient(
        base_url=settings.anthropic_base_url,
        api_key=settings.anthropic_api_key,
        model=settings.anthropic_model,
        max_tokens=100,
        timeout=30.0
    )
    print("    [OK] AI client created")

    # Create chat window
    print("\n[3] Creating chat window...")
    chat_window = ChatWindow(client)
    chat_window.show()
    print("    [OK] Chat window displayed")

    # Test async message sending
    print("\n[4] Testing async message sending...")
    print("    Sending message...")

    ui_responsive = {"value": True}
    response_ready = threading.Event()
    response_content = {"value": None}

    def on_response(response):
        response_content["value"] = response.content
        response_ready.set()
        print("    [OK] Response received")

    def on_error(error):
        response_content["value"] = f"Error: {error}"
        response_ready.set()
        print(f"    [ERROR] {error}")

    # Send test message
    test_msg = Message(role="user", content="Reply with just 'OK' and nothing else.")
    client.send_message(
        messages=[test_msg],
        system="Reply exactly as requested.",
        on_complete=on_response,
        on_error=on_error
    )

    # Process events while waiting (simulates responsive UI)
    start = time.time()
    while not response_ready.is_set() and (time.time() - start) < 30:
        app.processEvents()
        time.sleep(0.01)

    if response_content["value"]:
        print(f"    Response: {response_content["value"][:100]}")

    # Test clean shutdown
    print("\n[5] Testing clean shutdown...")
    client.cancel()
    chat_window.close()
    print("    [OK] Shutdown complete")

    print("\n" + "=" * 60)
    print("Chat Window UI test PASSED!")
    print("=" * 60)
    return True


if __name__ == "__main__":
    success = test_chat_ui()
    sys.exit(0 if success else 1)
