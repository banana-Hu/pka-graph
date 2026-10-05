"""收件箱监听：实时检测新文件 → 模型分析 → 生成知识建议。

用法（用户视角）：把 PDF / Markdown / 纯文本拖进
  个人学习状态/inbox/
守护进程（服务进程内线程，非新守护）轮询该目录，发现新文件后：
  1. 稳定性检测（大小连续两次扫描不变才处理，避免读到半写文件）
  2. 内容哈希去重：只有**已成功处理**（已出建议/材料不足/确认重复）的内容才拦截；
     待处理与失败的内容保持可重试，不会被误判为重复
  3. 提取正文（PDF 用 PyMuPDF；MD/TXT 直读）
  4. 调用发现模块语义分析器 → 结构校验 → 生成待审核建议（进现有「发现」队列）
  5. 登记处理结果（含失败原因、重试次数与下次重试时间）

重试语义：
- 未配置模型 / 预算耗尽 → pending_analysis，每轮检查，配置或预算恢复后自动继续；
- 读取失败 / 模型失败 → failed，按退避间隔自动重试（5 分钟起、封顶 1 小时）；
- 同一路径内容哈希变化 → 视为新内容重新分析（旧建议保留待人审）；
- 与其他入口的待审建议内容相同 → 只登记不重复分析、不重复消耗预算。

边界：
- 只读收件箱文件，不移动、不删除、不修改（处理记录在 inbox_registry.json）。
- 未配置模型时不假装分析：只登记为 pending_analysis，不生成建议。
- 尊重发现模块的每日模型调用预算；用尽后保留待处理，下轮/次日继续。
- 常驻线程随服务进程启停；服务不开则不监听（应用未运行时不宣称监控）。
"""

from __future__ import annotations

import hashlib
import json
import os
import secrets
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

SUPPORTED_EXT = (".pdf", ".md", ".txt")
DEFAULT_INTERVAL_S = 30
STABLE_SCANS_REQUIRED = 2      # 连续两次扫描大小不变才认为写完
PDF_MAX_PAGES = None          # 单文件完整读取，不设页数上限
PDF_MAX_CHARS = None          # 单文件完整读取，不设字符上限
MIN_TEXT_CHARS = 100           # 有效长度（CJK×2）低于此值：材料不足，不调模型
TERMINAL_STATUSES = ("suggested", "insufficient", "skipped")   # 成功终结：内容已处理完
RETRYABLE_STATUSES = ("pending_analysis", "failed")            # 可重试：不算已完成
RETRY_BACKOFF_START_S = 300    # 失败重试起始退避
RETRY_BACKOFF_MAX_S = 3600     # 失败重试退避封顶


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _parse_ts(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(str(value))
    except ValueError:
        return datetime.min.replace(tzinfo=timezone.utc)
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _ts_after(now_iso: str, seconds: int) -> str:
    try:
        base = datetime.fromisoformat(now_iso)
    except ValueError:
        base = datetime.now(timezone.utc)
    if base.tzinfo is None:
        base = base.replace(tzinfo=timezone.utc)
    return (base + timedelta(seconds=seconds)).isoformat(timespec="seconds")


def _backoff_seconds(attempts: int) -> int:
    return min(RETRY_BACKOFF_START_S * (2 ** max(0, attempts - 1)), RETRY_BACKOFF_MAX_S)


def _effective_len(text: str) -> int:
    cjk = sum(1 for ch in text if "\u4e00" <= ch <= "\u9fff")
    return cjk * 2 + max(0, len(text) - cjk)


def read_pdf_text(path: Path, *, max_pages: int | None = PDF_MAX_PAGES,
                  max_chars: int | None = PDF_MAX_CHARS):
    """用 PyMuPDF 提取 PDF 文本。返回 (text, error, meta)；错误不抛，交由调用方登记。

    meta 如实报告读取范围：{total_pages, read_pages, truncated, reason}——
    达到页数/字符上限即 truncated=True，供下游内容准入判定（不因提取成功就当作完整）。
    """
    try:
        import fitz  # PyMuPDF
    except ImportError:
        return "", "PyMuPDF 未安装，无法读取 PDF", None
    try:
        doc = fitz.open(str(path))
    except Exception as exc:  # noqa: BLE001 —— 文件损坏等
        return "", f"PDF 打开失败：{type(exc).__name__}", None
    total_pages = int(doc.page_count or 0)
    parts, total = [], 0
    read_pages = 0
    truncated, reason = False, None
    try:
        for i, page in enumerate(doc):
            if max_pages is not None and i >= max_pages:
                truncated, reason = True, f"页数上限 {max_pages}"
                break
            text = page.get_text() or ""
            parts.append(text)
            read_pages += 1
            total += len(text)
            if max_chars is not None and total >= max_chars:
                truncated, reason = True, f"字符上限 {max_chars}"
                break
    finally:
        doc.close()
    joined = "\n".join(parts)[:max_chars]
    meta = {"total_pages": total_pages, "read_pages": read_pages,
            "truncated": truncated, "reason": reason}
    if not joined.strip():
        return "", "PDF 无可提取文本（可能是扫描件，需 OCR）", meta
    return joined, None, meta


def read_material(path: Path):
    """按扩展名提取文本。返回 (text, read_status, error, pdf_meta)。"""
    ext = path.suffix.lower()
    if ext == ".pdf":
        text, err, meta = read_pdf_text(path)
        if err:
            return "", "unreadable", err, meta
        return text, "pdf_extracted", None, meta
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        return "", "unreadable", f"读取失败：{type(exc).__name__}", None
    return text, "readable", None, None


class InboxWatcher:
    """服务进程内的收件箱轮询线程。"""

    def __init__(self, graph_service, *, analyzer=None, analyzer_budget=None,
                 interval_seconds: int | None = None, clock=_now, auto_accept: bool = False,
                 task_submitter=None):
        self.svc = graph_service
        self.analyzer = analyzer
        self.budget = analyzer_budget
        # task 模式下由主程序注入：收件箱只登记任务，不再自行 quick_scan + auto_accept；
        # 一个输入只能有一个调度所有者（协作约定 §6）。
        self.task_submitter = task_submitter
        self.clock = clock
        # 免逐条审核（2026-10-03 用户约定）：分析判定有知识增量即自动接受入库；
        # uncertain/insufficient/news_only/duplicate 不入库，只留流水记录。
        self.auto_accept = bool(auto_accept)
        self.inbox_dir = graph_service.store.state_path.parent / "inbox"
        self.registry_path = graph_service.store.state_path.parent / "inbox_registry.json"
        self.interval = interval_seconds or int(os.environ.get("PKA_INBOX_INTERVAL_S", DEFAULT_INTERVAL_S))
        self._sizes: dict[str, tuple[int, int]] = {}   # relpath -> (size, 连续不变次数)
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._loop_error: dict | None = None
        self.last_scan_at: str | None = None
        self.last_result: dict = {}

    # ---------- 注册表 ----------

    def _load_registry(self) -> dict:
        if not self.registry_path.exists():
            return {"processed": {}}
        try:
            data = json.loads(self.registry_path.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) and "processed" in data else {"processed": {}}
        except (json.JSONDecodeError, OSError):
            return {"processed": {}}

    def _save_registry(self, registry: dict) -> None:
        self.registry_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.registry_path.with_name(f".inbox_registry.json.tmp-{secrets.token_hex(4)}")
        tmp.write_text(json.dumps(registry, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(self.registry_path)

    def registry_view(self) -> dict:
        """处理记录只读视图（知识库入口客户端轮询每条状态用）。"""
        return self._load_registry()["processed"]

    # ---------- 客户端直接投递 ----------

    MAX_INGEST_BYTES = 0       # 0 = 单文件不设字节上限

    def ingest_bytes(self, name: str, data_b64: str) -> dict:
        """知识库入口客户端投递：校验 → 落盘收件箱 → 立即触发处理。

        与目录投放完全等价（同一套稳定性/去重/分析/自动入库语义）；区别只在
        文件由服务端一次性写完，落地后连续两轮扫描即通过稳定性检测，无需等 30s 轮询。
        """
        import base64
        import binascii

        from .models import ValidationError
        if not isinstance(name, str) or not isinstance(data_b64, str):
            raise ValidationError("一次只能投递一个文件，name/data_b64 必须是字符串")
        clean = Path(str(name or "")).name
        if not clean or clean in (".", "..") or clean != str(name or "").strip():
            raise ValidationError("文件名非法（须为纯文件名，不含路径）")
        if not clean.lower().endswith(SUPPORTED_EXT):
            raise ValidationError(f"仅支持 {'/'.join(SUPPORTED_EXT)} 文件")
        if len(clean) > 120:
            raise ValidationError("文件名过长（>120 字符）")
        try:
            raw = base64.b64decode(str(data_b64 or ""), validate=True)
        except (binascii.Error, ValueError) as exc:
            raise ValidationError("data_b64 不是合法 base64") from exc
        if not raw:
            raise ValidationError("文件内容为空")
        if self.MAX_INGEST_BYTES > 0 and len(raw) > self.MAX_INGEST_BYTES:
            raise ValidationError(f"文件超过上限（{self.MAX_INGEST_BYTES // (1024 * 1024)}MB）")

        target = self.inbox_dir / clean
        if target.exists():   # 同名不覆盖：加短随机后缀，避免与已登记内容混淆
            stem, suffix = Path(clean).stem, Path(clean).suffix
            clean = f"{stem}-{secrets.token_hex(3)}{suffix}"
            target = self.inbox_dir / clean
        self.svc.store.safe_write_path(str(target))
        self.inbox_dir.mkdir(parents=True, exist_ok=True)
        tmp = target.with_name(f".{clean}.tmp-{secrets.token_hex(4)}")
        tmp.write_bytes(raw)
        tmp.replace(target)

        rel = target.relative_to(self.inbox_dir).as_posix()
        if self.task_submitter is not None:
            # task 模式：只登记任务（上传只原子落地，提交只接收已落地单文件路径）
            sha = hashlib.sha256(raw).hexdigest()
            snapshot, error = self._submit_task(target, sha)
            with self._lock:
                registry = self._load_registry()
                if snapshot is not None:
                    registry["processed"][rel] = self._entry(
                        sha, str(snapshot.get("status") or "queued"),
                        f"已登记任务 {snapshot.get('task_id')}", task_id=snapshot.get("task_id"))
                    self._save_registry(registry)
                    return {"queued": rel, "status": "queued", "task_id": snapshot.get("task_id")}
                registry["processed"][rel] = self._entry(
                    sha, "failed", f"任务登记失败：{error}（扫描轮会自动重试登记）")
                self._save_registry(registry)
            return {"queued": rel, "status": "queued", "task_error": error}
        threading.Thread(target=self._quick_scan, daemon=True, name="inbox-quick").start()
        return {"queued": rel, "status": "queued"}

    def _quick_scan(self) -> None:
        """落地后连续两轮扫描：第二轮即通过稳定性检测开始处理。"""
        try:
            self.scan_once()
            self.scan_once()
        except Exception as exc:  # noqa: BLE001 —— 失败交给常规 30s 轮询兜底
            print(f"[inbox] 快速处理异常（{type(exc).__name__}）：{exc}", flush=True)

    # ---------- 扫描与处理 ----------

    def scan_once(self) -> dict:
        """扫描一轮：返回本轮处理摘要。可被测试直接调用。

        处理语义：只有成功处理过（suggested/insufficient/skipped）的内容算已完成；
        待处理与失败的内容不算完成——后续扫描自动重试（失败按退避，待处理每轮检查）；
        同一路径内容哈希变化视为新内容重新分析。
        """
        with self._lock, self.svc.material_lock:
            self.inbox_dir.mkdir(parents=True, exist_ok=True)
            registry = self._load_registry()
            processed = registry["processed"]
            success_hashes = {v.get("sha256") for v in processed.values()
                              if v.get("sha256") and v.get("status") in TERMINAL_STATUSES}
            summary = {"scanned": 0, "new": 0, "suggested": 0, "skipped": 0,
                       "insufficient": 0, "pending_analysis": 0, "failed": 0,
                       "retried": 0, "reanalyzed": 0}
            for path in sorted(self.inbox_dir.rglob("*")):
                # 跳过隐藏文件与说明类文件（_ 或 . 前缀）：说明不是待分析材料
                if not path.is_file() or path.name.startswith((".", "_")):
                    continue
                rel = path.relative_to(self.inbox_dir).as_posix()
                if path.suffix.lower() not in SUPPORTED_EXT:
                    if rel not in processed:
                        summary["skipped"] += 1
                    continue
                summary["scanned"] += 1
                size = path.stat().st_size
                prev = self._sizes.get(rel)
                stable = prev is not None and prev[0] == size and prev[1] + 1 >= STABLE_SCANS_REQUIRED
                self._sizes[rel] = (size, (prev[1] + 1) if prev and prev[0] == size else 1)
                if not stable:
                    continue  # 可能是半写文件，下一轮再看
                try:
                    sha = hashlib.sha256(path.read_bytes()).hexdigest()
                except OSError as exc:
                    processed[rel] = self._entry(None, "failed", f"读取失败：{type(exc).__name__}")
                    summary["failed"] += 1
                    continue
                prior = processed.get(rel)
                if prior and prior.get("status") in TERMINAL_STATUSES:
                    if prior.get("sha256") == sha:
                        continue  # 内容未变：已完成，不再处理
                    summary["reanalyzed"] += 1   # 内容已变化：按新内容重新分析
                elif prior and prior.get("status") in RETRYABLE_STATUSES:
                    not_before = prior.get("not_before")
                    if not_before and _parse_ts(not_before) > _parse_ts(self.clock()):
                        continue  # 失败退避等待中
                    summary["retried"] += 1      # 待处理/失败：自动重试
                else:
                    summary["new"] += 1          # 首次发现的文件
                outcome = self._process_file(path, rel, processed, success_hashes, sha, prior)
                summary[outcome] = summary.get(outcome, 0) + 1
            if self.task_submitter is not None:
                self._refresh_task_entries(processed)
            self._save_registry(registry)
            self.last_scan_at = self.clock()
            self.last_result = summary
            return summary

    def _entry(self, sha: str | None, status: str, note: str | None, **extra) -> dict:
        entry = {"sha256": sha, "at": self.clock(), "status": status}
        if note:
            entry["note"] = note
        entry.update(extra)
        return entry

    def _submit_task(self, path: Path, sha: str):
        """登记任务；返回 (任务快照|None, 错误说明|None)。登记失败不丢文件，扫描轮再试。"""
        try:
            return self.task_submitter(path, sha), None
        except Exception as exc:  # noqa: BLE001 —— 任务账本/授权问题不在这里冒充成功
            return None, f"{type(exc).__name__}: {exc}"[:200]

    def _refresh_task_entries(self, processed: dict) -> None:
        """任务模式下 registry 是任务快照的派生兼容视图，不另推进状态。"""
        workflow = getattr(self.svc, "workflow", None)
        store = getattr(workflow, "store", None)
        if store is None:
            return
        for entry in processed.values():
            task_id = entry.get("task_id")
            if not task_id:
                continue
            try:
                task = store.get(task_id)
            except Exception:  # noqa: BLE001 —— 账本不可读时不覆盖原记录
                continue
            if not task:
                continue
            entry["status"] = task["status"]
            entry["stage"] = task.get("stage")
            entry["task_updated_at"] = task.get("updated_at")
            error = task.get("error") or {}
            if error.get("message"):
                entry["note"] = str(error["message"])[:200]
            elif task.get("result"):
                entry["note"] = "任务结果：" + str(task["result"])[:200]

    def _task_process_file(self, path: Path, rel: str, processed: dict,
                           sha: str, prior: dict | None) -> str:
        """task 模式：内容去重 + 只登记任务；重试由任务接口负责，不在扫描轮重复烧模型。"""
        if prior and prior.get("task_id") and prior.get("sha256") == sha:
            return "skipped"   # 同一路径同一内容已登记任务
        for other in processed.values():
            if other.get("sha256") == sha and other.get("task_id"):
                processed[rel] = self._entry(
                    sha, other.get("status") or "queued",
                    f"与已登记任务内容相同（{other.get('task_id')}），未重复登记",
                    task_id=other.get("task_id"))
                return "skipped"
        snapshot, error = self._submit_task(path, sha)
        if snapshot is None:
            attempts = int((prior or {}).get("attempts") or 0) + 1
            processed[rel] = self._entry(
                sha, "failed", f"任务登记失败：{error}（第 {attempts} 次，将自动重试登记）",
                attempts=attempts, not_before=_ts_after(self.clock(), _backoff_seconds(attempts)))
            return "failed"
        processed[rel] = self._entry(sha, str(snapshot.get("status") or "queued"),
                                     f"已登记任务 {snapshot.get('task_id')}",
                                     task_id=snapshot.get("task_id"))
        return "queued"

    def _process_file(self, path: Path, rel: str, processed: dict,
                      success_hashes: set, sha: str, prior: dict | None) -> str:
        if self.task_submitter is not None:
            return self._task_process_file(path, rel, processed, sha, prior)
        # 内容与某个已成功处理的文件相同：只登记，不重复分析
        if sha in success_hashes:
            processed[rel] = self._entry(sha, "skipped", "内容与已成功处理的文件相同（去重）")
            return "skipped"
        text, read_status, err, pdf_meta = read_material(path)
        if err:
            attempts = int((prior or {}).get("attempts") or 0) + 1
            processed[rel] = self._entry(
                sha, "failed", f"{err}（第 {attempts} 次失败，将自动重试）",
                attempts=attempts, not_before=_ts_after(self.clock(), _backoff_seconds(attempts)))
            return "failed"
        pdf_partial = bool(pdf_meta and pdf_meta.get("truncated"))
        pdf_note = (f"PDF 部分读取：{pdf_meta.get('read_pages')}/{pdf_meta.get('total_pages')} 页，"
                    f"原因：{pdf_meta.get('reason')}" if pdf_partial else None)
        if _effective_len(text) < MIN_TEXT_CHARS:
            processed[rel] = self._entry(sha, "insufficient",
                                         "内容不足（可提取文本太短），未调用模型")
            success_hashes.add(sha)
            return "insufficient"
        if self.analyzer is None:
            processed[rel] = self._entry(sha, "pending_analysis",
                                         "未配置模型：已登记，配置并重启服务后自动继续")
            return "pending_analysis"
        if not self._budget_available():
            processed[rel] = self._entry(sha, "pending_analysis",
                                         "今日模型预算已用尽：保留待处理，预算恢复后自动继续")
            return "pending_analysis"

        contributed_by, declared_sources, body_text = self._split_provenance(text)
        # 与其他入口的待审建议内容相同：只登记，不重复分析、不重复消耗预算
        dup_of = self._find_same_content_suggestion(body_text)
        if dup_of:
            processed[rel] = self._entry(sha, "skipped",
                                         f"与其他入口的待审建议内容相同（{dup_of}），未重复分析")
            success_hashes.add(sha)
            return "skipped"

        suggestion_id, analyze_error, verdict = self._analyze_and_suggest(
            path, rel, sha, body_text, read_status, contributed_by, declared_sources)
        if suggestion_id:
            note = None
            if prior and prior.get("status") in TERMINAL_STATUSES:
                note = f"内容已变化，重新分析（原建议 {prior.get('suggestion_id')} 保留待人审）"
            entry = self._entry(sha, "suggested", note, suggestion_id=suggestion_id,
                                **({"pdf_partial": pdf_note} if pdf_note else {}))
            if self.auto_accept and self._auto_accept(suggestion_id):
                entry["auto_accepted"] = True
            processed[rel] = entry
            success_hashes.add(sha)
            return "suggested"
        attempts = int((prior or {}).get("attempts") or 0) + 1
        processed[rel] = self._entry(
            sha, "failed",
            f"模型分析失败：{analyze_error}（第 {attempts} 次，将自动重试）",
            attempts=attempts, not_before=_ts_after(self.clock(), _backoff_seconds(attempts)))
        return "failed"

    @staticmethod
    def _split_provenance(text: str):
        """解析投递溯源头部。返回 (contributed_by, origin_sources, body_text)。"""
        from .frontmatter import FrontmatterError, split_frontmatter
        try:
            meta, body_text = split_frontmatter(text)
        except FrontmatterError:
            return None, [], text
        contributed_by = str(meta.get("contributed_by") or "").strip()[:80] or None
        raw_declared = meta.get("origin_sources")
        declared_sources: list[str] = []
        if isinstance(raw_declared, str) and raw_declared.strip():
            declared_sources = [raw_declared.strip()[:200]]
        elif isinstance(raw_declared, list):
            declared_sources = [str(item).strip()[:200]
                                for item in raw_declared if str(item).strip()][:10]
        return contributed_by, declared_sources, body_text

    def _find_same_content_suggestion(self, body_text: str) -> str | None:
        """跨入口去重：已有待审/已接受建议与该正文完全相同时返回其 id。"""
        from .discovery import DiscoveryService
        disc = getattr(self.svc, "discovery", None) or DiscoveryService(
            self.svc, upstream=[], analyzer=self.analyzer, analyzer_budget=self.budget)
        body_sha = hashlib.sha256(body_text.encode("utf-8")).hexdigest()
        for s in disc._load_suggestions():
            if s.get("content_sha256") == body_sha and s.get("status") in ("pending", "accepted"):
                return s.get("id")
        return None

    def _auto_accept(self, suggestion_id: str) -> bool:
        """免逐条审核：判定有知识增量（new_node/supplement/revision）即直接入库。

        入库即正式笔记（{领域}/正式笔记/），节点仍 untested；失败不拦截投递，
        建议保留 pending 供稍后处理，异常打日志。
        """
        from .discovery import DiscoveryService
        disc = getattr(self.svc, "discovery", None)
        if disc is None:
            disc = DiscoveryService(self.svc, upstream=[],
                                    analyzer=self.analyzer, analyzer_budget=self.budget)
        sug = next((s for s in disc._load_suggestions() if s["id"] == suggestion_id), None)
        if sug is None or sug.get("verdict") not in ("new_node", "supplement", "revision"):
            return False
        try:
            disc.accept(None, suggestion_id, request_id=f"inbox-auto-{suggestion_id}",
                        strict=True)
            return True
        except Exception as exc:  # noqa: BLE001 —— 自动入库失败不丢建议、不中断扫描
            print(f"[inbox] 自动入库失败（{type(exc).__name__}）：{exc}", flush=True)
            return False

    # ---------- 模型分析 → 建议 ----------

    def _budget_available(self) -> bool:
        if self.budget is None:
            return True
        limit = int(getattr(self.budget, "daily_model_calls_limit", 0) or 0)
        if limit <= 0:
            return True  # 0 表示不限制（用户显式选择）
        used = self._used_today()
        return used < limit

    def _budget_state_path(self) -> Path:
        return self.svc.store.state_path.parent / "inbox_budget.json"

    def _used_today(self) -> int:
        p = self._budget_state_path()
        if not p.exists():
            return 0
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return 0
        today = datetime.now(timezone.utc).date().isoformat()
        return int(data.get("used", 0)) if data.get("date") == today else 0

    def _spend_budget(self) -> None:
        p = self._budget_state_path()
        today = datetime.now(timezone.utc).date().isoformat()
        used = self._used_today()
        p.write_text(json.dumps({"date": today, "used": used + 1}, ensure_ascii=False),
                     encoding="utf-8")

    def _analyze_and_suggest(self, path: Path, rel: str, file_sha: str, body_text: str,
                             read_status: str, contributed_by: str | None,
                             declared_sources: list[str], source_path_rel: str | None = None):
        from .discovery import (DiscoveryService, extract_contribution, classify_material,
                                novelty_flags, novelty_label, compare_with_vault)
        from .discovery_model import validate_semantic_output, SemanticModelError
        from .models import now_iso

        disc = getattr(self.svc, "discovery", None) or DiscoveryService(
            self.svc, upstream=[], analyzer=self.analyzer, analyzer_budget=self.budget)
        title = path.stem
        material = {"title": title, "text": body_text}
        # source_path_rel：任务编排用冻结正文的库内相对路径，默认沿用收件箱路径
        sources = [{"upstream_id": f"inbox:{file_sha[:12]}", "source_name": "收件箱",
                    "url": None, "path": source_path_rel or f"个人学习状态/inbox/{rel}",
                    "read_status": read_status, "published_at": None}]
        for i, item in enumerate(declared_sources):
            sources.append({"upstream_id": f"declared:{file_sha[:12]}:{i}", "source_name": item,
                            "url": None, "path": None,
                            "read_status": "agent_declared", "published_at": None})
        cand = {"title": title, "summary": body_text[:1500]}
        related = disc._related_nodes(self.svc.state(), disc._candidate_tokens(cand), set())
        try:
            self._spend_budget()
            raw = self.analyzer.analyze(material, sources, related, [])
            result = validate_semantic_output(raw, body_text, related)
        except SemanticModelError as exc:
            self.last_result = {"error": f"{exc.category}: {exc}"}
            return None, f"{exc.category}: {exc}", None
        except Exception as exc:  # noqa: BLE001 —— 网络/超时等；只记本机异常信息，不回显响应体
            self.last_result = {"error": type(exc).__name__}
            return None, f"{type(exc).__name__}: {exc}"[:120], None

        # 语义结论优先；同时保留确定性预筛作为对照
        material_info = classify_material(body_text)
        extraction = extract_contribution(material_info["text"], title)
        best_score = related[0]["score"] if related else 0.0
        novelty = novelty_flags(body_text, True, best_score, None)
        novelty["first_collected_at"] = now_iso()

        suggestion = {
            "id": "sug-inbox-" + secrets.token_hex(6),
            "created_at": now_iso(),
            "status": "pending",
            "verdict": result["classification"],
            "name": title[:80],
            "contributed_by": contributed_by,
            "content_sha256": hashlib.sha256(body_text.encode("utf-8")).hexdigest(),
            "novelty": novelty,
            "novelty_label": novelty_label(novelty),
            "contribution": result["contribution"],
            "related": related,
            "sources": sources,
            "reason": ("投递方：" + contributed_by + "。" if contributed_by else "")
                      + "收件箱文件经模型语义分析（" + self.analyzer.describe() + "）",
            "material": {"level": "full_text" if read_status != "readable" else "readable_summary",
                         "text": body_text[:500], "note": f"来源：收件箱 {rel}"},
            "action": {"new_node": "新增候选节点", "supplement": "补充资料关联",
                       "revision": "保存更新提案", "duplicate": "不处理（重复）",
                       "news_only": "不处理（无知识增量）", "insufficient": "待核查",
                       "uncertain": "待核查（分析不确定）"}.get(result["classification"], "待核查"),
            "purse": None,
            "accepted": None,
            "semantic": {
                "differences_vs_related": result["differences_vs_related"],
                "source_excerpts": result["source_excerpts"],
                "unlocated_excerpts": result["unlocated_excerpts"],
                "excerpt_note": result["excerpt_note"],
                "unknowns": result["unknowns"],
                "validation_issues": result.get("validation_issues"),
            },
            "prescreen": {"verdict": compare_with_vault(extraction, title, material_info["text"], related)[0],
                          "extraction_quality": extraction["extraction_quality"]},
        }
        suggestion.pop("purse", None)
        suggestions = disc._load_suggestions()
        suggestions.append(suggestion)
        disc._save_suggestions(suggestions)
        return suggestion["id"], None, result["classification"]

    # ---------- 常驻线程 ----------

    def start(self) -> dict:
        if self._thread is not None:
            return {"inbox": "already_running", "dir": str(self.inbox_dir)}
        try:
            self.inbox_dir.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            return {"inbox": "error", "reason": str(exc)}

        def loop():
            errors = 0
            while not self._stop.wait(self.interval):
                try:
                    self.scan_once()
                    errors = 0
                    self._loop_error = None
                except Exception as exc:  # noqa: BLE001 —— 单轮异常不退出，但必须可见
                    errors += 1
                    self._loop_error = {"count": errors, "type": type(exc).__name__,
                                        "detail": str(exc)[:200], "at": self.clock()}
                    print(f"[inbox] 扫描异常（连续 {errors} 次）："
                          f"{type(exc).__name__}: {exc}", flush=True)

        self._thread = threading.Thread(target=loop, daemon=True, name="inbox-watcher")
        self._thread.start()
        return {"inbox": "started", "dir": str(self.inbox_dir), "interval_seconds": self.interval,
                "analyzer": self.analyzer.describe() if self.analyzer else "未配置（仅登记，不分析）"}

    def stop(self) -> None:
        self._stop.set()

    def status(self) -> dict:
        registry = self._load_registry()
        processed = registry["processed"]
        by_status: dict[str, int] = {}
        pending_items: list[dict] = []
        failure_items: list[dict] = []
        last_success_at: str | None = None
        for rel, v in processed.items():
            st = v.get("status", "?")
            by_status[st] = by_status.get(st, 0) + 1
            if st == "pending_analysis":
                pending_items.append({"path": rel, "at": v.get("at"), "note": v.get("note")})
            elif st == "failed":
                failure_items.append({"path": rel, "at": v.get("at"), "note": v.get("note"),
                                      "attempts": v.get("attempts"),
                                      "not_before": v.get("not_before")})
            elif st == "suggested":
                at = v.get("at")
                if at and (last_success_at is None or at > last_success_at):
                    last_success_at = at
        return {
            "dir": str(self.inbox_dir),
            "interval_seconds": self.interval,
            "running": self._thread is not None and self._thread.is_alive(),
            "auto_accept": self.auto_accept,
            "last_scan_at": self.last_scan_at,
            "last_success_at": last_success_at,
            "last_result": self.last_result,
            "loop_error": self._loop_error,
            "pending": pending_items[:50],
            "failures": failure_items[:50],
            "processed_total": len(processed),
            "by_status": by_status,
            "task_mode": self.task_submitter is not None,
            "analyzer": ("任务队列（任务模式下不再自行分析入库）" if self.task_submitter is not None
                         else (self.analyzer.describe() if self.analyzer else "未配置（仅登记，不分析）")),
            "used_today": self._used_today(),
            "daily_limit": int(getattr(self.budget, "daily_model_calls_limit", 0) or 0) if self.budget else 0,
            "note": "监听仅随服务进程运行；服务未启动时不监控。文件只读，不移动不删除。"
                    "待处理与失败的内容会自动重试，不会因去重被跳过。",
        }
