"""NotesService: to-do list and quick notes persistence, validation and recovery."""
import json

import pytest

from src.services import notes_service as ns
from src.services.notes_service import NotesError, NotesService


@pytest.fixture
def svc(tmp_path):
    return NotesService(tmp_path / "notes.json")


def test_todo_add_list_complete_delete(svc):
    item = svc.todo_add("Buy milk", due="2026-11-21 18:00")
    assert item["task"] == "Buy milk" and item["due"] == "2026-11-21 18:00" and not item["done"]

    assert [t["id"] for t in svc.todo_list()] == [item["id"]]

    done = svc.todo_complete(item["id"])
    assert done["done"] and done["completed"] is not None
    assert svc.todo_list() == []                       # done items hidden by default
    assert [t["id"] for t in svc.todo_list(include_done=True)] == [item["id"]]

    svc.todo_delete(item["id"])
    assert svc.todo_list(include_done=True) == []


def test_todo_complete_is_idempotent_and_keeps_completion_time(svc):
    item = svc.todo_add("Write report")
    first = svc.todo_complete(item["id"])["completed"]
    assert svc.todo_complete(item["id"])["completed"] == first


def test_unknown_todo_id_raises(svc):
    with pytest.raises(NotesError):
        svc.todo_complete("nope")
    with pytest.raises(NotesError):
        svc.todo_delete("")


def test_todo_validation(svc):
    with pytest.raises(NotesError):
        svc.todo_add("   ")
    with pytest.raises(NotesError):
        svc.todo_add("x" * 301)
    with pytest.raises(NotesError):
        svc.todo_add("bad\x00char")
    with pytest.raises(NotesError):
        svc.todo_add("ok", due="d" * 101)


def test_note_save_then_read_by_title_or_id(svc):
    note = svc.note_save("Wifi password", "hunter2")
    assert svc.note_read("Wifi password")["content"] == "hunter2"
    assert svc.note_read("wifi PASSWORD")["id"] == note["id"]        # case-insensitive title
    assert svc.note_read(note["id"])["title"] == "Wifi password"      # by id
    assert svc.note_read("wifi")["id"] == note["id"]                  # unique partial match


def test_note_save_with_same_title_replaces_content(svc):
    first = svc.note_save("Ideas", "one")
    second = svc.note_save("ideas", "two")
    assert second["id"] == first["id"]
    assert len(svc.note_list()) == 1
    assert svc.note_read("Ideas")["content"] == "two"


def test_note_list_newest_first(svc):
    svc.note_save("A", "a")
    svc.note_save("B", "b")
    svc.note_save("A", "a2")   # A updated last
    assert [n["title"] for n in svc.note_list()] == ["A", "B"]


def test_note_read_ambiguous_or_missing_raises(svc):
    svc.note_save("Meeting notes june", "x")
    svc.note_save("Meeting notes july", "y")
    with pytest.raises(NotesError, match="Several notes"):
        svc.note_read("meeting")
    with pytest.raises(NotesError):
        svc.note_read("nothing like this")
    with pytest.raises(NotesError):
        svc.note_read("  ")


def test_note_validation(svc):
    with pytest.raises(NotesError):
        svc.note_save("", "content")
    with pytest.raises(NotesError):
        svc.note_save("t" * 121, "content")
    with pytest.raises(NotesError):
        svc.note_save("t", "c" * (ns.MAX_NOTE_CHARS + 1))
    note = svc.note_save("empty body is fine", "")
    assert note["content"] == ""


def test_data_survives_restart(tmp_path):
    path = tmp_path / "notes.json"
    first = NotesService(path)
    first.todo_add("persist me")
    first.note_save("Persist", "still here")

    second = NotesService(path)
    assert [t["task"] for t in second.todo_list()] == ["persist me"]
    assert second.note_read("Persist")["content"] == "still here"


def test_save_is_atomic_and_leaves_no_temp_file(svc):
    svc.todo_add("one")
    assert svc.path.exists()
    assert not svc.path.with_name(svc.path.name + ".tmp").exists()
    data = json.loads(svc.path.read_text(encoding="utf-8"))
    assert len(data["todos"]) == 1


def test_corrupt_file_is_moved_aside_not_overwritten(tmp_path):
    path = tmp_path / "notes.json"
    path.write_text("{not json", encoding="utf-8")

    svc = NotesService(path)

    assert svc.todo_list() == []
    moved = list(tmp_path.glob("notes.json.corrupt-*"))
    assert len(moved) == 1 and moved[0].read_text(encoding="utf-8") == "{not json"


def test_wrong_shape_json_is_treated_as_corrupt(tmp_path):
    path = tmp_path / "notes.json"
    path.write_text(json.dumps(["a list, not an object"]), encoding="utf-8")
    assert NotesService(path).note_list() == []
    assert list(tmp_path.glob("notes.json.corrupt-*"))


def test_corrupt_file_logs_a_warning(tmp_path, caplog):
    path = tmp_path / "notes.json"
    path.write_text("garbage", encoding="utf-8")
    with caplog.at_level("WARNING"):
        NotesService(path)
    assert any("unreadable" in rec.getMessage() for rec in caplog.records)


def test_singleton_uses_runtime_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("ALTRON_DATA_DIR", str(tmp_path))
    ns.reset_notes_service()
    try:
        service = ns.get_notes_service()
        assert service.path == tmp_path / "notes.json"
        assert ns.get_notes_service() is service
    finally:
        ns.reset_notes_service()
