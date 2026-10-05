"""合成时钟、进程与 HTTP 响应；不启动真实服务或注册系统自启。"""

import io
import json
import os
import subprocess
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

import continuous_start
from pka_graph.file_lock import FileMutex
from pka_graph.runtime import (PeriodicRunner, RuntimeSupervisor, candidate_pool_status,
                               inbox_health, read_runtime_status, request_runtime_stop)


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


class Process:
    pid = 34567

    def __init__(self, exit_code=None):
        self.exit_code = exit_code
        self.terminated = 0
        self.killed = 0

    def poll(self):
        return self.exit_code

    def terminate(self):
        self.terminated += 1
        self.exit_code = 0

    def wait(self, timeout):
        return self.exit_code

    def kill(self):
        self.killed += 1
        self.exit_code = -1


class RuntimeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.vault = Path(self.temp.name)
        self.clock = Clock()

    def supervisor(self, process=None, **kwargs):
        return RuntimeSupervisor(self.vault, ["fake-service", "--vault", str(self.vault)], port=8765,
                                 process_factory=lambda *a, **kw: process or Process(),
                                 health_probe=lambda: {"healthy": True}, clock=self.clock, **kwargs)

    def test_periodic_resume_does_not_replay_missed_intervals(self):
        calls = []
        runner = PeriodicRunner(lambda: calls.append(self.clock()) or {"processed": 2}, 10, clock=self.clock)
        self.assertTrue(runner.run_due())
        self.clock.now += 10000
        self.assertTrue(runner.run_due())
        self.assertFalse(runner.run_due())
        self.assertEqual(len(calls), 2)
        self.assertEqual(runner.status()["next_run_at"], self.clock() + 10)

    def test_periodic_errors_preserve_success_and_do_not_echo_body(self):
        def failure():
            raise RuntimeError("private answer and API key")
        state = self.vault / "periodic.json"
        runner = PeriodicRunner(failure, 10, state_path=state, clock=self.clock)
        runner.run_due()
        self.assertIsNone(runner.status()["last_success_at"])
        self.assertEqual(runner.status()["error"]["type"], "RuntimeError")
        self.assertNotIn("private answer", state.read_text(encoding="utf-8"))

    def test_periodic_skipped_is_not_success(self):
        runner = PeriodicRunner(lambda: {"skipped": True}, 10, clock=self.clock)
        runner.run_due()
        self.assertIsNone(runner.status()["last_success_at"])

    def test_periodic_calls_do_not_overlap_and_stop_is_truthful(self):
        entered, release = threading.Event(), threading.Event()
        def callback():
            entered.set()
            release.wait(2)
        runner = PeriodicRunner(callback, 10)
        runner.start()
        self.assertTrue(entered.wait(1))
        self.assertFalse(runner.run_due())
        self.assertFalse(runner.stop(timeout=0.01)["stopped"])
        release.set()
        self.assertTrue(runner.stop(timeout=1)["stopped"])
        self.assertFalse(runner.run_due())

    def test_restart_budget_and_backoff(self):
        launched = []
        runtime = self.supervisor(max_restarts=2)
        runtime.process_factory = lambda *a, **kw: launched.append(Process(1)) or launched[-1]
        self.assertTrue(runtime.tick())
        self.assertTrue(runtime.tick())
        self.assertEqual(len(launched), 1)
        self.clock.now += 1
        self.assertTrue(runtime.tick())
        self.clock.now += 2
        self.assertFalse(runtime.tick())
        self.assertEqual(len(launched), 3)
        self.assertEqual(runtime.status()["status"], "blocked")

    def test_sustained_health_resets_crash_budget(self):
        runtime = self.supervisor(healthy_reset_seconds=20)
        runtime._failures = 3
        runtime.tick()
        self.clock.now += 19
        runtime.tick()
        self.assertEqual(runtime.status()["restart_count"], 3)
        self.clock.now += 1
        runtime.tick()
        self.assertEqual(runtime.status()["restart_count"], 0)

    def test_degraded_health_does_not_kill_busy_worker(self):
        worker = Process()
        runtime = self.supervisor(worker)
        runtime.health_probe = lambda: {"healthy": False, "code": "scan_stale"}
        runtime.tick()
        self.assertEqual(runtime.status()["status"], "degraded")
        self.assertEqual(worker.terminated, 0)

    def test_stop_identity_and_owned_process_cleanup(self):
        worker = Process()
        runtime = self.supervisor(worker)
        runtime.tick()
        result = request_runtime_stop(self.vault)
        self.assertEqual(result["run_id"], runtime.run_id)
        self.assertFalse(runtime.tick())
        runtime._close_worker()
        self.assertEqual(worker.terminated, 1)
        other = self.supervisor()
        self.assertFalse(other._stop_requested())

    def test_injected_graceful_stop_does_not_terminate_finished_worker(self):
        worker = Process()
        stopped = []
        def graceful(child):
            stopped.append(child.pid)
            child.exit_code = 0
        runtime = self.supervisor(worker, stop_worker=graceful)
        runtime.tick()
        self.assertTrue(runtime._close_worker())
        self.assertEqual(stopped, [worker.pid])
        self.assertEqual(worker.terminated, 0)

    def test_stop_timeout_is_visible_not_reported_as_stopped(self):
        class StuckProcess(Process):
            def terminate(self):
                self.terminated += 1
            def kill(self):
                self.killed += 1
            def wait(self, timeout):
                raise subprocess.TimeoutExpired("fake-service", timeout)
        worker = StuckProcess()
        runtime = self.supervisor(worker)
        runtime.tick()
        self.assertFalse(runtime._close_worker())
        self.assertEqual(runtime.status()["status"], "stop_incomplete")
        self.assertEqual(worker.terminated, 1)
        self.assertEqual(worker.killed, 1)

    def test_supervisor_exclusivity_and_read_status_no_writes(self):
        empty = read_runtime_status(self.vault)
        self.assertEqual(empty["status"], "not_started")
        self.assertEqual(list(self.vault.iterdir()), [])
        runtime = self.supervisor()
        with FileMutex(runtime.runtime_dir / "supervisor.lock", timeout=0):
            with self.assertRaises(TimeoutError):
                runtime.run()
        self.assertIsNone(runtime._process)

    def test_http_success_requires_recent_scanning_and_right_vault(self):
        from datetime import datetime, timezone
        payloads = [{"test_vault": str(self.vault)},
                    {"running": True, "last_scan_at": datetime.fromtimestamp(1000, timezone.utc).isoformat(),
                     "interval_seconds": 10, "processed_total": 2}]
        def opener(*a, **kw):
            return io.BytesIO(json.dumps(payloads.pop(0)).encode("utf-8"))
        self.assertTrue(inbox_health(8765, expected_vault=self.vault, opener=opener, clock=self.clock)["healthy"])
        self.clock.now += 1000
        payloads.extend([{"test_vault": str(self.vault)}, {"running": True, "last_scan_at": "1970-01-01T00:16:40+00:00"}])
        self.assertFalse(inbox_health(8765, expected_vault=self.vault, opener=opener, clock=self.clock)["healthy"])
        payloads.append({"test_vault": str(self.vault / "wrong")})
        self.assertEqual(inbox_health(8765, expected_vault=self.vault, opener=opener, clock=self.clock)["code"], "vault_mismatch")

    def test_candidates_are_read_only_and_freshness_is_not_novelty(self):
        path = self.vault / "candidates.js"
        path.write_text("const CANDIDATES = [];", encoding="utf-8")
        os.utime(path, (self.clock.now, self.clock.now))
        result = candidate_pool_status([path, path.with_name("missing.js")], clock=self.clock)
        self.assertEqual(result["sources"][0]["status"], "fresh")
        self.assertEqual(result["sources"][1]["status"], "missing")
        self.clock.now += 90000
        self.assertEqual(candidate_pool_status([path], clock=self.clock)["sources"][0]["status"], "stale")
        self.assertEqual(path.read_text(encoding="utf-8"), "const CANDIDATES = [];")

    def test_start_arguments_preserve_vault_port_and_explicit_authorization(self):
        command = continuous_start.service_command(self.vault, 9988, True)
        self.assertEqual(command[-5:], ["--vault", str(self.vault.resolve()), "--port", "9988", "--allow-real-vault"])
        autostart = continuous_start.autostart_command(self.vault, 9988, True)
        self.assertIn(str(self.vault.resolve()), autostart)
        self.assertIn("--port 9988", autostart)
        self.assertIn("--allow-real-vault", autostart)
        self.assertNotIn("test_vault", autostart)

    def test_frozen_app_uses_explicit_dispatch_modes(self):
        with patch.object(continuous_start.sys, "frozen", True, create=True):
            self.assertEqual(continuous_start.service_command(self.vault, 9988, False)[1], "--server-worker")
            self.assertIn("--continuous", continuous_start.autostart_command(self.vault, 9988, False))

    def test_cli_status_does_not_spawn_or_install(self):
        with patch.object(continuous_start, "RuntimeSupervisor") as supervisor, \
                patch.object(continuous_start, "install_autostart") as install, \
                patch("sys.stdout", new=io.StringIO()):
            self.assertEqual(continuous_start.main(["--vault", str(self.vault), "--status"]), 0)
            supervisor.assert_not_called()
            install.assert_not_called()
        self.assertEqual(list(self.vault.iterdir()), [])


if __name__ == "__main__":
    unittest.main()
