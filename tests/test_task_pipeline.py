"""真实模块接线的离线端到端：假模型不是能力验收，不触碰正式库。"""
import hashlib
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from pka_graph.server import build_service, OWNER_SUBJECT, OWNER_SCOPES
from pka_graph.workflow import Principal
from test_note_processor import FakeNoteModel, make_processor


class Analyzer:
    def __init__(self):
        self.parts = []

    def describe(self):
        return "offline-analyzer"

    def analyze(self, material, sources, related, fragments):
        self.parts.append(material["text"])
        return {"classification": "new_node", "contribution": {
            "problem": "进度怎样持久化", "claims": ["进度记录需要持久化"],
            "differences": [], "conditions_limits": []}, "differences_vs_related": [],
            "source_excerpts": [{"upstream_id": sources[0]["upstream_id"],
                                  "excerpt": material["text"][:20]}], "unknowns": []}


class Reviewer:
    def __init__(self, verdict="pass"):
        self.verdict, self.calls = verdict, 0

    def review(self, source_text, note_body, skill_text, task_context=None):
        self.calls += 1
        return {"verdict": self.verdict, "coverage": {"complete": True},
                "issues": [] if self.verdict == "pass" else [{"reason": "底层解释不完整"}],
                "reviewer_mode": "offline-test"}


class TaskPipelineTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.env = patch.dict(os.environ, {"PKA_TASK_PIPELINE": "task"}, clear=True)
        self.env.start()
        self.addCleanup(self.env.stop)
        self.service = build_service(self.root)
        self.skills = self.root / "测试技能"
        self.skills.mkdir()
        (self.skills / "2026-10-05-通用.md").write_text("结论先行，中文精华笔记。", encoding="utf-8")
        self.model = FakeNoteModel()
        processor = make_processor(self.model, self.skills, self.root)
        self.service.note_processor = processor
        self.service.discovery.note_processor = processor
        self.service.discovery.note_reviewer = Reviewer()
        self.service.discovery.require_ai_review = True
        self.analyzer = Analyzer()
        self.service.discovery.analyzer = self.analyzer
        self.service.inbox.analyzer = self.analyzer
        self.source = self.root / "个人学习状态" / "inbox" / "进度持久化.md"
        self.source.parent.mkdir(parents=True, exist_ok=True)
        self.text = "# 原理\n" + "进度持久化帮助系统在重启后恢复，不需要重新进行全部工作。" * 40
        self.source.write_text(self.text, encoding="utf-8")
        self.principal = Principal(OWNER_SUBJECT, OWNER_SCOPES)

    def run_task(self, publish_to=None):
        wf = self.service.workflow
        task = wf.submit({"contract_version": 1, "request_id": "offline-pipeline",
                          "source": {"kind": "file", "path": str(self.source)},
                          "options": {"pipeline": "note", "publish_to": publish_to}}, principal=self.principal)
        wf.execute(task["task_id"])
        return wf.get(task["task_id"], principal=self.principal)

    def test_full_module_pipeline_reviews_then_creates_untested_note(self):
        result = self.run_task()
        self.assertEqual(result["status"], "succeeded", result)
        self.assertEqual(self.service.discovery.note_reviewer.calls, 1)
        notes = list(self.root.rglob("正式笔记/*.md"))
        self.assertEqual(len(notes), 1)
        self.assertIn("## 核心结论", notes[0].read_text(encoding="utf-8"))
        self.assertNotIn("**原文**", notes[0].read_text(encoding="utf-8"))
        self.assertEqual(self.service.state()["nodes"][0]["status"], "untested")
        self.assertEqual(self.source.read_text(encoding="utf-8"), self.text)
        self.assertTrue(any(a["kind"] == "note" for a in result["outputs"]))

    def test_missing_reviewer_does_not_publish(self):
        self.service.discovery.note_reviewer = None
        result = self.run_task()
        self.assertEqual(result["status"], "blocked", result)
        self.assertEqual(list(self.root.rglob("正式笔记/*.md")), [])
        self.assertTrue(any(a["kind"] == "note_body" for a in result["outputs"]))

    def test_rejected_note_keeps_draft_and_stops_without_formal_write(self):
        self.service.discovery.note_reviewer = Reviewer("revise")
        result = self.run_task()
        self.assertEqual(result["status"], "needs_review", result)
        self.assertEqual(self.service.discovery.note_reviewer.calls, 3)
        self.assertEqual(list(self.root.rglob("正式笔记/*.md")), [])
        self.assertTrue(any(a["kind"] == "note_body" for a in result["outputs"]))

    def test_long_source_is_fully_analyzed_without_single_context_overflow(self):
        text = "# 原理\n" + "持久化进度可以在重启后恢复完整处理范围。" * 5000 + "末尾证据。"
        self.source.write_text(text, encoding="utf-8")
        result = self.run_task()
        self.assertEqual(result["status"], "succeeded", result["error"])
        self.assertGreater(len(self.analyzer.parts), 1)
        self.assertEqual("".join(self.analyzer.parts), text)
        self.assertEqual("".join(self.model.segment_texts), text)

    def test_revision_can_pass_only_for_the_revised_prepared_version(self):
        reviewer = Reviewer("revise")
        original_review = reviewer.review
        def review(*args, **kwargs):
            if reviewer.calls:
                reviewer.verdict = "pass"
            return original_review(*args, **kwargs)
        reviewer.review = review
        self.service.discovery.note_reviewer = reviewer
        result = self.run_task()
        self.assertEqual(result["status"], "succeeded", result["error"])
        self.assertEqual(reviewer.calls, 2)
        task = self.service.workflow.store.get(result["task_id"])
        approvals = list(task["review_approvals"].values())
        self.assertEqual([a["review"]["verdict"] for a in approvals], ["revise", "pass"])
        self.assertEqual(len([a for a in task["outputs"] if a["kind"] == "prepared_note"
                              and "prepared_ref" in a]), 2)

    def test_sync_retry_does_not_repeat_generation_or_review_or_commit(self):
        class Notion:
            cfg = {"targets": {"test": {}}}
            calls = 0
            def sync(self, **kwargs):
                self.calls += 1
                return {"status": "failed"} if self.calls == 1 else {
                    "status": "full_success", "page_id": "offline-page"}
        core = Notion()
        self.service.notion_core = core
        self.service.workflow.adapters["sync"]._core = core
        result = self.run_task("test")
        self.assertEqual(result["status"], "failed", result)
        model_calls = len(self.model.calls)
        wf = self.service.workflow
        wf.retry(result["task_id"], request_id="sync-retry", principal=self.principal)
        wf.execute(result["task_id"])
        second = wf.get(result["task_id"], principal=self.principal)
        self.assertEqual(second["status"], "succeeded", second["error"])
        self.assertEqual(core.calls, 2)
        self.assertEqual(len(self.model.calls), model_calls)
        self.assertEqual(self.service.discovery.note_reviewer.calls, 1)
        self.assertEqual(len(list(self.root.rglob("正式笔记/*.md"))), 1)


if __name__ == "__main__":
    unittest.main()
