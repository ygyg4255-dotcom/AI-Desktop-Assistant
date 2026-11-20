"""Daily-driver tools: media, Wi-Fi/Bluetooth, power, to-do/notes, web search and URLs.

Every OS or network call is mocked: no media key is pressed, no adapter is changed, no PC is
shut down, and no request leaves the machine. The notes store lives in a temp dir.
"""
import subprocess
from unittest.mock import MagicMock

import pytest

from src.agent import tools as tools_mod
from src.agent.executor import AgentExecutor
from src.agent.permissions import PermissionGate
from src.agent.tools import create_default_registry
from src.services import media_service, network_service, notes_service, web_search_service
from src.services.media_service import MediaError
from src.services.network_service import NetworkError
from src.services.notes_service import NotesService

DAILY_TOOLS = {
    "media_control", "play_music", "network_control", "power_control",
    "todo_add", "todo_list", "todo_complete", "todo_delete",
    "note_save", "note_list", "note_read", "open_url", "web_search",
}


@pytest.fixture(autouse=True)
def _isolated_runtime(tmp_path, monkeypatch):
    monkeypatch.setenv("ALTRON_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setattr(notes_service, "_service", NotesService(tmp_path / "notes.json"))
    yield
    notes_service.reset_notes_service()


# --- registration and permission gate ----------------------------------

def test_all_daily_tools_are_registered_with_schemas():
    registry = create_default_registry()
    names = {t.name for t in registry.list_tools()}
    assert DAILY_TOOLS <= names
    schemas = {s["name"]: s for s in registry.get_tools_schema()}
    for name in DAILY_TOOLS:
        assert schemas[name]["input_schema"]["type"] == "object", name


def test_daily_tools_need_no_approval_and_are_not_dangerous():
    registry = create_default_registry()
    gate = PermissionGate()
    for name in DAILY_TOOLS:
        tool = registry.get(name)
        assert tool.requires_permission is False, name
        assert not gate.requires_permission(name), name
        assert name in PermissionGate.SAFE_TOOLS, name


def test_file_delete_and_move_still_require_approval():
    registry = create_default_registry()
    assert registry.get("file_delete").requires_permission is True
    assert registry.get("file_move").requires_permission is True


def test_executor_runs_daily_tool_without_asking(monkeypatch):
    """Through the real gate: no permission_callback, no pending request, handler executed."""
    executor = AgentExecutor(ai_client=MagicMock(), database=None)
    seen = []
    monkeypatch.setattr(media_service, "press_playback", lambda action: seen.append(action))

    result = executor._execute_tool_with_permission("media_control", {"action": "next"})

    assert result.success and not result.requires_permission
    assert executor.pending_permission is None
    assert seen == ["next"]


# --- media ----------------------------------------------------------------

def test_press_playback_sends_the_right_media_key():
    sent = []
    media_service.press_playback("play_pause", sender=sent.append)
    media_service.press_playback("next", sender=sent.append)
    media_service.press_playback("previous", sender=sent.append)
    media_service.press_playback("stop", sender=sent.append)
    assert sent == [0xB3, 0xB0, 0xB1, 0xB2]


def test_press_playback_rejects_unknown_action():
    with pytest.raises(MediaError):
        media_service.press_playback("eject", sender=lambda vk: None)


def test_now_playing_falls_back_when_winsdk_missing():
    def no_winsdk():
        raise ImportError("winsdk")
    info = media_service.now_playing(reader=no_winsdk)
    assert info["available"] is False and "winsdk" in info["message"]


def test_now_playing_reports_no_session_and_errors_without_raising():
    assert media_service.now_playing(reader=lambda: None)["available"] is False

    def broken():
        raise RuntimeError("gsmtc exploded")
    info = media_service.now_playing(reader=broken)
    assert info["available"] is False


def test_now_playing_returns_track_details():
    info = media_service.now_playing(reader=lambda: {"title": "Song", "artist": "Band",
                                                     "album": "", "status": "playing"})
    assert info == {"available": True, "title": "Song", "artist": "Band", "album": "", "status": "playing"}


def test_media_control_handler_sends_key_and_reports(monkeypatch):
    sent = []
    monkeypatch.setattr(media_service, "send_media_key", sent.append)
    result = tools_mod.handle_media_control({"action": "play_pause"})
    assert result.success and sent == [0xB3]
    assert "play/pause" in result.output["message"]


def test_media_control_now_playing_message(monkeypatch):
    monkeypatch.setattr(media_service, "now_playing",
                        lambda: {"available": True, "title": "Halo", "artist": "Beyonce", "status": "playing"})
    result = tools_mod.handle_media_control({"action": "now_playing"})
    assert "Halo by Beyonce" in result.output["message"]


def test_media_control_rejects_unknown_action():
    assert tools_mod.handle_media_control({"action": "explode"}).success is False


def test_media_control_surfaces_platform_error(monkeypatch):
    def boom(vk):
        raise MediaError("Media keys are only available on Windows.")
    monkeypatch.setattr(media_service, "send_media_key", boom)
    result = tools_mod.handle_media_control({"action": "stop"})
    assert result.success is False and "Windows" in result.error


# --- play_music -------------------------------------------------------------

def test_play_music_youtube_delegates_to_youtube_play(monkeypatch):
    calls = []
    monkeypatch.setattr(tools_mod, "handle_youtube_play",
                        lambda params: calls.append(params) or tools_mod.ToolResult(True, {"ok": 1}))
    tools_mod.handle_play_music({"query": "lofi beats"})
    assert calls == [{"query": "lofi beats"}]


def test_play_music_spotify_uses_app_uri(monkeypatch):
    started = []
    monkeypatch.setattr(tools_mod.os, "startfile", started.append, raising=False)
    result = tools_mod.handle_play_music({"query": "daft punk", "service": "spotify"})
    assert result.success and started == ["spotify:search:daft%20punk"]


def test_play_music_spotify_falls_back_to_web_player(monkeypatch):
    def no_app(uri):
        raise OSError("no handler")
    opened = []
    monkeypatch.setattr(tools_mod.os, "startfile", no_app, raising=False)
    monkeypatch.setattr(tools_mod.webbrowser, "open", lambda url, new=0: opened.append(url) or True)
    assert tools_mod.handle_play_music({"query": "x y", "service": "spotify"}).success
    assert opened == ["https://open.spotify.com/search/x%20y"]


def test_play_music_validation():
    assert tools_mod.handle_play_music({"query": "  "}).success is False
    assert tools_mod.handle_play_music({"query": "a\nb"}).success is False
    assert tools_mod.handle_play_music({"query": "x" * 201}).success is False
    assert tools_mod.handle_play_music({"query": "x", "service": "napster"}).success is False


# --- Wi-Fi / Bluetooth ----------------------------------------------------

NETSH_INTERFACES = """There is 1 interface on the system:

    Name                   : Wi-Fi
    Description            : Intel(R) Wi-Fi 6 AX201
    State                  : connected
    SSID                   : HomeNet
    BSSID                  : aa:bb:cc:dd:ee:ff
    Signal                 : 88%
"""

NETSH_PROFILES = """Profiles on interface Wi-Fi:

Group policy profiles (read only)
---------------------------------
    <None>

User profiles
-------------
    All User Profile     : HomeNet
    All User Profile     : Cafe 5G
"""


def test_wifi_status_parses_netsh_output(monkeypatch):
    monkeypatch.setattr(network_service, "_run", lambda argv: NETSH_INTERFACES)
    info = network_service.wifi_status()
    assert info == {"adapter": "Wi-Fi", "state": "connected", "ssid": "HomeNet", "signal": "88%"}


def test_wifi_status_without_adapter_raises(monkeypatch):
    monkeypatch.setattr(network_service, "_run", lambda argv: "There is no wireless interface on the system.")
    with pytest.raises(NetworkError):
        network_service.wifi_status()


def test_wifi_known_networks_parses_profiles(monkeypatch):
    monkeypatch.setattr(network_service, "_run", lambda argv: NETSH_PROFILES)
    assert network_service.wifi_known_networks() == ["HomeNet", "Cafe 5G"]


def test_wifi_connect_only_joins_saved_networks(monkeypatch):
    calls = []
    monkeypatch.setattr(network_service, "_run", lambda argv: calls.append(argv) or "")
    monkeypatch.setattr(network_service, "wifi_known_networks", lambda: ["HomeNet"])

    assert network_service.wifi_connect("HomeNet") == "HomeNet"
    assert calls == [["netsh", "wlan", "connect", 'name="HomeNet"']]

    calls.clear()
    with pytest.raises(NetworkError, match="No saved Wi-Fi profile"):
        network_service.wifi_connect("Evil & calc")
    assert calls == []


def test_wifi_ssid_validation():
    with pytest.raises(NetworkError):
        network_service.validate_ssid("")
    with pytest.raises(NetworkError):
        network_service.validate_ssid("x" * 33)
    with pytest.raises(NetworkError):
        network_service.validate_ssid("bad\x01name")


def test_wifi_toggle_targets_detected_adapter_with_fixed_argv(monkeypatch):
    calls = []
    monkeypatch.setattr(network_service, "_run", lambda argv: calls.append(argv) or NETSH_INTERFACES)
    network_service.wifi_set_enabled(False)
    assert calls[-1] == ["netsh", "interface", "set", "interface", 'name="Wi-Fi"', "admin=disabled"]


def test_run_never_uses_a_shell_and_maps_elevation_errors(monkeypatch):
    seen = {}

    def fake_run(argv, **kwargs):
        seen.update(kwargs)
        return subprocess.CompletedProcess(argv, 1, stdout="",
                                           stderr="The requested operation requires elevation.")

    monkeypatch.setattr(network_service.shutil, "which", lambda name: "C:\\Windows\\System32\\netsh.exe")
    monkeypatch.setattr(network_service.subprocess, "run", fake_run)

    with pytest.raises(NetworkError, match="administrator"):
        network_service._run(["netsh", "interface", "set", "interface", "name=x", "admin=enabled"])
    assert not seen.get("shell")


def test_run_reports_missing_executable(monkeypatch):
    monkeypatch.setattr(network_service.shutil, "which", lambda name: None)
    with pytest.raises(NetworkError, match="not available"):
        network_service._run(["netsh", "wlan", "show", "interfaces"])


def test_bluetooth_status_parses_json(monkeypatch):
    monkeypatch.setattr(network_service, "_powershell", lambda script: (
        '{"service":"Running","adapters":{"FriendlyName":"Intel Bluetooth","Status":"OK"}}'))
    info = network_service.bluetooth_status()
    assert info["service"] == "Running"
    assert info["adapters"] == [{"name": "Intel Bluetooth", "status": "OK"}]


def test_bluetooth_devices_handles_empty_and_single(monkeypatch):
    monkeypatch.setattr(network_service, "_powershell", lambda script: "")
    assert network_service.bluetooth_devices() == []
    monkeypatch.setattr(network_service, "_powershell", lambda script: '{"FriendlyName":"Headset","InstanceId":"BTH\\\\1"}')
    assert network_service.bluetooth_devices() == [{"name": "Headset", "id": "BTH\\1"}]


def test_bluetooth_toggle_counts_adapters(monkeypatch):
    monkeypatch.setattr(network_service, "_powershell", lambda script: "2")
    assert network_service.bluetooth_set_enabled(True) == 2


def test_network_handler_dispatch_and_errors(monkeypatch):
    monkeypatch.setattr(network_service, "wifi_status", lambda: {
        "adapter": "Wi-Fi", "state": "connected", "ssid": "HomeNet", "signal": "88%"})
    result = tools_mod.handle_network_control({"action": "wifi_status"})
    assert result.success and "HomeNet" in result.output["message"]

    def denied(enabled):
        raise NetworkError("Altron must run as administrator for it.")
    monkeypatch.setattr(network_service, "bluetooth_set_enabled", denied)
    result = tools_mod.handle_network_control({"action": "bluetooth_off"})
    assert result.success is False and "administrator" in result.error

    monkeypatch.setattr(network_service, "bluetooth_set_enabled", lambda enabled: 0)
    assert tools_mod.handle_network_control({"action": "bluetooth_on"}).success is False

    assert tools_mod.handle_network_control({"action": "reboot_router"}).success is False


def test_network_handler_unparseable_output_is_an_error(monkeypatch):
    def bad():
        raise ValueError("junk")
    monkeypatch.setattr(network_service, "wifi_known_networks", bad)
    result = tools_mod.handle_network_control({"action": "wifi_networks"})
    assert result.success is False and "Could not read" in result.error


# --- power ----------------------------------------------------------------

@pytest.fixture
def power_calls(monkeypatch):
    calls = {"system": [], "lock": 0, "sleep": 0}

    def fake_system(argv):
        calls["system"].append(argv)
        return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")

    def fake_lock():
        calls["lock"] += 1
        return True

    def fake_sleep():
        calls["sleep"] += 1

    monkeypatch.setattr(tools_mod, "_run_system_command", fake_system)
    monkeypatch.setattr(tools_mod, "_lock_workstation", fake_lock)
    monkeypatch.setattr(tools_mod, "_start_sleep", fake_sleep)
    return calls


def test_power_lock_and_sleep(power_calls):
    assert tools_mod.handle_power_control({"action": "lock"}).success
    assert tools_mod.handle_power_control({"action": "sleep"}).success
    assert power_calls["lock"] == 1 and power_calls["sleep"] == 1


def test_power_shutdown_immediate_and_delayed(power_calls):
    assert tools_mod.handle_power_control({"action": "shutdown"}).success
    assert tools_mod.handle_power_control({"action": "shutdown", "delay_minutes": 5}).success
    assert power_calls["system"] == [["shutdown", "/s", "/t", "0"], ["shutdown", "/s", "/t", "300"]]


def test_power_restart_and_cancel(power_calls):
    assert tools_mod.handle_power_control({"action": "restart", "delay_minutes": 2}).success
    assert tools_mod.handle_power_control({"action": "cancel_shutdown"}).success
    assert power_calls["system"] == [["shutdown", "/r", "/t", "120"], ["shutdown", "/a"]]


def test_power_rejects_bad_input(power_calls):
    assert tools_mod.handle_power_control({"action": "hibernate_everything"}).success is False
    assert tools_mod.handle_power_control({"action": "shutdown", "delay_minutes": 2000}).success is False
    assert tools_mod.handle_power_control({"action": "shutdown", "delay_minutes": -1}).success is False
    assert tools_mod.handle_power_control({"action": "shutdown", "delay_minutes": "soon"}).success is False
    assert tools_mod.handle_power_control({"action": "lock", "delay_minutes": 3}).success is False
    assert power_calls == {"system": [], "lock": 0, "sleep": 0}


def test_power_reports_failed_shutdown(monkeypatch):
    monkeypatch.setattr(tools_mod, "_run_system_command", lambda argv: subprocess.CompletedProcess(
        argv, 1, stdout="", stderr="A shutdown is not scheduled."))
    result = tools_mod.handle_power_control({"action": "cancel_shutdown"})
    assert result.success is False and "not scheduled" in result.error


def test_power_lock_failure_is_reported(monkeypatch):
    monkeypatch.setattr(tools_mod, "_lock_workstation", lambda: False)
    assert tools_mod.handle_power_control({"action": "lock"}).success is False


# --- to-do and notes handlers -------------------------------------------------

def test_todo_handlers_round_trip():
    added = tools_mod.handle_todo_add({"task": "Call dentist", "due": "tomorrow 9am"})
    assert added.success and "Call dentist" in added.output["message"]
    task_id = added.output["todo"]["id"]

    listed = tools_mod.handle_todo_list({})
    assert [t["id"] for t in listed.output["todos"]] == [task_id]

    assert tools_mod.handle_todo_complete({"task_id": task_id}).success
    assert tools_mod.handle_todo_list({}).output["todos"] == []
    assert len(tools_mod.handle_todo_list({"include_done": "true"}).output["todos"]) == 1

    assert tools_mod.handle_todo_delete({"task_id": task_id}).success
    assert tools_mod.handle_todo_delete({"task_id": task_id}).success is False


def test_todo_add_requires_task():
    assert tools_mod.handle_todo_add({"task": ""}).success is False


def test_note_handlers_round_trip():
    saved = tools_mod.handle_note_save({"title": "Gym", "content": "Leg day"})
    assert saved.success and saved.output["note"]["title"] == "Gym"
    assert tools_mod.handle_note_list({}).output["notes"][0]["title"] == "Gym"
    read = tools_mod.handle_note_read({"title_or_id": "gym"})
    assert read.output["note"]["content"] == "Leg day"
    assert tools_mod.handle_note_read({"title_or_id": "missing"}).success is False


# --- open_url ---------------------------------------------------------------

@pytest.mark.parametrize("url", [
    "file:///C:/Windows/win.ini",
    "javascript:alert(1)",
    "ms-settings:bluetooth",
    "https://user:pw@example.com/",
    "https://example.com/has space",
    "https:///nohost",
    "",
])
def test_open_url_rejects_unsafe_or_malformed(url, monkeypatch):
    opened = []
    monkeypatch.setattr(tools_mod.webbrowser, "open", lambda *a, **k: opened.append(a) or True)
    assert tools_mod.handle_open_url({"url": url}).success is False
    assert opened == []


def test_open_url_opens_https_in_default_browser(monkeypatch):
    opened = []
    monkeypatch.setattr(tools_mod.webbrowser, "open", lambda url, new=0: opened.append((url, new)) or True)
    result = tools_mod.handle_open_url({"url": "https://example.com/docs?q=1"})
    assert result.success
    assert opened == [("https://example.com/docs?q=1", 2)]


def test_open_url_reports_browser_failure(monkeypatch):
    monkeypatch.setattr(tools_mod.webbrowser, "open", lambda *a, **k: False)
    assert tools_mod.handle_open_url({"url": "https://example.com"}).success is False


# --- web_search ---------------------------------------------------------------

DDG_HTML = """
<div class="result">
  <h2><a rel="nofollow" class="result__a"
     href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fen.wikipedia.org%2Fwiki%2FPython&amp;rut=abc">Python &amp; <b>friends</b></a></h2>
  <a class="result__snippet" href="x">Python is a <b>programming</b> language.</a>
</div>
<div class="result">
  <a class="result__a" href="javascript:alert(1)">Bad link</a>
  <a class="result__snippet" href="x">Should be skipped.</a>
</div>
<div class="result">
  <a class="result__a" href="https://example.org/direct">Direct result</a>
  <a class="result__snippet" href="x">A direct snippet.</a>
</div>
"""


def test_parse_results_unwraps_redirects_and_drops_unsafe_links():
    results = web_search_service.parse_results(DDG_HTML, limit=5)
    assert [r["url"] for r in results] == ["https://en.wikipedia.org/wiki/Python", "https://example.org/direct"]
    assert results[0]["title"] == "Python & friends"
    assert results[0]["snippet"] == "Python is a programming language."
    assert results[1]["snippet"] == "A direct snippet."  # snippets pair by position, skipped links included


def test_parse_results_respects_limit_and_dedupes():
    dup = DDG_HTML + DDG_HTML
    assert len(web_search_service.parse_results(dup, limit=1)) == 1
    assert len(web_search_service.parse_results(dup, limit=10)) == 2


def test_search_builds_encoded_query_and_clamps_limit():
    seen = []

    def fake_fetch(url):
        seen.append(url)
        return DDG_HTML

    results = web_search_service.search("best pizza & pasta", fetch=fake_fetch, limit=99)
    assert seen == ["https://html.duckduckgo.com/html/?q=best+pizza+%26+pasta"]
    assert len(results) == 2


@pytest.mark.parametrize("query", ["", "   ", "x" * 201, "bad\x00query"])
def test_search_rejects_bad_query(query):
    with pytest.raises(web_search_service.WebSearchError):
        web_search_service.search(query, fetch=lambda url: "")


def test_web_search_handler_returns_results(monkeypatch):
    monkeypatch.setattr(tools_mod, "_public_get_text", lambda url: DDG_HTML)
    result = tools_mod.handle_web_search({"query": "python", "max_results": 1})
    assert result.success
    assert len(result.output["results"]) == 1
    assert "top_page" not in result.output


def test_web_search_fetch_top_includes_page_text(monkeypatch):
    monkeypatch.setattr(tools_mod, "_public_get_text", lambda url: DDG_HTML)
    fetched = []

    def fake_fetch(params):
        fetched.append(params)
        return tools_mod.ToolResult(True, {"url": params["url"], "text": "Page body"})

    monkeypatch.setattr(tools_mod, "handle_web_fetch", fake_fetch)
    result = tools_mod.handle_web_search({"query": "python", "fetch_top": True})
    assert fetched[0]["url"] == "https://en.wikipedia.org/wiki/Python"
    assert result.output["top_page"]["text"] == "Page body"


def test_web_search_no_results_is_a_success(monkeypatch):
    monkeypatch.setattr(tools_mod, "_public_get_text", lambda url: "<html>nothing</html>")
    result = tools_mod.handle_web_search({"query": "zzqx"})
    assert result.success and result.output["results"] == []
    assert "No results" in result.output["message"]


def test_web_search_network_failure_is_reported_not_raised(monkeypatch):
    def offline(url):
        raise OSError("network unreachable")
    monkeypatch.setattr(tools_mod, "_public_get_text", offline)
    result = tools_mod.handle_web_search({"query": "python"})
    assert result.success is False and "failed" in result.error


def test_web_search_rejects_empty_query_before_fetching(monkeypatch):
    called = MagicMock()
    monkeypatch.setattr(tools_mod, "_public_get_text", called)
    assert tools_mod.handle_web_search({"query": ""}).success is False
    called.assert_not_called()
