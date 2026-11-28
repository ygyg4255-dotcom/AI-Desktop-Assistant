from unittest.mock import MagicMock, patch

import pytest

from src.agent.permissions import PermissionGate
from src.agent import tools as T
from src.services import browser_service as bs


def _page(title, url, text="hello"):
    p = MagicMock()
    p.title.return_value, p.url = title, url
    p.inner_text.return_value = text
    p.evaluate.return_value = "hidden"
    return p


@pytest.fixture
def env():
    """BrowserService wired to fake pages via a mocked sync_playwright."""
    pages = [_page("Gmail", "https://mail.google.com"), _page("GitHub", "https://github.com/a"),
             _page("GitHub Issues", "https://github.com/b")]
    ctx = MagicMock(pages=pages)
    ctx.new_page.side_effect = lambda: _new(ctx)
    browser = MagicMock(contexts=[ctx])
    browser.is_connected.return_value = True
    pw = MagicMock()
    pw.chromium.connect_over_cdp.return_value = browser
    with patch("playwright.sync_api.sync_playwright") as sp:
        sp.return_value.start.return_value = pw
        svc = bs.BrowserService()
        yield svc, pages, browser, pw
        svc._pool.shutdown()


def _new(ctx):
    p = _page("", "about:blank")
    p.goto.side_effect = lambda u: setattr(p, "url", u)
    ctx.pages.append(p)
    return p


def test_list_tabs(env):
    svc, *_ = env
    assert svc.list_tabs() == [{"index": 0, "title": "Gmail", "url": "https://mail.google.com"},
                               {"index": 1, "title": "GitHub", "url": "https://github.com/a"},
                               {"index": 2, "title": "GitHub Issues", "url": "https://github.com/b"}]


def test_switch_tab_exact_index_and_title(env):
    svc, pages, *_ = env
    assert svc.switch_tab("1")["ok"]
    pages[1].bring_to_front.assert_called_once()
    assert svc.switch_tab("GMAIL")["tab"]["index"] == 0


def test_switch_tab_ambiguous_returns_candidates(env):
    svc, pages, *_ = env
    r = svc.switch_tab("github")
    assert r["ok"] is False and [c["index"] for c in r["candidates"]] == [1, 2]
    assert not any(p.bring_to_front.called for p in pages)


def test_switch_tab_no_match_and_bad_index(env):
    svc, *_ = env
    assert svc.switch_tab("zzz")["ok"] is False
    assert svc.switch_tab("9")["ok"] is False


def test_close_tab_only_that_tab(env):
    svc, pages, *_ = env
    r = svc.close_tab("mail.google")
    assert r["ok"] and r["closed"]["title"] == "Gmail"
    pages[0].close.assert_called_once()
    assert not pages[1].close.called


def test_close_tab_ambiguous_closes_nothing(env):
    svc, pages, *_ = env
    assert svc.close_tab("github")["ok"] is False
    assert not any(p.close.called for p in pages)


def test_open_url_new_tab_prefixes_https(env):
    svc, *_ = env
    r = svc.open_url("example.com")
    assert r["url"] == "https://example.com"


def test_open_url_same_tab(env):
    svc, pages, *_ = env
    pages[2].evaluate.return_value = "visible"
    svc.open_url("http://x.org", new_tab=False)
    pages[2].goto.assert_called_once_with("http://x.org")


@pytest.mark.parametrize("bad", ["javascript:alert(1)", "file:///C:/x", "ftp://x", "data:text/html,hi", ""])
def test_open_url_rejects_non_http(env, bad):
    svc, *_ = env
    with pytest.raises(bs.BrowserError):
        svc.open_url(bad)


def test_read_page_truncates_and_marks_untrusted(env):
    svc, pages, *_ = env
    pages[1].inner_text.return_value = "x" * 50
    r = svc.read_page("github.com/a", max_chars=10)
    assert r["truncated"] and "x" * 10 in r["text"] and "x" * 11 not in r["text"]
    assert r["text"].startswith(bs.UNTRUSTED_OPEN)


def test_read_page_active_tab(env):
    svc, pages, *_ = env
    pages[0].evaluate.return_value = "visible"
    pages[0].inner_text.return_value = "inbox"
    r = svc.read_page()
    assert r["title"] == "Gmail" and not r["truncated"]


def test_cdp_unreachable_message(env):
    svc, _p, _b, pw = env
    pw.chromium.connect_over_cdp.side_effect = Exception("ECONNREFUSED")
    with patch.object(bs.BrowserService, "_auto_launch", return_value=False), pytest.raises(bs.BrowserError) as e:
        svc.list_tabs()
    assert "--remote-debugging-port=9222" in str(e.value) and "127.0.0.1:9222" in str(e.value)
    pw.stop.assert_called()  # failed attempt is cleaned up


def test_cdp_url_configurable(env, monkeypatch):
    svc, _p, _b, pw = env
    monkeypatch.setattr(bs, "get_config_value", lambda *a, **k: "http://127.0.0.1:9333")
    svc.list_tabs()
    pw.chromium.connect_over_cdp.assert_called_with("http://127.0.0.1:9333")


def test_disconnect_closes_connection_only(env):
    svc, _p, browser, pw = env
    svc.list_tabs()
    svc.disconnect()
    browser.close.assert_called_once()  # CDP browser.close() detaches; Brave keeps running
    pw.stop.assert_called_once()


def test_permission_tiers():
    g = PermissionGate()
    for t in ("browser_list_tabs", "browser_read_page"):
        assert g.is_safe(t) and not g.is_dangerous(t)
    for t in ("browser_open_url", "browser_switch_tab", "browser_close_tab"):
        assert g.is_dangerous(t) and not g.is_safe(t)


def test_handlers_gate_and_registered():
    r = T.handle_browser_close_tab({"query": "x"}, approved=False)
    assert r.requires_permission and r.is_dangerous
    src = open(T.__file__, encoding="utf-8").read()
    assert all(f'"{n}"' in src for n in ("browser_list_tabs", "browser_read_page", "browser_open_url",
                                         "browser_switch_tab", "browser_close_tab"))


def test_handler_reports_ambiguity_as_failure():
    svc = MagicMock()
    svc.switch_tab.return_value = {"ok": False, "error": "2 tabs match", "candidates": [1, 2]}
    with patch("src.services.browser_service.get_browser_service", return_value=svc):
        r = T.handle_browser_switch_tab({"query": "gh"}, approved=True)
    assert not r.success and r.output["candidates"] == [1, 2]


# ---- auto-launch ----
def _cfg(flag="true", brave="C:/b/brave.exe"):
    vals = {"ALTRON_BROWSER_AUTO_LAUNCH": flag, "ALTRON_BRAVE_PATH": brave}
    return lambda k, default="": vals.get(k, default)


def _fail_then_ok(pw, browser):
    pw.chromium.connect_over_cdp.side_effect = [Exception("refused"), browser]


def test_auto_launch_success_on_retry(env):
    svc, _p, browser, pw = env
    _fail_then_ok(pw, browser)
    with patch.object(bs, "get_config_value", _cfg()), patch.object(bs.os.path, "isfile", return_value=True),             patch.object(bs.subprocess, "Popen") as popen, patch.object(bs.urllib.request, "urlopen"):
        assert svc.list_tabs()
    cmd = popen.call_args[0][0]
    assert cmd[0] == "C:/b/brave.exe" and "--remote-debugging-port=9222" in cmd
    assert any(a.startswith("--user-data-dir=") and a.endswith(".altron-brave-profile") for a in cmd)
    kw = popen.call_args[1]
    assert kw["close_fds"] and kw["creationflags"] & bs.subprocess.DETACHED_PROCESS
    assert pw.chromium.connect_over_cdp.call_count == 2


def test_auto_launch_disabled(env):
    svc, _p, _b, pw = env
    pw.chromium.connect_over_cdp.side_effect = Exception("refused")
    with patch.object(bs, "get_config_value", _cfg(flag="false")), patch.object(bs.subprocess, "Popen") as popen,             pytest.raises(bs.BrowserError, match="remote-debugging-port"):
        svc.list_tabs()
    popen.assert_not_called()


def test_auto_launch_brave_missing(env):
    svc, _p, _b, pw = env
    pw.chromium.connect_over_cdp.side_effect = Exception("refused")
    with patch.object(bs, "get_config_value", _cfg(brave="")), patch.object(bs.os.path, "isfile", return_value=False),             patch.object(bs.subprocess, "Popen") as popen, pytest.raises(bs.BrowserError, match="ALTRON_BRAVE_PATH"):
        svc.list_tabs()
    popen.assert_not_called()


def test_auto_launch_never_for_remote_url(env):
    svc, _p, _b, pw = env
    pw.chromium.connect_over_cdp.side_effect = Exception("refused")
    base = _cfg()
    cfg = lambda k, default="": "http://10.0.0.5:9222" if k == "ALTRON_BROWSER_CDP_URL" else base(k, default)
    with patch.object(bs, "get_config_value", cfg), patch.object(bs.subprocess, "Popen") as popen,             pytest.raises(bs.BrowserError):
        svc.list_tabs()
    popen.assert_not_called()


# ---- close tools need no approval / PIN ----
def test_app_close_window_close_not_gated():
    g = PermissionGate()
    for n in ("app_close", "window_close"):
        assert g.is_safe(n) and not g.is_dangerous(n)
    reg = T.create_default_registry()
    assert not reg.get("app_close").requires_permission and not reg.get("window_close").requires_permission
    r = T.handle_app_close({"app_name": "definitely_not_running_xyz"})
    assert not r.requires_permission and "nothing was closed" in r.error
    r = T.handle_window_close({"title": "zzz-no-such-window"})
    assert not r.requires_permission


def test_url_tools_live_in_browser_pack():
    from src.agent.tool_router import PACKS
    assert {"browser_open", "open_url", "browser_open_url"} <= set(PACKS["browser"])
