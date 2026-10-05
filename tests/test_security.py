"""第四阶段本地接口安全测试（任务 6 + 必测 14/15）。"""

import json
import sys
import unittest
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tests"))

from test_server import ServerHarness


class LocalApiSecurityTests(unittest.TestCase):
    def test_missing_token_rejected(self):
        with ServerHarness() as h:
            req = urllib.request.Request(
                f"http://127.0.0.1:{h.port}/api/nodes/create",
                data=json.dumps({"name": "x", "domain": "d", "scope": "s"}).encode(),
                headers={"Content-Type": "application/json"}, method="POST",
            )
            try:
                urllib.request.urlopen(req, timeout=5)
                self.fail("缺令牌的写请求应被拒绝")
            except urllib.error.HTTPError as exc:
                self.assertEqual(exc.code, 403)

    def test_wrong_token_rejected(self):
        with ServerHarness() as h:
            code, _ = h.post("/api/nodes/create",
                             {"name": "x", "domain": "d", "scope": "s"}, token="wrong-token")
            self.assertEqual(code, 403)

    def test_foreign_origin_rejected(self):
        """防 CSRF：浏览器跨站 POST 会带 Origin，非同源一律拒绝。"""
        with ServerHarness() as h:
            code, body = h.post("/api/nodes/create",
                                {"name": "x", "domain": "d", "scope": "s"},
                                origin="https://evil.example.com")
            self.assertEqual(code, 403)

    def test_wrong_host_rejected(self):
        """防 DNS rebinding：Host 不是回环地址即拒绝。"""
        with ServerHarness() as h:
            code, _ = h.post("/api/nodes/create",
                             {"name": "x", "domain": "d", "scope": "s"},
                             origin=None)
            # 正常 Host 由 urllib 自动设为 127.0.0.1:port → 通过；
            # 显式伪造 Host 需要绕过 urllib 自动头：
            req = urllib.request.Request(
                f"http://127.0.0.1:{h.port}/api/nodes/create",
                data=json.dumps({"name": "x", "domain": "d", "scope": "s"}).encode(),
                headers={"Content-Type": "application/json",
                         "X-PKA-Token": h.token(), "Host": "attacker.example.com:80"},
                method="POST",
            )
            opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))  # 绕过系统代理直连
            try:
                opener.open(req, timeout=5)
                self.fail("伪造 Host 应被拒绝")
            except urllib.error.HTTPError as exc:
                self.assertEqual(exc.code, 403)
            self.assertEqual(code, 200)  # 正常请求仍工作

    def test_non_json_content_type_rejected(self):
        with ServerHarness() as h:
            req = urllib.request.Request(
                f"http://127.0.0.1:{h.port}/api/nodes/create",
                data=b"name=x",
                headers={"Content-Type": "application/x-www-form-urlencoded",
                         "X-PKA-Token": h.token()},
                method="POST",
            )
            try:
                urllib.request.urlopen(req, timeout=5)
                self.fail("非 JSON 内容类型应被拒绝")
            except urllib.error.HTTPError as exc:
                self.assertEqual(exc.code, 400)

    def test_14_forged_passed_via_update_rejected(self):
        """必测 14：前端伪造 passed 被拒绝——update 的 changes 不含 status 字段通道。"""
        with ServerHarness() as h:
            code, created = h.post("/api/nodes/create",
                                   {"name": "伪造测试", "domain": "D", "scope": "S"})
            self.assertEqual(code, 200)
            node_id = created["node"]["id"]
            # 尝试通过 update 夹带 status
            code, _ = h.post("/api/nodes/update",
                             {"node_id": node_id,
                              "changes": {"name": "改名", "status": "passed",
                                          "last_quiz_at": "2099-01-01",
                                          "next_review_at": "2099-01-02"}})
            self.assertEqual(code, 200)  # 改名成功
            code, detail = h.get(f"/api/node/{node_id}")
            node = detail["node"]
            self.assertEqual(node["status"], "untested")       # 状态未被夹带字段改动
            self.assertIsNone(node["last_quiz_at"])            # 伪造测验时间无效
            self.assertIsNone(node["next_review_at"])          # 复习安排不可写

    def test_15_create_with_status_field_ignored(self):
        """必测 15：普通 CRUD 夹带 status 字段不能标记通过。"""
        with ServerHarness() as h:
            code, created = h.post("/api/nodes/create",
                                   {"name": "夹带status", "domain": "D", "scope": "S",
                                    "status": "passed", "last_quiz_at": "2099-01-01"})
            self.assertEqual(code, 200)
            node = created["node"]
            self.assertEqual(node["status"], "untested")
            self.assertIsNone(node["last_quiz_at"])
            # import / 关系等其他入口同理会忽略（这里抽查 merge 路径不写状态）
            code, merged = h.post("/api/merge/apply",
                                  {"source_id": node["id"], "target_id": node["id"]})
            self.assertEqual(code, 400)  # 自合并被业务拒绝，而非把状态写穿

    def test_quiz_result_submission_not_exposed(self):
        """判定结果不存在前端可提交的通道：伪造的 quiz 提交端点不存在。"""
        with ServerHarness() as h:
            code, _ = h.post("/api/quiz/submit",
                             {"session_id": "quiz-x", "verdict": "passed"})
            self.assertEqual(code, 404)  # 没有这个路由——判定只能由服务端模型链路产生


if __name__ == "__main__":
    unittest.main()
