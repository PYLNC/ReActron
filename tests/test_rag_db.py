"""Project-scoped RAG database lifecycle tests."""

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from backend.db import Database, ResourceNotFound


class RAGDatabaseTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db = Database(Path(self.temp.name) / "rag.db")
        self.project = self.db.create_project("RAG")
        self.db.set_project_rag_enabled(self.project["id"], True)

    def tearDown(self):
        self.db.close()
        self.temp.cleanup()

    def register(self, title="guide.md", digest="a" * 64, parser_version=1):
        return self.db.register_rag_document(
            self.project["id"], title, f"knowledge/{title}", 128,
            "text/markdown", digest, parser_version,
        )

    def ready(self, *, content="完整部署指南 supports deployment and AI.", digest="a" * 64):
        document, _ = self.register(digest=digest)
        claimed = self.db.claim_next_rag_document("worker", 300)
        self.assertEqual(claimed["id"], document["id"])
        self.assertTrue(self.db.renew_rag_lease(document["id"], "worker", 300))
        ready = self.db.complete_rag_document(document["id"], "worker", [{
            "content": content,
            "section": "部署",
            "locator": {"line_start": 1, "line_end": 2},
        }])
        return ready

    def test_schema_dedupes_content_regardless_of_parser_and_survives_without_fts(self):
        self.assertEqual(self.db._conn.execute("PRAGMA user_version").fetchone()[0], 7)
        first, created = self.register(parser_version=1)
        self.assertTrue(created)
        duplicate, created = self.register(parser_version=1)
        self.assertFalse(created)
        self.assertEqual(duplicate["id"], first["id"])
        reparsed, created = self.register(parser_version=2)
        self.assertFalse(created)
        self.assertEqual(reparsed["id"], first["id"])
        self.assertEqual(reparsed["parser_version"], 1)
        self.assertEqual([
            row["name"] for row in self.db._conn.execute(
                "PRAGMA index_info(uq_knowledge_document_content)"
            )
        ], ["project_id", "content_sha256"])
        self.assertEqual(self.db._conn.execute("PRAGMA foreign_key_check").fetchall(), [])
        self.assertEqual(self.db._conn.execute("PRAGMA integrity_check").fetchone()[0], "ok")

        class NoFTSDatabase(Database):
            def ensure_rag_fts(self):
                return False

        other = NoFTSDatabase(Path(self.temp.name) / "no-fts.db")
        try:
            self.assertFalse(other.rag_fts_available())
            self.assertEqual(other._conn.execute("PRAGMA user_version").fetchone()[0], 7)
        finally:
            other.close()

    def test_prerelease_v7_parser_keyed_duplicates_are_repaired(self):
        path = Path(self.temp.name) / "prerelease-v7.db"
        legacy = Database(path)
        project = legacy.create_project("prerelease")
        first, _ = legacy.register_rag_document(
            project["id"], "first.md", "knowledge/first.md", 1,
            "text/markdown", "c" * 64, 1, "first",
        )
        legacy._execute("DROP INDEX uq_knowledge_document_content")
        legacy._execute("""
            CREATE UNIQUE INDEX uq_knowledge_document_content
            ON knowledge_documents(project_id,content_sha256,parser_version)
            WHERE deleted_at IS NULL
        """)
        legacy._execute("""
            INSERT INTO knowledge_documents
                (id,project_id,title,stored_path,size,mime_type,content_sha256,
                 parser_version,status,created_at,updated_at)
            VALUES ('second',?,'second.md','knowledge/second.md',1,'text/markdown',
                    ?,2,'pending','now','now')
        """, (project["id"], "c" * 64))
        legacy.close()

        repaired = Database(path)
        try:
            rows = repaired.list_rag_documents(project["id"], include_deleted=True)
            by_id = {row["id"]: row for row in rows}
            self.assertEqual(by_id[first["id"]]["status"], "pending")
            self.assertEqual(by_id["second"]["status"], "deleting")
            self.assertIsNotNone(by_id["second"]["deleted_at"])
            self.assertEqual([
                row["name"] for row in repaired._conn.execute(
                    "PRAGMA index_info(uq_knowledge_document_content)"
                )
            ], ["project_id", "content_sha256"])
        finally:
            repaired.close()

    def test_ingest_search_snapshot_and_delete(self):
        document = self.ready()
        self.assertEqual(self.db.search_rag_chunks(self.project["id"], "部署指南")[0]["document_id"], document["id"])
        self.assertEqual(self.db.search_rag_chunks(self.project["id"], "AI")[0]["document_id"], document["id"])
        self.assertEqual(self.db.search_rag_chunks(self.project["id"], "missingterm AI"), [])
        with self.assertRaises(ResourceNotFound):
            self.db.search_rag_chunks("missing", "部署")

        session = self.db.create_session("RAG Agent", self.project["id"])
        task = self.db.create_task(session["id"], "请按部署指南完成 deployment")
        run, created = self.db.create_run(
            task["id"], "rag-run", config_snapshot={}, tool_snapshot={},
        )
        self.assertTrue(created)
        snapshot = json.loads(run["rag_snapshot_json"])
        self.assertEqual(snapshot["project_id"], self.project["id"])
        self.assertEqual(snapshot["chunks"][0]["citation_id"], "K1")
        self.assertIn("部署指南", self.db.hydrate_run_rag_chunks(run["id"])[0]["content"])
        self.assertTrue(self.db.get_run_rag_sources(run["id"])[0]["available"])
        with patch.object(
            self.db, "hydrate_run_rag_chunks",
            side_effect=AssertionError("detail lookup must stay in one atomic read"),
        ):
            self.assertIn("部署指南", self.db.get_run_rag_source(run["id"], "K1")["content"])

        self.db.soft_delete_rag_document(self.project["id"], document["id"])
        self.assertEqual(self.db.search_rag_chunks(self.project["id"], "部署指南"), [])
        self.assertEqual(self.db.hydrate_run_rag_chunks(run["id"]), [])
        source = self.db.get_run_rag_source(run["id"], "K1")
        self.assertFalse(source["available"])
        self.assertNotIn("content", source)
        self.db.finalize_rag_document_delete(document["id"])
        self.assertEqual(self.db.get_rag_status(self.project["id"])["source_bytes"], 0)

    def test_lease_recovery_retry_and_project_move(self):
        document, _ = self.register(digest="b" * 64)
        claimed = self.db.claim_next_rag_document("worker", 300)
        self.assertEqual(claimed["id"], document["id"])
        self.assertEqual(
            self.db.recover_expired_rag_leases("9999-01-01T00:00:00.000Z"),
            [document["id"]],
        )
        for attempt in range(2, 4):
            claimed = self.db.claim_next_rag_document("worker", 300)
            failed = self.db.fail_rag_document(document["id"], "worker", "parse_failed")
            self.assertEqual(failed["attempt"], attempt)
        self.assertEqual(failed["status"], "failed")
        retried = self.db.retry_rag_document(self.project["id"], document["id"])
        self.assertEqual((retried["status"], retried["attempt"]), ("pending", 0))

        claimed = self.db.claim_next_rag_document("worker", 300)
        self.db.complete_rag_document(claimed["id"], "worker", [{
            "content": "movable knowledge content",
            "locator": {"line_start": 1},
        }])
        default_project = next(item for item in self.db.list_projects() if item["is_default"])
        default_copy, _ = self.db.register_rag_document(
            default_project["id"], "default.md", "knowledge/default.md", 1,
            "text/markdown", "c" * 64, 1,
        )
        source_copy, _ = self.db.register_rag_document(
            self.project["id"], "source.md", "knowledge/source.md", 1,
            "text/markdown", "c" * 64, 2,
        )
        self.assertTrue(self.db.delete_project(self.project["id"]))
        moved = self.db.get_rag_document(default_project["id"], document["id"])
        self.assertEqual(moved["status"], "ready")
        self.assertEqual(
            self.db.get_rag_document(default_project["id"], default_copy["id"])["status"],
            "pending",
        )
        conflicted = self.db.get_rag_document(default_project["id"], source_copy["id"])
        self.assertEqual(conflicted["status"], "deleting")
        self.assertEqual(self.db._conn.execute("PRAGMA foreign_key_check").fetchall(), [])


if __name__ == "__main__":
    unittest.main()
