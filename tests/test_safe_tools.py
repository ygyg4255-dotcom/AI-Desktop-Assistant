"""Regression tests for audit item S4: the auto-allow ("safe tools") list must not run risky inputs unprompted."""
import ipaddress
import os
import socket
import urllib.request
from unittest.mock import MagicMock

import pytest

from src.agent import tools as tools_mod
from src.agent.executor import AgentExecutor
from src.agent.permissions import PermissionGate, PermissionLevel, is_sensitive_path
from src.agent.tools import (
    handle_app_open,
    handle_file_copy,
    handle_file_open,
    handle_file_read,
    handle_send_to_telegram,
    handle_web_fetch,
)


@pytest.fixture
def executor():
    return AgentExecutor(ai_client=MagicMock(), database=None)


# --- gate configuration ---------------------------------------------------

def test_risky_tools_are_not_blanket_safe():
    for name in ("file_read", "file_copy", "file_open", "app_open", "send_to_telegram"):
        assert not PermissionGate().is_safe(name), name
        assert PermissionGate().get_permission_level(name) == PermissionLevel.ASK, name


def test_blanket_session_allow_is_refused_for_conditional_tools():
    gate = PermissionGate()
    gate.set_session_permission("app_open", PermissionLevel.ALLOW)
    assert gate.get_permission_level("app_open") == PermissionLevel.ASK


# --- 1. file_copy / send_to_telegram(file) --------------------------------

def test_file_copy_requires_approval_and_does_not_copy(tmp_path):
    src, dst = tmp_path / "a.txt", tmp_path / "b.txt"
    src.write_text("new")
    dst.write_text("precious")

    result = handle_file_copy({"source": str(src), "destination": str(dst)})

    assert result.requires_permission is True
    assert dst.read_text() == "precious", "file_copy overwrote a file without approval"


def test_file_copy_runs_when_approved(tmp_path):
    src, dst = tmp_path / "a.txt", tmp_path / "b.txt"
    src.write_text("new")
    assert handle_file_copy({"source": str(src), "destination": str(dst)}, approved=True).success
    assert dst.read_text() == "new"


def test_telegram_file_requires_approval_and_sends_nothing(monkeypatch):
    monkeypatch.setattr(tools_mod, "_telegram_credentials", lambda: ("t", "c"))
    send_doc, send_msg = MagicMock(return_value=True), MagicMock(return_value=True)
    monkeypatch.setattr(tools_mod, "send_telegram_document", send_doc)
    monkeypatch.setattr(tools_mod, "send_telegram_message", send_msg)

    result = handle_send_to_telegram({"file_path": r"C:\secret.txt"})
    assert result.requires_permission is True
    send_doc.assert_not_called()

    assert handle_send_to_telegram({"file_path": r"C:\secret.txt"}, approved=True).success
    send_doc.assert_called_once()


def test_telegram_text_only_still_auto_runs(monkeypatch):
    monkeypatch.setattr(tools_mod, "_telegram_credentials", lambda: ("t", "c"))
    send_msg = MagicMock(return_value=True)
    monkeypatch.setattr(tools_mod, "send_telegram_message", send_msg)

    result = handle_send_to_telegram({"message": "hello"})
    assert result.success and not result.requires_permission
    send_msg.assert_called_once()


# --- 2. file_read denylist ------------------------------------------------

SENSITIVE_NAMES = [
    ".env", ".ENV", ".Env.local", "prod.env", ".env.", ".env ",
    "server.pem", "SERVER.PEM", "tls.key", "id_rsa", "ID_RSA.pub", "id_ed25519",
    "memory.json", "Memory.JSON", "status.json",
]


@pytest.mark.parametrize("name", SENSITIVE_NAMES)
def test_file_read_denylisted_names_need_approval(tmp_path, name):
    f = tmp_path / name
    try:
        f.write_text("SECRET=1")
    except OSError:
        pass  # some names (trailing space/dot) cannot be created on Windows; the check is name-based
    assert is_sensitive_path(str(f))
    result = handle_file_read({"path": str(f)})
    assert result.requires_permission is True and result.output is None


def test_file_read_dotdot_traversal_to_env(tmp_path):
    (tmp_path / ".env").write_text("SECRET=1")
    (tmp_path / "sub").mkdir()
    for sep in {"\\", "/", os.sep}:
        p = f"{tmp_path}{sep}sub{sep}..{sep}.env"
        assert handle_file_read({"path": p}).requires_permission is True, p


def test_file_read_mixed_case_directory_and_dotdot(tmp_path):
    (tmp_path / ".SSH").mkdir()
    (tmp_path / "x").mkdir()
    p = str(tmp_path / "X" / ".." / ".sSh" / "config")
    assert handle_file_read({"path": p}).requires_permission is True


@pytest.mark.parametrize("path", [
    r"C:\Users\me\.ssh\config",
    r"C:\Users\me\AppData\Local\Google\Chrome\User Data\Default\Login Data",
    r"C:\Users\me\AppData\Local\Microsoft\Edge\User Data\Local State",
    r"C:\Users\me\AppData\Local\BraveSoftware\Brave-Browser\User Data\Default\Cookies",
    r"C:\Users\me\AppData\Roaming\Mozilla\Firefox\Profiles\abc.default\logins.json",
    r"C:\Users\me\AppData\Roaming\Microsoft\Credentials\ABCDEF",
    r"C:\Users\me\AppData\Roaming\Microsoft\Protect\S-1-5-21\key",
    r"C:\Users\me\AppData\Local\Microsoft\Vault\x",
    r"C:\Windows\System32\config\SAM",
    r"c:\users\me\appdata\local\google\chrome\USER DATA\default\cookies",
    r"C:\Users\me\project\.env::$DATA",
])
def test_sensitive_locations_need_approval(path):
    assert is_sensitive_path(path), path


def test_file_read_symlink_to_secret_needs_approval(tmp_path):
    secret = tmp_path / ".env"
    secret.write_text("SECRET=1")
    link = tmp_path / "innocent.txt"
    try:
        os.symlink(secret, link)
    except (OSError, NotImplementedError):
        pytest.skip("symlink creation not permitted on this machine")
    assert handle_file_read({"path": str(link)}).requires_permission is True


def test_file_read_symlinked_directory_to_secret_dir(tmp_path):
    ssh = tmp_path / ".ssh"
    ssh.mkdir()
    (ssh / "config").write_text("Host x")
    link = tmp_path / "docs"
    try:
        os.symlink(ssh, link, target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("symlink creation not permitted on this machine")
    assert handle_file_read({"path": str(link / "config")}).requires_permission is True


def test_file_read_ordinary_file_auto_allowed(tmp_path):
    f = tmp_path / "notes.txt"
    f.write_text("hello")
    result = handle_file_read({"path": str(f)})
    assert result.success and result.output["content"] == "hello"


def test_file_read_denylisted_runs_when_approved(tmp_path):
    f = tmp_path / ".env"
    f.write_text("A=1")
    assert handle_file_read({"path": str(f)}, approved=True).success


# --- 3. file_open / app_open ----------------------------------------------

@pytest.fixture
def startfile(monkeypatch):
    mock = MagicMock()
    monkeypatch.setattr(tools_mod.os, "startfile", mock, raising=False)
    monkeypatch.setattr(tools_mod.subprocess, "Popen", MagicMock())
    return mock


@pytest.mark.parametrize("ext", [".exe", ".bat", ".cmd", ".ps1", ".vbs", ".js", ".msi", ".lnk",
                                 ".EXE", ".Ps1", ".exe.", ".exe "])
def test_file_open_executable_types_need_approval(tmp_path, startfile, ext):
    f = tmp_path / f"thing{ext}"
    try:
        f.write_text("x")
    except OSError:
        f = tmp_path / "thing.exe"
        f.write_text("x")
    result = handle_file_open({"path": str(f)})
    assert result.requires_permission is True
    startfile.assert_not_called()


def test_file_open_symlink_to_executable_needs_approval(tmp_path, startfile):
    exe = tmp_path / "evil.exe"
    exe.write_text("x")
    link = tmp_path / "report.pdf"
    try:
        os.symlink(exe, link)
    except (OSError, NotImplementedError):
        pytest.skip("symlink creation not permitted on this machine")
    assert handle_file_open({"path": str(link)}).requires_permission is True
    startfile.assert_not_called()


def test_file_open_document_auto_allowed(tmp_path, startfile):
    f = tmp_path / "report.pdf"
    f.write_text("x")
    result = handle_file_open({"path": str(f)})
    assert result.success and not result.requires_permission
    startfile.assert_called_once()


def test_file_open_executable_runs_when_approved(tmp_path, startfile):
    f = tmp_path / "tool.exe"
    f.write_text("x")
    assert handle_file_open({"path": str(f)}, approved=True).success
    startfile.assert_called_once()


@pytest.mark.parametrize("params", [
    {"path": r"C:\Windows\System32\calc.exe"},
    {"path": "tool.exe"},
    {"app_name": r"C:\Windows\System32\calc.exe"},
    {"app_name": "..\\evil"},
    {"app_name": "sub/evil"},
    {"app_name": r"\\attacker\share\x"},
    {"app_name": "notepad.exe"},
    {"app_name": "NOTEPAD.EXE"},
    {"app_name": "setup.msi"},
    {"app_name": "run.ps1"},
    {"app_name": "x.lnk"},
    {"app_name": 'x" & calc & "'},
    {"app_name": "calc | calc"},
    {"app_name": "file:///C:/x.exe"},
    {"app_name": "http://example.com"},
])
def test_app_open_risky_targets_need_approval(startfile, params):
    result = handle_app_open(params)
    assert result.requires_permission is True
    startfile.assert_not_called()


@pytest.mark.parametrize("name", ["notepad", "Visual Studio Code", "Google Chrome", "ms-settings:", "ms-settings:network"])
def test_app_open_plain_names_auto_allowed(startfile, name):
    result = handle_app_open({"app_name": name})
    assert result.success and not result.requires_permission
    startfile.assert_called_once_with(name)


def test_app_open_full_path_runs_when_approved(startfile):
    assert handle_app_open({"path": r"C:\Windows\System32\calc.exe"}, approved=True).success
    startfile.assert_called_once()


# --- executor integration: one run, no blanket-allow bypass ----------------

def test_executor_runs_plain_app_open_exactly_once(executor, startfile):
    callback = MagicMock()
    result = executor._execute_tool_with_permission("app_open", {"app_name": "notepad"}, permission_callback=callback)
    assert result.success
    assert startfile.call_count == 1, "handler ran twice"
    callback.assert_not_called()


def test_executor_plain_app_open_once_with_approval_flow(executor, startfile):
    """Plain safe name: the permission path must not queue a second approved run,
    and an explicit approved run (as done after a user approval) launches exactly once."""
    callback = MagicMock()
    first = executor._execute_tool_with_permission(
        "app_open", {"app_name": "notepad"}, permission_callback=callback)
    # mirrors the _process_sync loop: a second run only happens if a prompt is pending
    assert not first.requires_permission and executor._pending_permission is None
    assert startfile.call_count == 1

    startfile.reset_mock()
    approved = executor._execute_tool("app_open", {"app_name": "notepad"}, approved=True)
    assert approved.success
    startfile.assert_called_once_with("notepad")


def test_executor_plain_app_open_once_with_remembered_allow(startfile):
    db = MagicMock()
    db.get_tool_permission.return_value = "allow"
    ex = AgentExecutor(ai_client=MagicMock(), database=db)
    callback = MagicMock()
    result = ex._execute_tool_with_permission(
        "app_open", {"app_name": "notepad"}, permission_callback=callback)
    assert result.success
    assert startfile.call_count == 1, "handler ran twice"
    callback.assert_not_called()


def test_executor_prompts_for_risky_app_open(executor, startfile):
    callback = MagicMock()
    result = executor._execute_tool_with_permission(
        "app_open", {"path": r"C:\x.exe", "_approved": True, "approved": True}, permission_callback=callback)
    assert result.requires_permission is True
    callback.assert_called_once()
    startfile.assert_not_called()


def test_executor_session_allow_does_not_unlock_risky_inputs(executor, startfile):
    executor.gate.set_session_permission("app_open", PermissionLevel.ALLOW)
    callback = MagicMock()
    result = executor._execute_tool_with_permission("app_open", {"path": r"C:\x.exe"}, permission_callback=callback)
    assert result.requires_permission is True
    startfile.assert_not_called()


def test_executor_remembered_allow_does_not_unlock_risky_inputs(startfile):
    db = MagicMock()
    db.get_tool_permission.return_value = "allow"
    ex = AgentExecutor(ai_client=MagicMock(), database=db)
    result = ex._execute_tool_with_permission("app_open", {"path": r"C:\x.exe"}, permission_callback=MagicMock())
    assert result.requires_permission is True
    startfile.assert_not_called()


def test_executor_prompts_for_file_copy(executor, tmp_path):
    src, dst = tmp_path / "a", tmp_path / "b"
    src.write_text("n")
    dst.write_text("old")
    callback = MagicMock()
    result = executor._execute_tool_with_permission(
        "file_copy", {"source": str(src), "destination": str(dst)}, permission_callback=callback)
    assert result.requires_permission is True
    callback.assert_called_once()
    assert dst.read_text() == "old"


def test_registry_execute_text_fallback_cannot_bypass(tmp_path):
    from src.agent.tools import create_default_registry
    secret = tmp_path / ".env"
    secret.write_text("A=1")
    result = create_default_registry().execute("file_read", path=str(secret), _approved=True, approved=True)
    assert result.requires_permission is True and result.output is None


# --- 4. web_fetch SSRF -----------------------------------------------------

@pytest.fixture
def fake_dns(monkeypatch):
    table = {}

    def getaddrinfo(host, port, *a, **kw):
        addr = table.get(host, host)  # IP literals resolve to themselves, like the real resolver
        try:
            ipaddress.ip_address(addr)
        except ValueError:
            raise socket.gaierror(f"no such host {host}")
        fam = socket.AF_INET6 if ":" in addr else socket.AF_INET
        return [(fam, socket.SOCK_STREAM, 6, "", (addr, port))]

    monkeypatch.setattr(tools_mod.socket, "getaddrinfo", getaddrinfo)
    return table


@pytest.mark.parametrize("url", [
    "file:///C:/Windows/win.ini",
    "ftp://example.com/x",
    "gopher://example.com/",
    "javascript://example.com/%0Aalert(1)",
    "data://text/plain,hi",
])
def test_web_fetch_rejects_non_http_schemes(url):
    result = handle_web_fetch({"url": url})
    assert result.success is False and "refused" in result.error


@pytest.mark.parametrize("ip", [
    "127.0.0.1", "127.255.255.254", "10.0.0.5", "172.16.0.1", "172.31.255.255", "192.168.1.1",
    "169.254.169.254", "0.0.0.0", "::1", "fc00::1", "fd12:3456::1", "fe80::1", "::ffff:127.0.0.1", "::ffff:10.0.0.1",
])
def test_web_fetch_rejects_non_public_addresses(fake_dns, monkeypatch, ip):
    fake_dns["internal.example"] = ip
    opener = MagicMock()
    monkeypatch.setattr(tools_mod, "_build_public_opener", opener)
    for url in ("http://internal.example/", "https://internal.example/x"):
        result = handle_web_fetch({"url": url})
        assert result.success is False and "non-public" in result.error, (ip, url)
    opener.assert_not_called()


@pytest.mark.parametrize("url", [
    "http://127.0.0.1/", "http://localhost/", "http://[::1]/", "http://10.1.2.3:8080/",
    "http://169.254.169.254/latest/meta-data/", "http://2130706433/", "http://0x7f.0.0.1/", "http://user@127.0.0.1/",
])
def test_web_fetch_rejects_literal_local_hosts(url):
    result = handle_web_fetch({"url": url})
    assert result.success is False, url


@pytest.mark.parametrize("ip", ["93.184.216.34", "8.8.8.8", "2606:4700:4700::1111"])
def test_ip_is_public_accepts_public_addresses(ip):
    assert tools_mod._ip_is_public(ip)


def test_web_fetch_rejects_host_with_any_private_record(monkeypatch):
    monkeypatch.setattr(tools_mod.socket, "getaddrinfo", lambda h, p, *a, **k: [
        (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", p)),
        (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("10.0.0.1", p)),
    ])
    assert handle_web_fetch({"url": "http://mixed.example/"}).success is False


def test_web_fetch_redirect_to_private_address_is_blocked(fake_dns):
    fake_dns["public.example"] = "93.184.216.34"
    fake_dns["internal.example"] = "10.0.0.5"
    handler = tools_mod._CheckedRedirectHandler()
    req = urllib.request.Request("http://public.example/")

    with pytest.raises(ValueError, match="non-public"):
        handler.redirect_request(req, None, 302, "Found", {}, "http://internal.example/admin")
    with pytest.raises(ValueError, match="non-public"):
        handler.redirect_request(req, None, 302, "Found", {}, "http://127.0.0.1/")
    with pytest.raises(ValueError, match="http"):
        handler.redirect_request(req, None, 302, "Found", {}, "file:///C:/Windows/win.ini")
    # A redirect to another public host is fine.
    fake_dns["other.example"] = "8.8.8.8"
    assert handler.redirect_request(req, None, 302, "Found", {}, "http://other.example/") is not None


def test_web_fetch_redirect_error_surfaces_as_tool_error(fake_dns, monkeypatch):
    fake_dns["public.example"] = "93.184.216.34"
    opener = MagicMock()
    opener.open.side_effect = ValueError("Blocked: 'internal.example' resolves to a non-public address (10.0.0.5)")
    monkeypatch.setattr(tools_mod, "_build_public_opener", lambda: opener)
    result = handle_web_fetch({"url": "http://public.example/"})
    assert result.success is False and "non-public" in result.error


def test_connect_rechecks_resolution_at_connect_time(fake_dns):
    """DNS rebinding: a name that turns private after the pre-check must not be connected to."""
    fake_dns["rebind.example"] = "127.0.0.1"
    with pytest.raises(ValueError, match="non-public"):
        tools_mod._connect_public("rebind.example", 80, 1)


class _FakeResponse:
    def __init__(self, body: bytes):
        self._body = body
        self.headers = {"Content-Type": "text/plain; charset=utf-8"}
        self.requested = None

    def read(self, n=-1):
        self.requested = n
        return self._body if n is None or n < 0 else self._body[:n]

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def test_web_fetch_caps_response_size(fake_dns, monkeypatch):
    fake_dns["big.example"] = "93.184.216.34"
    body = b"a" * (tools_mod.WEB_FETCH_MAX_BYTES + 1000)
    resp = _FakeResponse(body)
    opener = MagicMock()
    opener.open.return_value = resp
    monkeypatch.setattr(tools_mod, "_build_public_opener", lambda: opener)

    result = handle_web_fetch({"url": "http://big.example/", "max_chars": 10**9})

    assert result.success
    assert resp.requested == tools_mod.WEB_FETCH_MAX_BYTES, "response read was unbounded"
    assert len(result.output["text"]) <= tools_mod.WEB_FETCH_MAX_BYTES


def test_web_fetch_public_url_still_works(fake_dns, monkeypatch):
    fake_dns["ok.example"] = "93.184.216.34"
    opener = MagicMock()
    opener.open.return_value = _FakeResponse(b"<html><body><p>hello world</p></body></html>")
    monkeypatch.setattr(tools_mod, "_build_public_opener", lambda: opener)
    result = handle_web_fetch({"url": "ok.example/page"})  # no scheme -> https
    assert result.success and "hello world" in result.output["text"]
    assert result.output["url"] == "https://ok.example/page"


def test_web_fetch_stays_auto_allowed():
    assert PermissionGate().get_permission_level("web_fetch") == PermissionLevel.ALLOW
