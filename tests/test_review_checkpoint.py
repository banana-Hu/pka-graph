"""Resume independent AI review from validated replies; synthetic calls only."""

import copy
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from pka_graph.note_review import HttpNoteReviewer, NoteReviewError


class CheckpointContext:
    def __init__(self):
        self.checkpoint = {"another_stage": {"preserved": True}}
        self.cancelled = False

    def load_checkpoint(self):
        return copy.deepcopy(self.checkpoint)

    def save_checkpoint(self, data):
        self.checkpoint = copy.deepcopy(data)

    def cancel_requested(self):
        return self.cancelled

    def report_progress(self, *args):
        pass


class LegacyContext:
    def cancel_requested(self):
        return False

    def report_progress(self, *args):
        pass


class ReviewCheckpointTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.reviewer = self.make_reviewer()
        self.source = "持久化状态支持恢复。" + "甲" * 490 + "乙" * 500 + "丙" * 500
        self.note = "## 核心结论\n我理解恢复需要持久化状态。"
        self.skill = "中文、个人理解、结论先行"
        self.context = CheckpointContext()
        self.calls = []

    def make_reviewer(self):
        return HttpNoteReviewer("https://offline.invalid/v1", "synthetic", "offline-model",
                                skill_text="逐字证据核验", state_dir=self.root,
                                segment_chars=500, note_segment_chars=500)

    def response(self, operation, payload):
        self.calls.append((operation, copy.deepcopy(payload)))
        if operation == "claims":
            return {"claims": [{"claim": "恢复需要持久化状态。",
                                "note_excerpt": payload["note"][:12]}]}
        if operation == "context":
            return {"summary": "恢复需要持久化状态。"}
        if operation == "ground":
            supported = "持久化状态支持恢复。" in payload["source"]
            return {"findings": [{"claim_id": c["claim_id"],
                    "status": "supported" if supported else "not_in_segment",
                    "source_excerpt": "持久化状态支持恢复。" if supported else "",
                    "reason": ""} for c in payload["claims"]], "omissions": []}
        if operation == "style":
            return {"verdict": "pass", "issues": []}
        raise AssertionError(operation)

    def review(self, response=None, *, reviewer=None, source=None, note=None,
               skill=None, context=None):
        reviewer = reviewer or self.reviewer
        with patch.object(reviewer, "_chat", side_effect=response or self.response):
            return reviewer.review(source or self.source, note or self.note, skill or self.skill,
                                   task_context=context or self.context)

    def test_network_failure_resumes_without_repeating_earlier_requests(self):
        grounds = []
        def interrupted(operation, payload):
            if operation == "ground":
                grounds.append(payload["source"])
                if len(grounds) == 2:
                    self.calls.append((operation, copy.deepcopy(payload)))
                    raise NoteReviewError("模拟网络中断", category="network")
            return self.response(operation, payload)
        first = self.review(interrupted)
        self.assertEqual(first["verdict"], "blocked")
        self.assertEqual(first["error_category"], "network")
        saved = copy.deepcopy(self.context.checkpoint)
        count_before = len(self.calls)
        second = self.review(reviewer=self.make_reviewer())
        self.assertEqual(second["verdict"], "pass")
        self.assertEqual(sum(op == "claims" for op, _ in self.calls), 1)
        first_source = self.reviewer._parts(self.source, 500)[0]
        self.assertEqual(sum(op == "ground" and p["source"] == first_source
                             for op, p in self.calls), 1)
        self.assertLess(len(self.calls) - count_before, len(self.reviewer._parts(self.source, 500)) + 2)
        self.assertTrue(saved["note_review"]["responses"])
        self.assertEqual(self.context.checkpoint["another_stage"], {"preserved": True})

    def test_changed_source_note_skills_model_or_endpoint_does_not_reuse_replies(self):
        self.assertEqual(self.review()["verdict"], "pass")
        original_checkpoint = copy.deepcopy(self.context.checkpoint)
        for field in ("source", "note", "processing_skill", "review_skill", "model", "endpoint"):
            with self.subTest(field=field):
                self.context.checkpoint = copy.deepcopy(original_checkpoint)
                reviewer = self.make_reviewer()
                options = {"reviewer": reviewer}
                if field == "source":
                    options["source"] = self.source + "追加原文"
                elif field == "note":
                    options["note"] = self.note + "补充理解"
                elif field == "processing_skill":
                    options["skill"] = self.skill + "新的边界"
                elif field == "review_skill":
                    reviewer.skill_text += "新审核规则"
                elif field == "model":
                    reviewer.model = "other-model"
                elif field == "endpoint":
                    reviewer._transport.api_base = "https://other.invalid/v1"
                before = len(self.calls)
                result = self.review(**options)
                self.assertEqual(result["verdict"], "pass")
                self.assertTrue(any(op == "claims" for op, _ in self.calls[before:]))
                self.assertNotEqual(self.context.checkpoint["note_review"]["binding"],
                                    original_checkpoint["note_review"]["binding"])

    def test_schema_failure_is_not_cached_and_can_be_corrected(self):
        def bad(operation, payload):
            response = self.response(operation, payload)
            if operation == "ground":
                response["findings"] = "不是列表"
            return response
        first = self.review(bad)
        self.assertEqual(first["error_category"], "parse")
        cached = self.context.checkpoint["note_review"]["responses"].values()
        self.assertFalse(any("findings" in response for response in cached))
        self.assertEqual(self.review()["verdict"], "pass")
        self.assertEqual(sum(op == "claims" for op, _ in self.calls), 1)

    def test_unlocated_evidence_is_not_cached_and_can_be_corrected(self):
        def bad(operation, payload):
            response = self.response(operation, payload)
            if operation == "ground" and response["findings"][0]["status"] == "supported":
                response["findings"][0]["source_excerpt"] = "原文没有的引文"
            return response
        self.assertEqual(self.review(bad)["error_category"], "unlocated_excerpt")
        self.assertEqual(len(self.context.checkpoint["note_review"]["responses"]), 1)
        self.assertEqual(self.review()["verdict"], "pass")
        self.assertEqual(sum(op == "claims" for op, _ in self.calls), 1)

    def test_bad_cached_reply_is_evicted_instead_of_failing_forever(self):
        self.assertEqual(self.review()["verdict"], "pass")
        responses = self.context.checkpoint["note_review"]["responses"]
        damaged_key = next(key for key, response in responses.items()
                           if response.get("findings")
                           and response["findings"][0]["status"] == "supported")
        responses[damaged_key]["findings"][0]["source_excerpt"] = "不在原文"
        self.assertEqual(self.review()["error_category"], "unlocated_excerpt")
        self.assertNotIn(damaged_key, self.context.checkpoint["note_review"]["responses"])
        before = len(self.calls)
        self.assertEqual(self.review()["verdict"], "pass")
        self.assertEqual(len(self.calls) - before, 1)

    def test_cancel_keeps_prior_validated_replies(self):
        grounds = []
        def cancelled(operation, payload):
            response = self.response(operation, payload)
            if operation == "ground":
                grounds.append(payload["source"])
                if len(grounds) == 2:
                    self.context.cancelled = True
            return response
        result = self.review(cancelled)
        self.assertEqual(result["error_category"], "cancelled")
        self.assertEqual(len(self.context.checkpoint["note_review"]["responses"]), 2)
        self.context.cancelled = False
        self.assertEqual(self.review()["verdict"], "pass")
        self.assertEqual(sum(op == "claims" for op, _ in self.calls), 1)

    def test_context_summary_and_completed_style_responses_are_reused(self):
        long_note = "我理解恢复的机制。" * 65
        def long_claims(operation, payload):
            if operation == "claims":
                self.calls.append((operation, copy.deepcopy(payload)))
                return {"claims": [{"claim": "恢复需要持久化状态。" * 65,
                                    "note_excerpt": payload["note"][:10]}]}
            if operation == "style" and not getattr(self, "failed_style_once", False):
                self.failed_style_once = True
                self.calls.append((operation, copy.deepcopy(payload)))
                raise NoteReviewError("模拟样式请求中断", category="network")
            return self.response(operation, payload)
        first = self.review(long_claims, note=long_note)
        self.assertEqual(first["error_category"], "network")
        self.assertTrue(any(op == "context" for op, _ in self.calls))
        counts = {op: sum(current == op for current, _ in self.calls)
                  for op in ("claims", "context", "ground")}
        second = self.review(long_claims, note=long_note)
        self.assertEqual(second["verdict"], "pass")
        for op, expected in counts.items():
            self.assertEqual(sum(current == op for current, _ in self.calls), expected)
        before = len(self.calls)
        self.assertEqual(self.review(long_claims, note=long_note)["verdict"], "pass")
        self.assertEqual(len(self.calls), before)

    def test_legacy_context_without_checkpoint_methods_keeps_previous_behavior(self):
        legacy = LegacyContext()
        self.assertEqual(self.review(context=legacy)["verdict"], "pass")
        before = len(self.calls)
        self.assertEqual(self.review(context=legacy)["verdict"], "pass")
        self.assertEqual(len(self.calls), before * 2)

    def test_corrupt_checkpoint_is_not_overwritten(self):
        self.context.checkpoint["note_review"] = []
        original = copy.deepcopy(self.context.checkpoint)
        result = self.review()
        self.assertEqual(result["error_category"], "checkpoint_corrupt")
        self.assertEqual(self.context.checkpoint, original)
        self.assertEqual(self.calls, [])

