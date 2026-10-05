"""HTTP API 层测试：路径越界、异常请求、版本冲突（必测 13/16 的服务面）。"""

import json
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tests"))

from fixtures import make_test_vault
from http.server import HTTPServer
from pka_graph.server import Handler


class ServerHarness:
    def __init__(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.vault = make_test_vault(Path(self._tmp.name))
        self.port = 8765

    def __enter__(self):
        import socket

        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            self.port = s.getsockname()[1]
        # 单线程 HTTPServer：避免 ThreadingHTTPServer 的 daemon handler threads
        # 在 server_close() 后仍持有 selector 导致 serve_forever() 不能可靠退出。
        self._tmp_vault = self.vault
        self._tmp_port = self.port
        self.thread = threading.Thread(target=self._serve_bg, daemon=True)
        self.thread.start()
        import time

        for _ in range(50):
            try:
                urllib.request.urlopen(f"http://127.0.0.1:{self.port}/api/meta", timeout=2)
                return self
            except urllib.error.HTTPError:
                return self
            except Exception:
                time.sleep(0.1)
        raise RuntimeError("服务器未能在超时内启动")

    def _serve_bg(self):
        from pka_graph.server import build_service

        service = build_service(self._tmp_vault)
        import secrets

        handler = type(
            "BoundHandler",
            (Handler,),
            {
                "service": service,
                "server_ref": None,
                "token": secrets.token_urlsafe(32),
                "port": self._tmp_port,
            },
        )
        server = HTTPServer(("127.0.0.1", self._tmp_port), handler)
        handler.server_ref = server
        server.serve_forever(poll_interval=0.1)

    def __exit__(self, *exc):
        # 先发 shutdown 请求（让 serve_forever 返回）
        try:
            urllib.request.urlopen(
                f"http://127.0.0.1:{self.port}/api/shutdown",
                data=b"{}",
                headers={"Content-Type": "application/json"},
                timeout=3,
            )
        except Exception:
            pass
        # 不在这里 join server 线程（daemon 线程数过多时
        # join(timeout=5) 不足以等到所有线程退出，导致 discover 套件最后一个
        # ServerHarness 超时；daemon 线程会随进程退出自动清理）
        self._tmp.cleanup()
        return False

    def token(self) -> str:
        with urllib.request.urlopen(f"http://127.0.0.1:{self.port}/api/meta", timeout=5) as resp:
            return json.loads(resp.read().decode("utf-8"))["token"]

    def post(self, path: str, payload, token: str | None = None, origin: str | None = None) -> tuple[int, dict]:
        headers = {"Content-Type": "application/json", "X-PKA-Token": token if token is not None else self.token()}
        if origin:
            headers["Origin"] = origin
        req = urllib.request.Request(
            f"http://127.0.0.1:{self.port}{path}",
            data=json.dumps(payload).encode("utf-8"),
            headers=headers,
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                return resp.status, json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read().decode("utf-8"))

    def get(self, path: str) -> tuple[int, dict]:
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{self.port}{path}", timeout=10) as resp:
                return resp.status, json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read().decode("utf-8"))


class ServerApiTests(unittest.TestCase):
    def test_ui_page_and_banner(self):
        with ServerHarness() as h:
            with urllib.request.urlopen(f"http://127.0.0.1:{h.port}/", timeout=5) as resp:
                html = resp.read().decode("utf-8")
            self.assertIn("测试知识库，未连接正式写入", html)

    def test_16_traversal_and_malformed_rejected(self):
        """必测 16：越界路径 403；异常 JSON 400。"""
        with ServerHarness() as h:
            code, body = h.post("/api/import/preview", {"paths": ["../../RealVaultSecret.md"], "which": "test"})
            self.assertEqual(code, 403)
            code, body = h.post("/api/nodes/create", {"name": "x", "domain": "d", "scope": "s", "base_version": 99999})
            self.assertIn(code, (409, 400))
            # 非法 JSON（带有效令牌才会到达解析层——防护顺序：令牌 → Content-Type → JSON）
            req = urllib.request.Request(
                f"http://127.0.0.1:{h.port}/api/nodes/create", data=b"{not json",
                headers={"Content-Type": "application/json", "X-PKA-Token": h.token()}, method="POST",
            )
            try:
                urllib.request.urlopen(req, timeout=5)
                self.fail("应当拒绝非法 JSON")
            except urllib.error.HTTPError as exc:
                self.assertEqual(exc.code, 400)

    def test_13_conflict_over_http(self):
        """必测 13（服务面）：旧 base_version 触发 409。"""
        with ServerHarness() as h:
            code, state = h.get("/api/state")
            stale = state["version"]
            code1, out1 = h.post("/api/nodes/create", {"name": "N1", "domain": "D", "scope": "S",
                                                       "request_id": "r1", "base_version": stale})
            self.assertEqual(code1, 200)
            code2, out2 = h.post("/api/nodes/create", {"name": "N2", "domain": "D", "scope": "S",
                                                       "request_id": "r2", "base_version": stale})
            self.assertEqual(code2, 409)
            self.assertIn("current_version", out2)

    def test_real_vault_scan_readonly_over_api(self):
        """真实库扫描经 API 可用且只读（详细哈希对比见 test_real_vault_untouched）。"""
        from pka_graph.server import REAL_VAULT

        if REAL_VAULT is None or not REAL_VAULT.exists():
            self.skipTest("未配置 PKA_REAL_VAULT（或真实知识库不在本机）")
        with ServerHarness() as h:
            code, report = h.post("/api/scan", {"which": "real"})
            self.assertEqual(code, 200)
            self.assertGreater(report["stats"]["with_id"], 0)


if __name__ == "__main__":
    unittest.main()
