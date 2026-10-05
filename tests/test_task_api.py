"""统一任务接口的离线联调（ZCode 接线验收）。

- 假适配器 + 合成材料：不调用真实模型、Notion、采集或正式库。
- 覆盖协作约定 §8 接线验收：票据重放/过期、未认证 401、ext 只读无写权限、
  两种模式只有一个执行所有者、缺模型不当作成功跳过、浏览器与桌面同一主人、
  取消发生在提交前后分别保留真实结果。
"""

import json
import os
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from http.server import HTTPServer
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tests"))

from fixtures import make_test_vault
from pka_graph.file_lock import FileMutex
from pka_graph.server import (DesktopTicketChannel, Handler, OWNER_SCOPES, OWNER_SUBJECT,
                              SESSION_COOKIE, SESSION_HEADER, SessionStore, build_service)
from pka_graph.task_store import TaskStore
from pka_graph.workflow import Principal, StageResult, WorkflowError, WorkflowService

SOURCE_MATERIAL = "# 合成材料\n\n" + "这是一段用于离线联调的合成正文，不包含真实私人内容。" * 20 + "\n"


class FakeAdapter:
    """记录调用并按脚本返回结果的假适配器；不调用任何外部依赖。"""

    def __init__(self, name, result=None, *, fail=False, record=None, artifacts=(), delay=0.0,
                 effect_kind=None, effect_id=None, entered=None, gate=None):
        self.name = name
        self.scripted = result
        self.fail = fail
        self.record = record if record is not None else []
        self.artifact_spec = artifacts
        self.delay = delay
        self.effect_kind = effect_kind
        self.effect_id = effect_id or f"{name}:fake-effect"
        self.entered = entered      # 进入副作用时置位，供测试制造"提交进行中"
        self.gate = gate            # 副作用等待放行，模拟提交尚未结束
        self.available = True

    def _write(self, context):
        if self.entered is not None:
            self.entered.set()
        if self.gate is not None:
            self.gate.wait(5)
        artifacts = []
        for kind, filename in self.artifact_spec:
            target = Path(context.work_dir) / filename
            target.write_text(f"{self.name}-{filename}", encoding="utf-8")
            artifacts.append({"kind": kind, "path": target.resolve().relative_to(context.vault_root).as_posix(),
                              "sha256": __import__("hashlib").sha256(target.read_bytes()).hexdigest(),
                              "complete": True})
        return artifacts

    def execute(self, context):
        self.record.append(self.name)
        if self.delay:
            time.sleep(self.delay)
        if self.fail:
            raise RuntimeError("合成失败：模型不可用")
        if self.effect_kind:
            artifacts = context.run_effect(self.effect_id, kind=self.effect_kind,
                                           action=lambda: {"artifacts": self._write(context)})["artifacts"]
        else:
            artifacts = self._write(context)
        if isinstance(self.scripted, StageResult):
            return self.scripted
        return StageResult("completed", artifacts=tuple(artifacts))


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


class SessionStoreTests(unittest.TestCase):
    def test_ticket_single_use_expiry_and_session_window(self):
        clock = Clock()
        store = SessionStore(clock=clock)
        ticket = store.issue()
        self.assertTrue(store.valid(ticket))
        status, token = store.consume(ticket)
        self.assertEqual(status, "ok")
        self.assertFalse(store.valid(ticket))
        self.assertEqual(store.consume(ticket)[0], "invalid")           # 重放
        self.assertEqual(store.consume("not-a-ticket")[0], "invalid")   # 非法
        self.assertEqual(store.session(token)[0], OWNER_SUBJECT)
        self.assertEqual(store.session(token)[1], OWNER_SCOPES)
        clock.now += store.session_ttl + 1
        self.assertIsNone(store.session(token))

    def test_expired_ticket_is_reported_as_expired(self):
        clock = Clock()
        store = SessionStore(clock=clock)
        ticket = store.issue()
        clock.now += store.ticket_ttl + 1
        self.assertEqual(store.consume(ticket)[0], "expired")


class TaskHarness:
    """task 模式离线服务：真实 HTTP 路由 + 会话票据，适配器可注入假实现。"""

    def __init__(self, *, mode="task", start_worker=True, adapters=None, env=None):
        self.mode = mode
        self.start_worker = start_worker
        self.fake_adapters = adapters
        self.extra_env = env or {}
        self._tmp = tempfile.TemporaryDirectory()
        self.vault = make_test_vault(Path(self._tmp.name))
        self.port = 0
        self.sessions = SessionStore()
        self.tickets = None

    def __enter__(self):
        self._env = patch.dict(os.environ, {"PKA_TASK_PIPELINE": self.mode, **self.extra_env})
        self._env.start()
        import socket

        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            self.port = probe.getsockname()[1]
        self.service = build_service(self.vault)
        if getattr(self.service, "workflow", None) is not None and self.fake_adapters:
            self.service.workflow.adapters = dict(self.fake_adapters)
            self.service.adapter_inventory = dict(self.fake_adapters)
        self.tickets = DesktopTicketChannel(
            self.vault / "个人学习状态" / "runtime" / "desktop_ticket.json", self.sessions)
        handler = type("BoundHandler", (Handler,), {
            "service": self.service, "server_ref": None, "token": "legacy-token-for-tests",
            "port": self.port, "sessions": self.sessions, "desktop_tickets": self.tickets})
        self.server = HTTPServer(("127.0.0.1", self.port), handler)
        handler.server_ref = self.server
        self.thread = threading.Thread(target=self.server.serve_forever,
                                       kwargs={"poll_interval": 0.05}, daemon=True)
        self.thread.start()
        if self.service.workflow is not None and self.start_worker and self.mode == "task":
            self.service.workflow.start()
        return self

    def __exit__(self, *exc):
        if getattr(self.service, "workflow", None) is not None:
            self.service.workflow.stop(timeout=5)
        try:
            self.server.shutdown()
            self.server.server_close()
        except Exception:  # noqa: BLE001 —— 清理失败不影响断言结果
            pass
        self._env.stop()
        self._tmp.cleanup()
        return False

    # ---------- HTTP ----------

    def request(self, method, path, payload=None, *, session=None, headers=None, raw=False):
        request_headers = dict(headers or {})
        data = None
        if payload is not None:
            data = json.dumps(payload).encode("utf-8")
            request_headers["Content-Type"] = "application/json"
        if session is not None:
            request_headers[SESSION_HEADER] = session
        request = urllib.request.Request(f"http://127.0.0.1:{self.port}{path}", data=data,
                                         headers=request_headers, method=method)
        try:
            with urllib.request.urlopen(request, timeout=10) as response:
                body = json.loads(response.read().decode("utf-8"))
                return response.status, body, response.headers
        except urllib.error.HTTPError as exc:
            raw_body = exc.read().decode("utf-8")
            try:
                return exc.code, json.loads(raw_body), exc.headers
            except json.JSONDecodeError:
                return exc.code, {"raw": raw_body}, exc.headers

    def browser_session(self) -> str:
        ticket = self.sessions.issue()
        status, body, headers = self.request("POST", "/api/session/exchange", {"ticket": ticket})
        assert status == 200, body
        return body["session_token"]

    def desktop_session(self) -> str:
        ticket = self.tickets.ensure_fresh()
        status, body, _ = self.request("POST", "/api/session/exchange", {"ticket": ticket})
        assert status == 200, body
        return body["session_token"]

    def source_file(self, name="材料.md", text=SOURCE_MATERIAL) -> Path:
        path = self.vault / "收件箱" / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        return path

    def submit(self, session, path, request_id="req-1", **options) -> tuple[int, dict]:
        payload = {"contract_version": 1, "request_id": request_id,
                   "source": {"kind": "file", "path": str(path)},
                   "options": {"pipeline": "note", "publish_to": None, **options}}
        status, body, _ = self.request("POST", "/api/tasks", payload, session=session)
        return status, body


class TaskApiTests(unittest.TestCase):
    def test_unauthenticated_task_routes_need_session(self):
        with TaskHarness() as harness:
            for method, path in (("GET", "/api/tasks/capabilities"), ("GET", "/api/tasks/anything"),
                                 ("POST", "/api/tasks")):
                status, body, _ = harness.request(method, path, {} if method == "POST" else None)
                self.assertEqual(status, 401, (method, path, body))
                self.assertEqual(body["error"]["code"], "unauthenticated")
            self.assertEqual(harness.request("POST", "/api/tasks/x/cancel", {})[0], 401)

    def test_ticket_exchange_replay_and_expiry(self):
        with TaskHarness() as harness:
            ticket = harness.sessions.issue()
            status, body, headers = harness.request("POST", "/api/session/exchange", {"ticket": ticket})
            self.assertEqual(status, 200)
            self.assertEqual(sorted(body["scopes"]), sorted(OWNER_SCOPES))
            self.assertIn(f"{SESSION_COOKIE}={body['session_token']}", headers.get("Set-Cookie", ""))
            self.assertIn("HttpOnly", headers.get("Set-Cookie", ""))
            self.assertIn("SameSite=Strict", headers.get("Set-Cookie", ""))
            self.assertEqual(harness.request("POST", "/api/session/exchange", {"ticket": ticket})[0], 403)
            expired = harness.sessions.issue()
            harness.sessions._tickets[expired] = time.monotonic() - 1
            status, body, _ = harness.request("POST", "/api/session/exchange", {"ticket": expired})
            self.assertEqual(status, 410)
            self.assertEqual(body["error"]["code"], "ticket_expired")
            self.assertEqual(harness.request("POST", "/api/session/exchange", {"ticket": "wrong"})[0], 403)

    def test_meta_hides_write_token_in_task_mode_and_exposes_desktop_channel(self):
        with TaskHarness() as harness:
            status, meta, _ = harness.request("GET", "/api/meta")
            self.assertEqual(status, 200)
            self.assertIsNone(meta["token"])
            self.assertEqual(meta["pipeline_mode"], "task")
            self.assertEqual(meta["auth"]["exchange"], "/api/session/exchange")
            ticket_file = harness.vault / "个人学习状态" / "runtime" / "desktop_ticket.json"
            self.assertTrue(ticket_file.exists())
            self.assertTrue(harness.sessions.valid(harness.tickets.read()))

    def test_ext_readonly_identity_has_no_task_write(self):
        with TaskHarness(env={"PKA_EXT_TOKEN": "ext-token-test"}) as harness:
            path = harness.source_file()
            payload = {"contract_version": 1, "request_id": "ext-1",
                       "source": {"kind": "file", "path": str(path)},
                       "options": {"pipeline": "note"}}
            status, body, _ = harness.request("POST", "/api/tasks", payload,
                                              headers={"Authorization": "Bearer ext-token-test"})
            self.assertEqual(status, 403, body)
            self.assertEqual(body["error"]["code"], "permission_denied")

    def test_legacy_mode_keeps_old_token_and_reports_task_mode_honestly(self):
        with TaskHarness(mode="legacy", start_worker=False) as harness:
            status, meta, _ = harness.request("GET", "/api/meta")
            self.assertEqual(meta["pipeline_mode"], "legacy")
            self.assertTrue(meta["token"])       # 旧入口兼容：legacy 保留明文令牌
            status, body, _ = harness.request("POST", "/api/tasks", {"contract_version": 1},
                                              session=harness.browser_session())
            self.assertEqual(status, 400)
            self.assertEqual(body["error"]["code"], "not_supported")
            caps_status, caps, _ = harness.request("GET", "/api/tasks/capabilities",
                                                  session=harness.browser_session())
            self.assertEqual(caps_status, 200)
            self.assertEqual(caps["pipeline_mode"], "legacy")

    def test_submit_idempotency_conflict_and_snapshot(self):
        with TaskHarness() as harness:
            session = harness.browser_session()
            path = harness.source_file()
            status, first = harness.submit(session, path, "req-A")
            self.assertEqual(status, 202, first)
            self.assertEqual(first["contract_version"], 1)
            self.assertEqual(first["status"], "queued")
            self.assertEqual([stage["name"] for stage in first["stages"]],
                             ["acquire", "extract", "analyze", "generate", "validate",
                              "review", "commit", "sync"])
            self.assertTrue(all(stage["status"] == "pending" for stage in first["stages"]))
            self.assertEqual(first["outputs"][0]["kind"], "source_file")
            self.assertFalse(first["outputs"][0]["path"].startswith("/"))
            replay_status, replay = harness.submit(session, path, "req-A")
            self.assertEqual(replay_status, 202)
            self.assertEqual(replay["task_id"], first["task_id"])
            other = harness.source_file(name="另一份.md", text=SOURCE_MATERIAL + "\n差异\n")
            conflict_status, conflict = harness.submit(session, other, "req-A")
            self.assertEqual(conflict_status, 409)
            self.assertEqual(conflict["error"]["code"], "request_conflict")
            self.assertEqual(harness.request("POST", "/api/tasks", {
                "contract_version": 2, "request_id": "req-B",
                "source": {"kind": "file", "path": str(harness.source_file(name="第三份.md"))},
                "options": {"pipeline": "note"}}, session=session)[0], 400)
            fetched_status, fetched, _ = harness.request("GET", f"/api/tasks/{first['task_id']}",
                                                        session=session)
            self.assertEqual(fetched_status, 200)
            self.assertEqual(fetched["task_id"], first["task_id"])

    def test_file_outside_authorized_roots_is_rejected(self):
        with TaskHarness() as harness:
            outside = Path(harness._tmp.name).parent / "外部材料.md"
            outside.write_text(SOURCE_MATERIAL, encoding="utf-8")
            self.addCleanup(lambda: outside.unlink(missing_ok=True))
            status, body = harness.submit(harness.browser_session(), outside, "req-outside")
            self.assertEqual(status, 403, body)
            self.assertEqual(body["error"]["code"], "permission_denied")

    def test_capabilities_report_missing_providers_and_worker(self):
        with TaskHarness() as harness:
            status, body, _ = harness.request("GET", "/api/tasks/capabilities",
                                              session=harness.browser_session())
            self.assertEqual(status, 200)
            self.assertEqual(body["contract_version"], 1)
            self.assertEqual(body["pipeline_mode"], "task")
            by_name = {item["name"]: item for item in body["capabilities"]}
            self.assertIn("note", by_name)
            # 本测试环境未配置模型与 Notion：能力如实报告不可用，且写明缺什么
            self.assertFalse(by_name["note"]["available"])
            detail = by_name.get("note_detail", {}).get("reason", "")
            self.assertIn("analyze", by_name["note"]["reason"] + detail)
            self.assertIn("sync", by_name["note"]["reason"] + detail)
            self.assertFalse(by_name["transcribe_then_note"]["available"])
            self.assertIn("转写", by_name["transcribe_then_note"]["reason"])
            self.assertFalse(by_name["notion_sync"]["available"])

    def test_browser_and_desktop_see_same_owner_tasks(self):
        with TaskHarness() as harness:
            browser = harness.browser_session()
            status, task = harness.submit(browser, harness.source_file(), "req-owner")
            self.assertEqual(status, 202)
            desktop = harness.desktop_session()
            fetched_status, fetched, _ = harness.request("GET", f"/api/tasks/{task['task_id']}",
                                                        session=desktop)
            self.assertEqual(fetched_status, 200)
            self.assertEqual(fetched["task_id"], task["task_id"])
            other_status, other, _ = harness.request(
                "GET", f"/api/tasks/{task['task_id']}",
                headers={"Authorization": "Bearer nothing"})
            self.assertEqual(other_status, 401)


class TaskControlApiTests(unittest.TestCase):
    def test_cancel_and_retry_routes_record_truthfully(self):
        # 不启动工作线程：控制语义本身要确定，不等竞态
        with TaskHarness(start_worker=False) as harness:
            session = harness.browser_session()
            status, task = harness.submit(session, harness.source_file(), "ctl-1")
            self.assertEqual(status, 202)
            cancel_status, cancelled, _ = harness.request(
                "POST", f"/api/tasks/{task['task_id']}/cancel", {"request_id": "ctl-cancel"}, session=session)
            self.assertEqual(cancel_status, 200, cancelled)
            self.assertTrue(cancelled["cancel_requested"])
            # 同一控制请求标识重放：幂等，不重复改状态
            replay_status, replay, _ = harness.request(
                "POST", f"/api/tasks/{task['task_id']}/cancel", {"request_id": "ctl-cancel"}, session=session)
            self.assertEqual(replay_status, 200)
            self.assertEqual(replay["cancel_requested"], True)
            # 控制请求缺少 request_id：明确拒绝，不静默执行
            self.assertEqual(harness.request(
                "POST", f"/api/tasks/{task['task_id']}/cancel", {}, session=session)[0], 400)
            retry_status, retried, _ = harness.request(
                "POST", f"/api/tasks/{task['task_id']}/retry", {"request_id": "ctl-retry"}, session=session)
            self.assertEqual(retry_status, 202, retried)
            self.assertEqual(retried["attempt"], 2)
            self.assertFalse(retried["cancel_requested"])

    def test_unknown_task_is_404_not_500(self):
        with TaskHarness() as harness:
            session = harness.browser_session()
            status, body, _ = harness.request("GET", "/api/tasks/does-not-exist", session=session)
            self.assertEqual(status, 404)
            self.assertEqual(body["error"]["code"], "not_found")


class PipelineTests(unittest.TestCase):
    """假适配器的调度语义：不经过 HTTP，直接验证工作流接线。"""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.vault = make_test_vault(Path(self.temp.name))
        self.calls = []
        self.store = TaskStore(self.vault / "个人学习状态" / "tasks" / "_ledger")

    def service(self, adapters):
        return WorkflowService(self.store, adapters=adapters,
                               pipelines={"note": ("acquire", "extract", "generate", "commit")},
                               authorize=lambda *args: None)

    def submit(self, service, path, request_id="p-1"):
        return service.submit({"contract_version": 1, "request_id": request_id,
                               "source": {"kind": "file", "path": str(path)},
                               "options": {"pipeline": "note"}},
                              principal=Principal(OWNER_SUBJECT, OWNER_SCOPES))

    def test_fake_pipeline_runs_all_stages_and_records_artifacts(self):
        source = self.vault / "收件箱" / "合成.md"
        source.parent.mkdir(parents=True, exist_ok=True)
        source.write_text(SOURCE_MATERIAL, encoding="utf-8")
        adapters = {
            "extract": FakeAdapter("extract", record=self.calls,
                                   artifacts=[("source_text", "source.txt")]),
            "generate": FakeAdapter("generate", record=self.calls,
                                    artifacts=[("note_body", "note.md")]),
            "commit": FakeAdapter("commit", record=self.calls,
                                  artifacts=[("note", "final.md")]),
        }
        service = self.service(adapters)
        task = self.submit(service, source)
        service.execute(task["task_id"])
        snapshot = service.get(task["task_id"], principal=Principal(OWNER_SUBJECT, OWNER_SCOPES))
        self.assertEqual(snapshot["status"], "succeeded")
        self.assertEqual(self.calls, ["extract", "generate", "commit"])
        self.assertEqual([item["kind"] for item in snapshot["outputs"]],
                         ["source_file", "source_text", "note_body", "note"])
        self.assertEqual(snapshot["progress"]["completed_units"], 0)  # 假适配器未报进度，不编造
        self.assertTrue(all(stage["status"] == "completed" for stage in snapshot["stages"]))

    def test_failed_stage_keeps_earlier_artifacts_and_stops_chain(self):
        source = self.vault / "收件箱" / "合成.md"
        source.parent.mkdir(parents=True, exist_ok=True)
        source.write_text(SOURCE_MATERIAL, encoding="utf-8")
        adapters = {
            "extract": FakeAdapter("extract", record=self.calls,
                                   artifacts=[("source_text", "source.txt")]),
            "generate": FakeAdapter("generate", fail=True, record=self.calls),
            "commit": FakeAdapter("commit", record=self.calls),
        }
        service = self.service(adapters)
        task = self.submit(service, source)
        service.execute(task["task_id"])
        snapshot = service.get(task["task_id"], principal=Principal(OWNER_SUBJECT, OWNER_SCOPES))
        self.assertEqual(snapshot["status"], "failed")
        self.assertEqual(self.calls, ["extract", "generate"])
        self.assertIn("source_text", [item["kind"] for item in snapshot["outputs"]])
        stages = {stage["name"]: stage for stage in snapshot["stages"]}
        self.assertEqual(stages["extract"]["status"], "completed")
        self.assertEqual(stages["generate"]["status"], "failed")
        self.assertEqual(stages["commit"]["status"], "pending")

    def test_cancel_before_commit_never_commits(self):
        source = self.vault / "收件箱" / "合成.md"
        source.parent.mkdir(parents=True, exist_ok=True)
        source.write_text(SOURCE_MATERIAL, encoding="utf-8")
        committed = []
        adapters = {
            "extract": FakeAdapter("extract", record=self.calls,
                                   artifacts=[("source_text", "source.txt")]),
            "generate": FakeAdapter("generate", record=self.calls, delay=0.05,
                                    artifacts=[("note_body", "note.md")]),
            "commit": FakeAdapter("commit", record=committed),
        }
        service = self.service(adapters)
        task = self.submit(service, source)
        worker = threading.Thread(target=service.execute, args=(task["task_id"],), daemon=True)
        worker.start()
        deadline = time.time() + 5
        while time.time() < deadline and "generate" not in self.calls:
            time.sleep(0.01)
        service.cancel(task["task_id"], request_id="c-1", principal=Principal(OWNER_SUBJECT, OWNER_SCOPES))
        worker.join(timeout=5)
        snapshot = service.get(task["task_id"], principal=Principal(OWNER_SUBJECT, OWNER_SCOPES))
        self.assertEqual(snapshot["status"], "cancelled")
        self.assertEqual(committed, [])                      # 取消后不迟到提交
        self.assertNotIn("note", [item["kind"] for item in snapshot["outputs"]])

    def test_cancel_after_commit_keeps_true_result(self):
        source = self.vault / "收件箱" / "合成.md"
        source.parent.mkdir(parents=True, exist_ok=True)
        source.write_text(SOURCE_MATERIAL, encoding="utf-8")
        entered, gate = threading.Event(), threading.Event()
        adapters = {
            "extract": FakeAdapter("extract", record=self.calls,
                                   artifacts=[("source_text", "source.txt")]),
            "generate": FakeAdapter("generate", record=self.calls,
                                    artifacts=[("note_body", "note.md")]),
            # 提交经 run_effect(local_commit) 执行；在提交进行中发出取消请求
            "commit": FakeAdapter("commit", record=self.calls, artifacts=[("note", "final.md")],
                                  effect_kind="local_commit", entered=entered, gate=gate),
        }
        service = self.service(adapters)
        task = self.submit(service, source)
        worker = threading.Thread(target=service.execute, args=(task["task_id"],), daemon=True)
        worker.start()
        self.assertTrue(entered.wait(5), "提交副作用未开始")
        cancel_thread = threading.Thread(
            target=service.cancel, args=(task["task_id"],),
            kwargs={"request_id": "c-2", "principal": Principal(OWNER_SUBJECT, OWNER_SCOPES)},
            daemon=True)
        cancel_thread.start()
        time.sleep(0.05)
        gate.set()          # 本地提交完成；取消与提交共用任务锁，取消在此之后才生效
        cancel_thread.join(timeout=5)
        worker.join(timeout=5)
        snapshot = service.get(task["task_id"], principal=Principal(OWNER_SUBJECT, OWNER_SCOPES))
        # 提交已经发生：不假装"什么都没发生"——成果、真实结果与副作用记录都保留
        self.assertIn("commit", self.calls)
        self.assertIn("note", [item["kind"] for item in snapshot["outputs"]])
        self.assertEqual(snapshot["status"], "cancelled")
        self.assertTrue(snapshot["cancel_requested"])
        effects = self.store.get(task["task_id"])["effects"]
        self.assertEqual(effects["commit:fake-effect"]["status"], "confirmed")
        self.assertEqual(effects["commit:fake-effect"]["kind"], "local_commit")

    def test_retry_reuses_completed_stages(self):
        source = self.vault / "收件箱" / "合成.md"
        source.parent.mkdir(parents=True, exist_ok=True)
        source.write_text(SOURCE_MATERIAL, encoding="utf-8")
        generate = FakeAdapter("generate", fail=True, record=self.calls)
        adapters = {
            "extract": FakeAdapter("extract", record=self.calls,
                                   artifacts=[("source_text", "source.txt")]),
            "generate": generate,
            "commit": FakeAdapter("commit", record=self.calls),
        }
        service = self.service(adapters)
        task = self.submit(service, source)
        service.execute(task["task_id"])
        self.assertEqual(self.calls, ["extract", "generate"])
        generate.fail = False
        generate.artifact_spec = [("note_body", "note.md")]
        service.retry(task["task_id"], request_id="r-1", principal=Principal(OWNER_SUBJECT, OWNER_SCOPES))
        service.execute(task["task_id"])
        self.assertEqual(self.calls, ["extract", "generate", "generate", "commit"])
        self.assertEqual(service.get(task["task_id"], principal=Principal(OWNER_SUBJECT, OWNER_SCOPES))["attempt"], 2)

    def test_other_identity_cannot_read_or_control_task(self):
        source = self.vault / "收件箱" / "合成.md"
        source.parent.mkdir(parents=True, exist_ok=True)
        source.write_text(SOURCE_MATERIAL, encoding="utf-8")
        service = self.service({"extract": FakeAdapter("extract"), "generate": FakeAdapter("generate"),
                                "commit": FakeAdapter("commit")})
        task = self.submit(service, source, "owner-1")
        other = Principal("local:someone-else", frozenset({"tasks:read:self", "tasks:control:self"}))
        with self.assertRaises(WorkflowError) as read_error:
            service.get(task["task_id"], principal=other)
        self.assertEqual(read_error.exception.http_status, 404)     # 不可见，不泄露存在性
        with self.assertRaises(WorkflowError) as control_error:
            service.cancel(task["task_id"], request_id="steal-1", principal=other)
        self.assertEqual(control_error.exception.code, "not_found")
        read_only = Principal(OWNER_SUBJECT, frozenset({"tasks:read:self"}))
        with self.assertRaises(WorkflowError) as scope_error:
            service.cancel(task["task_id"], request_id="no-scope", principal=read_only)
        self.assertEqual(scope_error.exception.http_status, 403)

    def test_only_one_worker_lease_per_vault(self):
        adapters = {"extract": FakeAdapter("extract"), "generate": FakeAdapter("generate"),
                    "commit": FakeAdapter("commit")}
        first, second = self.service(adapters), self.service(adapters)
        first.start()
        self.addCleanup(first.stop)
        with self.assertRaises(WorkflowError) as caught:
            second.start()
        self.assertEqual(caught.exception.code, "worker_busy")
        self.assertEqual(second.stop()["remaining"], [])
        self.assertTrue(first.stop(timeout=5)["stopped"])

    def test_sync_stage_skipped_without_publish_target(self):
        source = self.vault / "收件箱" / "合成.md"
        source.parent.mkdir(parents=True, exist_ok=True)
        source.write_text(SOURCE_MATERIAL, encoding="utf-8")
        service = WorkflowService(
            self.store, adapters={"extract": FakeAdapter("extract"),
                                  "commit": FakeAdapter("commit")},
            pipelines={"note": ("acquire", "extract", "commit", "sync")},
            authorize=lambda *args: None)
        task = self.submit(service, source, "s-1")
        service.execute(task["task_id"])
        snapshot = service.get(task["task_id"], principal=Principal(OWNER_SUBJECT, OWNER_SCOPES))
        stages = {stage["name"]: stage for stage in snapshot["stages"]}
        self.assertEqual(stages["sync"]["status"], "skipped")
        self.assertEqual(snapshot["status"], "succeeded")


class FakeReviewer:
    """确定性假审核者：不调用外部模型，只用于验证链路接线与取舍。"""

    def __init__(self, verdict="pass"):
        self.verdict = verdict
        self.calls = []

    def describe(self):
        return "假审核者（确定性）"

    def review(self, source_text, note_body, skill_text, *, task_context=None):
        self.calls.append({"source_chars": len(source_text), "note_chars": len(note_body)})
        if task_context is not None:
            task_context.report_progress(1, 1)
        issues = ([] if self.verdict == "pass"
                  else [{"reason": "合成审核意见：机制解释不足", "note_excerpt": note_body[:12]}])
        return {"verdict": self.verdict, "issues": issues,
                "coverage": {"complete": True, "segments": 1, "total": 1},
                "reviewer_mode": "fake", "model": "fake-reviewer"}


class FullChainTests(unittest.TestCase):
    """离线验收：一个文件完整生成 → AI 审核 → 自动入库；拒绝/未配置不入库。

    外部依赖全部用确定性假实现（假分析器、假加工模型、假审核者），链路本体是真实代码。
    """

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.vault = make_test_vault(Path(self.temp.name))
        self.env = patch.dict(os.environ, {"PKA_TASK_PIPELINE": "task"})
        self.env.start()
        self.addCleanup(self.env.stop)
        from test_inbox import FakeAnalyzer
        from test_note_processor import FakeNoteModel, make_processor

        self.service = build_service(self.vault)
        self.addCleanup(lambda: self.service.workflow.stop(timeout=5))
        self.service.inbox.analyzer = FakeAnalyzer("new_node")
        self.service.discovery.analyzer = self.service.inbox.analyzer
        self.service.inbox.budget = None
        self.skills = Path(__file__).resolve().parents[1] / "skills" / "note_processing"
        self.processor = make_processor(FakeNoteModel(), self.skills, self.vault)
        self.service.discovery.note_processor = self.processor

    def submit_and_run(self, name="离线材料.md"):
        path = self.vault / "收件箱" / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("# 水位线原理\n\n" + "水位线用于记录流水线已处理位置，重启后从中恢复，必须持久化。" * 40 + "\n",
                        encoding="utf-8")
        request = {"contract_version": 1, "request_id": "chain-" + name,
                   "source": {"kind": "file", "path": str(path)},
                   "options": {"pipeline": "note", "publish_to": None}}
        task = self.service.workflow.submit(request, principal=Principal(OWNER_SUBJECT, OWNER_SCOPES))
        self.service.workflow.execute(task["task_id"])
        return self.service.workflow.get(task["task_id"], principal=Principal(OWNER_SUBJECT, OWNER_SCOPES))

    def test_review_pass_collects_note(self):
        self.service.discovery.note_reviewer = FakeReviewer("pass")
        snapshot = self.submit_and_run()
        self.assertEqual(snapshot["status"], "succeeded", snapshot.get("error"))
        stages = {stage["name"]: stage["status"] for stage in snapshot["stages"]}
        self.assertEqual(stages["extract"], "completed")
        self.assertEqual(stages["generate"], "completed")
        self.assertEqual(stages["validate"], "completed")
        self.assertEqual(stages["review"], "completed")
        self.assertEqual(stages["commit"], "completed")
        self.assertEqual(stages["sync"], "skipped")          # publish_to=null：不假装已同步
        self.assertEqual(snapshot["result"]["disposition"], "collected")
        # 收录不等于已掌握：note_status 沿用入库结果（未测验）
        self.assertIn("未测验", snapshot["result"]["note_status"])
        self.assertTrue(self.service.discovery.note_reviewer.calls)   # 确实调用了审核
        suggestions = self.service.discovery._load_suggestions()
        self.assertTrue(suggestions and suggestions[0]["status"] == "accepted")
        relpath = suggestions[0]["accepted"]["note_relpath"]
        self.assertIn("正式笔记", relpath)
        self.assertTrue((self.vault / relpath).is_file())

    def _note_files(self):
        """知识库里的笔记文件（排除收件箱与任务工作区）。"""
        return {p for p in self.vault.rglob("*.md")
                if "收件箱" not in p.parts and "个人学习状态" not in p.parts
                and "个人学习状态" not in str(p)}

    def test_review_rejection_is_not_collected(self):
        self.service.discovery.note_reviewer = FakeReviewer("revise")
        before = self._note_files()
        snapshot = self.submit_and_run(name="被拒材料.md")
        # 最多两轮修正后仍不通过：保留候选、记录原因，不入库
        self.assertEqual(snapshot["status"], "needs_review", snapshot.get("error"))
        self.assertEqual(snapshot["result"]["note_status"], "review_revise")
        self.assertEqual(len(self.service.discovery.note_reviewer.calls), 3)  # 首审 + 两轮复核
        suggestions = self.service.discovery._load_suggestions()
        self.assertTrue(suggestions and suggestions[0]["status"] == "pending")
        self.assertEqual(before, self._note_files())   # 候选留在任务工作区，正式/候选笔记目录没有新文件

    def test_missing_reviewer_blocks_instead_of_false_pass(self):
        self.service.discovery.note_reviewer = None
        snapshot = self.submit_and_run(name="无审核者.md")
        self.assertEqual(snapshot["status"], "blocked", snapshot.get("error"))
        stages = {stage["name"]: stage for stage in snapshot["stages"]}
        self.assertEqual(stages["review"]["status"], "blocked")
        self.assertEqual(stages["commit"]["status"], "pending")     # 没通过就不提交
        self.assertNotEqual(snapshot["result"] or {}, {"disposition": "collected"})
        self.assertIsNone(snapshot["result"].get("disposition") if snapshot["result"] else None)
        suggestions = self.service.discovery._load_suggestions()
        self.assertTrue(suggestions and suggestions[0]["status"] == "pending")


class ServeLifecycleTests(unittest.TestCase):
    """主程序启动/停止接线：task 模式下起工作线程、发入口票据、退出时停止任务。"""

    def test_serve_starts_and_stops_task_worker_with_ticket_entry(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        vault = make_test_vault(Path(temp.name))
        import socket

        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            port = probe.getsockname()[1]
        from pka_graph.server import serve

        with patch.dict(os.environ, {"PKA_TASK_PIPELINE": "task"}):
            thread = threading.Thread(target=serve, args=(vault, port), daemon=True)
            thread.start()
            base = f"http://127.0.0.1:{port}"
            deadline = time.time() + 15
            meta = None
            while time.time() < deadline:
                try:
                    with urllib.request.urlopen(f"{base}/api/meta", timeout=2) as response:
                        meta = json.loads(response.read().decode("utf-8"))
                        break
                except Exception:  # noqa: BLE001 —— 启动中
                    time.sleep(0.1)
            self.assertIsNotNone(meta, "服务未在超时内启动")
            self.assertEqual(meta["pipeline_mode"], "task")
            self.assertIsNone(meta["token"])                    # task 模式不下发明文写令牌
            self.assertTrue(meta["tasks"]["available"])         # 工作线程与账本已就绪
            entry = vault / "个人学习状态" / "runtime" / "browser_entry.url"
            self.assertTrue(entry.exists())
            self.assertIn("#ticket=", entry.read_text(encoding="utf-8"))
            ticket_file = vault / "个人学习状态" / "runtime" / "desktop_ticket.json"
            self.assertTrue(ticket_file.exists())
            ticket = json.loads(ticket_file.read_text(encoding="utf-8"))["ticket"]
            exchange = urllib.request.Request(f"{base}/api/session/exchange",
                                              data=json.dumps({"ticket": ticket}).encode("utf-8"),
                                              headers={"Content-Type": "application/json"}, method="POST")
            with urllib.request.urlopen(exchange, timeout=5) as response:
                session = json.loads(response.read().decode("utf-8"))["session_token"]
            shutdown = urllib.request.Request(f"{base}/api/shutdown", data=b"{}",
                                              headers={"Content-Type": "application/json",
                                                       SESSION_HEADER: session}, method="POST")
            with urllib.request.urlopen(shutdown, timeout=5) as response:
                self.assertEqual(response.status, 200)
            thread.join(timeout=10)
            self.assertFalse(thread.is_alive(), "退出后任务工作线程未停止")


class InboxBridgeTests(unittest.TestCase):
    def test_ingest_registers_single_task_and_keeps_legacy_fields(self):
        with TaskHarness() as harness:
            session = harness.browser_session()
            import base64

            body = {"name": "投递.md", "data_b64": base64.b64encode(SOURCE_MATERIAL.encode()).decode()}
            status, response, _ = harness.request("POST", "/api/inbox/ingest", body, session=session)
            self.assertEqual(status, 200, response)
            self.assertEqual(response["status"], "queued")
            self.assertTrue(response["task_id"])
            registry_status, registry, _ = harness.request("GET", "/api/inbox/registry", session=session)
            self.assertEqual(registry_status, 200)
            entry = registry["processed"]["投递.md"]
            self.assertEqual(entry["task_id"], response["task_id"])
            self.assertEqual(harness.service.inbox.status()["task_mode"], True)
            # 同一内容重复投递：复用同一任务，不重复登记、不重复烧模型
            second_status, second, _ = harness.request("POST", "/api/inbox/ingest", body, session=session)
            self.assertEqual(second_status, 200)
            self.assertEqual(second["task_id"], response["task_id"])
            deadline = time.time() + 15
            snapshot = harness.service.workflow.store.get(response["task_id"])
            while time.time() < deadline and snapshot["status"] in ("queued", "running", "retry_wait"):
                time.sleep(0.05)
                snapshot = harness.service.workflow.store.get(response["task_id"])
            stages = {stage["name"]: stage for stage in snapshot["stages"]}
            # 真实适配器：extract 完成；analyze 因本环境未配置模型 blocked —— 缺模型不当作成功跳过
            self.assertEqual(stages["extract"]["status"], "completed")
            self.assertEqual(stages["analyze"]["status"], "blocked")
            self.assertEqual(stages["analyze"]["error"]["code"], "config_error")
            self.assertIn("模型", stages["analyze"]["error"]["message"])
            self.assertNotEqual(snapshot["status"], "succeeded")
            self.assertEqual(snapshot["status"], "blocked")
            self.assertEqual(snapshot["result"], None)
            self.assertIn("source_text", [item["kind"] for item in snapshot["outputs"]])


if __name__ == "__main__":
    unittest.main()
