"""S8: runtime state lives outside the project root."""
from pathlib import Path

from src.config import paths
from src.agent.memory import ConversationMemory

ROOT = Path(paths.__file__).resolve().parent.parent.parent


def test_runtime_file_uses_data_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("ALTRON_DATA_DIR", str(tmp_path))
    assert paths.runtime_file("zz_test.json").parent == tmp_path


def test_default_memory_not_in_project_root(tmp_path, monkeypatch):
    monkeypatch.setenv("ALTRON_DATA_DIR", str(tmp_path))
    mem = ConversationMemory()
    assert mem._file_path.parent == tmp_path
    assert ROOT not in mem._file_path.parents


def test_legacy_file_migrated_once(tmp_path, monkeypatch):
    legacy_root = tmp_path / "proj"
    legacy_root.mkdir()
    (legacy_root / "memory.json").write_text('[{"role":"user","content":"hi"}]', encoding="utf-8")
    monkeypatch.setattr(paths, "_LEGACY_ROOT", legacy_root)
    monkeypatch.setenv("ALTRON_DATA_DIR", str(tmp_path / "data"))
    target = paths.runtime_file("memory.json")
    assert "hi" in target.read_text(encoding="utf-8")
    assert not (legacy_root / "memory.json").exists()


def test_gitignore_lists_runtime_files():
    ignore = (ROOT / ".gitignore").read_text(encoding="utf-8")
    assert "status.json" in ignore and "memory.json" in ignore
