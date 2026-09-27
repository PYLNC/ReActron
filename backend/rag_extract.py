"""Bounded full-text extraction for local RAG documents."""

import csv
import json
import multiprocessing
import re
import unicodedata
import xml.etree.ElementTree as ET
from pathlib import Path
from zipfile import ZipFile

from openpyxl import load_workbook
from pptx import Presentation

from backend.file_parser import (
    DocumentExtractionError,
    DocumentExtractionTimeout,
    DocumentLimitExceeded,
    DocumentNoText,
    DocumentSegment,
    UnsupportedDocumentType,
    _check_zip_limits,
)


MAX_DOCUMENT_BYTES = 20 * 1024 * 1024
MAX_TEXT_CHARS = 2_000_000
MAX_SEGMENTS = 50_000
MAX_JSON_DEPTH = 32
MAX_JSON_NODES = 200_000
MAX_CSV_ROWS = 100_000
MAX_SHEETS = 20
MAX_SHEET_ROWS = 20_000
MAX_COLUMNS = 256
MAX_SLIDES = 2_000
MAX_PDF_PAGES = 2_000
SEGMENT_TARGET_CHARS = 1_200

_W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
_DC_TITLE = "{http://purl.org/dc/elements/1.1/}title"
_CONTENT_TYPES = "{http://schemas.openxmlformats.org/package/2006/content-types}"
_HEADING = re.compile(r"^\s{0,3}#{1,6}\s+(.+?)\s*#*\s*$")


def normalize_text(value: str) -> str:
    """Normalize text without flattening structural newlines."""
    value = unicodedata.normalize("NFKC", value).replace("\r\n", "\n").replace("\r", "\n")
    value = "".join(
        char for char in value
        if char in "\n\t" or not unicodedata.category(char).startswith("C")
    )
    lines = [re.sub(r"[^\S\n]+", " ", line).strip() for line in value.split("\n")]
    return re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()


class _Collector:
    def __init__(self):
        self.segments: list[DocumentSegment] = []
        self.characters = 0

    def add(self, text, section, locator):
        text = normalize_text(str(text))
        if not text:
            return
        if self.characters + len(text) > MAX_TEXT_CHARS or len(self.segments) >= MAX_SEGMENTS:
            raise DocumentLimitExceeded("document_limit_exceeded")
        self.characters += len(text)
        self.segments.append(DocumentSegment(text, normalize_text(str(section)), dict(locator)))


class _TableSegments:
    def __init__(self, collector, section, locator):
        self.collector = collector
        self.section = section
        self.locator = locator
        self.header = None
        self.rows = []
        self.row_chars = 0

    @staticmethod
    def _clean(row):
        if len(row) > MAX_COLUMNS:
            raise DocumentLimitExceeded("document_limit_exceeded")
        return [normalize_text("" if cell is None else str(cell)) for cell in row]

    @staticmethod
    def _render(rows):
        # Tabs keep tables readable while csv.writer has already handled input quoting.
        return "\n".join("\t".join(row) for row in rows)

    def add(self, number, row):
        row = self._clean(row)
        if self.header is None:
            self.header = (number, row)
            return
        size = sum(len(cell) + 1 for cell in row)
        if self.rows and self.row_chars + size > SEGMENT_TARGET_CHARS:
            self.flush()
        self.rows.append((number, row))
        self.row_chars += size

    def flush(self):
        if self.header is None:
            return
        if self.rows:
            locator = {
                **self.locator,
                "header_row": self.header[0],
                "row_start": self.rows[0][0],
                "row_end": self.rows[-1][0],
            }
            rows = [self.header[1], *(row for _, row in self.rows)]
        else:
            locator = {
                **self.locator,
                "row_start": self.header[0],
                "row_end": self.header[0],
            }
            rows = [self.header[1]]
        self.collector.add(self._render(rows), self.section, locator)
        self.rows = []
        self.row_chars = 0


def _extract_text(path, collector):
    try:
        text = path.read_text(encoding="utf-8-sig")
    except UnicodeDecodeError as exc:
        raise DocumentExtractionError("document_parse_failed") from exc
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    section = ""
    block = []
    start = 1

    def flush(end):
        nonlocal block
        collector.add("\n".join(block), section, {"line_start": start, "line_end": end})
        block = []

    for number, line in enumerate(text.split("\n"), 1):
        heading = _HEADING.match(line)
        if heading:
            if block:
                flush(number - 1)
            section = normalize_text(heading.group(1))
            start = number
            block = [line]
        elif not line.strip():
            if block:
                flush(number - 1)
        else:
            if not block:
                start = number
            block.append(line)
    if block:
        flush(len(text.split("\n")))


def _check_json_shape(value):
    count = 0
    stack = [(value, 1)]
    while stack:
        item, depth = stack.pop()
        count += 1
        if count > MAX_JSON_NODES or depth > MAX_JSON_DEPTH:
            raise DocumentLimitExceeded("document_limit_exceeded")
        if isinstance(item, dict):
            stack.extend((child, depth + 1) for child in item.values())
        elif isinstance(item, list):
            stack.extend((child, depth + 1) for child in item)


def _json_dump(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _extract_json(path, collector):
    try:
        value = json.loads(path.read_text(encoding="utf-8-sig"))
    except (UnicodeDecodeError, json.JSONDecodeError, RecursionError) as exc:
        raise DocumentExtractionError("document_parse_failed") from exc
    _check_json_shape(value)
    if isinstance(value, dict) and value:
        for key in sorted(value, key=str):
            path_key = json.dumps(str(key), ensure_ascii=False)
            collector.add(f"{key}: {_json_dump(value[key])}", str(key), {"json_path": f"$[{path_key}]"})
    elif isinstance(value, list) and value:
        for index, item in enumerate(value):
            collector.add(_json_dump(item), f"[{index}]", {"json_path": f"$[{index}]"})
    else:
        collector.add(_json_dump(value), "", {"json_path": "$"})


def _extract_csv(path, collector):
    previous_limit = csv.field_size_limit()
    csv.field_size_limit(MAX_TEXT_CHARS)
    try:
        with path.open("r", encoding="utf-8-sig", newline="") as handle:
            table = _TableSegments(collector, "", {})
            try:
                for number, row in enumerate(csv.reader(handle), 1):
                    if number > MAX_CSV_ROWS:
                        raise DocumentLimitExceeded("document_limit_exceeded")
                    table.add(number, row)
            except (csv.Error, UnicodeDecodeError) as exc:
                raise DocumentExtractionError("document_parse_failed") from exc
            table.flush()
    finally:
        csv.field_size_limit(previous_limit)


def _extract_xlsx(path, collector):
    _validate_ooxml(
        path,
        "xl/workbook.xml",
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml",
    )
    workbook = load_workbook(path, data_only=True, read_only=True)
    try:
        if len(workbook.worksheets) > MAX_SHEETS:
            raise DocumentLimitExceeded("document_limit_exceeded")
        for sheet in workbook.worksheets:
            if (sheet.max_row or 0) > MAX_SHEET_ROWS or (sheet.max_column or 0) > MAX_COLUMNS:
                raise DocumentLimitExceeded("document_limit_exceeded")
            table = _TableSegments(collector, sheet.title, {"sheet": sheet.title})
            for number, row in enumerate(sheet.iter_rows(values_only=True), 1):
                if number > MAX_SHEET_ROWS:
                    raise DocumentLimitExceeded("document_limit_exceeded")
                table.add(number, row)
            table.flush()
    finally:
        workbook.close()


def _extract_pptx(path, collector):
    _validate_ooxml(
        path,
        "ppt/presentation.xml",
        "application/vnd.openxmlformats-officedocument.presentationml.presentation.main+xml",
    )
    presentation = Presentation(path)
    if len(presentation.slides) > MAX_SLIDES:
        raise DocumentLimitExceeded("document_limit_exceeded")
    for number, slide in enumerate(presentation.slides, 1):
        parts = []
        for shape in slide.shapes:
            if getattr(shape, "has_text_frame", False):
                for paragraph in shape.text_frame.paragraphs:
                    text = "".join(run.text for run in paragraph.runs) or paragraph.text
                    if text.strip():
                        parts.append(text)
            if getattr(shape, "has_table", False):
                for row in shape.table.rows:
                    parts.append("\t".join(cell.text for cell in row.cells))
        title_shape = slide.shapes.title
        title = title_shape.text if title_shape is not None else f"Slide {number}"
        collector.add("\n".join(parts), title, {"slide": number})


def _xml_root(archive, name):
    try:
        data = archive.read(name)
    except KeyError as exc:
        raise DocumentExtractionError("document_parse_failed") from exc
    upper = data.upper()
    if b"<!DOCTYPE" in upper or b"<!ENTITY" in upper:
        raise DocumentExtractionError("document_parse_failed")
    try:
        return ET.fromstring(data)
    except ET.ParseError as exc:
        raise DocumentExtractionError("document_parse_failed") from exc


def _validate_ooxml(path, main_part, content_type):
    """Require the declared non-macro OOXML family before invoking its parser."""
    _check_zip_limits(path, required=("[Content_Types].xml", main_part))
    with ZipFile(path) as archive:
        root = _xml_root(archive, "[Content_Types].xml")
        if root.tag != f"{_CONTENT_TYPES}Types":
            raise DocumentExtractionError("document_parse_failed")
        expected_part = f"/{main_part}"
        if not any(
            node.get("PartName") == expected_part and node.get("ContentType") == content_type
            for node in root.findall(f"{_CONTENT_TYPES}Override")
        ):
            raise UnsupportedDocumentType("unsupported_document_type")


def _word_text(element):
    parts = []
    for node in element.iter():
        if node.tag == f"{_W}t":
            parts.append(node.text or "")
        elif node.tag == f"{_W}tab":
            parts.append("\t")
        elif node.tag in {f"{_W}br", f"{_W}cr"}:
            parts.append("\n")
    return normalize_text("".join(parts))


def _paragraph_style(paragraph):
    style = paragraph.find(f"{_W}pPr/{_W}pStyle")
    return style.get(f"{_W}val", "") if style is not None else ""


def _extract_docx_part(root, part, collector):
    container = root.find(f"{_W}body") if root.tag == f"{_W}document" else root
    if container is None:
        raise DocumentExtractionError("document_parse_failed")
    section = ""
    paragraphs = []
    paragraph_number = 0
    table_number = 0
    paragraph_chars = 0

    def flush_paragraphs():
        nonlocal paragraphs, paragraph_chars
        if paragraphs:
            collector.add(
                "\n\n".join(text for _, text in paragraphs),
                section,
                {
                    "part": part,
                    "paragraph_start": paragraphs[0][0],
                    "paragraph_end": paragraphs[-1][0],
                },
            )
        paragraphs = []
        paragraph_chars = 0

    for child in container:
        if child.tag == f"{_W}p":
            paragraph_number += 1
            text = _word_text(child)
            if not text:
                continue
            style = _paragraph_style(child).lower()
            if style == "title":
                flush_paragraphs()
                section = text
                collector.add(text, section, {"part": part, "kind": "title", "paragraph": paragraph_number})
                continue
            if style.startswith("heading"):
                flush_paragraphs()
                section = text
            if paragraphs and paragraph_chars + len(text) > SEGMENT_TARGET_CHARS:
                flush_paragraphs()
            paragraphs.append((paragraph_number, text))
            paragraph_chars += len(text)
        elif child.tag == f"{_W}tbl":
            flush_paragraphs()
            table_number += 1
            table = _TableSegments(
                collector,
                section,
                {"part": part, "table": table_number},
            )
            for row_number, row in enumerate(child.findall(f"{_W}tr"), 1):
                table.add(row_number, [_word_text(cell) for cell in row.findall(f"{_W}tc")])
            table.flush()
    flush_paragraphs()


def _extract_docx(path, collector):
    _validate_ooxml(
        path,
        "word/document.xml",
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml",
    )
    with ZipFile(path) as archive:
        names = set(archive.namelist())
        if "docProps/core.xml" in names:
            core = _xml_root(archive, "docProps/core.xml")
            title = core.find(_DC_TITLE)
            if title is not None and title.text:
                collector.add(title.text, title.text, {"part": "core", "kind": "title"})
        _extract_docx_part(_xml_root(archive, "word/document.xml"), "document", collector)
        for prefix, label in (("word/header", "header"), ("word/footer", "footer")):
            for name in sorted(item for item in names if item.startswith(prefix) and item.endswith(".xml")):
                stem = Path(name).stem
                _extract_docx_part(_xml_root(archive, name), stem or label, collector)
        if "word/footnotes.xml" in names:
            root = _xml_root(archive, "word/footnotes.xml")
            for footnote in root.findall(f"{_W}footnote"):
                identifier = footnote.get(f"{_W}id", "")
                if identifier.startswith("-"):
                    continue
                collector.add(
                    _word_text(footnote),
                    "Footnote",
                    {"part": "footnote", "footnote_id": identifier},
                )


def _extract_pdf(path, collector):
    from pypdf import PdfReader

    try:
        with path.open("rb") as handle:
            if handle.read(5) != b"%PDF-":
                raise DocumentExtractionError("document_parse_failed")
    except OSError as exc:
        raise DocumentExtractionError("document_parse_failed") from exc
    reader = PdfReader(str(path), strict=False)
    if reader.is_encrypted and not reader.decrypt(""):
        raise UnsupportedDocumentType("document_encrypted")
    if len(reader.pages) > MAX_PDF_PAGES:
        raise DocumentLimitExceeded("document_limit_exceeded")
    for number, page in enumerate(reader.pages, 1):
        collector.add(page.extract_text() or "", f"Page {number}", {"page": number})
    if not collector.segments:
        raise DocumentNoText("document_no_text")


def _extract_document_inner(path_string):
    path = Path(path_string)
    try:
        size = path.stat().st_size
    except OSError as exc:
        raise DocumentExtractionError("document_parse_failed") from exc
    if not path.is_file():
        raise DocumentExtractionError("document_parse_failed")
    if size > MAX_DOCUMENT_BYTES:
        raise DocumentLimitExceeded("document_limit_exceeded")

    collector = _Collector()
    suffix = path.suffix.lower()
    extractors = {
        ".txt": _extract_text,
        ".md": _extract_text,
        ".log": _extract_text,
        ".json": _extract_json,
        ".csv": _extract_csv,
        ".xlsx": _extract_xlsx,
        ".pptx": _extract_pptx,
        ".docx": _extract_docx,
        ".pdf": _extract_pdf,
    }
    extractor = extractors.get(suffix)
    if extractor is None:
        raise UnsupportedDocumentType("unsupported_document_type")
    extractor(path, collector)
    if not collector.segments:
        raise DocumentNoText("document_no_text")
    return collector.segments


def _extract_worker(path, sender):
    try:
        sender.send(("ok", _extract_document_inner(path)))
    except DocumentExtractionError as exc:
        sender.send(("error", exc.code))
    except Exception:
        sender.send(("error", "document_parse_failed"))
    finally:
        sender.close()


def _stop_process(process):
    process.terminate()
    process.join(1)
    if process.is_alive():
        process.kill()
        process.join(1)


def extract_document(path: Path, *, timeout_seconds: float = 20.0) -> list[DocumentSegment]:
    """Extract a document with hard wall-clock and serialized-output bounds."""
    if not isinstance(timeout_seconds, (int, float)) or not 0.1 <= timeout_seconds <= 120:
        raise ValueError("timeout_seconds must be 0.1..120")
    receiver, sender = multiprocessing.get_context("spawn").Pipe(duplex=False)
    process = multiprocessing.get_context("spawn").Process(
        target=_extract_worker,
        args=(str(Path(path)), sender),
        daemon=True,
    )
    try:
        process.start()
        sender.close()
        if not receiver.poll(timeout_seconds):
            _stop_process(process)
            raise DocumentExtractionTimeout("document_extraction_timeout")
        try:
            status, value = receiver.recv()
        except EOFError as exc:
            raise DocumentExtractionError("document_parse_failed") from exc
    finally:
        sender.close()
        receiver.close()
    process.join(1)
    if process.is_alive():
        _stop_process(process)
    if status == "ok":
        return value
    error_types = {
        "document_limit_exceeded": DocumentLimitExceeded,
        "unsupported_document_type": UnsupportedDocumentType,
        "document_no_text": DocumentNoText,
        "document_extraction_timeout": DocumentExtractionTimeout,
    }
    raise error_types.get(value, DocumentExtractionError)(value)
