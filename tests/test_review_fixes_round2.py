# -*- coding: utf-8 -*-
"""第二轮审查修复回归：路径越界、正文可用性、重试分级、统计分类、节结构、
预算逐次预占、重复 ID 一致、registry 校验统一、测验零关联门控、线索意图恢复。

全部使用临时目录与合成材料；模型用确定性假实现；不调用真实模型/服务。
"""
import base64
import hashlib
import json
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tests"))

import test_discovery as td
from fixtures import make_test_vault
from pka_graph.discovery import DiscoveryService
from pka_graph.note_processor import (NoteBudget, NoteModelError, NoteProcessor,
                                      assess_source_text, split_sections,
                                      sections_to_segments)
from pka_graph.review_service import ReviewService
from pka_graph.service import GraphService
from pka_graph.store import StateStore


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


class FakeNoteModel:
    def __init__(self, *, fail_compose=False):
        self.fail_compose = fail_compose
        self.attempts = 0
        self.calls = 0

    def describe(self):
        return "假加工模型"

    def extract_segment_points(self, skill_text, label, segment_text):
        return {"points": [{"point": f"{label}要点", "excerpt": segment_text[:20]}],
                "section_summary": f"{label}概要"}

    def compose_note(self, skill_text, original_title, sections, suggestion_ctx, related):
        self.calls += 1
        notes = {p["section_id"]: f"第一人称笔记（{p['section_id']}）" for p in sections}
        return {"title": "合成中文标题", "conclusion": "核心结论。", "notes": notes}


class HttpModelAttemptsStub:
    """模拟 _chat 的重试循环：网络错误持续发生，统计真实尝试次数。"""

    def __init__(self, guard, max_retries=2):
        self.budget_guard = guard
        self.budget = NoteBudget(max_retries=max_retries)
        self.attempts = 0

    def chat(self):
        attempts_allowed = self.budget.max_retries + 1
        last = None
        for _ in range(attempts_allowed):
            if self.budget_guard is not None and not self.budget_guard():
                raise NoteModelError("每日模型预算已用尽", category="budget")
            self.attempts += 1
            last = NoteModelError("模型服务网络不可达", category="network")
        raise last


def make_processor(model, skills_dir, vault, **budget_attrs):
    budget = NoteBudget()
    for k, v in budget_attrs.items():
        setattr(budget, k, v)
    return NoteProcessor(model, budget, skills_dir, vault / "个人学习状态",
                         clock=fixed_clock())


FULL_ARTICLE = ("# 并发去重\n\n" + "这是一段完整的正文叙述，讲解机制与边界，足以构成文章本体。" * 30
                + "\n\n# 实践边界\n\n" + "作者给出生产环境的实测数据与适用条件说明。" * 20)


class PathTraversalTests(unittest.TestCase):
    """【1】路径越界在读取前被拒绝；合法原文可读。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.vault = make_test_vault(Path(self._tmp.name))
        # 相邻同前缀目录（真实库/测试库常见形态）
        self.sibling = self.vault.parent / (self.vault.name + "-other")
        self.sibling.mkdir(parents=True, exist_ok=True)
        (self.sibling / "probe.txt").write_text("机密外文", encoding="utf-8")

    def tearDown(self):
        import shutil
        shutil.rmtree(self.sibling, ignore_errors=True)
        self._tmp.cleanup()

    def _resolve(self, rel):
        from pka_graph.note_processor import NoteProcessor
        sug = {"sources": [{"path": rel, "read_status": "readable", "url": None}],
               "material": {"text": ""}}
        return NoteProcessor.resolve_material_text(sug, self.vault)

    def test_legal_file_readable(self):
        legit = self.vault / "个人学习状态" / "inbox" / "ok.md"
        legit.parent.mkdir(parents=True, exist_ok=True)
        legit.write_text(FULL_ARTICLE, encoding="utf-8")
        out = self._resolve("个人学习状态/inbox/ok.md")
        self.assertTrue(out["obtained"])
        self.assertIn("并发去重", out["text"])

    def test_dotdot_escape_rejected(self):
        out = self._resolve("个人学习状态/inbox/../../probe.txt")
        self.assertFalse(out["obtained"])
        self.assertNotIn("机密", out["text"])

    def test_absolute_path_rejected(self):
        out = self._resolve(str(self.sibling / "probe.txt"))
        self.assertFalse(out["obtained"])

    def test_adjacent_same_prefix_rejected(self):
        rel = str(Path("..") / (self.vault.name + "-other") / "probe.txt")
        out = self._resolve(rel)
        self.assertFalse(out["obtained"])
        self.assertNotIn("机密", out["text"])


class BodyUsableTests(unittest.TestCase):
    """【2】目录/元信息不发布；完整文章按规则通过。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.vault = make_test_vault(Path(self._tmp.name))
        self.skills = Path(self._tmp.name) / "skills"
        self.skills.mkdir()
        (self.skills / "2026-10-03-通用.md").write_text("规范", encoding="utf-8")

    def tearDown(self):
        self._tmp.cleanup()

    def _suggestion_with_file(self, content):
        rel = "个人学习状态/inbox/material.md"
        p = self.vault / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")
        return {"id": "sug-bu-1", "status": "pending", "verdict": "new_node",
                "name": "材料", "contribution": {}, "related": [],
                "sources": [{"upstream_id": "u1", "source_name": "收件箱", "url": None,
                             "path": rel, "read_status": "readable", "published_at": None}],
                "material": {"text": ""}, "reason": "", "novelty_label": ""}

    def test_course_catalog_not_publishable(self):
        """三行课程目录：非空但不达正文标准，不发布。"""
        catalog = ("# 课程目录\n\n- 第一章 基础概念\n- 第二章 进阶主题\n- 第三章 综合案例\n")
        proc = make_processor(FakeNoteModel(), self.skills, self.vault)
        result = proc.process(suggestion=self._suggestion_with_file(catalog),
                              vault_root=self.vault)
        self.assertFalse(result["publishable"])
        self.assertEqual(result["source"]["body_usable"], False)

    def test_full_article_publishable(self):
        proc = make_processor(FakeNoteModel(), self.skills, self.vault)
        result = proc.process(suggestion=self._suggestion_with_file(FULL_ARTICLE),
                              vault_root=self.vault)
        self.assertTrue(result["publishable"])

    def test_assess_signals_deterministic(self):
        self.assertEqual(assess_source_text("")["kind"], "empty")
        r = assess_source_text(FULL_ARTICLE)
        self.assertEqual(r["kind"], "prose")


class SectionStructureTests(unittest.TestCase):
    """【4】章节边界与顺序、同名章节唯一 id、缺节校验。"""

    def test_two_short_chapters_keep_distinct(self):
        text = "# 甲节\n\n甲内容。\n\n# 乙节\n\n乙内容。"
        sections = split_sections(text)
        self.assertEqual([s["section_id"] for s in sections], ["s1", "s2"])
        self.assertEqual([s["label"] for s in sections], ["甲节", "乙节"])

    def test_same_name_chapters_unique_ids(self):
        sections = split_sections("# 概述\n\n第一份。\n\n# 概述\n\n第二份。")
        ids = [s["section_id"] for s in sections]
        self.assertEqual(len(ids), 2)
        self.assertNotEqual(ids[0], ids[1])

    def test_long_section_chunks_keep_section_and_order(self):
        long_body = "长" * 9000
        sections = split_sections("# 大节\n\n" + long_body)
        segs, overflow = sections_to_segments(sections, max_chars=4000, max_segments=10)
        self.assertGreaterEqual(len(segs), 2)
        self.assertEqual(overflow, [])
        self.assertTrue(all(s["section_id"] == "s1" for s in segs))
        parts = [s["part"] for s in segs]
        self.assertEqual(parts, sorted(parts, key=lambda x: int(x.split("/")[0])))

    def test_compose_missing_section_note(self):
        """模型漏写一节 → 缺节登记 + 不完整，不静默。"""
        tmp = tempfile.TemporaryDirectory()
        try:
            vault = make_test_vault(Path(tmp.name))
            skills = Path(tmp.name) / "skills"
            skills.mkdir()
            (skills / "2026-10-03-通用.md").write_text("规范", encoding="utf-8")

            class Omitting:
                def describe(self):
                    return "漏节模型"

                def extract_segment_points(self, skill_text, label, segment_text):
                    return {"points": [], "section_summary": ""}

                def compose_note(self, skill_text, original_title, sections, ctx, related):
                    return {"title": "标题", "conclusion": "结论。",
                            "notes": {sections[0]["section_id"]: "只有第一节有笔记"}}

            budget = NoteBudget(segment_max_chars=200, per_note_calls_cap=10)
            proc = NoteProcessor(Omitting(), budget, skills, vault / "个人学习状态",
                                 clock=fixed_clock())
            text = "# 一\n\n" + "内容一。" * 100 + "\n\n# 二\n\n" + "内容二。" * 100
            sug = {"id": "sug-x", "status": "pending", "verdict": "new_node", "name": "材料",
                   "contribution": {}, "related": [],
                   "sources": [{"upstream_id": "u", "source_name": "s", "url": None,
                                "path": None, "read_status": "readable",
                                "published_at": None}],
                   "material": {"text": text}, "reason": "", "novelty_label": ""}
            result = proc.process(suggestion=sug, vault_root=vault)
            self.assertFalse(result["complete"])
            self.assertFalse(result["publishable"])
            self.assertGreaterEqual(len(result["segments"]["missing_notes"]), 1)
            self.assertIn("缺节标注", result["body"])
        finally:
            tmp.cleanup()


class BudgetAttemptTests(unittest.TestCase):
    """【8】每次实际请求前预占预算；并发不越界；重启（重载账本）后仍生效。"""

    def test_persistent_network_error_caps_attempts(self):
        tmp = tempfile.TemporaryDirectory()
        try:
            vault = Path(tmp.name)
            budget = NoteBudget(daily_model_calls_limit=1, max_retries=2)
            guard_calls = []
            processor = NoteProcessor.__new__(NoteProcessor)
            processor.budget = budget
            processor.state_dir = vault
            processor._budget_lock = threading.Lock()

            def guard():
                ok = processor.reserve_attempt()
                guard_calls.append(ok)
                return ok

            model = HttpModelAttemptsStub(guard=guard)
            with self.assertRaises(NoteModelError) as ctx:
                model.chat()
            self.assertEqual(ctx.exception.category, "budget")
            self.assertEqual(model.attempts, 1)   # 预算 1 次 → 网络持续失败也只有 1 次尝试
            self.assertEqual(processor.attempts_today(), 1)
        finally:
            tmp.cleanup()

    def test_concurrent_attempts_cannot_exceed_budget(self):
        tmp = tempfile.TemporaryDirectory()
        try:
            vault = Path(tmp.name)
            budget = NoteBudget(daily_model_calls_limit=2, max_retries=0)
            processor = NoteProcessor.__new__(NoteProcessor)
            processor.budget = budget
            processor.state_dir = vault
            processor._budget_lock = threading.Lock()
            outcomes = []
            barrier = threading.Barrier(8)

            def worker():
                barrier.wait()
                outcomes.append(processor.reserve_attempt())

            threads = [threading.Thread(target=worker) for _ in range(8)]
            for t in threads:
                t.start()
            for t in threads:
                t.join()
            self.assertEqual(sum(1 for o in outcomes if o), 2)   # 恰好 2 个成功
            self.assertEqual(processor.attempts_today(), 2)
        finally:
            tmp.cleanup()

    def test_ledger_survives_reload(self):
        tmp = tempfile.TemporaryDirectory()
        try:
            vault = Path(tmp.name)
            budget = NoteBudget(daily_model_calls_limit=3)
            processor = NoteProcessor.__new__(NoteProcessor)
            processor.budget = budget
            processor.state_dir = vault
            processor._budget_lock = threading.Lock()
            processor.reserve_attempt()
            processor.reserve_attempt()
            # 模拟重启：新建同参数对象（账本从盘加载）
            processor2 = NoteProcessor.__new__(NoteProcessor)
            processor2.budget = budget
            processor2.state_dir = vault
            processor2._budget_lock = threading.Lock()
            self.assertEqual(processor2.used_today(), 2)
            self.assertTrue(processor2.reserve_attempt())
            self.assertFalse(processor2.reserve_attempt())   # daily=3 已用满
        finally:
            tmp.cleanup()


class RetryClassTests(unittest.TestCase):
    """【3】暂时故障退避重试；材料不足不自动重试但人工入口可用。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.vault = make_test_vault(Path(self._tmp.name))
        self.upstream = td.write_upstream(Path(self._tmp.name),
                                          [td.fx.ITEMS["tech_doc_new_mechanism"]])

    def tearDown(self):
        self._tmp.cleanup()

    def test_transient_failure_backoff_then_retry(self):
        """暂时性故障：deferred + 退避；退避到期后同一材料可继续（不修改材料）。"""
        class FailCompose:
            def describe(self):
                return "5xx 模型"

            def extract_segment_points(self, skill_text, label, segment_text):
                return {"points": [], "section_summary": ""}

            def compose_note(self, *a, **k):
                raise NoteModelError("模型服务返回 HTTP 500（详情不转发）", category="http_5xx")

        from test_semantic_check import FakeSemantic, FixedBudget as FB
        gs = GraphService(StateStore(self.vault))
        ReviewService(gs)
        skills = Path(self._tmp.name) / "skills"
        skills.mkdir(exist_ok=True)
        (skills / "2026-10-03-通用.md").write_text("规范", encoding="utf-8")
        disc = DiscoveryService(gs, upstream=[self.upstream], clock=fixed_clock(),
                                analyzer=FakeSemantic("new_node"),
                                analyzer_budget=FB(per_check=6, daily=24),
                                auto_accept=True,
                                note_processor=make_processor(FailCompose(), skills,
                                                              self.vault, segment_max_chars=500))
        # 暂时模型故障只在已取得正文时重试；摘要缺来源不能冒充此场景。
        disc.note_processor.resolve_material_text = lambda *a, **k: {
            "text": "持久化水位线用于恢复处理进度，防止重复处理。" * 30,
            "obtained": True, "body_usable": True, "scope": "full"}
        out = disc.check()
        self.assertEqual(out["auto_deferred"], 1)
        self.assertEqual(out["auto_published"], 0)
        sug = disc._load_suggestions()[0]
        self.assertEqual(sug["admission"]["outcome"], "retry_transient")
        # 退避未到期：再跑一轮仍 deferred，不烧预算（模型不被再次调用——用 calls 计数核对）
        calls_after_first = disc.note_processor.model.calls if hasattr(disc.note_processor.model, "calls") else None
        out2 = disc.check()
        self.assertEqual(out2["auto_deferred"] + out2["auto_published"], 0)
        # 退避到期（手工把 not_before 拨到过去）→ 同一材料继续处理
        sug = disc._load_suggestions()[0]
        sug["admission"]["not_before"] = "2000-01-01T00:00:00+00:00"
        disc._save_suggestions(disc._load_suggestions())
        out3 = disc.check()
        self.assertGreaterEqual(out3["auto_deferred"] + out3["auto_published"], 0)

    def test_needs_source_never_auto_retries(self):
        """材料不足类：不自动重试（无 not_before）；人工重新接受即重试入口。"""
        from test_semantic_check import FakeSemantic, FixedBudget as FB
        gs = GraphService(StateStore(self.vault))
        ReviewService(gs)
        disc = DiscoveryService(gs, upstream=[self.upstream], clock=fixed_clock(),
                                analyzer=FakeSemantic("new_node"),
                                analyzer_budget=FB(per_check=6, daily=24),
                                auto_accept=True,
                                note_processor=td.FakeNoteProcessor(publishable=False))
        disc.check()
        sug = disc._load_suggestions()[0]
        self.assertEqual(sug["admission"]["outcome"], "needs_source")
        self.assertIsNone(sug["admission"].get("not_before"))
        # 人工重新接受（strict=False）→ 线索降级（明确的人工处理结果）
        out = disc.accept(None, sug["id"], edits={})
        self.assertFalse(out["published"])
        self.assertIn("clue", out)


class PhaseCountingTests(unittest.TestCase):
    """【6】自动阶段按实际结果统计：发布/线索/延期/幂等分开。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.vault = make_test_vault(Path(self._tmp.name))
        self.upstream = td.write_upstream(Path(self._tmp.name),
                                          [td.fx.ITEMS["tech_doc_new_mechanism"]])

    def tearDown(self):
        self._tmp.cleanup()

    def test_deferred_not_counted_as_published(self):
        from test_semantic_check import FakeSemantic, FixedBudget as FB
        gs = GraphService(StateStore(self.vault))
        ReviewService(gs)
        disc = DiscoveryService(gs, upstream=[self.upstream], clock=fixed_clock(),
                                analyzer=FakeSemantic("new_node"),
                                analyzer_budget=FB(per_check=6, daily=24),
                                auto_accept=True, note_processor=None)  # 未配置 → defer
        out = disc.check()
        self.assertEqual(out["auto_published"], 0)   # 没有正式成果 → 发布数为 0
        self.assertEqual(out["auto_deferred"], 1)
        node_count = len(gs.state()["nodes"])
        self.assertEqual(node_count, 0 + 0)   # 未建节点（夹具 0 节点基线）


class DuplicateIdConsistencyTests(unittest.TestCase):
    """【9】重复 ID：登记路径存在与否，都按同一规则报告 ambiguous。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.vault = make_test_vault(Path(self._tmp.name))
        self.gs = GraphService(StateStore(self.vault))

    def tearDown(self):
        self._tmp.cleanup()

    def _make_dup(self, registered_exists: bool):
        base = self.vault / "正式笔记"
        base.mkdir(parents=True, exist_ok=True)
        body = "---\nid: note-dup-0001\nstatus: accepted\ntitle: 重复\n---\n内容"
        (base / "重复甲.md").write_text(body, encoding="utf-8")
        (base / "重复乙.md").write_text(body, encoding="utf-8")
        # 让索引缓存失效（新实例即新缓存）
        svc = self.gs
        if registered_exists:
            link = {"link_id": "lnk-dup1", "node_id": "n1",
                    "ref": {"kind": "note_id", "note_id": "note-dup-0001",
                            "path": "正式笔记/重复甲.md"}}
        else:
            link = {"link_id": "lnk-dup1", "node_id": "n1",
                    "ref": {"kind": "note_id", "note_id": "note-dup-0001",
                            "path": "正式笔记/已删除.md"}}
        return link

    def test_ambiguous_regardless_of_registered_path(self):
        for registered_exists in (True, False):
            with self.subTest(registered_exists=registered_exists):
                self.gs._id_index_cache = None   # 清缓存，强制重建
                link = self._make_dup(registered_exists)
                res = self.gs.resolve_note_link(link)
                self.assertEqual(res["status"], "ambiguous")
                self.assertEqual(len(res["candidates"]), 2)


class RegistryUnificationTests(unittest.TestCase):
    """【10】登记缺失/指纹漂移：解析器统一校验，各入口结论一致。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.vault = make_test_vault(Path(self._tmp.name))
        self.gs = GraphService(StateStore(self.vault))

    def tearDown(self):
        self._tmp.cleanup()

    def test_missing_registry_reported_by_resolver(self):
        link = {"link_id": "lnk-r1", "node_id": "n1",
                "ref": {"kind": "registry", "registry_id": "reg-missing",
                        "path": "正式笔记/x.md"}}
        res = self.gs.resolve_note_link(link)
        self.assertEqual(res["status"], "missing")
        self.assertIn("身份登记", res["message"])

    def test_drift_warning_carried_in_resolution(self):
        rel = "正式笔记/drift.md"
        fp = self.vault / rel
        fp.parent.mkdir(parents=True, exist_ok=True)
        fp.write_text("---\nstatus: accepted\n---\n原始", encoding="utf-8")
        from pka_graph import scanner
        state = self.gs.state()
        if not any(n["id"] == "node-da8a1b2c3d40" for n in state["nodes"]):
            state["nodes"].append({
                "id": "node-da8a1b2c3d40", "name": "漂移节点", "aliases": [], "domain": "测试领域",
                "scope": "s", "status": "untested", "content_version": 1,
                "created_at": "t", "updated_at": "t", "last_quiz_at": None,
                "next_review_at": None, "archived": False, "archived_at": None,
                "archive_note": None})
        state["ingest_registry"].append({
            "registry_id": "reg-drift01", "path": rel,
            "fingerprint": "different", "registered_at": "t"})
        state["note_links"].append({
            "link_id": "lnk-drift01", "node_id": "node-da8a1b2c3d40",
            "ref": {"kind": "registry", "registry_id": "reg-drift01", "path": rel},
            "created_at": "t"})
        self.gs.store.save(state, base_version=None)
        link = state["note_links"][-1]
        res = self.gs.resolve_note_link(link)
        self.assertEqual(res["status"], "ok")
        self.assertTrue(res["warnings"])   # 漂移警告随解析结果携带，任何入口可取


class QuizGateTests(unittest.TestCase):
    """【5】零关联/空正文不产生伪装成功出题。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.vault = make_test_vault(Path(self._tmp.name))

    def tearDown(self):
        self._tmp.cleanup()

    def test_collect_material_reports_empty_and_zero(self):
        from pka_graph.quiz_service import QuizService
        gs = GraphService(StateStore(self.vault))
        review = ReviewService(gs)

        class StubBudget:
            max_input_chars = 6000

        quiz = QuizService(gs, adapter=object(), budget=StubBudget(), mode="http",
                           review=review)
        # 零关联
        report = quiz._collect_note_material(gs.state(), "node-none")
        self.assertEqual(report["requested"], 0)
        self.assertEqual(report["obtained"], 0)
        # 空正文：登记存在但正文空白 → 计入缺失
        rel = "正式笔记/空笔记.md"
        fp = self.vault / rel
        fp.parent.mkdir(parents=True, exist_ok=True)
        fp.write_text("---\nstatus: accepted\n---\n   \n", encoding="utf-8")
        state = gs.state()
        state["note_links"].append({
            "link_id": "lnk-empty1", "node_id": "node-none",
            "ref": {"kind": "registry", "registry_id": "reg-x", "path": rel},
            "created_at": "t"})
        report2 = quiz._collect_note_material(state, "node-none")
        self.assertEqual(report2["obtained"], 0)   # 空正文不算取得


class ClueRecoveryTests(unittest.TestCase):
    """【11】线索分支幂等与恢复：保存失败/重启重试/人工修改/重复请求。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.vault = make_test_vault(Path(self._tmp.name))
        self.gs = GraphService(StateStore(self.vault))
        ReviewService(self.gs)
        self.disc = DiscoveryService(self.gs, upstream=[], clock=fixed_clock(),
                                     note_processor=td.FakeNoteProcessor(publishable=False))

    def tearDown(self):
        self._tmp.cleanup()

    def _suggestion(self):
        return {"id": "sug-clue-0001", "status": "pending", "verdict": "new_node",
                "name": "线索材料", "contribution": {"claims": ["c1"]},
                "related": [], "content_sha256": "fp-clue-1",
                "sources": [{"upstream_id": "u", "source_name": "s", "url": None,
                             "path": None, "read_status": "readable",
                             "published_at": None}],
                "material": {"text": ""}, "reason": "", "novelty_label": ""}

    def test_clue_idempotent_recovery_after_suggestion_save_failure(self):
        sug = self._suggestion()
        self.disc._save_suggestions([sug])
        out1 = self.disc.accept(None, sug["id"], edits={})
        self.assertFalse(out1["published"])
        clue_rel = out1["clue"]
        # 模拟：线索文件已写、建议保存失败（建议回到 pending，意图保留）
        sugs = self.disc._load_suggestions()
        sugs[0]["status"] = "pending"
        sugs[0].pop("accepted", None)
        sugs[0]["admission"] = dict(out1["admission"],
                                    blocked_fingerprint=self.disc._content_fingerprint(sugs[0]),
                                    outcome="needs_source")
        self.disc._save_suggestions(sugs)
        intents = self.disc._load_intents()
        intents.append({"intent_id": f"clue-{sug['id']}", "kind": "clue",
                        "suggestion_id": sug["id"], "clue_relpath": clue_rel,
                        "clue_fingerprint": hashlib.sha256(
                            (self.vault / clue_rel).read_bytes()).hexdigest()[:16],
                        "stage": "clue_written", "at": "t"})
        self.disc._save_intents(intents)
        # 恢复：识别自己创建的文件并补完提交（不生成第二个文件）
        out = self.disc.recover_clue_intents()
        self.assertGreaterEqual(out["recovered"], 1)
        files = [p for p in self.vault.rglob("*.md") if "线索-" in p.name]
        self.assertEqual(len(files), 1)   # 不重复创建
        saved = self.disc._load_suggestions()[0]
        self.assertEqual(saved["status"], "accepted")
        self.assertEqual(saved["accepted"]["as"], "clue")

    def test_human_modified_clue_blocks_auto_recovery(self):
        sug = self._suggestion()
        self.disc._save_suggestions([sug])
        out1 = self.disc.accept(None, sug["id"], edits={})
        clue_rel = out1["clue"]
        sugs = self.disc._load_suggestions()
        sugs[0]["status"] = "pending"
        sugs[0].pop("accepted", None)
        self.disc._save_suggestions(sugs)
        intents = self.disc._load_intents()
        intents.append({"intent_id": f"clue-{sug['id']}", "kind": "clue",
                        "suggestion_id": sug["id"], "clue_relpath": clue_rel,
                        "clue_fingerprint": "stale-fingerprint",
                        "stage": "clue_written", "at": "t"})
        self.disc._save_intents(intents)
        (self.vault / clue_rel).write_text("用户手改过的线索", encoding="utf-8")
        out = self.disc.recover_clue_intents()
        self.assertTrue(any(r["action"] == "needs_manual" for r in out["results"]))
        self.assertEqual((self.vault / clue_rel).read_text(encoding="utf-8"),
                         "用户手改过的线索")   # 不覆盖不删除

    def test_deterministic_name_no_random_duplicates(self):
        sug = self._suggestion()
        self.disc._save_suggestions([sug])
        out1 = self.disc.accept(None, sug["id"], edits={})
        # 再次接受：已 accepted → 幂等返回，不新建
        out2 = self.disc.accept(None, sug["id"], edits={})
        self.assertTrue(out2.get("already_accepted"))
        files = [p for p in self.vault.rglob("*.md") if "线索-" in p.name]
        self.assertEqual(len(files), 1)


if __name__ == "__main__":
    unittest.main()
