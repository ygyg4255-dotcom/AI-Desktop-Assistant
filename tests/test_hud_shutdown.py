"""stop_hud_server: flags the HUD Offline, releases the port, and is idempotent."""
import socket
import time

import ui_server
from src.services.hud_status import get_hud_status


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _can_connect(port: int) -> bool:
    with socket.socket() as s:
        s.settimeout(0.5)
        return s.connect_ex(("127.0.0.1", port)) == 0


def test_stop_hud_server_flags_offline_and_frees_port(tmp_path, monkeypatch):
    monkeypatch.setenv("ALTRON_DATA_DIR", str(tmp_path))
    port = _free_port()
    thread = ui_server.start_hud_server(host="127.0.0.1", port=port)
    deadline = time.monotonic() + 3
    while not _can_connect(port) and time.monotonic() < deadline:
        time.sleep(0.05)
    assert _can_connect(port)

    ui_server.stop_hud_server(grace=0)

    assert get_hud_status()["state"] == "Offline"
    assert not thread.is_alive()
    assert not _can_connect(port)
    assert ui_server._SERVER is None


def test_stop_hud_server_is_idempotent(tmp_path, monkeypatch):
    monkeypatch.setenv("ALTRON_DATA_DIR", str(tmp_path))
    ui_server.stop_hud_server(grace=0)
    ui_server.stop_hud_server(grace=0)
    assert get_hud_status()["state"] == "Offline"
