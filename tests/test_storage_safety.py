"""Offline transaction and OS-lock regression tests using temporary directories."""

import copy
import multiprocessing
import tempfile
import threading
import unittest
from pathlib import Path

from pka_graph.file_lock import FileMutex
from pka_graph.store import StateStore, VersionConflict


def _hold_mutex(path, acquired, release):
    with FileMutex(Path(path)):
        acquired.set()
        release.wait(15)


class StorageSafetyTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def test_two_writers_cannot_both_commit_same_base_version(self):
        first = StateStore(self.root)
        state = first.load()
        barrier = threading.Barrier(2)
        results = []

        def save():
            store = StateStore(self.root)
            candidate = copy.deepcopy(state)
            barrier.wait()
            try:
                results.append(store.save(candidate, base_version=state["version"])["version"])
            except VersionConflict:
                results.append("conflict")

        workers = [threading.Thread(target=save) for _ in range(2)]
        for worker in workers:
            worker.start()
        for worker in workers:
            worker.join(5)
            self.assertFalse(worker.is_alive())
        self.assertCountEqual(results, [state["version"] + 1, "conflict"])
        self.assertEqual(first.load()["version"], state["version"] + 1)
        self.assertEqual(first.pending_tmp_files(), [])

    def test_backups_are_sorted_by_numeric_version(self):
        store = StateStore(self.root)
        state = store.load()
        initial_version = state["version"]
        for _ in range(105):
            store.save(state, base_version=state["version"])
        backups = store.list_backups()
        self.assertEqual(len(backups), 20)
        self.assertEqual(backups[0], f"state-v{initial_version + 85}.json")
        self.assertEqual(backups[-1], f"state-v{initial_version + 104}.json")
        self.assertEqual(store.restore_latest_backup(), f"state-v{initial_version + 104}.json")
        self.assertEqual(store.load()["version"], initial_version + 104)

    def test_initialization_is_locked_and_does_not_reset_existing_state(self):
        StateStore(self.root).load()
        writer = StateStore(self.root)
        state = writer.load()
        initial_version = state["version"]
        writer.save(state, base_version=state["version"])
        reader = StateStore(self.root)
        self.assertEqual(reader.load()["version"], initial_version + 1)

    def test_process_lock_releases_without_deleting_lock_file(self):
        context = multiprocessing.get_context("spawn")
        acquired, release = context.Event(), context.Event()
        path = self.root / "shared.lock"
        child = context.Process(target=_hold_mutex, args=(str(path), acquired, release))
        child.start()
        try:
            self.assertTrue(acquired.wait(10))
            with self.assertRaises(TimeoutError):
                FileMutex(path, timeout=0.05).acquire()
            release.set()
            child.join(10)
            self.assertEqual(child.exitcode, 0)
            with FileMutex(path, timeout=1):
                self.assertTrue(path.exists())
            self.assertTrue(path.exists())
        finally:
            release.set()
            child.join(2)
            if child.is_alive():
                child.terminate()
                child.join(5)

    def test_foreign_thread_cannot_release_owned_mutex(self):
        mutex = FileMutex(self.root / "owned.lock")
        mutex.acquire()
        errors = []
        def release():
            try:
                mutex.release()
            except RuntimeError:
                errors.append("rejected")
        worker = threading.Thread(target=release)
        worker.start()
        worker.join(2)
        self.assertEqual(errors, ["rejected"])
        with self.assertRaises(TimeoutError):
            FileMutex(mutex.path, timeout=0).acquire()
        mutex.release()
        with FileMutex(mutex.path, timeout=0):
            pass
