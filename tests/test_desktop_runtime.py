"""桌面入口离线验证：fake Tk/API，不打开窗口，不访问真实服务。"""

import importlib.util
import io
import json
import queue
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch
from urllib.error import URLError


MODULE_PATH = Path(__file__).resolve().parents[1] / "知识库入口.py"
SPEC = importlib.util.spec_from_file_location("pka_desktop_under_test", MODULE_PATH)
desktop = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(desktop)


class FakeRoot:
    def __init__(self):
        self.scheduled = []
        self.cancelled = []
        self.titles = []
        self.destroyed = False

    def after(self, delay, callback):
        self.scheduled.append((delay, callback))
        return "timer-" + str(len(self.scheduled))

    def after_cancel(self, identity):
        self.cancelled.append(identity)

    def title(self, text):
        self.titles.append(text)

    def destroy(self):
        self.destroyed = True


class FakeVar:
    def __init__(self):
        self.values = []

    def set(self, value):
        self.values.append(value)


class FakeTree:
    def __init__(self):
        self.rows = {}

    def insert(self, parent, index, values):
        identity = "row-" + str(len(self.rows) + 1)
        self.rows[identity] = {"file": values[0], "status": values[1]}
        return identity

    def set(self, identity, column, value):
        self.rows.setdefault(identity, {})[column] = value


class FakeApi:
    def __init__(self):
        self.port = 8765
        self.token = "session-for-tests"
        self.session = ""
        self.pipeline_mode = "task"
        self.banner = "合成知识库"
        self.auth_note = ""
        self.connect_calls = 0
        self.snapshot_calls = 0
        self.ingest_calls = 0
        self.scanned = threading.Event()
        self.fail_first_scan = False

    def connect(self):
        self.connect_calls += 1
        self.token = "session-for-tests"
        return True

    def snapshot(self):
        self.snapshot_calls += 1
        if self.fail_first_scan and self.snapshot_calls == 1:
            raise URLError("合成断连")
        self.scanned.set()
        return {"registry": {}, "accepted": {}, "tasks": {},
                "status": {"task_mode": True, "daily_limit": 0, "used_today": 3}}

    def ingest(self, path):
        self.ingest_calls += 1
        return {"queued": "task:example", "task_id": "example", "status": "queued"}


def fake_app(api=None):
    app = desktop.App.__new__(desktop.App)
    app.root = FakeRoot()
    app.api = api or FakeApi()
    app.events = queue.Queue()
    app.rows, app.pending_files = {}, {}
    app.tree = FakeTree()
    app.status_var = FakeVar()
    app._stop = threading.Event()
    app._worker = None
    app._connected = False
    app._after_id = None
    return app


class DesktopRuntimeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name)

    def large_file(self):
        path = self.directory / "超过二十MB的单文件.txt"
        with path.open("wb") as output:
            output.truncate(21 * 1024 * 1024 + 1)
        return path

    def test_task_submission_accepts_large_single_file_without_reading_body(self):
        api = desktop.ApiClient(8765)
        api.pipeline_mode = "task"
        source = self.large_file()
        with patch.object(api, "_post", return_value={"task_id": "task-1", "status": "queued"}) as post, \
                patch.object(Path, "read_bytes", side_effect=AssertionError("task提交不应把正文装进HTTP请求")):
            result = api.ingest(source)
        self.assertEqual(result["task_id"], "task-1")
        endpoint, payload = post.call_args.args
        self.assertEqual(endpoint, "/api/tasks")
        self.assertEqual(payload["source"], {"kind": "file", "path": str(source.resolve())})
        self.assertIsNone(payload["options"]["publish_to"])
        self.assertNotIn("data_b64", payload)

    def test_legacy_large_single_file_has_no_twenty_mb_rejection(self):
        api = desktop.ApiClient(8765)
        source = self.large_file()
        with patch.object(api, "_post", return_value={"queued": source.name}) as post:
            api.ingest(source)
        endpoint, payload = post.call_args.args
        self.assertEqual(endpoint, "/api/inbox/ingest")
        self.assertGreater(len(payload["data_b64"]), 20 * 1024 * 1024)

    def test_task_get_sends_authenticated_session(self):
        api = desktop.ApiClient(8765)
        api.session = "owned-session"
        with patch.object(desktop, "urlopen", return_value=io.BytesIO(b'{"status":"queued"}')) as request:
            self.assertEqual(api._get("/api/tasks/task-1")["status"], "queued")
        headers = {name.lower(): value for name, value in request.call_args.args[0].headers.items()}
        self.assertEqual(headers["x-pka-session"], "owned-session")

    def test_direct_task_submission_is_visible_in_poll_snapshot(self):
        api = desktop.ApiClient(8765)
        api.pipeline_mode = "task"
        source = self.directory / "原文.txt"
        source.write_text("合成原文", encoding="utf-8")
        with patch.object(api, "_post", return_value={"task_id": "direct-task", "status": "queued"}):
            api.ingest(source)
        def get(path):
            if path == "/api/inbox/registry":
                return {"processed": {}}
            if path.startswith("/api/discovery/"):
                return {"suggestions": []}
            if path == "/api/inbox/status":
                return {"task_mode": True}
            if path == "/api/tasks/direct-task":
                return {"task_id": "direct-task", "status": "succeeded",
                        "result": {"note_status": "accepted"}}
            raise AssertionError(path)
        with patch.object(api, "_get", side_effect=get):
            snapshot = api.snapshot()
        self.assertEqual(snapshot["registry"]["task:direct-task"]["name"], source.name)
        self.assertEqual(snapshot["tasks"]["direct-task"]["status"], "succeeded")

    def test_one_event_consumer_handles_connection_row_and_summary(self):
        app = fake_app()
        app.events.put({"type": "conn", "ok": True})
        app.events.put({"type": "row", "item": "chosen-row", "key": "task:task-1", "status": "已提交"})
        app.events.put({"type": "snap", "statusline": "本轮总结：AI审核通过，节点未测验",
            "snap": {"registry": {"task:task-1": {"task_id": "task-1"}}, "accepted": {},
                     "tasks": {"task-1": {"status": "succeeded", "stage": "commit",
                                          "result": {"note_status": "accepted"}}}}})
        with patch.object(app, "_poll_once", side_effect=AssertionError("UI定时器不能另开轮询")), \
                patch.object(desktop.threading, "Thread", side_effect=AssertionError("消费事件不能启动线程")):
            app._on_tick()
        self.assertTrue(app.events.empty())
        self.assertEqual(app.rows["task:task-1"], "chosen-row")
        self.assertIn("已完成", app.tree.rows["chosen-row"]["status"])
        self.assertIn("笔记：accepted", app.tree.rows["chosen-row"]["status"])
        self.assertEqual(app.status_var.values[-1], "本轮总结：AI审核通过，节点未测验")
        self.assertEqual(len(app.root.scheduled), 1)
        app._drain_events()
        self.assertEqual(len(app.root.scheduled), 1)

    def test_auth_and_connection_loss_events_are_not_dropped(self):
        app = fake_app()
        app.events.put({"type": "auth_lost"})
        app.events.put({"type": "conn_lost"})
        app._drain_events()
        self.assertIn("会话已失效", app.status_var.values[0])
        self.assertIn("连接中断", app.status_var.values[1])

    def test_reconnect_reuses_one_poll_thread(self):
        api = FakeApi()
        api.fail_first_scan = True
        app = fake_app(api)
        with patch.object(desktop, "POLL_MS", 10):
            app._start_worker()
            worker = app._worker
            self.assertTrue(api.scanned.wait(1))
            app._start_worker()
            self.assertIs(app._worker, worker)
            app.close()
            worker.join(1)
        self.assertFalse(worker.is_alive())
        self.assertEqual(api.connect_calls, 2)
        self.assertGreaterEqual(api.snapshot_calls, 2)
        self.assertTrue(app.root.destroyed)

    def test_close_interrupts_wait_and_stops_future_submission(self):
        app = fake_app()
        app._after_id = "existing-timer"
        app._start_worker()
        self.assertTrue(app.api.scanned.wait(1))
        worker = app._worker
        app.close()
        worker.join(1)
        self.assertFalse(worker.is_alive())
        self.assertEqual(app.root.cancelled, ["existing-timer"])
        app._start_worker()
        self.assertIs(app._worker, worker)
        app._submit_worker([(str(self.directory / "unused.txt"), "row")])
        self.assertEqual(app.api.ingest_calls, 0)
        scheduled = len(app.root.scheduled)
        app._on_tick()
        self.assertEqual(len(app.root.scheduled), scheduled)

    def test_submit_worker_registers_rows_only_in_ui_consumer(self):
        app = fake_app()
        app._submit_worker([(str(self.directory / "synthetic.txt"), "selected-row")])
        self.assertEqual(app.rows, {})
        app._drain_events()
        self.assertEqual(app.rows["task:example"], "selected-row")
        self.assertIn("已提交", app.tree.rows["selected-row"]["status"])


if __name__ == "__main__":
    unittest.main()
