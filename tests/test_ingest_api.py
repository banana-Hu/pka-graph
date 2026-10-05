"""知识库入口客户端的 HTTP 投递端点测试（POST /api/inbox/ingest 等）。

走 ServerHarness 起真实 HTTP 服务；模型未配置 → 投递后登记 pending_analysis
（语义与目录投放一致），端点契约与校验规则是本文件的重点。
"""
import base64
import json
import sys
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tests"))

from test_server import ServerHarness

MD = """---
contributed_by: "gui-client-test"
---

# 客户端投递材料

这里是足够长的正文内容，用于通过最短长度检查，验证投递后进入处理登记。
""" * 3


class IngestApiTests(unittest.TestCase):
    def setUp(self):
        self.h = ServerHarness().__enter__()

    def tearDown(self):
        time.sleep(0.8)   # 等后台快速扫描线程收尾，避免临时目录清理竞态
        self.h.__exit__()

    def _post(self, path: str, payload: dict):
        return self.h.post(path, payload)

    def _get_registry(self):
        with urllib.request.urlopen(
                f"http://127.0.0.1:{self.h.port}/api/inbox/registry", timeout=10) as resp:
            return json.loads(resp.read().decode("utf-8"))["processed"]

    def test_ingest_lands_in_inbox_and_registers(self):
        status, out = self._post("/api/inbox/ingest", {
            "name": "客户端材料.md", "data_b64": base64.b64encode(MD.encode("utf-8")).decode()})
        self.assertEqual(status, 200)
        self.assertEqual(out["status"], "queued")
        rel = out["queued"]
        self.assertTrue(rel.startswith("客户端材料.md") or rel == "客户端材料.md")
        # 快速扫描在后台线程：轮询等待登记出现
        deadline = time.time() + 10
        while time.time() < deadline:
            reg = self._get_registry()
            if rel in reg:
                break
            time.sleep(0.2)
        self.assertIn(rel, reg)
        self.assertEqual(reg[rel]["status"], "pending_analysis")  # 无模型：如实登记不假装
        # 文件真实落在收件箱目录
        self.assertTrue((self.h.vault / "个人学习状态" / "inbox" / rel).exists())

    def test_ingest_rejects_path_traversal_and_bad_ext(self):
        status, out = self._post("/api/inbox/ingest", {
            "name": "../evil.md", "data_b64": base64.b64encode(b"x").decode()})
        self.assertEqual(status, 400)
        status, out = self._post("/api/inbox/ingest", {
            "name": "程序.exe", "data_b64": base64.b64encode(b"MZ").decode()})
        self.assertEqual(status, 400)
        status, out = self._post("/api/inbox/ingest", {
            "name": "空.md", "data_b64": ""})
        self.assertEqual(status, 400)

    def test_ingest_accepts_single_file_over_old_size_limit(self):
        size = 21 * 1024 * 1024
        big = base64.b64encode(b"x" * size).decode()
        status, out = self._post("/api/inbox/ingest", {"name": "大.md", "data_b64": big})
        self.assertEqual(status, 200)
        self.assertEqual(out["status"], "queued")
        stored = self.h.vault / "个人学习状态" / "inbox" / out["queued"]
        self.assertEqual(stored.stat().st_size, size)

    def test_same_name_reingest_does_not_overwrite(self):
        s1, o1 = self._post("/api/inbox/ingest", {
            "name": "同名.md", "data_b64": base64.b64encode(MD.encode("utf-8")).decode()})
        s2, o2 = self._post("/api/inbox/ingest", {
            "name": "同名.md", "data_b64": base64.b64encode(MD.encode("utf-8")).decode()})
        self.assertEqual((s1, s2), (200, 200))
        self.assertNotEqual(o1["queued"], o2["queued"])   # 同名加后缀，不覆盖


if __name__ == "__main__":
    unittest.main()
