"""steam_add_to_cart: one atomic tool, stops before checkout."""
from src.agent import tools as tools_mod
from src.agent.permissions import PermissionGate
from src.services import steam_service as ss

BTN = {"label": "Add to Cart", "x": 400, "y": 500}


def _run(locates, name="Hollow Knight", search_results=None, open_problem=None, click_err=None, attempts=5):
    log = {"opened": [], "clicks": [], "scrolls": 0, "sleeps": []}
    queue = list(locates)

    def locate():
        return queue.pop(0) if queue else None

    def open_uri(uri):
        log["opened"].append(uri)
        return open_problem

    def click(x, y):
        log["clicks"].append((x, y))
        return click_err

    def scroll():
        log["scrolls"] += 1

    results = [{"appid": 367520, "name": "Hollow Knight"}] if search_results is None else search_results
    out = ss.add_to_cart(name, search=lambda q: results, open_uri=open_uri, locate=locate,
                         click=click, scroll=scroll, sleep=log["sleeps"].append, attempts=attempts)
    return out, log


def test_found_first_try_clicks_once_and_stops():
    out, log = _run([BTN])
    assert out["added"] and log["clicks"] == [(400, 500)] and log["scrolls"] == 0
    assert log["opened"] == ["steam://store/367520"]
    assert log["sleeps"][0] == ss.PAGE_LOAD_WAIT_S


def test_scrolls_until_button_appears():
    out, log = _run([None, None, BTN])
    assert out["added"] and out["attempts"] == 3
    assert log["scrolls"] == 2 and len(log["clicks"]) == 1


def test_never_clicks_checkout_or_purchase():
    bad = [{"label": "Checkout", "x": 1, "y": 1}, {"label": "Purchase for myself", "x": 2, "y": 2},
           {"label": "Add to Cart and Pay", "x": 3, "y": 3}]
    out, log = _run(bad + [None, None])
    assert not out["added"] and log["clicks"] == []
    assert "could not find" in out["message"]


def test_gives_up_after_five_attempts():
    out, log = _run([], attempts=5)
    assert not out["added"] and log["scrolls"] == 4 and log["clicks"] == []


def test_appid_skips_search_and_click_failure_reported():
    def boom(q):
        raise AssertionError("search must not run for an appid")

    log = []
    out = ss.add_to_cart("367520", search=boom, open_uri=lambda u: None, locate=lambda: BTN,
                         click=lambda x, y: "nope", scroll=lambda: None, sleep=log.append)
    assert not out["added"] and "click failed" in out["message"]


def test_errors():
    for kwargs in ({"name": ""}, {"search_results": []}, {"open_problem": "no steam"}):
        try:
            _run([BTN], **kwargs)
        except ss.SteamError:
            continue
        raise AssertionError(f"expected SteamError for {kwargs}")


def test_handler_wires_mocks_and_stops_before_checkout(monkeypatch):
    clicks, scrolls = [], []
    monkeypatch.setattr(tools_mod, "_public_get_text", lambda url: '{"items":[{"id":1,"name":"G"}]}')
    monkeypatch.setattr(tools_mod, "_steam_open_uri", lambda uri: None)
    monkeypatch.setattr(tools_mod, "_steam_scroll_down", lambda: scrolls.append(1))
    monkeypatch.setattr(tools_mod, "_steam_click", lambda x, y: clicks.append((x, y)))
    seq = iter([None, {"label": "Checkout", "x": 9, "y": 9}, BTN])
    monkeypatch.setattr(tools_mod, "_steam_locate_add_to_cart", lambda: next(seq))
    monkeypatch.setattr("time.sleep", lambda s: None)
    res = tools_mod.handle_steam_add_to_cart({"name_or_appid": "G"})
    assert res.success and clicks == [(400, 500)] and len(scrolls) == 2


def test_registered_and_safe():
    assert tools_mod.create_default_registry().get("steam_add_to_cart") is not None
    assert "steam_add_to_cart" in PermissionGate.SAFE_TOOLS
