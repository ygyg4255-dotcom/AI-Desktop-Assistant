"""Test OmniRoute connectivity and AI client."""
import sys
import time
from pathlib import Path

# Add project root to path
sys.path.insert(0, str(Path(__file__).parent))

from src.config import Settings
from src.services import AIClient, Message, ConnectionStatus


def test_omniroute():
    """Test OmniRoute connection and messaging."""
    print("=" * 60)
    print("OmniRoute Connection Test")
    print("=" * 60)

    # Load settings
    settings = Settings()
    print(f"\n[Configuration]")
    print(f"  Base URL: {settings.anthropic_base_url}")
    print(f"  Model: {settings.anthropic_model}")
    print(f"  API Key: {'*' * 10}...{settings.anthropic_api_key[-6:] if len(settings.anthropic_api_key) > 6 else 'Not set'}")

    # Create client
    print(f"\n[1] Creating AI client...")
    client = AIClient(
        base_url=settings.anthropic_base_url,
        api_key=settings.anthropic_api_key,
        model=settings.anthropic_model,
        max_tokens=100,
        timeout=30.0
    )
    print("    [OK] Client created")

    # Test connection
    print(f"\n[2] Testing connection to OmniRoute...")
    try:
        connected = client.test_connection()
        if connected:
            print(f"    [OK] Connected - Status: {client.status.value}")
        else:
            print(f"    [FAIL] Could not connect - Status: {client.status.value}")
            if client.last_error:
                print(f"    Error: {client.last_error}")
            return False
    except Exception as e:
        print(f"    [FAIL] Connection error: {e}")
        return False

    # Send test message
    print(f"\n[3] Sending test message with model 'serve'...")
    test_messages = [
        Message(role="user", content="Say 'Hello from OmniRoute' and nothing else.", timestamp=time.time())
    ]

    response_received = {"done": False, "response": None, "error": None}

    def on_complete(response):
        response_received["response"] = response
        response_received["done"] = True
        print(f"    [OK] Response received!")

    def on_error(error):
        response_received["error"] = error
        response_received["done"] = True
        print(f"    [ERROR] {error}")

    future = client.send_message(
        messages=test_messages,
        system="Reply exactly as requested. Be brief.",
        on_complete=on_complete,
        on_error=on_error
    )

    # Wait for response
    print("    Waiting for response...")
    start_time = time.time()
    while not response_received["done"] and (time.time() - start_time) < 30:
        time.sleep(0.1)

    if not response_received["done"]:
        print("    [FAIL] Timeout waiting for response")
        return False

    if response_received["error"]:
        print(f"    [FAIL] Error: {response_received['error']}")
        return False

    response = response_received["response"]
    print(f"\n[4] Response Details:")
    print(f"    Model used: {response.model}")
    print(f"    Stop reason: {response.stop_reason}")
    print(f"    Usage: {response.usage}")
    print(f"    Content: {response.content[:200]}{'...' if len(response.content) > 200 else ''}")

    # Verify "serve" was used
    print(f"\n[5] Verification:")
    if response.model == "serve" or "serve" in response.model:
        print("    [OK] Model 'serve' confirmed")
    else:
        print(f"    [INFO] Model reported as: {response.model}")

    print("\n" + "=" * 60)
    print("OmniRoute connection test PASSED!")
    print("=" * 60)
    return True


if __name__ == "__main__":
    success = test_omniroute()
    sys.exit(0 if success else 1)
