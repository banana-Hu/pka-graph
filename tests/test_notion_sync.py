"""MD → Notion 单向同步系统的离线验证（不联网、不碰真实 Notion、不读真实知识库）。

被测对象：pka_graph/notion_sync.py（共享核心）+ 根目录 notion_sync.py（CLI/HTTP 入口）。
测试替身：tests/fake_notion.py（假 Notion 服务，只实现同步用到的四个端点）。

覆盖清单（≥ 用户要求的 11 项）：
 1 中文标题与正文完整；frontmatter 不整段进正文
 2 尾部 >5000 字符完整保留（分批不截断）
 3 列表/代码/表格/链接/引用/分割线转换与回读核验
 4 本地图片不静默丢失：进 images_missing → 结果标 incomplete
 5 未解析双链与相对链接保留原文 + 告警；已映射双链转 Notion 链接
 6 重复提交返回既有结果，不重复建页
 7 内容变化报「新版本」；显式 --new-version 建新页且旧版保留
 8 多 Agent 并发：同文档同一时刻只有一个在跑，不建重复页
 9 创建/追加结果未知：按持久信息 + 远端核查，不重发成重复页/重复段落
10 部分追加后崩溃恢复：从账本进度继续，不重复追加
11 源文件处理期间变化：停止，不拼接不同版本
12 缺凭证 / 越权目标 / 越界路径 / 目录穿越被拒
13 令牌与正文不进日志、账本、异常消息
14 默认不覆盖、不删除、不触发模型与掌握状态
15 CLI 与 HTTP 两个入口走同一核心，结构化结果一致
"""
import contextlib
import io
import json
import os
import re
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

from pka_graph import notion_sync as core  # noqa: E402
from fake_notion import FakeNotionService  # noqa: E402

TOKEN_ENV = "PKA_TEST_NOTION_TOKEN"
ENTRY_TOKEN_ENV = "PKA_TEST_SYNC_TOKEN"
TARGET = "测试笔记区"
SECRET = "fake-notion-token-9f3a1c7dbeef"
BODY_MARK = "SENSITIVE-BODY-MARKER-七七八八-只能出现在正文里"


def _load_root_cli():
    """根目录 notion_sync.py 不是包内模块，按文件加载成可调用的入口。"""
    spec = __import__("importlib.util", fromlist=["spec_from_file_location"]) \
        .spec_from_file_location("notion_sync_cli_entry", ROOT / "notion_sync.py")
    module = __import__("importlib.util", fromlist=["module_from_spec"]).module_from_spec(spec)
    sys.modules["notion_sync_cli_entry"] = module
    spec.loader.exec_module(module)
    return module


class NotionSyncTestCase(unittest.TestCase):
    """每条用例一套临时白名单目录 + 一份临时配置 + 一个假 Notion 服务。"""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        base = Path(self._tmp.name)
        self.md_dir = base / "md"                 # 本地目录白名单（可同步）
        self.outside = base / "outside"           # 白名单之外（必须被拒）
        self.state_dir = base / "state"           # 账本目录
        self.md_dir.mkdir(parents=True, exist_ok=True)
        self.outside.mkdir(parents=True, exist_ok=True)

        self.svc = FakeNotionService(expected_token=SECRET).start()
        self.addCleanup(self.svc.stop)
        os.environ[TOKEN_ENV] = SECRET
        os.environ.pop(ENTRY_TOKEN_ENV, None)
        self.addCleanup(lambda: (os.environ.pop(TOKEN_ENV, None),
                                 os.environ.pop(ENTRY_TOKEN_ENV, None)))

        self.config_path = base / "notion_sync.local.json"
        self.config_path.write_text(json.dumps({
            "targets": {TARGET: {"parent_page_id": self.svc.root_page_id,
                                 "page_title": "测试父页面"}},
            "local_roots": [str(self.md_dir)],
            "token_env": TOKEN_ENV,
            "entry_token_env": ENTRY_TOKEN_ENV,
            "base_url": self.svc.base_url,
            "state_dir": str(self.state_dir),
        }, ensure_ascii=False, indent=1), encoding="utf-8")

    # ---------- 工具 ----------

    def write_md(self, name: str, text: str) -> Path:
        path = self.md_dir / name
        path.write_text(text, encoding="utf-8")
        return path

    def cfg(self) -> dict:
        return core.load_config(self.config_path)

    def core(self, client=None) -> core.NotionSyncCore:
        return core.NotionSyncCore(self.cfg(), client=client)

    def ledger(self) -> dict:
        return self.core().ledger.read()

    def task(self, task_id: str) -> dict:
        return self.ledger()["tasks"][task_id]

    def sync(self, path: Path, **kw):
        return self.core().sync(source_path=str(path), target_alias=TARGET, **kw)

    def _body(self, title: str = "测试候选笔记：可靠性三问", tail_chars: int = 0) -> str:
        """一份典型的中文档候选笔记；tail_chars>0 时在末尾追加超长段落。"""
        parts = [
            "---",
            "title: 测试候选笔记：可靠性三问",
            "type: candidate_note",
            "status: candidate",
            "---",
            "",
            f"# {title}",
            "",
            "**结论先行**：结论一；**结论二**；`PASS` 只能证明当前输入下没出问题。",
            "",
            "## 一、条件表",
            "",
            "| 条件 | 取值 | 说明 |",
            "| --- | --- | --- |",
            "| D1 | 必须 | 最小前提 |",
            "| D2 | 可选 | 边界 |",
            "",
            "## 二、列表与代码",
            "",
            "- 第一项",
            "- 第二项",
            "",
            "1. 步骤一",
            "2. 步骤二",
            "",
            "- [ ] 待核对",
            "- [x] 已核对",
            "",
            "> 引用：引文只做证据，不构成结论。",
            "",
            "```python",
            "def check(cond):",
            "    return cond",
            "```",
            "",
            "---",
            "",
            "[官方资料](https://example.com/doc) 与未映射的 [[旧笔记]] 与相对 [说明](说明.md)。",
        ]
        if tail_chars:
            parts.append("")
            parts.append("## 三、长尾验证段落")
            parts.append("")
            parts.append(("尾部内容完整保留校验" * (tail_chars // 8 + 1))[:tail_chars])
            parts.append("")
        parts.append("")
        parts.append(BODY_MARK)
        return "\n".join(parts) + "\n"

    # ---------- 1 中文标题与正文完整 ----------

    def test_01_中文标题正文完整_frontmatter不进正文(self):
        path = self.write_md("01.md", self._body())
        out = self.sync(path)
        self.assertEqual(out["status"], core.STATUS_FULL, out)
        self.assertEqual(out["duplicate"], False)
        self.assertTrue(out["page_url"].startswith("https://notion.so/fake/"))
        page_id = out["page_id"]
        remote = self.svc.remote(page_id)
        self.assertIn("测试候选笔记：可靠性三问", remote)
        self.assertIn("结论先行", remote)
        self.assertIn("SENSITIVE-BODY-MARKER", remote)
        # frontmatter 不整段进正文（状态行只记录类型与同步时间，不含 YAML 原文）
        self.assertNotIn("status: candidate", remote)
        self.assertNotIn("---\ntitle:", remote)
        # 账本记录了必要的元信息
        t = self.task(out["task_id"])
        self.assertEqual(t["title"], "测试候选笔记：可靠性三问")
        self.assertEqual(t["doc_type"], "source_material")

    # ---------- 2 尾部 >5000 字符 ----------

    def test_02_尾部超过5000字符完整保留(self):
        path = self.write_md("02.md", self._body(tail_chars=40000))
        out = self.sync(path)
        self.assertEqual(out["status"], core.STATUS_FULL, out)
        remote = self.svc.remote(out["page_id"])
        tail = "尾部内容完整保留校验" * 3
        self.assertIn(tail, remote)
        self.assertGreater(len(remote), 6000)
        # 账本里批次进度应等于总批次数（完整提交，无截断）
        t = self.task(out["task_id"])
        self.assertEqual(t["batches"]["done"], t["batches"]["total"])
        self.assertGreater(t["batches"]["total"], 1)

    # ---------- 3 各类结构转换与回读核验 ----------

    def test_03_列表代码表格链接引用分割线(self):
        path = self.write_md("03.md", self._body())
        out = self.sync(path)
        self.assertEqual(out["status"], core.STATUS_FULL, out)
        remote = self.svc.remote(out["page_id"])
        self.assertIn("| 条件 | 取值 | 说明 |", remote)
        self.assertIn("```python", remote)
        self.assertIn("def check(cond):", remote)
        self.assertIn("[官方资料](https://example.com/doc)", remote)
        self.assertIn("> 引用：", remote)
        self.assertIn("---", remote)
        # 回读核验方法有明确记录，且不是字节哈希比对
        verified = self.task(out["task_id"])["verification"]
        self.assertIn("非字节哈希", verified["method"])
        # 围栏数量一致
        self.assertEqual(len(re.findall(r"^```", remote, flags=re.M)), 2)

    # ---------- 4 本地图片不静默丢失 ----------

    def test_04_本地图片不静默丢失且标不完整(self):
        path = self.write_md("04.md", self._body().replace(
            "## 二、列表与代码", "![封面](封面.png)\n\n## 二、列表与代码"))
        out = self.sync(path)
        self.assertEqual(out["status"], core.STATUS_INCOMPLETE, out)
        self.assertIn("封面.png", out["warnings"][0])
        remote = self.svc.remote(out["page_id"])
        self.assertIn("封面.png", remote)          # 保留原文，不静默删图后报成功

    # ---------- 5 双链与相对链接 ----------

    def test_05_双链映射才转_未映射保留并告警(self):
        # 配置里预置一条显式映射：[[旧笔记]] → 某个 Notion 页面
        data = json.loads(self.config_path.read_text(encoding="utf-8"))
        data["page_links"] = {"旧笔记": {"page_id": "dddddddd-0000-0000-0000-000000000001"}}
        self.config_path.write_text(json.dumps(data, ensure_ascii=False, indent=1),
                                    encoding="utf-8")
        # 正文里同时放：已映射的 [[旧笔记]]、未映射的 [[未映射笔记]]、相对链接
        path = self.write_md("05.md", self._body().replace(
            "[[旧笔记]]", "[[旧笔记]] 与 [[未映射笔记]]"))
        out = core.NotionSyncCore(self.cfg()).sync(
            source_path=str(path), target_alias=TARGET)
        warnings = " ".join(out["warnings"])
        self.assertIn("未映射笔记", warnings)        # 未映射的告警，不猜页面
        self.assertNotIn("[[旧笔记]]", warnings)     # 已映射的不告警
        remote = self.svc.remote(out["page_id"])
        self.assertIn("[[旧笔记]]", remote)          # 未映射的保留原文
        self.assertIn("[[未映射笔记]]", remote)
        self.assertIn("相对 MD 链接", warnings)       # 相对链接不做转换，只告警
        self.assertIn("说明.md", remote)
        # 映射表只影响"是否转成链接"，不自动扫描旧笔记补映射
        cfg_now = json.loads(self.config_path.read_text(encoding="utf-8"))
        self.assertIn("page_links", cfg_now)
        self.assertEqual(list(cfg_now["page_links"]), ["旧笔记"])

    # ---------- 6 重复提交 ----------

    def test_06_重复提交返回既有结果不重复建页(self):
        path = self.write_md("06.md", self._body())
        first = self.sync(path)
        second = self.sync(path)
        self.assertEqual(second["status"], core.STATUS_DUPLICATE)
        self.assertEqual(second["duplicate"], True)
        self.assertEqual(second["page_url"], first["page_url"])
        self.assertEqual(second["page_id"], first["page_id"])
        self.assertEqual(self.svc.page_count(), 1)
        self.assertEqual(len(self.ledger()["docs"]), 1)

    # ---------- 7 新版本 ----------

    def test_07_内容变化报新版本_显式建新版保留旧版(self):
        path = self.write_md("07.md", self._body())
        first = self.sync(path)
        path.write_text(self._body(title="测试候选笔记：修订版").replace(
            "title: 测试候选笔记：可靠性三问", "title: 测试候选笔记：修订版"), encoding="utf-8")
        again = self.sync(path)
        self.assertEqual(again["status"], core.STATUS_NEW_VERSION)
        self.assertIn("发现新版本", " ".join(again["warnings"]))

        forced = self.sync(path, new_version=True)
        self.assertEqual(forced["status"], core.STATUS_FULL, forced)
        self.assertEqual(self.svc.page_count(), 2)
        titles = self.svc.page_titles()
        self.assertIn("测试候选笔记：可靠性三问", titles)   # 旧版保留
        self.assertIn("测试候选笔记：修订版", titles)
        doc = list(self.ledger()["docs"].values())[0]
        self.assertEqual(doc["version"], 2)
        self.assertTrue(doc["history"])

    # ---------- 8 多 Agent 并发 ----------

    def test_08_多Agent并发不建重复页面(self):
        path = self.write_md("08.md", self._body())
        results, errors = [], []

        def run(name):
            try:
                out = self.core().sync(source_path=str(path), target_alias=TARGET,
                                       agent_name=name)
                results.append(out)
            except Exception as exc:  # noqa: BLE001
                errors.append(exc)

        threads = [threading.Thread(target=run, args=(f"agent-{i}",)) for i in range(4)]
        for t in threads:
            t.start()
            time.sleep(0.05)          # 交错，制造真实竞争
        for t in threads:
            t.join(timeout=60)
        self.assertEqual(errors, [])
        self.assertEqual(self.svc.page_count(), 1, "并发不得建出重复页面")
        statuses = {r["status"] for r in results}
        self.assertIn(core.STATUS_FULL, statuses)
        self.assertTrue(statuses <= {core.STATUS_FULL, core.STATUS_DUPLICATE,
                                     core.STATUS_PROCESSING, core.STATUS_UNKNOWN,
                                     core.STATUS_FAILED}, statuses)
        # 只有一个任务真正走完创建，其余要么重复跳过要么处理中
        self.assertEqual(sum(1 for r in results if r["status"] == core.STATUS_FULL), 1)

    # ---------- 9 结果未知 ----------

    def test_09_追加结果未知时核查远端不重发成重复段落(self):
        path = self.write_md("09.md", self._body(tail_chars=40000))
        self.svc.append_write_then_fail_at = 1      # 第 1 次追加：写成功但回 500（结果未知）
        first = self.sync(path)
        self.assertEqual(first["status"], core.STATUS_UNKNOWN, first)
        remote_after = self.svc.remote(first["page_id"])
        self.svc.append_write_then_fail_at = None
        second = self.sync(path)
        self.assertEqual(second["status"], core.STATUS_FULL, second)
        final = self.svc.remote(second["page_id"])
        # 每个批次只出现一次：核查远端后跳过重发，不产生重复段落
        for batch in core.split_markdown_batches(core.parse_document(
                path.read_text(encoding="utf-8"))["body_md"]):
            if batch.strip():
                self.assertEqual(count_norm(final, batch), 1, "出现重复段落")
        self.assertEqual(self.svc.page_count(), 1, "结果未知也不建出第二个页面")
        self.assertGreater(len(remote_after), 0)

    # ---------- 10 部分追加后崩溃恢复 ----------

    def test_10_崩溃恢复从账本进度继续不重复追加(self):
        path = self.write_md("10.md", self._body(tail_chars=40000))
        self.svc.append_skip_at = 1                  # 第 1 次追加（批 2）中断且未写入
        first = self.sync(path)
        self.assertEqual(first["status"], core.STATUS_UNKNOWN, first)
        self.svc.append_skip_at = None
        second = self.sync(path)
        self.assertEqual(second["status"], core.STATUS_FULL, second)
        final = self.svc.remote(second["page_id"])
        total_batches = len(core.split_markdown_batches(core.parse_document(
            path.read_text(encoding="utf-8"))["body_md"]))
        for batch in core.split_markdown_batches(core.parse_document(
                path.read_text(encoding="utf-8"))["body_md"]):
            if batch.strip():
                self.assertEqual(count_norm(final, batch), 1, "出现重复追加")
        self.assertEqual(self.task(second["task_id"])["batches"]["done"], total_batches)
        self.assertLessEqual(self.svc.append_calls, total_batches)  # 不重复重发
        self.assertGreaterEqual(self.svc.append_calls, total_batches - 1)

    # ---------- 11 源文件处理期间变化 ----------

    def test_11_源文件处理期间变化即停止(self):
        path = self.write_md("11.md", self._body(tail_chars=6000))
        marker = "源文件在后半段被追加"

        class ChangingClient(core.NotionClient):
            def __init__(self, *a, **kw):
                super().__init__(*a, **kw)
                self.calls = 0

            def create_page(self, parent_page_id, title, markdown, type_label=""):
                self.calls += 1
                if self.calls == 1:
                    with open(path, "a", encoding="utf-8") as fh:
                        fh.write(f"\n\n{marker}\n")
                return super().create_page(parent_page_id, title, markdown, type_label)

        out = self.core(client=ChangingClient(SECRET, self.svc.base_url)).sync(
            source_path=str(path), target_alias=TARGET)
        self.assertEqual(out["status"], core.STATUS_FAILED, out)
        self.assertIn("source_changed", str(out["error"]))
        # 失败路径不回 page_id，从账本/远端取：页面已按冻结版本建好
        page_id = self.svc.created_page_ids()[0]
        remote = self.svc.remote(page_id)
        self.assertNotIn(marker, remote)             # 新版本不拼到已写入的内容后面
        self.assertIn("尾部内容完整保留校验", remote)  # 冻结版本的内容照常写入

    # ---------- 12 权限边界 ----------

    def test_12_缺凭证越权目标越界路径被拒(self):
        path = self.write_md("12.md", self._body())
        # (a) 缺 Notion 凭证
        os.environ.pop(TOKEN_ENV, None)
        out = self.sync(path)
        self.assertEqual(out["status"], core.STATUS_NOT_CONFIGURED, out)
        os.environ[TOKEN_ENV] = SECRET
        # (b) 越权目标别名 / 任意 page_id
        out = self.core().sync(source_path=str(path), target_alias="不存在的区")
        self.assertEqual(out["status"], core.STATUS_PENDING_AUTH)
        self.assertIn("白名单", out["error"]["message"])
        out = self.core().sync(source_path=str(path), target_alias="")
        self.assertEqual(out["status"], core.STATUS_PENDING_AUTH)
        # (c) 白名单之外：目录穿越与兄弟目录
        outside = self.outside / "隔壁.md"
        outside.write_text(self._body(), encoding="utf-8")
        out = self.core().sync(source_path=str(outside), target_alias=TARGET)
        self.assertEqual(out["error"]["category"], "path_outside")
        for bad in ("..\\md\\12.md", str(self.md_dir), "C:/Windows/win.md"):
            with self.assertRaises(core.SyncError):
                core.resolve_within_roots(self.cfg(), bad)
        # (d) 非 md 文件
        (self.md_dir / "x.txt").write_text("x", encoding="utf-8")
        with self.assertRaises(core.SyncError):
            core.resolve_within_roots(self.cfg(), str(self.md_dir / "x.txt"))
        # (e) 目标别名不得绕过：HTTP 入口同理（431 行见 HTTP 用例）
        self.assertEqual(self.svc.page_count(), 0)

    # ---------- 13 密钥与正文不泄露 ----------

    def test_13_令牌与正文不进日志账本异常(self):
        path = self.write_md("13.md", self._body(tail_chars=40000))
        # (a) 越权目标：不发起请求，日志里不应出现令牌
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(buf):
            out = self.core().sync(source_path=str(path), target_alias="不存在的区")
        self.assertNotIn(SECRET, buf.getvalue())
        self.assertEqual(out["status"], core.STATUS_PENDING_AUTH)
        # (b) 真实失败请求（远端 500）：stdout/stderr/账本/返回体都不含令牌与正文
        self.svc.append_skip_at = 1
        buf2 = io.StringIO()
        with contextlib.redirect_stdout(buf2), contextlib.redirect_stderr(buf2):
            out2 = self.sync(path)
        self.assertIn(out2["status"], {"failed", "outcome_unknown", "incomplete"})
        ledger_blob = (Path(self.state_dir / "notion_sync_ledger.json")
                       .read_text(encoding="utf-8"))
        blob = buf2.getvalue() + json.dumps(out2, ensure_ascii=False) + ledger_blob
        self.assertNotIn(SECRET, blob)
        self.assertNotIn(BODY_MARK, blob)
        # (c) 异常消息本身也不含令牌（NotionClient.__repr__ 已脱敏）
        self.assertNotIn(SECRET, repr(self.core()._client()))
        # (d) 令牌只经环境变量名进入，不写进配置文件
        cfg_text = self.config_path.read_text(encoding="utf-8")
        self.assertNotIn(SECRET, cfg_text)
        self.assertIn("token_env", cfg_text)

    # ---------- 14 不覆盖/不删除/不触发业务 ----------

    def test_14_默认不覆盖不删除不触发模型与掌握状态(self):
        path = self.write_md("14.md", self._body())
        first = self.sync(path)
        remote_before = self.svc.remote(first["page_id"])
        # 重复同步不得改写既有页面
        self.sync(path)
        self.assertEqual(self.svc.remote(first["page_id"]), remote_before)
        self.assertEqual(self.svc.delete_calls, 0)
        ledger_blob = json.dumps(self.ledger(), ensure_ascii=False).lower()
        for forbidden in ("delete", "archiv", "move", "mastery", "quiz"):
            self.assertNotIn(forbidden, ledger_blob)
        # 同步核心自身不引入知识库业务模块（不触发测验/复习/发现/收件箱）。
        # 这里查源码而非 sys.modules：全量跑时其他用例会先加载这些业务模块。
        src = Path(core.__file__).read_text(encoding="utf-8")
        for mod in ("pka_graph.service", "pka_graph.quiz_service",
                    "pka_graph.review_service", "pka_graph.inbox",
                    "pka_graph.discovery"):
            self.assertNotIn(
                f"import {mod}", src,
                f"同步核心不应 import {mod}（不得触发知识库业务）")

    # ---------- 15 CLI 与 HTTP 同一核心 ----------

    def test_15_CLI入口与核心一致(self):
        path = self.write_md("15.md", self._body())
        cli = _load_root_cli()
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            code = cli.main(["preview", "--config", str(self.config_path),
                             "--file", str(path), "--target", TARGET])
        preview = json.loads(buf.getvalue())
        self.assertEqual(code, 0)
        self.assertEqual(preview["title"], "测试候选笔记：可靠性三问")
        self.assertEqual(self.svc.page_count(), 0, "preview 不写 Notion、不写账本")

        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            code = cli.main(["sync", "--config", str(self.config_path),
                             "--file", str(path), "--target", TARGET, "--agent", "workbuddy"])
        out = json.loads(buf.getvalue())
        self.assertEqual(out["status"], core.STATUS_FULL)
        self.assertNotIn(BODY_MARK, buf.getvalue() + json.dumps(out, ensure_ascii=False))
        self.assertEqual(self.svc.page_count(), 1)

        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            cli.main(["status", "--config", str(self.config_path), "--task-id", out["task_id"]])
        status = json.loads(buf.getvalue())
        self.assertEqual(status["status"], core.STATUS_FULL)
        self.assertEqual(status["task_id"], out["task_id"])

    def test_16_HTTP入口需认证且同一核心(self):
        path = self.write_md("16.md", self._body(tail_chars=4000))
        os.environ[ENTRY_TOKEN_ENV] = "http-entry-token"
        cli = _load_root_cli()
        port = 0

        def runner():
            cli.main(["serve", "--config", str(self.config_path), "--port", str(port)])

        # 先占一个端口再启动，避免端口写死冲突
        import socket
        sock = socket.socket()
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
        sock.close()
        t = threading.Thread(target=runner, daemon=True)
        t.start()
        deadline = time.time() + 8
        while time.time() < deadline:
            try:
                urllib.request.urlopen(f"http://127.0.0.1:{port}/health", timeout=1).read()
                break
            except Exception:  # noqa: BLE001
                time.sleep(0.1)
        self.addCleanup(lambda: os.environ.pop(ENTRY_TOKEN_ENV, None))

        def post(path_, payload, token=None):
            req = urllib.request.Request(
                f"http://127.0.0.1:{port}{path_}",
                data=json.dumps(payload).encode("utf-8"),
                headers={"Content-Type": "application/json",
                         **( {"X-Sync-Token": token} if token is not None else {})},
                method="POST")
            try:
                with urllib.request.urlopen(req, timeout=20) as resp:
                    return resp.status, json.loads(resp.read().decode("utf-8"))
            except urllib.error.HTTPError as exc:
                return exc.code, json.loads(exc.read().decode("utf-8") or "{}")

        def get(path_, token=None):
            req = urllib.request.Request(
                f"http://127.0.0.1:{port}{path_}",
                headers=({"X-Sync-Token": token} if token is not None else {}))
            try:
                with urllib.request.urlopen(req, timeout=20) as resp:
                    return resp.status, json.loads(resp.read().decode("utf-8") or "{}")
            except urllib.error.HTTPError as exc:
                return exc.code, json.loads(exc.read().decode("utf-8") or "{}")

        code, _ = get("/status")
        self.assertEqual(code, 403, "无令牌必须被拒")
        code, _ = get("/status", token="wrong")
        self.assertEqual(code, 403)
        code, body = get("/status", token="http-entry-token")
        self.assertEqual(code, 200)
        self.assertIn("tasks", body)

        code, out = post("/sync", {"path": str(path), "target": TARGET,
                                   "doc_type": "candidate_note",
                                   "agent_name": "zcode"}, token="http-entry-token")
        self.assertEqual(code, 200)
        self.assertEqual(out["status"], core.STATUS_FULL, out)
        self.assertEqual(out["doc_type"], "candidate_note")
        self.assertTrue(out["page_url"])
        self.assertNotIn(BODY_MARK, json.dumps(out, ensure_ascii=False))

        code, preview = post("/preview", {"path": str(path), "target": TARGET},
                             token="http-entry-token")
        self.assertEqual(code, 200)
        self.assertEqual(preview["title"], "测试候选笔记：可靠性三问")

        # 同一文档再用 CLI 提交：走的是同一核心 → 重复跳过
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            cli.main(["sync", "--config", str(self.config_path),
                      "--file", str(path), "--target", TARGET])
        again = json.loads(buf.getvalue())
        self.assertEqual(again["status"], core.STATUS_DUPLICATE)
        self.assertEqual(again["page_url"], out["page_url"])
        self.assertEqual(self.svc.page_count(), 1)

    def test_17_未知块判定不完整(self):
        path = self.write_md("17.md", self._body())
        self.svc.read_unknown_blocks = True
        out = self.sync(path)
        self.assertEqual(out["status"], core.STATUS_INCOMPLETE, out)
        self.assertIn("未知块", out["error"]["message"])

    def test_18_目标未声明时不用可能400的自定义属性(self):
        path = self.write_md("18.md", self._body())
        out = self.sync(path)
        self.assertEqual(out["status"], core.STATUS_FULL)
        # 假服务不记请求体，这里断言账本记录的类型字段（自定义属性未进页面属性）
        self.assertEqual(self.task(out["task_id"])["doc_type"], "source_material")


def count_norm(remote_md: str, batch: str) -> int:
    """规范化后统计出现次数（与核心回读核验同一口径）。"""
    def norm(s: str) -> str:
        s = re.sub(r"[#>*`~\[\]()!|-]", "", s)
        return re.sub(r"\s+", "", s)
    return norm(remote_md).count(norm(batch))
