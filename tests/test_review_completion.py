"""修复实际调用链的回归；仅临时库、合成正文和模拟 HTTP 响应。"""
import json
import tempfile
import unittest
import urllib.error
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from test_note_processor import FakeNoteModel, make_processor, make_suggestion
import test_review_fixes_round2 as round2
from pka_graph.discovery import DiscoveryService
from pka_graph.note_processor import HttpNoteModel, NoteBudget, NoteProcessor, split_sections, assess_source_text
from pka_graph.quiz_service import QuizService
from pka_graph.models import ValidationError


class ProcessingCompletionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.skills = self.root / "skills"
        self.skills.mkdir()
        (self.skills / "2026-10-03-通用.md").write_text("结论先行", encoding="utf-8")
        self.sug = make_suggestion(self.root, full_text="# 原理\n" + "恢复进度需要持久化水位线。" * 70)

    def tearDown(self):
        self.tmp.cleanup()

    def test_one_budget_allows_one_actual_attempt_in_process(self):
        budget = NoteBudget(daily_model_calls_limit=1, max_retries=2)
        model = HttpNoteModel("https://offline.invalid/v1", "synthetic", "offline", budget)
        proc = NoteProcessor(model, budget, self.skills, self.root / "个人学习状态")
        with patch.object(model, "_send", side_effect=urllib.error.URLError("offline")) as send:
            out = proc.process(suggestion=self.sug, vault_root=self.root)
        self.assertFalse(out["publishable"])
        self.assertEqual(send.call_count, 1)
        self.assertEqual(proc.attempts_today(), 1)

    def test_two_successful_http_calls_charge_two_not_four(self):
        budget = NoteBudget(daily_model_calls_limit=2, max_retries=0)
        model = HttpNoteModel("https://offline.invalid/v1", "synthetic", "offline", budget)
        proc = NoteProcessor(model, budget, self.skills, self.root / "个人学习状态")
        def response(text):
            return {"choices": [{"message": {"content": text}}]}
        responses = [response(json.dumps({"points": [], "section_summary": ""})),
                     response("标题：水位线原理\n结论：恢复需要持久化。\n[笔记：s1]\n我理解了持久化的意义。")]
        with patch.object(model, "_send", side_effect=responses) as send:
            out = proc.process(suggestion=self.sug, vault_root=self.root)
        self.assertTrue(out["publishable"])
        self.assertEqual(send.call_count, 2)
        self.assertEqual(proc.attempts_today(), 2)

    def test_partial_pdf_never_publishes(self):
        proc = make_processor(FakeNoteModel(), self.skills, self.root)
        source = proc.resolve_material_text(self.sug, self.root)
        source.update(scope="pdf_partial", pdf={"truncated": True})
        with patch.object(proc, "resolve_material_text", return_value=source):
            out = proc.process(suggestion=self.sug, vault_root=self.root)
        self.assertFalse(out["complete"])
        self.assertFalse(out["publishable"])
        self.assertEqual(DiscoveryService._admission(out)["outcome"], "needs_source")

    def test_missing_source_precedes_model_retry(self):
        out = {"ok": False, "reason": "提取失败", "source": {
            "obtained": False, "body_usable": False, "scope": "summary_only"}}
        self.assertEqual(DiscoveryService._admission(out)["outcome"], "needs_source")

    def test_short_catalog_with_heading_is_not_body(self):
        out = assess_source_text("# 课程目录\n第一章 系统\n第二章 反馈\n第三章 建模")
        self.assertFalse(out["body_usable"])

    def test_hard_chunks_reassemble_without_inserted_newlines(self):
        model = FakeNoteModel()
        proc = make_processor(model, self.skills, self.root,
                              segment_max_chars=160, per_note_calls_cap=20)
        text = (self.root / self.sug["sources"][0]["path"]).read_text(encoding="utf-8")
        out = proc.process(suggestion=self.sug, vault_root=self.root)
        self.assertTrue(out["complete"])
        self.assertEqual("".join(model.segment_texts), text)
        self.assertNotIn(split_sections(text)[0]["text"], out["body"])

    def test_composer_receives_tail_of_long_section(self):
        model = HttpNoteModel("https://offline.invalid/v1", "synthetic", "offline", NoteBudget())
        section = {"section_id": "s1", "label": "长节", "text": "原" * 6200 + "末尾关键机制", "points": []}
        with patch.object(model, "_chat", return_value="标题：原理\n结论：机制\n[笔记：s1]\n笔记") as chat:
            model.compose_note("规范", "标题", [section], {}, [])
        self.assertIn("末尾关键机制", chat.call_args.args[1])

    def test_unknown_or_duplicate_sections_block_publication(self):
        model = FakeNoteModel()
        compose = model.compose_note
        for extra in ("unknown", "duplicate"):
            def output(*args, **kwargs):
                result = compose(*args, **kwargs)
                if extra == "unknown":
                    result["notes"]["s999"] = "多余笔记"
                else:
                    result["duplicate_ids"] = ["s1"]
                return result
            with patch.object(model, "compose_note", side_effect=output):
                out = make_processor(model, self.skills, self.root).process(
                    suggestion=self.sug, vault_root=self.root)
            self.assertFalse(out["publishable"])


class ClueRecoveryCompletionTests(unittest.TestCase):
    setUp = round2.ClueRecoveryTests.setUp
    tearDown = round2.ClueRecoveryTests.tearDown
    _suggestion = round2.ClueRecoveryTests._suggestion

    def prepare(self):
        sug = self._suggestion()
        self.disc._save_suggestions([sug])
        return sug

    def test_main_recovery_entry_repairs_crash_before_file_publish(self):
        sug = self.prepare()
        with patch("pka_graph.discovery.os.link", side_effect=OSError("模拟发布中断")):
            with self.assertRaises(OSError):
                self.disc.accept(None, sug["id"], edits={})
        intent = self.disc._load_intents()[0]
        self.assertEqual(intent["stage"], "prepared")
        self.assertFalse((self.vault / intent["clue_relpath"]).exists())
        out = self.disc.recover_intents()
        self.assertTrue(any(r["action"] == "completed" for r in out["recovered"]))
        self.assertEqual((self.vault / intent["clue_relpath"]).read_text(encoding="utf-8"), intent["payload"])
        self.assertEqual(self.disc._load_suggestions()[0]["status"], "accepted")
        self.assertEqual(self.disc._load_intents(), [])

    def test_recovery_save_failure_keeps_intent_for_retry(self):
        sug = self.prepare()
        with patch.object(self.disc, "_save_suggestions", side_effect=OSError("保存失败")):
            with self.assertRaises(OSError):
                self.disc.accept(None, sug["id"], edits={})
            with self.assertRaises(OSError):
                self.disc.recover_intents()
        self.assertEqual(len(self.disc._load_intents()), 1)
        self.disc.recover_intents()
        self.assertEqual(self.disc._load_suggestions()[0]["status"], "accepted")
        self.assertEqual(self.disc._load_intents(), [])

    def test_direct_retry_never_overwrites_manual_changes(self):
        sug = self.prepare()
        with patch.object(self.disc, "_save_suggestions", side_effect=OSError("保存失败")):
            with self.assertRaises(OSError):
                self.disc.accept(None, sug["id"], edits={})
        intent = self.disc._load_intents()[0]
        path = self.vault / intent["clue_relpath"]
        path.write_text("人工修改不能覆盖", encoding="utf-8")
        with self.assertRaises(ValidationError):
            self.disc.accept(None, sug["id"], edits={})
        self.disc.recover_intents()
        self.assertEqual(path.read_text(encoding="utf-8"), "人工修改不能覆盖")
        self.assertEqual(len(self.disc._load_intents()), 1)

    def test_matching_existing_file_reused_before_write_guard(self):
        sug = self.prepare()
        clue = self.disc._write_clue_note(sug, "测试", {"reason": "缺原文"}, None)
        prior = self.disc._load_intents()[0]
        with patch.object(self.gs.store, "safe_write_path", side_effect=AssertionError("已有文件不得重写")):
            reused = self.disc._write_clue_note(sug, "测试", {}, None, prior=prior)
        self.assertTrue(reused["reused"])
        self.assertEqual(reused["relpath"], clue["relpath"])

    def test_quiz_retains_resolver_warnings(self):
        path = self.vault / "正式笔记" / "取材.md"
        path.parent.mkdir(exist_ok=True)
        path.write_text("---\nstatus: accepted\n---\n正文", encoding="utf-8")
        quiz = QuizService.__new__(QuizService)
        quiz.svc = self.gs
        quiz.budget = SimpleNamespace(max_input_chars=6000)
        with patch.object(self.gs, "resolve_note_link", return_value={
            "status": "ok", "path": "正式笔记/取材.md", "warnings": ["登记指纹漂移"]}):
            out = quiz._collect_note_material({"note_links": [{"node_id": "node-test"}]}, "node-test")
        self.assertEqual(out["excerpts"][0]["warnings"], ["登记指纹漂移"])
