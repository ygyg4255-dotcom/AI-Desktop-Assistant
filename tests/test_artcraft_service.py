"""Artcraft integration: DeckCraft, WordCraft, GridCraft and PdfCraft.

Pure logic (planners, parsers, path rules, refusals) runs everywhere. The round-trip tests drive
the real Artcraft engines and are skipped when an app is not installed. Every file is written
under tmp_path; nothing touches the user's Desktop.
"""
import json
import subprocess
import zipfile

import pytest

from src.services import artcraft_service as ac


ENGINES_INSTALLED = all(
    (ac.ARTCRAFT_ROOT / spec["folder"] / spec["engine"]).is_file() for spec in ac.APPS.values()
)
needs_engines = pytest.mark.skipif(not ENGINES_INSTALLED, reason="Artcraft engines are not installed")


@pytest.fixture
def no_open(monkeypatch):
    """Record open requests instead of launching any app."""
    calls = []
    monkeypatch.setattr(ac, "open_in_artcraft", lambda path: calls.append(path) or "Test")
    return calls


def _write_deck(path, slides):
    """A minimal DeckCraft archive in the discovered layout (mimetype first, then presentation.json)."""
    shapes_for = lambda title, body: [
        {"id": 1, "name": "Title", "ph": {"kind": "title", "idx": 0},
         "text": {"paragraphs": [{"level": 0, "runs": [{"text": title}]}]}},
        {"id": 2, "name": "Body", "ph": {"kind": "body", "idx": 1},
         "text": {"paragraphs": [{"level": 0, "runs": [{"text": line}]} for line in body]}},
    ]
    data = {"format": "deckcraft", "version": 1, "presentation": {
        "slides": [{"id": 10 + i, "layout": 18, "shapes": shapes_for(t, b)} for i, (t, b) in enumerate(slides)]}}
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("mimetype", ac.DECK_MIME)
        archive.writestr("presentation.json", json.dumps(data))


# ---------------------------------------------------------------- DeckCraft logic

def test_deckcraft_read_extracts_titles_and_body_from_archive(tmp_path):
    deck = tmp_path / "a.deckcraft"
    _write_deck(deck, [("Intro", ["First", "Second"]), ("Done", [])])
    result = ac.deckcraft_read(str(deck))
    assert result["slide_count"] == 2
    assert result["slides"][0]["title"] == "Intro"
    assert result["slides"][0]["body"] == ["First", "Second"]
    assert result["slides"][1]["body"] == []


def test_deckcraft_read_rejects_wrong_mimetype(tmp_path):
    bad = tmp_path / "bad.deckcraft"
    with zipfile.ZipFile(bad, "w") as archive:
        archive.writestr("mimetype", "application/zip")
        archive.writestr("presentation.json", "{}")
    with pytest.raises(ac.ArtcraftError, match="not a DeckCraft file"):
        ac.deckcraft_read(str(bad))


def test_plan_deck_edit_update_keeps_old_body_and_shifts_indices():
    slides = [{"title": "A", "body": ["a1"]}, {"title": "B", "body": ["b1"]}]
    commands, model = ac._plan_deck_edit(slides, [{"op": "update", "index": 1, "title": "B2"}])
    assert commands == [
        ("slide.new", {"layout": "titleAndContent", "title": "B2", "body": "b1", "at": 1}),
        ("slide.delete", {"index": 2}),
    ]
    assert model[1] == {"title": "B2", "body": ["b1"], "layout": None}


def test_plan_deck_edit_add_move_delete_model_matches_expected_order():
    slides = [{"title": "A", "body": []}, {"title": "B", "body": []}, {"title": "C", "body": []}]
    ops = [
        {"op": "add", "title": "D", "bullets": ["x"]},          # appended
        {"op": "move", "from": 3, "to": 0},                     # D to the front
        {"op": "delete", "index": 2},                           # removes B
    ]
    commands, model = ac._plan_deck_edit(slides, ops)
    assert [s["title"] for s in model] == ["D", "A", "C"]
    assert commands[0][1]["at"] == 3
    assert commands[1] == ("slide.move", {"from": 3, "to": 0})
    assert commands[2] == ("slide.delete", {"index": 2})


@pytest.mark.parametrize("op", [
    {"op": "delete", "index": 5},
    {"op": "delete", "index": True},
    {"op": "move", "from": 0, "to": 9},
    {"op": "explode"},
    "not-a-dict",
])
def test_plan_deck_edit_rejects_bad_operations(op):
    slides = [{"title": "A", "body": []}, {"title": "B", "body": []}]
    with pytest.raises(ac.ArtcraftError):
        ac._plan_deck_edit(slides, [op])


def test_plan_deck_edit_requires_operations():
    with pytest.raises(ac.ArtcraftError, match="non-empty"):
        ac._plan_deck_edit([], [])


def test_deck_create_commands_start_with_title_slide_and_remove_blank_first():
    commands = ac._deck_create_commands("Q3", [ac._content_from_op({"title": "One", "bullets": ["p"]})])
    assert commands[0] == ("slide.new", {"layout": "title", "title": "Q3"})
    assert commands[-2] == ("slide.delete", {"index": 0})
    assert commands[-1] == ("file.properties", {"title": "Q3"})


# ---------------------------------------------------------------- WordCraft / GridCraft logic

def test_word_edit_commands_map_operations_and_escape_caret():
    commands = ac._word_edit_commands([
        {"op": "append_heading", "text": "Hi", "level": 2},
        {"op": "replace", "find": "a^b", "replace": "c"},
    ])
    assert commands[0] == ("caret.docEnd", {})
    assert ("para.style", {"style": "Heading 2"}) in commands
    assert ("edit.replaceAll", {"text": "a^^b", "with": "c"}) in commands


def test_word_edit_rejects_unknown_op_and_empty_list():
    with pytest.raises(ac.ArtcraftError, match="unknown op"):
        ac._word_edit_commands([{"op": "rotate"}])
    with pytest.raises(ac.ArtcraftError, match="non-empty"):
        ac._word_edit_commands([])


def test_section_specs_validate_heading_level():
    with pytest.raises(ac.ArtcraftError, match="Heading level"):
        ac._section_specs([{"heading": "X", "level": 7}])


def test_plan_grid_edit_tracks_sheet_names_and_refuses_duplicates():
    lines, names = ac._plan_grid_edit(["Budget"], [
        {"op": "add_sheet", "name": "Extra"},
        {"op": "rename_sheet", "sheet": "Budget", "name": "Plan"},
        {"op": "set", "sheet": "Plan", "range": "a1", "values": [["x", 1]]},
    ])
    assert names == ["Plan", "Extra"]
    assert lines[0].startswith('home.insertSheet={"name": "Extra", "before": 1}')
    assert lines[-1].startswith('range.setValues={"range": "A1"')
    with pytest.raises(ac.ArtcraftError, match="already exists"):
        ac._plan_grid_edit(["Budget"], [{"op": "add_sheet", "name": "budget"}])


def test_grid_sheet_specs_reject_bad_and_duplicate_names():
    with pytest.raises(ac.ArtcraftError, match="not a valid sheet name"):
        ac._grid_sheet_specs([{"name": "bad:name", "rows": []}])
    with pytest.raises(ac.ArtcraftError, match="used twice"):
        ac._grid_sheet_specs([{"name": "A", "rows": []}, {"name": "a", "rows": []}])


def test_grid_rows_pad_and_reject_nested_values():
    assert ac._grid_rows([[1, 2], ["x"]]) == [[1, 2], ["x", None]]
    with pytest.raises(ac.ArtcraftError):
        ac._grid_rows([[{"nested": 1}]])


# ---------------------------------------------------------------- PdfCraft logic

def test_page_numbers_parse_ranges_and_bounds():
    assert ac._page_numbers("1-3,5") == [1, 2, 3, 5]
    assert ac._page_numbers(["2", "4-5"]) == [2, 4, 5]
    with pytest.raises(ac.ArtcraftError, match="does not exist"):
        ac._page_numbers("1-9", count=3)
    with pytest.raises(ac.ArtcraftError, match="start at 1"):
        ac._page_numbers("0")
    with pytest.raises(ac.ArtcraftError):
        ac._page_numbers("abc")


def test_range_label_compresses_runs():
    assert ac._range_label([1, 2, 3, 5]) == "1-3_5"
    assert ac._range_label([4]) == "4"


# ---------------------------------------------------------------- paths, refusals, opening

def test_create_refuses_to_overwrite_existing_file(tmp_path):
    existing = tmp_path / "taken.docx"
    existing.write_bytes(b"do not touch")
    with pytest.raises(ac.ArtcraftError, match="already exists"):
        ac.wordcraft_create(str(existing), "T", [], open_after=False)
    assert existing.read_bytes() == b"do not touch"


def test_wrong_extension_is_refused(tmp_path):
    with pytest.raises(ac.ArtcraftError, match="must end in"):
        ac.deckcraft_create(str(tmp_path / "deck.pptx"), "T", [], open_after=False)


def test_relative_paths_resolve_to_desktop(tmp_path, monkeypatch):
    monkeypatch.setattr(ac.Path, "home", classmethod(lambda cls: tmp_path))
    assert ac._user_path("notes.docx") == tmp_path / "Desktop" / "notes.docx"


def test_missing_engine_is_reported_clearly(tmp_path, monkeypatch):
    monkeypatch.setattr(ac, "ARTCRAFT_ROOT", tmp_path / "nowhere")
    with pytest.raises(ac.ArtcraftError, match="is not installed"):
        ac.gridcraft_create(str(tmp_path / "x.xlsx"), None, open_after=False)


def test_open_routes_by_extension(tmp_path, no_open):
    assert ac.app_for_path(tmp_path / "a.pdf") == "pdfcraft"
    assert ac.app_for_path(tmp_path / "a.xlsx") == "gridcraft"
    with pytest.raises(ac.ArtcraftError, match="No Artcraft app"):
        ac.app_for_path(tmp_path / "a.exe")


def test_create_opens_result_when_requested(tmp_path, monkeypatch, no_open):
    def fake_engine(app, args, **kwargs):
        # Stand-in for the engine: write a valid DeckCraft archive at the --save path.
        target = args[args.index("--save") + 1]
        _write_deck(target, [("Only", [])])
        return ""

    monkeypatch.setattr(ac, "_engine", fake_engine)
    dest = tmp_path / "opened.deckcraft"
    result = ac.deckcraft_create(str(dest), "Opened", [], open_after=True)
    assert result["slide_count"] == 1
    assert no_open == [dest]
    assert not list(tmp_path.glob("*.part-*"))  # staging file was moved, not left behind


def test_failed_edit_leaves_original_untouched(tmp_path, monkeypatch):
    deck = tmp_path / "keep.deckcraft"
    _write_deck(deck, [("A", []), ("B", [])])
    before = deck.read_bytes()

    def broken_engine(app, args, **kwargs):
        # Writes three slides where the edit expects one; the service must refuse to replace the file.
        _write_deck(args[args.index("--save") + 1], [("A", []), ("B", []), ("C", [])])
        return ""

    monkeypatch.setattr(ac, "_engine", broken_engine)
    with pytest.raises(ac.ArtcraftError, match="left unchanged"):
        ac.deckcraft_edit(str(deck), [{"op": "delete", "index": 0}])
    assert deck.read_bytes() == before
    assert not list(tmp_path.glob("*.part-*"))


# ---------------------------------------------------------------- round trips on the real engines

@needs_engines
def test_deckcraft_round_trip(tmp_path):
    dest = tmp_path / "round.deckcraft"
    created = ac.deckcraft_create(str(dest), "Q3 Review", [
        {"title": "Revenue", "bullets": ["Up 12%", "Costs flat"]},
        {"title": "Plan", "bullets": ["Hire", "Ship"]},
        "Thanks",
    ], open_after=False)
    assert created["slide_count"] == 4
    assert [s["title"] for s in ac.deckcraft_read(str(dest))["slides"]] == ["Q3 Review", "Revenue", "Plan", "Thanks"]

    ac.deckcraft_edit(str(dest), [
        {"op": "update", "index": 2, "title": "Plan v2"},
        {"op": "add", "title": "Appendix", "bullets": ["x"]},
        {"op": "move", "from": 4, "to": 1},
        {"op": "delete", "index": 0},
    ])
    slides = ac.deckcraft_read(str(dest))["slides"]
    assert [s["title"] for s in slides] == ["Appendix", "Revenue", "Plan v2", "Thanks"]
    assert slides[2]["body"] == ["Hire", "Ship"]  # update kept the old body
    assert slides[0]["body"] == ["x"]


@needs_engines
def test_wordcraft_round_trip(tmp_path):
    dest = tmp_path / "round.docx"
    ac.wordcraft_create(str(dest), "Status", [
        {"heading": "Summary", "paragraphs": ["All good."], "bullets": ["One", "Two"]},
        {"heading": "Next", "level": 2, "paragraphs": ["Ship it"]},
    ], open_after=False)
    doc = ac.wordcraft_read(str(dest))
    styles = {p["text"]: p["style"] for p in doc["paragraphs"]}
    assert styles["Status"] == "Title"
    assert styles["Summary"] == "Heading1"
    assert styles["Next"] == "Heading2"
    assert "All good." in doc["text"]

    ac.wordcraft_edit(str(dest), [
        {"op": "append_heading", "text": "Appendix", "level": 1},
        {"op": "append_paragraph", "text": "Added later"},
        {"op": "replace", "find": "All good", "replace": "All great"},
    ])
    text = ac.wordcraft_read(str(dest))["text"]
    assert "All great." in text and "Appendix" in text and "Added later" in text


@needs_engines
def test_gridcraft_round_trip(tmp_path):
    dest = tmp_path / "round.xlsx"
    ac.gridcraft_create(str(dest), [
        {"name": "Budget", "rows": [["Item", "Cost"], ["Rent", 1200], ["Food", "=B2*0.5"]]},
        {"name": "Notes", "rows": [["hello"]]},
    ], open_after=False)
    read = ac.gridcraft_read(str(dest), sheet="Budget")["sheets"][0]
    assert read["rows"][0] == ["Item", "Cost"]
    assert read["rows"][2] == ["Food", "600"]  # formula value is computed by the engine

    ac.gridcraft_edit(str(dest), [
        {"op": "set", "sheet": "Budget", "range": "B4", "values": [["=SUM(B2:B3)"]]},
        {"op": "add_sheet", "name": "Extra"},
        {"op": "rename_sheet", "sheet": "Notes", "name": "Log"},
    ])
    names =[s["name"] for s in ac.gridcraft_read(str(dest))["sheets"]]
    assert names == ["Budget", "Log", "Extra"]
    assert ac.gridcraft_read(str(dest), sheet="Budget")["sheets"][0]["rows"][3] == ["", "1800"]


@needs_engines
def test_pdfcraft_merge_split_read_round_trip(tmp_path):
    word = ac.ARTCRAFT_ROOT / "WordCraft" / "wordcraft-cli.exe"
    pdfs = []
    for n in (1, 2):
        docx = tmp_path / f"p{n}.docx"
        ac.wordcraft_create(str(docx), f"P{n}", [{"paragraphs": [f"Page {n} text"]}], open_after=False)
        pdf = tmp_path / f"p{n}.pdf"
        subprocess.run([str(word), "convert", str(docx), str(pdf)], check=True, capture_output=True, timeout=180)
        pdfs.append(str(pdf))

    assert ac.pdfcraft_read(pdfs[0])["page_count"] == 1
    merged = ac.pdfcraft_merge(pdfs, str(tmp_path / "merged.pdf"), open_after=False)
    assert merged["page_count"] == 2
    assert "Page 2 text" in ac.pdfcraft_read(merged["path"], pages="2")["pages"][0]["text"]

    split = ac.pdfcraft_split(merged["path"], ["1", "2"], str(tmp_path / "split"))
    assert len(split["outputs"]) == 2
    assert all(ac.pdfcraft_read(o["path"])["page_count"] == 1 for o in split["outputs"])


