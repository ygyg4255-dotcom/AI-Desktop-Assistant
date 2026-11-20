"""Steam service: VDF/ACF parsing, steam:// URIs, mocked store search, and tool wiring (no network)."""
import json
from pathlib import Path

import pytest

from src.agent import tools
from src.agent.permissions import PermissionGate
from src.services import steam_service as steam

LIBRARY_VDF = r'''
"libraryfolders"
{
	"0"
	{
		"path"		"C:\Program Files (x86)\Steam"
		"apps"
		{
			"730"		"1234"
		}
	}
	"1"
	{
		"path"		"__SECOND__"
	}
}
'''

ACF = '''"AppState"
{
	"appid"		"%d"
	"name"		"%s"
	"installdir"		"%s"
}
'''


def _steam_tree(tmp_path: Path) -> Path:
    root = tmp_path / "Steam"
    second = tmp_path / "Games"
    (root / "steamapps").mkdir(parents=True)
    (second / "steamapps").mkdir(parents=True)
    vdf = LIBRARY_VDF.replace(r"C:\Program Files (x86)\Steam", str(root).replace("\\", "\\\\"))
    vdf = vdf.replace("__SECOND__", str(second).replace("\\", "\\\\"))
    (root / "steamapps" / "libraryfolders.vdf").write_text(vdf, encoding="utf-8")
    (root / "steamapps" / "appmanifest_730.acf").write_text(ACF % (730, "Counter-Strike 2", "cs2"), encoding="utf-8")
    (second / "steamapps" / "appmanifest_620.acf").write_text(ACF % (620, "Portal 2", "Portal 2"), encoding="utf-8")
    (second / "steamapps" / "appmanifest_bad.acf").write_text("garbage", encoding="utf-8")
    return root


def test_parse_vdf_nested_and_escapes():
    data = steam.parse_vdf(LIBRARY_VDF)
    folders = data["libraryfolders"]
    assert folders["0"]["path"] == r"C:\Program Files (x86)\Steam"
    assert folders["0"]["apps"]["730"] == "1234"
    assert folders["1"]["path"] == "__SECOND__"


def test_parse_acf_appstate():
    state = steam.parse_vdf(ACF % (440, "Team Fortress 2", "tf2"))["AppState"]
    assert state["appid"] == "440" and state["name"] == "Team Fortress 2"


def test_list_installed_scans_all_libraries(tmp_path):
    games = steam.list_installed(_steam_tree(tmp_path))
    assert {g["appid"] for g in games} == {730, 620}
    assert [g["name"] for g in games] == sorted(g["name"] for g in games)


def test_resolve_installed_by_name_and_appid(tmp_path):
    games = steam.list_installed(_steam_tree(tmp_path))
    assert steam.resolve_installed("portal", games)["appid"] == 620
    assert steam.resolve_installed("730", games)["name"] == "Counter-Strike 2"
    with pytest.raises(steam.SteamError):
        steam.resolve_installed("nothing here", games)
    with pytest.raises(steam.SteamError):
        steam.resolve_installed("999", games)


def test_uri_building_and_validation():
    assert steam.launch_uri(730) == "steam://rungameid/730"
    assert steam.store_uri("620") == "steam://store/620"
    assert steam.cart_uri() == "steam://openurl/https://store.steampowered.com/cart/"
    for bad in ("abc", "", "-1", "0", "730/../x", "730 -applaunch"):
        with pytest.raises(steam.SteamError):
            steam.store_uri(bad)


SEARCH_JSON = json.dumps({"total": 2, "items": [
    {"id": 620, "name": "Portal 2", "price": {"currency": "USD", "initial": 999, "final": 199}},
    {"id": 400, "name": "Portal", "price": {"currency": "USD", "initial": 999, "final": 999}},
    {"id": 1, "name": "Free Thing"},
]})


def test_search_store_mocked():
    seen = []

    def fake_fetch(url):
        seen.append(url)
        return SEARCH_JSON

    results = steam.search_store("portal 2", fake_fetch, limit=5)
    assert seen == ["https://store.steampowered.com/api/storesearch/?term=portal%202&l=english&cc=US"]
    assert results[0] == {"appid": 620, "name": "Portal 2", "price": "1.99 USD",
                          "discount_percent": 80, "original_price": "9.99 USD"}
    assert results[1]["discount_percent"] == 0
    assert results[2]["price"] == "Free or not available"


def test_search_store_rejects_bad_input():
    with pytest.raises(steam.SteamError):
        steam.search_store("  ", lambda u: "{}")
    with pytest.raises(steam.SteamError):
        steam.search_store("x" * 500, lambda u: "{}")
    with pytest.raises(steam.SteamError):
        steam.search_store("portal", lambda u: "<html>")


@pytest.fixture
def launched(monkeypatch):
    started = []
    monkeypatch.setattr(tools.platform, "system", lambda: "Windows")
    monkeypatch.setattr(tools.os, "startfile", lambda t: started.append(t), raising=False)
    monkeypatch.setattr(tools, "_public_get_text", lambda url: SEARCH_JSON)
    return started


def test_tools_are_safe_and_registered():
    registry = tools.create_default_registry()
    gate = PermissionGate()
    for name in ("steam_list_installed", "steam_launch_game", "steam_search_store", "steam_open_store_page"):
        assert registry.get(name) is not None
        assert name in PermissionGate.SAFE_TOOLS
    assert gate is not None


def test_launch_game_tool(monkeypatch, launched, tmp_path):
    root = _steam_tree(tmp_path)
    monkeypatch.setattr(steam, "find_steam_path", lambda: root)
    res = tools.handle_steam_launch_game({"name_or_appid": "portal 2"})
    assert res.success and launched == ["steam://rungameid/620"]
    assert not tools.handle_steam_launch_game({"name_or_appid": "zzz"}).success


def test_open_store_page_tool(launched):
    assert tools.handle_steam_open_store_page({"appid_or_query": "620"}).success
    assert tools.handle_steam_open_store_page({"appid_or_query": "portal 2"}).success
    assert tools.handle_steam_open_store_page({"appid_or_query": "cart"}).success
    assert launched == ["steam://store/620", "steam://store/620",
                        "steam://openurl/https://store.steampowered.com/cart/"]


def test_search_tool_network_error(monkeypatch):
    def boom(url):
        raise OSError("down")
    monkeypatch.setattr(tools, "_public_get_text", boom)
    res = tools.handle_steam_search_store({"query": "portal"})
    assert not res.success
