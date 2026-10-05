"""假 Notion 服务：离线验证 MD→Notion 同步用（不联网、不碰真实 Notion）。

只实现同步系统实际调用到的官方端点子集：
    POST  /v1/pages                 创建页面（收 markdown 参数）
    PATCH /v1/pages/{id}/markdown   追加（insert_content）
    GET   /v1/pages/{id}/markdown   回读核验
    GET   /v1/blocks/{id}/children  结果未知时按标题定位子页面

对外只暴露计数与只读快照供测试断言；**不提供删除/移动/归档接口**，
因此测试可以断言"同步过程没有任何删除行为"。

场景开关（默认全部关闭）：
    drop_create_response      = True   -> 页面已创建但不回响应（模拟响应丢失）
    append_skip_at            = n      -> 第 n 次追加写入前即失败（模拟中断）
    append_write_then_fail_at = n      -> 第 n 次追加写入后才失败（模拟写成功回执丢）
    read_unknown_blocks       = True   -> 回读带 unknown_block_ids（模拟远端读不完整）
"""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

ROOT_PAGE_TITLE = "测试父页面"


class FakeNotionService:
    """假服务本体：状态在内存，测试结束后可丢弃。"""

    def __init__(self, expected_token: str = "fake-notion-token"):
        self.lock = threading.RLock()
        self.expected_token = expected_token
        self.pages: dict[str, dict] = {}      # page_id -> {title, markdown, parent}
        self.children: dict[str, list[str]] = {}
        self.root_page_id = "aaaaaaaa-0000-0000-0000-000000000001"
        with self.lock:
            self.pages[self.root_page_id] = {"title": ROOT_PAGE_TITLE, "markdown": "",
                                             "parent": None}
            self.children[self.root_page_id] = []
            self._seq = 0
        # 计数与事件
        self.create_calls = 0
        self.append_calls = 0
        self.request_tokens = []
        self.delete_calls = 0                 # 恒为 0：本服务没有删除接口
        # 场景开关
        self.drop_create_response = False
        self.append_skip_at = None
        self.append_write_then_fail_at = None
        self.read_unknown_blocks = False
        # 客户端
        self.server = None
        self.port = 0

    # ---------- 对外只读视图 ----------

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.port}/v1"

    def page_count(self) -> int:
        with self.lock:
            return sum(1 for p in self.pages.values() if p.get("parent"))

    def remote(self, page_id: str) -> str:
        with self.lock:
            return self.pages[page_id]["markdown"]

    def page_titles(self) -> list[str]:
        with self.lock:
            return [p["title"] for p in self.pages.values() if p.get("parent")]

    def created_page_ids(self) -> list[str]:
        with self.lock:
            return [pid for pid, p in self.pages.items() if p.get("parent")]

    def count_occurrences(self, needle: str) -> int:
        """所有页面正文里 needle 出现次数（用于断言"没有重复追加"）。"""
        with self.lock:
            return sum(page["markdown"].count(needle) for page in self.pages.values())

    # ---------- 内部状态操作 ----------

    def _new_page_id(self) -> str:
        with self.lock:
            self._seq += 1
            return f"bbbbbbbb-{self._seq:04d}-0000-0000-000000000000"

    def _create(self, parent_id: str, title: str, markdown: str) -> dict:
        page_id = self._new_page_id()
        with self.lock:
            self.pages[page_id] = {"title": title, "markdown": markdown, "parent": parent_id}
            self.children.setdefault(parent_id, []).append(page_id)
            self.create_calls += 1
        return {"id": page_id, "url": f"https://notion.so/fake/{page_id.replace('-', '')}"}

    def _append(self, page_id: str, content: str) -> None:
        with self.lock:
            self.pages[page_id]["markdown"] += "\n\n" + content

    def _children(self, parent_id: str) -> list[dict]:
        with self.lock:
            out = []
            for pid in self.children.get(parent_id, []):
                blk = self.pages[pid]
                out.append({"object": "block", "id": pid, "type": "child_page",
                            "child_page": {"title": blk["title"]}})
            return out

    # ---------- HTTP ----------

    def _handler_cls(self):
        svc = self

        class _Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, fmt, *a):        # 静默：假服务不记日志
                pass

            def _send_json(self, code: int, payload: dict) -> None:
                body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
                self.send_response(code)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def _auth_ok(self) -> bool:
                token = self.headers.get("Authorization", "")
                with svc.lock:
                    svc.request_tokens.append(token)
                return token == f"Bearer {svc.expected_token}"

            def _body(self) -> dict:
                length = int(self.headers.get("Content-Length") or 0)
                if not length:
                    return {}
                try:
                    return json.loads(self.rfile.read(length).decode("utf-8"))
                except json.JSONDecodeError:
                    return {}

            def do_POST(self):
                if not self._auth_ok():
                    self._send_json(401, {"object": "error", "status": 401, "code": "unauthorized"})
                    return
                if not self.path.startswith("/v1/pages"):
                    self._send_json(404, {"object": "error", "code": "not_found"})
                    return
                payload = self._body()
                parent_id = (payload.get("parent") or {}).get("page_id", "")
                title = ((payload.get("properties") or {}).get("title") or {}).get(
                    "title", [{}])
                title = (title[0].get("text", {}) if title else {}).get("content", "") or ""
                markdown = payload.get("markdown") or ""
                if parent_id not in svc.pages:
                    self._send_json(404, {"object": "error", "status": 404, "code": "not_found"})
                    return
                created = svc._create(parent_id, title, markdown)
                if svc.drop_create_response:
                    # 页面已建好，但不回任何响应体（客户端按结果未知处理）
                    self.close_connection = True
                    return
                self._send_json(200, {"object": "page", **created})

            def do_PATCH(self):
                if not self._auth_ok():
                    self._send_json(401, {"object": "error", "status": 401, "code": "unauthorized"})
                    return
                page_id = self.path.split("/v1/pages/", 1)[-1].split("/markdown", 1)[0]
                with svc.lock:
                    svc.append_calls += 1
                    n = svc.append_calls
                content = ""
                if page_id in svc.pages:
                    inserter = ((self._body().get("insert_content") or {}).get("content") or "")
                    content = inserter
                if svc.append_skip_at == n:
                    self._send_json(500, {"object": "error", "status": 500, "code": "internal"})
                    return
                if svc.append_write_then_fail_at == n:
                    # 远端确实写进去了，但响应体丢失（连接直接关闭）→ 客户端判定结果未知
                    svc._append(page_id, content)
                    self.close_connection = True
                    return
                svc._append(page_id, content)
                self._send_json(200, {"object": "page", "id": page_id})

            def do_GET(self):
                if not self._auth_ok():
                    self._send_json(401, {"object": "error", "status": 401, "code": "unauthorized"})
                    return
                if self.path.startswith("/v1/pages/") and self.path.endswith("/markdown"):
                    page_id = self.path.split("/v1/pages/", 1)[-1].split("/markdown", 1)[0]
                    if page_id not in svc.pages:
                        self._send_json(404, {"object": "error", "status": 404, "code": "not_found"})
                        return
                    payload = {"object": "page", "id": page_id,
                               "markdown": svc.pages[page_id]["markdown"]}
                    if svc.read_unknown_blocks:
                        payload["unknown_block_ids"] = ["cccccccc-0000-0000-0000-000000000001"]
                    self._send_json(200, payload)
                    return
                if self.path.startswith("/v1/blocks/") and self.path.endswith("/children"):
                    parent_id = self.path.split("/v1/blocks/", 1)[-1].split("/children", 1)[0]
                    if parent_id not in svc.pages:
                        self._send_json(404, {"object": "error", "status": 404, "code": "not_found"})
                        return
                    self._send_json(200, {"object": "list", "results": svc._children(parent_id)})
                    return
                self._send_json(404, {"object": "error", "code": "not_found"})

        return _Handler

    def start(self) -> "FakeNotionService":
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), self._handler_cls())
        self.port = self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        return self

    def stop(self) -> None:
        if self.server is not None:
            self.server.shutdown()
            self.server.server_close()
            self.server = None


class FakeNotionHarness:
    """上下文管理器：起停假服务。"""

    def __enter__(self) -> FakeNotionService:
        return FakeNotionService().start()

    def __exit__(self, *exc) -> None:
        self.stop()
