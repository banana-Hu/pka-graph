"""模块六回归：check() 语义阶段——预筛→语义→自动入库（机会判断主流程）。

全部用确定性假分析器，不调用真实模型；预期在测试前人工注明，不为输出改预期。
"""
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tests"))

import discovery_fixtures as fx
from test_discovery import write_upstream
from fixtures import make_test_vault
from pka_graph.discovery import DiscoveryService
from pka_graph.review_service import ReviewService
from pka_graph.service import GraphService
from pka_graph.store import StateStore


class FixedBudget:
    def __init__(self, per_check=6, daily=24):
        self.per_check_model_calls = per_check
        self.daily_model_calls_limit = daily


def fixed_clock(y=2026, mo=10, d=3, h=9):
    from datetime import datetime, timezone

    class _Clock:
        def __call__(self):
            return datetime(y, mo, d, h, tzinfo=timezone.utc)

        def isoformat(self, timespec="seconds"):
            return self().isoformat(timespec=timespec)

        def date(self):
            return self().date()

    return _Clock()


class FakeSemantic:
    """确定性假语义分析器：返回固定分类，摘录取材料前 20 字（保证可定位）。"""

    def __init__(self, classification="new_node"):
        self.classification = classification
        self.calls = 0

    def describe(self):
        return "假语义分析器（确定性）"

    def analyze(self, material, sources, related, note_fragments):
        self.calls += 1
        excerpt = material["text"][:20]
        return {
            "classification": self.classification,
            "contribution": {"problem": None,
                             "claims": [excerpt], "differences": [], "conditions_limits": []},
            "differences_vs_related": [],
            "source_excerpts": [{"upstream_id": sources[0]["upstream_id"],
                                 "excerpt": excerpt}],
            "unknowns": [],
        }


class SemanticCheckTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.vault = make_test_vault(Path(self._tmp.name))
        self.upstream = write_upstream(Path(self._tmp.name),
                                       [fx.ITEMS["tech_doc_new_mechanism"]])

    def tearDown(self):
        self._tmp.cleanup()

    def _service(self, analyzer, auto_accept=True, budget=None, note_processor=None):
        gs = GraphService(StateStore(self.vault))
        ReviewService(gs)
        return gs, DiscoveryService(gs, upstream=[self.upstream], clock=fixed_clock(),
                                    analyzer=analyzer, analyzer_budget=budget or FixedBudget(),
                                    auto_accept=auto_accept, note_processor=note_processor)

    def test_semantic_new_node_auto_accepts_into_formal_note(self):
        """预筛疑似增量 → 语义确认 new_node → 自动入库（正式笔记 + untested 节点）。"""
        from test_discovery import FakeNoteProcessor
        analyzer = FakeSemantic("new_node")
        gs, disc = self._service(analyzer, note_processor=FakeNoteProcessor(publishable=True))
        out = disc.check()
        self.assertEqual(out["semantic_done"], 1)
        self.assertEqual(out["auto_published"], 1)
        sugs = disc._load_suggestions()
        self.assertEqual(sugs[0]["status"], "accepted")
        self.assertEqual(sugs[0]["verdict"], "new_node")
        self.assertEqual(sugs[0]["prescreen_verdict"], "new_node")
        self.assertIn("语义分析确认", sugs[0]["reason"])
        node = gs.node_detail(sugs[0]["accepted"]["node_id"])["node"]
        self.assertEqual(node["status"], "untested")   # 入库不等于掌握
        notes = list(self.vault.rglob(sugs[0]["accepted"]["note"]))
        self.assertEqual(len(notes), 1)

    def test_semantic_uncertain_stays_pending(self):
        """语义判 uncertain → 不入库，建议留队带语义记录。"""
        analyzer = FakeSemantic("uncertain")
        gs, disc = self._service(analyzer)
        out = disc.check()
        self.assertEqual(out["auto_published"], 0)
        sugs = disc._load_suggestions()
        self.assertEqual(sugs[0]["status"], "pending")
        self.assertEqual(sugs[0]["verdict"], "uncertain")
        self.assertIsNotNone(sugs[0].get("semantic"))

    def test_semantic_reclassifies_news(self):
        """预筛判疑似增量、语义改判 news_only → 不入库，改判留痕。"""
        analyzer = FakeSemantic("news_only")
        gs, disc = self._service(analyzer)
        disc.check()
        sugs = disc._load_suggestions()
        self.assertEqual(sugs[0]["prescreen_verdict"], "new_node")
        self.assertEqual(sugs[0]["verdict"], "news_only")
        self.assertIn("语义分析改判", sugs[0]["reason"])

    def test_per_check_cap_defers_to_backlog(self):
        """每轮语义上限 1：本轮只分析一条，其余留 pending 无 semantic 字段。"""
        self.upstream = write_upstream(Path(self._tmp.name), [
            fx.ITEMS["tech_doc_new_mechanism"], fx.ITEMS["old_article_new_to_user"]])
        analyzer = FakeSemantic("new_node")
        gs, disc = self._service(analyzer, budget=FixedBudget(per_check=1, daily=24))
        out = disc.check()
        self.assertEqual(out["semantic_done"], 1)
        self.assertEqual(analyzer.calls, 1)
        pending = [s for s in disc._load_suggestions() if s["status"] == "pending"]
        self.assertEqual(len(pending), 2)
        # 已语义复核的那条：上游摘要无原文 → 准入阻断，留队且登记指纹（不反复烧预算）
        done = [s for s in pending if s.get("semantic")]
        self.assertEqual(len(done), 1)
        self.assertTrue(done[0].get("admission", {}).get("blocked_fingerprint"))
        # 未轮到的那条：无 semantic 字段，下轮优先补
        self.assertTrue(any(not s.get("semantic") for s in pending))
        # 下一轮 check：积压项被补上语义
        out2 = disc.check()
        self.assertEqual(out2["semantic_done"], 1)
        self.assertEqual(analyzer.calls, 2)
        self.assertFalse(any(s["status"] == "pending" and not s.get("semantic")
                             for s in disc._load_suggestions()))
        # 第三轮：全部已有语义结论，不再调用模型；被阻断项也不重复 accept
        out3 = disc.check()
        self.assertEqual(out3["semantic_done"], 0)
        self.assertEqual(analyzer.calls, 2)

    def test_daily_budget_exhausted_skips_semantic(self):
        """每日预算耗尽 → 本轮零语义调用，预筛建议留队待复核。"""
        analyzer = FakeSemantic("new_node")
        gs, disc = self._service(analyzer, budget=FixedBudget(per_check=6, daily=1))
        # 预置当日账本已用满（daily=1, used=1）——state 每次从盘加载，须显式保存
        state = gs.state()
        section = disc._discovery_state(state)
        section["model_calls"] = {"date": "2026-10-03", "used": 1}
        gs.store.save(state, base_version=None)
        out = disc.check()
        self.assertEqual(out["semantic_done"], 0)
        self.assertEqual(analyzer.calls, 0)
        self.assertEqual(disc._load_suggestions()[0]["status"], "pending")

    def test_no_analyzer_never_auto_accepts(self):
        """未配置语义分析器时：即使 auto_accept=True 也绝不自动入库。"""
        gs, disc = self._service(None)
        out = disc.check()
        self.assertEqual(out.get("semantic_done"), 0)
        self.assertEqual(disc._load_suggestions()[0]["status"], "pending")

    def test_semantic_error_keeps_verdict_and_costs_budget(self):
        """语义调用抛异常 → 维持预筛结论、如实标注、计入预算。"""

        class Exploding(FakeSemantic):
            def analyze(self, *a, **k):
                self.calls += 1
                raise RuntimeError("网络不可达")

        analyzer = Exploding()
        gs, disc = self._service(analyzer)
        out = disc.check()
        self.assertEqual(out["semantic_done"], 1)
        sugs = disc._load_suggestions()
        self.assertEqual(sugs[0]["verdict"], "new_node")   # 维持预筛结论
        self.assertIn("semantic_error", sugs[0])
        self.assertIn("网络不可达", sugs[0]["semantic_error"])
        state = gs.state()
        ledger = state["discovery"]["model_calls"]
        self.assertGreaterEqual(ledger["used"], 1)


if __name__ == "__main__":
    unittest.main()

    def test_unacquired_summary_never_auto_published(self):
        """上游摘要（未取得原文）：语义判 new_node 也不自动转正式——只留队待重试。"""
        analyzer = FakeSemantic("new_node")
        gs, disc = self._service(analyzer, note_processor=None)  # 无加工器 → 必不正式
        out = disc.check()
        self.assertEqual(out["semantic_done"], 1)
        self.assertEqual(out["auto_published"], 0)
        sugs = disc._load_suggestions()
        self.assertEqual(sugs[0]["status"], "pending")
        self.assertFalse(sugs[0]["admission"]["publishable"])
        self.assertIn("未取得原文全文", sugs[0]["admission"]["reason"])
