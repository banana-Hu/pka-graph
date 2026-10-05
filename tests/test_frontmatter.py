"""受限 front-matter 解析器测试（必测 16 的"异常格式被拒绝"部分）。"""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from pka_graph.frontmatter import FrontmatterError, split_frontmatter

VALID = """---
id: note-20260926-001
status: accepted
title: "带引号的标题"
domain: "AI技术领域"
evidence_ids:
  - "a.pdf#page=1"
  - "b.pdf#page=2"
related_notes:
---

正文内容。
"""


class FrontmatterTests(unittest.TestCase):
    def test_valid_note_parses(self):
        meta, body = split_frontmatter(VALID)
        self.assertEqual(meta["id"], "note-20260926-001")
        self.assertEqual(meta["status"], "accepted")
        self.assertEqual(meta["title"], "带引号的标题")
        self.assertEqual(meta["evidence_ids"], ["a.pdf#page=1", "b.pdf#page=2"])
        self.assertEqual(meta["related_notes"], [])
        self.assertIn("正文内容", body)

    def test_no_frontmatter_returns_empty(self):
        meta, body = split_frontmatter("# 纯正文\n没有元数据")
        self.assertEqual(meta, {})
        self.assertIn("纯正文", body)

    def test_unclosed_rejected(self):
        with self.assertRaises(FrontmatterError):
            split_frontmatter("---\nid: x\n没有结束线")

    def test_inline_flat_list_parses(self):
        """真实库书单类笔记使用单行内联扁平列表，必须可靠支持。"""
        meta, _ = split_frontmatter("---\nevidence_ids: [ev-a-p8, ev-a-p32]\nrelated_notes: []\n---\n正文")
        self.assertEqual(meta["evidence_ids"], ["ev-a-p8", "ev-a-p32"])
        self.assertEqual(meta["related_notes"], [])
        meta2, _ = split_frontmatter('---\nnames: ["带引号, 引号内逗号不切分", plain]\n---\n正文')
        self.assertEqual(meta2["names"], ["带引号, 引号内逗号不切分", "plain"])

    def test_inline_nested_rejected(self):
        with self.assertRaises(FrontmatterError):
            split_frontmatter("---\ntags: [[a], [b]]\n---\n正文")

    def test_inline_unclosed_quote_rejected(self):
        with self.assertRaises(FrontmatterError):
            split_frontmatter('---\ntags: ["未闭合]\n---\n正文')

    def test_half_open_collection_rejected(self):
        with self.assertRaises(FrontmatterError):
            split_frontmatter("---\ntags: [a, b\n---\n正文")

    def test_missing_colon_rejected(self):
        with self.assertRaises(FrontmatterError):
            split_frontmatter("---\n这行没有冒号\n---\n正文")

    def test_duplicate_key_rejected(self):
        with self.assertRaises(FrontmatterError):
            split_frontmatter("---\nid: a\nid: b\n---\n正文")

    def test_tab_indent_rejected(self):
        with self.assertRaises(FrontmatterError):
            split_frontmatter("---\nitems:\n\t- a\n---\n正文")


if __name__ == "__main__":
    unittest.main()
