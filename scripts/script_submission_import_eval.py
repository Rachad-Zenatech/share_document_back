"""Shared submission import retry and access regressions; no live database writes."""
import importlib.util
import sys
import types
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, Mock, MagicMock, patch
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from models.sec_filings.script_review_model import AppImportSubmission


class ImportError(Exception):
    def __init__(self, message, status_code=400):
        super().__init__(message)
        self.status_code = status_code


class SharedSubmissionImportTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.owner = dict(id=uuid4(), email="owner@example.com", name="Owner")
        self.reviewer = dict(id=uuid4(), email="reviewer@example.com", name="Reviewer")
        self.payload = AppImportSubmission(user_email=self.owner["email"], title="abc", document_id="doc-social-submission-" + uuid4().hex,
            source_hash="a" * 64, external_ref={"id": str(uuid4())}, rows=[{"voiceover": "First line"}],
            invitees=[{"email": self.reviewer["email"], "role": "REVIEWER"}])
        self.app = types.ModuleType("app")
        self.app.AppServiceError = ImportError
        self.app.user_by_email = AsyncMock(side_effect=self.person)
        self.app.build_document = Mock(return_value={"blocks": [{"type": "paragraph", "text": "Hook score | invented"}, {"type": "financial_table"}]})
        self.app.save_document = AsyncMock()
        self.app.get_script = AsyncMock(return_value={"text": "Edited shared text", "revision": 5})
        self.app.rules = types.SimpleNamespace(TEMPLATE_ID="social-media-script", FORM_TYPE="Social Media Script")
        self.review = types.ModuleType("review")
        self.review.init_review = AsyncMock()
        self.review.ensure_schema = AsyncMock()
        self.review._set_invitees = AsyncMock()
        self.conn = MagicMock()
        self.conn.execute = AsyncMock()
        self.conn.executemany = AsyncMock()
        self.conn.fetch = AsyncMock(return_value=[{"email": self.reviewer["email"]}])
        self.conn.fetchrow = AsyncMock(side_effect=[None, {"status": "IN_REVIEW", "round": 1}])
        self.conn.fetchval = AsyncMock(return_value=False)
        self.conn.transaction.return_value.__aenter__.return_value = self.conn
        self.pool = MagicMock()
        self.pool.acquire.return_value.__aenter__.return_value = self.conn
        self.pool.fetchrow = AsyncMock(side_effect=[None, {"status": "IN_REVIEW", "round": 1}])
        self.pool.fetchval = AsyncMock(return_value=False)
        database = types.ModuleType("database")
        database.get_pool = Mock(return_value=self.pool)
        modules = {"postgresql_db.database": database, "services.sec_filings.script_app_service": self.app,
                   "services.sec_filings.script_review_service": self.review}
        with patch.dict(sys.modules, modules):
            spec = importlib.util.spec_from_file_location("submission_import", ROOT / "services/sec_filings/script_submission_import.py")
            self.service = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(self.service)

    def person(self, email):
        if email == self.owner["email"]:
            return self.owner
        if email == self.reviewer["email"]:
            return self.reviewer
        raise ImportError("Unknown person", 422)

    async def test_import_creates_review_with_assigned_people_and_no_invented_facts(self):
        got = await self.service.import_submission(self.payload)
        self.assertEqual(got["status"], "IN_REVIEW")
        self.assertEqual(got["document_id"], self.payload.document_id)
        people = self.conn.executemany.call_args.args[-1]
        self.assertEqual(people[0][1], self.reviewer["id"])
        import json
        created = next(c for c in self.conn.execute.await_args_list if "INSERT INTO sec_documents" in c.args[0])
        document = json.loads(created.args[-1])
        self.assertEqual(document["blocks"], [{"type": "financial_table"}])
        self.assertIn("pg_advisory_xact_lock", self.conn.execute.await_args_list[0].args[0])

    async def test_retry_preserves_current_shared_edits_status_and_checkins(self):
        content = {"externalRef": {"sourceHash": self.payload.source_hash}, "blocks": [{"text": "Real edits"}]}
        self.conn.fetchrow.side_effect = [{"owner_id": self.owner["id"], "content": content, "deleted_at": None}, {"status": "APPROVED", "round": 2}]
        self.conn.fetchval.return_value = True
        result = await self.service.import_submission(self.payload)
        self.assertEqual(result["status"], "APPROVED")
        self.assertEqual(result["text"], "Edited shared text")
        self.app.save_document.assert_not_awaited()
        self.review._set_invitees.assert_not_awaited()
        self.conn.executemany.assert_not_awaited()
        self.assertFalse(any("INSERT INTO sec_documents" in c.args[0] for c in self.conn.execute.await_args_list))
        self.assertFalse(any("UPDATE sec_script_reviews" in c.args[0] for c in self.conn.execute.await_args_list))

    async def test_different_upload_content_cannot_overwrite_existing_shared_review(self):
        self.conn.fetchrow.side_effect = [{"owner_id": self.owner["id"], "deleted_at": None, "content": {"externalRef": {"sourceHash": "b" * 64}}}]
        with self.assertRaises(ImportError) as error:
            await self.service.import_submission(self.payload)
        self.assertEqual(error.exception.status_code, 409)
        self.app.save_document.assert_not_awaited()
        self.assertIn("pg_advisory_xact_lock", self.conn.execute.await_args_list[0].args[0])

    async def test_unknown_required_reviewer_fails_before_creation(self):
        self.payload.invitees[0].email = "missing@example.com"
        with self.assertRaises(ImportError):
            await self.service.import_submission(self.payload)
        self.app.save_document.assert_not_awaited()
        self.conn.fetchrow.assert_not_awaited()

    async def test_owner_cannot_be_only_reviewer(self):
        self.payload.invitees[0].email = self.owner["email"]
        with self.assertRaises(ImportError):
            await self.service.import_submission(self.payload)
        self.app.save_document.assert_not_awaited()

    async def test_optional_missing_manager_does_not_block_assigned_review(self):
        from models.sec_filings.script_review_model import AppInvitee
        self.payload.invitees.append(AppInvitee(email="missing@example.com", role="MANAGER", optional=True))
        result = await self.service.import_submission(self.payload)
        self.assertEqual(result["status"], "IN_REVIEW")
        self.assertEqual(len(self.conn.executemany.call_args.args[-1]), 1)

    async def test_deleted_document_is_not_recreated(self):
        self.conn.fetchrow.side_effect = [{"owner_id": self.owner["id"], "deleted_at": "deleted", "content": {}}]
        with self.assertRaises(ImportError):
            await self.service.import_submission(self.payload)
        self.app.save_document.assert_not_awaited()


if __name__ == "__main__":
    unittest.main(verbosity=2)
