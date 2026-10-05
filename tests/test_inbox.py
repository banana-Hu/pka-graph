"""收件箱监听测试：检测、去重、半写保护、格式过滤、PDF、无模型、预算。

全部使用临时库与合成材料，不触碰真实知识库；模型用确定性假实现。
"""
import hashlib
import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tests"))

from fixtures import make_test_vault
from pka_graph.inbox import InboxWatcher, read_pdf_text
from pka_graph.service import GraphService
from pka_graph.store import StateStore

LONG_MD = """---
title: "合成材料：版本指纹缓存"
---

# 版本指纹缓存

为避免重复计算，把输入内容与配置版本一起计算哈希作为缓存键，命中则复用结果。
配置变化必须使缓存失效；该方法不适合依赖实时外部状态的任务，因为外部数据
变化时缓存键不变却结果已过期。作者主张这是最小可用的失效策略。
"""


class FakeAnalyzer:
    """确定性假分析器：直接返回固定分类，用于验证链路而非判定质量。"""

    def __init__(self, classification="new_node"):
        self.classification = classification
        self.calls = 0
        self.last_material = None

    def describe(self) -> str:
        return "测试假分析器（确定性）"

    def analyze(self, material, sources, related, note_fragments):
        self.calls += 1
        self.last_material = material
        excerpt = material["text"][:20]
        return {
            "classification": self.classification,
            "contribution": {"problem": None, "claims": [excerpt],
                             "differences": [], "conditions_limits": []},
            "differences_vs_related": [],
            "source_excerpts": [{"upstream_id": sources[0]["upstream_id"], "excerpt": excerpt}],
            "unknowns": [],
        }


class BudgetStub:
    def __init__(self, limit):
        self.daily_model_calls_limit = limit


class InboxTestBase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.vault = make_test_vault(Path(self._tmp.name))
        self.store = StateStore(self.vault)
        self.gs = GraphService(self.store)
        self.analyzer = FakeAnalyzer()
        self.watcher = InboxWatcher(self.gs, analyzer=self.analyzer, analyzer_budget=BudgetStub(10))

    def tearDown(self):
        self._tmp.cleanup()

    def drop(self, name: str, content: str) -> Path:
        self.watcher.inbox_dir.mkdir(parents=True, exist_ok=True)
        p = self.watcher.inbox_dir / name
        p.write_text(content, encoding="utf-8")
        return p

    def suggestions(self):
        from pka_graph.discovery import DiscoveryService

        return DiscoveryService(self.gs, upstream=[])._load_suggestions()

    def scan_twice(self):
        """稳定性检测需要连续两次扫描大小不变。"""
        self.watcher.scan_once()
        return self.watcher.scan_once()


class DetectionTests(InboxTestBase):
    def test_new_markdown_creates_suggestion(self):
        self.drop("材料.md", LONG_MD)
        summary = self.scan_twice()
        self.assertEqual(summary["suggested"], 1)
        self.assertEqual(self.analyzer.calls, 1)
        sug = self.suggestions()
        self.assertEqual(len(sug), 1)
        self.assertEqual(sug[0]["verdict"], "new_node")
        self.assertTrue(sug[0]["id"].startswith("sug-inbox-"))
        self.assertIn("inbox", sug[0]["sources"][0]["path"])

    def test_same_content_not_analyzed_twice(self):
        self.drop("材料.md", LONG_MD)
        self.scan_twice()
        self.drop("副本改名.md", LONG_MD)   # 同内容不同名
        summary = self.scan_twice()
        self.assertEqual(self.analyzer.calls, 1)      # 只分析一次
        self.assertGreaterEqual(summary.get("skipped", 0), 1)
        self.assertEqual(len(self.suggestions()), 1)

    def test_half_written_file_not_processed(self):
        """第一次扫描时文件还在写（大小变化）→ 不处理；稳定后才处理。"""
        p = self.drop("半写.md", LONG_MD[:60])
        self.watcher.scan_once()           # 第 1 次：记录大小
        p.write_text(LONG_MD, encoding="utf-8")  # 变大（模拟仍在写）
        summary = self.watcher.scan_once()  # 第 2 次：大小变了，仍不处理
        self.assertEqual(summary["suggested"], 0)
        self.assertEqual(self.analyzer.calls, 0)
        summary = self.watcher.scan_once()  # 第 3 次：连续两次一致 → 处理
        self.assertEqual(summary["suggested"], 1)

    def test_underscore_and_dot_files_skipped(self):
        """说明类文件（_ 或 . 前缀）不当材料处理。"""
        self.drop("_说明.txt", LONG_MD)
        self.drop(".hidden.md", LONG_MD)
        summary = self.scan_twice()
        self.assertEqual(summary["suggested"], 0)
        self.assertEqual(self.analyzer.calls, 0)

    def test_unsupported_extension_ignored(self):
        self.drop("图片.png", "not a document")
        summary = self.scan_twice()
        self.assertEqual(summary["suggested"], 0)
        self.assertEqual(self.analyzer.calls, 0)

    def test_short_material_skips_model(self):
        self.drop("太短.md", "太短了")
        summary = self.scan_twice()
        self.assertEqual(summary["suggested"], 0)
        self.assertEqual(self.analyzer.calls, 0)   # 内容不足不烧模型
        reg = json.loads(self.watcher.registry_path.read_text(encoding="utf-8"))
        self.assertEqual(reg["processed"]["太短.md"]["status"], "insufficient")


AGENT_MD = """---
contributed_by: "测试智能体A"
origin_sources: ["仓库D:/proj/README.md", "会议记录 2026-09-30"]
---

# 增量索引的水位线设计

处理流水线重复扫描时，用单调递增的水位线记录已处理位置，重启后从水位线恢复，
避免全量重扫。作者主张水位线必须持久化，否则重启即失去意义。
"""


class ProvenanceTests(InboxTestBase):
    """投递溯源：front-matter 的 contributed_by / origin_sources 进建议记录。"""

    def test_provenance_recorded_and_body_stripped(self):
        self.drop("智能体投递.md", AGENT_MD)
        self.scan_twice()
        sug = self.suggestions()
        self.assertEqual(len(sug), 1)
        self.assertEqual(sug[0]["contributed_by"], "测试智能体A")
        sources = sug[0]["sources"]
        self.assertEqual(sources[0]["read_status"], "readable")
        declared = [s for s in sources if s["read_status"] == "agent_declared"]
        self.assertEqual(len(declared), 2)
        self.assertEqual(declared[0]["source_name"], "仓库D:/proj/README.md")
        self.assertTrue(all(s["upstream_id"].startswith("declared:") for s in declared))
        # 分析器看到的是剥离头部后的正文
        body = self.analyzer.last_material["text"]
        self.assertNotIn("contributed_by", body)
        self.assertIn("水位线", body)
        # reason 中可见投递方，供人工审核
        self.assertIn("测试智能体A", sug[0]["reason"])

    def test_malformed_frontmatter_still_processed(self):
        broken = ("---\ncontributed_by: 没有闭合\n\n# 正文\n\n"
                  "这里有一段足够长的正文内容用于通过最短长度检查，"
                  "并且再补充一句以确保剥离失败时整篇仍按普通材料处理。\n")
        self.drop("坏头部.md", broken)
        summary = self.scan_twice()
        self.assertEqual(summary["suggested"], 1)
        sug = self.suggestions()
        self.assertEqual(len(sug), 1)
        self.assertIsNone(sug[0]["contributed_by"])
        self.assertEqual(len(sug[0]["sources"]), 1)

    def test_plain_text_unchanged_shape(self):
        self.drop("无头部.md", "这是一段没有任何 front-matter 的普通材料正文，长度足够通过最短检查。" * 3)
        self.scan_twice()
        sug = self.suggestions()
        self.assertEqual(len(sug), 1)
        self.assertIsNone(sug[0]["contributed_by"])
        self.assertEqual(len(sug[0]["sources"]), 1)

    def test_files_not_moved_or_deleted(self):
        p = self.drop("材料.md", LONG_MD)
        before = p.read_bytes()
        self.scan_twice()
        self.assertTrue(p.exists())
        self.assertEqual(p.read_bytes(), before)   # 只读，不改动


class ModelAndBudgetTests(InboxTestBase):
    def test_no_analyzer_registers_pending_without_fake_success(self):
        watcher = InboxWatcher(self.gs, analyzer=None, analyzer_budget=BudgetStub(10))
        watcher.inbox_dir.mkdir(parents=True, exist_ok=True)
        (watcher.inbox_dir / "材料.md").write_text(LONG_MD, encoding="utf-8")
        watcher.scan_once(); summary = watcher.scan_once()
        self.assertEqual(summary["pending_analysis"], 1)
        self.assertEqual(summary["suggested"], 0)   # 未配置模型绝不假装分析成功
        self.assertEqual(len(self.suggestions()), 0)

    def test_budget_exhausted_keeps_pending(self):
        watcher = InboxWatcher(self.gs, analyzer=self.analyzer, analyzer_budget=BudgetStub(1))
        watcher.inbox_dir.mkdir(parents=True, exist_ok=True)
        (watcher.inbox_dir / "a.md").write_text(LONG_MD, encoding="utf-8")
        (watcher.inbox_dir / "b.md").write_text(LONG_MD.replace("版本指纹", "配置指纹"), encoding="utf-8")
        watcher.scan_once(); summary = watcher.scan_once()
        self.assertEqual(summary["suggested"], 1)          # 预算 1：只分析一个
        self.assertEqual(summary["pending_analysis"], 1)   # 另一个保留待处理
        self.assertEqual(self.analyzer.calls, 1)

    def test_analyzer_failure_recorded_not_fake_suggestion(self):
        class Failing:
            def describe(self):
                return "失败分析器"

            def analyze(self, *a, **k):
                raise RuntimeError("模拟网络失败")

        watcher = InboxWatcher(self.gs, analyzer=Failing(), analyzer_budget=BudgetStub(10))
        watcher.inbox_dir.mkdir(parents=True, exist_ok=True)
        (watcher.inbox_dir / "材料.md").write_text(LONG_MD, encoding="utf-8")
        watcher.scan_once(); summary = watcher.scan_once()
        self.assertEqual(summary["failed"], 1)
        self.assertEqual(len(self.suggestions()), 0)
        reg = json.loads(watcher.registry_path.read_text(encoding="utf-8"))
        self.assertEqual(reg["processed"]["材料.md"]["status"], "failed")


LONG_PLAIN = "这是一段没有 front-matter 的长正文，用于跨入口去重测试。" * 8


class ReliabilityTests(InboxTestBase):
    """模块一回归：待处理/失败/预算耗尽不丢任务，成功不重复，内容变化重分析。"""

    def _registry(self):
        return json.loads(self.watcher.registry_path.read_text(encoding="utf-8"))["processed"]

    def _force_retry_now(self, rel: str):
        """把注册表里该文件的下次重试时间改到过去，模拟退避到期。"""
        reg = json.loads(self.watcher.registry_path.read_text(encoding="utf-8"))
        reg["processed"][rel]["not_before"] = "2000-01-01T00:00:00+00:00"
        self.watcher.registry_path.write_text(json.dumps(reg, ensure_ascii=False),
                                              encoding="utf-8")

    def test_pending_retried_after_model_configured(self):
        """未配置模型时投放 → 配置后能够继续处理（验收标准）。"""
        self.watcher.analyzer = None
        self.drop("材料.md", LONG_MD)
        self.scan_twice()
        self.assertEqual(self._registry()["材料.md"]["status"], "pending_analysis")
        self.watcher.analyzer = FakeAnalyzer()   # 模拟配置模型并重启
        self.scan_twice()
        entry = self._registry()["材料.md"]
        self.assertEqual(entry["status"], "suggested")
        self.assertEqual(len(self.suggestions()), 1)

    def test_pending_not_marked_as_duplicate(self):
        """待处理文件重扫绝不能被误判为"内容与已处理文件相同"。"""
        self.watcher.analyzer = None
        self.drop("材料.md", LONG_MD)
        self.scan_twice()
        self.scan_twice()   # 再扫几轮：待处理文件保持待处理，不被标 skipped
        self.scan_twice()
        entry = self._registry()["材料.md"]
        self.assertEqual(entry["status"], "pending_analysis")
        self.assertEqual(self.analyzer.calls, 0)

    def test_failed_retries_with_backoff_then_succeeds(self):
        """模型失败 → 登记退避；退避到期后自动重试并成功（任务不丢失）。"""
        class Failing:
            def describe(self):
                return "失败分析器"

            def analyze(self, *a, **k):
                raise RuntimeError("模拟网络失败")

        self.watcher.analyzer = Failing()
        self.drop("材料.md", LONG_MD)
        self.scan_twice()
        entry = self._registry()["材料.md"]
        self.assertEqual(entry["status"], "failed")
        self.assertEqual(entry["attempts"], 1)
        self.assertIn("not_before", entry)
        # 退避期内不重试
        self.watcher.scan_once()
        self.assertEqual(self._registry()["材料.md"]["attempts"], 1)
        # 退避到期 + 模型恢复 → 重试成功
        self.watcher.analyzer = FakeAnalyzer()
        self._force_retry_now("材料.md")
        self.scan_twice()
        entry = self._registry()["材料.md"]
        self.assertEqual(entry["status"], "suggested")
        self.assertEqual(len(self.suggestions()), 1)

    def test_budget_recovered_continues(self):
        """预算耗尽保留待处理；预算恢复后自动继续（验收标准）。"""
        self.watcher.budget = BudgetStub(1)
        self.drop("a.md", LONG_MD)
        self.drop("b.md", LONG_MD.replace("版本指纹", "配置指纹"))
        self.scan_twice()
        self.assertEqual(self._registry()["a.md"]["status"], "suggested")
        self.assertEqual(self._registry()["b.md"]["status"], "pending_analysis")
        self.watcher.budget = BudgetStub(10)   # 预算恢复（次日/上调）
        self.scan_twice()
        self.assertEqual(self._registry()["b.md"]["status"], "suggested")
        self.assertEqual(self.analyzer.calls, 2)

    def test_content_change_reanalyzes(self):
        """同一文件内容变化 → 重新分析并标注，旧建议保留待人审。"""
        self.drop("材料.md", LONG_MD)
        self.scan_twice()
        self.assertEqual(self.analyzer.calls, 1)
        old_sug = self.suggestions()[0]["id"]
        self.drop("材料.md", LONG_MD.replace("版本指纹缓存", "单调水位线设计"))
        self.scan_twice()
        self.assertEqual(self.analyzer.calls, 2)
        entry = self._registry()["材料.md"]
        self.assertEqual(entry["status"], "suggested")
        self.assertIn("内容已变化", entry["note"])
        self.assertIn(old_sug, entry["note"])
        self.assertEqual(len(self.suggestions()), 2)   # 新旧建议都在，人审决定

    def test_unchanged_file_never_reprocessed(self):
        """已成功处理且内容未变 → 无论如何重扫都不再调模型。"""
        self.drop("材料.md", LONG_MD)
        self.scan_twice()
        for _ in range(3):
            self.scan_twice()
        self.assertEqual(self.analyzer.calls, 1)
        self.assertEqual(len(self.suggestions()), 1)

    def test_cross_entry_duplicate_suggestion_not_recreated(self):
        """与其他入口的待审建议内容相同 → 只登记，不重复分析不重复入库。"""
        from pka_graph.discovery import DiscoveryService
        body_sha = hashlib.sha256(LONG_PLAIN.encode("utf-8")).hexdigest()
        disc = DiscoveryService(self.gs, upstream=[])
        disc._save_suggestions([{"id": "sug-upstream-1", "status": "pending",
                                 "content_sha256": body_sha}])
        self.drop("别的入口同内容.md", LONG_PLAIN)
        self.scan_twice()
        entry = self._registry()["别的入口同内容.md"]
        self.assertEqual(entry["status"], "skipped")
        self.assertIn("sug-upstream-1", entry["note"])
        self.assertEqual(self.analyzer.calls, 0)
        self.assertEqual(len(self.suggestions()), 1)   # 不新增建议

    def test_status_exposes_pending_failures_last_success(self):
        """状态可见：上次成功时间、待处理清单、失败原因（含重试次数）。"""
        self.drop("好.md", LONG_MD)
        self.drop("坏.md", LONG_MD.replace("版本指纹", "水位线"))

        class Picky:
            def describe(self):
                return "挑文件的分析器"

            def analyze(self, material, *a, **k):
                if "版本指纹" not in material["text"]:
                    raise RuntimeError("网络超时")
                return FakeAnalyzer().analyze(material, *a, **k)

        self.watcher.analyzer = Picky()
        self.scan_twice()
        st = self.watcher.status()
        self.assertTrue(st["last_success_at"])
        self.assertEqual([p["path"] for p in st["pending"]], [])
        self.assertEqual(len(st["failures"]), 1)
        self.assertEqual(st["failures"][0]["path"], "坏.md")
        self.assertIn("网络超时", st["failures"][0]["note"])
        self.assertEqual(st["failures"][0]["attempts"], 1)
        # 未配置模型场景：待处理清单可见（内容须独特，避免与已成功文件去重）
        self.watcher2 = InboxWatcher(self.gs, analyzer=None, analyzer_budget=BudgetStub(10))
        self.watcher2.inbox_dir.mkdir(parents=True, exist_ok=True)
        (self.watcher2.inbox_dir / "等.md").write_text(
            LONG_MD.replace("版本指纹缓存", "预算耗尽场景"), encoding="utf-8")
        self.watcher2.scan_once()
        self.watcher2.scan_once()
        st2 = self.watcher2.status()
        self.assertEqual([p["path"] for p in st2["pending"]], ["等.md"])
        # 注册表跨 watcher 共享：last_success_at 反映的是整个收件箱的历史成功
        self.assertIsInstance(st2["last_success_at"], str)

    def test_duplicate_of_pending_sibling_resolved_without_double_model(self):
        """同内容两个文件都待处理 → 恢复模型后只分析一次、只出一条建议。"""
        self.drop("a.md", LONG_MD)
        self.drop("b.md", LONG_MD)   # 同内容不同名
        watcher = InboxWatcher(self.gs, analyzer=None, analyzer_budget=BudgetStub(10))
        watcher.inbox_dir = self.watcher.inbox_dir
        watcher.registry_path = self.watcher.registry_path
        watcher.scan_once()
        watcher.analyzer = FakeAnalyzer()
        watcher.scan_once()
        self.assertEqual(watcher.analyzer.calls, 1)
        reg = json.loads(watcher.registry_path.read_text(encoding="utf-8"))["processed"]
        statuses = sorted(v["status"] for v in reg.values())
        self.assertEqual(statuses, ["skipped", "suggested"])
        self.assertEqual(len(self.suggestions()), 1)


class ExcerptMatchingTests(unittest.TestCase):
    """摘录定位：格式归一化不放松"文字必须存在"的约束。"""

    def test_markdown_markers_do_not_block_matching(self):
        from pka_graph.discovery_model import validate_semantic_output
        material = "**时间戳水位线**：实现简单，但存在时钟漂移。\n\n- 单调序列水位线：无时钟问题。"
        out = validate_semantic_output(
            {"classification": "new_node",
             "source_excerpts": [{"upstream_id": "u", "excerpt": "时间戳水位线：实现简单，但存在时钟漂移。"}]},
            material, [])
        self.assertEqual(out["classification"], "new_node")   # 不加粗也能定位
        self.assertEqual(out["unlocated_excerpts"], 0)

    def test_fabricated_excerpt_still_rejected(self):
        from pka_graph.discovery_model import validate_semantic_output
        material = "**时间戳水位线**：实现简单。"
        out = validate_semantic_output(
            {"classification": "new_node",
             "source_excerpts": [{"upstream_id": "u", "excerpt": "这部分是模型编造的结论"}]},
            material, [])
        self.assertEqual(out["classification"], "uncertain")  # 编造仍被拦截
        self.assertEqual(out["unlocated_excerpts"], 1)
        self.assertEqual(out["classification_before_downgrade"], "new_node")


class PdfTests(unittest.TestCase):
    def test_pdf_text_extraction(self):
        try:
            import fitz
        except ImportError:
            self.skipTest("PyMuPDF 未安装")
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "sample.pdf"
            doc = fitz.open()
            page = doc.new_page()
            page.insert_text((72, 72), "Version fingerprint cache keeps a hash of input and config.")
            doc.save(str(p))
            doc.close()
            text, err, meta = read_pdf_text(p)
        self.assertIsNotNone(meta)
        self.assertFalse(meta["truncated"])   # 短文档不截断
        self.assertIsNone(err)
        self.assertIn("fingerprint", text)

    def test_scanned_pdf_without_text_reports_reason(self):
        try:
            import fitz
        except ImportError:
            self.skipTest("PyMuPDF 未安装")
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "blank.pdf"
            doc = fitz.open()
            doc.new_page()   # 空白页 = 无文本层（模拟扫描件）
            doc.save(str(p))
            doc.close()
            text, err, meta = read_pdf_text(p)
        self.assertIsNotNone(meta)
        self.assertFalse(meta["truncated"])   # 短文档不截断
        self.assertEqual(text, "")
        self.assertIn("OCR", err)


if __name__ == "__main__":
    unittest.main()

class FakeNoteProcessor:
    """可编程假加工器：publishable=True 模拟已取得原文且加工完整（共享夹具）。"""

    def __init__(self, publishable: bool = True):
        self.publishable_flag = publishable
        self.calls = 0

    def process(self, *, suggestion, vault_root, remote_fetch=False):
        self.calls += 1
        from pka_graph.note_processor import NoteProcessor

        source = NoteProcessor.resolve_material_text(suggestion, vault_root,
                                                     remote_fetch=False)
        if self.publishable_flag:
            # 模拟"已成功取得完整正文"：正式准入由夹具标志控制，不依赖真实解析
            source = dict(source, obtained=True, body_usable=True, scope="full")
            body = ("## 核心结论\n\n结论。\n\n## 正文\n\n**原文**\n\n"
                    + source["text"][:200] + "\n\n**笔记**\n\n笔记内容")
            return {"ok": True, "title": "入库中文标题",
                    "body": body,
                    "skill_version": "2026-10-03", "skill_kind": "通用", "calls": 1,
                    "source": source, "processed_range": "全部 1 段",
                    "publishable": True, "complete": True,
                    "segments": {"total": 1, "processed": 1, "unprocessed": [],
                                 "missing_notes": []}}
        return {"ok": True, "title": "部分标题", "body": "## 核心结论\n\n部分。",
                "skill_version": "2026-10-03", "skill_kind": "通用", "calls": 1,
                "source": source, "processed_range": "已处理 1/2 段（未处理：后段）",
                "publishable": False, "complete": False,
                "segments": {"total": 2, "processed": 1, "unprocessed": ["后段"],
                             "missing_notes": []}}


class AutoAcceptTests(InboxTestBase):
    """免逐条审核：有知识增量自动入库；uncertain 留待处理。"""

    def test_auto_accept_creates_formal_note_and_untested_node(self):
        from pka_graph.discovery import DiscoveryService
        from test_discovery import FakeNoteProcessor as _F
        self.watcher.auto_accept = True
        self.gs.discovery = DiscoveryService(self.gs, upstream=[],
                                             note_processor=_F(publishable=True))
        self.drop("材料.md", LONG_MD)
        self.scan_twice()
        sugs = DiscoveryService(self.gs, upstream=[])._load_suggestions()
        self.assertEqual(sugs[0]["status"], "accepted")
        reg = json.loads(self.watcher.registry_path.read_text(encoding="utf-8"))
        self.assertTrue(reg["processed"]["材料.md"].get("auto_accepted"))
        nodes = [n for n in self.gs.state()["nodes"] if n["name"] == "入库中文标题"]
        self.assertEqual(len(nodes), 1)
        self.assertEqual(nodes[0]["status"], "untested")   # 自动入库不等于掌握
        notes = list((self.vault / "待分类领域" / "正式笔记").glob("*.md"))
        self.assertEqual(len(notes), 1)
        self.assertIn("status: accepted", notes[0].read_text(encoding="utf-8"))

    def test_auto_accept_skips_uncertain(self):
        from pka_graph.discovery import DiscoveryService
        self.gs.discovery = DiscoveryService(self.gs, upstream=[])
        self.watcher.auto_accept = True
        self.watcher.analyzer = FakeAnalyzer(classification="uncertain")
        self.drop("材料.md", LONG_MD)
        self.scan_twice()
        sugs = DiscoveryService(self.gs, upstream=[])._load_suggestions()
        self.assertEqual(sugs[0]["status"], "pending")   # uncertain 不自动入库
        nodes = [n for n in self.gs.state()["nodes"] if n["name"] == "材料"]
        self.assertEqual(nodes, [])
