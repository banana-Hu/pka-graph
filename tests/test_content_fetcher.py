"""第七阶段：受控原文获取安全边界测试。"""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from pka_graph.content_fetcher import _is_private_url, read_local_file


class PrivateUrlTests(unittest.TestCase):
    def test_localhost_blocked(self):
        for url in ("http://localhost/x", "http://127.0.0.1/x", "http://[::1]/x"):
            blocked, _ = _is_private_url(url)
            self.assertTrue(blocked, url)

    def test_intranet_blocked(self):
        for url in ("http://10.0.0.1/x", "http://172.16.0.1/x",
                     "http://192.168.1.1/x", "http://host.internal/x"):
            blocked, _ = _is_private_url(url)
            self.assertTrue(blocked, url)

    def test_non_http_blocked(self):
        for url in ("file:///etc/passwd", "ftp://x/y"):
            blocked, _ = _is_private_url(url)
            self.assertTrue(blocked, url)

    def test_public_allowed(self):
        blocked, _ = _is_private_url("https://example.com/page")
        self.assertFalse(blocked)

    def test_local_file_missing(self):
        out = read_local_file("nonexistent.md", Path("."))
        self.assertEqual(out["status"], "missing")

    def test_local_file_over_old_limit_read_in_full(self):
        from fixtures import make_test_vault
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            vault = make_test_vault(Path(tmp))
            big = vault / "正式笔记" / "big.md"
            big.write_bytes(b"x" * 600_000)
            out = read_local_file(str(big), vault)
            self.assertEqual(out["status"], "ok")
            self.assertEqual(len(out["text"]), 600_000)
            self.assertEqual(out["read_scope"], "full_text")

    def test_pdf_unsupported_without_dependency(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "test.pdf"
            p.write_bytes(b"%PDF-1.4")
            out = read_local_file(str(p), Path(tmp))
            self.assertEqual(out["status"], "unsupported_format")


if __name__ == "__main__":
    unittest.main()
