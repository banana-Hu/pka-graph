"""冻结来源、恢复分段、取消与全文层级汇总；临时目录、注入模型。"""
import copy
import hashlib
import tempfile
import unittest
from pathlib import Path

from pka_graph.note_processor import (HttpNoteModel, NoteBudget, NoteModelError,
                                      NoteProcessor, render_note_sections)


class Context:
    def __init__(self, root, text):
        self.vault_root, self.source = root, {"frozen_text": text}
        self.artifacts, self.saved, self.cancelled, self.progress = (), None, False, []

    def cancel_requested(self):
        return self.cancelled

    def load_checkpoint(self):
        return copy.deepcopy(self.saved)

    def save_checkpoint(self, value):
        self.saved = copy.deepcopy(value)

    def report_progress(self, completed, total=None):
        self.progress.append((completed, total))


class Model:
    model, api_base = "offline", "https://offline.invalid/v1"

    def __init__(self):
        self.texts, self.composes = [], 0
        self.fail_compose, self.cancel_after_extract = False, None

    def describe(self):
        return "离线模型"

    def extract_segment_points(self, skill, label, text):
        self.texts.append(text)
        if self.cancel_after_extract:
            self.cancel_after_extract.cancelled = True
        return {"points": [{"point": "持久化状态能够支持恢复。", "excerpt": text[:8]}],
                "section_summary": "持久化状态能够支持恢复。"}

    def compose_note(self, skill, title, sections, ctx, related):
        self.composes += 1
        if self.fail_compose:
            raise NoteModelError("离线超时", category="timeout")
        return {"title": "恢复的依据", "conclusion": "我记住的是恢复需要持久状态。",
                "notes": {s["section_id"]: "恢复取决于持久状态，而不是进程是否继续存在。" for s in sections},
                "section_titles": {s["section_id"]: "恢复机制" for s in sections}}


class LayeredModel(HttpNoteModel):
    def __init__(self, budget):
        super().__init__("https://offline.invalid/v1", "offline", "offline", budget)
        self.texts, self.inputs = [], []

    def extract_segment_points(self, skill, label, text):
        self.texts.append(text)
        return {"points": [{"point": "持久化" + text[:100], "excerpt": text[:100]}],
                "section_summary": "恢复需要状态。"}

    def compose_note(self, skill, title, sections, ctx, related):
        self.inputs.append(sum(len(s["text"]) for s in sections))
        return {"title": "状态恢复", "conclusion": "恢复需要持久化与边界检查。",
                "notes": {s["section_id"]: "我记住恢复需要持久化与边界检查。" for s in sections},
                "section_titles": {s["section_id"]: "恢复机制" for s in sections}}

    def reduce_points(self, skill, text):
        self.inputs.append(len(text))
        return "恢复需要持久化与边界检查。"

    def summarize_note(self, skill, title, text):
        self.inputs.append(len(text))
        return {"title": "状态恢复", "conclusion": "我记住恢复需要持久化与边界检查。"}


class CheckpointTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.skills = self.root / "skills"
        self.skills.mkdir()
        self.skill_file = self.skills / "2026-10-05-通用.md"
        self.skill_file.write_text("中文精华，结论先行。", encoding="utf-8")
        self.text = "# 恢复\n" + "状态持久化后可通过检查点恢复处理。" * 80
        self.suggestion = {"name": "original", "sources": [], "related": []}

    def tearDown(self):
        self.temp.cleanup()

    def processor(self, model, budget=None):
        return NoteProcessor(model, budget or NoteBudget(segment_max_chars=500),
                             self.skills, self.root / "state")

    def test_compose_retry_reuses_all_successful_segments_across_restart(self):
        model, context = Model(), Context(self.root, self.text)
        model.fail_compose = True
        first = self.processor(model).process(suggestion=self.suggestion, vault_root=self.root,
                                             task_context=context)
        self.assertFalse(first["ok"])
        count = len(model.texts)
        model.fail_compose = False
        second = self.processor(model).process(suggestion=self.suggestion, vault_root=self.root,
                                              task_context=context)
        self.assertTrue(second["publishable"])
        self.assertEqual(len(model.texts), count)
        self.assertEqual(second["calls"], 1)
        self.assertNotIn(self.text, second["body"])
        self.assertEqual(second["source"]["text"], self.text)

    def test_skill_content_change_invalidates_checkpoint(self):
        model, context = Model(), Context(self.root, self.text)
        processor = self.processor(model)
        processor.process(suggestion=self.suggestion, vault_root=self.root, task_context=context)
        count = len(model.texts)
        self.skill_file.write_text("机制与边界，结论先行。", encoding="utf-8")
        processor.process(suggestion=self.suggestion, vault_root=self.root, task_context=context)
        self.assertEqual(len(model.texts), count * 2)

    def test_feedback_reuses_extraction_and_recomposes(self):
        model, context = Model(), Context(self.root, self.text)
        processor = self.processor(model)
        processor.process(suggestion=self.suggestion, vault_root=self.root, task_context=context)
        count = len(model.texts)
        result = processor.process(suggestion={**self.suggestion, "revision_feedback": [{"reason": "增加必要边界"}]},
                                   vault_root=self.root, task_context=context)
        self.assertEqual(len(model.texts), count)
        self.assertEqual(result["calls"], 1)

    def test_cancel_before_call_does_not_generate(self):
        model, context = Model(), Context(self.root, self.text)
        context.cancelled = True
        result = self.processor(model).process(suggestion=self.suggestion, vault_root=self.root,
                                              task_context=context)
        self.assertTrue(result["cancelled"])
        self.assertEqual(model.texts, [])

    def test_late_result_after_cancel_cannot_compose(self):
        model, context = Model(), Context(self.root, self.text)
        model.cancel_after_extract = context
        result = self.processor(model).process(suggestion=self.suggestion, vault_root=self.root,
                                              task_context=context)
        self.assertTrue(result["cancelled"])
        self.assertEqual(model.composes, 0)

    def test_frozen_artifact_instead_of_changed_original(self):
        frozen = self.root / "frozen.txt"
        frozen.write_text(self.text, encoding="utf-8")
        original = self.root / "original.txt"
        original.write_text("新的原文与冻结原文不同", encoding="utf-8")
        context = Context(self.root, "")
        context.source = {"path": str(original)}
        context.artifacts = ({"kind": "source_text", "path": "frozen.txt",
                              "sha256": hashlib.sha256(frozen.read_bytes()).hexdigest(), "complete": True},)
        model = Model()
        result = self.processor(model).process(suggestion=self.suggestion, vault_root=self.root,
                                              task_context=context)
        self.assertEqual("".join(model.texts), self.text)
        self.assertTrue(result["publishable"])

    def test_changed_frozen_artifact_blocks_before_model(self):
        path = self.root / "frozen.txt"
        path.write_text(self.text, encoding="utf-8")
        context = Context(self.root, "")
        context.source = {}
        context.artifacts = ({"kind": "source_text", "path": "frozen.txt", "sha256": "wrong", "complete": True},)
        model = Model()
        result = self.processor(model).process(suggestion=self.suggestion, vault_root=self.root,
                                              task_context=context)
        self.assertEqual(result["category"], "source_changed")
        self.assertEqual(model.texts, [])

    def test_long_file_hierarchy_reads_tail(self):
        budget = NoteBudget(segment_max_chars=500, compose_max_chars=2000)
        model = LayeredModel(budget)
        text = "# 机制\n" + "恢复依赖状态。" * 5000 + "最后的完整边界。"
        result = self.processor(model, budget).process(suggestion=self.suggestion, vault_root=self.root,
                                                       task_context=Context(self.root, text))
        self.assertTrue(result["complete"])
        self.assertEqual("".join(model.texts), text)
        self.assertTrue(all(size <= budget.compose_max_chars for size in model.inputs))
        self.assertNotIn("**原文**", result["body"])

    def test_compare_renderer_available(self):
        section = {"section_id": "s1", "label": "机制", "text": "原始依据。"}
        composed = {"conclusion": "总论", "notes": {"s1": "我的认识。"}}
        self.assertIn("原始依据。", render_note_sections([section], composed)["body"])
        self.assertNotIn("原始依据。", render_note_sections([section], composed, essence=True)["body"])


if __name__ == "__main__":
    unittest.main()
