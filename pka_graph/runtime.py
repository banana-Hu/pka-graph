"""本地常驻监督和服务内周期调用；导入本模块不会启动线程或进程。"""

from __future__ import annotations

import json
import os
import subprocess
import threading
import time
import urllib.request
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from .file_lock import FileMutex


def _utc(timestamp: float) -> str:
    return datetime.fromtimestamp(timestamp, timezone.utc).isoformat()


def _write_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp-" + uuid.uuid4().hex)
    try:
        temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def read_runtime_status(vault: Path) -> dict:
    """只读状态，不创建目录，也不以陈旧记录推断进程仍在运行。"""
    path = Path(vault).resolve() / "个人学习状态" / "runtime" / "supervisor.json"
    if not path.exists():
        return {"status": "not_started", "vault": str(Path(vault).resolve())}
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(value, dict):
            raise ValueError("invalid runtime status")
        age = max(0.0, time.time() - float(value.get("heartbeat_at", 0)))
        value["heartbeat_age_seconds"] = round(age, 1)
        value["heartbeat_stale"] = age > 30
        return value
    except (OSError, ValueError, TypeError):
        return {"status": "unreadable", "reason": "运行状态文件无法读取，原文件已保留"}


def request_runtime_stop(vault: Path) -> dict:
    """向当前 run_id 发停止请求，不能误停后来启动的新监督器。"""
    state = read_runtime_status(vault)
    if not state.get("run_id") or state.get("status") in {"stopped", "blocked"}:
        return {"requested": False, "reason": "没有可确认的当前监督器"}
    path = Path(vault).resolve() / "个人学习状态" / "runtime" / "stop.request"
    _write_json(path, {"run_id": state["run_id"], "requested_at": _utc(time.time())})
    return {"requested": True, "run_id": state["run_id"]}


class PeriodicRunner:
    """周期回调串行运行。睡眠恢复只执行一次，下一次从完成时间重新排期。"""

    def __init__(self, callback: Callable[[], dict | None], interval_seconds: float, *,
                 state_path: Path | None = None, clock: Callable[[], float] = time.time):
        if interval_seconds <= 0:
            raise ValueError("interval_seconds must be positive")
        self.callback = callback
        self.interval = float(interval_seconds)
        self.state_path = Path(state_path) if state_path else None
        self.clock = clock
        self._stop = threading.Event()
        self._callback_lock = threading.Lock()
        self._state_lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._state = {"running": False, "next_run_at": clock(), "last_attempt_at": None,
                       "last_success_at": None, "error": None, "result": None}

    def status(self) -> dict:
        with self._state_lock:
            return dict(self._state)

    def _update(self, **changes) -> None:
        with self._state_lock:
            self._state.update(changes)
            if self.state_path:
                _write_json(self.state_path, self._state)

    def run_due(self) -> bool:
        if self._stop.is_set() or self.clock() < self.status()["next_run_at"]:
            return False
        if not self._callback_lock.acquire(blocking=False):
            return False
        try:
            # 第二次检查避免另一调用者刚完成回调后抢到锁再重复执行。
            if self._stop.is_set() or self.clock() < self.status()["next_run_at"]:
                return False
            self._update(last_attempt_at=self.clock())
            try:
                result = self.callback() or {}
                safe_result = {key: value for key, value in result.items()
                               if key in {"processed", "checked", "created", "skipped", "pending"}
                               and isinstance(value, (bool, int, float))}
                if result.get("skipped"):
                    self._update(error={"code": "callback_skipped"}, result=safe_result)
                else:
                    self._update(last_success_at=self.clock(), error=None, result=safe_result)
            except Exception as exc:
                self._update(error={"code": "callback_failed", "type": type(exc).__name__})
            finally:
                self._update(next_run_at=self.clock() + self.interval)
            return True
        finally:
            self._callback_lock.release()

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._update(running=True)
        self._thread = threading.Thread(target=self._loop, name="pka-periodic", daemon=True)
        self._thread.start()

    def _loop(self) -> None:
        try:
            while not self._stop.is_set():
                self.run_due()
                delay = max(0.05, min(30.0, self.status()["next_run_at"] - self.clock()))
                self._stop.wait(delay)
        finally:
            self._update(running=False)

    def stop(self, timeout: float = 5.0) -> dict:
        self._stop.set()
        if self._thread and self._thread is not threading.current_thread():
            self._thread.join(timeout)
        alive = bool((self._thread and self._thread.is_alive()) or self._callback_lock.locked())
        if not alive:
            self._update(running=False)
        return {"stopped": not alive, "callback_in_flight": alive}


def inbox_health(port: int, *, expected_vault: Path | None = None,
                 opener=urllib.request.urlopen, clock=time.time) -> dict:
    """验证身份和实际扫描心跳；HTTP 200 本身不等于工作健康。"""
    try:
        with opener(f"http://127.0.0.1:{port}/api/meta", timeout=3) as response:
            meta = json.loads(response.read())
        actual_vault = meta.get("vault") or meta.get("vault_path") or meta.get("test_vault")
        if expected_vault is not None and (not actual_vault or
                Path(actual_vault).resolve() != Path(expected_vault).resolve()):
            return {"healthy": False, "code": "vault_mismatch"}
        if meta.get("pipeline_mode") == "task":
            tasks = meta.get("tasks") or {}
            if not tasks.get("available") or not tasks.get("worker_running"):
                return {"healthy": False, "code": "task_worker_unavailable"}
        with opener(f"http://127.0.0.1:{port}/api/inbox/status", timeout=3) as response:
            state = json.loads(response.read())
        last_scan = state.get("last_scan_at")
        scanned_at = datetime.fromisoformat(str(last_scan).replace("Z", "+00:00")).timestamp()
        tolerance = max(180.0, float(state.get("interval_seconds", 10)) * 3)
        healthy = bool(state.get("running") and not state.get("loop_error") and
                       0 <= clock() - scanned_at <= tolerance)
        return {"healthy": healthy, "code": "ok" if healthy else "scan_stale",
                "last_scan_at": last_scan, "processed_total": state.get("processed_total", 0)}
    except Exception as exc:
        return {"healthy": False, "code": "health_unavailable", "type": type(exc).__name__}


def candidate_pool_status(paths: list[Path], *, max_age_seconds: float = 86400,
                          clock=time.time) -> dict:
    """只查看已配置候选池的文件新鲜度；不启动上游采集或增加来源授权。"""
    sources = []
    for source in paths:
        path = Path(source)
        try:
            age = max(0.0, clock() - path.stat().st_mtime)
            sources.append({"path": str(path), "status": "fresh" if age <= max_age_seconds else "stale",
                            "age_seconds": round(age, 1)})
        except OSError:
            sources.append({"path": str(path), "status": "missing", "age_seconds": None})
    return {"sources": sources, "freshness_basis": "file_mtime",
            "note": "仅表示候选文件最近写入时间，不证明条目或知识是新出现的"}


class RuntimeSupervisor:
    """监督自己创建的服务进程；每个 vault 只有一份监督器。"""

    def __init__(self, vault: Path, command: list[str] | tuple[str, ...], *, port: int,
                 process_factory=subprocess.Popen, health_probe: Callable[[], dict] | None = None,
                 stop_worker: Callable[[object], None] | None = None,
                 max_restarts: int = 5, healthy_reset_seconds: float = 300,
                 clock: Callable[[], float] = time.time):
        self.vault = Path(vault).resolve()
        if not self.vault.is_dir():
            raise ValueError("vault must be an existing directory")
        if not command or max_restarts < 0 or not 1 <= int(port) <= 65535:
            raise ValueError("invalid runtime configuration")
        self.command = tuple(str(item) for item in command)
        self.port = int(port)
        self.process_factory = process_factory
        self.health_probe = health_probe or (lambda: inbox_health(port, expected_vault=self.vault))
        self.stop_worker = stop_worker
        self.max_restarts = max_restarts
        self.healthy_reset_seconds = healthy_reset_seconds
        self.clock = clock
        self.runtime_dir = self.vault / "个人学习状态" / "runtime"
        self.state_path = self.runtime_dir / "supervisor.json"
        self.stop_path = self.runtime_dir / "stop.request"
        self.run_id = uuid.uuid4().hex
        self._stop = threading.Event()
        self._process = None
        self._failures = 0
        self._next_start = 0.0
        self._healthy_since: float | None = None
        self._state = {"status": "starting", "run_id": self.run_id, "vault": str(self.vault),
                       "port": self.port, "supervisor_pid": os.getpid(), "worker_pid": None,
                       "restart_count": 0, "health": None, "error": None}

    def status(self) -> dict:
        return dict(self._state)

    def _save(self, **changes) -> None:
        self._state.update(changes)
        self._state["heartbeat_at"] = self.clock()
        _write_json(self.state_path, self._state)

    def request_stop(self) -> None:
        self._stop.set()

    def _stop_requested(self) -> bool:
        if self._stop.is_set():
            return True
        if self.stop_path.exists():
            try:
                return json.loads(self.stop_path.read_text(encoding="utf-8")).get("run_id") == self.run_id
            except (OSError, ValueError, AttributeError):
                return False
        return False

    def _exited(self, exit_code: int | None, error_type: str | None = None) -> None:
        self._process = None
        self._healthy_since = None
        self._failures += 1
        self._next_start = self.clock() + min(60.0, 2.0 ** min(self._failures - 1, 6))
        exhausted = self._failures > self.max_restarts
        self._save(status="blocked" if exhausted else "restart_wait", worker_pid=None,
                   restart_count=self._failures, next_start_at=None if exhausted else self._next_start,
                   error={"code": "restart_exhausted" if exhausted else "worker_exited",
                          "exit_code": exit_code, "type": error_type})

    def tick(self) -> bool:
        """执行一次监督步骤，供离线验证；返回 False 表示应该结束。"""
        if self._stop_requested() or self._state["status"] == "blocked":
            return False
        if self._process is None:
            if self.clock() < self._next_start:
                self._save()
                return True
            flags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0
            try:
                self._process = self.process_factory(self.command, cwd=str(Path(__file__).resolve().parents[1]),
                                                     stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                                     stderr=subprocess.DEVNULL, creationflags=flags,
                                                     env=dict(os.environ, PKA_RUNTIME_RUN_ID=self.run_id,
                                                              PKA_TASK_PIPELINE="task"))
                self._save(status="starting", worker_pid=self._process.pid, error=None)
            except OSError as exc:
                self._exited(None, type(exc).__name__)
                return self._state["status"] != "blocked"
        exit_code = self._process.poll()
        if exit_code is not None:
            self._exited(exit_code)
            return self._state["status"] != "blocked"
        try:
            health = self.health_probe()
        except Exception as exc:
            health = {"healthy": False, "code": "health_unavailable", "type": type(exc).__name__}
        if health.get("healthy"):
            if self._healthy_since is None:
                self._healthy_since = self.clock()
            if self.clock() - self._healthy_since >= self.healthy_reset_seconds:
                self._failures = 0
        else:
            self._healthy_since = None
        self._save(status="running" if health.get("healthy") else "degraded",
                   health=health, restart_count=self._failures)
        return True

    def _close_worker(self) -> bool:
        worker = self._process
        if worker is None:
            return True
        if worker.poll() is None and self.stop_worker is not None:
            try:
                # 主程序可注入已认证的优雅关闭操作；默认不请求匿名 HTTP shutdown。
                self.stop_worker(worker)
                worker.wait(timeout=5)
            except Exception as exc:
                self._save(shutdown_error={"type": type(exc).__name__})
        if worker.poll() is None:
            try:
                worker.terminate()
                worker.wait(timeout=5)
            except subprocess.TimeoutExpired:
                try:
                    worker.kill()
                    worker.wait(timeout=5)
                except (OSError, subprocess.TimeoutExpired) as exc:
                    self._save(status="stop_incomplete", shutdown_error={"type": type(exc).__name__})
                    return False
            except OSError as exc:
                self._save(status="stop_incomplete", shutdown_error={"type": type(exc).__name__})
                return False
        self._process = None
        return True

    def run(self) -> dict:
        self.runtime_dir.mkdir(parents=True, exist_ok=True)
        # 锁由内核释放；不使用 PID 信号探测，也不删除别人的锁文件。
        with FileMutex(self.runtime_dir / "supervisor.lock", timeout=0):
            self._save(status="starting")
            try:
                while self.tick():
                    self._stop.wait(1)
            except KeyboardInterrupt:
                self.request_stop()
            finally:
                stopped = self._close_worker()
                if stopped and self._state["status"] != "blocked":
                    self._save(status="stopped", worker_pid=None)
                if stopped and self.stop_path.exists():
                    try:
                        if json.loads(self.stop_path.read_text(encoding="utf-8")).get("run_id") == self.run_id:
                            self.stop_path.unlink()
                    except (OSError, ValueError, AttributeError):
                        pass
            return self.status()
