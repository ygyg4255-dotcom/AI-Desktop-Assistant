"""All file tools resolve 'desktop' to the real (OneDrive) Desktop."""
import pytest

from src.agent import tools


@pytest.fixture
def od_desktop(tmp_path, monkeypatch):
    d = tmp_path / "OneDrive" / "Desktop"
    d.mkdir(parents=True)
    (d / "report.txt").write_text("x")
    monkeypatch.setattr(tools, "_desktop_dir", lambda: d)
    return d


@pytest.mark.parametrize("value", ["desktop", "Desktop/report.txt", "~/Desktop/report.txt", r"~\Desktop\report.txt"])
def test_resolve_desktop_variants(od_desktop, value):
    assert str(tools.resolve_user_path(value)).startswith(str(od_desktop))


def test_non_desktop_path_unchanged(od_desktop, tmp_path):
    assert tools.resolve_user_path(str(tmp_path / "a")) == tmp_path / "a"


def test_file_search_finds_onedrive_desktop(od_desktop):
    res = tools.handle_file_search({"query": "report", "directory": "desktop"})
    assert res.success and res.output["count"] == 1


def test_file_delete_resolves_onedrive_desktop(od_desktop):
    gate = tools.handle_file_delete({"path": "desktop/report.txt"})
    assert gate.requires_permission and (od_desktop / "report.txt").exists()
    res = tools.handle_file_delete({"path": "desktop/report.txt"}, approved=True)
    assert res.success and not (od_desktop / "report.txt").exists()


def test_system_prompt_has_no_double_confirmation():
    from pathlib import Path
    text = (Path(__file__).parent.parent / "prompts" / "system_prompt.txt").read_text(encoding="utf-8")
    assert "PIN prompt is the confirmation" in text
