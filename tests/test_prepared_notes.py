"""笔记准备、独立审核和受控提交的合成链路验证，不调用真实模型。"""

import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from pka_graph.discovery import DiscoveryService
from pka_graph.service import GraphService
from pka_graph.store import StateStore
from pka_graph.task_store import TaskStore
from pka_graph.workflow import Principal, StageContext, WorkflowError, WorkflowService


class FakeProcessor:
    def __init__(self, source_text):
        self.source_text = source_text
        self.calls = 0
        self.feedback = []
        self.complete = True

    def process(self, *, suggestion, vault_root, remote_fetch=False, task_context=None):
        self.calls += 1
        self.feedback.append(suggestion.get("revision_feedback"))
        return {"ok": True, "title": "串行处理与失败恢复", "body":
                "## 核心结论\n\n我理解的可靠处理，是保存完成的成果，只重试失败步骤。\n\n"
                "## 运作原理\n\n冻结原文固定本次输入，提交前重新核验版本；收录不代表掌握。",
                "source": {"text": self.source_text, "obtained": True, "body_usable": True,
                           "scope": "full"},
                "source_sha256": hashlib.sha256(self.source_text.encode("utf-8")).hexdigest(),
                "skill_text": "中文、结论先行、个人理解、原文依据、不要复述案例。",
                "skill_version": "synthetic-v1", "skill_kind": "通用知识处理",
                "complete": self.complete, "publishable": self.complete, "calls": 1,
                "processed_range": "全部原文", "segments": {"total": 1, "processed": 1,
                                                             "unprocessed": [], "missing_notes": []}}


class FakeReviewer:
    def __init__(self, verdict="pass", complete=True):
        self.verdict = verdict
        self.complete = complete
        self.calls = 0
        self.sources = []

    def review(self, source_text, note_body, skill_text, *, task_context=None):
        self.calls += 1
        self.sources.append(source_text)
        return {"verdict": self.verdict, "issues": [] if self.verdict == "pass" else
                [{"reason": "需要解释版本核验为什么能防止混用内容"}],
                "coverage": {"segments": 1 if self.complete else 0, "total": 1,
                             "complete": self.complete}, "reviewer_mode": "test",
                "source_sha256": hashlib.sha256(source_text.encode("utf-8")).hexdigest(),
                "note_sha256": hashlib.sha256(note_body.encode("utf-8")).hexdigest()}


class PreparedNoteTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.vault = Path(self.temp.name)
        (self.vault / ".pka-test-vault.json").write_text('{"purpose":"test"}', encoding="utf-8")
        (self.vault / "01-AI技术领域").mkdir()
        self.source_text = "冻结来源，按阶段保存成果。" * 1000 + "全文末尾：收录不代表使用者已经掌握。"
        self.original = self.vault / "原始研究.txt"
        self.original.write_text(self.source_text, encoding="utf-8")
        self.task_store = TaskStore(self.vault / "个人学习状态" / "tasks" / "_ledger")
        self.workflow = WorkflowService(self.task_store, adapters={},
            pipelines={"note": ("acquire", "extract", "generate", "validate", "review", "commit")},
            authorize=lambda principal, operation, task: None)
        self.owner = Principal("local:owner", frozenset({"tasks:submit", "tasks:read:self", "tasks:control:self"}))
        self.task_id = self.new_task("prepared-1")
        self.context = self.stage_context("generate")
        self.graph = GraphService(StateStore(self.vault))
        self.processor = FakeProcessor(self.source_text)
        self.reviewer = FakeReviewer()
        self.discovery = DiscoveryService(self.graph, upstream=[], note_processor=self.processor,
                                          note_reviewer=self.reviewer, require_ai_review=True)
        self.source_path = self.context.work_dir / "完整原文.txt"
        self.source_path.write_text(self.source_text, encoding="utf-8")
        source_artifact = self.discovery._prepared_artifact(self.context, self.source_path, "source_text")
        self.suggestion_id = "sug-prepared-test"
        self.suggestion = {"id": self.suggestion_id, "status": "pending", "verdict": "new_node",
            "name": "可靠笔记加工机制", "sources": [{"source_name": "合成原文", "url": "",
                "path": source_artifact["path"], "read_status": "full_text"}],
            "material": {"level": "full_text", "path": source_artifact["path"], "text": ""},
            "content_sha256": source_artifact["sha256"],
            "contribution": {"problem": "避免重复加工和错用原文版本", "claims": ["保存阶段成果"],
                             "differences": [], "conditions_limits": ["收录不代表掌握"]},
            "related": [{"domain": "AI技术领域"}], "accepted": None,
            "novelty_label": "合成材料中的可能知识增量", "reason": "仅用于离线接口验证"}
        self.discovery._save_suggestions([self.suggestion])

    def new_task(self, request_id):
        return self.workflow.submit({"contract_version": 1, "request_id": request_id,
            "source": {"kind": "file", "path": str(self.original)},
            "options": {"pipeline": "note", "publish_to": None}}, principal=self.owner)["task_id"]

    def stage_context(self, stage, task_id=None):
        return StageContext(self.workflow, self.task_store.get(task_id or self.task_id), stage)

    def prepare(self):
        return self.discovery.prepare_note(self.suggestion_id, context=self.context)

    def approve(self, prepared):
        ref = prepared["prepared_ref"]
        self.assertTrue(self.discovery.validate_prepared(ref, context=self.stage_context("validate"))["publishable"])
        review = self.discovery.review_prepared(ref, context=self.stage_context("review"))
        self.assertEqual(review["verdict"], "pass")
        return ref

    def assert_no_published_notes(self):
        self.assertEqual(self.graph.state()["nodes"], [])
        self.assertEqual(list((self.vault / "01-AI技术领域").rglob("*.md")), [])

    def test_prepare_only_creates_task_draft_not_node_or_formal_note(self):
        prepared = self.prepare()
        self.assert_no_published_notes()
        self.assertEqual(self.processor.calls, 1)
        self.assertEqual(self.reviewer.calls, 0)
        for artifact in prepared["artifacts"]:
            self.assertTrue((self.vault / artifact["path"]).is_relative_to(self.context.work_dir))
        envelope = json.loads((self.vault / prepared["prepared_ref"]).read_text(encoding="utf-8"))
        self.assertNotIn("text", envelope["processing_result"]["source"])

    def test_review_pass_allows_only_untested_node_and_preserves_full_source(self):
        ref = self.approve(self.prepare())
        result = self.discovery.commit_prepared(ref, context=self.stage_context("commit"))
        self.assertTrue(result["published"])
        nodes = self.graph.state()["nodes"]
        self.assertEqual(len(nodes), 1)
        self.assertEqual(nodes[0]["status"], "untested")
        self.assertEqual(self.reviewer.sources, [self.source_text])
        self.assertTrue(self.reviewer.sources[0].endswith("使用者已经掌握。"))
        formal = list((self.vault / "01-AI技术领域").rglob("*.md"))
        self.assertEqual(len(formal), 1)
        self.assertIn("我理解的可靠处理", formal[0].read_text(encoding="utf-8"))

    def test_commit_requires_review_and_does_not_regenerate_on_replay(self):
        prepared = self.prepare()
        with self.assertRaises(WorkflowError) as missing:
            self.discovery.commit_prepared(prepared["prepared_ref"], context=self.stage_context("commit"))
        self.assertEqual(missing.exception.code, "review_required")
        self.assert_no_published_notes()
        ref = self.approve(prepared)
        first = self.discovery.commit_prepared(ref, context=self.stage_context("commit"))
        replay = self.discovery.commit_prepared(ref, context=self.stage_context("commit"))
        self.assertEqual(first["node_id"], replay["node_id"])
        self.assertEqual(self.processor.calls, 1)
        self.assertEqual(self.reviewer.calls, 1)
        self.assertEqual(len(self.graph.state()["nodes"]), 1)

    def test_unconfigured_reviewer_blocks_even_complete_generation(self):
        self.discovery.note_reviewer = None
        prepared = self.prepare()
        review = self.discovery.review_prepared(prepared["prepared_ref"], context=self.stage_context("review"))
        self.assertEqual(review["verdict"], "blocked")
        with self.assertRaises(WorkflowError) as result:
            self.discovery.commit_prepared(prepared["prepared_ref"], context=self.stage_context("commit"))
        self.assertEqual(result.exception.code, "review_required")
        self.assert_no_published_notes()

    def test_incomplete_generation_never_reaches_reviewer_or_commit(self):
        self.processor.complete = False
        prepared = self.prepare()
        self.assertFalse(self.discovery.validate_prepared(prepared["prepared_ref"], context=self.context)["publishable"])
        self.assertEqual(self.discovery.review_prepared(prepared["prepared_ref"], context=self.context)["verdict"], "blocked")
        self.assertEqual(self.reviewer.calls, 0)
        with self.assertRaises(WorkflowError):
            self.discovery.commit_prepared(prepared["prepared_ref"], context=self.context)
        self.assert_no_published_notes()

    def test_tampered_prepared_envelope_is_rejected(self):
        ref = self.approve(self.prepare())
        path = self.vault / ref
        envelope = json.loads(path.read_text(encoding="utf-8"))
        envelope["processing_result"]["body"] += "\n没有依据的新结论。"
        path.write_text(json.dumps(envelope, ensure_ascii=False), encoding="utf-8")
        with self.assertRaises(WorkflowError) as result:
            self.discovery.commit_prepared(ref, context=self.stage_context("commit"))
        self.assertEqual(result.exception.code, "source_changed")
        self.assert_no_published_notes()

    def test_prepared_reference_from_other_task_is_rejected(self):
        prepared = self.prepare()
        other_id = self.new_task("prepared-other")
        with self.assertRaises(WorkflowError) as result:
            self.discovery.validate_prepared(prepared["prepared_ref"], context=self.stage_context("validate", other_id))
        self.assertEqual(result.exception.http_status, 403)

    def test_revisions_are_limited_to_two_and_require_fresh_approval(self):
        prepared = self.prepare()
        self.reviewer.verdict = "revise"
        for expected_round in (1, 2):
            review = self.discovery.review_prepared(prepared["prepared_ref"], context=self.stage_context("review"))
            self.assertEqual(review["verdict"], "revise")
            prepared = self.discovery.revise_prepared(prepared["prepared_ref"], review,
                                                      context=self.stage_context("review"))
            self.assertEqual(prepared["revision_round"], expected_round)
            with self.assertRaises(WorkflowError):
                self.discovery.commit_prepared(prepared["prepared_ref"], context=self.stage_context("commit"))
        with self.assertRaises(WorkflowError) as limited:
            self.discovery.revise_prepared(prepared["prepared_ref"], {"issues": []}, context=self.context)
        self.assertEqual(limited.exception.code, "review_limit")
        self.assertEqual(self.processor.calls, 3)
        self.assertIsNotNone(self.processor.feedback[-1])
        self.reviewer.verdict = "pass"
        ref = self.approve(prepared)
        self.discovery.commit_prepared(ref, context=self.stage_context("commit"))
        self.assertEqual(self.processor.calls, 3)

    def test_pass_without_full_coverage_is_downgraded(self):
        self.reviewer.complete = False
        prepared = self.prepare()
        review = self.discovery.review_prepared(prepared["prepared_ref"], context=self.stage_context("review"))
        self.assertEqual(review["verdict"], "blocked")
        with self.assertRaises(WorkflowError):
            self.discovery.commit_prepared(prepared["prepared_ref"], context=self.stage_context("commit"))
        self.assert_no_published_notes()

    def test_changed_suggestion_cannot_publish_previously_approved_note(self):
        ref = self.approve(self.prepare())
        suggestions = self.discovery._load_suggestions()
        suggestions[0]["material"]["path"] = "另一个原文.txt"
        self.discovery._save_suggestions(suggestions)
        with self.assertRaises(WorkflowError):
            self.discovery.commit_prepared(ref, context=self.stage_context("commit"))
        self.assert_no_published_notes()
        self.assertEqual(self.processor.calls, 1)

    def test_source_changed_after_review_is_rejected_before_commit(self):
        ref = self.approve(self.prepare())
        self.source_path.write_text("审核后人为替换了来源全文。", encoding="utf-8")
        with self.assertRaises(WorkflowError):
            self.discovery.commit_prepared(ref, context=self.stage_context("commit"))
        self.assert_no_published_notes()


if __name__ == "__main__":
    unittest.main()
