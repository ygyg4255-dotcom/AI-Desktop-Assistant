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
    for pg in pages:  # real Playwright drops a page from context.pages when it closes
        pg.close.side_effect = lambda pg=pg: pages.remove(pg)
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
    gmail, github = pages[0], pages[1]
    r = svc.close_tab("mail.google")
    assert r["ok"] and [c["title"] for c in r["closed"]] == ["Gmail"]
    gmail.close.assert_called_once()
    assert not github.close.called


def test_close_tab_ambiguous_closes_nothing(env):
    svc, pages, *_ = env
    assert svc.close_tab("github")["ok"] is False
    assert not any(p.close.called for p in pages)


def test_close_all_matching_in_one_call(env):
    svc, pages, *_ = env
    gh = [pages[1], pages[2]]
    r = svc.close_tab("github", close_all=True)
    assert r["ok"] and r["closed_count"] == 2 and not r["remaining_matches"]
    assert all(p.close.called for p in gh) and not pages[0].close.called
    assert [t["title"] for t in svc.list_tabs()] == ["Gmail"] and r["open_tabs"] == 1


def test_close_ambiguous_without_close_all_lists_matches(env):
    svc, pages, *_ = env
    r = svc.close_tab("github")
    assert r["ok"] is False and len(r["candidates"]) == 2
    assert len(svc.list_tabs()) == 3


def test_close_all_partial_failure_reports_remaining(env):
    svc, pages, *_ = env
    stuck = pages[1]
    stuck.close.side_effect = RuntimeError("beforeunload")
    stuck.is_closed.return_value = False
    r = svc.close_tab("github", close_all=True)
    assert r["ok"] is False and r["closed_count"] == 1
    assert [t["url"] for t in r["failed"]] == ["https://github.com/a"]
    assert [t["url"] for t in r["remaining_matches"]] == ["https://github.com/a"]


def test_close_all_tab_vanishes_before_execution(env):
    svc, pages, *_ = env
    gone = pages[1]

    def _vanish():
        pages.remove(gone)
        raise RuntimeError("Target closed")
    gone.close.side_effect = _vanish
    gone.is_closed.return_value = True
    r = svc.close_tab("github", close_all=True)
    assert r["ok"] and r["closed_count"] == 1 and len(r["already_gone"]) == 1
    assert r["remaining_matches"] == []


def test_close_reported_but_tab_still_open_is_failure(env):
    svc, pages, *_ = env
    pages[0].close.side_effect = lambda: None  # claims success, tab stays
    r = svc.close_tab("mail.google")
    assert r["ok"] is False and r["failed"] and r["remaining_matches"]


def test_handler_passes_close_all():
    svc = MagicMock()
    svc.close_tab.return_value = {"ok": True}
    with patch("src.services.browser_service.get_browser_service", return_value=svc):
        T.handle_browser_close_tab({"query": "x", "close_all": True})
    svc.close_tab.assert_called_once_with("x", True)


def test_open_url_new_tab_prefixes_https(env):
    svc, *_ = env
    r = svc.open_url("example.com")
    assert r["url"] == "https://example.com"


def test_open_url_same_tab(env):
    svc, pages, *_ = env
    pages[2].evaluate.return_value = "visible"
    svc.open_url("http://x.org", new_tab=False)
    pages[2].goto.assert_called_once_with("http://x.org")


@pytest.mark.parametrize("bad", ["javascript:alert(1)", "file:///C:/x", "ftp://x", "data:text/html,hi"])
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
    for t in ("browser_list_tabs", "browser_read_page", "browser_open_url", "browser_switch_tab", "browser_close_tab"):
        assert g.is_safe(t) and not g.is_dangerous(t)


def test_handlers_gate_and_registered():
    reg = T.create_default_registry()
    for n in ("browser_open_url", "browser_switch_tab", "browser_close_tab"):
        assert not reg.get(n).requires_permission and not reg.get(n).is_dangerous
    svc = MagicMock()
    svc.close_tab.return_value = {"closed": "x"}
    with patch("src.services.browser_service.get_browser_service", return_value=svc):
        r = T.handle_browser_close_tab({"query": "x"})  # no approved kwarg: runs without PIN
    assert r.success and not r.requires_permission
    src = open(T.__file__, encoding="utf-8").read()
    assert all(f'"{n}"' in src for n in ("browser_list_tabs", "browser_read_page", "browser_open_url",
                                         "browser_switch_tab", "browser_close_tab"))


def test_handler_reports_ambiguity_as_failure():
    svc = MagicMock()
    svc.switch_tab.return_value = {"ok": False, "error": "2 tabs match", "candidates": [1, 2]}
    with patch("src.services.browser_service.get_browser_service", return_value=svc):
        r = T.handle_browser_switch_tab({"query": "gh"})
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
    assert "browser_open_url" in PACKS["browser"]
    assert not {"browser_open", "open_url"} & set(PACKS["browser"])


def test_only_browser_open_url_exposed_for_urls():
    reg = T.create_default_registry()
    names = {s["name"] for s in reg.get_tools_schema()}
    assert "browser_open_url" in names and not {"browser_open", "open_url"} & names
    svc = MagicMock()
    svc.open_url.return_value = {"url": "https://example.com"}
    with patch("src.services.browser_service.get_browser_service", return_value=svc):
        assert reg.get("open_url").run({"url": "https://example.com"}).success
        assert reg.get("browser_open").run({"url": "example.com"}).success
    assert svc.open_url.call_count == 2  # legacy aliases route through Brave CDP


def test_close_failure_with_dropped_connection_is_failed_with_error(env):
    svc, pages, *_ = env
    pages[0].close.side_effect = RuntimeError("Target closed")
    pages[0].is_closed.side_effect = RuntimeError("connection dropped")
    r = svc.close_tab("mail.google")
    assert r["ok"] is False and r["closed_count"] == 0
    assert r["failed"][0]["error"] == "Target closed"


def test_close_failed_entry_has_error_field(env):
    svc, pages, *_ = env
    pages[0].close.side_effect = RuntimeError("beforeunload")
    pages[0].is_closed.return_value = False
    assert svc.close_tab("mail.google")["failed"][0]["error"] == "beforeunload"


@pytest.mark.parametrize("url", ["", "about:blank", "newtab", "chrome://newtab"])
def test_open_url_blank_tab(env, url):
    svc, pages, *_ = env
    r = T.handle_browser_open_url({"url": url}) if False else svc.open_url(url)
    assert r["ok"] and len(pages) == 4 and pages[-1].url == "about:blank"
    assert not pages[-1].goto.called


@pytest.mark.parametrize("url", ["javascript:alert(1)", "file:///c:/x", "data:text/html,hi"])
def test_open_url_still_blocks_unsafe_schemes(url):
    with pytest.raises(bs.BrowserError):
        bs.normalize_url(url)


# ---- browser_video_control ----
_ST = {"current_time": 125.0, "duration": 3725.0, "paused": False, "muted": False, "playback_rate": 1.0}


def _video_env(pages, with_video=()):
    """Only pages whose title is in with_video have a <video>; the rest return null."""
    for p in pages:
        p.evaluate.side_effect = lambda js, arg=None, p=p: dict(_ST) if p.title() in with_video else None


@pytest.mark.parametrize("raw,expected", [(120, 120.0), ("120", 120.0), ("2:00", 120.0), ("1:02:05", 3725.0),
                                          ("0:05", 5.0), (7.5, 7.5)])
def test_parse_timestamp_valid(raw, expected):
    assert bs.parse_timestamp(raw) == expected


@pytest.mark.parametrize("raw", ["abc", "", None, "-5", -5, "1:75", "1:2:3:4", True, float("nan"), float("inf")])
def test_parse_timestamp_invalid(raw):
    with pytest.raises(bs.BrowserError):
        bs.parse_timestamp(raw)


def test_format_time():
    assert bs.format_time(125) == "02:05" and bs.format_time(3725) == "1:02:05" and bs.format_time(None) is None


@pytest.mark.parametrize("action,value,expect_arg", [
    ("seek", "2:00", 120.0), ("seek", 90, 90.0), ("forward", None, 10.0), ("forward", "30", 30.0),
    ("rewind", None, 10.0), ("rewind", 5, 5.0), ("play", None, 0), ("pause", None, 0), ("toggle", None, 0),
    ("mute", None, 0), ("unmute", None, 0), ("status", None, 0),
    ("speed", 1.5, 1.5), ("speed", 10, 4.0), ("speed", "0.1", 0.25)])
def test_video_actions_pass_parsed_args(env, action, value, expect_arg):
    svc, pages, _, _ = env
    _video_env(pages, with_video=("GitHub",))
    r = svc.video_control(action, value, tab_query="0")
    assert r["ok"] is False  # tab 0 (Gmail) has no video
    r = svc.video_control(action, value, tab_query="1")
    assert r["ok"] and r["action"] == action
    pages[1].evaluate.assert_called_with(bs._VIDEO_JS, [action, expect_arg])


def test_video_result_shape(env):
    svc, pages, _, _ = env
    _video_env(pages, with_video=("GitHub",))
    r = svc.video_control("status", tab_query="1")
    assert r == {"ok": True, "action": "status", "tab_title": "GitHub", "url": "https://github.com/a",
                 "current_time": 125.0, "current_time_formatted": "02:05", "duration": 3725.0,
                 "duration_formatted": "1:02:05", "paused": False, "muted": False, "playback_rate": 1.0}


@pytest.mark.parametrize("action,value", [("seek", "nope"), ("seek", None), ("speed", "fast"), ("forward", "x"),
                                          ("dance", None)])
def test_video_bad_input_returns_error_without_touching_page(env, action, value):
    svc, pages, _, _ = env
    _video_env(pages, with_video=("GitHub",))
    r = svc.video_control(action, value, tab_query="1")
    assert r["ok"] is False and r["error"]
    pages[1].evaluate.assert_not_called()


def test_video_prefers_youtube_tab(env):
    svc, pages, _, _ = env
    yt = _page("Cool video - YouTube", "https://www.youtube.com/watch?v=abc")
    pages.append(yt)
    _video_env(pages, with_video=("GitHub", "Cool video - YouTube"))  # GitHub has a video too
    r = svc.video_control("pause")
    assert r["ok"] and r["tab_title"] == "Cool video - YouTube"
    pages[1].evaluate.assert_not_called()


def test_video_falls_back_to_first_tab_with_video(env):
    svc, pages, _, _ = env
    _video_env(pages, with_video=("GitHub Issues",))
    r = svc.video_control("play")
    assert r["ok"] and r["tab_title"] == "GitHub Issues"


def test_video_skips_tabs_that_refuse_evaluate(env):
    svc, pages, _, _ = env
    _video_env(pages, with_video=("GitHub",))
    pages[0].evaluate.side_effect = RuntimeError("chrome:// page")
    assert svc.video_control("status")["tab_title"] == "GitHub"


def test_video_explicit_tab_query(env):
    svc, pages, _, _ = env
    _video_env(pages, with_video=("GitHub", "GitHub Issues"))
    r = svc.video_control("status", tab_query="issues")
    assert r["tab_title"] == "GitHub Issues"


def test_video_no_video_clear_error(env):
    svc, pages, _, _ = env
    _video_env(pages, with_video=())
    assert "No <video>" in svc.video_control("status")["error"]
    assert "that tab" in svc.video_control("status", tab_query="1")["error"]


def test_video_explicit_tab_not_found_raises_browser_error(env):
    svc, pages, _, _ = env
    _video_env(pages, with_video=())
    with pytest.raises(bs.BrowserError):
        svc.video_control("status", tab_query="nonexistent")


def test_video_control_safe_and_in_browser_pack():
    from src.agent.tool_router import PACKS, ToolRouter
    g = PermissionGate()
    assert g.is_safe("browser_video_control") and not g.is_dangerous("browser_video_control")
    assert PACKS["browser"][-1] == "browser_video_control"
    reg = T.create_default_registry()
    assert not reg.get("browser_video_control").requires_permission
    for msg in ("انتقل للدقيقة 2 في يوتيوب", "pause the video", "open youtube", "فيديو", "ثانية"):
        r = ToolRouter(reg)
        r.observe(msg)
        assert "browser" in r.active_packs, msg


def test_video_handler_forwards_args():
    svc = MagicMock()
    svc.video_control.return_value = {"ok": True}
    with patch("src.services.browser_service.get_browser_service", return_value=svc):
        assert T.handle_browser_video_control({"action": "seek", "value": "2:00", "tab_query": "yt"}).success
    svc.video_control.assert_called_once_with("seek", "2:00", "yt")
