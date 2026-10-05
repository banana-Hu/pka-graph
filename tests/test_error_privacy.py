"""问题 C 回归：模型服务错误正文不得泄露回答内容或密钥。"""

import sys
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from pka_graph.quiz_model import Budget, HttpChatModelAdapter, ModelError

ANSWER_MARKER = "用户回答里的隐私标记ANSWER-XYZ"
FAKE_KEY = "sk-TEST-SECRET-KEY-DO-NOT-LEAK"


class LeakyHandler(BaseHTTPRequestHandler):
    """恶意/故障模型服务：把请求内容拼进错误正文回显。"""

    def log_message(self, fmt, *args):
        pass

    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length).decode("utf-8", "replace")
        payload = (
            '{"error": {"message": "请求校验失败：' + ANSWER_MARKER + " / key=" + FAKE_KEY
            + ' / 回显：' + body[:200] + '"}}'
        ).encode("utf-8")
        self.send_response(500)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)


class ErrorSanitizationTests(unittest.TestCase):
    def test_error_body_never_leaks_answer_or_key(self):
        server = HTTPServer(("127.0.0.1", 0), LeakyHandler)
        port = server.server_address[1]
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            adapter = HttpChatModelAdapter(
                f"http://127.0.0.1:{port}/v1", FAKE_KEY, "test-model",
                Budget(timeout_seconds=5, max_retries=0),
            )
            with self.assertRaises(ModelError) as ctx:
                adapter._chat("system", f"包含{ANSWER_MARKER}的用户内容", True)
            message = str(ctx.exception)
            self.assertNotIn(ANSWER_MARKER, message, "错误消息泄露了回答内容")
            self.assertNotIn(FAKE_KEY, message, "错误消息泄露了密钥")
            self.assertNotIn("回显", message)
            self.assertEqual(ctx.exception.category, "http_5xx")
            self.assertEqual(ctx.exception.status_code, 500)
            # 安全分类可展示
            self.assertIn("500", message)
        finally:
            server.shutdown()

    def test_4xx_no_retry(self):
        class FourXXHandler(BaseHTTPRequestHandler):
            def log_message(self, fmt, *args):
                pass

            calls = {"n": 0}

            def do_POST(self):
                type(self).calls["n"] += 1
                self.send_response(401)
                self.send_header("Content-Length", "0")
                self.end_headers()

        server = HTTPServer(("127.0.0.1", 0), FourXXHandler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            adapter = HttpChatModelAdapter(
                f"http://127.0.0.1:{server.server_address[1]}/v1", FAKE_KEY, "m",
                Budget(timeout_seconds=5, max_retries=3),
            )
            with self.assertRaises(ModelError) as ctx:
                adapter._chat("s", "u", True)
            self.assertEqual(FourXXHandler.calls["n"], 1)  # 4xx 不重试
            self.assertEqual(ctx.exception.category, "http")
        finally:
            server.shutdown()

    def test_parse_error_category(self):
        adapter = HttpChatModelAdapter("http://127.0.0.1:1/v1", FAKE_KEY, "m",
                                       Budget(timeout_seconds=1, max_retries=0))
        with self.assertRaises(ModelError) as ctx:
            adapter._chat("s", "u", True)
        self.assertIn(ctx.exception.category, ("network", "timeout"))


if __name__ == "__main__":
    unittest.main()
