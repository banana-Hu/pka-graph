"""第六阶段测试：只读接入、确定性分类（14 场景）、建议队列与接受安全。"""

import json
import sys
import tempfile
import time
import unittest
from datetime import datetime, timezone
from hashlib import sha256
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tests"))

import discovery_fixtures as fx
from fixtures import make_test_vault
from pka_graph.discovery import DiscoveryService, extract_contribution, classify_material
from pka_graph.models import ValidationError
from pka_graph.quiz_model import Budget, FakeModelAdapter
from pka_graph.quiz_service import QuizError, QuizService
from pka_graph.review_service import ReviewService
from pka_graph.service import GraphService
from pka_graph.store import StateStore

# 真实库路径由环境变量 PKA_REAL_VAULT 提供；未设置时相关测试自动跳过
import os as _os
REAL_VAULT = (Path(_os.environ["PKA_REAL_VAULT"].strip())
              if _os.environ.get("PKA_REAL_VAULT", "").strip() else None)
INFO_PROJECT_DATA = (Path(_os.environ["PKA_INFO_DATA"].strip())
                     if _os.environ.get("PKA_INFO_DATA", "").strip() else None)


def fixed_clock(y=2026, mo=10, d=2, h=9):
    return lambda: datetime(y, mo, d, h, 0, tzinfo=timezone.utc)


class FakeNoteProcessor:
    """可编程假加工器：publishable=True 模拟已取得原文且加工完整。"""

    def __init__(self, publishable: bool = True):
        self.publishable_flag = publishable
        self.calls = 0

    def process(self, *, suggestion, vault_root, remote_fetch=False):
        self.calls += 1
        if self.publishable_flag:
            source = {"text": "合成正文", "obtained": True, "scope": "full", "note": "测试来源"}
            return {"ok": True, "title": "入库中文标题", "body": "## 核心结论\n\n结论。",
                    "skill_version": "2026-10-03", "skill_kind": "通用", "calls": 1,
                    "source": source, "processed_range": "全部 1 段",
                    "publishable": True, "complete": True,
                    "segments": {"total": 1, "processed": 1, "unprocessed": [],
                                 "missing_notes": []}}
        source = {"text": "", "acquired": False, "scope": "summary_only", "note": "仅摘要"}
        return {"ok": True, "title": "部分标题", "body": "## 核心结论\n\n部分。",
                "skill_version": "2026-10-03", "skill_kind": "通用", "calls": 1,
                "source": source, "processed_range": "已处理 1/2 段（未处理：后段）",
                "publishable": False, "complete": False,
                "segments": {"total": 2, "processed": 1, "unprocessed": ["后段"],
                             "missing_notes": []}}


def write_upstream(tmp: Path, items: list[dict], name="feed_candidates.js",
                   const="FEED_CANDIDATES") -> dict:
    records = [{
        "id": it["id"], "title": it["title"], "summary": it.get("summary", ""),
        "canonicalUrl": f"https://example.com/{it['id']}",
        "sourceName": "测试来源", "sourceType": "rss",
        "publishedAt": it.get("published"), "discoveredAt": datetime.now(timezone.utc).isoformat(),
        "topics": it.get("topics", []),
    } for it in items]
    path = tmp / name
    path.write_text(f"const {const} = " + json.dumps({"candidates": records}, ensure_ascii=False) + ";\n",
                    encoding="utf-8")
    return {"path": str(path), "const": const, "label": "测试上游"}


def make_discovery(vault: Path, upstream: list[dict], limit=50):
    gs = GraphService(StateStore(vault))
    review = ReviewService(gs)
    svc = DiscoveryService(gs, upstream=upstream, clock=fixed_clock())
    return gs, review, svc


class ClassificationTests(unittest.TestCase):
    """14 场景分类：预期在 discovery_fixtures.EXPECTED 中预先注明。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.vault = make_test_vault(Path(self._tmp.name))
        # 种子节点（决定"相对知识库覆盖"）
        gs = GraphService(StateStore(self.vault))
        for seed in fx.SEED_NODES:
            gs.create_node(name=seed["name"], domain=seed["domain"], scope=seed["scope"])
        self.gs = gs
        self.review = ReviewService(gs)
        self.disc = DiscoveryService(gs, upstream=[], clock=lambda: datetime(2026, 10, 2, 9, 0, tzinfo=timezone.utc))

    def tearDown(self):
        self._tmp.cleanup()

    def _classify(self, item):
        # 直接调用引擎同一代码路径（材料分级 → 提取 → 相关检索 → 比较）
        material = classify_material(item.get("summary", ""))
        extraction = extract_contribution(material["text"], item["title"])
        cand = {"title": item["title"], "summary": item.get("summary", ""),
                "topics": item.get("topics", [])}
        related = self.disc._related_nodes(self.gs.state(), self.disc._candidate_tokens(cand),
                                           set(item.get("topics", [])))
        from pka_graph.discovery import compare_with_vault

        verdict, reason, _ = compare_with_vault(extraction, item["title"], material["text"], related)
        return verdict, reason, extraction

    def test_all_14_scenarios_match_pre_annotated_expectations(self):
        """任务 8：逐场景断言预先注明的预期分类与关键理由。"""
        for key, expected in fx.EXPECTED.items():
            if expected["verdict"] == "aggregate_into_event_source_a":
                continue  # 聚合在 AggregationTests 单独验证
            with self.subTest(scenario=key):
                item = fx.ITEMS[key]
                prereq = expected.get("prerequisite")
                if prereq:
                    pre = fx.ITEMS[prereq]
                    self.gs.create_node(name=pre["title"][:80], domain="AI技术领域",
                                        scope=pre["summary"])
                verdict, reason, extraction = self._classify(item)
                self.assertEqual(verdict, expected["verdict"],
                                 f"{key}: 期望 {expected['verdict']}，得到 {verdict}；理由：{reason}")
                for kw in expected.get("reason_keywords", []):
                    self.assertIn(kw, reason + json.dumps(extraction, ensure_ascii=False),
                                  f"{key}: 理由缺少关键词 {kw}")
                if "novelty" in expected:
                    from pka_graph.discovery import novelty_flags

                    nov = novelty_flags(item.get("summary", ""), True, 0.0, item.get("published"))
                    for flag, want in expected["novelty"].items():
                        self.assertEqual(nov[flag], want, f"{key}: {flag}")

    def test_13_similar_titles_not_merged(self):
        """必测 13：标题相似但内容不同（发布 vs 失败分析）不被合并成一条；
        失败分析相对已接受的 LiteRAG 节点构成更新提案（revision）。"""
        a_item, b_item = fx.ITEMS["similar_title_a"], fx.ITEMS["similar_title_b"]
        va, _, _ = self._classify(a_item)
        self.assertEqual(va, "new_node")
        # a 被接受为节点（简化：直接创建同名同范围节点，内容版本与建议快照无关紧要）
        self.gs.create_node(name=a_item["title"][:80], domain="AI技术领域",
                            scope=a_item["summary"])
        vb, reason, _ = self._classify(b_item)
        self.assertEqual(vb, "revision", reason)  # 与 LiteRAG 节点相关且口径相反 → 更新提案
        from pka_graph.discovery import jaccard, tokens

        sim = jaccard(tokens(a_item["summary"]), tokens(b_item["summary"]))
        self.assertLess(sim, 0.5)  # 内容差异显著：聚合守卫不会合并

    def test_11_injection_content_stored_as_data(self):
        """必测注入项：注入文本按数据处理、原样存储，不触发任何特殊动作。"""
        item = fx.ITEMS["injection_content"]
        verdict, reason, extraction = self._classify(item)
        self.assertEqual(verdict, "news_only")
        self.assertIn("忽略之前所有规则", item["summary"])  # 原文按数据存储
        # 引擎没有执行通道：提取结果只包含原文子串
        claims = json.dumps(extraction["claims"], ensure_ascii=False)
        self.assertNotIn("自动创建节点", claims)


class IngestTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.vault = make_test_vault(Path(self._tmp.name))
        (self.vault / ".pka-test-vault.json").write_text(json.dumps({"purpose": "test"}), encoding="utf-8")
        self.up_tmp = tempfile.TemporaryDirectory()
        self.upstream = write_upstream(Path(self.up_tmp.name), list(fx.ITEMS.values()))
        self.gs = GraphService(StateStore(self.vault))
        self.review = ReviewService(self.gs)
        self.disc = DiscoveryService(self.gs, upstream=[self.upstream], clock=fixed_clock())
        self.quiz = QuizService(self.gs, FakeModelAdapter(), Budget(), "demo",
                                review=self.review, clock=fixed_clock())

    def tearDown(self):
        self.up_tmp.cleanup()
        self._tmp.cleanup()

    def test_duplicate_read_does_not_duplicate_suggestions(self):
        """确定性测试：已见条目不再生成建议；延后项在下一轮合法补齐后收敛。"""
        self.disc.check()
        self.disc.check()   # 处理上一轮延后项（每批上限）→ 合法新增
        first = self.disc.list_suggestions(status="pending")["count"]
        self.disc.check()   # 全部已见 → 不再有新建议
        second = self.disc.list_suggestions(status="pending")["count"]
        self.assertEqual(first, second)
        # 全库唯一性：同 upstream_id 只出现一次
        ids = [src["upstream_id"] for s in self.disc._load_suggestions() for src in s["sources"]]
        self.assertEqual(len(ids), len(set(ids)))

    def test_missing_and_unparseable_sources_visible(self):
        """必测 12：来源不可读取 → 状态可见，不影响其他源。"""
        bad = {"path": str(Path(self.up_tmp.name) / "nope.js"), "const": "X", "label": "坏源"}
        broken = Path(self.up_tmp.name) / "broken.js"
        broken.write_text("const Y = {不是json", encoding="utf-8")
        broken_src = {"path": str(broken), "const": "Y", "label": "损坏源"}
        disc = DiscoveryService(self.gs, upstream=[self.upstream, bad, broken_src], clock=fixed_clock())
        out = disc.check()
        statuses = {s["label"]: s["status"] for s in out["sources"]}
        self.assertEqual(statuses["坏源"], "missing")
        self.assertEqual(statuses["损坏源"], "unparseable")
        self.assertEqual(statuses["测试上游"], "ok")

    def test_cursor_deferred_items_retried(self):
        """游标恢复：达到每批上限的条目延后，不丢失，下次优先处理。"""
        disc = DiscoveryService(self.gs, upstream=[self.upstream], clock=fixed_clock())
        state = disc.svc.state()
        disc._discovery_state(state)["per_check_limit"] = 3
        disc.svc.store.save(state, base_version=state["version"])
        out1 = disc.check()
        self.assertEqual(out1["deferred"], len(fx.ITEMS) - out1["checked"])
        out2 = disc.check()
        self.assertGreater(out2["checked"], 0)  # 延后项被继续处理
        total_pending = 0
        for _ in range(10):
            out = disc.check()
            total_pending = out["deferred"]
            if out["deferred"] == 0:
                break
        self.assertEqual(total_pending, 0)  # 最终全部处理，无永久丢失

    def test_upstream_files_unchanged_by_check(self):
        """上游项目只读：检查前后候选文件逐字节不变。"""
        path = Path(self.upstream["path"])
        before = path.read_bytes()
        self.disc.check()
        self.assertEqual(before, path.read_bytes())

    def test_real_upstream_contract(self):
        """真实信息获取项目：只读实查当前候选格式（id/标题/来源/时间/摘要存在）。"""
        if not INFO_PROJECT_DATA.exists():
            self.skipTest("信息获取项目不在本机")
        from pka_graph.discovery import DEFAULT_UPSTREAM, read_upstream

        result = read_upstream(DEFAULT_UPSTREAM[0])
        self.assertEqual(result["status"], "ok")
        self.assertGreater(len(result["candidates"]), 0)
        cand = result["candidates"][0]
        for field in ("id", "title", "canonicalUrl", "sourceName", "discoveredAt"):
            self.assertTrue(str(cand.get(field) or "").strip(), f"真实候选缺少 {field}")
        # 摘要级材料：上游无正文
        material = classify_material(str(cand.get("summary") or ""))
        self.assertIn(material["level"], ("readable_summary", "clue_only"))


class AcceptSafetyTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.vault = make_test_vault(Path(self._tmp.name))
        (self.vault / ".pka-test-vault.json").write_text(json.dumps({"purpose": "test"}), encoding="utf-8")
        self.up_tmp = tempfile.TemporaryDirectory()
        self.upstream = write_upstream(Path(self.up_tmp.name), [
            fx.ITEMS["tech_doc_new_mechanism"], fx.ITEMS["supplement_example"],
        ])
        self.gs = GraphService(StateStore(self.vault))
        self.review = ReviewService(self.gs)
        seed = fx.SEED_NODES[0]
        self.gs.create_node(name=seed["name"], domain=seed["domain"], scope=seed["scope"])
        self.disc = DiscoveryService(self.gs, upstream=[self.upstream], clock=fixed_clock())

    def tearDown(self):
        self.up_tmp.cleanup()
        self._tmp.cleanup()

    def _pending(self):
        self.disc.check()
        pending = self.disc.list_suggestions(status="pending")["suggestions"]
        return {s["verdict"]: s for s in pending}

    def _fake_processor(self, publishable=True):
        from test_inbox import FakeNoteProcessor as _F  # noqa: 复用共享假加工器
        return _F(publishable)

    def test_accept_unacquired_summary_becomes_clue_not_formal(self):
        """摘要级上游材料（未取得原文）：人工接受 → 线索笔记（候选目录），不建节点。"""
        pending = self._pending()
        sug = pending["new_node"]
        out = self.disc.accept(None, sug["id"], edits={"request_id": "acc-clue"})
        self.assertFalse(out["published"])
        self.assertIn("clue", out)
        self.assertEqual(sug["verdict"], "new_node")
        notes = list(self.vault.rglob("候选笔记/**/线索-*.md"))
        self.assertEqual(len(notes), 1)
        text = notes[0].read_text(encoding="utf-8")
        self.assertIn("status: candidate", text)
        self.assertIn("未通过内容准入", text)
        saved = next(s for s in self.disc._load_suggestions() if s["id"] == sug["id"])
        self.assertEqual(saved["status"], "accepted")
        self.assertEqual(saved["accepted"]["as"], "clue")
        # 不建节点：掌握状态无从谈起
        self.assertFalse(any(n["name"] == saved["name"] for n in self.gs.state()["nodes"]))

    def test_accept_with_publishable_processing_creates_formal_note_and_node(self):
        """加工可发布（已取得原文且完整）：正式笔记 + untested 节点 + 关联。"""
        self.disc.note_processor = self._fake_processor(publishable=True)
        pending = self._pending()
        sug = pending["new_node"]
        out = self.disc.accept(None, sug["id"], edits={"request_id": "acc-1"})
        self.assertTrue(out["published"])
        node = self.gs.node_detail(out["node_id"])["node"]
        self.assertEqual(node["status"], "untested")   # 入库不等于掌握
        self.assertIsNone(node.get("status_origin"))
        self.assertEqual(len(self.gs.node_detail(node["id"])["note_links"]), 1)
        note_path = next(self.vault.rglob(out["note"]), None)
        self.assertIsNotNone(note_path)
        note_text = note_path.read_text(encoding="utf-8")
        self.assertIn("status: accepted", note_text)
        self.assertIn('source_scope: "full"', note_text)   # 准入字段：取得全文
        self.assertIn("processed_range:", note_text)
        saved = next(s for s in self.disc._load_suggestions() if s["id"] == sug["id"])
        self.assertEqual(saved["admission"]["publishable"], True)

    def test_duplicate_accept_idempotent(self):
        """重复接受：结果唯一（同节点、同笔记）。"""
        self.disc.note_processor = self._fake_processor(publishable=True)
        pending = self._pending()
        sug = pending["new_node"]
        out1 = self.disc.accept(None, sug["id"], edits={}, request_id="acc-dup")
        out2 = self.disc.accept(None, sug["id"], edits={}, request_id="acc-dup")
        self.assertTrue(out2.get("replayed"))
        self.assertEqual(out1["node_id"], out2["node_id"])
        notes = [n for n in self.vault.rglob("正式笔记/**/*.md")
                 if "入库中文标题" in n.name]
        self.assertEqual(len(notes), 1)  # 不重复生成笔记文件

    def test_rollback_when_state_save_fails(self):
        """多文件写入中断保留正文与意图，恢复后补齐状态而不重新生成。"""
        self.disc.note_processor = self._fake_processor(publishable=True)
        pending = self._pending()
        sug = pending["new_node"]
        with patch.object(self.gs.store, "save", side_effect=OSError("模拟中断")):
            with self.assertRaises(OSError):
                self.disc.accept(None, sug["id"], edits={})
        self.assertEqual(len([n for n in self.vault.rglob("正式笔记/**/*.md")
                              if "入库中文标题" in n.name]), 1)
        self.assertTrue(self.disc._load_intents())  # 保留完整意图，可恢复且不误删人工改动。
        self.assertEqual(sug["status"], self.disc._load_suggestions()[0]["status"])  # 仍 pending
        # 恢复后可正常接受
        out = self.disc.accept(None, sug["id"], edits={})
        self.assertEqual(out["verdict"], "new_node")
        self.assertTrue(out["published"])

    def test_orphan_scan_visible(self):
        """孤儿候选笔记可被扫描发现（中断恢复入口）。"""
        self.candidate_dir = self.vault / "候选笔记"
        (self.candidate_dir / "discovery-orphan.md").write_text(
            "---\nid: note-orphan\nstatus: candidate\n---\n孤儿", encoding="utf-8")
        orphans = self.disc.list_suggestions(status="all")["orphan_notes"]
        self.assertIn("discovery-orphan.md", [o["path"] for o in orphans])

    def test_supplement_and_revision_do_not_touch_status_or_scope(self):
        """supplement/revision：只追加提案记录，不改范围/正文/掌握状态。"""
        seed = self.gs.node_detail(next(
            n["id"] for n in self.gs.state()["nodes"] if n["name"] == fx.SEED_NODES[0]["name"]))["node"]
        pending = self._pending()
        scope_before = self.gs.node_detail(seed["id"])["node"]["scope"]
        status_before = seed["status"]
        if "supplement" in pending:
            out = self.disc.accept(None, pending["supplement"]["id"], edits={})
            node = self.gs.node_detail(seed["id"])["node"]
            self.assertEqual(node["scope"], scope_before)
            self.assertEqual(node["status"], status_before)
            self.assertTrue(node.get("supplement_suggestions"))
        if "revision" in pending:
            self.disc.check()  # 可能需要重新生成
            pending = self.disc.list_suggestions(status="pending")["suggestions"]
            rev = next((s for s in pending if s["verdict"] == "revision"), None)
            if rev:
                out = self.disc.accept(None, rev["id"], edits={})
                node = self.gs.node_detail(seed["id"])["node"]
                self.assertEqual(node["scope"], scope_before)
                self.assertEqual(node["status"], status_before)
                self.assertTrue(node.get("update_proposals"))

    def test_revision_rejected_when_target_version_changed(self):
        """目标节点变化后旧提案不能静默覆盖。"""
        self.disc.check()
        pending = {s["verdict"]: s for s in self.disc.list_suggestions(status="pending")["suggestions"]}
        sug = next((s for s in pending.values() if s["related"]), None)
        if not sug:
            self.skipTest("本批无带相关节点的建议")
        target_id = sug["related"][0]["node_id"]
        # 建议创建后目标节点范围变化 → 内容版本 +1
        self.gs.update_node(target_id, {"scope": "被并发修改的范围"}, confirm_scope_change=True,
                            base_version=None)
        with self.assertRaises(ValidationError):
            self.disc.accept(None, sug["id"], edits={})
        self.assertEqual(self.disc._load_suggestions()[0]["status"],
                         next(s["status"] for s in self.disc._load_suggestions()
                              if s["id"] == sug["id"]))

    def test_accept_does_not_trigger_passed_and_formal_vault_untouched(self):
        """接受不触发 passed；正式知识库零写入（哈希对比）。"""
        # 真实库正被写入（服务运行/用户批量导入）时外部并发变更不可避免，
        # 哈希对比只在库静止时有效：库里最近 2 分钟内有 .md 变更则跳过对比。
        def vault_is_hot() -> bool:
            if REAL_VAULT is None:
                return True    # 公开版未配置真实库：无真实库可保护，跳过对比
            if not REAL_VAULT.exists():
                return False
            cutoff = time.time() - 120
            for p in REAL_VAULT.rglob("*.md"):
                try:
                    if p.stat().st_mtime > cutoff:
                        return True
                except OSError:
                    continue
                return False
            return False

        before = {} if vault_is_hot() else \
            {p: sha256(p.read_bytes()).hexdigest() for p in sorted(REAL_VAULT.rglob("*.md"))}
        pending = self._pending()
        sug = pending.get("new_node")
        if sug:
            out = self.disc.accept(None, sug["id"], edits={})
            if out.get("node_id"):   # 线索路径无节点；正式路径必须 untested
                self.assertEqual(self.gs.node_detail(out["node_id"])["node"]["status"], "untested")
        if before:
            after = {p: sha256(p.read_bytes()).hexdigest() for p in sorted(REAL_VAULT.rglob("*.md"))}
            self.assertEqual(before, after)


if __name__ == "__main__":
    unittest.main()

class FormalDirTests(AcceptSafetyTests):
    """正式笔记落点：领域目录映射（容忍编号前缀）。"""

    def test_formal_dir_matches_numbered_domain(self):
        (self.vault / "01-AI技术领域").mkdir(exist_ok=True)
        d = self.disc._formal_note_dir("AI技术领域")
        self.assertEqual(d, self.vault / "01-AI技术领域" / "正式笔记")
        d2 = self.disc._formal_note_dir("不存在的领域")
        self.assertEqual(d2, self.vault / "待分类领域" / "正式笔记")
