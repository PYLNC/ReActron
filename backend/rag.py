"""Deterministic local RAG ingestion helpers and background indexer."""

import asyncio
import hashlib
import logging
import re
import threading
import unicodedata
import uuid
import weakref
from pathlib import Path

from backend.config import BASE_DIR
from backend.file_parser import DocumentExtractionError, DocumentSegment, extract_document


logger = logging.getLogger("agent.rag")
CHUNK_TARGET_CHARS = 1_200
CHUNK_MAX_CHARS = 1_600
CHUNK_OVERLAP_CHARS = 160
INDEX_LEASE_SECONDS = 60
INDEX_POLL_SECONDS = 2.0
KNOWLEDGE_DIR = BASE_DIR / "data" / "knowledge"

_indexers = weakref.WeakKeyDictionary()
_indexers_lock = threading.Lock()


def _normalized(value):
    value = unicodedata.normalize("NFKC", str(value or "")).replace("\r\n", "\n").replace("\r", "\n")
    return "".join(
        char for char in value
        if char in "\n\t" or not unicodedata.category(char).startswith("C")
    )


def _split_text(text):
    """Yield deterministic bounded spans, preferring structural boundaries."""
    start, length = 0, len(text)
    while start < length:
        if length - start <= CHUNK_MAX_CHARS:
            end = length
        else:
            target = min(start + CHUNK_TARGET_CHARS, length)
            floor = start + CHUNK_TARGET_CHARS // 2
            breaks = [
                text.rfind(marker, floor, target)
                for marker in ("\n\n", "\n", " ")
            ]
            split = max(breaks)
            end = split if split > start else target
        raw = text[start:end]
        left = len(raw) - len(raw.lstrip())
        right = len(raw.rstrip())
        if right > left:
            yield start + left, start + right, raw[left:right]
        if end >= length:
            break
        start = max(start + 1, end - CHUNK_OVERLAP_CHARS)
        while start < length and text[start].isspace():
            start += 1


def chunk_segments(document_id, parser_version, title, segments):
    """Convert extracted segments to deterministic DB-ready chunks."""
    if not isinstance(document_id, str) or not document_id:
        raise ValueError("document_id must be non-empty")
    if not isinstance(parser_version, int) or isinstance(parser_version, bool) or parser_version < 1:
        raise ValueError("parser_version must be positive")
    if not isinstance(title, str) or not title.strip():
        raise ValueError("title must be non-empty")
    chunks = []
    for segment in segments:
        if not isinstance(segment, DocumentSegment):
            raise ValueError("segments must contain DocumentSegment values")
        text = _normalized(segment.text).strip()
        for start, end, content in _split_text(text):
            digest = hashlib.sha256(content.encode("utf-8")).hexdigest()
            ordinal = len(chunks)
            chunk_id = hashlib.sha256(
                f"{document_id}\0{parser_version}\0{ordinal}\0{digest}".encode("utf-8")
            ).hexdigest()
            chunks.append({
                "id": chunk_id,
                "ordinal": ordinal,
                "title": title.strip()[:500],
                "section": _normalized(segment.section).strip()[:1000],
                "content": content,
                "content_sha256": digest,
                "locator": {**segment.locator, "char_start": start, "char_end": end},
            })
    return chunks


def _is_link(path):
    return path.is_symlink() or bool(getattr(path, "is_junction", lambda: False)())


def _knowledge_file_path(stored_path, *, allow_missing=False):
    """Resolve one direct regular file without following storage links."""
    if not isinstance(stored_path, (str, Path)) or not str(stored_path).strip():
        raise DocumentExtractionError("unsafe_source_path")
    path = Path(stored_path)
    try:
        root = KNOWLEDGE_DIR.resolve(strict=False)
        resolved = path.resolve(strict=False)
        if (
            _is_link(KNOWLEDGE_DIR.parent) or _is_link(KNOWLEDGE_DIR) or
            _is_link(path) or resolved.parent != root
        ):
            raise DocumentExtractionError("unsafe_source_path")
        if not path.exists():
            if allow_missing:
                return resolved
            raise DocumentExtractionError("source_missing")
        if not path.is_file() or path.resolve(strict=True) != resolved:
            raise DocumentExtractionError("unsafe_source_path")
        return resolved
    except DocumentExtractionError:
        raise
    except OSError as exc:
        raise DocumentExtractionError("source_missing") from exc


def _verify_source(document):
    path = _knowledge_file_path(document["stored_path"])
    digest, size = hashlib.sha256(), 0
    try:
        with path.open("rb") as handle:
            for block in iter(lambda: handle.read(1024 * 1024), b""):
                size += len(block)
                if size > document["size"] or size > 20 * 1024 * 1024:
                    raise DocumentExtractionError("source_hash_mismatch")
                digest.update(block)
    except OSError as exc:
        raise DocumentExtractionError("source_missing") from exc
    if size != document["size"] or digest.hexdigest() != document["content_sha256"]:
        raise DocumentExtractionError("source_hash_mismatch")
    return path


class RAGIndexer:
    def __init__(self, database):
        self.database = database
        self.owner = f"rag-{uuid.uuid4()}"
        self._event = asyncio.Event()
        self._task = None
        self._loop = None
        self._closing = False
        self._wake_requested = True

    async def start(self):
        if self._task and not self._task.done():
            return
        self._event = asyncio.Event()
        self._loop = asyncio.get_running_loop()
        self._closing = False
        try:
            await asyncio.to_thread(self.database.recover_expired_rag_leases)
        except Exception:
            # The live loop retries recovery; one transient startup error must not strand pending work.
            logger.warning("RAG 启动恢复失败；索引循环将继续重试")
        self._task = asyncio.create_task(self._run(), name="rag-indexer")
        self.wake()

    async def close(self):
        task = self._task
        if not task:
            return
        self._closing = True
        self.wake()
        await task
        self._task = None
        self._loop = None

    def wake(self):
        self._wake_requested = True
        loop = self._loop
        if loop and loop.is_running():
            loop.call_soon_threadsafe(self._event.set)

    async def _cleanup_deleting(self):
        rows = await asyncio.to_thread(self.database.list_deleting_rag_documents, 100)
        for row in rows:
            try:
                stored_path = row.get("stored_path")
                if stored_path:
                    path = await asyncio.to_thread(
                        _knowledge_file_path, stored_path, allow_missing=True,
                    )
                    await asyncio.to_thread(path.unlink, missing_ok=True)
                await asyncio.to_thread(self.database.finalize_rag_document_delete, row["id"])
            except (DocumentExtractionError, OSError):
                logger.warning("RAG 文件清理失败：document_id=%s", row["id"])

    async def _index(self, document):
        try:
            path = await asyncio.to_thread(_verify_source, document)
            if not await asyncio.to_thread(
                self.database.renew_rag_lease,
                document["id"], self.owner, INDEX_LEASE_SECONDS,
            ):
                return
            segments = await asyncio.to_thread(extract_document, path)
            chunks = await asyncio.to_thread(
                chunk_segments,
                document["id"], document["parser_version"], document["title"], segments,
            )
            if not await asyncio.to_thread(
                self.database.renew_rag_lease,
                document["id"], self.owner, INDEX_LEASE_SECONDS,
            ):
                return
            await asyncio.to_thread(
                self.database.complete_rag_document,
                document["id"], self.owner, chunks,
            )
            logger.info("RAG 文档索引完成：document_id=%s chunks=%d", document["id"], len(chunks))
        except asyncio.CancelledError:
            raise
        except DocumentExtractionError as exc:
            await self._fail(document["id"], exc.args[0] if exc.args else exc.code)
        except Exception:
            logger.warning("RAG 文档索引失败：document_id=%s", document["id"])
            await self._fail(document["id"], "index_failed")

    async def _fail(self, document_id, code):
        if not isinstance(code, str) or re.fullmatch(r"[a-z0-9_]{1,64}", code) is None:
            code = "index_failed"
        try:
            await asyncio.to_thread(
                self.database.fail_rag_document, document_id, self.owner, code,
            )
        except Exception:
            logger.warning("RAG 失败状态写入被 lease 拒绝：document_id=%s", document_id)

    async def _run(self):
        while not self._closing:
            try:
                await self._cleanup_deleting()
                await asyncio.to_thread(self.database.recover_expired_rag_leases)
                document = await asyncio.to_thread(
                    self.database.claim_next_rag_document,
                    self.owner, INDEX_LEASE_SECONDS,
                )
                if document:
                    await self._index(document)
                    continue
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.warning("RAG 索引循环失败")
            self._event.clear()
            if self._wake_requested:
                self._wake_requested = False
                continue
            try:
                await asyncio.wait_for(self._event.wait(), INDEX_POLL_SECONDS)
            except asyncio.TimeoutError:
                pass


def get_indexer(database):
    with _indexers_lock:
        indexer = _indexers.get(database)
        if indexer is None:
            indexer = RAGIndexer(database)
            _indexers[database] = indexer
        return indexer
