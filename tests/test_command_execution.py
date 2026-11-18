"""S7: no shell=True in app launching / OmniRoute spawning."""
import subprocess
from unittest import mock

from src.agent import tools
from src.services.omniroute_manager import OmniRouteManager


def _force_windows_fallback(monkeypatch):
    monkeypatch.setattr(tools.platform, "system", lambda: "Windows")
    monkeypatch.setattr(tools.os, "startfile", mock.Mock(side_effect=OSError("no")), raising=False)


def test_app_open_fallback_uses_arg_list_without_shell(monkeypatch):
    _force_windows_fallback(monkeypatch)
    monkeypatch.setattr(tools.shutil, "which", lambda n: r"C:\Apps\foo.exe")
    popen = mock.Mock()
    monkeypatch.setattr(tools.subprocess, "Popen", popen)
    res = tools.handle_app_open({"app_name": "foo"}, approved=True)
    assert res.success
    args, kwargs = popen.call_args
    assert args[0] == [r"C:\Apps\foo.exe"]
    assert not kwargs.get("shell")


def test_app_open_injection_name_never_reaches_a_shell(monkeypatch):
    _force_windows_fallback(monkeypatch)
    monkeypatch.setattr(tools.shutil, "which", lambda n: None)
    popen = mock.Mock()
    monkeypatch.setattr(tools.subprocess, "Popen", popen)
    res = tools.handle_app_open({"app_name": 'x" & calc & "'}, approved=True)
    assert not res.success
    popen.assert_not_called()


def test_omniroute_spawn_uses_arg_list_without_shell(monkeypatch):
    import src.services.omniroute_manager as om
    mgr = OmniRouteManager()
    monkeypatch.setattr(mgr, "is_running", lambda *a, **k: False)
    monkeypatch.setattr(om.shutil, "which", lambda n: r"C:\npm\omniroute.CMD")
    popen = mock.Mock()
    monkeypatch.setattr(om.subprocess, "Popen", popen)
    monkeypatch.setattr(om.time, "sleep", lambda s: None)
    mgr.POLL_TIMEOUT_SECONDS = 0
    mgr.start_if_needed()
    args, kwargs = popen.call_args
    assert args[0] == [r"C:\npm\omniroute.CMD"]
    assert not kwargs.get("shell")


def test_omniroute_missing_binary_returns_false(monkeypatch):
    import src.services.omniroute_manager as om
    mgr = OmniRouteManager()
    monkeypatch.setattr(mgr, "is_running", lambda *a, **k: False)
    monkeypatch.setattr(om.shutil, "which", lambda n: None)
    assert mgr.start_if_needed() is False
