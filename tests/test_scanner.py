"""只读扫描与身份解析测试（必测 8/9/10）。"""

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tests"))

from fixtures import make_test_vault
from pka_graph import scanner


class ScannerTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.vault = make_test_vault(Path(self._tmp.name))

    def tearDown(self):
        self._tmp.cleanup()

    def _scan(self):
        return scanner.scan_vault(self.vault)

    def test_scan_counts_and_kinds(self):
        r = self._scan()
        self.assertEqual(r["stats"]["target_total"], 5)
        self.assertEqual(r["stats"]["formal"], 1)
        self.assertEqual(r["stats"]["candidate"], 4)
        self.assertEqual(r["stats"]["without_id"], 1)

    def test_8_note_with_stable_id_resolves_after_rename(self):
        """必测 8：有稳定 ID 的笔记改名后仍可解析（按 ID 关联）。"""
        r1 = self._scan()
        match1 = next(e for e in r1["target_notes"] if e["note_id"] == "note-20260926-001")
        old_path = self.vault / match1["path"]
        new_path = old_path.with_name("改过名-Unit1.md")
        old_path.rename(new_path)  # 改名
        r2 = self._scan()
        match2 = next(e for e in r2["target_notes"] if e["note_id"] == "note-20260926-001")
        self.assertEqual(match2["path"], "正式笔记/改过名-Unit1.md")
        self.assertEqual(match2["fingerprint"], match1["fingerprint"])  # 内容未变

    def test_9_duplicate_id_reported_as_conflict(self):
        """必测 9：重复 ID 报冲突，不任取一篇。"""
        r = self._scan()
        self.assertIn("note-20260101-999", r["duplicate_ids"])
        self.assertEqual(len(r["duplicate_ids"]["note-20260101-999"]), 2)
        dup_entries = [e for e in r["target_notes"] if e.get("duplicate_of")]
        self.assertEqual(len(dup_entries), 2)
        # 两篇都保留在报告中（都可见，供人工裁决）
        self.assertEqual({e["path"] for e in dup_entries},
                         {"候选笔记/重复ID甲.md", "候选笔记/重复ID乙.md"})

    def test_10_no_id_note_has_fingerprint_no_id(self):
        """必测 10（前半）：无 ID 笔记以路径+指纹登记，不虚构 ID。"""
        r = self._scan()
        entry = next(e for e in r["target_notes"] if e["note_id"] is None)
        self.assertEqual(entry["path"], "候选笔记/无ID笔记.md")
        self.assertTrue(entry["fingerprint"])

    def test_10b_no_id_content_change_requires_manual_confirm(self):
        """必测 10（后半）：无 ID 笔记内容变化后，旧指纹失效 → 登记服务标记待修复，不猜新身份。
        这里验证服务层预览给出警告且不自动更新登记。"""
        from pka_graph.service import GraphService
        from pka_graph.store import StateStore

        svc = GraphService(StateStore(self.vault))
        svc.import_apply(["候选笔记/无ID笔记.md"], [], request_id="reg-1", base_version=1)
        # 内容变化 → 指纹变化
        path = self.vault / "候选笔记" / "无ID笔记.md"
        path.write_text(path.read_text(encoding="utf-8") + "\n追加内容。\n", encoding="utf-8")
        preview2 = svc.import_preview(["候选笔记/无ID笔记.md"])
        warnings = " ".join(preview2["plan"][0]["warnings"])
        self.assertIn("待修复", warnings)
        # 登记记录未变（仍是旧指纹），提示待人工确认
        state = svc.state()
        self.assertEqual(len(state["ingest_registry"]), 1)

    def test_scan_is_read_only(self):
        """扫描前后：所有目标文件内容逐字节不变。"""
        before = {p: p.read_bytes() for p in self.vault.rglob("*.md")}
        self._scan()
        after = {p: p.read_bytes() for p in self.vault.rglob("*.md")}
        self.assertEqual(
            {p: len(b) for p, b in before.items()}, {p: len(b) for p, b in after.items()}
        )
        for p in before:
            self.assertEqual(before[p], after[p])


    def test_near_target_exclusion_visible(self):
        """文件名含"候选笔记/正式笔记"但目录非目标目录的文件：显式列出排除原因，不静默消失。"""
        (self.vault / "说明目录").mkdir(exist_ok=True)
        frontmatter = "---\ntitle: 流程说明\n---\n正文"
        (self.vault / "说明目录" / "候选笔记说明.md").write_text(frontmatter, encoding="utf-8")
        r = self._scan()
        near = [e for e in r["near_target_excluded"] if e["path"].startswith("说明目录/")]
        self.assertEqual(len(near), 1)
        self.assertIn("非知识笔记", near[0]["reason"])
        # 不计入目标笔记
        self.assertEqual(r["stats"]["target_total"], 5)


if __name__ == "__main__":
    unittest.main()
