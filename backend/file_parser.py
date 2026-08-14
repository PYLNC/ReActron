"""上传文件解析：支持 Excel、PPT、CSV、文本和 JSON。"""

import csv
import json
from pathlib import Path

from openpyxl import load_workbook
from pptx import Presentation


def _parse_excel(path: Path) -> dict:
    workbook = load_workbook(path, data_only=True, read_only=True)
    try:
        sheet = workbook[workbook.sheetnames[0]]
        rows = []
        for row in sheet.iter_rows(max_row=20, values_only=True):
            rows.append([str(cell) if cell is not None else "" for cell in row])
        return {
            "type": "excel",
            "sheets": workbook.sheetnames,
            "sheet": sheet.title,
            "preview_rows": rows,
        }
    finally:
        workbook.close()


def _parse_pptx(path: Path) -> dict:
    presentation = Presentation(path)
    slides = []
    for index, slide in enumerate(presentation.slides):
        texts = []
        for shape in slide.shapes:
            if not shape.has_text_frame:
                continue
            for paragraph in shape.text_frame.paragraphs:
                text = "".join(run.text for run in paragraph.runs) or paragraph.text
                if text.strip():
                    texts.append(text.strip())
        slides.append({"index": index + 1, "texts": texts[:10]})
    return {"type": "pptx", "slide_count": len(slides), "slides": slides[:10]}


def _parse_csv(path: Path) -> dict:
    with path.open("r", encoding="utf-8-sig", errors="replace") as handle:
        reader = csv.reader(handle)
        rows = [row for _, row in zip(range(20), reader)]
    return {"type": "csv", "preview_rows": rows}


def _parse_text(path: Path) -> dict:
    text = path.read_text(encoding="utf-8", errors="replace")
    return {
        "type": "text",
        "preview": text[:2000],
        "char_count": len(text),
    }


def parse_uploaded_file(path: Path) -> dict:
    """根据扩展名解析文件并返回摘要。"""
    suffix = path.suffix.lower()
    if suffix == ".xlsx":
        return _parse_excel(path)
    if suffix == ".pptx":
        return _parse_pptx(path)
    if suffix == ".csv":
        return _parse_csv(path)
    if suffix in (".txt", ".md", ".json", ".log"):
        return _parse_text(path)
    return {"type": "unsupported", "preview": ""}


def to_json_safe(data) -> str:
    return json.dumps(data, ensure_ascii=False, default=str)
