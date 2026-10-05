"""必测 18：真实知识库零写入验证。

方法：扫描真实知识库前后，对"所有被本程序读取的文件"计算逐文件 SHA-256 并全量对比；
任何差异都意味着发生了写入（本测试环境中没有其他进程会改这些文件；
若真实库本身正在被 Obsidian 编辑导致差异，测试会失败并如实报告，不掩盖）。
同时验证状态存储对真实库路径的写入拒绝（test_store 已覆盖，这里做端到端确认）。
"""

import hashlib
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tests"))

# 真实库路径由环境变量 PKA_REAL_VAULT 提供；未设置时相关测试自动跳过
import os as _os
REAL_VAULT = (Path(_os.environ["PKA_REAL_VAULT"].strip())
              if _os.environ.get("PKA_REAL_VAULT", "").strip() else None)


def snapshot(paths) -> dict:
    return {
        str(p): hashlib.sha256(p.read_bytes()).hexdigest()
        for p in sorted(paths)
    }


@unittest.skipUnless(REAL_VAULT and REAL_VAULT.exists(), "真实知识库不在本机")
class RealVaultUntouchedTests(unittest.TestCase):
    def test_18_scan_real_vault_leaves_no_trace(self):
        from pka_graph import scanner
        from pka_graph.service import GraphService
        from pka_graph.store import StateStore

        service = GraphService(StateStore(tempfile.mkdtemp()), real_vault=REAL_VAULT)

        # 读取集：扫描会读取的目标 md（含不可读的尝试）+ 前后对全库 md 做快照
        all_md_before = snapshot(REAL_VAULT.rglob("*.md"))
        report1 = service.scan("real")
        report2 = service.scan("real")
        all_md_after = snapshot(REAL_VAULT.rglob("*.md"))

        self.assertEqual(all_md_before, all_md_after, "真实知识库的 md 文件在扫描前后发生了变化！")
        # 两次扫描结果一致（必测 17 的真实库版）
        self.assertEqual(report1["target_notes"], report2["target_notes"])
        self.assertGreater(report1["stats"]["target_total"], 0)
        # 状态文件写在临时目录，绝不在真实库内
        self.assertFalse(
            str(service.store.state_path).startswith(str(REAL_VAULT))
        )

    def test_18b_real_vault_path_rejected_for_writes(self):
        """真实库前缀拉黑机制：用假"真实库"路径验证（不依赖本机真实路径）。"""
        import tempfile
        from unittest.mock import patch
        from pka_graph.store import PathOutsideVault, StateStore

        with tempfile.TemporaryDirectory() as tmp:
            fake_real = Path(tmp).resolve() / "fake-real"
            fake_real.mkdir()
            with patch("pka_graph.store.REAL_VAULT_PREFIX", fake_real):
                with self.assertRaises(PathOutsideVault):
                    StateStore(fake_real)  # 根目录即真实库
                store = StateStore(Path(tmp))
                with self.assertRaises(PathOutsideVault):
                    store.safe_write_path(str(fake_real / "evil.md"))


if __name__ == "__main__":
    unittest.main()
