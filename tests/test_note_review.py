"""AI 审核协议、原文全覆盖和失败不放行；注入离线响应。"""
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from pka_graph.note_review import HttpNoteReviewer, build_note_reviewer_from_env


class ReviewTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.reviewer = HttpNoteReviewer("https://offline.invalid/v1", "offline", "offline",
                                        skill_text="按原文核对", state_dir=self.root)
        self.source = "持久化状态支持恢复，但需要检查版本一致。"
        self.note = "## 核心结论\n我记住恢复需要持久化状态。"
        self.seen_sources = []

    def tearDown(self):
        self.temp.cleanup()

    def response(self, operation, payload):
        if operation == "claims":
            return {"claims": [{"claim": "恢复需要持久化状态。", "note_excerpt": "恢复需要持久化状态。"}]}
        if operation == "ground":
            self.seen_sources.append(payload["source"])
            present = "持久化状态支持恢复" in payload["source"]
            return {"findings": [{"claim_id": claim["claim_id"],
                    "status": "supported" if present else "not_in_segment",
                    "source_excerpt": "持久化状态支持恢复" if present else "", "reason": ""}
                    for claim in payload["claims"]], "omissions": []}
        if operation == "style":
            return {"verdict": "pass", "issues": []}
        if operation == "context":
            return {"summary": "恢复需要持久化状态。"}
        raise AssertionError(operation)

    def review(self, response=None, source=None):
        with patch.object(self.reviewer, "_chat", side_effect=response or self.response):
            return self.reviewer.review(source or self.source, self.note, "中文个人视角、结论先行")

    def test_evidence_and_style_pass(self):
        result = self.review()
        self.assertEqual(result["verdict"], "pass")
        self.assertTrue(result["coverage"]["complete"])
        self.assertEqual(result["issues"], [])

    def test_tail_beyond_5000_actually_reviewed(self):
        source = "前面是不相关材料。" * 1000 + self.source
        result = self.review(source=source)
        self.assertEqual(result["verdict"], "pass")
        self.assertEqual("".join(self.seen_sources), source)
        self.assertGreater(result["coverage"]["segments"], 1)

    def test_unlocated_source_quote_blocks(self):
        def response(operation, payload):
            result = self.response(operation, payload)
            if operation == "ground":
                result["findings"][0]["source_excerpt"] = "原文根本没有写这个"
            return result
        result = self.review(response)
        self.assertEqual(result["verdict"], "blocked")
        self.assertEqual(result["error_category"], "unlocated_excerpt")

    def test_unlocated_note_quote_blocks(self):
        def response(operation, payload):
            result = self.response(operation, payload)
            if operation == "claims":
                result["claims"][0]["note_excerpt"] = "不存在的笔记文本"
            return result
        self.assertEqual(self.review(response)["verdict"], "blocked")

    def test_unsupported_claim_revises(self):
        result = self.review(source="这里完全没有恢复或持久化依据。")
        self.assertEqual(result["verdict"], "revise")
        self.assertIn("未找到", result["issues"][0]["reason"])

    def test_missing_grounding_blocks(self):
        def response(operation, payload):
            result = self.response(operation, payload)
            if operation == "ground":
                result["findings"] = []
            return result
        self.assertEqual(self.review(response)["verdict"], "blocked")

    def test_case_omission_not_hardcoded_failure(self):
        self.assertEqual(self.review(source=self.source + "原作者举了一个很长的案例。")["verdict"], "pass")

    def test_style_violation_exact_evidence(self):
        def response(operation, payload):
            if operation == "style":
                return {"verdict": "revise", "issues": [{"reason": "措辞需要修订", "note_excerpt": "我记住"}]}
            return self.response(operation, payload)
        result = self.review(response)
        self.assertEqual(result["verdict"], "revise")
        self.assertEqual(result["issues"][0]["note_excerpt"], "我记住")

    def test_no_config_has_no_fake_reviewer(self):
        with patch.dict(os.environ, {}, clear=True):
            reviewer, reason = build_note_reviewer_from_env(self.root, self.root)
        self.assertIsNone(reviewer)
        self.assertIn("未配置", reason)

    def test_note_provider_fallback_separate_role(self):
        skill = self.root / "note_review" / "SKILL.md"
        skill.parent.mkdir()
        skill.write_text("审核规范", encoding="utf-8")
        env = {"PKA_NOTE_ADAPTER": "http", "PKA_NOTE_API_BASE": "https://offline.invalid/v1",
               "PKA_NOTE_API_KEY": "synthetic", "PKA_NOTE_MODEL": "offline"}
        with patch.dict(os.environ, env, clear=True):
            reviewer, reason = build_note_reviewer_from_env(self.root, self.root)
        self.assertIsInstance(reviewer, HttpNoteReviewer)
        self.assertIn("自动审核已配置", reason)

    def test_explicit_unknown_mode_not_fallback(self):
        with patch.dict(os.environ, {"PKA_REVIEW_ADAPTER": "fake", "PKA_NOTE_ADAPTER": "http"}, clear=True):
            reviewer, reason = build_note_reviewer_from_env(self.root, self.root)
        self.assertIsNone(reviewer)
        self.assertIn("不降级", reason)

    def test_usage_records_attempts_without_key(self):
        self.reviewer._record_attempt()
        self.reviewer._record_attempt()
        record = (self.root / "review_budget.json").read_text(encoding="utf-8")
        self.assertIn('"attempts": 2', record)
        self.assertNotIn("offline", record)


if __name__ == "__main__":
    unittest.main()
