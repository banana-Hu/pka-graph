"""中文显示与稳定章节对齐；离线测试，不调用模型、不写正式库。"""
import unittest
from unittest.mock import patch

from pka_graph.note_processor import (HttpNoteModel, NoteBudget, NoteModelError,
                                      NoteSkillRouter, render_note_sections, split_sections)
from pathlib import Path


class NoteLanguageTests(unittest.TestCase):
    def model(self):
        return HttpNoteModel("https://offline.invalid/v1", "synthetic", "offline", NoteBudget())

    def test_v3_keeps_chinese_heading_separate_from_id_and_original(self):
        source = "# Shipping more CSS\n\nOriginal **performance** evidence."
        sections = split_sections(source)
        for sec in sections:
            sec["points"] = []
        raw = ("标题：把样式工作移到构建期\n结论：我把它理解为计算位置的变化。\n"
               "[小标题：s1]\n构建期与运行时的成本\n[笔记：s1]\n我记住的是成本并未消失，而是改变了位置。")
        model = self.model()
        with patch.object(model, "_chat", return_value=raw):
            result = model.compose_note("规范", "Shipping more CSS", sections, {}, [])
        rendered = render_note_sections(sections, result)
        self.assertEqual(result["section_titles"], {"s1": "构建期与运行时的成本"})
        self.assertEqual(result["duplicate_ids"], [])
        self.assertIn("## 构建期与运行时的成本", rendered["body"])
        self.assertIn(source, rendered["body"])
        self.assertNotIn("## Shipping more CSS", rendered["body"])
        self.assertEqual(rendered["display_warnings"], [])

    def test_chinese_display_title_cannot_change_original(self):
        sec = {"section_id": "s1", "label": "Migration", "text": "# Migration\n`C#` stays unchanged."}
        rendered = render_note_sections([sec], {"conclusion": "我先记下边界。",
            "notes": {"s1": "我理解的是迁移条件。"}, "section_titles": {"s1": "迁移条件"}})
        self.assertIn(sec["text"], rendered["body"])
        self.assertEqual(sec["label"], "Migration")

    def test_legacy_output_still_aligns_with_original_chinese_heading(self):
        model = self.model()
        with patch.object(model, "_chat", return_value="标题：原理\n结论：机制\n[笔记：s1]\n我记下了机制。"):
            result = model.compose_note("规范", "原理", [], {}, [])
        rendered = render_note_sections([{"section_id": "s1", "label": "边界", "text": "原文。"}], result)
        self.assertIn("## 边界", rendered["body"])
        self.assertEqual(rendered["missing_notes"], [])

    def test_missing_or_english_translation_has_visible_fallback_not_original_title(self):
        sections = [{"section_id": "s1", "label": "English heading", "text": "original"}]
        for titles in ({}, {"s1": "Another English heading"}):
            result = render_note_sections(sections, {"conclusion": "结论", "notes": {"s1": "笔记"}, "section_titles": titles})
            self.assertIn("## 第 1 节", result["body"])
            self.assertNotIn("English heading", result["body"])
            self.assertEqual(result["display_warnings"][0]["section_id"], "s1")

    def test_unknown_title_id_is_visible_to_existing_completeness_gate(self):
        rendered = render_note_sections([{"section_id": "s1", "label": "正文", "text": "原文"}],
            {"conclusion": "结论", "notes": {"s1": "笔记"}, "section_titles": {"s999": "未知节"}})
        self.assertEqual(rendered["unknown_ids"], ["s999"])
        self.assertNotIn("未知节", rendered["body"])

    def test_duplicate_heading_does_not_silently_replace_first(self):
        raw = ("标题：原理\n结论：结论\n[小标题：s1]\n原始小标题\n"
               "[小标题：s1]\n第二小标题\n[笔记：s1]\n笔记")
        model = self.model()
        with patch.object(model, "_chat", return_value=raw):
            out = model.compose_note("规范", "原理", [], {}, [])
        self.assertEqual(out["section_titles"]["s1"], "原始小标题")
        self.assertEqual(out["duplicate_ids"], ["s1"])

    def test_english_only_main_title_is_protocol_failure(self):
        model = self.model()
        with patch.object(model, "_chat", return_value="标题：CSS Migration\n结论：结论\n[笔记：s1]\n笔记"):
            with self.assertRaises(NoteModelError):
                model.compose_note("规范", "原理", [], {}, [])

    def test_hashes_in_fenced_code_do_not_create_false_sections(self):
        for fence in ("```", "~~~~"):
            text = "# 机制\n\n" + fence + "python\n#!/usr/bin/env python3\n# parking lot\nprint('ok')\n" + fence + "\n\n# 边界\n结束。"
            sections = split_sections(text)
            self.assertEqual([s["label"] for s in sections], ["机制", "边界"])
            self.assertIn("# parking lot", sections[0]["text"])

    def test_long_original_heading_not_cut_to_forty_chars(self):
        label = "完整原始标题" * 20
        self.assertEqual(split_sections("# " + label + "\n正文")[0]["label"], label)

    def test_unclosed_fence_keeps_remaining_code_in_same_section(self):
        self.assertEqual(len(split_sections("# 机制\n```python\n# comment\n# still code")), 1)

    def test_inline_backticks_do_not_swallow_next_heading(self):
        sections = split_sections("```inline```\n\n## 后续\n正文")
        self.assertEqual([s["label"] for s in sections], ["开头", "后续"])

    def test_protocol_markers_in_code_example_remain_note_text(self):
        raw = ("标题：协议记录\n结论：我记住的是协议边界。\n[笔记：s1]\n"
               "我保留这个代码示例：\n```text\n[小标题：s999]\nexample\n```\n这仍然属于第一节。")
        model = self.model()
        with patch.object(model, "_chat", return_value=raw):
            out = model.compose_note("规范", "原理", [], {}, [])
        self.assertEqual(out["section_titles"], {})
        self.assertIn("[小标题：s999]\nexample\n```\n这仍然属于第一节。", out["notes"]["s1"])

    def test_latest_version_selects_new_prompt_without_replacing_history(self):
        router = NoteSkillRouter(Path(__file__).resolve().parents[1] / "skills" / "note_processing")
        version, kind, text = router.latest(None)
        self.assertEqual(version, "2026-10-05")
        self.assertEqual(kind, "通用")
        self.assertIn("单文件全文处理", text)
        self.assertTrue((router.skills_dir / "2026-10-03-3-通用.md").exists())
        self.assertTrue((router.skills_dir / "2026-10-03-2-通用.md").exists())


if __name__ == "__main__":
    unittest.main()
