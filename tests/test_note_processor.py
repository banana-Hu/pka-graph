"""笔记加工（模块二）测试：Skill 路由、分段、汇总、未读完标注、accept 集成。

全部使用临时库与确定性假模型，不触碰真实知识库、不调用真实模型。
"""
import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tests"))

from fixtures import make_test_vault
from pka_graph.discovery import DiscoveryService
from pka_graph.note_processor import (NoteBudget, NoteModelError, NoteProcessor,
                                      NoteSkillRouter, segment_material)
from pka_graph.service import GraphService
from pka_graph.store import StateStore

SKILL_TEXT_V1 = "通用规范 v1：总分结构。"
SKILL_TEXT_V2 = "通用规范 v2：中文标题优先。"


def fixed_clock(y=2026, mo=10, d=3):
    from datetime import datetime, timezone, timedelta

    class _Clock:
        def __call__(self):
            return datetime(y, mo, d, tzinfo=timezone.utc)

        def isoformat(self, timespec="seconds"):
            return self().isoformat(timespec=timespec)

        def date(self):
            return self().date()

    return _Clock()


class FakeNoteModel:
    """确定性假加工模型：记录调用；可注入失败。"""

    def __init__(self, *, fail_segments=False, fail_compose=False,
                 compose_protocol_violation=False, omit_labels=()):
        self.fail_segments = fail_segments
        self.fail_compose = fail_compose
        self.compose_protocol_violation = compose_protocol_violation
        self.omit_labels = set(omit_labels)
        self.calls = []
        self.segment_texts = []

    def describe(self):
        return "假加工模型（确定性）"

    def extract_segment_points(self, skill_text, label, segment_text):
        if self.fail_segments:
            raise NoteModelError("模型服务连接超时", category="timeout")
        self.calls.append(("seg", label, len(segment_text)))
        self.segment_texts.append(segment_text)
        return {"points": [{"point": f"{label}要点", "excerpt": segment_text[:20]}],
                "section_summary": f"{label}概要"}

    def compose_note(self, skill_text, original_title, section_points, suggestion_ctx, related):
        self.calls.append(("compose", original_title, len(skill_text)))
        if self.compose_protocol_violation:
            # 模拟真实适配器的协议解析失败（真实模型输出缺「标题：」首行时同样如此）
            raise NoteModelError("加工输出缺少「标题：」首行，违反输出协议", category="parse")
        if self.fail_compose:
            raise NoteModelError("模型服务返回 HTTP 500（详情不转发）", category="http_5xx")
        notes = {p["section_id"]: f"第一人称笔记（{p['label']}）：结论先行，机制与边界随后。"
                 for p in section_points
                 if not self.omit_labels or p["section_id"] not in self.omit_labels}
        return {"title": "增量索引水位线的中文标题",
                "conclusion": "这是核心结论（顶部先行）。",
                "notes": notes}


def make_processor(model, skills_dir: Path, vault: Path, **budget_attrs) -> NoteProcessor:
    budget = NoteBudget()
    for k, v in budget_attrs.items():
        setattr(budget, k, v)
    return NoteProcessor(model, budget, skills_dir, vault / "个人学习状态",
                         clock=fixed_clock())


LONG_TEXT = ("# 水位线基础\n\n" + "水位线用于记录流水线已处理位置，重启后从中恢复。" * 120
             + "\n\n# 持久化要求\n\n" + "水位线必须持久化，否则重启即失去意义。" * 120)


def make_suggestion(vault: Path, *, name: str = "incremental-index-watermark-en.md",
                    full_text: str = LONG_TEXT, domain: str = "AI技术") -> dict:
    rel = "个人学习状态/inbox/incremental-index-watermark-en.md"
    p = vault / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(full_text, encoding="utf-8")
    return {
        "id": "sug-proc-0001", "created_at": "2026-10-03T00:00:00+00:00",
        "status": "pending", "verdict": "new_node",
        "name": name,
        "contribution": {"problem": "重复扫描浪费", "claims": ["水位线持久化"],
                          "differences": [], "conditions_limits": ["不适用实时外部状态"]},
        "related": [{"node_id": "n-seed", "name": "检查点恢复", "scope": "恢复语义",
                     "score": 0.2, "domain": domain}],
        "sources": [{"upstream_id": "inbox:abc123def456", "source_name": "收件箱",
                     "url": None, "path": rel, "read_status": "readable",
                     "published_at": None}],
        "material": {"level": "full_text", "text": "只有一小段片段"},
        "reason": "测试建议", "novelty_label": "来源声称的新贡献（未经独立核实）",
        "novelty": {"first_collected_at": "2026-10-03T00:00:00+00:00"},
    }


class RouterTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.skills = Path(self._tmp.name)
        (self.skills / "2026-10-01-通用.md").write_text(SKILL_TEXT_V1, encoding="utf-8")
        (self.skills / "2026-10-03-通用.md").write_text(SKILL_TEXT_V2, encoding="utf-8")

    def tearDown(self):
        self._tmp.cleanup()

    def test_latest_version_wins(self):
        version, kind, text = NoteSkillRouter(self.skills).latest(None)
        self.assertEqual((version, kind), ("2026-10-03", "通用"))
        self.assertEqual(text, SKILL_TEXT_V2)

    def test_domain_skill_preferred_then_fallback(self):
        (self.skills / "2026-10-02-AI技术.md").write_text("AI 领域规范", encoding="utf-8")
        router = NoteSkillRouter(self.skills)
        self.assertEqual(router.latest("AI技术")[:2], ("2026-10-02", "AI技术"))
        self.assertEqual(router.latest("产品")[:2], ("2026-10-03", "通用"))  # 领域缺失回退

    def test_missing_skill_raises(self):
        empty = Path(self._tmp.name) / "空"
        empty.mkdir()
        with self.assertRaises(NoteModelError) as ctx:
            NoteSkillRouter(empty).latest(None)
        self.assertEqual(ctx.exception.category, "skill_missing")


class SegmentTests(unittest.TestCase):
    def test_headings_split_and_pack(self):
        segments, overflow = segment_material(LONG_TEXT, max_chars=500, max_segments=20)
        self.assertGreaterEqual(len(segments), 2)
        self.assertEqual(overflow, [])
        self.assertTrue(all(len(s["text"]) <= 500 for s in segments))
        labels = " | ".join(s["label"] for s in segments)
        self.assertIn("水位线基础", labels)
        self.assertIn("持久化要求", labels)   # 不再只读开头

    def test_overflow_recorded_not_silently_dropped(self):
        segments, overflow = segment_material(LONG_TEXT, max_chars=500, max_segments=1)
        self.assertEqual(len(segments), 1)
        self.assertGreaterEqual(len(overflow), 1)
        self.assertIn("chars", overflow[0])


class ProcessTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.vault = make_test_vault(Path(self._tmp.name))
        self.skills = Path(self._tmp.name) / "skills"
        self.skills.mkdir()
        (self.skills / "2026-10-03-通用.md").write_text(SKILL_TEXT_V2, encoding="utf-8")

    def tearDown(self):
        self._tmp.cleanup()

    def test_full_text_processed_not_fragment(self):
        """加工读取来源原文全文（不是建议里截断的片段）。"""
        model = FakeNoteModel()
        proc = make_processor(model, self.skills, self.vault)
        sug = make_suggestion(self.vault)
        result = proc.process(suggestion=sug, vault_root=self.vault)
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["title"], "增量索引水位线的中文标题")
        self.assertEqual(result["skill_version"], "2026-10-03")
        self.assertTrue(result["complete"])
        self.assertGreater(len(model.segment_texts[0]), 1000)   # 全文进模型
        self.assertEqual(result["segments"]["processed"] + len(result["segments"]["unprocessed"]),
                         result["segments"]["total"])

    def test_segment_failure_marks_unprocessed_and_still_composes(self):
        """单段提取失败 → 记入未处理并照常汇总，笔记带未读完标注。"""
        model = FakeNoteModel(fail_segments=True)
        proc = make_processor(model, self.skills, self.vault, segment_max_chars=500)
        sug = make_suggestion(self.vault)
        result = proc.process(suggestion=sug, vault_root=self.vault)
        self.assertFalse(result["ok"])   # 全部段失败 → 无法成文
        self.assertIn("提取失败", result["reason"] or "" if isinstance(result["reason"], str) else "")

        # 只失败一段：用按标签失败的可控模型
        class Partial(FakeNoteModel):
            def extract_segment_points(self, skill_text, label, segment_text):
                if "持久化" in label:
                    raise NoteModelError("超时", category="timeout")
                return super().extract_segment_points(skill_text, label, segment_text)

        proc2 = make_processor(Partial(), self.skills, self.vault, segment_max_chars=500)
        result2 = proc2.process(suggestion=make_suggestion(self.vault), vault_root=self.vault)
        self.assertTrue(result2["ok"])
        self.assertFalse(result2["complete"])
        self.assertFalse(result2["publishable"])   # 部分处理不算完整成功
        self.assertTrue(any("持久化" in u for u in result2["segments"]["unprocessed"]))
        self.assertIn("未处理", result2["processed_range"])   # 处理范围如实记录

    def test_compose_omits_section_note_marks_missing(self):
        """模型漏写某节笔记 → 缺节标注，不假装完整。"""
        import re as _re
        label_re = _re.compile(r"水位线基础")
        class Omitter(FakeNoteModel):
            def compose_note(self, skill_text, original_title, section_points,
                             suggestion_ctx, related):
                out = super().compose_note(skill_text, original_title, section_points,
                                           suggestion_ctx, related)
                drop = {p["section_id"] for p in section_points if label_re.search(p["label"])}
                out["notes"] = {k: v for k, v in out["notes"].items() if k not in drop}
                return out

        proc = make_processor(Omitter(), self.skills, self.vault, segment_max_chars=500)
        result = proc.process(suggestion=make_suggestion(self.vault), vault_root=self.vault)
        self.assertTrue(result["ok"])
        self.assertFalse(result["complete"])
        self.assertFalse(result["publishable"])
        self.assertGreaterEqual(len(result["segments"]["missing_notes"]), 1)
        self.assertIn("缺节标注", result["body"])

    def test_overflow_yields_incomplete_note(self):
        """段数超上限 → 产出笔记但如实标注未处理部分。"""
        proc = make_processor(FakeNoteModel(), self.skills, self.vault,
                              segment_max_chars=500, per_note_calls_cap=2)
        result = proc.process(suggestion=make_suggestion(self.vault), vault_root=self.vault)
        self.assertTrue(result["ok"])
        self.assertFalse(result["complete"])
        self.assertFalse(result["publishable"])
        self.assertGreaterEqual(len(result["segments"]["unprocessed"]), 1)
        self.assertIn("未处理", result["processed_range"])

    def test_daily_budget_exhausted_reports_honestly(self):
        budget = NoteBudget()
        budget.daily_model_calls_limit = 1
        budget.segment_max_chars = 500
        proc = NoteProcessor(FakeNoteModel(), budget, self.skills,
                             self.vault / "个人学习状态", clock=fixed_clock())
        proc.reserve_attempt()   # 模拟当日预算已用尽（used=1 ≥ daily=1）
        result = proc.process(suggestion=make_suggestion(self.vault), vault_root=self.vault)
        self.assertFalse(result["ok"])
        self.assertIn("预算", result["reason"])
        self.assertEqual(result["calls"], 0)

    def test_compose_protocol_violation_is_failure(self):
        """汇总输出违反协议（缺「标题：」首行）→ 判失败，不假装成功。"""
        model = FakeNoteModel(compose_protocol_violation=True)
        proc = make_processor(model, self.skills, self.vault)
        result = proc.process(suggestion=make_suggestion(self.vault), vault_root=self.vault)
        self.assertFalse(result["ok"])
        self.assertIn("汇总", result["reason"])

    def test_source_file_missing_falls_back_to_fragment(self):
        """原文文件被删 → 用建议片段加工并如实标注来源状态（acquired=False）。"""
        model = FakeNoteModel()
        proc = make_processor(model, self.skills, self.vault)
        sug = make_suggestion(self.vault)
        (self.vault / sug["sources"][0]["path"]).unlink()
        result = proc.process(suggestion=sug, vault_root=self.vault)
        self.assertTrue(result["ok"])
        self.assertFalse(result["publishable"])   # 片段级材料不达正式入库门槛
        self.assertEqual(result["source"]["scope"], "fragment")
        self.assertIn("片段", result["source"]["note"])
        self.assertTrue(all(len(t) < 100 for t in model.segment_texts))


class AcceptIntegrationTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.vault = make_test_vault(Path(self._tmp.name))
        self.gs = GraphService(StateStore(self.vault))
        self.skills = Path(self._tmp.name) / "skills"
        self.skills.mkdir()
        (self.skills / "2026-10-03-通用.md").write_text(SKILL_TEXT_V2, encoding="utf-8")
        self.disc = DiscoveryService(self.gs, upstream=[],
                                     note_processor=make_processor(
                                         FakeNoteModel(), self.skills, self.vault))

    def tearDown(self):
        self._tmp.cleanup()

    def _suggestion(self):
        return make_suggestion(self.vault)

    def test_accept_creates_chinese_note_with_skill_record(self):
        sug = self._suggestion()
        self.disc._save_suggestions([sug])
        out = self.disc.accept(None, sug["id"], edits={})
        node = self.gs.node_detail(out["node_id"])["node"]
        self.assertEqual(node["status"], "untested")            # 加工不等于掌握
        self.assertEqual(node["name"], "增量索引水位线的中文标题")   # 中文标题进节点名
        notes = [p for p in self.vault.rglob("*.md")
                 if p.name.startswith("增量索引水位线的中文标题")]
        self.assertEqual(len(notes), 1)
        text = notes[0].read_text(encoding="utf-8")
        self.assertIn('processor_version: "note-skill-2026-10-03"', text)
        self.assertIn('note_skill: "通用/2026-10-03"', text)   # 领域版缺失 → 回退通用
        self.assertIn('original_title: "incremental-index-watermark-en.md"', text)
        self.assertIn("## 核心结论", text)
        self.assertNotIn("**原文**", text)  # 新成品保留精华，原件通过来源单独对照。
        self.assertIn("第一人称笔记", text)
        self.assertLess(text.index("## 核心结论"), text.index("## 水位线基础"))
        self.assertTrue(any("持久化" in part for part in self.disc.note_processor.model.segment_texts))
        saved = self.disc._load_suggestions()[0]
        self.assertEqual(saved["note_processing"]["status"], "ok")
        self.assertEqual(saved["note_processing"]["skill_version"], "2026-10-03")

    def test_accept_fails_processing_leaves_clue_not_formal(self):
        """加工失败 + 人工接受 → 线索笔记（候选目录），不建节点不进正式区。"""
        model = FakeNoteModel(fail_segments=True)
        sug = self._suggestion()
        disc = DiscoveryService(self.gs, upstream=[],
                                note_processor=make_processor(model, self.skills, self.vault))
        disc._save_suggestions([sug])
        out = disc.accept(None, sug["id"], edits={})
        self.assertFalse(out["published"])
        self.assertIn("clue", out)
        notes = [p for p in self.vault.rglob("*.md") if p.name.startswith("线索-")]
        self.assertEqual(len(notes), 1)
        text = notes[0].read_text(encoding="utf-8")
        self.assertIn("status: candidate", text)
        self.assertIn("未通过内容准入", text)
        saved = disc._load_suggestions()[0]
        self.assertEqual(saved["status"], "accepted")
        self.assertEqual(saved["accepted"]["as"], "clue")
        self.assertEqual(saved["note_processing"]["status"], "failed")
        # 未创建节点（失败分支不得产生误导性正式节点）
        self.assertFalse(any(n["name"] == "增量索引水位线的中文标题" or n["name"] == sug["name"]
                             for n in self.gs.state()["nodes"]))

    def test_explicit_edits_name_wins_over_generated_title(self):
        sug = self._suggestion()
        self.disc._save_suggestions([sug])
        self.disc.accept(None, sug["id"], edits={"name": "人工指定的标题"})
        node = [n for n in self.gs.state()["nodes"] if n["name"] == "人工指定的标题"]
        self.assertEqual(len(node), 1)


if __name__ == "__main__":
    unittest.main()
