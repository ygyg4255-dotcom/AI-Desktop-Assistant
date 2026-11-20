"""Native integration with the Artcraft apps: DeckCraft, WordCraft, GridCraft and PdfCraft.

Each Artcraft app installs a headless engine next to its window (``<app>-cli.exe``). This
module drives those engines with argument lists (never a shell), so every file it writes is
the app's own output and opens in the matching app. DeckCraft archives are also read straight
from their ZIP/JSON layout, the schema found on this PC:

    mimetype           application/vnd.storyteller.deckcraft+zip
    presentation.json  {"format": "deckcraft", "version": 1, "presentation": {"slides": [...]}}

Writes never touch the target directly: the engine writes a sibling staging file, the result is
checked, and only then is it moved onto the destination. Create refuses to overwrite a file.
Relative paths resolve to the user's Desktop.
"""
from __future__ import annotations

import csv
import io
import json
import math
import os
import re
import subprocess
import tempfile
import uuid
import zipfile
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional, Sequence, Tuple

from src.config.log import get_logger, redact_secrets

logger = get_logger(__name__)

ARTCRAFT_ROOT = Path(r"C:\Program Files\ArtCraft")
DECK_MIME = "application/vnd.storyteller.deckcraft+zip"
ENGINE_TIMEOUT = 180  # seconds per engine call
_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
_DETACHED = getattr(subprocess, "DETACHED_PROCESS", 0)

APPS: Dict[str, Dict[str, Any]] = {
    "deckcraft": {"label": "DeckCraft", "folder": "DeckCraft", "engine": "deckcraft-cli.exe", "gui": "deckcraft.exe"},
    "wordcraft": {"label": "WordCraft", "folder": "WordCraft", "engine": "wordcraft-cli.exe", "gui": "wordcraft.exe"},
    "gridcraft": {"label": "GridCraft", "folder": "GridCraft", "engine": "gridcraft-cli.exe", "gui": "gridcraft.exe"},
    "pdfcraft": {"label": "PdfCraft", "folder": "PdfCraft", "engine": "pdfcraft-cli.exe", "gui": "pdfcraft.exe"},
}
# File extension -> app that opens it.
OPENABLE = {".deckcraft": "deckcraft", ".docx": "wordcraft", ".xlsx": "gridcraft",
            ".csv": "gridcraft", ".pdf": "pdfcraft"}
DECK_EXTS = (".deckcraft",)
WORD_EXTS = (".docx",)
GRID_EXTS = (".xlsx",)
GRID_READ_EXTS = (".xlsx", ".csv")
PDF_EXTS = (".pdf",)

_RANGE_RE = re.compile(r"^[A-Za-z]{1,3}\d{1,7}(:[A-Za-z]{1,3}\d{1,7})?$")
_PAGE_TOKEN_RE = re.compile(r"^\s*(\d+)\s*(?:-\s*(\d+)\s*)?$")
_SHEET_BAD_CHARS = re.compile(r"[\[\]:*?/\\]")


class ArtcraftError(Exception):
    """An Artcraft engine is missing, rejected the input, or failed."""


# ---------------------------------------------------------------- paths and engines

def _user_path(raw: object) -> Path:
    text = str(raw or "").strip().strip('"').strip()
    if not text:
        raise ArtcraftError("A file path is required.")
    path = Path(os.path.expandvars(os.path.expanduser(text)))
    if not path.is_absolute():
        path = Path.home() / "Desktop" / path
    return path


def _check_ext(path: Path, exts: Sequence[str]) -> None:
    if path.suffix.lower() not in exts:
        raise ArtcraftError(f"'{path.name}' must end in {' or '.join(exts)}.")


def _source(raw: object, exts: Sequence[str]) -> Path:
    path = _user_path(raw)
    _check_ext(path, exts)
    if not path.is_file():
        raise ArtcraftError(f"File not found: {path}")
    return path


def _new_target(raw: object, exts: Sequence[str]) -> Path:
    path = _user_path(raw)
    _check_ext(path, exts)
    if path.exists():
        raise ArtcraftError(f"{path} already exists. Choose another name, or edit that file instead.")
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


@contextmanager
def _staged(dest: Path) -> Iterator[Path]:
    """Yield a sibling staging path; move it onto ``dest`` only if the block finishes."""
    stage = dest.with_name(f"{dest.stem}.part-{uuid.uuid4().hex[:8]}{dest.suffix}")
    try:
        yield stage
        if not stage.is_file():
            raise ArtcraftError(f"The engine did not write {dest.name}.")
        os.replace(stage, dest)
    finally:
        try:
            stage.unlink(missing_ok=True)
        except OSError as err:
            logger.debug("Could not remove staging file %s: %s", stage, err)


def _engine_path(app: str) -> Path:
    spec = APPS[app]
    candidate = ARTCRAFT_ROOT / spec["folder"] / spec["engine"]
    if candidate.is_file():
        return candidate
    raise ArtcraftError(f"{spec['label']} is not installed: expected {candidate}.")


def _engine(app: str, args: Sequence[str], *, timeout: int = ENGINE_TIMEOUT) -> str:
    """Run an app's engine with an argument list (no shell) and return its stdout."""
    spec = APPS[app]
    exe = _engine_path(app)
    logger.debug("Artcraft %s engine: %s", spec["label"], args[0] if args else "")
    try:
        proc = subprocess.run(
            [str(exe), *args], capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=timeout, creationflags=_NO_WINDOW, check=False,
        )
    except subprocess.TimeoutExpired as err:
        raise ArtcraftError(f"{spec['label']} took longer than {timeout} seconds and was stopped.") from err
    except OSError as err:
        raise ArtcraftError(f"Could not start {spec['label']}: {redact_secrets(err)}") from err
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or "").strip()[-600:]
        raise ArtcraftError(
            f"{spec['label']} failed (exit {proc.returncode}): {redact_secrets(detail) or 'no details given'}"
        )
    return proc.stdout


def _parse_json(text: str, what: str) -> Any:
    try:
        return json.loads(text)
    except json.JSONDecodeError as err:
        raise ArtcraftError(f"{what} returned output that is not JSON.") from err


def _cmd_line(command: str, params: Dict[str, Any]) -> str:
    """One ``id={json}`` line, as read by DeckCraft --cmd-file and GridCraft --script."""
    return f"{command}={json.dumps(params, ensure_ascii=False)}"


def _write_lines(directory: Path, lines: Sequence[str]) -> Path:
    path = directory / "commands.txt"
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def _maybe_open(dest: Path, open_after: bool) -> Optional[str]:
    return open_in_artcraft(dest) if open_after else None


def open_in_artcraft(path: Path) -> str:
    """Open a file in its Artcraft app, or the OS default handler when the app is missing.

    Returns the name of whatever opened the file.
    """
    path = Path(path)
    app = app_for_path(path)
    spec = APPS[app]
    gui = ARTCRAFT_ROOT / spec["folder"] / spec["gui"]
    try:
        if gui.is_file():
            subprocess.Popen([str(gui), str(path)], creationflags=_DETACHED, close_fds=True)
            return spec["label"]
        if hasattr(os, "startfile"):
            os.startfile(str(path))  # noqa: S606 - the OS handler opens a path we just wrote
            return "the default app"
    except OSError as err:
        raise ArtcraftError(f"Could not open {path.name}: {redact_secrets(err)}") from err
    raise ArtcraftError(f"Could not open {path.name}: no Artcraft app or default handler was found.")


def app_for_path(path: Path) -> str:
    key = Path(path).suffix.lower()
    if key not in OPENABLE:
        raise ArtcraftError(f"No Artcraft app opens '{key or 'this'}' files.")
    return OPENABLE[key]


# ---------------------------------------------------------------- shared input helpers

def _as_lines(raw: object) -> List[str]:
    if raw is None:
        return []
    if isinstance(raw, str):
        items: List[str] = raw.splitlines()
    elif isinstance(raw, (list, tuple)):
        items = [str(item) for item in raw]
    else:
        raise ArtcraftError("Text must be a string or a list of strings.")
    return [item.strip() for item in items if item.strip()]


def _index(op: Dict[str, Any], key: str, limit: int) -> int:
    value = op.get(key)
    if isinstance(value, bool) or not isinstance(value, int):
        raise ArtcraftError(f"'{key}' must be a whole number (slide indices start at 0).")
    if not 0 <= value < limit:
        raise ArtcraftError(f"'{key}' {value} is out of range (valid: 0 to {limit - 1}).")
    return value


# ---------------------------------------------------------------- DeckCraft

def _load_deck(path: Path) -> Dict[str, Any]:
    try:
        with zipfile.ZipFile(path) as archive:
            mimetype = archive.read("mimetype").decode("ascii", "replace").strip()
            if mimetype != DECK_MIME:
                raise ArtcraftError(f"{path.name} is not a DeckCraft file (mimetype {mimetype!r}).")
            data = json.loads(archive.read("presentation.json").decode("utf-8"))
    except (zipfile.BadZipFile, KeyError, UnicodeDecodeError, json.JSONDecodeError) as err:
        raise ArtcraftError(f"{path.name} is not a readable DeckCraft file: {err}") from err
    presentation = data.get("presentation") if isinstance(data, dict) else None
    if not isinstance(presentation, dict):
        raise ArtcraftError(f"{path.name} has no presentation section.")
    return presentation


def _paragraph_text(paragraph: Dict[str, Any]) -> str:
    return "".join(str(run.get("text") or "") for run in paragraph.get("runs") or [])


def _shape_paragraphs(shape: Dict[str, Any]) -> List[str]:
    text = shape.get("text") or {}
    paragraphs = (_paragraph_text(p) for p in text.get("paragraphs") or [])
    return [line for line in paragraphs if line.strip()]


def _slide_summary(index: int, slide: Dict[str, Any]) -> Dict[str, Any]:
    title = ""
    body: List[str] = []
    for shape in slide.get("shapes") or []:
        paragraphs = _shape_paragraphs(shape)
        if not paragraphs:
            continue
        ph_kind = str((shape.get("ph") or {}).get("kind") or "")
        if not title and ph_kind in ("title", "ctrTitle"):
            title = " ".join(paragraphs)
        else:
            body.extend(paragraphs)
    if not title and body:
        title = body.pop(0)
    return {"index": index, "id": slide.get("id"), "layout": slide.get("layout"),
            "hidden": bool(slide.get("hidden")), "title": title, "body": body}


def _deck_slides(path: Path) -> List[Dict[str, Any]]:
    presentation = _load_deck(path)
    return [_slide_summary(i, s) for i, s in enumerate(presentation.get("slides") or [])]


def _content_from_op(op: Dict[str, Any], fallback: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Slide content from {title, bullets|body, layout}; missing keys keep the fallback's values."""
    title = op["title"] if "title" in op else (fallback["title"] if fallback else "")
    if "bullets" in op or "body" in op:
        body = _as_lines(op.get("bullets", op.get("body")))
    else:
        body = list(fallback["body"]) if fallback else []
    return {"title": str(title or "").strip(), "body": body, "layout": op.get("layout")}


def _slide_specs(slides: object) -> List[Dict[str, Any]]:
    if slides is None:
        return []
    if not isinstance(slides, list):
        raise ArtcraftError("slides must be a list.")
    specs = []
    for number, item in enumerate(slides, 1):
        if isinstance(item, str):
            item = {"title": item}
        if not isinstance(item, dict):
            raise ArtcraftError(f"Slide {number} must be an object with a title and bullets.")
        specs.append(_content_from_op(item))
    return specs


def _slide_params(title: str, body: List[str], layout: Optional[str], at: Optional[int] = None) -> Dict[str, Any]:
    params: Dict[str, Any] = {"layout": layout or ("titleAndContent" if body else "titleOnly"), "title": title}
    if body:
        params["body"] = "\n".join(body)
    if at is not None:
        params["at"] = at
    return params


def _content_params(content: Dict[str, Any], at: Optional[int] = None) -> Dict[str, Any]:
    return _slide_params(content["title"], content["body"], content.get("layout"), at)


def _deck_create_commands(title: str, specs: List[Dict[str, Any]]) -> List[Tuple[str, Dict[str, Any]]]:
    # A new DeckCraft presentation starts with one blank title slide at index 0; it is removed last.
    commands: List[Tuple[str, Dict[str, Any]]] = [("slide.new", _slide_params(title, [], "title"))]
    commands += [("slide.new", _content_params(content)) for content in specs]
    commands.append(("slide.delete", {"index": 0}))
    commands.append(("file.properties", {"title": title}))
    return commands


def _plan_deck_edit(slides: List[Dict[str, Any]], operations: object) -> Tuple[List[Tuple[str, Dict[str, Any]]], List[Dict[str, Any]]]:
    """Turn add/update/delete/move operations into engine commands, checking indices as it goes."""
    if not isinstance(operations, list) or not operations:
        raise ArtcraftError("operations must be a non-empty list.")
    model = [{"title": s["title"], "body": list(s["body"]), "layout": None} for s in slides]
    commands: List[Tuple[str, Dict[str, Any]]] = []
    for number, op in enumerate(operations, 1):
        if not isinstance(op, dict):
            raise ArtcraftError(f"Operation {number} must be an object.")
        kind = str(op.get("op", "")).strip().lower()
        if kind == "add":
            at = _index(op, "index", len(model) + 1) if "index" in op else len(model)
            content = _content_from_op(op)
            commands.append(("slide.new", _content_params(content, at=at)))
            model.insert(at, content)
        elif kind == "update":
            at = _index(op, "index", len(model))
            content = _content_from_op(op, fallback=model[at])
            commands.append(("slide.new", _content_params(content, at=at)))
            commands.append(("slide.delete", {"index": at + 1}))  # the old slide moved to at+1
            model[at] = content
        elif kind == "delete":
            at = _index(op, "index", len(model))
            commands.append(("slide.delete", {"index": at}))
            del model[at]
        elif kind == "move":
            source = _index(op, "from", len(model))
            target = _index(op, "to", len(model))
            commands.append(("slide.move", {"from": source, "to": target}))
            model.insert(target, model.pop(source))
        else:
            raise ArtcraftError(f"Operation {number} has unknown op '{kind}'. Use add, update, delete or move.")
    return commands, model


def deckcraft_read(path: object) -> Dict[str, Any]:
    src = _source(path, DECK_EXTS)
    slides = _deck_slides(src)
    return {"path": str(src), "slide_count": len(slides), "slides": slides,
            "message": f"{len(slides)} slide(s) in {src.name}."}


def deckcraft_create(output_path: object, title: object, slides: object = None, open_after: bool = True) -> Dict[str, Any]:
    dest = _new_target(output_path, DECK_EXTS)
    deck_title = str(title or "").strip() or dest.stem
    specs = _slide_specs(slides)
    commands = _deck_create_commands(deck_title, specs)
    with tempfile.TemporaryDirectory(prefix="altron-deck-") as tmp, _staged(dest) as stage:
        cmd_file = _write_lines(Path(tmp), [_cmd_line(name, params) for name, params in commands])
        _engine("deckcraft", ["run", "--cmd-file", str(cmd_file), "--save", str(stage)])
        count = len(_deck_slides(stage))
        if count != len(specs) + 1:
            raise ArtcraftError(f"DeckCraft wrote {count} slides, expected {len(specs) + 1}.")
    opened = _maybe_open(dest, open_after)
    return {"path": str(dest), "slide_count": count, "opened_in": opened,
            "message": f"Created {dest.name} with {count} slide(s)" + (f" and opened it in {opened}." if opened else ".")}


def deckcraft_edit(path: object, operations: object, open_after: bool = False) -> Dict[str, Any]:
    src = _source(path, DECK_EXTS)
    commands, expected = _plan_deck_edit(_deck_slides(src), operations)
    with tempfile.TemporaryDirectory(prefix="altron-deck-") as tmp, _staged(src) as stage:
        cmd_file = _write_lines(Path(tmp), [_cmd_line(name, params) for name, params in commands])
        _engine("deckcraft", ["run", "--in", str(src), "--cmd-file", str(cmd_file), "--save", str(stage)])
        result = _deck_slides(stage)
        if len(result) != len(expected):
            raise ArtcraftError(f"DeckCraft produced {len(result)} slides, expected {len(expected)}; {src.name} was left unchanged.")
    opened = _maybe_open(src, open_after)
    return {"path": str(src), "slide_count": len(result), "slides": result, "opened_in": opened,
            "message": f"Edited {src.name}: {len(operations)} operation(s), now {len(result)} slide(s)."}


# ---------------------------------------------------------------- WordCraft

def _heading_level(value: object) -> int:
    try:
        level = int(value)
    except (TypeError, ValueError) as err:
        raise ArtcraftError("Heading level must be 1, 2 or 3.") from err
    if level not in (1, 2, 3):
        raise ArtcraftError("Heading level must be 1, 2 or 3.")
    return level


def _section_specs(sections: object) -> List[Dict[str, Any]]:
    if sections is None:
        return []
    if not isinstance(sections, list):
        raise ArtcraftError("sections must be a list.")
    specs = []
    for item in sections:
        if isinstance(item, str):
            item = {"paragraphs": [item]}
        if not isinstance(item, dict):
            raise ArtcraftError("Each section must be an object with heading, paragraphs and bullets.")
        specs.append({
            "heading": str(item.get("heading") or "").strip(),
            "level": _heading_level(item.get("level", 1)),
            "paragraphs": _as_lines(item.get("paragraphs")),
            "bullets": _as_lines(item.get("bullets")),
        })
    return specs


def _word_para(commands: List[Tuple[str, Dict[str, Any]]], text: str, style: str = "Normal",
               bullet: bool = False, new: bool = True) -> None:
    if new:
        commands.append(("text.newParagraph", {}))
    commands.append(("para.style", {"style": style}))
    commands.append(("para.bullets", {"kind": "bullet"} if bullet else {"off": True}))
    if text:
        commands.append(("text.insert", {"text": text}))


def _word_create_commands(title: str, specs: List[Dict[str, Any]]) -> List[Tuple[str, Dict[str, Any]]]:
    commands: List[Tuple[str, Dict[str, Any]]] = [("file.properties", {"title": title})]
    _word_para(commands, title, style="Title", new=False)  # the blank document's first paragraph
    for section in specs:
        if section["heading"]:
            _word_para(commands, section["heading"], style=f"Heading {section['level']}")
        for text in section["paragraphs"]:
            _word_para(commands, text)
        for text in section["bullets"]:
            _word_para(commands, text, bullet=True)
    return commands


def _escape_word_find(text: str) -> str:
    """Find/Replace treats ^ as the start of a special code (^p, ^t, ...); ^^ is a literal caret."""
    return text.replace("^", "^^")


def _word_edit_commands(operations: object) -> List[Tuple[str, Dict[str, Any]]]:
    if not isinstance(operations, list) or not operations:
        raise ArtcraftError("operations must be a non-empty list.")
    commands: List[Tuple[str, Dict[str, Any]]] = [("caret.docEnd", {})]
    for number, op in enumerate(operations, 1):
        if not isinstance(op, dict):
            raise ArtcraftError(f"Operation {number} must be an object.")
        kind = str(op.get("op", "")).strip().lower()
        if kind == "append_heading":
            _word_para(commands, str(op.get("text") or "").strip(), style=f"Heading {_heading_level(op.get('level', 1))}")
        elif kind == "append_paragraph":
            _word_para(commands, str(op.get("text") or "").strip())
        elif kind == "append_bullets":
            items = _as_lines(op.get("items"))
            if not items:
                raise ArtcraftError(f"Operation {number} needs a non-empty items list.")
            for item in items:
                _word_para(commands, item, bullet=True)
        elif kind == "page_break":
            commands.append(("text.pageBreak", {}))
        elif kind == "replace":
            find = str(op.get("find") or "")
            if not find:
                raise ArtcraftError(f"Operation {number} needs 'find' text.")
            commands.append(("edit.replaceAll", {"text": _escape_word_find(find),
                                                 "with": _escape_word_find(str(op.get("replace") or ""))}))
        else:
            raise ArtcraftError(f"Operation {number} has unknown op '{kind}'. "
                                "Use append_heading, append_paragraph, append_bullets, page_break or replace.")
    return commands


def _run_word_script(commands: List[Tuple[str, Dict[str, Any]]], save_to: Path, source: Optional[Path] = None) -> None:
    script: Dict[str, Any] = {"commands": [{"command": name, "params": params} for name, params in commands],
                              "save": [str(save_to)]}
    if source is not None:
        script["file"] = str(source)
    with tempfile.TemporaryDirectory(prefix="altron-word-") as tmp:
        script_path = Path(tmp) / "script.json"
        script_path.write_text(json.dumps(script, ensure_ascii=False), encoding="utf-8")
        _engine("wordcraft", ["run", "--script", str(script_path)])


def _word_read_doc(path: Path) -> Dict[str, Any]:
    data = _parse_json(_engine("wordcraft", ["inspect", str(path)]), "WordCraft inspect")
    paragraphs = [{"style": block.get("style") or "", "text": block.get("text") or ""}
                  for block in data.get("blocks") or [] if block.get("type") == "paragraph"]
    props = data.get("properties") or {}
    return {"title": props.get("title") or "", "pages": data.get("pages"), "paragraphs": paragraphs,
            "text": "\n".join(p["text"] for p in paragraphs)}


def wordcraft_read(path: object) -> Dict[str, Any]:
    src = _source(path, WORD_EXTS)
    doc = _word_read_doc(src)
    return {"path": str(src), **doc, "message": f"{len(doc['paragraphs'])} paragraph(s) in {src.name}."}


def wordcraft_create(output_path: object, title: object, sections: object = None, open_after: bool = True) -> Dict[str, Any]:
    dest = _new_target(output_path, WORD_EXTS)
    doc_title = str(title or "").strip() or dest.stem
    commands = _word_create_commands(doc_title, _section_specs(sections))
    with _staged(dest) as stage:
        _run_word_script(commands, stage)
        doc = _word_read_doc(stage)
    opened = _maybe_open(dest, open_after)
    return {"path": str(dest), "title": doc["title"], "paragraph_count": len(doc["paragraphs"]), "opened_in": opened,
            "message": f"Created {dest.name} ({len(doc['paragraphs'])} paragraphs)" + (f" and opened it in {opened}." if opened else ".")}


def wordcraft_edit(path: object, operations: object, open_after: bool = False) -> Dict[str, Any]:
    src = _source(path, WORD_EXTS)
    commands = _word_edit_commands(operations)
    with _staged(src) as stage:
        _run_word_script(commands, stage, source=src)
        doc = _word_read_doc(stage)
    opened = _maybe_open(src, open_after)
    return {"path": str(src), "paragraph_count": len(doc["paragraphs"]), "opened_in": opened,
            "message": f"Edited {src.name}: {len(operations)} operation(s)."}


# ---------------------------------------------------------------- GridCraft

def _sheet_name(raw: object) -> str:
    name = str(raw or "").strip()
    if not name or len(name) > 31 or _SHEET_BAD_CHARS.search(name):
        raise ArtcraftError(f"'{name}' is not a valid sheet name (1-31 characters, none of []:*?/\\).")
    return name


def _grid_rows(rows: object) -> List[List[Any]]:
    if not isinstance(rows, list):
        raise ArtcraftError("rows must be a list of rows, each a list of cell values.")
    cleaned: List[List[Any]] = []
    for row in rows:
        if not isinstance(row, list):
            raise ArtcraftError("Each row must be a list of cell values.")
        for cell in row:
            if cell is not None and not isinstance(cell, (str, bool, int, float)):
                raise ArtcraftError("Cell values must be text, numbers, booleans or null.")
            if isinstance(cell, float) and not math.isfinite(cell):
                raise ArtcraftError("Cell numbers must be finite.")
        cleaned.append(list(row))
    width = max((len(r) for r in cleaned), default=0)
    return [r + [None] * (width - len(r)) for r in cleaned]


def _grid_sheet_specs(sheets: object) -> List[Dict[str, Any]]:
    if sheets is None:
        return [{"name": "Sheet1", "rows": []}]
    if not isinstance(sheets, list) or not sheets:
        raise ArtcraftError("sheets must be a non-empty list.")
    specs: List[Dict[str, Any]] = []
    seen = set()
    for item in sheets:
        if not isinstance(item, dict):
            raise ArtcraftError("Each sheet must be an object with name and rows.")
        name = _sheet_name(item.get("name") or f"Sheet{len(specs) + 1}")
        if name.lower() in seen:
            raise ArtcraftError(f"Sheet name '{name}' is used twice.")
        seen.add(name.lower())
        specs.append({"name": name, "rows": _grid_rows(item.get("rows") or [])})
    return specs


def _grid_create_lines(specs: List[Dict[str, Any]]) -> List[str]:
    lines: List[str] = []
    for index, sheet in enumerate(specs):
        if index == 0:
            lines.append(_cmd_line("sheet.rename", {"name": sheet["name"]}))  # the first sheet exists already
        else:
            # "before" is explicit: a plain insert lands after the active sheet, not at the end.
            lines.append(_cmd_line("home.insertSheet", {"name": sheet["name"], "before": index}))
        if sheet["rows"]:
            lines.append(_cmd_line("sheet.activate", {"sheet": sheet["name"]}))
            lines.append(_cmd_line("range.setValues", {"range": "A1", "values": sheet["rows"], "parse": False}))
    return lines


def _sheet_ref(raw: object, names: List[str]) -> str:
    if raw is None or str(raw).strip() == "":
        return names[0]
    wanted = str(raw).strip().lower()
    for name in names:
        if name.lower() == wanted:
            return name
    raise ArtcraftError(f"No sheet named '{raw}'. Sheets: {', '.join(names)}.")


def _plan_grid_edit(names: List[str], operations: object) -> Tuple[List[str], List[str]]:
    if not isinstance(operations, list) or not operations:
        raise ArtcraftError("operations must be a non-empty list.")
    names = list(names)
    lines: List[str] = []
    for number, op in enumerate(operations, 1):
        if not isinstance(op, dict):
            raise ArtcraftError(f"Operation {number} must be an object.")
        kind = str(op.get("op", "")).strip().lower()
        if kind == "set":
            sheet = _sheet_ref(op.get("sheet"), names)
            cell_range = str(op.get("range") or "A1").strip().upper()
            if not _RANGE_RE.match(cell_range):
                raise ArtcraftError(f"'{cell_range}' is not a cell or range like B2 or A1:C4.")
            rows = _grid_rows(op.get("values"))
            if not rows:
                raise ArtcraftError(f"Operation {number} needs at least one row of values.")
            lines.append(_cmd_line("sheet.activate", {"sheet": sheet}))
            lines.append(_cmd_line("range.setValues", {"range": cell_range, "values": rows, "parse": False}))
        elif kind == "add_sheet":
            name = _sheet_name(op.get("name"))
            if name.lower() in (n.lower() for n in names):
                raise ArtcraftError(f"A sheet named '{name}' already exists.")
            lines.append(_cmd_line("home.insertSheet", {"name": name, "before": len(names)}))
            names.append(name)
        elif kind == "rename_sheet":
            old = _sheet_ref(op.get("sheet"), names)
            new = _sheet_name(op.get("name"))
            lines.append(_cmd_line("sheet.rename", {"sheet": old, "name": new}))
            names[names.index(old)] = new
        elif kind == "delete_sheet":
            old = _sheet_ref(op.get("sheet"), names)
            if len(names) == 1:
                raise ArtcraftError("A workbook must keep at least one sheet.")
            lines.append(_cmd_line("home.deleteSheet", {"sheet": old}))
            names.remove(old)
        else:
            raise ArtcraftError(f"Operation {number} has unknown op '{kind}'. "
                                "Use set, add_sheet, rename_sheet or delete_sheet.")
    return lines, names


def _grid_sheet_names(path: Path) -> List[str]:
    data = _parse_json(_engine("gridcraft", ["info", str(path), "--json"]), "GridCraft info")
    return [str(sheet.get("name")) for sheet in data.get("sheets") or []]


def _read_csv_rows(text: str, max_rows: int) -> Tuple[List[List[str]], int]:
    rows = list(csv.reader(io.StringIO(text)))
    return rows[:max_rows], len(rows)


def gridcraft_read(path: object, sheet: object = None, max_rows: object = 200) -> Dict[str, Any]:
    src = _source(path, GRID_READ_EXTS)
    try:
        limit = max(1, min(int(max_rows or 200), 1000))
    except (TypeError, ValueError) as err:
        raise ArtcraftError("max_rows must be a whole number.") from err
    if src.suffix.lower() == ".csv":
        text = src.read_text(encoding="utf-8-sig", errors="replace")
        names = [src.stem]
        texts = {src.stem: text}
    else:
        names = _grid_sheet_names(src)
        texts = {}
    wanted = [_sheet_ref(sheet, names)] if sheet else names
    sheets_out = []
    for name in wanted:
        text = texts.get(name)
        if text is None:
            text = _engine("gridcraft", ["cat", str(src), "--sheet", name, "--csv"])
        rows, total = _read_csv_rows(text, limit)
        sheets_out.append({"name": name, "row_count": total, "rows": rows, "truncated": total > limit})
    return {"path": str(src), "sheets": sheets_out,
            "message": f"{len(sheets_out)} sheet(s) in {src.name}."}


def gridcraft_create(output_path: object, sheets: object = None, open_after: bool = True) -> Dict[str, Any]:
    dest = _new_target(output_path, GRID_EXTS)
    specs = _grid_sheet_specs(sheets)
    lines = _grid_create_lines(specs)
    with tempfile.TemporaryDirectory(prefix="altron-grid-") as tmp, _staged(dest) as stage:
        script = _write_lines(Path(tmp), lines)
        _engine("gridcraft", ["run", "--script", str(script), "--out", str(stage)])
        names = _grid_sheet_names(stage)
        if names != [s["name"] for s in specs]:
            raise ArtcraftError(f"GridCraft wrote sheets {names}, expected {[s['name'] for s in specs]}.")
    opened = _maybe_open(dest, open_after)
    return {"path": str(dest), "sheets": names, "opened_in": opened,
            "message": f"Created {dest.name} with {len(names)} sheet(s)" + (f" and opened it in {opened}." if opened else ".")}


def gridcraft_edit(path: object, operations: object, open_after: bool = False) -> Dict[str, Any]:
    src = _source(path, GRID_EXTS)
    lines, expected = _plan_grid_edit(_grid_sheet_names(src), operations)
    with tempfile.TemporaryDirectory(prefix="altron-grid-") as tmp, _staged(src) as stage:
        script = _write_lines(Path(tmp), lines)
        _engine("gridcraft", ["run", "--in", str(src), "--script", str(script), "--out", str(stage)])
        names = _grid_sheet_names(stage)
        if names != expected:
            raise ArtcraftError(f"GridCraft produced sheets {names}, expected {expected}; {src.name} was left unchanged.")
    opened = _maybe_open(src, open_after)
    return {"path": str(src), "sheets": names, "opened_in": opened,
            "message": f"Edited {src.name}: {len(operations)} operation(s), sheets: {', '.join(names)}."}


# ---------------------------------------------------------------- PdfCraft

def _pdf_info(path: Path) -> Dict[str, Any]:
    return _parse_json(_engine("pdfcraft", ["info", str(path)]), "PdfCraft info")


def _page_numbers(spec: object, count: int = 0) -> List[int]:
    """Parse pages such as "3", "1-3,5" or ["1-3", "5"] into 1-based page numbers, in order."""
    tokens = [str(item) for item in spec] if isinstance(spec, (list, tuple)) else str(spec or "").split(",")
    numbers: List[int] = []
    for token in tokens:
        for part in token.split(","):
            if not part.strip():
                continue
            match = _PAGE_TOKEN_RE.match(part)
            if not match:
                raise ArtcraftError(f"'{part.strip()}' is not a page or page range such as 3 or 2-5.")
            start = int(match.group(1))
            end = int(match.group(2) or start)
            if start < 1 or end < start:
                raise ArtcraftError(f"'{part.strip()}' is not a valid page range (pages start at 1).")
            if count and end > count:
                raise ArtcraftError(f"Page {end} does not exist; the PDF has {count} page(s).")
            if end > 100000:
                raise ArtcraftError("That page range is too large.")
            numbers.extend(n for n in range(start, end + 1) if n not in numbers)
    if not numbers:
        raise ArtcraftError("No pages were given.")
    return numbers


def _range_label(numbers: List[int]) -> str:
    runs: List[str] = []
    start = prev = numbers[0]
    for n in numbers[1:] + [None]:
        if n is not None and n == prev + 1:
            prev = n
            continue
        runs.append(str(start) if start == prev else f"{start}-{prev}")
        if n is not None:
            start = prev = n
    return "_".join(runs)


def _unique_path(path: Path) -> Path:
    if not path.exists():
        return path
    for number in range(2, 1000):
        candidate = path.with_name(f"{path.stem} ({number}){path.suffix}")
        if not candidate.exists():
            return candidate
    raise ArtcraftError(f"Too many files named like {path.name} already exist.")


def pdfcraft_read(path: object, pages: object = None) -> Dict[str, Any]:
    src = _source(path, PDF_EXTS)
    info = _pdf_info(src)
    count = int(info.get("pages") or 0)
    text = _engine("pdfcraft", ["text", str(src)])
    chunks = text.split("\f")
    if count and len(chunks) > count and not chunks[-1].strip():
        chunks = chunks[:count]
    count = count or len(chunks)
    wanted = _page_numbers(pages, count) if pages else list(range(1, count + 1))
    out_pages = [{"page": n, "text": chunks[n - 1].strip() if n - 1 < len(chunks) else ""} for n in wanted]
    return {"path": str(src), "page_count": count, "title": info.get("title") or "",
            "author": info.get("author") or "", "pages": out_pages,
            "message": f"{count} page(s) in {src.name}."}


def pdfcraft_merge(input_paths: object, output_path: object, open_after: bool = True) -> Dict[str, Any]:
    if not isinstance(input_paths, list) or len(input_paths) < 2:
        raise ArtcraftError("Give at least two PDF files to merge.")
    sources = [_source(p, PDF_EXTS) for p in input_paths]
    expected = sum(int(_pdf_info(s).get("pages") or 0) for s in sources)
    dest = _new_target(output_path, PDF_EXTS)
    with _staged(dest) as stage:
        _engine("pdfcraft", ["combine", *(str(s) for s in sources), "--out", str(stage)])
        got = int(_pdf_info(stage).get("pages") or 0)
        if got != expected:
            raise ArtcraftError(f"Merged PDF has {got} page(s), expected {expected}.")
    opened = _maybe_open(dest, open_after)
    return {"path": str(dest), "page_count": got, "opened_in": opened,
            "message": f"Merged {len(sources)} PDFs into {dest.name} ({got} pages)" + (f" and opened it in {opened}." if opened else ".")}


def pdfcraft_split(input_path: object, page_ranges: object, output_dir: object = None) -> Dict[str, Any]:
    src = _source(input_path, PDF_EXTS)
    count = int(_pdf_info(src).get("pages") or 0)
    if isinstance(page_ranges, str):
        page_ranges = [page_ranges]
    if not isinstance(page_ranges, list) or not page_ranges:
        raise ArtcraftError("Give at least one page range, such as ['1-3', '5'].")
    target_dir = _user_path(output_dir) if output_dir else src.parent / f"{src.stem}_split"
    target_dir.mkdir(parents=True, exist_ok=True)
    outputs = []
    for spec in page_ranges:
        numbers = _page_numbers(spec, count)
        dest = _unique_path(target_dir / f"{src.stem}_pages_{_range_label(numbers)}.pdf")
        with _staged(dest) as stage:
            _engine("pdfcraft", ["extract", str(src), "--pages", ",".join(map(str, numbers)), "--out", str(stage)])
            got = int(_pdf_info(stage).get("pages") or 0)
            if got != len(numbers):
                raise ArtcraftError(f"Extracted PDF has {got} page(s), expected {len(numbers)}.")
        outputs.append({"path": str(dest), "pages": numbers})
    return {"source": str(src), "outputs": outputs,
            "message": f"Split {src.name} into {len(outputs)} file(s) in {target_dir}."}
