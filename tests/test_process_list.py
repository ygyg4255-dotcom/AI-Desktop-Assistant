from types import SimpleNamespace
from unittest.mock import patch

from src.agent.tools import handle_process_list

MB = 1024 * 1024


def _p(pid, name, mb):
    return SimpleNamespace(info={"pid": pid, "name": name, "cpu_percent": 0,
                                 "memory_info": SimpleNamespace(rss=mb * MB)})


def _run(procs, **params):
    with patch("psutil.process_iter", return_value=procs):
        return handle_process_list(params).output


def test_aggregates_by_name_and_sorts_by_ram():
    out = _run([_p(1, "chrome.exe", 100), _p(2, "Chrome.exe", 200), _p(3, "big.exe", 250), _p(4, "a.exe", 1)])
    assert out["count"] == 4
    assert [g["name"] for g in out["processes"]][:2] == ["chrome.exe", "big.exe"]
    assert out["processes"][0]["memory_mb"] == 300 and out["processes"][0]["instances"] == 2


def test_name_filter_finds_low_ram_process_beyond_index_50():
    procs = [_p(i, f"p{i}.exe", 500 - i) for i in range(100)] + [_p(999, "windhawk.exe", 1)]
    out = _run(procs, name_filter="WindHawk")
    assert out["count"] == 101
    assert [g["name"] for g in out["processes"]] == ["windhawk.exe"]
