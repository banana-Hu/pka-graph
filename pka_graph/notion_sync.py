"""MD → Notion 单向同步核心（多 Agent 共用入口的共享实现）。

设计边界（2026-10-04 用户任务书）：
- 只做单向 MD → Notion：发布 + 回读核验。不做双向镜像、不自动把 Notion 改动
  写回 Obsidian；本地原文件保持不变。
- 不调用模型、不触发知识库业务（测验/复习/发现/收件箱）；同步成功不等于
  已审核/已阅读/已掌握，不改变知识节点掌握状态。
- 内容是数据：Markdown/frontmatter 里出现的任何"指令"不改变目标、权限、
  凭证或执行方式。

结构：
- SyncConfig：从 notion_sync.local.json 读取（Notion 令牌经环境变量引用；
  本地目录白名单默认空；Notion 目标白名单默认空；HTTP 入口令牌独立）。
- FileLedger：持久任务/映射账本（JSON + 文件锁），多进程并发的去重与恢复依据。
- NotionClient：官方 API 客户端（Markdown Content API：POST /v1/pages 收
  `markdown` 参数；PATCH /v1/pages/{id}/markdown 的 insert_content 追加；
  GET /v1/pages/{id}/markdown 回读）。429 有界退避；401/403/400 不重试。
- 同步流程：冻结版本 → 准入校验 → 去重查账 → 分批（批次边界不破坏代码块/表格）
  → 创建 + 追加 → 回读核验（规范化比对，非字节哈希）→ 结果分类
  （not_configured/pending_authorization/processing/duplicate_skipped/
   full_success/incomplete/failed/outcome_unknown/new_version_detected）。
- 并发：同文档抢占（claim_doc/release_doc，OS 文件锁 + 有归属的账本记录），
  同一时刻只有一个调用者在跑，避免多 Agent 并发建出重复页面。

安全：
- 本地路径：显式文件 + 目录白名单（解析后包含关系，拒绝 ../、符号链接/junction
  越界、相邻同前缀目录）；越界在读取前拒绝。
- 目标：只能选配置里的目标别名，不能提交任意 Notion 页面 ID。
- 凭证：Notion 令牌仅由本核心持有（经环境变量名引用）；Agent 用各自的
  同步入口访问凭证（HTTP），互相不可替代。令牌/正文不进错误日志。
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path
from uuid import uuid4

from .file_lock import FileMutex

NOTION_VERSION = "2022-06-05"
DEFAULT_BASE_URL = "https://api.notion.com/v1"   # API 根地址，内部路径不再重复补 /v1
MAX_BATCH_CHARS = 30000          # 单次请求的 markdown 字符预算（分批提交，不截断）
RATE_LIMIT_MAX_RETRIES = 3       # 429 有界退避
LEDGER_LOCK_TIMEOUT_S = 15

STATUS_NOT_CONFIGURED = "not_configured"
STATUS_PENDING_AUTH = "pending_authorization"
STATUS_PROCESSING = "processing"
STATUS_DUPLICATE = "duplicate_skipped"
STATUS_FULL = "full_success"
STATUS_INCOMPLETE = "incomplete"
STATUS_FAILED = "failed"
STATUS_UNKNOWN = "outcome_unknown"
STATUS_NEW_VERSION = "new_version_detected"


class SyncError(RuntimeError):
    """带类别的同步错误。消息只含类别与简述，不含令牌与正文。"""

    def __init__(self, message: str, *, category: str = "failed"):
        super().__init__(message)
        self.category = category


def _sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def load_config(config_path: Path) -> dict:
    """读取同步配置（notion_sync.local.json，gitignored）。

    必需字段：targets（Notion 目标别名白名单）、local_roots（本地目录白名单）。
    token_env 指定 Notion 令牌的环境变量名（默认 NOTION_API_TOKEN）；
    entry_token_env 指定 HTTP 入口访问令牌的环境变量名（默认 PKA_SYNC_TOKEN）。
    """
    path = Path(config_path)
    if not path.exists():
        raise SyncError(
            f"同步配置不存在：{path}（复制 notion_sync.example.json 为 "
            f"notion_sync.local.json 并填写 Notion 目标与目录白名单）",
            category=STATUS_NOT_CONFIGURED)
    try:
        cfg = json.loads(path.read_text(encoding="utf-8-sig"))
    except (json.JSONDecodeError, OSError) as exc:
        raise SyncError(f"同步配置无法读取：{type(exc).__name__}", category="invalid_input") from exc
    if not isinstance(cfg.get("targets"), dict) or not cfg["targets"]:
        raise SyncError("配置缺少 targets（Notion 目标别名白名单为空）",
                        category=STATUS_NOT_CONFIGURED)
    roots = cfg.get("local_roots") or []
    if not isinstance(roots, list) or not roots:
        raise SyncError("配置缺少 local_roots（本地目录白名单为空，默认不开放任何目录）",
                        category=STATUS_NOT_CONFIGURED)
    for alias, target in cfg["targets"].items():
        pid = str((target or {}).get("parent_page_id") or "").strip()
        # 占位符/随手填的页面 ID 一律按"未配置"处理，避免拿假 ID 打到真实 Notion
        if not re.fullmatch(r"[0-9a-fA-F]{8}-?[0-9a-fA-F]{4}-?[0-9a-fA-F]{4}-?"
                            r"[0-9a-fA-F]{4}-?[0-9a-fA-F]{12}", pid):
            raise SyncError(
                f"目标 {alias} 的 parent_page_id 不是合法 Notion 页面 ID"
                f"（应为 8-4-4-4-12 位十六进制）；请先到 Notion 复制真实页面 ID",
                category=STATUS_NOT_CONFIGURED)
        if not str(alias).strip():
            raise SyncError("目标别名为空", category=STATUS_NOT_CONFIGURED)
    for root in roots:
        if not Path(str(root)).is_absolute():
            raise SyncError(f"本地目录白名单必须是绝对路径：{root}",
                            category=STATUS_NOT_CONFIGURED)
    cfg.setdefault("token_env", "NOTION_API_TOKEN")
    cfg.setdefault("entry_token_env", "PKA_SYNC_TOKEN")
    cfg.setdefault("base_url", DEFAULT_BASE_URL)
    cfg.setdefault("state_dir", str(Path(config_path).parent / "notion_sync_data"))
    return cfg


def get_notion_token(cfg: dict) -> str:
    """Notion 令牌只经环境变量进入内存；缺失按未配置处理（不猜测、不找其他密钥）。"""
    token = os.environ.get(cfg["token_env"], "").strip()
    if not token:
        raise SyncError(
            f"Notion 令牌未配置：请设置环境变量 {cfg['token_env']}"
            f"（Integration Token，仅授权目标页面）",
            category=STATUS_NOT_CONFIGURED)
    return token


def resolve_within_roots(cfg: dict, source_path: str) -> Path:
    """把提交的文件路径解析到本地目录白名单内；越界/不存在/非 md 在读取前拒绝。

    - 解析后包含关系校验（relative_to），拒绝 ../、符号链接/junction、相邻同前缀目录；
    - 只接受明确指定的单个 .md 文件；"它在某个目录里"之外的推断一律拒绝。
    """
    if not source_path or not str(source_path).strip():
        raise SyncError("缺少 source_path（要同步的 MD 文件路径）", category="invalid_input")
    raw = Path(str(source_path))
    resolved = raw.resolve()
    for root in cfg["local_roots"]:
        root_resolved = Path(root).resolve()
        try:
            rel = resolved.relative_to(root_resolved)
        except ValueError:
            continue
        if resolved.suffix.lower() != ".md":
            raise SyncError(f"仅支持 .md 文件，得到：{resolved.suffix}", category="invalid_input")
        if not resolved.is_file():
            raise SyncError(f"文件不存在：{resolved}", category="invalid_input")
        return resolved
    raise SyncError(
        f"路径不在本地目录白名单内：{resolved}（白名单：{cfg['local_roots']}）",
        category="path_outside")


def parse_document(text: str) -> dict:
    """拆 frontmatter 与正文。frontmatter 只取必要信息，不整段进正文。"""
    fm: dict = {}
    body = text
    if text.startswith("---\n"):
        end = text.find("\n---\n", 3)
        if end > 0:
            raw_fm = text[4:end]
            body = text[end + 5:]
            for line in raw_fm.splitlines():
                if ":" in line and not line.startswith((" ", "-", "#")):
                    k, _, v = line.partition(":")
                    fm[k.strip()] = v.strip().strip("\"'")
    lines = body.splitlines()
    title = str(fm.get("title") or "").strip()
    body_start = 0
    for i, ln in enumerate(lines):
        if ln.strip():
            if ln.lstrip().startswith("# ") and not title:
                title = ln.lstrip()[2:].strip()
                body_start = i + 1
            break
    return {"frontmatter": fm, "body_md": "\n".join(lines[body_start:]).strip(),
            "title": title or "（未命名材料）"}


def split_markdown_batches(body_md: str, max_chars: int = MAX_BATCH_CHARS) -> list[str]:
    """把正文按**顶层边界**切成若干批，批次完整提交，不截断、不破坏结构。

    - 边界 = 顶层标题行 / 水平线 / 顶层空行分组；绝不从代码块围栏或表格行中间切开；
    - 单块超过 max_chars 的（如超长代码块）独立成批并如实告警由调用方记录；
    - 返回的各批拼接（以 \\n\\n 相连）语义上等于原文。
    """
    lines = body_md.splitlines()
    blocks: list[list[str]] = []
    cur: list[str] = []
    in_fence = False
    fence_marker = ""
    for ln in lines:
        stripped = ln.lstrip()
        m = re.match(r"^(`{3,}|~{3,})", stripped)
        if m:
            marker = m.group(1)[:3]
            if not in_fence:
                in_fence, fence_marker = True, marker
            elif marker == fence_marker:
                in_fence, fence_marker = False, ""
            cur.append(ln)
            continue
        if not in_fence and ln.strip() and (
                ln.startswith("#") or re.match(r"^(---|\*\*\*|___)\s*$", ln)):
            if cur:
                blocks.append(cur)
            cur = [ln]
            continue
        cur.append(ln)
    if cur:
        blocks.append(cur)

    batches: list[str] = []
    buf: list[str] = []
    size = 0
    for blk in blocks:
        blk_text = "\n".join(blk)
        if size + len(blk_text) > max_chars and buf:
            batches.append("\n\n".join(buf).strip())
            buf, size = [], 0
        buf.append(blk_text)
        size += len(blk_text) + 2
    if buf:
        batches.append("\n\n".join(buf).strip())
    return [b for b in batches if b.strip()]


def find_images_and_links(body_md: str, page_links: dict | None) -> tuple[list[str], list[str]]:
    """扫描本地图片引用与未解析双链/相对链接。返回 (图片缺失清单, 链接警告清单)。

    - 本地图片（D:\\... 或相对路径/[[嵌入]]）：首版不上传 → 缺失清单，结果标不完整；
    - http(s) 外链图片：放行但记提示（外链未核验）；
    - [[双链]]：仅当 page_links 显式映射时转换，否则保留原文并警告；
    - 相对链接：保留可读文字并警告（不扫描旧笔记补映射）。
    """
    image_missing: list[str] = []
    link_warnings: list[str] = []
    page_links = page_links or {}
    for m in re.finditer(r"!\[[^\]]*\]\(([^)]+)\)", body_md):
        ref = m.group(1).strip()
        if ref.startswith(("http://", "https://")):
            link_warnings.append(f"外链图片未核验：{ref[:80]}")
        else:
            image_missing.append(ref[:120])
    for m in re.finditer(r"!\[\[([^\]]+)\]\]", body_md):
        image_missing.append(f"Obsidian 嵌入：{m.group(1)[:120]}")
    for m in re.finditer(r"(?<!\!)\[\[([^\]|]+)(?:\|([^\]]+))?\]\]", body_md):
        name = m.group(1).strip()
        if name in page_links:
            continue
        link_warnings.append(f"未解析双链（保留原文）：[[{name[:60]}]]")
    for m in re.finditer(r"(?<!\!)\((?!https?://)([^)\s]+\.(?:md))\)", body_md):
        link_warnings.append(f"相对 MD 链接（保留原文，不做转换）：{m.group(1)[:80]}")
    return image_missing, link_warnings


class NotionClient:
    """Notion 官方 API 客户端（Markdown Content API）。

    - 429：按 Retry-After 有界退避重试；401/403/400：不重试，按类别上抛；
    - 超时/连接错误：抛 outcome_unknown（远端可能已写入，交由恢复流程核查）；
    - 错误消息只含状态码与类别，不含请求体与令牌。
    """

    def __init__(self, token: str, base_url: str = DEFAULT_BASE_URL,
                 version: str = NOTION_VERSION, timeout: float = 60.0):
        self.token = token
        self.base_url = base_url.rstrip("/")
        self.version = version
        self.timeout = timeout

    def _headers(self) -> dict:
        return {"Authorization": f"Bearer {self.token}",
                "Notion-Version": self.version,
                "Content-Type": "application/json"}

    def _request(self, method: str, path: str, payload: dict | None = None) -> dict:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8") if payload is not None else None
        req = urllib.request.Request(f"{self.base_url}{path}", data=body,
                                     headers=self._headers(), method=method)
        retries = 0
        while True:
            try:
                with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                    return json.loads(resp.read().decode("utf-8"))
            except urllib.error.HTTPError as exc:
                if exc.code == 429 and retries < RATE_LIMIT_MAX_RETRIES:
                    retry_after = float(exc.headers.get("Retry-After") or 2 ** retries)
                    time.sleep(min(retry_after, 30))
                    retries += 1
                    continue
                # Any write-side 5xx may arrive after the server committed.
                # The client cannot use a test server's knowledge of rollback.
                if 500 <= exc.code < 600 and method in ("POST", "PATCH"):
                    raise SyncError(
                        f"Notion API HTTP {exc.code}（网关错误，写入结果未知；先核查远端再重试）",
                        category=STATUS_UNKNOWN) from exc
                category = {401: "auth", 403: "forbidden"}.get(
                    exc.code, "validation" if exc.code == 400 else "remote_error")
                raise SyncError(f"Notion API HTTP {exc.code}（{category}，详情不转发）",
                                category=category) from exc
            except (urllib.error.URLError, TimeoutError, OSError) as exc:
                # 超时/连接错误 ≠ 远端没写入：结果未知，交由恢复流程核查
                raise SyncError(
                    f"Notion 请求结果未知（{type(exc).__name__}）；先核查远端再重试",
                    category=STATUS_UNKNOWN) from exc

    def create_page(self, parent_page_id: str, title: str, markdown: str,
                    type_label: str = "", source_note: str = "") -> dict:
        """创建页面（第一批 markdown）。标题经 properties 设置；来源行由调用方拼入正文。"""
        props = {"title": {"title": [{"text": {"content": title[:2000]}}]}}
        if type_label:
            props["类型"] = {"rich_text": [{"text": {"content": type_label}}]}
        payload = {"parent": {"page_id": parent_page_id},
                   "properties": props,
                   "markdown": markdown}
        data = self._request("POST", "/pages", payload)
        return {"page_id": data.get("id"), "url": data.get("url")}

    def append_markdown(self, page_id: str, markdown: str) -> dict:
        """向页面末尾追加 markdown（第 2..N 批）。"""
        payload = {"type": "insert_content",
                   "insert_content": {"content": markdown,
                                      "position": {"type": "end"}}}
        return self._request("PATCH", f"/pages/{page_id}/markdown", payload)

    def read_markdown(self, page_id: str) -> dict:
        """回读页面 markdown（核验用）。含 unknown_block_ids（Notion 截断/未知块）。"""
        return self._request("GET", f"/pages/{page_id}/markdown")

    def list_child_pages(self, parent_page_id: str) -> list[dict]:
        """列出父页面下的子页面（结果未知时的定位核查用）。"""
        data = self._request("GET", f"/blocks/{parent_page_id}/children")
        out = []
        for blk in data.get("results", []):
            if blk.get("type") == "child_page":
                out.append({"page_id": blk["id"], "title": blk.get("child_page", {}).get("title", "")})
        return out


class FileLedger:
    """持久任务/映射账本（JSON + 独占锁文件）。多进程并发安全；无外部依赖。"""

    def __init__(self, state_dir: Path):
        self.state_dir = Path(state_dir)
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self.path = self.state_dir / "notion_sync_ledger.json"
        self.lock_path = self.state_dir / "notion_sync_ledger.lock"
        self._mutex = FileMutex(self.lock_path, timeout=LEDGER_LOCK_TIMEOUT_S)

    def _acquire(self) -> None:
        try:
            self._mutex.acquire()
        except TimeoutError as exc:
            raise SyncError("账本锁获取超时", category="write_conflict") from exc

    def _release(self) -> None:
        self._mutex.release()

    def _load(self) -> dict:
        if not self.path.exists():
            return {"tasks": {}, "docs": {}}
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as exc:
            raise SyncError("同步账本损坏或无法读取；已停止写入，请修复原账本",
                            category="ledger_corrupt") from exc
        if not isinstance(data, dict) or any(
                not isinstance(data.get(key), dict) for key in ("tasks", "docs")) \
                or ("claims" in data and not isinstance(data["claims"], dict)) \
                or any(not isinstance(record, dict)
                       for key in ("tasks", "docs", "claims")
                       for record in data.get(key, {}).values()):
            raise SyncError("同步账本结构损坏；已停止写入，请修复原账本",
                            category="ledger_corrupt")
        data.setdefault("tasks", {})
        data.setdefault("docs", {})
        return data

    def _save(self, data: dict) -> None:
        tmp = self.path.with_name(f".ledger.tmp-{uuid4().hex}")
        try:
            with tmp.open("x", encoding="utf-8") as handle:
                handle.write(json.dumps(data, ensure_ascii=False, indent=1))
                handle.flush()
                os.fsync(handle.fileno())
            tmp.replace(self.path)
        finally:
            tmp.unlink(missing_ok=True)

    def update(self, mutate) -> None:
        """锁内读-改-写。mutate(data) 原地修改。"""
        self._acquire()
        try:
            data = self._load()
            mutate(data)
            self._save(data)
        finally:
            self._release()

    def read(self) -> dict:
        self._acquire()
        try:
            return self._load()
        finally:
            self._release()


class NotionSyncCore:
    """共享同步核心：CLI 与 HTTP 入口都调用这里（同一套权限/账本/去重/错误处理）。"""

    def __init__(self, config: dict, client=None):
        self.cfg = config
        self.client = client   # 测试可注入假客户端；None = 按配置真实创建
        self.ledger = FileLedger(Path(config["state_dir"]))
        self._claim_guard = threading.Lock()
        self._claims = {}

    def _client(self) -> NotionClient:
        if self.client is not None:
            return self.client
        return NotionClient(get_notion_token(self.cfg), self.cfg["base_url"])

    def _target(self, alias: str) -> dict:
        target = (self.cfg.get("targets") or {}).get(str(alias or "").strip())
        if target is None:
            raise SyncError(
                f"目标别名不在白名单：{alias!r}（可用：{sorted(self.cfg['targets'])}）",
                category="target_unknown")
        if not target.get("parent_page_id"):
            raise SyncError(f"目标 {alias} 缺少 parent_page_id", category="target_unknown")
        return target

    def _doc_key(self, alias: str, source_path: Path) -> str:
        return _sha256_text(f"{alias}|{source_path.as_posix()}")[:24]

    # ---------- preview ----------

    def preview(self, *, source_path: str, target_alias: str,
                doc_type: str = "source_material") -> dict:
        """预览：不写 Notion、不写账本。返回输入/目标/格式支持与预计动作。"""
        target = self._target(target_alias)
        if doc_type not in ("source_material", "candidate_note"):
            raise SyncError(f"doc_type 只能是 source_material|candidate_note，得到 {doc_type!r}",
                            category="invalid_input")
        resolved = resolve_within_roots(self.cfg, source_path)
        text = resolved.read_text(encoding="utf-8", errors="replace")
        doc = parse_document(text)
        batches = split_markdown_batches(doc["body_md"])
        images_missing, link_warnings = find_images_and_links(
            doc["body_md"], self.cfg.get("page_links"))
        return {
            "source": {"path": str(resolved), "size": len(text),
                       "sha256": _sha256_text(text)[:16],
                       "whitelisted": True},
            "target": {"alias": target_alias, "parent_page_id": target["parent_page_id"]},
            "title": doc["title"],
            "title_is_ascii": all(ord(c) < 128 for c in doc["title"]),
            "doc_type": doc_type,
            "batches": len(batches),
            "images_missing": images_missing,
            "link_warnings": link_warnings,
            "would_result": ("incomplete（含未上传图片）" if images_missing
                             else "full_success（回读核验通过后）"),
        }

    # ---------- sync ----------

    def sync(self, *, source_path: str, target_alias: str,
             doc_type: str = "source_material", agent_name: str = "",
             new_version: bool = False) -> dict:
        """同步一个完整 MD 文件到目标别名。返回结构化结果（不含正文）。"""
        if doc_type not in ("source_material", "candidate_note"):
            raise SyncError(f"doc_type 只能是 source_material|candidate_note，得到 {doc_type!r}",
                            category="invalid_input")
        # 准入校验与版本冻结：任何异常都转成结构化结果，不向上抛裸异常
        try:
            target = self._target(target_alias)
            resolved = resolve_within_roots(self.cfg, source_path)
            frozen_text = resolved.read_text(encoding="utf-8", errors="replace")
        except SyncError as exc:
            return _failure(None, "", exc)

        # 一次任务一个版本；中途文件变化在恢复点被检出
        content_sha = _sha256_text(frozen_text)
        doc_key = self._doc_key(target_alias, resolved)
        try:
            if not self.claim_doc(doc_key):
                return {"task_id": None, "doc_id": doc_key, "status": STATUS_PROCESSING,
                        "duplicate": False, "page_url": None, "page_id": None,
                        "warnings": [], "error": None,
                        "note": "同一文档正被另一个调用者处理中"}
        except SyncError as exc:
            return _failure(None, doc_key, exc)
        try:
            return self._sync_claimed(
                resolved=resolved, frozen_text=frozen_text, content_sha=content_sha,
                doc_key=doc_key, target=target, target_alias=target_alias,
                doc_type=doc_type, agent_name=agent_name, new_version=new_version)
        except SyncError as exc:
            return _failure(None, doc_key, exc)
        finally:
            try:
                self.release_doc(doc_key)
            except SyncError:
                # The OS lock is still released by release_doc. Never overwrite
                # a corrupt ledger just to remove its diagnostic claim record.
                pass

    def _sync_claimed(self, *, resolved, frozen_text, content_sha, doc_key,
                      target, target_alias, doc_type, agent_name, new_version):
        ledger = self.ledger.read()
        doc_record = ledger["docs"].get(doc_key)
        if doc_record and not new_version:
            if doc_record.get("content_sha") == content_sha \
                    and doc_record.get("status") == STATUS_FULL:
                return {"task_id": doc_record.get("task_id"), "doc_id": doc_key,
                        "status": STATUS_DUPLICATE, "duplicate": True,
                        "page_url": doc_record.get("url"),
                        "page_id": doc_record.get("page_id"),
                        "warnings": [], "error": None,
                        "note": "相同内容已同步至该目标：返回既有结果，未重复建页"}
            if doc_record.get("content_sha") != content_sha:
                return {"task_id": None, "doc_id": doc_key, "status": STATUS_NEW_VERSION,
                        "duplicate": False, "page_url": doc_record.get("url"),
                        "warnings": ["发现新版本：内容与上次同步不同；确认后以 new_version=true 创建新页面（旧版保留）"],
                        "error": None}
        version = (doc_record.get("version", 1) + 1) if (doc_record and new_version) else 1
        if doc_record and not new_version and doc_record.get("content_sha") == content_sha:
            version = doc_record.get("version", 1)

        doc = parse_document(frozen_text)
        images_missing, link_warnings = find_images_and_links(
            doc["body_md"], self.cfg.get("page_links"))
        batches = split_markdown_batches(doc["body_md"])
        previous = [t for t in ledger["tasks"].values()
                    if t.get("doc_key") == doc_key and t.get("content_sha") == content_sha
                    and t.get("version", 1) == version
                    and t.get("status") != STATUS_FULL]
        resumed = previous[-1] if previous else None
        task_id = resumed["task_id"] if resumed else "sync-" + str(uuid4())
        def mutate(data):
            if resumed:
                data["tasks"][task_id]["status"] = STATUS_PROCESSING
                data["tasks"][task_id]["updated_at"] = _now()
                return
            data["tasks"][task_id] = {
                "task_id": task_id, "agent_name": str(agent_name or "")[:60],
                "doc_key": doc_key, "source_path": str(resolved),
                "content_sha": content_sha, "target_alias": target_alias,
                "parent_page_id": target["parent_page_id"], "doc_type": doc_type,
                "version": version, "title": doc["title"],
                "status": STATUS_PROCESSING, "stage": "created",
                "batches": {"total": len(batches), "done": 0},
                "page_id": None, "url": None,
                "warnings": [], "error": None,
                "created_at": _now(), "updated_at": _now()}
            if version > 1:
                data["tasks"][task_id]["supersedes"] = doc_record.get("page_id")
        self.ledger.update(mutate)

        client = None
        try:
            client = self._client()   # 凭证缺失也走结构化返回，不向上抛裸异常
            return self._run_task(client, task_id=task_id, doc_key=doc_key,
                                  resolved=resolved, frozen_text=frozen_text,
                                  content_sha=content_sha, doc=doc, batches=batches,
                                  target=target, alias=target_alias, doc_type=doc_type,
                                  version=version, images_missing=images_missing,
                                  link_warnings=link_warnings, agent_name=agent_name,
                                  known_page_id=(None if new_version else
                                                 (doc_record.get("page_id") if doc_record else None)))
        except SyncError as exc:
            def mark_error(data):
                task = data["tasks"].get(task_id)
                if task:
                    task["error"] = {"category": exc.category, "message": str(exc)[:200]}
                    task["status"] = _status_from_category(exc.category)
                    if exc.category != STATUS_UNKNOWN:
                        task.pop("remote_pending", None)
                    task["updated_at"] = _now()
            self.ledger.update(mark_error)
            return _failure(task_id, doc_key, exc)

    def _run_task(self, client, *, task_id, doc_key, resolved, frozen_text, content_sha,
                  doc, batches, target, alias, doc_type, version, images_missing,
                  link_warnings, agent_name, known_page_id=None) -> dict:
        # 恢复点 1：来源版本复查（与冻结版本一致才继续）
        current = resolved.read_text(encoding="utf-8", errors="replace")
        if _sha256_text(current) != content_sha:
            raise SyncError("来源文件在处理期间发生变化：已停止；请重新提交以冻结新版本",
                            category="source_changed")

        task = next(t for t in self.ledger.read()["tasks"].values() if t["task_id"] == task_id)
        page_id = task.get("page_id") or known_page_id
        done = int(task.get("batches", {}).get("done", 0))
        title = doc["title"]
        source_line = (f"> 来源：{resolved.name} · 类型：{doc_type} · "
                       f"DocRef：{doc_key[:12]} · 同步：{self.clock_now()}")

        # Only this ledger's recorded page identity permits reading/appending.
        # An interrupted create with no page ID cannot safely be resolved by title.
        if not page_id and (task.get("remote_pending", {}).get("operation") == "create"
                            or (task.get("error") or {}).get("category") == STATUS_UNKNOWN):
            raise SyncError("创建结果未知且无已确认页面 ID；请核实后处理，未重复创建",
                            category=STATUS_UNKNOWN)
        if not page_id:
            first = source_line + "\n\n" + batches[0] if batches else source_line
            def mark_creating(data):
                t = data["tasks"][task_id]
                t["stage"] = "creating"
                t["remote_pending"] = {"operation": "create"}
                t["updated_at"] = _now()
            self.ledger.update(mark_creating)
            # 自定义属性（如「类型」）仅当目标显式声明 type_property 时才写，
            # 否则 Notion 父页面缺少该属性会直接 400。
            created = client.create_page(target["parent_page_id"], title, first,
                                         type_label=doc_type if target.get("type_property") else "")
            if not created.get("page_id"):
                raise SyncError("创建响应缺少页面 ID，结果未知；未重复创建",
                                category=STATUS_UNKNOWN)
            page_id, done = created["page_id"], 1

            def mark_created(data):
                t = data["tasks"][task_id]
                t["page_id"] = page_id
                t["url"] = created.get("url")
                t["stage"] = "created"
                t["batches"]["done"] = done
                t.pop("remote_pending", None)
                t["updated_at"] = _now()
            self.ledger.update(mark_created)
            task["url"] = created.get("url")   # 回填本地副本，保证最终结果带页面链接

        # 追加第 2..N 批（从账本记录的进度继续；重启不重复追加）
        for i in range(done, len(batches)):
            # 追加前先查远端：上次"结果未知"的追加可能其实已写入，跳过以免重复段落
            if self._already_appended(
                    client, page_id, batches[i], required_prefix="\n\n".join(batches[:i + 1])):
                def mark_batch_skip(data, i=i):
                    t = data["tasks"][task_id]
                    t["batches"]["done"] = i + 1
                    t["stage"] = f"batch_{i + 1}_already_remote"
                    t.pop("remote_pending", None)
                    t["updated_at"] = _now()
                self.ledger.update(mark_batch_skip)
                continue
            def mark_appending(data, i=i):
                t = data["tasks"][task_id]
                t["remote_pending"] = {"operation": "append", "batch_index": i}
                t["updated_at"] = _now()
            self.ledger.update(mark_appending)
            try:
                client.append_markdown(page_id, batches[i])
            except SyncError as exc:
                if exc.category == STATUS_UNKNOWN:
                    def mark_unknown(data):
                        t = data["tasks"][task_id]
                        t["stage"] = f"appended:{i}"
                        t["status"] = STATUS_UNKNOWN
                        t["error"] = {"category": STATUS_UNKNOWN,
                                      "message": f"第 {i + 1}/{len(batches)} 批追加结果未知；先核查再继续"}
                        t["updated_at"] = _now()
                    self.ledger.update(mark_unknown)
                    return {"task_id": task_id, "doc_id": doc_key,
                            "status": STATUS_UNKNOWN, "duplicate": False,
                            "page_url": None, "page_id": page_id,
                            "warnings": [f"第 {i + 1}/{len(batches)} 批追加结果未知"],
                            "error": {"category": STATUS_UNKNOWN,
                                      "message": "追加结果未知：请先核查远端页面再重试同步"}}
                raise

            def mark_batch(data, i=i):
                t = data["tasks"][task_id]
                t["batches"]["done"] = i + 1
                t["stage"] = f"appended:{i + 1}"
                t.pop("remote_pending", None)
                t["updated_at"] = _now()
            self.ledger.update(mark_batch)

        # 恢复点 3：写入完成后再查一次来源版本——处理期间被改动的，按冻结版本停止，
        # 不把新版本拼到旧内容后面（也不悄悄报成功）
        if _sha256_text(resolved.read_text(encoding="utf-8", errors="replace")) != content_sha:
            raise SyncError(
                "来源文件在同步过程中发生变化：已按冻结版本写入，未拼接新版本；"
                "请重新提交以冻结新版本", category="source_changed")

        # 回读核验（Notion 会规范化 Markdown：规范化文本比对，不做字节哈希）
        read_back = client.read_markdown(page_id)
        remote_md = read_back.get("markdown") or ""
        unknown_blocks = read_back.get("unknown_block_ids") or []
        verification = self._verify(doc["body_md"], batches, remote_md, unknown_blocks)

        # 未上传的本地附件/嵌入：内容不完整，即使远端回读通过也不能报完整成功
        if images_missing and verification["complete"]:
            verification.setdefault("problems", []).append(
                f"存在 N 处未上传的本地附件：{len(images_missing)}；远程页面不含这些图片")
            verification["complete"] = False
        status = STATUS_FULL if verification["complete"] else STATUS_INCOMPLETE
        url = task.get("url")

        def mark_final(data):
            t = data["tasks"][task_id]
            t["status"] = status
            t["stage"] = "verified"
            t["verification"] = verification
            t["warnings"] = images_missing + link_warnings
            t["updated_at"] = _now()
            if not t.get("url"):
                t["url"] = None
            doc_rec = data["docs"].setdefault(doc_key, {
                "content_sha": content_sha, "target_alias": alias,
                "version": version, "title": title,
                "task_id": task_id, "history": []})
            doc_rec["version"] = version          # 建新版时同步升级账本里的版本号
            doc_rec.update({"content_sha": content_sha, "page_id": page_id,
                             "url": t.get("url"), "synced_at": _now(),
                             "status": status, "task_id": task_id, "title": title})
            if version > 1:
                doc_rec["history"].append({"version": version - 1,
                                           "page_id": t.get("supersedes"),
                                           "replaced_at": _now()})
        self.ledger.update(mark_final)

        return {"task_id": task_id, "doc_id": doc_key, "status": status,
                "duplicate": False, "page_url": task.get("url"), "page_id": page_id,
                "target_alias": alias, "title": title, "doc_type": doc_type,
                "warnings": images_missing + link_warnings,
                "error": None if verification["complete"] else {
                    "category": STATUS_INCOMPLETE,
                    "message": "；".join(verification["problems"])[:300]}}

    def claim_doc(self, doc_key: str) -> bool:
        """Hold an OS document lock for the entire sync; never probe or kill PIDs."""
        with self._claim_guard:
            if doc_key in self._claims:
                return False
            lock_name = _sha256_text(doc_key)
            mutex = FileMutex(self.ledger.state_dir / "doc_locks" / (lock_name + ".lock"),
                              timeout=0)
            try:
                mutex.acquire()
            except TimeoutError:
                return False
            token = uuid4().hex
            try:
                def mutate(data):
                    data.setdefault("claims", {})[doc_key] = {
                        "owner": token, "created_at": _now()}
                self.ledger.update(mutate)
            except BaseException:
                mutex.release()
                raise
            self._claims[doc_key] = (token, mutex, threading.get_ident())
            return True

    def release_doc(self, doc_key: str) -> None:
        with self._claim_guard:
            claim = self._claims.get(doc_key)
            if not claim or claim[2] != threading.get_ident():
                return
            token, mutex, _ = claim
            try:
                def mutate(data):
                    claims = data.get("claims", {})
                    if (claims.get(doc_key) or {}).get("owner") == token:
                        claims.pop(doc_key, None)
                self.ledger.update(mutate)
            finally:
                self._claims.pop(doc_key, None)
                mutex.release()

    @staticmethod
    def _already_appended(client, page_id: str, batch: str,
                          *, required_prefix: str | None = None) -> bool:
        """远端是否已含该批内容（上次追加"结果未知"但实为已写入时跳过重发）。"""
        if not batch.strip():
            return True
        try:
            result = client.read_markdown(page_id)
            if result.get("unknown_block_ids"):
                raise SyncError("远端读取不完整，不能确认追加结果", category=STATUS_UNKNOWN)
            remote = result.get("markdown") or ""
        except SyncError as exc:
            raise SyncError("远端核查未完成；未重发追加请求",
                            category=STATUS_UNKNOWN) from exc
        def norm(s: str) -> str:
            s = re.sub(r"[#>*`~\[\]()!|-]", "", s)
            return re.sub(r"\s+", "", s)
        expected = norm(required_prefix if required_prefix is not None else batch)
        return bool(expected) and expected in norm(remote)

    @staticmethod
    def _verify(source_body: str, batches: list[str], remote_md: str,
                unknown_blocks: list[str]) -> dict:
        """回读核验（Notion 会规范化 Markdown，故用规范化文本比对而非字节哈希）。

        方法：
        1) 规范化：去 Markdown 标记符号（#/>/*/`/[-]）与全部空白后做子串包含比对；
        2) 标题必须出现；
        3) 每个批次的规范化文本必须完整出现（保证尾部超过 5000 字符的内容也在）；
        4) 代码围栏数量一致；
        5) unknown_block_ids 为空（Notion 截断/未知块 → 不算完整）。
        """
        def norm(s: str) -> str:
            s = re.sub(r"[#>*`~\[\]()!|-]", "", s)
            return re.sub(r"\s+", "", s)

        problems: list[str] = []
        remote_norm = norm(remote_md)
        if len(unknown_blocks):
            problems.append(f"远端存在未知块 {len(unknown_blocks)} 个（读取不完整）")
        remote_head = remote_md.splitlines()[0].strip() if remote_md.splitlines() else ""
        if not remote_head:
            problems.append("回读内容为空")
        for i, batch in enumerate(batches, 1):
            b_norm = norm(batch)
            if b_norm and b_norm not in remote_norm:
                problems.append(f"第 {i} 批内容未在回读中完整出现")
        if norm(source_body) and norm(source_body) not in remote_norm:
            problems.append("正文顺序或重复段落数量不一致")
        fences_source = len(re.findall(r"^```", source_body, flags=re.M))
        fences_remote = len(re.findall(r"^```", remote_md, flags=re.M))
        if fences_source != fences_remote:
            problems.append(f"代码块围栏数量不一致（源 {fences_source} / 回读 {fences_remote}）")
        return {"complete": not problems, "problems": problems,
                "method": "规范化文本包含比对 + 围栏计数 + unknown_block_ids 检查（非字节哈希）"}

    @staticmethod
    def clock_now() -> str:
        return _now()


def _failure(task_id: str | None, doc_id: str, exc: SyncError) -> dict:
    """统一的失败结果：只带类别与简述，不带令牌与正文。"""
    return {"task_id": task_id, "doc_id": doc_id,
            "status": _status_from_category(exc.category),
            "duplicate": False, "page_url": None, "page_id": None,
            "warnings": [], "error": {"category": exc.category, "message": str(exc)[:200]}}


def _status_from_category(category: str) -> str:
    return {"auth": STATUS_PENDING_AUTH, "forbidden": STATUS_PENDING_AUTH,
            "target_unknown": STATUS_PENDING_AUTH, "target_unauthorized": STATUS_PENDING_AUTH,
            STATUS_NOT_CONFIGURED: STATUS_NOT_CONFIGURED,
            STATUS_UNKNOWN: STATUS_UNKNOWN,
            "source_changed": STATUS_FAILED}.get(category, STATUS_FAILED)


def _now() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).isoformat(timespec="seconds")
