"""Crash recovery tests using only temporary Vaults and synthetic model output."""

import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from pka_graph.discovery import DiscoveryService
from pka_graph.models import ValidationError, new_node
from pka_graph.service import GraphService
from pka_graph.store import StateStore


class SyntheticProcessor:
    def __init__(self):
        self.calls = 0

    def process(self, **kwargs):
        self.calls += 1
        return {"ok": True, "title": "崩溃恢复的原理",
                "body": "## 核心结论\n\n我保留这段独有的完整模型正文，不能被恢复模板替代。\n",
                "skill_version": "test", "skill_kind": "通用", "calls": 1,
                "complete": True, "processed_range": "完整正文",
                "source": {"obtained": True, "body_usable": True, "scope": "full"},
                "segments": {"total": 1, "processed": 1, "unprocessed": [], "missing_notes": []},
                "ai_review": {"verdict": "pass"}}


class DiscoveryCommitSafetyTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.vault = Path(self.tmp.name)
        self.gs = GraphService(StateStore(self.vault))
        self.processor = SyntheticProcessor()
        self.disc = DiscoveryService(self.gs, upstream=[], note_processor=self.processor)
        self.suggestion = {
            "id": "sug-safety000001", "verdict": "new_node", "status": "pending",
            "name": "原始资料标题", "reason": "新增机制", "novelty_label": "待确认",
            "contribution": {"claims": ["机制"], "contribution_summary": "解释可靠提交"},
            "sources": [], "related": [], "material": {"text": "完整原始资料"},
        }
        self.disc._save_suggestions([self.suggestion])
        self.node = new_node(name="崩溃恢复的原理", domain="AI技术领域", scope="可靠提交")

    def write(self):
        proc = self.processor.process()
        return self.disc._write_candidate_note(
            self.suggestion, self.node, {}, "完整笔记.md", proc_result=proc,
            admission={"publishable": True, "source_scope": "full"})

    def accept(self):
        return self.disc.accept(None, self.suggestion["id"], edits={"domain": "AI技术领域"},
                                request_id="safety-request")

    def journal(self):
        return self.disc._load_intents()

    def test_full_payload_is_durable_before_note_becomes_visible(self):
        real_link = __import__("os").link
        observed = []
        def inspect_then_link(source, destination):
            intent = self.journal()[0]
            self.assertEqual(intent["stage"], "prepared")
            self.assertFalse(Path(destination).exists())
            self.assertEqual(Path(source).read_bytes(), intent["note_payload"].encode("utf-8"))
            observed.append(True)
            return real_link(source, destination)
        with patch("pka_graph.discovery.os.link", side_effect=inspect_then_link):
            result = self.write()
        self.assertEqual(observed, [True])
        self.assertTrue(result["path_obj"].exists())
        self.assertEqual(self.journal()[0]["stage"], "note_written")

    def test_failure_before_file_publish_recovers_exact_original_payload(self):
        with patch("pka_graph.discovery.os.link", side_effect=OSError("模拟发布前中断")):
            with self.assertRaises(OSError):
                self.write()
        intent = self.journal()[0]
        path = self.gs.store.safe_read_path(intent["note_relpath"])
        self.assertFalse(path.exists())
        report = self.disc.recover_intents()
        self.assertEqual(report["recovered"][0]["action"], "completed")
        self.assertEqual(path.read_text(encoding="utf-8"), intent["note_payload"])
        self.assertIn("独有的完整模型正文", path.read_text(encoding="utf-8"))
        self.assertEqual(self.processor.calls, 1)
        self.assertEqual(self.journal(), [])

    def test_journal_failure_does_not_create_a_note(self):
        with patch.object(self.disc, "_save_intents", side_effect=OSError("日志落盘失败")):
            with self.assertRaises(OSError):
                self.write()
        self.assertEqual(list(self.vault.rglob("完整笔记.md")), [])
        self.assertEqual(self.gs.state()["nodes"], [])

    def test_state_failure_preserves_note_and_journal_for_recovery(self):
        with patch.object(self.gs.store, "save", side_effect=OSError("状态落盘失败")):
            with self.assertRaises(OSError):
                self.accept()
        intent = self.journal()[0]
        path = self.gs.store.safe_read_path(intent["note_relpath"])
        self.assertTrue(path.exists())
        original = path.read_bytes()
        self.disc.recover_intents()
        self.assertEqual(path.read_bytes(), original)
        self.assertEqual(len(self.gs.state()["nodes"]), 1)
        self.assertEqual(self.disc._load_suggestions()[0]["status"], "accepted")
        self.assertEqual(self.journal(), [])
        self.assertEqual(self.processor.calls, 1)

    def test_suggestion_save_failure_keeps_intent_after_state_commit(self):
        with patch.object(self.disc, "_save_suggestions", side_effect=OSError("建议落盘失败")):
            with self.assertRaises(OSError):
                self.accept()
        self.assertEqual(len(self.gs.state()["nodes"]), 1)
        self.assertEqual(self.disc._load_suggestions()[0]["status"], "pending")
        self.assertEqual(len(self.journal()), 1)
        report = self.disc.recover_intents()
        self.assertEqual(report["recovered"][0]["action"], "already_committed")
        self.assertEqual(self.disc._load_suggestions()[0]["status"], "accepted")
        self.assertEqual(self.disc._load_suggestions()[0]["accepted"]["request_id"], "safety-request")
        self.assertEqual(len(self.gs.state()["nodes"]), 1)
        self.assertEqual(self.journal(), [])

    def test_manual_edit_during_failed_commit_is_never_deleted(self):
        def change_note_then_fail(*args, **kwargs):
            intent = self.journal()[0]
            self.gs.store.safe_read_path(intent["note_relpath"]).write_text("人工改过的正文", encoding="utf-8")
            raise OSError("模拟状态失败")
        with patch.object(self.gs.store, "save", side_effect=change_note_then_fail):
            with self.assertRaises(OSError):
                self.accept()
        intent = self.journal()[0]
        path = self.gs.store.safe_read_path(intent["note_relpath"])
        self.assertEqual(path.read_text(encoding="utf-8"), "人工改过的正文")
        report = self.disc.recover_intents()
        self.assertEqual(report["recovered"][0]["action"], "needs_manual")
        self.assertEqual(path.read_text(encoding="utf-8"), "人工改过的正文")
        self.assertEqual(self.gs.state()["nodes"], [])
        self.assertEqual(len(self.journal()), 1)

    def test_existing_note_collision_does_not_overwrite(self):
        directory = self.disc._formal_note_dir(self.node["domain"])
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / "完整笔记.md"
        path.write_text("原有个人笔记", encoding="utf-8")
        with self.assertRaises(FileExistsError):
            self.write()
        self.assertEqual(path.read_text(encoding="utf-8"), "原有个人笔记")
        report = self.disc.recover_intents()
        self.assertEqual(report["recovered"][0]["action"], "needs_manual")
        self.assertEqual(path.read_text(encoding="utf-8"), "原有个人笔记")

    def test_missing_file_restores_correct_formal_path_and_note_identity(self):
        result = self.write()
        intent = self.journal()[0]
        result["path_obj"].unlink()
        self.disc.recover_intents()
        links = self.gs.state()["note_links"]
        self.assertEqual(links[0]["ref"]["path"], intent["note_relpath"])
        self.assertEqual(links[0]["ref"]["note_id"], intent["note_id"])
        self.assertEqual(hashlib.sha256(result["path_obj"].read_bytes()).hexdigest(),
                         intent["note_sha256"])

    def test_legacy_missing_payload_does_not_generate_replacement_template(self):
        result = self.write()
        intent = self.journal()[0]
        intent.pop("note_payload")
        self.disc._save_intents([intent])
        result["path_obj"].unlink()
        report = self.disc.recover_intents()
        self.assertEqual(report["recovered"][0]["action"], "needs_manual")
        self.assertFalse(result["path_obj"].exists())
        self.assertEqual(self.gs.state()["nodes"], [])

    def test_corrupt_journal_or_suggestions_stop_before_writing(self):
        for attribute in ("intents_path", "suggestions_path"):
            with self.subTest(attribute=attribute):
                path = getattr(self.disc, attribute)
                existed = path.exists()
                original = path.read_bytes() if existed else None
                path.write_text('{"not": "a list"}', encoding="utf-8")
                corrupt = path.read_bytes()
                with self.assertRaises(ValidationError):
                    self.accept()
                self.assertEqual(path.read_bytes(), corrupt)
                self.assertEqual(list(self.vault.rglob("正式笔记/*.md")), [])
                if existed:
                    path.write_bytes(original)
                else:
                    path.unlink()

    def test_corrupt_intent_payload_is_not_used_for_recovery(self):
        result = self.write()
        intent = self.journal()[0]
        intent["note_payload"] += "未经确认的变更"
        self.disc._save_intents([intent])
        result["path_obj"].unlink()
        with self.assertRaises(ValidationError):
            self.disc.recover_intents()
        self.assertFalse(result["path_obj"].exists())
        self.assertEqual(self.gs.state()["nodes"], [])

    def test_direct_writer_stops_if_suggestion_queue_is_damaged(self):
        self.disc.suggestions_path.write_text("{broken", encoding="utf-8")
        with self.assertRaises(ValidationError):
            self.write()
        self.assertEqual(list(self.vault.rglob("完整笔记.md")), [])
        self.assertEqual(self.journal(), [])
