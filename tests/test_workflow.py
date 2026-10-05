"""统一任务协议的离线行为测试；所有原件、产物和账本都在临时库。"""

import copy
import sqlite3
import threading
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from pka_graph.task_store import TaskStore, TaskStoreError
from pka_graph.workflow import Principal, StageResult, WorkflowError, WorkflowService


class Adapter:
    available = True

    def __init__(self, action=None):
        self.action = action or (lambda context: StageResult("completed"))
        self.calls = 0

    def execute(self, context):
        self.calls += 1
        return self.action(context)


class WorkflowTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.vault = Path(self.temp.name)
        self.source = self.vault / "原始材料.txt"
        self.source.write_text("完整原文，不能按前五千字截断。", encoding="utf-8")
        self.store = TaskStore(self.vault / "个人学习状态" / "tasks" / "_ledger")
        self.owner = Principal("local:owner", frozenset({"tasks:submit", "tasks:read:self",
                                                       "tasks:control:self", "publish:notion"}))
        self.other = Principal("agent:other", self.owner.scopes)

    def service(self, stages=("acquire", "extract", "generate", "validate", "review", "commit", "sync"), adapters=None):
        adapters = adapters or {stage: Adapter() for stage in stages if stage != "acquire"}
        return WorkflowService(self.store, adapters=adapters, pipelines={"note": tuple(stages)},
                               authorize=lambda principal, operation, task: None)

    def submit(self, service, request_id="request-1", principal=None, publish_to=None):
        return service.submit({"contract_version": 1, "request_id": request_id,
                               "source": {"kind": "file", "path": str(self.source)},
                               "options": {"pipeline": "note", "publish_to": publish_to}},
                              principal=principal or self.owner)

    def test_owner_request_replay_is_idempotent_and_owner_scoped(self):
        service = self.service()
        first = self.submit(service)
        replay = self.submit(service)
        other = self.submit(service, principal=self.other)
        self.assertEqual(first["task_id"], replay["task_id"])
        self.assertNotEqual(first["task_id"], other["task_id"])
        self.assertEqual(len(self.store.all()), 2)

    def test_replay_with_changed_source_is_conflict(self):
        service = self.service()
        self.submit(service)
        self.source.write_text("同路径的新版本", encoding="utf-8")
        with self.assertRaises(WorkflowError) as result:
            self.submit(service)
        self.assertEqual(result.exception.code, "request_conflict")
        self.assertEqual(result.exception.http_status, 409)

    def test_other_identity_cannot_read_cancel_or_retry(self):
        service = self.service()
        task_id = self.submit(service)["task_id"]
        for operation in (
            lambda: service.get(task_id, principal=self.other),
            lambda: service.cancel(task_id, request_id="cancel-other", principal=self.other),
            lambda: service.retry(task_id, request_id="retry-other", principal=self.other),
        ):
            with self.assertRaises(WorkflowError) as result:
                operation()
            self.assertEqual(result.exception.http_status, 404)
        self.assertFalse(service.get(task_id, principal=self.owner)["cancel_requested"])

    def test_read_only_identity_cannot_submit(self):
        service = self.service()
        read_only = Principal("ext:reader", frozenset({"tasks:read:self"}))
        with self.assertRaises(WorkflowError) as result:
            self.submit(service, principal=read_only)
        self.assertEqual(result.exception.http_status, 403)
        self.assertEqual(self.store.all(), [])

    def test_frozen_source_preserves_tail_and_ignores_later_original_edits(self):
        original = "全文内容。" * 12000 + "最终结论：这个尾部必须完整保存。"
        self.source.write_text(original, encoding="utf-8")
        seen = []
        def extract(context):
            path = context.vault_root / context.source["frozen_path"]
            seen.append(path.read_text(encoding="utf-8"))
            return StageResult("completed")
        service = self.service(("acquire", "extract"), {"extract": Adapter(extract)})
        task_id = self.submit(service)["task_id"]
        self.source.write_text("用户已经修改了原始文件", encoding="utf-8")
        service.execute(task_id)
        self.assertEqual(seen, [original])
        self.assertTrue(seen[0].endswith("这个尾部必须完整保存。"))
        self.assertEqual(service.get(task_id, principal=self.owner)["status"], "succeeded")

    def test_cancelled_before_execute_never_commits(self):
        writes = []
        def commit(context):
            context.run_effect("note-v1", kind="local_commit", action=lambda: writes.append("written") or {})
            return StageResult("completed")
        service = self.service(("acquire", "commit"), {"commit": Adapter(commit)})
        task_id = self.submit(service)["task_id"]
        service.cancel(task_id, request_id="cancel-1", principal=self.owner)
        service.execute(task_id)
        self.assertEqual(writes, [])
        self.assertEqual(service.get(task_id, principal=self.owner)["status"], "cancelled")

    def test_cancel_during_generation_prevents_later_commit(self):
        entered, release = threading.Event(), threading.Event()
        writes = []
        def generate(context):
            entered.set()
            release.wait(2)
            return StageResult("completed")
        def commit(context):
            context.run_effect("note-v1", kind="local_commit", action=lambda: writes.append("written") or {})
            return StageResult("completed")
        service = self.service(("acquire", "generate", "commit"),
                               {"generate": Adapter(generate), "commit": Adapter(commit)})
        task_id = self.submit(service)["task_id"]
        worker = threading.Thread(target=service.execute, args=(task_id,))
        self.addCleanup(release.set)
        worker.start()
        self.assertTrue(entered.wait(1))
        service.cancel(task_id, request_id="cancel-running", principal=self.owner)
        release.set()
        worker.join(2)
        self.assertFalse(worker.is_alive())
        self.assertEqual(writes, [])
        self.assertEqual(service.get(task_id, principal=self.owner)["status"], "cancelled")

    def test_retry_reuses_completed_stages_and_control_request(self):
        extract = Adapter()
        generate = Adapter()
        def generation(context):
            if generate.calls == 1:
                return StageResult("failed", error={"code": "network_error", "message": "模拟临时故障", "retryable": False})
            return StageResult("completed")
        generate.action = generation
        commit = Adapter()
        service = self.service(("acquire", "extract", "generate", "commit"),
                               {"extract": extract, "generate": generate, "commit": commit})
        task_id = self.submit(service)["task_id"]
        service.execute(task_id)
        self.assertEqual(service.get(task_id, principal=self.owner)["status"], "failed")
        service.retry(task_id, request_id="retry-1", principal=self.owner)
        service.execute(task_id)
        replay = service.retry(task_id, request_id="retry-1", principal=self.owner)
        self.assertEqual(replay["status"], "succeeded")
        self.assertEqual(replay["attempt"], 2)
        self.assertEqual((extract.calls, generate.calls, commit.calls), (1, 2, 1))

    def test_remote_unknown_remains_unknown_and_cannot_be_retried(self):
        requests = []
        def sync(context):
            def action():
                requests.append("sent")
                raise TimeoutError("模拟发送后响应丢失")
            context.run_effect("notion-doc-v1", kind="remote_write", action=action)
            return StageResult("completed")
        service = self.service(("acquire", "sync"), {"sync": Adapter(sync)})
        task_id = self.submit(service, publish_to="approved-parent")["task_id"]
        service.execute(task_id)
        self.assertEqual(service.get(task_id, principal=self.owner)["status"], "unknown_result")
        service.recover()
        self.assertEqual(service.get(task_id, principal=self.owner)["status"], "unknown_result")
        with self.assertRaises(WorkflowError) as result:
            service.retry(task_id, request_id="retry-unknown", principal=self.owner)
        self.assertEqual(result.exception.http_status, 409)
        self.assertEqual(requests, ["sent"])

    def test_final_completion_cannot_overwrite_successful_cancellation(self):
        service = self.service(("acquire", "extract"), {"extract": Adapter()})
        task_id = self.submit(service)["task_id"]
        update = self.store.update
        cancellation_finished = threading.Event()
        cancellation_results = []
        cancel_thread = None
        triggered = False
        def cancel():
            try:
                cancellation_results.append(service.cancel(task_id, request_id="cancel-final", principal=self.owner))
            except WorkflowError as exc:
                cancellation_results.append(exc)
            finally:
                cancellation_finished.set()
        def intercepted_update(identity, mutate):
            nonlocal triggered, cancel_thread
            if identity == task_id and not triggered:
                previous = self.store.get(identity)
                proposed = copy.deepcopy(previous)
                mutate(proposed)
                if proposed["status"] == "succeeded":
                    triggered = True
                    cancel_thread = threading.Thread(target=cancel)
                    cancel_thread.start()
                    # 锁外旧实现会在这里完成取消；同锁实现会让取消等完成后返回409。
                    cancellation_finished.wait(0.2)
            return update(identity, mutate)
        self.store.update = intercepted_update
        service.execute(task_id)
        self.assertTrue(triggered)
        cancel_thread.join(2)
        self.assertFalse(cancel_thread.is_alive())
        final = service.get(task_id, principal=self.owner)
        result = cancellation_results[0]
        if isinstance(result, WorkflowError):
            self.assertEqual(result.http_status, 409)
            self.assertEqual(final["status"], "succeeded")
        else:
            self.assertEqual(result["status"], "cancelled")
            self.assertEqual(final["status"], "cancelled")

    def test_second_worker_for_same_vault_is_rejected(self):
        first, second = self.service(), self.service()
        self.addCleanup(first.stop)
        self.addCleanup(second.stop)
        first.start()
        with self.assertRaises(WorkflowError) as result:
            second.start()
        self.assertEqual(result.exception.code, "worker_busy")
        self.assertTrue(first.stop(timeout=1)["stopped"])

    def test_tasks_admin_does_not_grant_notion_publication(self):
        service = self.service()
        admin = Principal("local:admin", frozenset({"tasks:admin"}))
        with self.assertRaises(WorkflowError) as result:
            self.submit(service, principal=admin, publish_to="approved-parent")
        self.assertEqual(result.exception.http_status, 403)
        self.assertEqual(self.store.all(), [])

    def test_recovery_keeps_unknown_effect_and_honors_cancel(self):
        service = self.service(("acquire", "extract"), {"extract": Adapter()})
        cancelled = self.submit(service, request_id="recover-cancel")["task_id"]
        unknown = self.submit(service, request_id="recover-unknown")["task_id"]
        self.store.update(cancelled, lambda task: task.update(status="running", cancel_requested=True))
        def in_flight(task):
            task.update(status="running", stage="extract")
            task["effects"]["remote-v1"] = {"kind": "remote_write", "status": "in_flight", "stage": "extract"}
        self.store.update(unknown, in_flight)
        service.recover()
        self.assertEqual(service.get(cancelled, principal=self.owner)["status"], "cancelled")
        self.assertEqual(service.get(unknown, principal=self.owner)["status"], "unknown_result")

    def test_modified_frozen_artifact_is_blocked_before_model_stage(self):
        generation = Adapter()
        service = self.service(("acquire", "generate"), {"generate": generation})
        task_id = self.submit(service)["task_id"]
        stored = self.store.get(task_id)
        frozen = self.vault / stored["source"]["frozen_path"]
        frozen.write_text("人工修改过的冻结副本", encoding="utf-8")
        service.execute(task_id)
        self.assertEqual(generation.calls, 0)
        self.assertEqual(service.get(task_id, principal=self.owner)["status"], "blocked")

    def test_sqlite_connections_are_closed_after_reads_and_errors(self):
        connections = []
        connect = sqlite3.connect
        def tracked_connect(*args, **kwargs):
            connection = connect(*args, **kwargs)
            connections.append(connection)  # 保持强引用，不能靠垃圾回收掩盖泄漏。
            return connection
        with patch("pka_graph.task_store.sqlite3.connect", new=tracked_connect):
            self.store.all()
            self.store.get("missing")
            self.store.request("owner", "missing", "fingerprint")
            with self.assertRaises(TaskStoreError):
                self.store.update("missing", lambda task: None)
        self.assertEqual(len(connections), 4)
        for connection in connections:
            with self.assertRaises(sqlite3.ProgrammingError):
                connection.execute("SELECT 1")

    def test_recovery_failure_is_reported_by_start(self):
        service = self.service()
        def corrupt_recovery():
            raise TaskStoreError("模拟损坏账本")
        service.recover = corrupt_recovery
        self.addCleanup(service.stop)
        with self.assertRaises(WorkflowError) as result:
            service.start()
        self.assertEqual(result.exception.code, "worker_failed")

    def test_stop_timeout_does_not_start_sync_after_inflight_commit(self):
        entered, release = threading.Event(), threading.Event()
        def commit(context):
            def action():
                entered.set()
                release.wait(2)
                return {"node_id": "synthetic-node"}
            context.run_effect("commit-v1", kind="local_commit", action=action)
            return StageResult("completed")
        sync = Adapter()
        service = self.service(("acquire", "commit", "sync"),
                               {"commit": Adapter(commit), "sync": sync})
        self.addCleanup(service.stop)
        self.addCleanup(release.set)
        service.start()
        task_id = self.submit(service, publish_to="approved-parent")["task_id"]
        self.assertTrue(entered.wait(1))
        began = time.monotonic()
        result = service.stop(timeout=0.05)
        self.assertLess(time.monotonic() - began, 0.5)
        self.assertFalse(result["stopped"])
        self.assertIn(task_id, result["remaining"])
        release.set()
        service._thread.join(1)
        self.assertFalse(service._thread.is_alive())
        self.assertEqual(sync.calls, 0)
        self.assertEqual(service.get(task_id, principal=self.owner)["status"], "cancelled")


if __name__ == "__main__":
    unittest.main()
