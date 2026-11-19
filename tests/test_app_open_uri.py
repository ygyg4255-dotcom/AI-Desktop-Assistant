"""handle_app_open: Windows Store / URI-protocol apps and Start Apps fallback."""
import subprocess
from unittest.mock import MagicMock

import pytest

from src.agent import tools


@pytest.fixture
def windows(monkeypatch):
    monkeypatch.setattr(tools.platform, "system", lambda: "Windows")
    started = []
    monkeypatch.setattr(tools.os, "startfile", lambda t: started.append(t), raising=False)
    return started


def test_whatsapp_opens_via_uri(windows):
    res = tools.handle_app_open({"app_name": "whatsapp"})
    assert res.success
    assert windows == ["whatsapp:"]


@pytest.mark.parametrize("name,uri", [
    ("Spotify", "spotify:"),
    ("calculator", "calculator:"),
    ("settings", "ms-settings:"),
    ("store", "ms-windows-store:"),
])
def test_common_store_apps_use_uri(windows, name, uri):
    assert tools.handle_app_open({"app_name": name}).success
    assert windows == [uri]


def test_unknown_app_resolved_via_start_apps(monkeypatch, windows):
    def fail_then_ok(target):
        if target.startswith("shell:AppsFolder"):
            windows.append(target)
        else:
            raise OSError("not found")

    monkeypatch.setattr(tools.os, "startfile", fail_then_ok, raising=False)
    monkeypatch.setattr(tools.shutil, "which", lambda n: None)
    monkeypatch.setattr(tools, "_lookup_start_app", lambda n: "Contoso.App_abc!App")
    res = tools.handle_app_open({"app_name": "contoso"})
    assert res.success
    assert windows == ["shell:AppsFolder\\Contoso.App_abc!App"]


def test_unknown_app_fails_cleanly(monkeypatch, windows):
    monkeypatch.setattr(tools.os, "startfile", MagicMock(side_effect=OSError("nope")), raising=False)
    monkeypatch.setattr(tools.shutil, "which", lambda n: None)
    monkeypatch.setattr(tools, "_lookup_start_app", lambda n: None)
    res = tools.handle_app_open({"app_name": "nosuchapp"})
    assert not res.success
    assert "not found" in res.error.lower()


def test_lookup_start_app_matches_name(monkeypatch):
    out = '[{"Name":"WhatsApp","AppID":"5319275A.WhatsAppDesktop_cv1g1gvanyjgm!App"}]'
    monkeypatch.setattr(tools.shutil, "which", lambda n: "powershell.exe")
    monkeypatch.setattr(
        tools.subprocess, "run",
        lambda *a, **k: subprocess.CompletedProcess(a, 0, stdout=out, stderr=""),
    )
    assert tools._lookup_start_app("whatsapp") == "5319275A.WhatsAppDesktop_cv1g1gvanyjgm!App"


def test_lookup_rejects_unsafe_appid(monkeypatch):
    out = '[{"Name":"Evil","AppID":"x & calc"}]'
    monkeypatch.setattr(tools.shutil, "which", lambda n: "powershell.exe")
    monkeypatch.setattr(
        tools.subprocess, "run",
        lambda *a, **k: subprocess.CompletedProcess(a, 0, stdout=out, stderr=""),
    )
    assert tools._lookup_start_app("evil") is None
