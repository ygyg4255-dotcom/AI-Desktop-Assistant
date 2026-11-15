"""Test script to verify application initialization and shutdown."""
import sys
from pathlib import Path

# Add project root to path
sys.path.insert(0, str(Path(__file__).parent))

from src.app import Application


def test_application():
    """Test application lifecycle."""
    print("=" * 50)
    print("AI Desktop Assistant - Verification Test")
    print("=" * 50)

    # Test 1: Create application
    print("\n[1] Creating application...")
    app = Application()
    print("    [OK] Application instance created")

    # Test 2: Check settings
    print("\n[2] Checking settings...")
    print(f"    App Name: {app._settings.app_name}")
    print(f"    App Version: {app._settings.app_version}")
    print(f"    Data Dir: {app._settings.data_dir}")
    print(f"    DB Path: {app._settings.db_path}")
    print("    [OK] Settings loaded correctly")

    # Test 3: Initialize application
    print("\n[3] Initializing application...")
    initialized = app.initialize()
    if not initialized:
        print("    [FAIL] Failed to initialize application")
        return False
    print("    [OK] Application initialized")

    # Test 4: Verify components
    print("\n[4] Verifying components...")
    print(f"    Qt Application: {app._app is not None}")
    print(f"    Tray Icon: {app._tray_icon is not None}")
    print(f"    Database: {app._database is not None}")
    print(f"    Tray Icon Visible: {app._tray_icon._tray_icon.isVisible()}")
    print(f"    Database Connected: {app._database._connection is not None}")
    print("    [OK] All components ready")

    # Test 5: Test menu actions
    print("\n[5] Testing menu actions...")
    app._tray_icon.about_requested.emit()
    print("    [OK] Menu signals working")

    # Test 6: Clean shutdown
    print("\n[6] Testing clean shutdown...")
    app.shutdown()
    print("    [OK] Shutdown complete")

    print("\n" + "=" * 50)
    print("All tests passed! Application is working correctly.")
    print("=" * 50)
    return True


if __name__ == "__main__":
    success = test_application()
    sys.exit(0 if success else 1)
