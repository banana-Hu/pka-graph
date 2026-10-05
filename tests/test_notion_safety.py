"""Notion fault injection with an in-memory client; no network or real credentials."""

import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest.mock import patch
from uuid import uuid4

from pka_graph.notion_sync import (
    FileLedger, NotionClient, NotionSyncCore, SyncError, STATUS_FULL, STATUS_UNKNOWN,
)


class MemoryNotion:
    def __init__(self):
        self.pages = {}
        self.create_calls = 0
        self.append_calls = 0
        self.read_calls = 0
        self.list_calls = 0
        self.fail_create = False
        self.fail_append = False
        self.fail_reads = False

    def create_page(self, parent_page_id, title, markdown, type_label=""):
        self.create_calls += 1
        page_id = str(uuid4())
        self.pages[page_id] = {"title": title, "markdown": markdown}
        if self.fail_create:
            raise SyncError("模拟写入响应丢失", category=STATUS_UNKNOWN)
        return {"page_id": page_id, "url": "https://example.invalid/" + page_id}

    def append_markdown(self, page_id, markdown):
        self.append_calls += 1
        self.pages[page_id]["markdown"] += "\n\n" + markdown
        if self.fail_append:
            raise SyncError("模拟追加响应丢失", category=STATUS_UNKNOWN)
        return {}

    def read_markdown(self, page_id):
        self.read_calls += 1
        if self.fail_reads:
            raise SyncError("模拟核查不可用", category="remote_error")
        return {"markdown": self.pages[page_id]["markdown"], "unknown_block_ids": []}

    def list_child_pages(self, parent_page_id):
        self.list_calls += 1
        return [{"page_id": key, "title": value["title"]} for key, value in self.pages.items()]


class NotionSafetyTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.client = MemoryNotion()
        self.cfg = {
            "state_dir": str(self.root / "state"),
            "local_roots": [str(self.root)],
            "targets": {"notes": {"parent_page_id": str(uuid4())}},
            "page_links": {},
        }

    def make_core(self):
        return NotionSyncCore(self.cfg, client=self.client)

    def make_note(self, name="one.md", long=False):
        path = self.root / name
        text = "# 中文笔记\n\n我理解的核心原理。\n"
        if long:
            text += "\n## 第一部分\n\n" + "甲" * 25000 + "\n\n## 第二部分\n\n" + "乙" * 25000 + "\n"
        path.write_text(text, encoding="utf-8")
        return path

    def sync(self, path, **options):
        return self.make_core().sync(source_path=str(path), target_alias="notes", **options)

    def test_corrupt_ledger_blocks_writes_and_preserves_original(self):
        ledger = FileLedger(Path(self.cfg["state_dir"]))
        ledger.path.write_text("{broken", encoding="utf-8")
        original = ledger.path.read_bytes()
        with self.assertRaises(SyncError) as caught:
            ledger.update(lambda data: data["docs"].clear())
        self.assertEqual(caught.exception.category, "ledger_corrupt")
        result = self.sync(self.make_note())
        self.assertEqual(result["error"]["category"], "ledger_corrupt")
        self.assertEqual(ledger.path.read_bytes(), original)
        self.assertEqual(self.client.create_calls, 0)

    def test_wrong_ledger_structure_is_not_treated_as_empty(self):
        ledger = FileLedger(Path(self.cfg["state_dir"]))
        ledger.path.write_text('{"tasks": [], "docs": {}}', encoding="utf-8")
        with self.assertRaises(SyncError):
            ledger.read()
        self.assertIn('"tasks": []', ledger.path.read_text(encoding="utf-8"))

    def test_write_http_500_is_unknown_and_not_automatically_retried(self):
        error = urllib.error.HTTPError("https://example.invalid/pages", 500, "error", {}, None)
        with patch("urllib.request.urlopen", side_effect=error) as request:
            with self.assertRaises(SyncError) as caught:
                NotionClient("fake").create_page("parent", "标题", "正文")
        self.assertEqual(caught.exception.category, STATUS_UNKNOWN)
        self.assertEqual(request.call_count, 1)

    def test_same_second_same_content_different_files_have_distinct_task_ids(self):
        first, second = self.make_note("first.md"), self.make_note("second.md")
        with patch("pka_graph.notion_sync.time.time", return_value=1000):
            a, b = self.sync(first), self.sync(second)
        self.assertEqual(a["status"], STATUS_FULL)
        self.assertEqual(b["status"], STATUS_FULL)
        self.assertNotEqual(a["task_id"], b["task_id"])
        self.assertEqual(len(self.make_core().ledger.read()["tasks"]), 2)

    def test_same_title_user_page_is_not_read_or_appended(self):
        old_id = "personal-old-page"
        self.client.pages[old_id] = {"title": "中文笔记", "markdown": "用户自己的笔记"}
        result = self.sync(self.make_note(long=True))
        self.assertEqual(result["status"], STATUS_FULL)
        self.assertNotEqual(result["page_id"], old_id)
        self.assertEqual(self.client.pages[old_id]["markdown"], "用户自己的笔记")
        self.assertEqual(self.client.list_calls, 0)

    def test_unknown_creation_does_not_search_title_or_create_again(self):
        self.client.fail_create = True
        path = self.make_note()
        first = self.sync(path)
        self.assertEqual(first["status"], STATUS_UNKNOWN)
        self.client.fail_create = False
        second = self.sync(path)
        self.assertEqual(second["status"], STATUS_UNKNOWN)
        self.assertEqual(second["task_id"], first["task_id"])
        self.assertEqual(self.client.create_calls, 1)
        self.assertEqual(self.client.list_calls, 0)
        self.assertEqual(self.client.read_calls, 0)
        self.assertEqual(self.make_core().ledger.read()["tasks"][first["task_id"]]["status"],
                         STATUS_UNKNOWN)

    def test_unknown_append_needs_successful_read_before_retry(self):
        self.client.fail_append = True
        path = self.make_note(long=True)
        first = self.sync(path)
        self.assertEqual(first["status"], STATUS_UNKNOWN)
        self.client.fail_append = False
        self.client.fail_reads = True
        calls = self.client.append_calls
        second = self.sync(path)
        self.assertEqual(second["status"], STATUS_UNKNOWN)
        self.assertEqual(second["task_id"], first["task_id"])
        self.assertEqual(self.client.append_calls, calls)
        self.client.fail_reads = False
        third = self.sync(path)
        self.assertEqual(third["status"], STATUS_FULL)
        self.assertEqual(self.client.append_calls, calls)
        self.assertEqual(self.client.create_calls, 1)

    def test_identical_repeated_sections_are_not_mistaken_for_completed_batches(self):
        path = self.root / "repeated.md"
        repeated = "## 重复部分\n\n" + "甲" * 25000
        body = "# 中文笔记\n\n" + repeated + "\n\n" + repeated + "\n"
        path.write_text(body, encoding="utf-8")
        result = self.sync(path)
        self.assertEqual(result["status"], STATUS_FULL)
        self.assertEqual(self.client.append_calls, 1)
        self.assertEqual(self.client.pages[result["page_id"]]["markdown"].count(repeated), 2)

    def test_release_from_other_core_does_not_remove_claim(self):
        first, other = self.make_core(), self.make_core()
        with patch("os.kill", side_effect=AssertionError("PID probing forbidden")):
            self.assertTrue(first.claim_doc("doc"))
            other.release_doc("doc")
            self.assertFalse(other.claim_doc("doc"))
            owner = first.ledger.read()["claims"]["doc"]["owner"]
            first.ledger.update(lambda data: data["claims"]["doc"].update(owner="new-owner"))
            first.release_doc("doc")
            self.assertEqual(first.ledger.read()["claims"]["doc"]["owner"], "new-owner")
            self.assertNotEqual(owner, "new-owner")
            self.assertTrue(other.claim_doc("doc"))
            other.release_doc("doc")

    def test_unknown_new_version_reuses_pending_version_without_another_create(self):
        path = self.make_note()
        self.assertEqual(self.sync(path)["status"], STATUS_FULL)
        path.write_text("# 中文笔记新版\n\n新的原理。\n", encoding="utf-8")
        self.client.fail_create = True
        first = self.sync(path, new_version=True)
        self.assertEqual(first["status"], STATUS_UNKNOWN)
        self.client.fail_create = False
        second = self.sync(path, new_version=True)
        self.assertEqual(second["status"], STATUS_UNKNOWN)
        self.assertEqual(first["task_id"], second["task_id"])
        self.assertEqual(self.client.create_calls, 2)
