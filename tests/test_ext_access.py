"""第七阶段：外部 AI 只读接入测试。"""
import json
import sys
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tests"))

from test_server import ServerHarness


class ExtAccessTests(unittest.TestCase):
    def test_ext_disabled_without_token(self):
        """未设置 PKA_EXT_TOKEN → 403。"""
        with ServerHarness() as h:
            try:
                urllib.request.urlopen(f"http://127.0.0.1:{h.port}/api/ext/status", timeout=5)
                self.fail("应当 403")
            except urllib.error.HTTPError as exc:
                self.assertEqual(exc.code, 403)

    def test_ext_tools_with_token(self):
        """设置 PKA_EXT_TOKEN → 可查询工具清单。"""
        with ServerHarness() as h:
            # ServerHarness 没有 PKA_EXT_TOKEN，这里手动注入到 handler
            # 简化：直接测试 build_ext_tools 返回结构
            from pka_graph.ext_access import build_ext_tools
            tools = build_ext_tools(None)
            self.assertIn("tools", tools)
            self.assertTrue(all(t["method"] == "GET" for t in tools["tools"]))
            self.assertTrue(tools["readonly"])
            self.assertIn("demo", " ".join(tools["semantics"]))

    def test_ext_search_and_node(self):
        """外部搜索 + 读取节点详情（需有效令牌）。"""
        # 集成路径在 ui_phase7 中验证；这里单测 ext_search / ext_node 逻辑
        import tempfile
        from fixtures import make_test_vault
        from pka_graph.service import GraphService
        from pka_graph.store import StateStore
        from pka_graph.ext_access import ext_search, ext_node

        with tempfile.TemporaryDirectory() as tmp:
            gs = GraphService(StateStore(make_test_vault(Path(tmp))))
            gs.create_node(name="Agent 工具调用", domain="AI", scope="工具循环")
            out = ext_search(gs, "工具调用")
            self.assertEqual(len(out["results"]), 1)
            self.assertEqual(out["results"][0]["status"], "untested")
            node_id = out["results"][0]["id"]
            detail = ext_node(gs, node_id)
            self.assertEqual(detail["status"], "untested")
            self.assertIn("semantics", detail)

    def test_ext_semantics_not_mastery_for_all(self):
        """外部 AI 语义说明必须包含"一个节点通过不代表领域掌握"。"""
        from pka_graph.ext_access import ext_status
        import tempfile
        from fixtures import make_test_vault
        from pka_graph.service import GraphService
        from pka_graph.store import StateStore

        with tempfile.TemporaryDirectory() as tmp:
            gs = GraphService(StateStore(make_test_vault(Path(tmp))))
            out = ext_status(gs)
            self.assertIn("不代表整个领域", out["semantics"] or "")


if __name__ == "__main__":
    unittest.main()

class ExtHttpTests(unittest.TestCase):
    """审查修复 P2-6：带令牌/不带令牌的真实 HTTP 调用与契约验证。"""

    EXT_TOKEN = "ext-test-token-0001"

    def _get(self, h, path, token=None):
        req = urllib.request.Request(f"http://127.0.0.1:{h.port}{path}")
        if token is not None:
            req.add_header("Authorization", f"Bearer {token}")
        try:
            with urllib.request.urlopen(req, timeout=8) as resp:
                return resp.status, json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read().decode("utf-8"))

    def setUp(self):
        import os
        self._old = os.environ.get("PKA_EXT_TOKEN")
        os.environ["PKA_EXT_TOKEN"] = self.EXT_TOKEN
        self.h = ServerHarness().__enter__()
        st, out = self.h.post("/api/nodes/create", {
            "name": "水位线设计", "domain": "AI技术领域",
            "scope": "增量索引的水位线设计与持久化语义",
            "aliases": ["watermark index"],
            "request_id": "ext-setup-1"})
        assert st == 200, out
        self.node_id = out["node"]["id"] if "node" in out else out["id"]

    def tearDown(self):
        self.h.__exit__()
        import os
        if self._old is None:
            os.environ.pop("PKA_EXT_TOKEN", None)
        else:
            os.environ["PKA_EXT_TOKEN"] = self._old

    def test_wrong_token_403_and_correct_token_200(self):
        st, _ = self._get(self.h, "/api/ext/status", token="wrong-token")
        self.assertEqual(st, 403)
        st, body = self._get(self.h, "/api/ext/status", token=self.EXT_TOKEN)
        self.assertEqual(st, 200)
        self.assertIn("vision_count", body)

    def test_search_covers_name_alias_and_scope(self):
        import urllib.parse
        q1 = urllib.parse.quote("水位线")
        q2 = urllib.parse.quote("watermark")
        q3 = urllib.parse.quote("增量索引")
        st, by_name = self._get(self.h, f"/api/ext/search?q={q1}", token=self.EXT_TOKEN)
        st, by_alias = self._get(self.h, f"/api/ext/search?q={q2}", token=self.EXT_TOKEN)
        st, by_scope = self._get(self.h, f"/api/ext/search?q={q3}", token=self.EXT_TOKEN)
        self.assertEqual((st, len(by_name["results"])), (200, 1))
        self.assertEqual(len(by_alias["results"]), 1)   # 别名命中
        self.assertEqual(len(by_scope["results"]), 1)   # scope 命中
        self.assertEqual(by_name["search_kind"], "node_search")  # 不冒充笔记全文检索

    def test_node_audit_status_from_note_frontmatter(self):
        """audit_status 来自笔记 frontmatter 的实际 status，而非 ref.kind；
        解析结果（resolution）随关联返回。"""
        import hashlib
        from pka_graph.store import StateStore

        note_rel = "正式笔记/合成已发布笔记.md"
        fp = self.h.vault / note_rel
        fp.parent.mkdir(parents=True, exist_ok=True)
        note_body = "---\nid: note-exttest-001\nstatus: published\ntitle: 合成\n---\n正文"
        fp.write_text(note_body, encoding="utf-8")
        # 直接构造登记+关联（沿用 test_9 的既有先例：正常 API 无法构造的测试场景）
        store = StateStore(self.h.vault)
        state = store.load()
        state["ingest_registry"].append({
            "registry_id": "reg-exttest01", "path": note_rel,
            "fingerprint": hashlib.sha256(fp.read_bytes()).hexdigest().upper(),
            "registered_at": "t"})
        state["note_links"].append({
            "link_id": "lnk-exttest01", "node_id": self.node_id,
            "ref": {"kind": "registry", "registry_id": "reg-exttest01", "path": note_rel},
            "created_at": "t"})
        store.save(state, base_version=None)
        st, detail = self._get(self.h, f"/api/ext/node/{self.node_id}", token=self.EXT_TOKEN)
        self.assertEqual(st, 200)
        self.assertEqual(len(detail["note_links"]), 1)
        link = detail["note_links"][0]
        self.assertEqual(link["audit_status"], "published")   # 来自实际 frontmatter
        self.assertNotIn(link["audit_status"], ("note_id", "registry"))
        self.assertEqual(link["resolution"]["status"], "ok")
        self.assertEqual(link["resolution"]["path"], note_rel)


if __name__ == "__main__":
    unittest.main()
