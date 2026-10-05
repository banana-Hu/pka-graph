"""第三阶段：图谱接口、搜索、原文读取（必测 1-12 中的服务端部分）。"""

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tests"))

from fixtures import make_test_vault
from pka_graph.models import ValidationError
from pka_graph.service import GraphService
from pka_graph.store import PathOutsideVault, StateStore


def build_rich_service(tmp: Path):
    """构造多领域、多状态、含归档与关系的图。"""
    vault = make_test_vault(tmp)
    svc = GraphService(StateStore(vault))
    a = svc.create_node(name="Agent 工具调用", domain="AI技术领域", scope="工具循环")["node"]
    b = svc.create_node(name="提示词工程", domain="AI技术领域", scope="三类手法")["node"]
    c = svc.create_node(name="REST 设计", domain="程序开发领域", scope="资源建模")["node"]
    d = svc.create_node(name="REST 设计", domain="产品领域", scope="API 产品化视角")["node"]  # 同名不同领域
    e = svc.create_node(name="带<script>alert(1)</script>的范围", domain="AI技术领域", scope="特殊字符 <>&\" 节点")["node"]
    arch = svc.create_node(name="已归档节点", domain="AI技术领域", scope="不再显示")["node"]
    # 通过受控写入设置状态（模拟测验结果——测试数据）
    def force(node, status):
        state = svc.state()
        for n in state["nodes"]:
            if n["id"] == node["id"]:
                n["status"] = status
        svc.store.save(state, base_version=None)
    force(a, "passed")
    force(c, "failed")
    svc.add_relation(a["id"], b["id"], "prerequisite", base_version=None)
    svc.add_relation(b["id"], e["id"], "related", base_version=None)
    svc.add_relation(c["id"], d["id"], "related", base_version=None)
    svc.add_relation(arch["id"], a["id"], "related", base_version=None)  # 涉及归档 → 图谱中隐藏
    svc.archive_node(arch["id"], base_version=None)
    return svc, dict(a=a, b=b, c=c, d=d, e=e, arch=arch)


class GraphViewTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.svc, self.n = build_rich_service(Path(self._tmp.name))

    def tearDown(self):
        self._tmp.cleanup()

    def test_1_status_maps_to_layers(self):
        """必测 1：三种状态正确映射两层。"""
        g = self.svc.graph_view()
        by = {n["id"]: n for n in g["nodes"]}
        self.assertEqual(by[self.n["a"]["id"]]["layer"], "mastery")
        self.assertEqual(by[self.n["b"]["id"]]["layer"], "vision")
        self.assertEqual(by[self.n["c"]["id"]]["layer"], "vision")
        self.assertEqual(g["stats"]["mastery"], 1)
        self.assertEqual(g["stats"]["vision"], 4)  # b c d e（a 已掌握，arch 归档）
        self.assertEqual(g["stats"]["active_total"], 5)

    def test_2_archived_excluded(self):
        """必测 2：归档节点不计入默认图谱与统计。"""
        g = self.svc.graph_view()
        ids = {n["id"] for n in g["nodes"]}
        self.assertNotIn(self.n["arch"]["id"], ids)
        self.assertEqual(g["excluded_archived"], 1)
        self.assertEqual(g["stats"]["active_total"], 5)

    def test_3_no_dangling_visible_edges(self):
        """必测 3：不产生引用不存在（被排除）节点的可见边。"""
        g = self.svc.graph_view()
        ids = {n["id"] for n in g["nodes"]}
        for e in g["edges"]:
            self.assertIn(e["from_id"], ids)
            self.assertIn(e["to_id"], ids)
        # 与归档节点相关的那条边被隐藏并计数
        self.assertEqual(g["hidden_edges"], 1)

    def test_4_graph_list_detail_status_consistent(self):
        """必测 4：图谱、列表、详情对同一节点状态一致。"""
        gid = self.n["a"]["id"]
        g = self.svc.graph_view()
        graph_status = next(n["status"] for n in g["nodes"] if n["id"] == gid)
        list_status = next(n["status"] for n in self.svc.list_nodes() if n["id"] == gid)
        detail_status = self.svc.node_detail(gid)["node"]["status"]
        self.assertEqual(graph_status, list_status == "passed" and "passed" or list_status)
        self.assertEqual(graph_status, detail_status)

    def test_5_search_chinese_alias_special_chars(self):
        """必测 5：搜索支持中文、别名、特殊字符（纯子串匹配，不做正则）。"""
        g = self.svc.graph_view(q="提示词")
        self.assertEqual(len(g["nodes"]), 1)
        self.assertEqual(g["nodes"][0]["name"], "提示词工程")
        g2 = self.svc.graph_view(q="<script>")
        self.assertEqual(len(g2["nodes"]), 1)  # 特殊字符按字面匹配
        g3 = self.svc.graph_view(q="REST")
        self.assertEqual(len(g3["nodes"]), 2)  # 同名两篇都命中

    def test_6_same_name_not_conflated(self):
        """必测 6：同名不同领域/范围可区分、不串联。"""
        g = self.svc.graph_view(q="REST 设计")
        self.assertEqual(len(g["nodes"]), 2)
        domains = {n["domain"] for n in g["nodes"]}
        self.assertEqual(domains, {"程序开发领域", "产品领域"})

    def test_7_combined_filters(self):
        """必测 7：组合筛选正确。"""
        g = self.svc.graph_view(domain="AI技术领域", layer="mastery")
        self.assertEqual(len(g["nodes"]), 1)
        self.assertEqual(g["nodes"][0]["name"], "Agent 工具调用")
        g2 = self.svc.graph_view(status="failed", domain="程序开发领域")
        self.assertEqual(len(g2["nodes"]), 1)
        self.assertEqual(g2["nodes"][0]["name"], "REST 设计")
        g3 = self.svc.graph_view(layer="vision", status="passed")
        self.assertEqual(len(g3["nodes"]), 0)  # 互斥组合返回空而不报错

    def test_8_invalid_status_raises_not_passed(self):
        """必测 8：非法状态明确报异常，不默认当作已掌握。
        模拟外部工具绕过校验直改状态文件（正常 store.save 会拒绝非法状态——已单独验证）。"""
        import json

        state = self.svc.state()
        state["nodes"][0]["status"] = "mastered!!"
        self.svc.store.state_path.write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")
        with self.assertRaises(ValidationError) as ctx:
            self.svc.graph_view()
        self.assertIn("非法", str(ctx.exception))
        self.svc.store.recover()  # 从备份还原，避免影响其他用例（临时目录内）

    def test_stats_same_scope_as_graph(self):
        """统计与图谱同一数据范围：filtered = 返回节点数。"""
        g = self.svc.graph_view(q="REST")
        self.assertEqual(g["stats"]["filtered"], len(g["nodes"]))


class NoteContentTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.vault = make_test_vault(Path(self._tmp.name))
        self.svc = GraphService(StateStore(self.vault))
        self.node = self.svc.create_node(name="N", domain="D", scope="S")["node"]

    def tearDown(self):
        self._tmp.cleanup()

    def _link_formal(self):
        return self.svc.import_apply(
            ["正式笔记/04-Unit1.md"],
            [{"path": "正式笔记/04-Unit1.md", "node_id": self.node["id"]}],
            request_id=None, base_version=None,
        )["note_links"][0]["link_id"]

    def _link_no_id(self):
        self.svc.import_apply(["候选笔记/无ID笔记.md"], [], request_id=None, base_version=None)
        state = self.svc.state()
        reg_id = state["ingest_registry"][0]["registry_id"]
        from pka_graph.models import make_note_link

        state["note_links"].append(
            make_note_link(
                self.node["id"],
                {"kind": "registry", "registry_id": reg_id, "path": "候选笔记/无ID笔记.md"},
            )
        )
        self.svc.store.save(state, base_version=None)
        return state["note_links"][-1]["link_id"]

    def test_read_ok_returns_plain_text(self):
        link = self._link_formal()
        out = self.svc.read_note_content(link)
        self.assertEqual(out["status"], "ok")
        self.assertIn("Unit 1", out["content"])
        self.assertIsInstance(out["content"], str)  # 纯文本，转义交给界面

    def test_9_missing_and_traversal_rejected(self):
        """必测 9：来源不存在给提示；越界路径拒绝。
        越界场景：模拟外部篡改，把越界登记与关联直接写进状态文件（正常 API 无法构造）。"""
        import json

        link = self._link_formal()
        (self.vault / "正式笔记" / "04-Unit1.md").unlink()
        out = self.svc.read_note_content(link)
        self.assertEqual(out["status"], "missing")
        self.assertIn("不存在", out["message"])
        # 越界登记：直接写原始状态（make_note_link 的登记 ID 校验会正常拒绝这种构造）
        state = self.svc.state()
        state["ingest_registry"].append(
            {"registry_id": "reg-evil0001", "path": "../../evil.md", "fingerprint": "X", "registered_at": "t"}
        )
        state["note_links"].append(
            {
                "link_id": "lnk-evil0001",
                "node_id": self.node["id"],
                "ref": {"kind": "registry", "registry_id": "reg-evil0001", "path": "../../evil.md"},
                "created_at": "t",
            }
        )
        self.svc.store.state_path.write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")
        evil_link = "lnk-evil0001"
        with self.assertRaises(PathOutsideVault):
            self.svc.read_note_content(evil_link)

    def test_identity_conflict_refuses_content(self):
        """身份冲突：路径下的文件 ID 与登记不一致 → 拒绝展示，不自动换文件。"""
        link = self._link_formal()
        target = self.vault / "正式笔记" / "04-Unit1.md"
        target.write_text("---\nid: note-20990101-001\nstatus: accepted\ntitle: 冒名顶替\n---\n内容", encoding="utf-8")
        out = self.svc.read_note_content(link)
        self.assertEqual(out["status"], "identity_conflict")
        self.assertNotIn("content", out)
        self.assertIn("身份冲突", out["message"])

    def test_registry_drift_warns_but_shows(self):
        """登记指纹漂移：展示当前内容 + 待修复警告（同一路径内容变化，不换文件）。"""
        link = self._link_no_id()
        target = self.vault / "候选笔记" / "无ID笔记.md"
        target.write_text(target.read_text(encoding="utf-8") + "\n新增段落。\n", encoding="utf-8")
        out = self.svc.read_note_content(link)
        self.assertEqual(out["status"], "ok")
        self.assertTrue(any("指纹不一致" in w for w in out["warnings"]))
        self.assertIn("新增段落", out["content"])

    def test_unknown_link_404(self):
        with self.assertRaises(KeyError):
            self.svc.read_note_content("lnk-notexist")


if __name__ == "__main__":
    unittest.main()
