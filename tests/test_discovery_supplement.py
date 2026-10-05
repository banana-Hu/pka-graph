# -*- coding: utf-8 -*-
"""第六阶段补充测试：语义分析校验、崩溃恢复、周期检查语义、来源隔离。"""

import json
import sys
import tempfile
import threading
import time
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tests"))

from fixtures import make_test_vault
from pka_graph.discovery import DiscoveryService
from pka_graph.models import ValidationError
from pka_graph.discovery_model import (
    DiscoveryBudget,
    HttpDiscoveryAnalyzer,
    SemanticModelError,
    build_discovery_analyzer_from_env,
    validate_semantic_output,
)
from pka_graph.quiz_model import Budget, FakeModelAdapter
from pka_graph.quiz_service import QuizError, QuizService
from pka_graph.review_service import ReviewService
from pka_graph.service import GraphService
from pka_graph.store import StateStore


def fixed_clock():
    return datetime(2026, 10, 2, 9, 0, tzinfo=timezone.utc)


def make_full(tmp_name=None):
    tmp = tempfile.TemporaryDirectory()
    vault = make_test_vault(Path(tmp.name))
    (vault / ".pka-test-vault.json").write_text(json.dumps({"purpose": "test"}), encoding="utf-8")
    gs = GraphService(StateStore(vault))
    review = ReviewService(gs)
    disc = DiscoveryService(gs, upstream=[], clock=fixed_clock)
    return tmp, gs, review, disc


class SemanticValidationTests(unittest.TestCase):
    MATERIAL = ("RingAttn 提出一种环形注意力分块通信机制处理超长序列，"
                "显存峰值显著下降，实验表明通信开销降低 40%。")

    def test_valid_output_passes(self):
        payload = {
            "classification": "new_node",
            "contribution": {"problem": "长序列显存", "claims": ["环形分块"]},
            "source_excerpts": [{"upstream_id": "up-1",
                                 "excerpt": "环形注意力分块通信机制"}],
            "unknowns": ["未在真实集群验证"],
        }
        out = validate_semantic_output(payload, self.MATERIAL, [])
        self.assertEqual(out["classification"], "new_node")
        self.assertTrue(all(e["located"] for e in out["source_excerpts"]))

    def test_unlocatable_excerpt_downgrades_to_uncertain(self):
        payload = {"classification": "new_node",
                   "source_excerpts": [{"upstream_id": "up-1", "excerpt": "材料里根本没有这句话"}]}
        out = validate_semantic_output(payload, self.MATERIAL, [])
        self.assertEqual(out["classification"], "uncertain")
        self.assertEqual(out["unlocated_excerpts"], 1)
        self.assertIn("已降级为 uncertain", " ".join(out["unknowns"]))

    def test_invalid_node_reference_dropped(self):
        payload = {"classification": "revision",
                   "differences_vs_related": [{"node_id": "node-nononono", "what_differs": "x"}]}
        out = validate_semantic_output(payload, self.MATERIAL,
                                       [{"node_id": "node-aaaa11111111"}])
        self.assertEqual(out["dropped_invalid_differences"], 1)
        self.assertEqual(out["differences_vs_related"], [])

    def test_illegal_classification_rejected(self):
        with self.assertRaises(SemanticModelError):
            validate_semantic_output({"classification": "passed"}, self.MATERIAL, [])

    def test_missing_fields_stay_empty(self):
        """原文没有的内容保持空——不把任意子句塞进字段制造完整感。"""
        out = validate_semantic_output({"classification": "uncertain"}, self.MATERIAL, [])
        self.assertIsNone(out["contribution"]["problem"])
        self.assertEqual(out["contribution"]["claims"], [])


class UnconfiguredAnalyzerTests(unittest.TestCase):
    def test_prescreen_runs_and_semantic_marked_unconfigured(self):
        tmp, gs, review, disc = make_full()
        gs.create_node(name="占位节点", domain="D", scope="S")
        out = disc.check()
        self.assertEqual(out["analysis"]["rule_prescreen"], "已执行（确定性规则：可解释、可回归；不能认定知识增量已核实）")
        self.assertEqual(out["analysis"]["semantic_analysis"]["status"], "unconfigured")
        self.assertIn("未执行", out["analysis"]["semantic_analysis"]["reason"])
        listing = disc.list_suggestions(status="all")
        self.assertEqual(listing["analysis"]["semantic_analysis"]["status"], "unconfigured")
        self.assertIn("确定性规则", listing["analysis"]["rule_prescreen"])

    def test_env_incomplete_never_fakes_success(self):
        """缺配置不能触发假模型并假装语义分析成功。"""
        import os

        env = {"PKA_DISCOVERY_ADAPTER": "http", "PKA_DISCOVERY_API_BASE": "https://x/v1",
               "PKA_DISCOVERY_DAILY_LIMIT": "24"}
        clean = {k: v for k, v in env.items()}
        with patch.dict(os.environ, clean, clear=False):
            for k in ("PKA_DISCOVERY_API_KEY", "PKA_DISCOVERY_MODEL"):
                os.environ.pop(k, None)
            analyzer, budget, mode, available, reason = build_discovery_analyzer_from_env()
        self.assertIsNone(analyzer)
        self.assertFalse(available)
        self.assertIn("PKA_DISCOVERY_API_KEY", reason)


class CrashRecoveryTests(unittest.TestCase):
    """必测补充：两段提交的进程崩溃恢复（四场景）。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.vault = make_test_vault(Path(self._tmp.name))
        (self.vault / ".pka-test-vault.json").write_text(json.dumps({"purpose": "test"}), encoding="utf-8")
        self.gs = GraphService(StateStore(self.vault))
        self.review = ReviewService(self.gs)
        self.disc = DiscoveryService(self.gs, upstream=[], clock=fixed_clock)

    def tearDown(self):
        self._tmp.cleanup()

    def _accept_with_failpoint(self):
        """模拟崩溃：笔记已写、意图已登记、state 未提交。返回 (suggestion_id, node)。"""
        node = {"id": "node-cafe00001234", "name": "崩溃恢复测试节点", "aliases": [],
                "domain": "AI技术领域", "scope": "崩溃恢复的范围", "status": "untested",
                "content_version": 1}
        note_name = "discovery-sug-crash01.md"
        # 正常调用 _write_candidate_note（会生成带随机 note_id 的候选笔记）
        result = self.disc._write_candidate_note(
            {"id": "sug-crash01", "verdict": "new_node", "reason": "", "novelty_label": "",
             "contribution": {}, "sources": []},
            node, {}, note_name)
        # 用实际文件内容计算指纹（模拟真实 accept 的行为）
        import hashlib as _h

        fp = _h.sha256(result["path_obj"].read_bytes()).hexdigest()[:16]
        # 记录意图（state 未提交 = 崩溃点）
        intents = self._load_intents_fresh()
        intents.append({
            "intent_id": "intent-sug-crash01", "suggestion_id": "sug-crash01",
            "node": node, "note_name": note_name,
            "note_relpath": str(result["path_obj"].relative_to(self.gs.store.vault_root)),
            "note_id": result["note_id"], "note_fingerprint": fp,
            "stage": "note_written", "at": "2026-10-02T09:00:00+00:00",
        })
        self._save_intents_fresh(intents)
        # 保存建议种子（accept 需要找到它）
        sug_seed = {"id": "sug-crash01", "verdict": "new_node", "status": "pending",
                    "name": "崩溃恢复测试节点", "novelty": {}, "novelty_label": "",
                    "contribution": {"claims": ["崩溃恢复的贡献声明"]},
                    "related": [], "sources": [{"upstream_id": "up-c1", "url": "u"}],
                    "reason": "", "material": {"level": "readable_summary", "text": "x", "note": ""}}
        self.disc._save_suggestions([sug_seed])
        fresh = DiscoveryService(self.gs, upstream=[], clock=fixed_clock)
        return "sug-crash01", fresh

    def _load_intents_fresh(self):
        p = self.gs.store.state_path.parent / "discovery_intents.json"
        if p.exists():
            return json.loads(p.read_text(encoding="utf-8"))
        return []

    def _save_intents_fresh(self, intents):
        p = self.gs.store.state_path.parent / "discovery_intents.json"
        p.write_text(json.dumps(intents, ensure_ascii=False, indent=2), encoding="utf-8")

    def test_crash_1_note_written_state_missing_recovered(self):
        """场景 1：笔记已写、state 未提交 → 重启后补完提交，不重复生成。"""
        sug_id, fresh = self._accept_with_failpoint()
        report = fresh.recover_intents()
        action = report["recovered"][0]["action"]
        self.assertIn(action, ("completed", "already_committed"))
        nodes = [n for n in self.gs.state()["nodes"] if n["name"] == "崩溃恢复测试节点"]
        self.assertEqual(len(nodes), 1)
        note = next(self.vault.rglob("discovery-sug-crash01.md"), None)
        self.assertIsNotNone(note)
        self.assertTrue(note.exists())
        self.assertTrue(self.gs.state()["note_links"])  # 关联已恢复

    def test_crash_2_state_committed_response_missing(self):
        """场景 2：state 已提交但响应未返回 → 重启识别为已完成，不重复写。"""
        sug_seed = {"id": "sug-crash02", "verdict": "new_node", "status": "accepted",
                    "name": "已完成节点", "novelty": {}, "novelty_label": "",
                    "contribution": {}, "related": [], "sources": [],
                    "reason": "", "material": {}}
        self.disc._save_suggestions([sug_seed])
        node = self.gs.create_node(name="已完成节点", domain="D", scope="S")["node"]
        from pka_graph.models import make_note_link

        st = self.gs.state()
        st["note_links"].append(
            make_note_link(node["id"], {"kind": "note_id", "note_id": "note-20991231-002"}))
        self.gs.store.save(st, base_version=st["version"])
        # 手动登记一个"残留"意图（note_id 与 link 一致 → 已实际提交）
        self.disc._save_intents([{
            "intent_id": "intent-sug-crash02", "suggestion_id": "sug-crash02",
            "node": node, "note_name": "discovery-sug-crash02.md",
            "note_id": "note-20991231-002", "note_fingerprint": None,
            "stage": "state_committed", "at": "2026-10-02T09:00:00+00:00",
        }])
        fresh = DiscoveryService(self.gs, upstream=[], clock=fixed_clock)
        report = fresh.recover_intents()
        self.assertEqual(report["recovered"][0]["action"], "already_committed")
        self.assertEqual(len([n for n in self.gs.state()["nodes"]
                              if n["name"] == "已完成节点"]), 1)

    def test_crash_3_retry_after_restart_no_duplicate(self):
        """场景 3：重启后重试同一次接受 → 节点唯一、笔记唯一。"""
        sug_id, fresh = self._accept_with_failpoint()
        fresh.recover_intents()
        # 重试接受 → 建议已 accepted → 幂等返回既有结果，不重复建节点
        out = fresh.accept(None, "sug-crash01", edits={}, request_id="req-retry")
        self.assertTrue(out.get("already_accepted"))
        nodes = [n for n in self.gs.state()["nodes"] if n["name"] == "崩溃恢复测试节点"]
        self.assertEqual(len(nodes), 1)  # 恢复后只有一个节点
        notes = list(self.gs.store.vault_root.rglob("discovery-sug-crash01.md"))
        self.assertLessEqual(len(notes), 1)

    def test_crash_4_user_modified_note_not_deleted(self):
        """场景 4：候选笔记已被用户修改 → 不删除、不覆盖，标记需要人工处理。"""
        sug_id, fresh_svc = self._accept_with_failpoint()
        note = next(self.vault.rglob("discovery-sug-crash01.md"), None)
        note.write_text("---\nid: note-user-edited\n---\n用户改过的内容", encoding="utf-8")
        report = fresh_svc.recover_intents()
        actions = [r["action"] for r in report["recovered"]]
        self.assertIn("needs_manual", actions)
        self.assertIn("用户改过的内容", note.read_text(encoding="utf-8"))  # 未被覆盖


class PeriodicCheckTests(unittest.TestCase):
    def test_periodic_semantics(self):
        """周期检查在服务进程内：关闭默认；运行中防重叠；多标签无影响（同一进程）。"""
        tmp, gs, review, disc = make_full()
        cfg = disc.config()
        self.assertEqual(cfg["auto_check_minutes"], 0)  # 默认关闭
        # 手动 check 加锁：并发第二次调用被跳过
        with patch.object(disc, "_check_inner", wraps=disc._check_inner) as spy:
            first = disc.check()
            second = disc.check()
            self.assertEqual(spy.call_count, 2)  # 串行执行各自完成（不重叠场景）
        # 模拟一轮未完成：锁被占用 → 第二次跳过
        acquired = disc.check_lock.acquire(blocking=False)
        self.assertTrue(acquired)
        skipped = disc.check()
        self.assertTrue(skipped.get("skipped"))
        disc.check_lock.release()
        cfg = disc.config()
        self.assertIn("auto_check_minutes", cfg)  # 配置持久；周期属服务进程行为


if __name__ == "__main__":
    unittest.main()
