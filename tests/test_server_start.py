"""server_start.py 自启辅助功能测试（不触碰真实注册表）。"""
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import server_start


class AutostartCommandTests(unittest.TestCase):
    def test_command_quotes_both_paths(self):
        """自启命令必须给解释器与脚本路径加引号（中文路径含空格风险）。"""
        cmd = server_start.autostart_command()
        self.assertTrue(cmd.startswith('"'))
        parts = cmd.split('" "')
        self.assertEqual(len(parts), 2, cmd)
        self.assertEqual(parts[0], '"' + parts[0][1:])
        self.assertTrue(parts[1].rstrip('"').endswith("server_start.py"), cmd)

    def test_command_prefers_pythonw_when_present(self):
        """有 pythonw.exe 时必须用它（无窗口）；路径取自当前解释器同目录。"""
        cmd = server_start.autostart_command()
        pythonw = Path(sys.executable).with_name("pythonw.exe")
        if pythonw.exists():
            self.assertIn("pythonw.exe", cmd)
            self.assertIn(str(pythonw), cmd)
        else:
            self.assertIn(str(sys.executable), cmd)

    def test_run_value_is_project_specific(self):
        """Run 键名必须与其它项目的自启项区分（避免互相覆盖）。"""
        self.assertEqual(server_start.RUN_VALUE, "PkaGraphServer")
        self.assertNotIn("HackathonRadar", server_start.RUN_VALUE)
        self.assertIn("Run", server_start.RUN_KEY)


class SingleInstanceTests(unittest.TestCase):
    def test_second_bind_fails_on_same_port(self):
        """Windows 下单实例保证：同一端口第二次绑定必须失败（否则会双实例劫持）。"""
        from http.server import BaseHTTPRequestHandler
        from pka_graph.server import ExclusiveHTTPServer

        class Stub(BaseHTTPRequestHandler):
            def log_message(self, fmt, *args):
                pass

        first = ExclusiveHTTPServer(("127.0.0.1", 0), Stub)
        port = first.server_address[1]
        try:
            with self.assertRaises(OSError):
                ExclusiveHTTPServer(("127.0.0.1", port), Stub)
        finally:
            first.server_close()


class StreamGuardTests(unittest.TestCase):
    def test_noop_when_streams_present(self):
        """正常终端下 _ensure_streams 不改变现有流。"""
        out, err = sys.stdout, sys.stderr
        server_start._ensure_streams()
        self.assertIs(sys.stdout, out)
        self.assertIs(sys.stderr, err)


if __name__ == "__main__":
    unittest.main()
