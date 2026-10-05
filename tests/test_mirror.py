"""真实库受控写入闸门 + mastery_status 镜像的测试。

安全约定：本文件不触碰真实知识库——闸门测试通过 monkeypatch 把
REAL_VAULT_PREFIX 指向临时目录；镜像测试全部在临时副本上操作。
"""
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tests"))

import pka_graph.store as store_module
from fixtures import make_test_vault
from pka_graph import mirror
from pka_graph.service import GraphService
from pka_graph.store import PathOutsideVault, StateStore


class RealVaultGateTests(unittest.TestCase):
    """"目标=真实库"的开关行为（用临时目录冒充真实库前缀）。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.fake_real = Path(self._tmp.name)
        self._patches = patch.object(store_module, "REAL_VAULT_PREFIX", self.fake_real)
        self._patches.start()

    def tearDown(self):
        self._patches.stop()
        self._tmp.cleanup()

    def test_rejected_without_opt_in(self):
        """默认拒绝：指向真实库的构造必须抛 PathOutsideVault。"""
        with self.assertRaises(PathOutsideVault):
            StateStore(self.fake_real)

    def test_allowed_with_explicit_flag(self):
        """显式开关才能构造；且构造本身不写任何文件。"""
        store = StateStore(self.fake_real, allow_real_vault=True)
        self.assertTrue(store.allow_real_vault)
        self.assertFalse((self.fake_real / "个人学习状态").exists())  # 构造零写入

    def test_write_path_narrowed_to_management_dir(self):
        """真实库模式：管理区 + 正式笔记/候选笔记（仅新建）可写，其余被拒。"""
        store = StateStore(self.fake_real, allow_real_vault=True)
        ok = store.safe_write_path("个人学习状态/state.json")
        self.assertTrue(str(ok).endswith("state.json"))
        # 正式笔记/候选笔记 子树允许新建
        ok2 = store.safe_write_path("01-领域一/正式笔记/新笔记.md")
        self.assertTrue(str(ok2).endswith("新笔记.md"))
        # 该区域内已有文件拒绝覆盖
        existing = self.fake_real / "01-领域一" / "正式笔记"
        existing.mkdir(parents=True, exist_ok=True)
        (existing / "已有.md").write_text("人工内容", encoding="utf-8")
        with self.assertRaises(PathOutsideVault):
            store.safe_write_path("01-领域一/正式笔记/已有.md")
        # 其他目录一律拒绝
        with self.assertRaises(PathOutsideVault):
            store.safe_write_path("其他目录/evil.md")
        with self.assertRaises(PathOutsideVault):
            store.safe_write_path("../outside.md")

    def test_test_vault_still_blocks_real_prefix(self):
        """非真实库模式下，路径穿越到（冒充的）真实库仍被拒绝。"""
        with tempfile.TemporaryDirectory() as other:
            store = StateStore(Path(other))
            with self.assertRaises(PathOutsideVault):
                store.safe_write_path(str(self.fake_real / "x.md"))


class SetStatusInTextTests(unittest.TestCase):
    NOTE = '---\nid: note-1\nstatus: candidate\ntitle: "T"\n---\n正文\n'

    def test_insert_before_closing(self):
        new, action = mirror.set_status_in_text(self.NOTE, "untested")
        self.assertEqual(action, "inserted")
        self.assertIn("mastery_status: untested", new)
        # 原有行字节不动
        for line in ("id: note-1", "status: candidate", 'title: "T"'):
            self.assertIn(line, new)
        self.assertIn("正文", new)

    def test_update_existing(self):
        text = '---\nid: note-1\nmastery_status: failed\n---\n正文\n'
        new, action = mirror.set_status_in_text(text, "passed")
        self.assertEqual(action, "updated")
        self.assertIn("mastery_status: passed", new)
        self.assertNotIn("failed", new)

    def test_unchanged_when_same(self):
        text = '---\nid: note-1\nmastery_status: untested\n---\n正文\n'
        new, action = mirror.set_status_in_text(text, "untested")
        self.assertEqual(action, "unchanged")
        self.assertEqual(new, text)

    def test_refuses_without_frontmatter(self):
        with self.assertRaises(mirror.MirrorError):
            mirror.set_status_in_text("没有元数据的正文", "untested")

    def test_refuses_unclosed_frontmatter(self):
        with self.assertRaises(mirror.MirrorError):
            mirror.set_status_in_text("---\nid: x\n没有闭合", "untested")

    def test_refuses_invalid_status(self):
        with self.assertRaises(mirror.MirrorError):
            mirror.set_status_in_text(self.NOTE, "mastered")


class ApplyStatusTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.vault = make_test_vault(Path(self._tmp.name))
        self.store = StateStore(self.vault)
        self.svc = GraphService(self.store)

    def tearDown(self):
        self._tmp.cleanup()

    def test_write_creates_backup_and_is_reversible(self):
        note = self.vault / "正式笔记" / "04-Unit1.md"
        before = note.read_text(encoding="utf-8")
        out = mirror.apply_status(note, "untested", store=self.store)
        self.assertEqual(out["action"], "inserted")
        after = note.read_text(encoding="utf-8")
        self.assertIn("mastery_status: untested", after)
        self.assertIn(before.splitlines()[0], after)
        # 备份存在且可回退
        backups = list(mirror.note_backup_dir(self.store).glob("04-Unit1.*.md.bak"))
        self.assertEqual(len(backups), 1)
        self.assertEqual(backups[0].read_text(encoding="utf-8"), before)
        # 备份扩展名不能是 .md：否则 Obsidian 会把它索引成幽灵笔记节点
        self.assertFalse(backups[0].name.endswith(".md"))
        shutil.copy2(backups[0], note)
        self.assertEqual(note.read_text(encoding="utf-8"), before)  # 完全回退

    def test_refuses_path_outside_vault(self):
        with tempfile.TemporaryDirectory() as other:
            outside = Path(other) / "outside.md"
            outside.write_text("---\nid: x\n---\n", encoding="utf-8")
            with self.assertRaises(mirror.MirrorError):
                mirror.apply_status(outside, "untested", store=self.store)

    def test_sync_node_disabled_is_noop(self):
        node = self.svc.create_node(name="N", domain="D", scope="S")["node"]
        out = mirror.sync_node(self.svc, node["id"], enabled=False)
        self.assertTrue(out.get("skipped"))

    def test_sync_node_writes_linked_note(self):
        node = self.svc.create_node(name="N", domain="D", scope="S")["node"]
        self.svc.import_apply(["正式笔记/04-Unit1.md"],
                              [{"path": "正式笔记/04-Unit1.md", "node_id": node["id"]}],
                              request_id=None, base_version=None)
        out = mirror.sync_node(self.svc, node["id"], enabled=True)
        written = [r for r in out["results"] if r["action"] in ("inserted", "updated")]
        self.assertEqual(len(written), 1)
        self.assertEqual(mirror.read_status_from_text(
            (self.vault / "正式笔记" / "04-Unit1.md").read_text(encoding="utf-8")), "untested")


if __name__ == "__main__":
    unittest.main()
