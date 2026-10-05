"""MD → Notion 同步统一入口（多 Agent 共用）。

两种入口、同一套核心（pka_graph.notion_sync.NotionSyncCore）：
- CLI（本机操作者/能跑终端的 Agent）：preview / sync / status
- HTTP（能调 HTTP 的 Agent）：POST /preview、POST /sync、GET /status
  仅监听 127.0.0.1；必须携带 X-Sync-Token（PKA_SYNC_TOKEN，与 Notion 令牌互相独立）。

命令：
    python notion_sync.py preview --file <md路径> --target <别名> [--doc-type ...]
    python notion_sync.py sync   --file <md路径> --target <别名> [--new-version]
    python notion_sync.py status [--task-id <id>]
    python notion_sync.py serve  [--port 8767] [--config <路径>]

配置：notion_sync.local.json（复制 notion_sync.example.json 填写；gitignored）。
"""

from __future__ import annotations

import argparse
import json
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from pka_graph.notion_sync import (  # noqa: E402
    STATUS_PROCESSING, SyncError, load_config, NotionSyncCore)

DEFAULT_CONFIG = ROOT / "notion_sync.local.json"


def _core(config_path: Path) -> NotionSyncCore:
    return NotionSyncCore(load_config(config_path))


# ---------- CLI ----------

def _cli_preview(args, config_path) -> int:
    out = _core(config_path).preview(source_path=args.file, target_alias=args.target,
                                     doc_type=args.doc_type)
    print(json.dumps(out, ensure_ascii=False, indent=1))
    return 0


def _cli_sync(args, config_path) -> int:
    out = _core(config_path).sync(source_path=args.file, target_alias=args.target,
                                  doc_type=args.doc_type, agent_name=args.agent or "cli",
                                  new_version=args.new_version)
    print(json.dumps(out, ensure_ascii=False, indent=1))
    return 0 if out.get("status") == "full_success" else 1


def _cli_status(args, config_path) -> int:
    data = _core(config_path).ledger.read()
    if args.task_id:
        task = data["tasks"].get(args.task_id)
        if task is None:
            print(json.dumps({"error": "任务不存在", "task_id": args.task_id},
                             ensure_ascii=False))
            return 1
        print(json.dumps(task, ensure_ascii=False, indent=1))
        return 0 if task.get("status") == "full_success" else 1
    recent = sorted(data["tasks"].values(),
                    key=lambda t: t.get("updated_at", ""), reverse=True)[:20]
    print(json.dumps({"tasks": [{"task_id": t["task_id"], "status": t["status"],
                                 "title": t.get("title"), "page_url": t.get("url"),
                                 "error": t.get("error")} for t in recent]},
                    ensure_ascii=False, indent=1))
    return 0


# ---------- HTTP 入口 ----------

def _serve(args, config_path) -> int:
    core = _core(config_path)
    entry_token = ""
    import os

    try:
        cfg = core.cfg
        entry_token = os.environ.get(cfg["entry_token_env"], "").strip()
    except SyncError as exc:
        print(f"[notion-sync] 配置未就绪：{exc}", flush=True)

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, fmt, *a):
            pass

        def _json(self, code: int, payload) -> None:
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _authed(self) -> bool:
            if not entry_token:
                self._json(503, {"error": "HTTP 入口令牌未配置（设置 "
                                          f"{core.cfg.get('entry_token_env')} 后重启本入口）",
                                 "category": "not_configured"})
                return False
            if self.headers.get("X-Sync-Token", "") != entry_token:
                self._json(403, {"error": "同步入口访问令牌无效", "category": "forbidden"})
                return False
            return True

        def do_GET(self):
            if self.path == "/health":
                self._json(200, {"ok": True, "service": "notion-sync"})
                return
            if not self._authed():
                return
            if self.path.startswith("/status"):
                task_id = self.path.partition("?task_id=")[2] or None
                data = core.ledger.read()
                if task_id:
                    task = data["tasks"].get(task_id)
                    self._json(200, {"task": task} if task
                               else {"error": "任务不存在", "task_id": task_id})
                    return
                recent = sorted(data["tasks"].values(),
                                key=lambda t: t.get("updated_at", ""), reverse=True)[:20]
                self._json(200, {"tasks": [
                    {"task_id": t["task_id"], "status": t["status"],
                     "title": t.get("title"), "page_url": t.get("url"),
                     "error": t.get("error")} for t in recent]})
                return
            self._json(404, {"error": "未知路径"})

        def do_POST(self):
            if not self._authed():
                return
            length = int(self.headers.get("Content-Length") or 0)
            try:
                payload = json.loads(self.rfile.read(length).decode("utf-8")) if length else {}
            except json.JSONDecodeError:
                self._json(400, {"error": "请求体不是合法 JSON", "category": "invalid_input"})
                return
            try:
                if self.path == "/preview":
                    self._json(200, core.preview(
                        source_path=payload.get("path", ""),
                        target_alias=payload.get("target", ""),
                        doc_type=payload.get("doc_type", "source_material")))
                    return
                if self.path == "/sync":
                    out = core.sync(
                        source_path=payload.get("path", ""),
                        target_alias=payload.get("target", ""),
                        doc_type=payload.get("doc_type", "source_material"),
                        agent_name=str(payload.get("agent_name", ""))[:60],
                        new_version=bool(payload.get("new_version")))
                    self._json(200, out)
                    return
            except SyncError as exc:
                self._json(400, {"error": str(exc)[:200], "category": exc.category})
                return
            except Exception as exc:  # noqa: BLE001 —— 只回类别，不回正文/令牌
                self._json(500, {"error": f"{type(exc).__name__}", "category": "failed"})
                return
            self._json(404, {"error": "未知路径"})

    server = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    print(f"[notion-sync] HTTP 入口已启动：http://127.0.0.1:{args.port}"
          f"（仅本机；需 X-Sync-Token）", flush=True)
    print("[notion-sync] 路由：POST /preview、POST /sync、GET /status、GET /health",
          flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="MD → Notion 单向同步（多 Agent 共用入口）")
    parser.add_argument("--config", default=str(DEFAULT_CONFIG),
                        help="同步配置路径（默认工程根 notion_sync.local.json）")
    sub = parser.add_subparsers(dest="cmd", required=True)

    # 子命令也能带 --config（默认跟随顶层；SUPPRESS 避免子解析器的默认值覆盖顶层值）
    for name, help_ in (("preview", "预览输入与目标，不写 Notion"),
                        ("sync", "同步一个 MD 文件到目标别名"),
                        ("status", "查询任务状态")):
        sp = sub.add_parser(name, help=help_)
        sp.add_argument("--config", default=argparse.SUPPRESS,
                        help="同步配置路径（默认工程根 notion_sync.local.json）")
        if name != "status":
            sp.add_argument("--file", required=True, help="要同步的 MD 文件路径")
            sp.add_argument("--target", required=True, help="目标别名（配置内白名单）")
            sp.add_argument("--doc-type", default="source_material",
                            choices=["source_material", "candidate_note"],
                            help="材料类型（由提交方声明）")
        if name == "sync":
            sp.add_argument("--agent", default="", help="提交方标识（仅显示用）")
            sp.add_argument("--new-version", action="store_true",
                            help="内容变化时显式创建新版本（旧版保留）")
        if name == "status":
            sp.add_argument("--task-id", default=None, help="查询指定任务")

    sp_serve = sub.add_parser("serve", help="启动本地 HTTP 入口（仅 127.0.0.1）")
    sp_serve.add_argument("--port", type=int, default=8767)
    sp_serve.add_argument("--config", default=argparse.SUPPRESS,
                          help="同步配置路径（默认工程根 notion_sync.local.json）")

    args = parser.parse_args(argv)
    config_path = Path(args.config)
    handlers = {"preview": _cli_preview, "sync": _cli_sync, "status": _cli_status,
                "serve": _serve}
    try:
        return handlers[args.cmd](args, config_path)
    except SyncError as exc:
        print(json.dumps({"error": str(exc)[:300], "category": exc.category},
                         ensure_ascii=False, indent=1))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
