"""Small end-to-end checks for RAG extraction, ranking, and indexing."""

import asyncio
import hashlib
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from zipfile import ZIP_DEFLATED, ZipFile

from openpyxl import Workbook
from pptx import Presentation
from pypdf import PdfWriter
from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject

from backend.file_parser import (
    DocumentExtractionError,
    DocumentExtractionTimeout,
    DocumentLimitExceeded,
    DocumentSegment,
    UnsupportedDocumentType,
    extract_document,
)
from backend.rag import (
    RAGIndexer,
    _verify_source,
    chunk_segments,
)
from backend.rag_extract import _extract_document_inner


def _write_docx(path):
    content_types = b'''<?xml version="1.0" encoding="UTF-8"?>
<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
  <Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>
</Types>'''
    document = b'''<?xml version="1.0" encoding="UTF-8"?>
<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">
  <w:body><w:p><w:r><w:t>Hello DOCX</w:t></w:r></w:p></w:body>
</w:document>'''
    with ZipFile(path, "w", ZIP_DEFLATED) as archive:
        archive.writestr("[Content_Types].xml", content_types)
        archive.writestr("word/document.xml", document)


def _write_pdf(path):
    writer = PdfWriter()
    page = writer.add_blank_page(width=612, height=792)
    font = DictionaryObject({
        NameObject("/Type"): NameObject("/Font"),
        NameObject("/Subtype"): NameObject("/Type1"),
        NameObject("/BaseFont"): NameObject("/Helvetica"),
    })
    font_ref = writer._add_object(font)
    page[NameObject("/Resources")] = DictionaryObject({
        NameObject("/Font"): DictionaryObject({NameObject("/F1"): font_ref}),
    })
    stream = DecodedStreamObject()
    stream.set_data(b"BT /F1 12 Tf 72 720 Td (Hello PDF) Tj ET")
    page[NameObject("/Contents")] = writer._add_object(stream)
    with path.open("wb") as handle:
        writer.write(handle)


class ExtractionTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)

    def tearDown(self):
        self.temp.cleanup()

    def test_supported_formats_and_locators(self):
        for suffix in (".txt", ".md", ".log"):
            (self.root / f"plain{suffix}").write_text("# Heading\n\nHello text", encoding="utf-8")
        (self.root / "data.json").write_text('{"name":"Hello JSON"}', encoding="utf-8")
        (self.root / "data.csv").write_text("name,value\nhello,CSV", encoding="utf-8")

        workbook = Workbook()
        workbook.active.append(["name", "value"])
        workbook.active.append(["hello", "XLSX"])
        workbook.save(self.root / "data.xlsx")

        presentation = Presentation()
        slide = presentation.slides.add_slide(presentation.slide_layouts[1])
        slide.shapes.title.text = "PPTX title"
        slide.placeholders[1].text = "Hello PPTX"
        presentation.save(self.root / "data.pptx")

        _write_docx(self.root / "data.docx")
        _write_pdf(self.root / "data.pdf")

        expected = {
            ".txt": "Hello text", ".md": "Hello text", ".log": "Hello text",
            ".json": "Hello JSON", ".csv": "CSV", ".xlsx": "XLSX",
            ".pptx": "Hello PPTX", ".docx": "Hello DOCX", ".pdf": "Hello PDF",
        }
        for path in sorted(self.root.iterdir()):
            segments = _extract_document_inner(str(path))
            self.assertIn(expected[path.suffix], "\n".join(item.text for item in segments))
            self.assertTrue(all(isinstance(item.locator, dict) for item in segments))

        public = extract_document(self.root / "plain.txt", timeout_seconds=5)
        self.assertIn("Hello text", "\n".join(item.text for item in public))

    def test_limits_ooxml_family_and_timeout_termination(self):
        path = self.root / "large.txt"
        path.write_text("too much text", encoding="utf-8")
        with patch("backend.rag_extract.MAX_TEXT_CHARS", 3):
            with self.assertRaises(DocumentLimitExceeded):
                _extract_document_inner(str(path))

        invalid = self.root / "wrong.docx"
        with ZipFile(invalid, "w") as archive:
            archive.writestr(
                "[Content_Types].xml",
                '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
                '<Override PartName="/word/document.xml" ContentType="text/plain"/></Types>',
            )
            archive.writestr("word/document.xml", "<x/>")
        with self.assertRaises(UnsupportedDocumentType):
            _extract_document_inner(str(invalid))

        class Receiver:
            def poll(self, timeout):
                return False

            def close(self):
                pass

        class Sender:
            def close(self):
                pass

        class Process:
            terminated = False

            def start(self):
                pass

            def terminate(self):
                self.terminated = True

            def join(self, timeout):
                pass

            def is_alive(self):
                return not self.terminated

            def kill(self):
                self.terminated = True

        process = Process()

        class Context:
            def Pipe(self, duplex):
                return Receiver(), Sender()

            def Process(self, **kwargs):
                return process

        with patch("backend.rag_extract.multiprocessing.get_context", return_value=Context()):
            with self.assertRaises(DocumentExtractionTimeout):
                extract_document(path, timeout_seconds=0.1)
        self.assertTrue(process.terminated)


class ChunkingTest(unittest.TestCase):
    def test_chunks_are_deterministic(self):
        segment = DocumentSegment(("alpha beta gamma\n" * 180), "Section", {"line_start": 2})
        first = chunk_segments("doc", 1, "guide.md", [segment])
        second = chunk_segments("doc", 1, "guide.md", [segment])
        self.assertEqual(first, second)
        self.assertGreater(len(first), 1)
        self.assertTrue(all(1 <= len(item["content"]) <= 1600 for item in first))
        self.assertTrue(all(item["locator"]["line_start"] == 2 for item in first))


class IndexerLifecycleTest(unittest.TestCase):
    def test_indexer_restarts_on_a_new_event_loop(self):
        class Database:
            pending = False
            completed = None

            def recover_expired_rag_leases(self):
                return []

            def list_deleting_rag_documents(self, limit):
                return []

            def claim_next_rag_document(self, owner, lease_seconds):
                if not self.pending:
                    return None
                self.pending = False
                return {"id": "second-loop"}

        class Indexer(RAGIndexer):
            async def _index(self, document):
                self.database.completed = document["id"]

        database = Database()
        indexer = Indexer(database)

        async def cycle(process_document=False):
            await indexer.start()
            event = indexer._event
            try:
                for _ in range(100):
                    if event._loop is asyncio.get_running_loop():
                        break
                    await asyncio.sleep(0.01)
                self.assertIs(event._loop, asyncio.get_running_loop())
                if process_document:
                    database.pending = True
                    indexer.wake()
                    for _ in range(100):
                        if database.completed:
                            break
                        await asyncio.sleep(0.01)
                    self.assertEqual(database.completed, "second-loop")
            finally:
                await indexer.close()
            return event

        with patch("backend.rag.INDEX_POLL_SECONDS", 60):
            first_event = asyncio.run(cycle())
            second_event = asyncio.run(cycle(process_document=True))
        self.assertIsNot(first_event, second_event)


class IndexerTest(unittest.IsolatedAsyncioTestCase):
    async def test_indexer_recovers_verifies_and_completes(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "source.txt"
            path.write_text("Indexer text", encoding="utf-8")
            payload = path.read_bytes()

            class Database:
                def __init__(self):
                    self.document = {
                        "id": "doc", "title": "source.txt", "stored_path": str(path),
                        "size": len(payload), "content_sha256": hashlib.sha256(payload).hexdigest(),
                        "parser_version": 1,
                    }
                    self.claimed = False
                    self.completed = None

                def recover_expired_rag_leases(self):
                    return []

                def list_deleting_rag_documents(self, limit):
                    return []

                def claim_next_rag_document(self, owner, lease_seconds):
                    if self.claimed:
                        return None
                    self.claimed = True
                    return self.document

                def renew_rag_lease(self, document_id, owner, lease_seconds):
                    return True

                def complete_rag_document(self, document_id, owner, chunks):
                    self.completed = chunks

                def fail_rag_document(self, document_id, owner, code):
                    self.fail_code = code

            database = Database()
            indexer = RAGIndexer(database)
            with patch("backend.rag.KNOWLEDGE_DIR", Path(temp)):
                await indexer.start()
                for _ in range(100):
                    if database.completed is not None:
                        break
                    await asyncio.sleep(0.02)
                await indexer.close()
            self.assertIsNotNone(database.completed)
            self.assertEqual(database.completed[0]["content"], "Indexer text")

    async def test_indexer_retries_startup_recovery_and_rejects_outside_delete(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "knowledge"
            root.mkdir()
            outside = Path(temp) / "outside.txt"
            outside.write_text("keep", encoding="utf-8")

            class Database:
                recoveries = 0
                finalized = []

                def recover_expired_rag_leases(self):
                    self.recoveries += 1
                    if self.recoveries == 1:
                        raise RuntimeError("temporary")
                    return []

                def list_deleting_rag_documents(self, limit):
                    return [{"id": "outside", "stored_path": str(outside)}]

                def finalize_rag_document_delete(self, document_id):
                    self.finalized.append(document_id)

                def claim_next_rag_document(self, owner, lease_seconds):
                    return None

            database = Database()
            indexer = RAGIndexer(database)
            with patch("backend.rag.KNOWLEDGE_DIR", root), patch("backend.rag.INDEX_POLL_SECONDS", 0.01):
                with self.assertRaisesRegex(DocumentExtractionError, "unsafe_source_path"):
                    _verify_source({
                        "stored_path": str(outside),
                        "size": outside.stat().st_size,
                        "content_sha256": hashlib.sha256(outside.read_bytes()).hexdigest(),
                    })
                await indexer.start()
                for _ in range(100):
                    if database.recoveries > 1:
                        break
                    await asyncio.sleep(0.01)
                await indexer.close()

            self.assertGreater(database.recoveries, 1)
            self.assertEqual(outside.read_text(encoding="utf-8"), "keep")
            self.assertEqual(database.finalized, [])


if __name__ == "__main__":
    unittest.main()
