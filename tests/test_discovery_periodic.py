"""周期检查不重复拿非重入锁，不联网、不等待十分钟。"""
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from pka_graph.discovery import DiscoveryService
from pka_graph.service import GraphService
from pka_graph.store import StateStore


class OneCycle:
    def __init__(self):
        self.calls = 0

    def clear(self):
        self.calls = 0

    def set(self):
        self.calls = 2

    def wait(self, timeout):
        self.calls += 1
        return self.calls > 1


class DiscoveryPeriodicTests(unittest.TestCase):
    def test_configured_timer_reaches_real_check_once_and_can_restart(self):
        with tempfile.TemporaryDirectory() as directory:
            service = GraphService(StateStore(Path(directory)))
            state = service.state()
            state["discovery"] = {"auto_check_minutes": 10}
            service.store.save(state, base_version=state["version"])
            discovery = DiscoveryService(service, upstream=[])
            discovery._periodic_stop = OneCycle()
            calls = []
            def check_serial(request_id):
                calls.append(discovery.check_lock.locked())
                return {"checked": 0}
            with patch.object(discovery, "_check_serial", side_effect=check_serial):
                self.assertEqual(discovery.start_periodic_if_configured()["periodic"], "started")
                discovery._periodic_thread.join(2)
                self.assertEqual(calls, [True])
                self.assertTrue(discovery.stop_periodic()["stopped"])
                self.assertEqual(discovery.start_periodic_if_configured()["periodic"], "started")
                discovery._periodic_thread.join(2)
                self.assertEqual(calls, [True, True])


if __name__ == "__main__":
    unittest.main()
