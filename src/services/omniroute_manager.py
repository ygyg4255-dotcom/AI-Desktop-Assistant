"""OmniRoute server lifecycle manager.

Automatically spawns and manages the OmniRoute proxy server on app launch.
"""
import subprocess
import time
import urllib.request
import urllib.error
from typing import Optional


class OmniRouteManager:
    """Manages OmniRoute server lifecycle.

    - On startup, checks if OmniRoute is already running at localhost:20128
    - If not running, spawns it silently in the background
    - On app exit, terminates only if we spawned it ourselves
    """

    DEFAULT_URL = "http://localhost:20128"
    DEFAULT_PORT = 20128
    POLL_TIMEOUT_SECONDS = 8
    POLL_INTERVAL_SECONDS = 0.5

    def __init__(self):
        self._process: Optional[subprocess.Popen] = None
        self._spawned_by_us: bool = False

    def is_running(self, url: str = DEFAULT_URL, timeout: float = 2.0) -> bool:
        """Check if OmniRoute server is reachable.

        Args:
            url: Base URL to check
            timeout: Request timeout in seconds

        Returns:
            True if server responds, False otherwise
        """
        try:
            req = urllib.request.Request(f"{url}/v1/models", method="GET")
            urllib.request.urlopen(req, timeout=timeout)
            return True
        except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, ConnectionError, OSError):
            return False

    def start_if_needed(self, url: str = DEFAULT_URL) -> bool:
        """Start OmniRoute if it's not already running.

        Args:
            url: Base URL to check/start

        Returns:
            True if server is running (either already or started by us), False on failure
        """
        # Already running?
        if self.is_running(url):
            self._spawned_by_us = False
            return True

        # Try to spawn omniroute in background
        try:
            # CREATE_NO_WINDOW = 0x08000000 - prevents console window on Windows
            self._process = subprocess.Popen(
                "omniroute",
                shell=True,
                creationflags=0x08000000,  # CREATE_NO_WINDOW
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL
            )
            self._spawned_by_us = True
        except (FileNotFoundError, OSError, subprocess.SubprocessError):
            self._spawned_by_us = False
            return False

        # Poll until server is ready
        elapsed = 0.0
        while elapsed < self.POLL_TIMEOUT_SECONDS:
            if self.is_running(url):
                return True
            time.sleep(self.POLL_INTERVAL_SECONDS)
            elapsed += self.POLL_INTERVAL_SECONDS

        # Timeout - server didn't start in time
        self.stop()
        return False

    def stop(self) -> None:
        """Stop the OmniRoute process if we spawned it."""
        if self._process is not None and self._spawned_by_us:
            try:
                self._process.terminate()
                # Give it a moment to exit gracefully
                try:
                    self._process.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    self._process.kill()
            except (OSError, subprocess.SubprocessError):
                pass
            finally:
                self._process = None
                self._spawned_by_us = False

    @property
    def spawned_by_us(self) -> bool:
        """Returns True if this manager spawned the OmniRoute process."""
        return self._spawned_by_us
