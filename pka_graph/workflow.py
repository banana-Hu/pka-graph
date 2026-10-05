"""Single-file task orchestration with checkpoints and controlled effects."""
from __future__ import annotations

import hashlib
import json
import threading
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Mapping, Protocol

from .file_lock import FileMutex
from .task_store import RequestConflict, TaskStore


def _now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _hash_file(path: Path):
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


class WorkflowError(RuntimeError):
    def __init__(self, code, message, *, http_status=400):
        super().__init__(message)
        self.code, self.http_status = code, http_status

    def as_dict(self):
        return {"error": {"code": self.code, "message": str(self)}}


@dataclass(frozen=True)
class Principal:
    subject: str
    scopes: frozenset[str]


@dataclass(frozen=True)
class StageResult:
    outcome: str
    artifacts: tuple[dict, ...] = ()
    checkpoint: dict | None = None
    error: dict | None = None
    result: dict | None = None

    def __post_init__(self):
        if self.outcome not in {"completed", "skipped", "blocked", "needs_review", "unknown_result", "failed", "cancelled"}:
            raise ValueError("无效的阶段结果")


class StageAdapter(Protocol):
    def execute(self, context: "StageContext") -> StageResult: ...


class StageContext:
    def __init__(self, service, task, stage):
        self._service = service
        self.task_id, self.attempt, self.stage = task["task_id"], task["attempt"], stage
        self.source, self.options = dict(task["source"]), dict(task["options"])
        self.vault_root = service.vault_root
        self.work_dir = self.vault_root / "个人学习状态" / "tasks" / self.task_id / "work"
        self.work_dir.mkdir(parents=True, exist_ok=True)
        self.artifacts = tuple(task["outputs"])

    def cancel_requested(self):
        return self._service._stop.is_set() or bool(self._service.store.get(self.task_id)["cancel_requested"])

    def load_checkpoint(self):
        task = self._service.store.get(self.task_id)
        return task["checkpoints"].get(self.stage)

    def save_checkpoint(self, data):
        self._service.store.update(self.task_id, lambda t: t["checkpoints"].__setitem__(self.stage, data))

    def report_progress(self, completed_units, total_units=None):
        if completed_units < 0 or (total_units is not None and completed_units > total_units):
            raise ValueError("进度单元无效")
        def mutate(task):
            task["progress"] = {"completed_units": completed_units, "total_units": total_units}
            task["updated_at"] = _now()
        self._service.store.update(self.task_id, mutate)

    def run_effect(self, effect_id, *, kind, action):
        return self._service._effect(self, effect_id, kind, action)


class WorkflowService:
    def __init__(self, store: TaskStore, *, adapters: Mapping[str, StageAdapter],
                 pipelines: Mapping[str, tuple[str, ...]], authorize: Callable):
        self.store, self.adapters, self.pipelines, self.authorize = store, dict(adapters), dict(pipelines), authorize
        self.vault_root = store.vault_root
        self._locks, self._locks_guard = {}, threading.Lock()
        self._wake, self._stop = threading.Event(), threading.Event()
        self._thread = None
        self._startup = threading.Event()
        self._startup_error = None

    def _lock(self, task_id):
        with self._locks_guard:
            return self._locks.setdefault(task_id, threading.RLock())

    @staticmethod
    def _scope(principal, scope):
        admin_override = scope.startswith("tasks:") and "tasks:admin" in principal.scopes
        if scope not in principal.scopes and not admin_override:
            raise WorkflowError("permission_denied", "当前调用者无此权限", http_status=403)

    def _owned(self, task_id, principal, scope="tasks:read:self"):
        self._scope(principal, scope)
        task = self.store.get(task_id)
        if task is None or (task["owner"] != principal.subject and "tasks:admin" not in principal.scopes):
            raise WorkflowError("not_found", "任务不存在或不可见", http_status=404)
        return task

    def submit(self, request: dict, *, principal: Principal):
        self._scope(principal, "tasks:submit")
        if request.get("contract_version") != 1:
            raise WorkflowError("contract_version_unsupported", "仅支持任务协议版本 1")
        request_id = str(request.get("request_id") or "").strip()
        if not request_id or len(request_id) > 200:
            raise WorkflowError("invalid_request", "请提供有效的请求标识")
        source, options = dict(request.get("source") or {}), dict(request.get("options") or {})
        pipeline = options.get("pipeline", "note")
        options["pipeline"] = pipeline
        options.setdefault("publish_to", None)
        if pipeline not in self.pipelines or source.get("kind") not in ("file", "url"):
            raise WorkflowError("not_supported", "来源或处理流程尚未实现")
        self.authorize(principal, "submit", {"source": source, "options": options})
        if options["publish_to"]:
            self._scope(principal, "publish:notion")
        input_path = None
        if source["kind"] == "file":
            input_path = Path(str(source.get("path") or "")).resolve()
            if not input_path.is_file():
                raise WorkflowError("source_missing", "输入文件不存在")
            source = {"kind": "file", "path": str(input_path), "sha256": _hash_file(input_path)}
        elif not isinstance(source.get("url"), str):
            raise WorkflowError("invalid_request", "链接来源缺少 url")
        request_key = hashlib.sha256(json.dumps({"source": source, "options": options},
                                               ensure_ascii=False, sort_keys=True).encode()).hexdigest()
        try:
            prior = self.store.request(principal.subject, request_id, request_key)
            if prior:
                return self._public(prior)
        except RequestConflict as exc:
            raise WorkflowError("request_conflict", str(exc), http_status=409) from exc
        task_id = str(uuid.uuid4())
        task_dir = self.vault_root / "个人学习状态" / "tasks" / task_id
        for name in ("input", "work", "output"):
            (task_dir / name).mkdir(parents=True, exist_ok=True)
        outputs = []
        if input_path is not None:
            frozen = task_dir / "input" / ("source" + input_path.suffix.lower())
            digest = hashlib.sha256()
            with input_path.open("rb") as src, frozen.open("xb") as dest:
                for block in iter(lambda: src.read(1024 * 1024), b""):
                    digest.update(block)
                    dest.write(block)
            if digest.hexdigest() != source["sha256"]:
                raise WorkflowError("source_changed", "冻结期间来源变化，请重新提交")
            source["frozen_path"] = frozen.relative_to(self.vault_root).as_posix()
            outputs = [{"kind": "source_file", "path": source["frozen_path"], "sha256": source["sha256"], "complete": True}]
        stamp = _now()
        task = {"contract_version": 1, "task_id": task_id, "request_id": request_id,
                "owner": principal.subject, "scopes": sorted(principal.scopes), "source": source,
                "options": options, "attempt": 1, "status": "queued", "stage": None,
                "stages": [{"name": s, "status": "pending", "attempts": 0} for s in self.pipelines[pipeline]],
                "progress": {"completed_units": 0, "total_units": None}, "outputs": outputs,
                "error": None, "result": None, "cancel_requested": False, "next_retry_at": None,
                "checkpoints": {}, "effects": {}, "created_at": stamp, "updated_at": stamp}
        try:
            task = self.store.create(task, request_key)
        except RequestConflict as exc:
            raise WorkflowError("request_conflict", str(exc), http_status=409) from exc
        self._wake.set()
        return self._public(task)

    @staticmethod
    def _public(task):
        return {k: v for k, v in task.items() if k not in ("scopes", "checkpoints", "effects", "source")}

    def get(self, task_id, *, principal):
        return self._public(self._owned(task_id, principal))

    def cancel(self, task_id, *, request_id, principal):
        self._control_request(request_id)
        with self._lock(task_id):
            self._owned(task_id, principal, "tasks:control:self")
            def mutate(task):
                if task["status"] == "succeeded":
                    raise WorkflowError("state_conflict", "任务已经完成", http_status=409)
                task["cancel_requested"] = True
                if task["status"] != "running" and task["status"] != "unknown_result":
                    task["status"] = "cancelled"
                task["updated_at"] = _now()
            try:
                task = self.store.control(task_id, principal.subject, request_id, "cancel", mutate)
            except RequestConflict as exc:
                raise WorkflowError("request_conflict", str(exc), http_status=409) from exc
        self._wake.set()
        return self._public(task)

    def retry(self, task_id, *, request_id, principal):
        self._control_request(request_id)
        with self._lock(task_id):
            self._owned(task_id, principal, "tasks:control:self")
            def mutate(task):
                if task["status"] in ("succeeded", "running", "queued", "unknown_result"):
                    raise WorkflowError("state_conflict", "当前任务不能重试；未知远端结果需先核实", http_status=409)
                task.update(status="queued", attempt=task["attempt"] + 1, cancel_requested=False,
                            error=None, next_retry_at=None, updated_at=_now())
                for stage in task["stages"]:
                    if stage["status"] not in ("completed", "skipped"):
                        stage.update(status="pending", attempts=0)
            try:
                task = self.store.control(task_id, principal.subject, request_id, "retry", mutate)
            except RequestConflict as exc:
                raise WorkflowError("request_conflict", str(exc), http_status=409) from exc
        self._wake.set()
        return self._public(task)

    @staticmethod
    def _control_request(request_id):
        if not isinstance(request_id, str) or not request_id.strip() or len(request_id) > 200:
            raise WorkflowError("invalid_request", "请提供有效的控制请求标识")

    def capabilities(self, *, principal):
        self._scope(principal, "tasks:read:self")
        capabilities = []
        for name, stages in self.pipelines.items():
            required = [s for s in stages if s != "sync"]  # Notion 是可选目标，不阻止本地收录。
            missing = [s for s in required if s != "acquire" and s not in self.adapters]
            unavailable = [s for s in required if s in self.adapters and getattr(self.adapters[s], "available", True) is False]
            capabilities.append({"name": name, "implemented": not missing, "configured": not unavailable,
                                 "available": not missing and not unavailable,
                                 "reason": "缺少阶段：" + ",".join(missing + unavailable) if missing or unavailable else "可用"})
        return {"contract_version": 1, "pipeline_mode": "task", "capabilities": capabilities}

    def _verify(self, artifacts):
        for artifact in artifacts:
            path = Path(str(artifact.get("path") or ""))
            if path.is_absolute() or ".." in path.parts:
                raise WorkflowError("permission_denied", "产物路径越界", http_status=403)
            resolved = (self.vault_root / path).resolve()
            if not resolved.is_relative_to(self.vault_root) or not resolved.is_file():
                raise WorkflowError("artifact_missing", "任务产物缺失或越界")
            if artifact.get("sha256") != _hash_file(resolved):
                raise WorkflowError("source_changed", "任务产物被修改，不能复用旧结果")

    def execute(self, task_id):
        """Run one queued task; caller must own the service worker lease."""
        task = self.store.get(task_id)
        if task is None or task["status"] not in ("queued", "retry_wait"):
            return
        if task["next_retry_at"] and time.time() < task["next_retry_at"]:
            return
        try:
            self._verify(task["outputs"])
        except WorkflowError as exc:
            self._block(task_id, exc.code, str(exc))
            return
        principal = Principal(task["owner"], frozenset(task["scopes"]))
        for item in task["stages"]:
            stage = item["name"]
            if item["status"] in ("completed", "skipped"):
                continue
            with self._lock(task_id):
                current = self.store.get(task_id)
                if current["cancel_requested"] or self._stop.is_set():
                    self.store.update(task_id, lambda t: t.update(status="cancelled", updated_at=_now()))
                    return
                def started(t):
                    t.update(status="running", stage=stage, updated_at=_now(), next_retry_at=None)
                    record = next(s for s in t["stages"] if s["name"] == stage)
                    record.update(status="running", attempts=record["attempts"] + 1)
                current = self.store.update(task_id, started)
            context = StageContext(self, current, stage)
            try:
                self.authorize(principal, "execute", current)
                if stage == "sync" and not current["options"].get("publish_to"):
                    result = StageResult("skipped", result={"reason": "未请求 Notion 发布"})
                elif stage == "acquire" and current["source"].get("frozen_path"):
                    result = StageResult("completed")
                elif stage not in self.adapters:
                    result = StageResult("blocked", error={"code": "not_supported", "message": "处理阶段尚未接入", "retryable": False})
                else:
                    result = self.adapters[stage].execute(context)
                if not isinstance(result, StageResult):
                    raise ValueError("阶段没有返回 StageResult")
                self._verify(result.artifacts)
            except WorkflowError as exc:
                outcome = ("unknown_result" if exc.code in ("remote_result_unknown", "commit_result_unknown")
                           else "cancelled" if exc.code == "cancelled" else "blocked")
                result = StageResult(outcome,
                                     error={"code": exc.code, "message": str(exc), "retryable": False})
            except Exception:
                result = StageResult("failed", error={"code": "internal_error", "message": "处理阶段异常，已有成果已保留", "retryable": False})
            with self._lock(task_id):
                def finished(t):
                    record = next(s for s in t["stages"] if s["name"] == stage)
                    record["status"] = result.outcome
                    if result.error:
                        record["error"] = dict(result.error, stage=stage)
                    for artifact in result.artifacts:
                        if artifact not in t["outputs"]:
                            t["outputs"].append(artifact)
                    if result.checkpoint is not None:
                        prior_checkpoint = t["checkpoints"].get(stage)
                        t["checkpoints"][stage] = dict(prior_checkpoint or {}, **result.checkpoint)
                    if result.result:
                        t["result"] = dict(t["result"] or {}, **result.result)
                    t["updated_at"] = _now()
                    if result.outcome not in ("completed", "skipped"):
                        t["status"], t["error"] = result.outcome, record.get("error")
                        if result.outcome == "failed" and (result.error or {}).get("retryable") and record["attempts"] < 3:
                            t.update(status="retry_wait", next_retry_at=time.time() + 60 * 2 ** (record["attempts"] - 1))
                    elif t["cancel_requested"]:
                        t["status"] = "cancelled"
                task = self.store.update(task_id, finished)
            if result.outcome not in ("completed", "skipped") or task["status"] == "cancelled":
                return
        with self._lock(task_id):
            self.store.update(task_id, lambda t: t.update(
                status="cancelled" if t["cancel_requested"] else "succeeded", error=None, updated_at=_now()))

    def _block(self, task_id, code, message):
        return self.store.update(task_id, lambda t: t.update(status="blocked", error={"code": code, "message": message, "retryable": False}, updated_at=_now()))

    def _effect(self, context, effect_id, kind, action):
        if kind not in ("local_commit", "remote_write") or not effect_id:
            raise WorkflowError("invalid_effect", "副作用类型或标识无效")
        lock = self._lock(context.task_id)
        with lock:
            task = self.store.get(context.task_id)
            prior = task["effects"].get(effect_id)
            if prior:
                if prior["status"] == "confirmed":
                    return prior["result"]
                if prior["status"] != "not_completed":
                    raise WorkflowError("remote_result_unknown", "此前提交结果尚未核实", http_status=409)
            if task["cancel_requested"] or self._stop.is_set():
                raise WorkflowError("cancelled", "任务已请求取消")
            self.authorize(Principal(task["owner"], frozenset(task["scopes"])), kind, task)
            self.store.update(context.task_id, lambda t: t["effects"].__setitem__(effect_id, {"kind": kind, "status": "in_flight", "stage": context.stage}))
            if kind == "local_commit":
                return self._finish_effect(context.task_id, effect_id, kind, action)
        return self._finish_effect(context.task_id, effect_id, kind, action)

    def _finish_effect(self, task_id, effect_id, kind, action):
        try:
            result = action()
            if isinstance(result, dict) and result.get("status") in ("outcome_unknown", "unknown_result"):
                raise WorkflowError("remote_result_unknown", "提交结果尚未核实")
        except Exception:
            def unknown(t):
                t["effects"][effect_id]["status"] = "unknown"
                t.update(status="unknown_result", error={"code": "remote_result_unknown" if kind == "remote_write" else "commit_result_unknown", "message": "提交结果需要核实，已停止自动重发", "retryable": False})
            self.store.update(task_id, unknown)
            raise WorkflowError("remote_result_unknown" if kind == "remote_write" else "commit_result_unknown",
                                "提交结果未知，需核实后继续", http_status=409) from None
        not_completed = (kind == "remote_write" and isinstance(result, dict)
                         and result.get("status") in ("failed", "not_configured", "pending_authorization",
                                                     "processing", "new_version_detected"))
        self.store.update(task_id, lambda t: t["effects"][effect_id].update(
            status="not_completed" if not_completed else "confirmed", result=result))
        return result

    def recover(self):
        recovered = []
        for task in self.store.all():
            if task["status"] != "running":
                continue
            unresolved = any(e["status"] in ("in_flight", "unknown") for e in task["effects"].values())
            status = "unknown_result" if unresolved else ("cancelled" if task["cancel_requested"] else "queued")
            def mutate(t):
                t.update(status=status, updated_at=_now())
                for stage in t["stages"]:
                    if stage["status"] == "running":
                        stage["status"] = "pending" if status == "queued" else status
            self.store.update(task["task_id"], mutate)
            recovered.append({"task_id": task["task_id"], "status": status})
        return {"recovered": recovered}

    def start(self):
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._startup.clear()
        self._startup_error = None
        self._thread = threading.Thread(target=self._loop, daemon=True, name="knowledge-tasks")
        self._thread.start()
        self._startup.wait(5)
        if not self._startup.is_set() or self._startup_error or not self._thread.is_alive():
            raise WorkflowError(self._startup_error or "worker_start_failed", "任务执行者启动失败或已有实例", http_status=409)

    def status(self):
        return {"worker_running": bool(self._thread and self._thread.is_alive()),
                "worker_error": self._startup_error, "stopping": self._stop.is_set()}

    def _loop(self):
        mutex = FileMutex(self.store.state_dir / "worker.lock", timeout=0)
        try:
            mutex.acquire()
        except Exception:
            self._startup_error = "worker_busy"
            self._startup.set()
            return
        try:
            self.recover()
            self._startup.set()
            while not self._stop.is_set():
                for task in self.store.all():
                    if self._stop.is_set():
                        break
                    if task["status"] in ("queued", "retry_wait"):
                        self.execute(task["task_id"])
                self._wake.wait(1)
                self._wake.clear()
        except Exception:
            self._startup_error = "worker_failed"
        finally:
            mutex.release()
            self._startup.set()

    def stop(self, *, timeout=5.0):
        deadline = time.monotonic() + max(0, timeout)
        self._stop.set()
        self._wake.set()
        for task in self.store.all():
            if task["status"] == "running":
                lock = self._lock(task["task_id"])
                if not lock.acquire(timeout=max(0, deadline - time.monotonic())):
                    continue
                try:
                    self.store.update(task["task_id"], lambda t: t.update(cancel_requested=True))
                finally:
                    lock.release()
        if self._thread:
            self._thread.join(max(0, deadline - time.monotonic()))
        return {"stopped": not (self._thread and self._thread.is_alive()),
                "remaining": [t["task_id"] for t in self.store.all() if t["status"] in ("running", "unknown_result")]}
