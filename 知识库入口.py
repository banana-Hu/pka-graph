"""知识库入口：把文件和链接投进知识库的桌面客户端（增量一：文件通道）。

人工操作的主入口：选文件 → 点「进入知识库」→ 程序自动评估、写中文简报笔记、
录入 {领域}/正式笔记/。窗口只负责投递与状态展示；知识内容一律在 Obsidian 里看。

- 服务端：连接本机知能图谱服务（优先 8766 真实库，其次 8765 测试库），
  任务模式投递单文件路径到 POST /api/tasks；旧链路保留 /api/inbox/ingest。
- 状态：轮询 /api/inbox/registry 与已入库建议，把每条输入的实时结果显示在列表里。
- 本窗口不写任何知识库文件；离开服务进程不做任何事（不宣称离线功能）。

启动：pythonw 知识库入口.py   （见 启动知识库入口.bat）
"""

from __future__ import annotations

import base64
import json
import os
import queue
import threading
import tkinter as tk
import uuid
from pathlib import Path
from tkinter import filedialog, messagebox, ttk
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

PREFERRED_PORTS = (8766, 8765)
SUPPORTED = [("知识材料", "*.pdf *.md *.txt"), ("全部文件", "*.*")]
POLL_MS = 2500


class ApiClient:
    """对本地服务的最小 HTTP 封装。

    旧链路（legacy）：写令牌经 /api/meta 获取，仅内存。
    任务链路（task）：/api/meta 不再下发写令牌；桌面经管理区的单次票据
    （服务可再生）交换 HttpOnly 会话，票据过期或会话失效时提示从主程序重新连接，
    绝不退回读取页面明文令牌。
    """

    def __init__(self, port: int):
        self.port = port
        self.token = ""          # legacy 写令牌
        self.session = ""        # task 模式会话令牌（仅内存）
        self.pipeline_mode = "legacy"
        self.vault_root = ""
        self.banner = ""
        self.auth_note = ""
        self._submitted_tasks: dict[str, str] = {}
        self._tasks_lock = threading.Lock()

    def _headers(self) -> dict:
        headers = {"Content-Type": "application/json"}
        if self.session:
            headers["X-PKA-Session"] = self.session
        elif self.token:
            headers["X-PKA-Token"] = self.token
        return headers

    def _get(self, path: str, timeout: float = 8.0):
        request = Request(f"http://127.0.0.1:{self.port}{path}", headers=self._headers())
        with urlopen(request, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))

    def _post(self, path: str, payload: dict, timeout: float = 60.0):
        body = json.dumps(payload).encode("utf-8")
        req = Request(f"http://127.0.0.1:{self.port}{path}", data=body,
                      headers=self._headers(), method="POST")
        with urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))

    def desktop_ticket(self) -> str:
        """桌面票据来源：启动器注入的环境变量优先，其次知识库管理区的票据文件。"""
        injected = (os.environ.get("PKA_SESSION_TICKET") or "").strip()
        if injected:
            return injected
        if not self.vault_root:
            return ""
        path = Path(self.vault_root) / "个人学习状态" / "runtime" / "desktop_ticket.json"
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return ""
        return str((data or {}).get("ticket") or "")

    def connect(self) -> bool:
        for port in PREFERRED_PORTS:
            try:
                meta = ApiClient(port)._get("/api/meta", timeout=3)
            except Exception:  # noqa: BLE001 —— 端口不通就试下一个
                continue
            self.port = port
            self.vault_root = meta.get("test_vault") or ""
            self.banner = meta.get("banner") or ""
            self.pipeline_mode = meta.get("pipeline_mode") or "legacy"
            self.token = meta.get("token") or ""
            self.session = ""
            self.auth_note = ""
            if self.token:
                return True   # 旧链路兼容：行为与之前一致
            ticket = self.desktop_ticket()
            if not ticket:
                self.auth_note = ("服务在任务模式运行，但管理区没有可用票据："
                                  "请在主程序中重新打开入口连接（不会退回读取页面令牌）")
                return False
            try:
                out = self._post("/api/session/exchange", {"ticket": ticket}, timeout=5)
            except Exception as exc:  # noqa: BLE001 —— 交换失败按未认证处理
                self.auth_note = f"票据交换失败（{type(exc).__name__}）：请从主程序重新连接"
                return False
            self.session = str(out.get("session_token") or "")
            if not self.session:
                self.auth_note = "票据交换未返回会话：请从主程序重新连接"
                return False
            return True

    def ingest(self, path: Path) -> dict:
        path = Path(path).resolve()
        if not path.is_file():
            raise ValueError("请选择存在的单个文件")
        if self.pipeline_mode == "task":
            task = self._post("/api/tasks", {"contract_version": 1,
                "request_id": "desktop-" + uuid.uuid4().hex,
                "source": {"kind": "file", "path": str(path)},
                "options": {"pipeline": "note", "publish_to": None}})
            task_id = str(task.get("task_id") or "")
            if not task_id:
                raise ValueError("服务未返回任务标识，不能确认已投递")
            with self._tasks_lock:
                self._submitted_tasks[task_id] = path.name
            return {"queued": "task:" + task_id, "task_id": task_id,
                    "status": task.get("status", "queued")}
        raw = path.read_bytes()
        return self._post("/api/inbox/ingest",
                          {"name": path.name,
                           "data_b64": base64.b64encode(raw).decode("ascii")})

    def snapshot(self) -> dict:
        """一次轮询：处理记录 + 已入库建议（id → 标题）+ 收件箱状态（+ 任务快照）。"""
        registry = self._get("/api/inbox/registry").get("processed") or {}
        accepted = {}
        try:
            for s in self._get("/api/discovery/suggestions?status=accepted").get("suggestions") or []:
                accepted[s.get("id")] = s.get("name") or ""
        except HTTPError:
            pass
        status = self._get("/api/inbox/status")
        tasks: dict[str, dict] = {}
        if self.pipeline_mode == "task":
            with self._tasks_lock:
                submitted = dict(self._submitted_tasks)
            for task_id, name in submitted.items():
                registry.setdefault("task:" + task_id, {"task_id": task_id, "name": name})
            for entry in list(registry.values()):
                task_id = (entry or {}).get("task_id")
                if not task_id or task_id in tasks:
                    continue
                try:
                    tasks[task_id] = self._get(f"/api/tasks/{task_id}")
                except Exception:  # noqa: BLE001 —— 单个任务读取失败不影响整轮
                    continue
        return {"registry": registry, "accepted": accepted, "status": status, "tasks": tasks}


TASK_STATUS_TEXT = {"queued": "⏳ 排队中", "running": "⚙️ 处理中", "retry_wait": "⏳ 等待重试",
                    "blocked": "⛔ 缺少配置/材料", "needs_review": "🔎 待确认",
                    "unknown_result": "❓ 结果未知（需核实）", "succeeded": "✅ 已完成",
                    "failed": "❌ 失败", "cancelled": "🚫 已取消"}


def describe_task(task: dict) -> str:
    """把统一任务快照翻成一行中文状态（不把失败/未确认说成成功）。"""
    parts = [TASK_STATUS_TEXT.get(str(task.get("status")), str(task.get("status") or "?"))]
    if task.get("stage"):
        parts.append(f"阶段：{task['stage']}")
    progress = task.get("progress") or {}
    if progress.get("total_units"):
        parts.append(f"{progress.get('completed_units', 0)}/{progress['total_units']}")
    error = task.get("error") or {}
    if error.get("message"):
        parts.append(str(error["message"])[:120])
    result = task.get("result") or {}
    if result.get("note_status"):
        parts.append(f"笔记：{result['note_status']}")
    if result.get("disposition"):
        parts.append(f"处置：{result['disposition']}")
    if result.get("notion_page_id"):
        parts.append("已同步 Notion")
    return "｜".join(parts)


def describe_entry(entry: dict, accepted: dict, tasks: dict | None = None) -> str:
    """把注册表条目翻成用户能懂的一行状态；任务模式下优先显示统一任务快照。"""
    task = (tasks or {}).get((entry or {}).get("task_id"))
    if task:
        return describe_task(task)
    st = entry.get("status")
    if st == "suggested":
        if entry.get("auto_accepted") and accepted.get(entry.get("suggestion_id")):
            return "✅ 已入库：" + accepted[entry["suggestion_id"]]
        return "📝 已分析，等待入库"
    if st == "pending_analysis":
        return "⏳ 排队中（等待模型配置/预算恢复）"
    if st == "failed":
        return "❌ 失败：" + str(entry.get("note") or "原因未知")
    if st == "insufficient":
        return "⚠️ 材料不足（可提取文本太短）"
    if st == "skipped":
        return "↪️ 与已入库内容相同（去重）"
    return st or "?"


class App:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.api = ApiClient(PREFERRED_PORTS[0])
        self.events: queue.Queue = queue.Queue()
        self.rows: dict[str, str] = {}        # inbox rel → treeview item id
        self.pending_files: dict[str, str] = {}   # 本地路径 → treeview item id（未提交）
        self._stop = threading.Event()
        self._worker: threading.Thread | None = None
        self._connected = False
        self._after_id = None
        root.title("知识库入口")
        root.geometry("780x460")
        root.minsize(640, 380)

        top = ttk.Frame(root, padding=8)
        top.pack(fill="x")
        ttk.Button(top, text="选择文件…", command=self.pick_files).pack(side="left")
        ttk.Button(top, text="进入知识库", command=self.submit).pack(side="left", padx=6)
        ttk.Button(top, text="打开知识库", command=self.open_vault).pack(side="right")
        ttk.Label(top, text="支持 PDF / Markdown / TXT，完整处理单文件").pack(side="right", padx=10)

        cols = ("file", "status")
        self.tree = ttk.Treeview(root, columns=cols, show="headings", height=14)
        self.tree.heading("file", text="文件")
        self.tree.heading("status", text="状态")
        self.tree.column("file", width=280)
        self.tree.column("status", width=440)
        self.tree.pack(fill="both", expand=True, padx=8, pady=(2, 4))

        self.status_var = tk.StringVar(value="正在连接本机服务…")
        bar = ttk.Frame(root, padding=(8, 4))
        bar.pack(fill="x")
        ttk.Label(bar, textvariable=self.status_var, foreground="#555").pack(side="left")

        root.protocol("WM_DELETE_WINDOW", self.close)
        self._start_worker()
        self._after_id = root.after(400, self._on_tick)

    # ---------- 连接与轮询 ----------

    def _connect_loop(self):
        while not self._stop.is_set():
            if self.api.connect():
                self._connected = True
                self.events.put({"type": "conn", "ok": True})
                return True
            self.events.put({"type": "conn", "ok": False})
            self._stop.wait(3.0)
        return False

    def _start_worker(self):
        if self._stop.is_set() or (self._worker and self._worker.is_alive()):
            return
        self._worker = threading.Thread(target=self._poll_loop, daemon=True, name="knowledge-desktop-poll")
        self._worker.start()

    def _on_tick(self):
        if self._stop.is_set():
            return
        self._drain_events()
        self._after_id = self.root.after(400, self._on_tick)

    def _poll_loop(self):
        while not self._stop.is_set():
            if not self._connected and not self._connect_loop():
                return
            if self._stop.is_set():
                return
            if not self._poll_once():
                self._connected = False
                self.api.session = ""
                self.api.token = ""
            self._stop.wait(POLL_MS / 1000)

    def _poll_once(self):
        try:
            snap = self.api.snapshot()
        except HTTPError as exc:
            # 会话失效/无权限：明确提示重新连接，不假装还在正常工作
            self.events.put({"type": "auth_lost" if exc.code in (401, 403) else "conn_lost"})
            return False
        except Exception:  # noqa: BLE001 —— 服务短暂不可达：下一轮再试
            self.events.put({"type": "conn_lost"})
            return False
        if self._stop.is_set():
            return False
        inbox = snap.get("status") or {}
        limit = inbox.get("daily_limit") or 0
        budget = f"今日模型额度 {inbox.get('used_today', 0)}/{limit or '∞'}"
        if inbox.get("task_mode"):
            auto = "AI 自动审核入库（任务队列）"
        else:
            auto = "自动入库开" if inbox.get("auto_accept") else "自动入库关"
        self.events.put({"type": "snap", "snap": snap,
                         "statusline": f"已连接 127.0.0.1:{self.api.port}　|　{auto}　|　{budget}"})
        return True

    def close(self):
        if self._stop.is_set():
            return
        self._stop.set()
        if self._after_id is not None:
            self.root.after_cancel(self._after_id)
            self._after_id = None
        self.root.destroy()

    # ---------- 界面动作 ----------

    def refresh_caption(self):
        self.root.title(f"知识库入口 —— {self.api.banner or '知能图谱'}")

    def pick_files(self):
        names = filedialog.askopenfilenames(title="选择要进入知识库的材料", filetypes=SUPPORTED)
        for n in names:
            p = Path(n)
            if str(p) not in self.pending_files and p.name not in {
                    self.tree.set(i, "file") for i in self.tree.get_children()}:
                item = self.tree.insert("", "end", values=(p.name, "待提交"))
                self.pending_files[str(p)] = item

    def submit(self):
        if self._stop.is_set():
            return
        if not (self.api.token or self.api.session):
            messagebox.showwarning(
                "知识库入口",
                "服务未连接或未认证，无法投递。\n"
                + (self.api.auth_note or "请先启动知能图谱服务，并从主程序打开入口连接。"))
            return
        if not self.pending_files:
            messagebox.showinfo("知识库入口", "先点「选择文件…」选择要入库的材料。")
            return
        files = list(self.pending_files.items())
        self.pending_files = {}
        threading.Thread(target=self._submit_worker, args=(files,), daemon=True).start()

    def _submit_worker(self, files):
        for path_str, item in files:
            if self._stop.is_set():
                return
            p = Path(path_str)
            self.events.put({"type": "row", "item": item, "status": "提交中…"})
            try:
                out = self.api.ingest(p)
                task_note = f"，任务 {out['task_id'][:8]}" if out.get("task_id") else ""
                self.events.put({"type": "row", "item": item,
                                 "key": out["queued"],
                                 "status": f"⏳ 已提交（{out['queued']}{task_note}）"})
            except HTTPError as exc:
                try:
                    msg = json.loads(exc.read().decode("utf-8")).get("error", "")
                except Exception:  # noqa: BLE001
                    msg = f"HTTP {exc.code}"
                self.events.put({"type": "row", "item": item, "status": f"❌ 提交被拒：{msg}"})
            except (URLError, OSError, ValueError) as exc:
                self.events.put({"type": "row", "item": item,
                                 "status": f"❌ 提交失败：{exc}"})

    def open_vault(self):
        if self.api.vault_root and Path(self.api.vault_root).exists():
            os.startfile(self.api.vault_root)  # noqa: S606 —— 本机资源管理器
        else:
            messagebox.showwarning("知识库入口", "服务未连接或知识库路径不存在。")

    # ---------- 轮询结果应用到界面（仅主线程触碰控件） ----------

    def _drain_events(self):
        try:
            while True:
                ev = self.events.get_nowait()
                kind = ev.get("type")
                if kind == "row":
                    if ev.get("key"):
                        self.rows[ev["key"]] = ev["item"]
                    self.tree.set(ev["item"], "status", ev["status"])
                elif kind == "conn":
                    if ev["ok"]:
                        mode = "任务模式（AI 自动审核）" if self.api.pipeline_mode == "task" else "旧链路"
                        self.status_var.set(f"已连接 127.0.0.1:{self.api.port}｜{mode}｜{self.api.banner}")
                        self.refresh_caption()
                    else:
                        self.status_var.set("服务未运行或未认证：" + (self.api.auth_note or "请先启动主程序"))
                elif kind == "auth_lost":
                    self.status_var.set("会话已失效：请从主程序重新打开入口连接")
                elif kind == "snap":
                    reg = ev["snap"].get("registry") or {}
                    accepted = ev["snap"].get("accepted") or {}
                    tasks = ev["snap"].get("tasks") or {}
                    for rel, entry in reg.items():
                        item = self.rows.get(rel)
                        if item is None:
                            item = self.tree.insert("", "end",
                                                    values=(entry.get("name") or rel, describe_entry(entry, accepted, tasks)))
                            self.rows[rel] = item
                        else:
                            self.tree.set(item, "status", describe_entry(entry, accepted, tasks))
                    self.status_var.set(ev["statusline"])
                elif kind == "conn_lost":
                    self.status_var.set("服务连接中断——重试中…")
        except queue.Empty:
            pass


def main() -> int:
    root = tk.Tk()
    try:
        style = ttk.Style(root)
        if "vista" in style.theme_names():
            style.theme_use("vista")
    except tk.TclError:
        pass
    app = App(root)
    root.mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
