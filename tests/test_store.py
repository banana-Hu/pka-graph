"""安全存储测试（必测 13/14/16 及备份恢复）。"""

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tests"))

from fixtures import make_test_vault
from pka_graph.models import empty_state
from pka_graph.store import KEEP_BACKUPS, PathOutsideVault, StateStore, VersionConflict

# 真实库路径由环境变量 PKA_REAL_VAULT 提供；未设置时相关测试自动跳过
import os as _os
REAL_VAULT = (Path(_os.environ["PKA_REAL_VAULT"].strip())
              if _os.environ.get("PKA_REAL_VAULT", "").strip() else None)


class StoreTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = make_test_vault(Path(self._tmp.name))
        self.store = StateStore(self.root)

    def tearDown(self):
        self._tmp.cleanup()

    def test_lazy_init_creates_state(self):
        state = self.store.load()
        self.assertEqual(state["schema"], "pka-state/1")
        self.assertTrue(self.store.state_path.exists())

    def test_13_concurrent_edit_version_conflict(self):
        """必测 13：两次并发编辑不会静默覆盖——落后版本被拒绝。"""
        from pka_graph.models import new_node

        s1 = self.store.load()
        s2 = self.store.load()
        s1["nodes"].append(new_node(name="编辑A", domain="D", scope="S"))
        s2["nodes"].append(new_node(name="编辑B", domain="D", scope="S"))
        stale = s1["version"]
        self.store.save(s1, base_version=None)  # A 正常写入
        with self.assertRaises(VersionConflict):
            self.store.save(s2, base_version=stale)  # B 基于旧版本 → 拒绝
        # A 的修改仍然在，B 没有覆盖
        reloaded = self.store.load()
        self.assertTrue(any(n["name"] == "编辑A" for n in reloaded["nodes"]))
        self.assertFalse(any(n["name"] == "编辑B" for n in reloaded["nodes"]))
        self.assertEqual(reloaded["version"], stale + 1)

    def test_14_write_interrupt_leaves_old_state_recoverable(self):
        """必测 14：写入中断（os.replace 抛错）后旧状态完好、可恢复。"""
        state = self.store.load()
        before = self.store.state_path.read_text(encoding="utf-8")
        with patch("pka_graph.store.os.replace", side_effect=OSError("模拟写入中断")):
            with self.assertRaises(OSError):
                self.store.save(state, base_version=None)
        # 原文件完好
        self.assertEqual(self.store.state_path.read_text(encoding="utf-8"), before)
        # 残留临时文件可被 recover 清理
        self.assertTrue(self.store.pending_tmp_files())
        report = self.store.recover()
        self.assertEqual(len(report["removed_tmp"]), 1)
        self.assertIsNone(report["repaired_from"])
        self.store.load()  # 状态可正常解析

    def test_14b_corrupt_state_repaired_from_backup_keeps_original(self):
        """必测 14b：损坏状态从备份恢复——不覆盖损坏原件，明确报告来源与限制。"""
        state = self.store.load()
        self.store.save(state, base_version=None)  # 产生 v1 备份
        self.store.state_path.write_text("{broken json", encoding="utf-8")
        report = self.store.recover()
        self.assertIsNotNone(report["repaired_from"])
        # 损坏原件被保留（改名），没有被备份覆盖消失
        self.assertIsNotNone(report["corrupt_original"])
        corrupt = self.store.state_path.parent / report["corrupt_original"]
        self.assertTrue(corrupt.exists())
        self.assertIn("broken json", corrupt.read_text(encoding="utf-8"))
        self.assertIn("备份时间", report["warning"])
        self.assertIn("不能视为最新数据已全部恢复", report["warning"])
        self.store.load()  # 修复后可解析

    def test_backup_keeps_recent_only(self):
        for i in range(KEEP_BACKUPS + 3):
            state = self.store.load()
            state["version"] = i + 1
            self.store.save(state, base_version=None)
        self.assertLessEqual(len(self.store.list_backups()), KEEP_BACKUPS)

    def test_restore_latest_backup(self):
        state = self.store.load()
        self.store.save(state, base_version=None)
        self.store.restore_latest_backup()
        self.store.load()

    def test_16_path_outside_vault_rejected(self):
        """必测 16：越界路径被拒绝。"""
        with self.assertRaises(PathOutsideVault):
            self.store.safe_write_path("../outside.txt")
        with self.assertRaises(PathOutsideVault):
            self.store.safe_write_path(r"C:\Windows\system32\evil.json")

    def test_16b_real_vault_write_blocked(self):
        """真实知识库前缀显式拉黑：把状态文件指到真实库直接拒绝。"""
        import tempfile
        from unittest.mock import patch

        with tempfile.TemporaryDirectory() as fake_real:
            fake_real = Path(fake_real).resolve()
            (fake_real / "个人学习状态").mkdir()
            with patch("pka_graph.store.REAL_VAULT_PREFIX", fake_real):
                with self.assertRaises(PathOutsideVault):
                    StateStore(fake_real)          # 根目录即真实库
                with self.assertRaises(PathOutsideVault):
                    StateStore(fake_real, "x")     # vault_root 即真实库
                store = StateStore(Path(tempfile.mkdtemp()))
                with self.assertRaises(PathOutsideVault):
                    store.safe_write_path(str(fake_real / "evil.md"))

    def test_state_validation_on_load(self):
        """状态文件内容非法（如引用悬空）时 load 报错，不静默接受。"""
        bad = empty_state()
        bad["schema"] = "pka-state/999"
        self.store.state_path.write_text(json.dumps(bad), encoding="utf-8")
        from pka_graph.models import ValidationError

        with self.assertRaises(ValidationError):
            self.store.load()


if __name__ == "__main__":
    unittest.main()
