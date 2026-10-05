"""业务操作测试（必测 2/4/5/6/7/11/12/15/17）。"""

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tests"))

from fixtures import make_test_vault
from pka_graph.models import ValidationError
from pka_graph.service import ConfirmationRequired, GraphService, NotFound
from pka_graph.store import StateStore


class ServiceTestBase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.vault = make_test_vault(Path(self._tmp.name))
        self.svc = GraphService(StateStore(self.vault))
        self.state = self.svc.state

    def tearDown(self):
        self._tmp.cleanup()

    def _create(self, **kw):
        return self.svc.create_node(
            name=kw.get("name", "测试节点"),
            domain=kw.get("domain", "AI技术领域"),
            scope=kw.get("scope", "初始范围"),
            request_id=kw.get("request_id"),
        )["node"]


class NodeLifecycleTests(ServiceTestBase):
    def test_2_importing_note_does_not_change_node_status(self):
        """必测 2：接受/接入笔记不改变节点状态。"""
        node = self._create(scope="Harness 的定位与单元划分")
        node["status"] = "passed"  # 模拟一次内部测验写入的通过状态（非 UI 入口）
        state = self.state()
        for n in state["nodes"]:
            if n["id"] == node["id"]:
                n["status"] = "passed"
        self.svc.store.save(state, base_version=None)
        # 接入正式笔记到该节点
        self.svc.import_apply(
            ["正式笔记/04-Unit1.md"],
            [{"path": "正式笔记/04-Unit1.md", "node_id": node["id"]}],
            request_id="imp-1",
            base_version=None,
        )
        detail = self.svc.node_detail(node["id"])
        self.assertEqual(detail["node"]["status"], "passed")  # 状态未被接入动作改变
        self.assertEqual(len(detail["note_links"]), 1)

    def test_4_duplicate_submit_creates_single_node(self):
        """必测 4：重复提交不重复创建（request_id 幂等）。"""
        payload = dict(name="幂等节点", domain="AI技术领域", scope="边界", request_id="req-dup-1")
        first = self.svc.create_node(**payload)
        second = self.svc.create_node(**payload)
        self.assertEqual(first["node"]["id"], second["node_id"])
        self.assertTrue(second.get("replayed"))
        self.assertEqual(len([n for n in self.state()["nodes"] if n["name"] == "幂等节点"]), 1)

    def test_5_rename_keeps_status(self):
        """必测 5：改名不清除通过状态。"""
        node = self._create()
        state = self.state()
        for n in state["nodes"]:
            if n["id"] == node["id"]:
                n["status"] = "passed"
        self.svc.store.save(state, base_version=None)
        out = self.svc.update_node(node["id"], {"name": "改名后的节点"}, base_version=None)
        self.assertEqual(out["node"]["status"], "passed")
        self.assertEqual(out["node"]["name"], "改名后的节点")
        self.assertEqual(out["scope_changed"], False)

    def test_6_scope_change_requires_confirm_then_untested(self):
        """必测 6：改范围需确认；确认后回 untested。"""
        node = self._create()
        state = self.state()
        for n in state["nodes"]:
            if n["id"] == node["id"]:
                n["status"] = "passed"
        self.svc.store.save(state, base_version=None)
        # 未确认 → 拒绝执行并给出预览
        with self.assertRaises(ConfirmationRequired) as ctx:
            self.svc.update_node(node["id"], {"scope": "完全不同的新范围"}, base_version=None)
        self.assertIn("回到 untested", ctx.exception.preview["effect"])
        # 状态仍未变
        self.assertEqual(self.svc.node_detail(node["id"])["node"]["status"], "passed")
        # 明确确认后 → untested，内容版本 +1
        out = self.svc.update_node(
            node["id"], {"scope": "完全不同的新范围"}, confirm_scope_change=True, base_version=None
        )
        self.assertTrue(out["scope_changed"])
        self.assertEqual(out["node"]["status"], "untested")
        self.assertEqual(out["node"]["content_version"], 2)

    def test_7a_merge_new_state_not_inherited(self):
        """必测 7（合并）：被并入方曾通过，目标节点状态不变、新结构不继承。"""
        passed = self._create(name="旧节点A", scope="A 的原始范围")
        target = self._create(name="保留节点B", scope="B 的范围")
        state = self.state()
        for n in state["nodes"]:
            if n["id"] == passed["id"]:
                n["status"] = "passed"
        self.svc.store.save(state, base_version=None)
        out = self.svc.merge_nodes(passed["id"], target["id"], base_version=None)
        src = self.svc.node_detail(passed["id"])["node"]
        tgt = self.svc.node_detail(target["id"])["node"]
        self.assertTrue(src["archived"])
        self.assertEqual(src["archive_note"]["status_before"], "passed")
        self.assertEqual(tgt["status"], "untested")  # 目标没有被"合并"成通过
        # 恢复原节点 → 结构变更，一律 untested，不自动回到掌握层
        restored = self.svc.restore_node(passed["id"], base_version=None)["node"]
        self.assertEqual(restored["status"], "untested")

    def test_7b_split_new_nodes_untested(self):
        """必测 7（拆分）：新节点一律 untested，即使源节点曾通过。"""
        src = self._create(name="大节点", scope="混合范围")
        state = self.state()
        for n in state["nodes"]:
            if n["id"] == src["id"]:
                n["status"] = "passed"
        self.svc.store.save(state, base_version=None)
        out = self.svc.split_nodes(
            src["id"],
            [{"name": "拆分甲", "scope": "甲范围"}, {"name": "拆分乙", "scope": "乙范围"}],
            base_version=None,
        )
        self.assertTrue(all(n["status"] == "untested" for n in out["new_nodes"]))
        self.assertTrue(self.svc.node_detail(src["id"])["node"]["archived"])

    def test_11_archive_delete_keeps_notes(self):
        """必测 11：删除（归档）节点不动原笔记。"""
        node = self._create()
        note_path = self.vault / "正式笔记" / "04-Unit1.md"
        before = note_path.read_bytes()
        self.svc.import_apply(
            ["正式笔记/04-Unit1.md"],
            [{"path": "正式笔记/04-Unit1.md", "node_id": node["id"]}],
            request_id=None,
            base_version=None,
        )
        self.svc.archive_node(node["id"], base_version=None)
        self.assertEqual(note_path.read_bytes(), before)
        self.assertTrue(note_path.exists())
        self.assertEqual(len(self.svc.node_detail(node["id"])["note_links"]), 1)  # 关联仍在归档节点上

    def test_12_merge_relations_intact_no_dangling_no_self(self):
        """必测 12：合并后关系完整——无悬挂引用、无自连接、重复关系去重。"""
        a = self._create(name="A")
        b = self._create(name="B")
        c = self._create(name="C")
        self.svc.add_relation(a["id"], c["id"], "related", base_version=None)      # 迁移后 B—C
        self.svc.add_relation(a["id"], b["id"], "prerequisite", base_version=None)  # 迁移后成自连接 → 丢弃
        self.svc.add_relation(b["id"], c["id"], "related", base_version=None)       # 与迁移结果重复 → 丢弃
        out = self.svc.merge_nodes(a["id"], b["id"], base_version=None)
        state = self.state()
        ids = {n["id"] for n in state["nodes"]}
        for link in state["node_links"]:
            self.assertIn(link["from_id"], ids)
            self.assertIn(link["to_id"], ids)
            self.assertNotEqual(link["from_id"], link["to_id"])
        self.assertIn(out["archived"], ids)  # 原节点仍在（可恢复）
        # B—C 的 related 只剩一条
        bc = [l for l in state["node_links"] if {l["from_id"], l["to_id"]} == {b["id"], c["id"]}
              and l["relation"] == "related"]
        self.assertEqual(len(bc), 1)

    def test_15_merge_failure_no_half_state(self):
        """必测 15：多对象操作失败后没有半完成状态。"""
        before_state = self.state()
        with self.assertRaises(NotFound):
            self.svc.merge_nodes("node-" + "e" * 12, "node-" + "f" * 12, base_version=None)
        self.assertEqual(self.state(), before_state)

    def test_17_scan_and_import_idempotent(self):
        """必测 17：扫描与导入重复执行结果一致。"""
        r1 = self.svc.scan("test")
        r2 = self.svc.scan("test")
        self.assertEqual(r1["target_notes"], r2["target_notes"])
        node = self._create()["id"]
        payload = dict(
            paths=["候选笔记/00-知识地图.md"],
            assignments=[{"path": "候选笔记/00-知识地图.md", "node_id": node}],
            request_id="imp-same",
            base_version=None,
        )
        first = self.svc.import_apply(**payload)
        second = self.svc.import_apply(**payload)
        self.assertTrue(second.get("replayed"))
        self.assertEqual(first["note_links"], second["note_links"])
        # 第三次不带 request_id：内容级幂等 → 不产生重复登记/关联
        self.svc.import_apply(payload["paths"], payload["assignments"], request_id=None, base_version=None)
        self.assertEqual(len(self.state()["note_links"]), 1)


class MiscServiceTests(ServiceTestBase):
    def test_import_preview_reports_conflict_note(self):
        """重复 ID 笔记：预览可见其条目（冲突本身由扫描层报告，见 test_scanner）；
        接入服务不因重复 ID 崩溃，其 ID 不可作为可信关联依据——关联走登记路径时
        以路径+指纹为准。"""
        preview = self.svc.import_preview(["候选笔记/重复ID甲.md"])
        entry = preview["plan"][0]
        self.assertEqual(entry["note_id"], "note-20260101-999")
        self.assertIn("candidate", (entry["audit_status"] or ""))

    def test_unknown_node_update_404(self):
        with self.assertRaises(NotFound):
            self.svc.update_node("node-" + "0" * 12, {"name": "x"}, base_version=None)

    def test_review_fields_nullable_and_untouched(self):
        """规则 12：最近测验/下次复习时间仅预留可空字段，本阶段任何操作都不写它们。"""
        node = self._create()
        self.assertIsNone(node["last_quiz_at"])
        self.assertIsNone(node["next_review_at"])
        out = self.svc.update_node(node["id"], {"name": "改名"}, base_version=None)
        self.assertIsNone(out["node"]["last_quiz_at"])
        self.assertIsNone(out["node"]["next_review_at"])


if __name__ == "__main__":
    unittest.main()
