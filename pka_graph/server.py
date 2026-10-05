"""本地管理界面服务器（任务 6，第四阶段加固）。

- 仅监听 127.0.0.1 回环地址；用户手动启动，Ctrl+C 或 POST /api/shutdown 停止。
- 写防护（本地回环也不默认信任）：Host 限定本机；带 Origin 的跨站 POST 拒绝；
  全部写操作要求启动时生成的会话令牌（X-PKA-Token 头，仅内存、不进 URL）；
  POST body 必须 application/json。不开放任何 CORS 头。
- 静态文件只服务 web/index.html 一个页面；绝不把知识库目录整体映射为静态目录。
- 安全边界声明：本方案防的是"浏览器里的其他网页"借本机服务写入（DNS rebinding/
  CSRF）；对拥有本机任意执行权限的恶意程序不构成隔离——那需要进程级安全模型。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import secrets
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from .ext_access import build_ext_tools, ext_node, ext_search, ext_status
from pathlib import Path

from .models import ValidationError
from .quiz_service import QuizError
from .service import ConfirmationRequired, GraphService, NotFound
from .store import PathOutsideVault, StateStore, VersionConflict
from .workflow import Principal, WorkflowError, WorkflowService

WORKSPACE = Path(__file__).resolve().parents[1]
WEB_INDEX = WORKSPACE / "web" / "index.html"
# 真实知识库（只读扫描源）路径：环境变量 PKA_REAL_VAULT 显式提供，公开版不含私人路径。
REAL_VAULT = (Path(os.environ["PKA_REAL_VAULT"].strip())
              if os.environ.get("PKA_REAL_VAULT", "").strip() else None)

# ---------- 统一任务接口的身份与会话（协作约定 §3，ZCode 接线） ----------

OWNER_SUBJECT = "local:owner"
OWNER_SCOPES = frozenset({"tasks:submit", "tasks:read:self", "tasks:control:self",
                          "tasks:admin", "publish:notion"})
EXT_SUBJECT = "ext:readonly"          # 旧 PKA_EXT_TOKEN 只维持只读，不获得任务写权限
SESSION_COOKIE = "pka_session"
SESSION_HEADER = "X-PKA-Session"
TICKET_TTL_SECONDS = 60.0
SESSION_TTL_SECONDS = 12 * 3600


def task_pipeline_mode() -> str:
    mode = os.environ.get("PKA_TASK_PIPELINE", "legacy").strip().lower()
    return "task" if mode == "task" else "legacy"


class SessionStore:
    """一次性票据 → 会话。票据单次消费、60 秒有效；不提供匿名取票端点。"""

    def __init__(self, *, ticket_ttl: float = TICKET_TTL_SECONDS,
                 session_ttl: float = SESSION_TTL_SECONDS, clock=time.monotonic):
        self.ticket_ttl, self.session_ttl, self.clock = float(ticket_ttl), float(session_ttl), clock
        self._tickets: dict[str, float] = {}
        self._sessions: dict[str, tuple[str, frozenset, float]] = {}
        self._lock = threading.Lock()

    def issue(self) -> str:
        ticket = secrets.token_urlsafe(32)
        with self._lock:
            current = self.clock()
            self._tickets = {key: expiry for key, expiry in self._tickets.items()
                             if expiry >= current - 3600}
            self._sessions = {key: value for key, value in self._sessions.items()
                              if value[2] >= current}
            self._tickets[ticket] = self.clock() + self.ticket_ttl
        return ticket

    def valid(self, ticket: str) -> bool:
        with self._lock:
            expiry = self._tickets.get(str(ticket or ""))
            return bool(expiry and self.clock() <= expiry)

    def consume(self, ticket: str) -> tuple[str, str | None]:
        """返回 (状态, 会话令牌)；状态为 ok / expired / invalid（重放与非法同为 invalid）。"""
        with self._lock:
            expiry = self._tickets.pop(str(ticket or ""), None)
            if expiry is None:
                return "invalid", None
            if self.clock() > expiry:
                return "expired", None
            token = secrets.token_urlsafe(32)
            self._sessions[token] = (OWNER_SUBJECT, OWNER_SCOPES, self.clock() + self.session_ttl)
            return "ok", token

    def session(self, token: str) -> tuple[str, frozenset] | None:
        with self._lock:
            value = self._sessions.get(str(token or ""))
            if value is None:
                return None
            subject, scopes, expiry = value
            if self.clock() > expiry:
                self._sessions.pop(token, None)
                return None
            return subject, scopes

    def expires_at(self, token: str) -> str | None:
        with self._lock:
            value = self._sessions.get(str(token or ""))
        return None if value is None else _utc_iso(time.time() + max(0, value[2] - self.clock()))


def _utc_iso(expiry: float) -> str:
    from datetime import datetime, timezone
    return datetime.fromtimestamp(max(0.0, expiry), timezone.utc).isoformat()


class DesktopTicketChannel:
    """桌面入口的受控认证通道：服务在管理区放一枚单次票据，供本机桌面客户端交换。

    - 浏览器拿不到文件系统，因此不能像读 /api/meta 那样直接取到票据；
    - 同 Windows 用户下的任意进程本就不在信任边界内（协作约定 §3 已声明该限制）；
    - 票据单次消费、60 秒有效，被消费或过期后由服务自动换一枚新的，旧票作废。
    """

    def __init__(self, path: Path, store: SessionStore):
        self.path = Path(path)
        self.store = store
        self._lock = threading.Lock()

    def read(self) -> str:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return ""
        if not isinstance(data, dict):
            return ""
        ticket = str(data.get("ticket") or "")
        return ticket if self.store.valid(ticket) else ""

    def ensure_fresh(self) -> str:
        """文件里没有有效票据时换新；不返回票据内容，只保证文件可用。"""
        with self._lock:
            current = self.read()
            if current:
                return current
            ticket = self.store.issue()
            self.path.parent.mkdir(parents=True, exist_ok=True)
            temporary = self.path.with_name(self.path.name + ".tmp")
            temporary.write_text(json.dumps(
                {"ticket": ticket, "issued_at": _utc_iso(time.time()),
                 "ttl_seconds": TICKET_TTL_SECONDS,
                 "note": "单次消费；桌面客户端用 POST /api/session/exchange 交换会话"},
                ensure_ascii=False, indent=2), encoding="utf-8")
            os.replace(temporary, self.path)
            return ticket


def _authorized_input_roots(vault_root: Path) -> tuple[Path, ...]:
    """任务文件来源白名单：知识库根目录 + 显式配置的输入目录（逗号/分号分隔绝对路径）。"""
    roots = [Path(vault_root).resolve()]
    for item in os.environ.get("PKA_TASK_INPUT_ROOTS", "").replace(";", ",").split(","):
        candidate = item.strip()
        if not candidate:
            continue
        path = Path(candidate)
        if path.is_absolute() and path.is_dir():
            roots.append(path.resolve())
    return tuple(roots)


def _build_notion_core(workspace: Path):
    """统一 Notion 核心：配置存在才构造；不自动联网、不猜令牌。"""
    from .notion_sync import NotionSyncCore, SyncError, load_config
    config_path = Path(os.environ.get("PKA_NOTION_CONFIG") or (workspace / "notion_sync.local.json"))
    try:
        return NotionSyncCore(load_config(config_path))
    except SyncError:
        return None
    except Exception:  # noqa: BLE001 —— 配置损坏按未配置处理，不冒充可用
        return None


def _task_authorize(service: GraphService):
    """服务端授权回调：按当前配置复核路径、目标与外发权限（不用提交时的快照代替）。"""

    def authorize(principal, action, payload) -> None:
        from .workflow import WorkflowError
        scopes = set(getattr(principal, "scopes", frozenset()))
        task = payload if isinstance(payload, dict) else {}
        source = dict(task.get("source") or {})
        if action == "submit":
            if source.get("kind") == "file":
                path = Path(str(source.get("path") or "")).resolve()
                roots = _authorized_input_roots(service.test_vault)
                if not any(path.is_relative_to(root) for root in roots):
                    raise WorkflowError("permission_denied", "文件不在授权输入目录内", http_status=403)
            if str((task.get("options") or {}).get("publish_to") or "").strip() and "publish:notion" not in scopes:
                raise WorkflowError("permission_denied", "未获 Notion 发布授权", http_status=403)
            return
        if action in ("local_commit", "remote_write"):
            if action == "remote_write":
                alias = str((task.get("options") or {}).get("publish_to") or "").strip()
                if not alias or "publish:notion" not in scopes:
                    raise WorkflowError("permission_denied", "未获 Notion 发布授权", http_status=403)
                core = getattr(service, "notion_core", None)
                targets = set(((core.cfg.get("targets") if core is not None else {}) or {}).keys())
                if alias not in targets:
                    raise WorkflowError("config_error", "发布目标不在当前配置白名单")
            return
        if action == "execute":
            if source.get("kind") == "file":
                path = Path(str(source.get("path") or "")).resolve()
                roots = _authorized_input_roots(service.test_vault)
                if not any(path.is_relative_to(root) for root in roots):
                    raise WorkflowError("permission_denied", "来源目录授权已变化，停止执行", http_status=403)

    return authorize


def build_task_workflow(service: GraphService):
    """构造一次统一任务编排并挂到 svc.workflow；import 与构造都不启动线程。"""
    from . import workflow_adapters
    from .task_store import TaskStore
    from .workflow import WorkflowService

    state_dir = service.store.state_path.parent / "tasks" / "_ledger"
    store = TaskStore(state_dir)
    skills_dir = WORKSPACE / "skills"

    def reviewer_factory():
        from .note_review import build_note_reviewer_from_env
        return build_note_reviewer_from_env(skills_dir, service.store.state_path.parent)

    reviewer, reviewer_reason = reviewer_factory()
    service.discovery.note_reviewer = reviewer
    service.discovery.require_ai_review = True
    service.note_review_reason = reviewer_reason

    service.notion_core = _build_notion_core(WORKSPACE)
    adapters = workflow_adapters.build_adapters(
        service, reviewer_factory=reviewer_factory, notion_core=service.notion_core)
    service.adapter_inventory = adapters
    service.workflow = WorkflowService(
        store, adapters=adapters, pipelines={"note": workflow_adapters.NOTE_PIPELINE},
        authorize=_task_authorize(service))
    return service.workflow


def _task_submitter(service: GraphService):
    """收件箱与上传入口的单一调度入口：只登记任务，不在旧链路里再跑一遍模型。"""

    def submit_file(path: Path, sha256: str) -> dict:
        from .workflow import Principal
        resolved = Path(path).resolve()
        principal = Principal(OWNER_SUBJECT, OWNER_SCOPES)
        # 同源重复不烧模型：同一冻结原文已有未失败任务时复用同一任务
        for task in service.workflow.store.all():
            if (task["options"].get("pipeline") == "note"
                    and (task["source"] or {}).get("sha256") == sha256
                    and task["status"] not in ("failed", "cancelled")):
                return service.workflow.get(task["task_id"], principal=principal)
        request = {"contract_version": 1,
                   "request_id": "inbox-" + hashlib.sha256(
                       f"{resolved}|{sha256}".encode("utf-8")).hexdigest()[:32],
                   "source": {"kind": "file", "path": str(resolved)},
                   "options": {"pipeline": "note", "publish_to": None}}
        return service.workflow.submit(request, principal=Principal(OWNER_SUBJECT, OWNER_SCOPES))

    return submit_file


def build_service(test_vault: Path, allow_real_vault: bool = False) -> GraphService:
    store = StateStore(Path(test_vault), allow_real_vault=allow_real_vault)
    service = GraphService(store, real_vault=REAL_VAULT)
    from .quiz_model import build_adapter_from_env
    from .quiz_service import QuizService
    from .review_service import ReviewService

    from .discovery import DiscoveryService
    from .discovery_model import build_discovery_analyzer_from_env
    from .ext_access import ext_node, ext_search, ext_status, build_ext_tools

    discovery_analyzer, discovery_budget, discovery_mode, discovery_ok, discovery_reason = \
        build_discovery_analyzer_from_env()
    from .note_processor import build_note_processor_from_env

    note_processor, _note_budget, _note_mode, _note_ok, _note_reason = \
        build_note_processor_from_env(skills_dir=WORKSPACE / "skills" / "note_processing",
                                      state_dir=store.state_path.parent)
    adapter, budget, mode, available, reason = build_adapter_from_env()
    service.quiz_adapter = adapter
    service.quiz_mode = mode
    service.quiz_available = available
    service.quiz_reason = reason
    service.review = ReviewService(service)
    service.quiz = QuizService(service, adapter, budget, mode, review=service.review)
    auto_accept = os.environ.get("PKA_AUTO_ACCEPT", "1").strip().lower() not in ("0", "false", "off")
    service.discovery = DiscoveryService(service, analyzer=discovery_analyzer,
                                         analyzer_budget=discovery_budget,
                                         analyzer_mode=discovery_mode,
                                         note_processor=note_processor,
                                         auto_accept=auto_accept)
    from .inbox import InboxWatcher

    service.inbox = InboxWatcher(service, analyzer=discovery_analyzer,
                                 analyzer_budget=discovery_budget,
                                 auto_accept=auto_accept)
    if adapter is None:
        service.quiz.unavailable_reason = reason

    # 统一任务编排：构造不启动线程；task 模式下才由 serve() 启动工作线程并接管收件箱
    service.task_mode = task_pipeline_mode()
    service.note_processor = note_processor
    build_task_workflow(service)
    if service.task_mode == "task":
        service.inbox.task_submitter = _task_submitter(service)
    return service


class ExclusiveHTTPServer(ThreadingHTTPServer):
    """禁用 SO_REUSEADDR：Windows 下默认允许两个实例绑同一端口（劫持行为），
    关闭后第二个实例绑定失败并优雅退出，保证单实例。"""
    allow_reuse_address = False


class Handler(BaseHTTPRequestHandler):
    service: GraphService
    server_ref: ThreadingHTTPServer
    token: str = ""
    port: int = 0
    sessions: "SessionStore | None" = None
    desktop_tickets: "DesktopTicketChannel | None" = None

    # ---------- 基础 ----------

    def log_message(self, fmt, *args):  # 安静模式
        pass

    def _json(self, code: int, payload) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _body(self) -> dict:
        length = int(self.headers.get("Content-Length") or 0)
        if length == 0:
            return {}
        ctype = (self.headers.get("Content-Type") or "").split(";")[0].strip().lower()
        if ctype != "application/json":
            raise ValidationError("POST body 必须是 application/json")
        try:
            return json.loads(self.rfile.read(length).decode("utf-8"))
        except json.JSONDecodeError as exc:
            raise ValidationError(f"请求体不是合法 JSON：{exc}")

    def _origin_guard(self) -> str | None:
        """Host/Origin 校验：回环 + 同源，任何写与凭据交换都先过这一关。"""
        host = (self.headers.get("Host") or "").lower()
        if host not in (f"127.0.0.1:{self.port}", f"localhost:{self.port}"):
            return f"Host 不被允许：{host!r}（仅限本机回环）"
        origin = self.headers.get("Origin")
        if origin and origin.rstrip("/") not in (
            f"http://127.0.0.1:{self.port}",
            f"http://localhost:{self.port}",
        ):
            return f"Origin 不被允许：{origin!r}"
        return None

    def _session_identity(self):
        """从 X-PKA-Session 头或 pka_session cookie 取会话；不接受 body 自报身份。"""
        token = self.headers.get(SESSION_HEADER) or ""
        if not token:
            for item in (self.headers.get("Cookie") or "").split(";"):
                name, _, value = item.strip().partition("=")
                if name == SESSION_COOKIE:
                    token = value
                    break
        store = self.sessions
        return store.session(token) if store is not None and token else None

    def _task_principal(self):
        """任务接口的调用者身份：已交换会话 → 主人；ext 只读令牌 → 无任务权限。"""
        from .workflow import Principal

        identity = self._session_identity()
        if identity is not None:
            subject, scopes = identity
            return Principal(subject, frozenset(scopes))
        ext_token = os.environ.get("PKA_EXT_TOKEN", "")
        presented = self.headers.get("Authorization", "").replace("Bearer ", "").strip()
        if ext_token and presented == ext_token:
            return Principal(EXT_SUBJECT, frozenset())
        return None

    def _require_task_principal(self):
        principal = self._task_principal()
        if principal is None:
            self._json(401, {"error": {"code": "unauthenticated",
                                       "message": "任务接口需要会话；请从主程序入口链接或桌面通道交换票据"}})
            return None
        return principal

    def _workflow(self):
        from .workflow import WorkflowError

        workflow = getattr(self.service, "workflow", None)
        if workflow is None:
            raise WorkflowError("not_supported", "任务编排未构造", http_status=400)
        return workflow

    def _require_task_mode(self):
        """legacy 模式不冒称已接入任务编排：明确返回 not_supported。"""
        from .workflow import WorkflowError

        if task_pipeline_mode() != "task":
            raise WorkflowError(
                "not_supported",
                "当前为 legacy 模式，任务编排未启用（设置 PKA_TASK_PIPELINE=task 后可用）",
                http_status=400)

    def _task_summary(self) -> dict:
        workflow = getattr(self.service, "workflow", None)
        if workflow is None:
            return {"available": False, "by_status": {}}
        try:
            tasks = workflow.store.all()
        except Exception:  # noqa: BLE001 —— 账本损坏不在这里冒充空状态
            return {"available": False, "by_status": {}, "note": "任务账本不可读，原文件已保留"}
        counts: dict[str, int] = {}
        for task in tasks:
            counts[task["status"]] = counts.get(task["status"], 0) + 1
        return {"available": True, "total": len(tasks), "by_status": counts, **workflow.status()}

    def _capabilities(self, principal) -> dict:
        """统一能力报告：核心结果 + 本文件的补充条目，缺什么如实说清。"""
        from . import workflow_adapters

        workflow = self._workflow()
        payload = workflow.capabilities(principal=principal)
        # 模式由服务端开关决定，legacy 模式不冒称任务编排已接入
        payload["pipeline_mode"] = task_pipeline_mode()
        adapters = getattr(self.service, "adapter_inventory", None)
        if adapters:
            missing = [name for name in ("analyze", "generate", "validate", "review", "commit", "sync")
                       if not adapters[name].available]
            for item in payload.get("capabilities", []):
                if item.get("name") == "note" and missing and not item.get("available"):
                    reason = "阶段适配器未就绪：" + "、".join(missing)
                    if set(missing) & {"analyze", "generate", "validate", "commit"}:
                        reason += "（等 Codex 的加工/提交分离接口）"
                    if "sync" in missing:
                        reason += "；sync 需先配置 Notion 目标别名"
                    item["reason"] = reason
            payload["capabilities"].extend(workflow_adapters.capability_items(
                adapters, notion_configured=getattr(self.service, "notion_core", None) is not None))
        return payload

    def _write_guard(self) -> str | None:
        """写操作防护；返回错误消息或 None。"""
        origin_error = self._origin_guard()
        if origin_error:
            return origin_error
        if self._session_identity() is not None:
            return None
        # legacy 模式保留旧会话令牌；task 模式只认票据交换出的会话
        if task_pipeline_mode() == "legacy" and self.token and self.headers.get("X-PKA-Token") == self.token:
            return None
        return "缺少有效会话（浏览器请从主程序入口链接打开，桌面请经受控通道交换票据）"

    # ---------- 路由 ----------

    def do_GET(self):
        svc = self.service
        try:
            if self.path in ("/", "/index.html"):
                body = WEB_INDEX.read_bytes()
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return
            if self.path == "/api/meta":
                svc = self.service
                mode = task_pipeline_mode()
                if mode == "task" and self.desktop_tickets is not None:
                    # 桌面入口需要票据文件时才换新；这里不返回票据本身（浏览器读不到文件）
                    self.desktop_tickets.ensure_fresh()
                self._json(
                    200,
                    {
                        "test_vault": str(svc.test_vault),
                        "real_vault_readonly": str(svc.real_vault) if svc.real_vault else None,
                        "real_vault_mode": bool(getattr(svc.store, "allow_real_vault", False)),
                        "banner": ("真实知识库（受控写入：仅管理区 + 已关联笔记状态字段）"
                                   if getattr(svc.store, "allow_real_vault", False)
                                   else "测试知识库，未连接正式写入"),
                        "backups": svc.store.list_backups(),
                        # 仅 legacy 模式保留明文写令牌；task 模式改走票据交换的会话
                        "token": self.token if mode == "legacy" else None,
                        "pipeline_mode": mode,
                        "auth": {"exchange": "/api/session/exchange", "cookie": SESSION_COOKIE,
                                 "header": SESSION_HEADER,
                                 "ticket_ttl_seconds": int(TICKET_TTL_SECONDS),
                                 "note": "task 模式的写操作需要会话：浏览器经主程序入口链接（#ticket=）交换，"
                                         "桌面经管理区票据受控通道交换"},
                        "tasks": self._task_summary(),
                        "quiz_adapter": svc.quiz_adapter.describe() if (getattr(svc, "quiz_adapter", None) and svc.quiz_available) else (svc.quiz_reason if hasattr(svc, "quiz_reason") else None),
                        "quiz_available": getattr(svc, "quiz_available", False),
                        "discovery_analysis": svc.discovery.analysis_status(),
                        "inbox": svc.inbox.status(),
                        "discovery_recover_hint": "如检测到中断残留，可调用 POST /api/discovery/recover",
                        "review_notice": "应用关闭或服务未运行时不会主动通知；再次打开后可看到积压事项。",
                    },
                )
                return
            if self.path == "/api/tasks/capabilities":
                # 必须先于 /api/tasks/{task_id} 匹配
                principal = self._require_task_principal()
                if principal is None:
                    return
                self._json(200, self._capabilities(principal))
                return
            if self.path.startswith("/api/tasks/"):
                from urllib.parse import urlparse

                task_id = urlparse(self.path).path[len("/api/tasks/"):].strip("/")
                principal = self._require_task_principal()
                if principal is None:
                    return
                if not task_id or "/" in task_id:
                    self._json(404, {"error": {"code": "not_found", "message": "任务不存在"}})
                    return
                self._json(200, self._workflow().get(task_id, principal=principal))
                return
            if self.path == "/api/state":
                state = self.service.state()
                state.pop("idempotency", None)
                self._json(200, state)
                return
            if self.path.startswith("/api/graph?") or self.path == "/api/graph":
                from urllib.parse import parse_qs, urlparse

                query = parse_qs(urlparse(self.path).query)
                self._json(
                    200,
                    svc.graph_view(
                        q=(query.get("q") or [""])[0],
                        domain=(query.get("domain") or [""])[0],
                        layer=(query.get("layer") or [""])[0],
                        status=(query.get("status") or [""])[0],
                        include_archived=(query.get("archived") or [""])[0] == "1",
                    ),
                )
                return
            if self.path.startswith("/api/note/"):
                link_id = self.path.rsplit("/", 1)[-1]
                self._json(200, svc.read_note_content(link_id))
                return
            if self.path == "/api/review/overview":
                self._json(200, svc.review.due_groups())
                return
            if self.path == "/api/inbox/status":
                self._json(200, svc.inbox.status())
                return
            if self.path == "/api/inbox/registry":
                self._json(200, {"processed": svc.inbox.registry_view()})
                return
            if self.path == "/api/discovery/status":
                self._json(200, dict(svc.discovery.config(),
                                     banner="待审核建议不代表用户已经了解或掌握"))
                return
            if self.path.startswith("/api/discovery/suggestions"):
                from urllib.parse import parse_qs, urlparse

                query = parse_qs(urlparse(self.path).query)
                self._json(200, svc.discovery.list_suggestions(
                    status=(query.get("status") or ["pending"])[0],
                    limit=int((query.get("limit") or ["20"])[0]),
                ))
                return
            if self.path == "/api/review/settings":
                self._json(200, svc.review.settings())
                return
            if self.path.startswith("/api/quiz/session/"):
                self._json(200, svc.quiz.view(self.path.rsplit("/", 1)[-1]))
                return
            if self.path.startswith("/api/nodes?") or self.path == "/api/nodes":
                from urllib.parse import parse_qs, urlparse

                query = parse_qs(urlparse(self.path).query)
                nodes = self.service.list_nodes(
                    name=(query.get("name") or [""])[0],
                    domain=(query.get("domain") or [""])[0],
                    status=(query.get("status") or [""])[0],
                    include_archived=(query.get("archived") or [""])[0] == "1",
                )
                self._json(200, {"nodes": nodes})
                return
            if self.path.startswith("/api/node/"):
                node_id = self.path.rsplit("/", 1)[-1]
                self._json(200, self.service.node_detail(node_id))
                return
            if self.path.startswith("/api/ext/"):
                ext_token = os.environ.get("PKA_EXT_TOKEN", "")
                if not ext_token:
                    self._json(403, {"error": "外部访问未启用（未设置 PKA_EXT_TOKEN）"})
                    return
                req_token = self.headers.get("Authorization", "").replace("Bearer ", "")
                if req_token != ext_token:
                    self._json(403, {"error": "外部访问令牌无效"})
                    return
                if self.path == "/api/ext/tools":
                    self._json(200, build_ext_tools(self.service))
                    return
                if self.path.startswith("/api/ext/search"):
                    from urllib.parse import parse_qs, urlparse as up

                    q = parse_qs(up(self.path).query).get("q", [""])[0]
                    self._json(200, ext_search(self.service, q))
                    return
                if self.path.startswith("/api/ext/node/"):
                    self._json(200, ext_node(self.service, self.path.rsplit("/", 1)[-1]))
                    return
                if self.path == "/api/ext/status":
                    self._json(200, ext_status(self.service))
                    return
                self._json(404, {"error": "未知外部端点"})
                return
            self._json(404, {"error": "未知路径"})
        except NotFound as exc:
            self._json(404, {"error": str(exc)})
        except QuizError as exc:
            self._json(409, {"error": str(exc)})
        except WorkflowError as exc:
            self._json(exc.http_status, exc.as_dict())
        except Exception as exc:  # noqa: BLE001
            self._json(500, {"error": f"{type(exc).__name__}: {exc}"})

    def _drain_body(self) -> None:
        """抽空请求体：提前返回（如守卫拒绝）时若不读 body，
        关闭带未读数据的连接在 Windows 上会发 RST，客户端可能读不到响应。"""
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            return
        if length > 0:
            try:
                self.rfile.read(min(length, 1_000_000))
            except OSError:
                pass

    def _exchange_session(self) -> None:
        """POST /api/session/exchange：一次性票据换会话。重放/非法 403，过期 410。"""
        origin_error = self._origin_guard()
        if origin_error:
            self._drain_body()
            self._json(403, {"error": {"code": "origin_denied", "message": origin_error}})
            return
        try:
            payload = self._body()
        except ValidationError as exc:
            self._json(400, {"error": {"code": "invalid_request", "message": str(exc)}})
            return
        ticket = str((payload or {}).get("ticket") or "")
        if self.sessions is None or not ticket:
            self._json(400, {"error": {"code": "invalid_request", "message": "缺少 ticket"}})
            return
        status, token = self.sessions.consume(ticket)
        if status == "invalid":
            self._json(403, {"error": {"code": "ticket_rejected",
                                       "message": "票据无效或已被使用，请重新从主程序获取入口链接"}})
            return
        if status == "expired":
            self._json(410, {"error": {"code": "ticket_expired",
                                       "message": "票据已过期（60 秒内有效），请重新从主程序获取入口链接"}})
            return
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Set-Cookie",
                         f"{SESSION_COOKIE}={token}; Path=/; HttpOnly; SameSite=Strict")
        body = json.dumps({"session_token": token,
                           "expires_at": self.sessions.expires_at(token),
                           "scopes": sorted(OWNER_SCOPES)}, ensure_ascii=False).encode("utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _task_post(self, path: str, payload: dict) -> bool:
        """返回 True 表示已处理该路径。"""
        from .workflow import WorkflowError

        if path == "/api/tasks":
            self._require_task_mode()
            principal = self._require_task_principal()
            if principal is None:
                return True
            self._json(202, self._workflow().submit(payload, principal=principal))
            return True
        if path.startswith("/api/tasks/"):
            from urllib.parse import urlparse

            parts = urlparse(path).path[len("/api/tasks/"):].strip("/").split("/")
            principal = self._require_task_principal()
            if principal is None:
                return True
            if len(parts) != 2 or not parts[0] or parts[1] not in ("cancel", "retry"):
                self._json(404, {"error": {"code": "not_found", "message": "未知任务操作"}})
                return True
            task_id, action = parts
            request_id = str((payload or {}).get("request_id") or "").strip()
            if not request_id or len(request_id) > 200:
                raise WorkflowError("invalid_request", "控制请求需要有效的 request_id")
            workflow = self._workflow()
            if action == "cancel":
                self._json(200, workflow.cancel(task_id, request_id=request_id, principal=principal))
            else:
                self._json(202, workflow.retry(task_id, request_id=request_id, principal=principal))
            return True
        return False

    def do_POST(self):
        try:
            if self.path == "/api/session/exchange":
                self._exchange_session()
                return
            if self.path == "/api/tasks" or self.path.startswith("/api/tasks/"):
                # 任务接口先走 Host/Origin，再由路由按会话给出 401/403 与统一错误体
                origin_error = self._origin_guard()
                if origin_error:
                    self._drain_body()
                    self._json(403, {"error": {"code": "origin_denied", "message": origin_error}})
                    return
                if self._task_post(self.path, self._body()):
                    return
            guard_error = self._write_guard()
            if guard_error:
                self._drain_body()
                self._json(403, {"error": guard_error})
                return
            payload = self._body()
            svc = self.service
            if self.path == "/api/shutdown":
                self._json(200, {"stopping": True})
                import threading

                threading.Thread(target=self.server_ref.shutdown, daemon=True).start()
                return
            if self.path == "/api/scan":
                self._json(200, svc.scan(payload.get("which", "test")))
                return
            if self.path == "/api/import/preview":
                self._json(
                    200,
                    svc.import_preview(
                        payload.get("paths", []),
                        payload.get("assignments"),
                        payload.get("which", "test"),
                    ),
                )
                return
            if self.path == "/api/import/apply":
                result = svc.import_apply(
                    payload.get("paths", []),
                    payload.get("assignments"),
                    payload.get("request_id"),
                    int(payload.get("base_version", 0)),
                )
                self._json(200, result)
                return
            if self.path == "/api/nodes/create":
                result = svc.create_node(
                    name=payload.get("name", ""),
                    domain=payload.get("domain", ""),
                    scope=payload.get("scope", ""),
                    aliases=payload.get("aliases"),
                    request_id=payload.get("request_id"),
                    base_version=payload.get("base_version"),
                )
                self._json(200, result)
                return
            if self.path == "/api/nodes/update":
                result = svc.update_node(
                    payload.get("node_id", ""),
                    payload.get("changes", {}),
                    confirm_scope_change=bool(payload.get("confirm_scope_change")),
                    request_id=payload.get("request_id"),
                    base_version=payload.get("base_version"),
                )
                self._json(200, result)
                return
            if self.path == "/api/relations/add":
                result = svc.add_relation(
                    payload.get("from_id", ""),
                    payload.get("to_id", ""),
                    payload.get("relation", ""),
                    request_id=payload.get("request_id"),
                    base_version=payload.get("base_version"),
                )
                self._json(200, result)
                return
            if self.path == "/api/relations/remove":
                result = svc.remove_relation(
                    payload.get("link_id", ""),
                    request_id=payload.get("request_id"),
                    base_version=payload.get("base_version"),
                )
                self._json(200, result)
                return
            if self.path == "/api/merge/preview":
                self._json(200, svc.merge_preview(payload.get("source_id", ""), payload.get("target_id", "")))
                return
            if self.path == "/api/merge/apply":
                result = svc.merge_nodes(
                    payload.get("source_id", ""),
                    payload.get("target_id", ""),
                    confirm_migrate_note_links=bool(payload.get("confirm_migrate_note_links", True)),
                    confirm_migrate_node_links=bool(payload.get("confirm_migrate_node_links", True)),
                    request_id=payload.get("request_id"),
                    base_version=payload.get("base_version"),
                )
                self._json(200, result)
                return
            if self.path == "/api/split/preview":
                self._json(200, svc.split_preview(payload.get("source_id", ""), payload.get("parts", [])))
                return
            if self.path == "/api/split/apply":
                result = svc.split_nodes(
                    payload.get("source_id", ""),
                    payload.get("parts", []),
                    payload.get("note_link_assignments") or {},
                    payload.get("relation_assignments") or {},
                    request_id=payload.get("request_id"),
                    base_version=payload.get("base_version"),
                )
                self._json(200, result)
                return
            if self.path == "/api/discovery/recover":
                self._json(200, svc.discovery.recover_intents())
                return
            if self.path == "/api/discovery/check":
                out = svc.discovery.check(request_id=payload.get("request_id"))
                self._json(200, out)
                return
            if self.path == "/api/discovery/suggestion/accept":
                out = svc.discovery.accept(
                    None,
                    payload.get("suggestion_id", ""),
                    edits=payload.get("edits") or {},
                    request_id=payload.get("request_id"),
                )
                self._json(200, out)
                return
            if self.path == "/api/discovery/suggestion/set-status":
                out = svc.discovery.set_status(payload.get("suggestion_id", ""),
                                               payload.get("status", ""))
                self._json(200, out)
                return
            if self.path == "/api/discovery/config":
                # 只加载一次 state：改动与保存必须作用于同一份副本
                # （此前两次 state() 拿到不同副本，配置改动被静默丢弃）
                state = self.service.state()
                section = svc.discovery._discovery_state(state)
                if "auto_check_minutes" in payload:
                    minutes = int(payload["auto_check_minutes"])
                    if minutes and minutes < 10:
                        raise ValidationError("周期检查最短 10 分钟（默认关闭）")
                    section["auto_check_minutes"] = minutes
                if "per_check_limit" in payload:
                    limit = int(payload["per_check_limit"])
                    if not 1 <= limit <= 50:
                        raise ValidationError("每批条数需在 1-50")
                    section["per_check_limit"] = limit
                self.service.store.save(state, base_version=state["version"])
                periodic = svc.discovery.start_periodic_if_configured()
                self._json(200, dict(svc.discovery.config(), periodic=periodic))
                return
            if self.path == "/api/quiz/start":
                self._json(200, svc.quiz.start(payload.get("node_id", ""), payload.get("kind", "mastery")))
                return
            if self.path == "/api/review/overview":
                self._json(200, svc.review.due_groups())
                return
            if self.path == "/api/review/settings":
                self._json(200, svc.review.settings())
                return
            if self.path == "/api/review/update-settings":
                self._json(200, svc.review.update_settings(
                    enabled=payload.get("enabled"),
                    intervals=payload.get("intervals"),
                    confirm=bool(payload.get("confirm")),
                    recalc=bool(payload.get("recalc")),
                    recalc_confirm=bool(payload.get("recalc_confirm")),
                ))
                return
            if self.path == "/api/review/recalc-preview":
                self._json(200, {"diff": svc.review.recalc_preview(payload.get("intervals"))})
                return
            if self.path == "/api/review/backfill-preview":
                self._json(200, svc.review.backfill_preview())
                return
            if self.path == "/api/review/backfill-apply":
                self._json(200, svc.review.backfill_apply(confirm=bool(payload.get("confirm"))))
                return
            if self.path == "/api/review/snooze":
                self._json(200, svc.review.snooze(payload.get("node_id", ""), float(payload.get("hours", 8))))
                return
            if self.path == "/api/review/pause":
                self._json(200, svc.review.set_paused(payload.get("node_id", ""), True))
                return
            if self.path == "/api/review/resume":
                self._json(200, svc.review.set_paused(payload.get("node_id", ""), False))
                return
            if self.path == "/api/quiz/answer":
                self._json(200, svc.quiz.answer(payload.get("session_id", ""), str(payload.get("answer", ""))))
                return
            if self.path == "/api/quiz/hint":
                self._json(200, svc.quiz.request_hint(payload.get("session_id", "")))
                return
            if self.path == "/api/quiz/renew":
                self._json(200, svc.quiz.renew_question(payload.get("session_id", "")))
                return
            if self.path == "/api/quiz/cancel":
                self._json(200, svc.quiz.cancel(payload.get("session_id", "")))
                return
            if self.path == "/api/quiz/confirm-notes":
                self._json(200, svc.quiz.confirm_note_content(payload.get("session_id", "")))
                return
            if self.path == "/api/nodes/archive":
                result = svc.archive_node(
                    payload.get("node_id", ""),
                    reason=payload.get("reason", "manual"),
                    request_id=payload.get("request_id"),
                    base_version=payload.get("base_version"),
                )
                self._json(200, result)
                return
            if self.path == "/api/nodes/restore":
                result = svc.restore_node(
                    payload.get("node_id", ""),
                    request_id=payload.get("request_id"),
                    base_version=payload.get("base_version"),
                )
                self._json(200, result)
                return
            if self.path == "/api/inbox/ingest":
                # 知识库入口客户端投递：文件写入收件箱并立即触发处理
                # （客户端已完整写完文件，服务端落地后连续两轮扫描即通过稳定性检测）
                if "files" in payload or "paths" in payload:
                    raise ValidationError("一次只能投递一个文件，多个文件请分别排队")
                out = svc.inbox.ingest_bytes(payload.get("name") or "", payload.get("data_b64") or "")
                self._json(200, out)
                return
            self._json(404, {"error": "未知路径"})
        except ConfirmationRequired as exc:
            self._json(428, {"error": "需要确认", "preview": exc.preview})
        except VersionConflict as exc:
            self._json(409, {"error": str(exc), "current_version": exc.current_version})
        except PathOutsideVault as exc:
            self._json(403, {"error": str(exc)})
        except NotFound as exc:
            self._json(404, {"error": str(exc)})
        except QuizError as exc:
            self._json(409, {"error": str(exc)})
        except ValidationError as exc:
            self._json(400, {"error": str(exc)})
        except WorkflowError as exc:
            self._json(exc.http_status, exc.as_dict())
        except Exception as exc:  # noqa: BLE001
            self._json(500, {"error": f"{type(exc).__name__}: {exc}"})


def serve(test_vault: Path, port: int = 8765, allow_real_vault: bool = False) -> None:
    service = build_service(Path(test_vault), allow_real_vault=allow_real_vault)
    try:
        service.state()  # 启动即验证/初始化状态文件
    except ValidationError as exc:
        print(f"[pka-graph] 拒绝启动：状态文件存在完整性问题，不会自动恢复后继续写入。{exc}")
        print("[pka-graph] 请人工检查后显式运行 store.recover()（损坏原件会被保留），或从 backups 手动还原。")
        return 1
    sessions = SessionStore()
    runtime_dir = service.store.state_path.parent / "runtime"   # <vault>/个人学习状态/runtime
    desktop_tickets = DesktopTicketChannel(runtime_dir / "desktop_ticket.json", sessions)
    handler = type("BoundHandler", (Handler,), {
        "service": service, "server_ref": None, "token": secrets.token_urlsafe(32),
        "port": int(port), "sessions": sessions, "desktop_tickets": desktop_tickets})
    try:
        server = ExclusiveHTTPServer(("127.0.0.1", port), handler)
    except OSError as exc:
        print(f"[pka-graph] 端口 {port} 不可用（可能已有实例在运行）：{exc}")
        print("[pka-graph] 停止旧实例（界面右上角「停止服务」/任务管理器）后重试，或使用 --port 换端口。")
        return 1
    handler.server_ref = server
    mode = service.task_mode
    browser_ticket = sessions.issue()
    entry_url = f"http://127.0.0.1:{port}/#ticket={browser_ticket}"
    if mode == "task":
        # 票据放 URL fragment：交换后浏览器立即移除，不进访问日志、不给其他页面看
        try:
            runtime_dir.mkdir(parents=True, exist_ok=True)
            (runtime_dir / "browser_entry.url").write_text(entry_url, encoding="utf-8")
        except OSError:
            pass
        desktop_tickets.ensure_fresh()
    print(f"[pka-graph] 管理界面已启动：http://127.0.0.1:{port}  （Ctrl+C 停止）")
    print(f"[pka-graph] 任务流水线模式：{mode}"
          + (f"；入口链接（含一次性票据，60 秒内有效）请用：{entry_url}" if mode == "task" else ""))
    if mode == "task" and os.environ.get("PKA_OPEN_BROWSER", "").strip() == "1":
        def open_entry():
            try:
                import webbrowser
                webbrowser.open(entry_url)
            except Exception:  # noqa: BLE001 —— 打不开浏览器不影响服务
                pass
        threading.Timer(1.0, open_entry).start()
    if getattr(service.store, "allow_real_vault", False):
        print(f"[pka-graph] 知识库（真实库受控模式）：{service.test_vault}")
        print("[pka-graph] 写入范围：仅 个人学习状态/ 管理区；笔记仅经状态镜像更新 mastery_status 字段")
    else:
        print(f"[pka-graph] 测试知识库：{service.test_vault}（唯一写入目标）")
        print(f"[pka-graph] 真实知识库只读扫描源：{service.real_vault}")
    adapter_desc = service.quiz_adapter.describe() if getattr(service, "quiz_adapter", None) else (
        f"不可用：{getattr(service, 'quiz_reason', '未配置')}")
    print(f"[pka-graph] 模型适配器：{adapter_desc}")
    periodic = service.discovery.start_periodic_if_configured()
    print(f"[pka-graph] 发现周期检查：{periodic.get('periodic', 'off')}"
          f"（分钟配置 {periodic.get('minutes', 0)}；仅服务进程内，非新守护）")
    workflow = getattr(service, "workflow", None)
    if mode == "task" and workflow is not None:
        try:
            workflow.start()
        except WorkflowError as exc:
            server.server_close()
            print(f"[pka-graph] 任务工作线程未启动：{exc}（同一知识库只允许一个任务执行者）")
            return 1
        # 中断任务的恢复由工作线程启动时核实（workflow._loop 内 recover），这里不重复宣称
        print("[pka-graph] 任务工作线程：已启动（启动时核实恢复记录；一次处理一个文件）")
    inbox_state = service.inbox.start()
    print(f"[pka-graph] 收件箱监听：{inbox_state.get('inbox')}"
          f"（{inbox_state.get('dir')}，每 {inbox_state.get('interval_seconds', '?')}s；"
          + ("登记任务队列" if mode == "task" else f"分析器：{inbox_state.get('analyzer', '?')}")
          + "）")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        service.inbox.stop()
        service.discovery.stop_periodic()
        if workflow is not None and mode == "task":
            stopped = workflow.stop(timeout=5.0)
            if stopped.get("stopped"):
                print("[pka-graph] 任务工作线程已停止")
            else:
                print("[pka-graph] 任务工作线程未能按时停止，仍持有："
                      + "、".join(stopped.get("remaining") or []) + "（未假称全部退出）")
        print("[pka-graph] 已停止")


def main() -> int:
    parser = argparse.ArgumentParser(description="pka-graph 第二阶段：节点管理本地界面（仅回环）")
    parser.add_argument("--vault", default=str(WORKSPACE / "test_vault"), help="知识库根目录")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--allow-real-vault", action="store_true",
                        help="受控开启真实知识库写入（仅管理区 + 已关联笔记的状态镜像）")
    args = parser.parse_args()
    return serve(Path(args.vault), args.port, allow_real_vault=args.allow_real_vault) or 0


if __name__ == "__main__":
    raise SystemExit(main())
