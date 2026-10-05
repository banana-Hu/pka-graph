"""数据契约与产品规则测试（必测 1/3，及关系/状态校验）。"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tests"))

import unittest

from fixtures import make_test_vault
from pka_graph.models import (
    ValidationError,
    empty_state,
    make_node_link,
    make_note_link,
    new_node,
    validate_state,
)
from pka_graph.service import GraphService
from pka_graph.store import StateStore


class NodeContractTests(unittest.TestCase):
    """必测 1：新节点始终 untested；必测 3：同名不同范围可共存。"""

    def test_1_new_node_always_untested(self):
        for name in ("任何名字", "AI 节点"):
            node = new_node(name=name, domain="AI技术领域", scope="某个边界")
            self.assertEqual(node["status"], "untested")
            self.assertIsNone(node["last_quiz_at"])
            self.assertIsNone(node["next_review_at"])
            self.assertFalse(node["archived"])

    def test_3_same_name_different_scope_coexist(self):
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            store = StateStore(make_test_vault(Path(tmp)))
            svc = GraphService(store)
            a = svc.create_node(name="上下文窗口", domain="AI技术领域", scope="窗口的构成与计费影响")
            b = svc.create_node(name="上下文窗口", domain="AI技术领域", scope="截断与压缩策略")
            self.assertNotEqual(a["node"]["id"], b["node"]["id"])
            names = [n["name"] for n in svc.list_nodes()]
            self.assertEqual(names.count("上下文窗口"), 2)

    def test_relation_direction_and_self_loop(self):
        with self.assertRaises(ValidationError):
            make_node_link("node-" + "a" * 12, "node-" + "a" * 12, "related")
        with self.assertRaises(ValidationError):
            make_node_link("node-" + "a" * 12, "node-" + "b" * 12, "co_learning")  # 非法关系
        link = make_node_link("node-" + "a" * 12, "node-" + "b" * 12, "prerequisite")
        self.assertEqual(link["relation"], "prerequisite")

    def test_note_link_refs(self):
        with self.assertRaises(ValidationError):
            make_note_link("node-" + "a" * 12, {"kind": "url", "url": "https://x"})
        ok = make_note_link("node-" + "a" * 12, {"kind": "note_id", "note_id": "note-20260926-001"})
        self.assertEqual(ok["ref"]["note_id"], "note-20260926-001")

    def test_state_validation_rejects_dangling(self):
        state = empty_state()
        node = new_node(name="A", domain="D", scope="S")
        state["nodes"].append(node)
        state["note_links"].append(
            {"link_id": "lnk-x", "node_id": "node-" + "f" * 12,
             "ref": {"kind": "note_id", "note_id": "note-20260926-001"}, "created_at": "t"}
        )
        with self.assertRaises(ValidationError):
            validate_state(state)


class LayerTests(unittest.TestCase):
    def test_layer_derived_from_status_only(self):
        from pka_graph.service import GraphService

        self.assertEqual(GraphService.layer_of({"status": "passed"}), "mastery")
        self.assertEqual(GraphService.layer_of({"status": "untested"}), "vision")
        self.assertEqual(GraphService.layer_of({"status": "failed"}), "vision")


if __name__ == "__main__":
    unittest.main()
