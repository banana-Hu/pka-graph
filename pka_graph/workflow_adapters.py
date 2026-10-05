"""ZCode 认领的阶段适配器：把已登记模块包成 StageAdapter（协作约定 §2、§6）。

边界：
- 只调用模块公共入口，不推进任务状态、不直接写任务账本；状态由 `workflow.py` 调度层推进。
- 外部依赖缺失（模型未配置、无转写 worker、未授权 Notion）时如实 blocked/not_supported，
  不伪装成 completed/skipped；缺模型不是「不适用」。
- 卡死/越界路径按 resolve 后包含关系校验，不用字符串前缀。

产物 kind 约定（跨阶段交接，对接方按此登记）：
- `source_file`：冻结原件（核心 acquire 登记）
- `source_text`：完整正文文本（extract 或 URL 抓取产出；审核以它为原文依据）
- `suggestion`：待加工建议（analyze 产出，需含 suggestion_id）
- `note_body`：生成后的笔记正文（generate 产出，供 validate/review/commit）
- `review_issues`：审核问题清单（review 产出，供生成器修正）
- `note`：已入库的正式笔记（commit 产出，供 sync 使用）

Codex 的加工/提交分离接口（`prepare_note` / `validate_prepared` / `commit_prepared`，
协作约定 §6）尚未落地时，对应适配器在 capabilities 中如实报告不可用、执行时返回
blocked(not_supported)；一旦 discovery 上出现同名方法即自动接通，无需改本文件。
"""

from __future__ import annotations

import hashlib
import inspect
import json
import os
from pathlib import Path

from .inbox import MIN_TEXT_CHARS, _effective_len, read_material
from .workflow import StageResult, WorkflowError

# 流水线：与协作约定 §5 固定阶段一致；review 位于 validate 与 commit 之间
NOTE_PIPELINE = ("acquire", "extract", "analyze", "generate", "validate", "review", "commit", "sync")

KIND_SOURCE_FILE = "source_file"
KIND_SOURCE_TEXT = "source_text"
KIND_SUGGESTION = "suggestion"
KIND_NOTE_BODY = "note_body"
KIND_REVIEW_ISSUES = "review_issues"
KIND_NOTE = "note"

# 任务账本里 effect_id 跨 attempt 稳定：只用产物版本，不含尝试次数
def _effect_id(prefix: str, marker: str) -> str:
    digest = hashlib.sha256(marker.encode("utf-8")).hexdigest()[:16]
    return f"{prefix}:{digest}"


def _relative(vault_root: Path, path: Path) -> str:
    resolved = Path(path).resolve()
    root = Path(vault_root).resolve()
    try:
        rel = resolved.relative_to(root)
    except ValueError as exc:
        raise WorkflowError("permission_denied", "产物不在知识库范围内", http_status=403) from exc
    return rel.as_posix()


def _resolve_artifact(context, rel: str) -> Path | None:
    candidate = Path(str(rel or ""))
    if not str(rel or "").strip() or candidate.is_absolute() or ".." in candidate.parts:
        raise WorkflowError("permission_denied", "产物路径越界", http_status=403)
    resolved = (context.vault_root / candidate).resolve()
    try:
        resolved.relative_to(context.vault_root)
    except ValueError as exc:
        raise WorkflowError("permission_denied", "产物路径越界", http_status=403) from exc
    return resolved if resolved.is_file() else None


def _latest_artifact(context, *kinds: str) -> dict | None:
    for artifact in reversed(context.artifacts):
        if artifact.get("kind") in kinds:
            return artifact
    return None


def _artifact_file(context, *kinds: str) -> tuple[dict | None, Path | None]:
    artifact = _latest_artifact(context, *kinds)
    if artifact is None:
        return None, None
    return artifact, _resolve_artifact(context, str(artifact.get("path") or ""))


def _register(context, path: Path, kind: str, *, complete: bool = True, **extra) -> dict:
    data = Path(path).read_bytes()
    artifact = {"kind": kind, "path": _relative(context.vault_root, path),
                "sha256": hashlib.sha256(data).hexdigest(), "complete": bool(complete)}
    artifact.update(extra)
    return artifact


def _write_work_file(context, name: str, text: str) -> Path:
    """写入工作区；按字节写，避免 Windows 换行转换让正文指纹与产物不一致。"""
    target = Path(context.work_dir) / name
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(target.name + ".tmp")
    temporary.write_bytes(text.encode("utf-8"))
    os.replace(temporary, target)
    return target


def _read_text(path: Path) -> str:
    return path.read_bytes().decode("utf-8", errors="replace")


def _suggestion_id(context) -> str:
    artifact = _latest_artifact(context, KIND_SUGGESTION)
    value = str((artifact or {}).get("suggestion_id") or context.source.get("suggestion_id") or "").strip()
    if not value:
        raise WorkflowError("source_missing", "缺少待加工建议（analyze 阶段未登记 suggestion 产物）")
    return value


def _map_exception(exc: Exception) -> StageResult:
    """把模块异常按协作约定 §7 映射为阶段结果；不把未知异常伪装成成功。"""
    category = str(getattr(exc, "category", "") or "")
    message = str(exc)[:300] or type(exc).__name__
    blocked = {
        "authentication": ("auth_error", "认证失败，需修复配置后重试"),
        "auth": ("auth_error", "认证失败，需修复配置后重试"),
        "http": ("config_error", "模型服务拒绝请求，需检查请求参数与配置"),
        "config": ("config_error", "配置不完整，需修复后重试"),
        "budget": ("config_error", "模型预算或额度不足，未自动重试"),
        "permission": ("permission_denied", "权限被拒绝"),
        "permission_denied": ("permission_denied", "权限被拒绝"),
        "target_unknown": ("config_error", "目标不在白名单"),
        "target_unauthorized": ("config_error", "目标未获授权"),
        "not_configured": ("config_error", "相关能力未配置"),
        "not_supported": ("not_supported", "该能力未实现或缺少依赖"),
        "source_changed": ("source_changed", "来源已变化，产物版本不再一致"),
        "source_missing": ("source_missing", "来源缺失"),
        "source_incomplete": ("source_missing", "来源提取未完成"),
        "frozen_source_missing": ("source_missing", "缺少冻结原文"),
        "context_limit": ("context_exceeded", "超出上下文，需继续分段合成"),
        "context_exceeded": ("context_exceeded", "超出上下文，需继续分段合成"),
        "checkpoint_corrupt": ("ledger_corrupt", "检查点损坏，保留原件并停止"),
        "ledger_corrupt": ("ledger_corrupt", "记录损坏，保留原件并停止"),
        "state_corrupt": ("state_corrupt", "状态损坏，保留原件并停止"),
    }
    if category == "cancelled":
        return StageResult("cancelled", error={"code": "cancelled", "message": "任务已取消", "retryable": False})
    if category in ("http_5xx", "network", "timeout", "fetch_error", "rate_limit", "rate_limited"):
        code = "rate_limited" if category in ("rate_limit", "rate_limited") else "network_error"
        return StageResult("failed", error={"code": code, "message": message, "retryable": True})
    if category in ("insufficient", "invalid_output", "incomplete_output", "not_publishable"):
        return StageResult("needs_review", error={"code": category, "message": message,
                                                  "retryable": False, "requires_confirmation": True})
    if category in ("remote_result_unknown",):
        return StageResult("unknown_result", error={"code": "remote_result_unknown", "message": message, "retryable": False})
    if category in blocked:
        code, hint = blocked[category]
        return StageResult("blocked", error={"code": code, "message": f"{hint}：{message}", "retryable": False})
    return StageResult("failed", error={"code": "internal_error",
                                        "message": "处理阶段异常，已有成果已保留", "retryable": False})


class AcquireAdapter:
    """URL 来源准备：只抓取使用者明确提交的公开链接；文件来源由核心冻结，不在这里重复。"""

    def __init__(self, service=None, *, fetcher=None, fetch_enabled=None):
        self.svc = service
        self._fetcher = fetcher
        self._fetch_enabled = fetch_enabled

    def describe(self) -> str:
        return "URL 来源准备：仅公开链接正文（不绕过登录/付费/内网限制）"

    @property
    def available(self) -> bool:
        return True

    def _enabled(self) -> bool:
        if self._fetch_enabled is not None:
            return bool(self._fetch_enabled())
        discovery = getattr(self.svc, "discovery", None)
        probe = getattr(discovery, "_remote_fetch_enabled", None)
        try:
            return bool(probe()) if callable(probe) else False
        except Exception:  # noqa: BLE001 —— 配置不可读按关闭处理，不静默放行抓取
            return False

    def _fetch(self, url: str) -> dict:
        if self._fetcher is not None:
            return self._fetcher(url)
        from .content_fetcher import fetch_public_url
        return fetch_public_url(url)

    def execute(self, context):
        source = dict(context.source or {})
        if source.get("kind") != "url":
            # 文件来源的冻结属于核心 acquire；走到这里只说明没有可抓取的 URL
            return StageResult("completed", result=None)
        url = str(source.get("url") or "").strip()
        if not url:
            return StageResult("blocked", error={"code": "invalid_request", "message": "链接来源缺少 url", "retryable": False})
        if not self._enabled():
            return StageResult("blocked", error={"code": "not_supported",
                                                 "message": "远程抓取未启用（需在发现配置中显式开启来源范围）",
                                                 "retryable": False})
        if context.cancel_requested():
            return StageResult("cancelled", error={"code": "cancelled", "message": "任务已取消", "retryable": False})
        try:
            fetched = self._fetch(url)
        except Exception as exc:  # noqa: BLE001 —— 网络类异常按有限重试处理
            return _map_exception(exc)
        status = str((fetched or {}).get("status") or "")
        text = str((fetched or {}).get("text") or "")
        if status == "ok" and text.strip():
            path = _write_work_file(context, "fetched.txt", text)
            artifact = _register(context, path, KIND_SOURCE_TEXT, complete=True,
                                 origin="url", url=url)
            context.report_progress(1, 1)
            return StageResult("completed", artifacts=(artifact,),
                               checkpoint={"url": url, "chars": len(text)})
        if status in ("fetch_error", "http_error"):
            retryable = "网络" in str(fetched.get("note") or "")
            return StageResult("failed", error={"code": "network_error",
                                                "message": str(fetched.get("note") or "抓取失败")[:200],
                                                "retryable": retryable})
        return StageResult("blocked", error={"code": "source_missing",
                                             "message": str((fetched or {}).get("note") or "抓取未取得正文")[:200],
                                             "retryable": False})


class ExtractAdapter:
    """正文提取：把任务冻结的原文完整提取为纯文本，不调用模型、不改原文件。

    提取结果登记为 `source_text` 产物；PDF 部分读取、正文过短等如实标注，
    不静默截断后冒充完整正文。
    """

    def __init__(self, service=None):
        self.svc = service

    def describe(self) -> str:
        return "正文提取：冻结原件 → 完整文本（PDF/Markdown/TXT）"

    @property
    def available(self) -> bool:
        return True

    def execute(self, context):
        pipeline = (context.options or {}).get("pipeline")
        if pipeline == "transcribe_then_note":
            return StageResult("blocked", error={"code": "not_supported",
                                                 "message": "未登记可用的转写 worker，视频/抖音来源不可用",
                                                 "retryable": False})
        if context.cancel_requested():
            return StageResult("cancelled", error={"code": "cancelled", "message": "任务已取消", "retryable": False})
        if _latest_artifact(context, KIND_SOURCE_TEXT) is not None:
            # URL 抓取已产出正文：不重复提取
            return StageResult("completed")
        artifact, path = _artifact_file(context, KIND_SOURCE_FILE, KIND_SOURCE_TEXT)
        if path is None:
            rel = (artifact or {}).get("path") or (context.source or {}).get("frozen_path")
            resolved = _resolve_artifact(context, str(rel or "")) if rel else None
            if resolved is None:
                return StageResult("blocked", error={"code": "source_missing",
                                                     "message": "缺少冻结原件，拒绝回读可变路径",
                                                     "retryable": False})
            path = resolved
        try:
            text, read_status, err, pdf_meta = read_material(path)
        except OSError:
            return StageResult("blocked", error={"code": "source_missing",
                                                 "message": "冻结原件不可读", "retryable": False})
        if err:
            return StageResult("blocked", error={"code": "source_missing",
                                                 "message": f"正文提取失败：{err}"[:200], "retryable": False})
        truncated = bool(pdf_meta and pdf_meta.get("truncated"))
        if not text.strip():
            return StageResult("needs_review", error={"code": "insufficient",
                                                      "message": "提取不到可用正文，未调用模型",
                                                      "retryable": False, "requires_confirmation": True})
        target = _write_work_file(context, "source.txt", text)
        if truncated:
            artifact = _register(context, target, KIND_SOURCE_TEXT, complete=False,
                                 read_status=read_status, pdf=pdf_meta)
            return StageResult("blocked", artifacts=(artifact,),
                               checkpoint={"chars": len(text), "truncated": True},
                               error={"code": "source_missing",
                                      "message": "PDF 仅部分读取，不能作为完整正文入库",
                                      "retryable": False})
        artifact = _register(context, target, KIND_SOURCE_TEXT, complete=True,
                             read_status=read_status)
        context.report_progress(1, 1)
        if _effective_len(text) < MIN_TEXT_CHARS:
            return StageResult("needs_review", artifacts=(artifact,),
                               checkpoint={"chars": len(text)},
                               error={"code": "insufficient",
                                      "message": "可提取正文过短，未达知识加工门槛",
                                      "retryable": False, "requires_confirmation": True})
        return StageResult("completed", artifacts=(artifact,), checkpoint={"chars": len(text)})


class _ProviderAdapter:
    """包装 Codex 提供的分离接口；接口未落地时如实报告不可用。"""

    stage_name = ""
    method_name = ""
    missing_reason = "等待 Codex 的加工/提交分离接口"

    def __init__(self, service=None, *, provider=None, missing_reason=None):
        self.svc = service
        self._provider = provider
        if missing_reason:
            self.missing_reason = missing_reason

    def provider(self):
        if self._provider is not None:
            return self._provider if callable(self._provider) else None
        discovery = getattr(self.svc, "discovery", None)
        candidate = getattr(discovery, self.method_name, None)
        return candidate if callable(candidate) else None

    @property
    def available(self) -> bool:
        return self.provider() is not None

    def unavailable_reason(self) -> str:
        return f"{self.method_name} 尚未提供（{self.missing_reason}）"

    def execute(self, context):
        provider = self.provider()
        if provider is None:
            return StageResult("blocked", error={"code": "not_supported",
                                                 "message": self.unavailable_reason(),
                                                 "retryable": False})
        if context.cancel_requested():
            return StageResult("cancelled", error={"code": "cancelled", "message": "任务已取消", "retryable": False})
        try:
            value = self._call(provider, context)
        except WorkflowError:
            raise
        except Exception as exc:  # noqa: BLE001 —— 按 §7 分类映射，未预期异常不自动重试
            return _map_exception(exc)
        return self._map_value(value, context)

    def _call(self, provider, context):  # pragma: no cover - 由子类实现
        raise NotImplementedError

    def _map_value(self, value, context) -> StageResult:
        # 只认两类返回值：StageResult 本体，或各阶段文档化的字典形状（由 _map_completed 解释）。
        # 不按通用键猜语义——例如 admission 里也有 "outcome" 字段，猜会把校验结果读成阶段结果。
        if isinstance(value, StageResult):
            return value
        if not isinstance(value, dict):
            return StageResult("failed", error={"code": "invalid_output",
                                                "message": "阶段返回结构无效", "retryable": False})
        return self._map_completed(value, context)

    def _map_completed(self, value, context) -> StageResult:  # pragma: no cover - 由子类实现
        raise NotImplementedError


class AnalyzeAdapter:
    """知识增量分析：从冻结正文产出待加工建议。

    复用收件箱既有分析链路（同一套分类、去重、预算与建议登记），只把输入换成
    任务冻结正文，不复制一套分析实现；缺分析模型时 blocked，不跳过也不冒充完成。
    """

    def __init__(self, service=None):
        self.svc = service

    def describe(self) -> str:
        analyzer = getattr(getattr(self.svc, "inbox", None), "analyzer", None)
        return f"知识增量分析：{analyzer.describe()}" if analyzer is not None else "未配置分析模型"

    @property
    def available(self) -> bool:
        return getattr(getattr(self.svc, "inbox", None), "analyzer", None) is not None

    def unavailable_reason(self) -> str:
        return "笔记分析模型未配置（PKA_DISCOVERY_ADAPTER=http 等），分析阶段阻塞而非跳过"

    def execute(self, context):
        inbox = getattr(self.svc, "inbox", None)
        if inbox is None or getattr(inbox, "analyzer", None) is None:
            return StageResult("blocked", error={"code": "config_error",
                                                 "message": "笔记分析模型未配置，分析阶段阻塞（不跳过）",
                                                 "retryable": False})
        if context.cancel_requested():
            return StageResult("cancelled", error={"code": "cancelled", "message": "任务已取消", "retryable": False})
        discovery = getattr(self.svc, "discovery", None)
        analyze = getattr(discovery, "analyze_frozen_material", None)
        if callable(analyze):
            try:
                value = analyze(context=context)
                if value.get("outcome"):
                    return StageResult(value["outcome"], artifacts=tuple(value.get("artifacts") or ()),
                                       error=value.get("error"), result=value.get("result"))
                return StageResult("completed", artifacts=tuple(value.get("artifacts") or ()),
                                   result={"suggestion_id": value["suggestion_id"]})
            except WorkflowError:
                raise
            except Exception as exc:
                return _map_exception(exc)
        artifact, path = _artifact_file(context, KIND_SOURCE_TEXT)
        if path is None:
            return StageResult("blocked", error={"code": "source_missing",
                                                 "message": "缺少冻结正文产物，不能分析", "retryable": False})
        text = _read_text(path)
        contributed_by, declared_sources, body_text = inbox._split_provenance(text)
        existing = inbox._find_same_content_suggestion(body_text)
        if existing:
            return StageResult("needs_review", result={"disposition": "duplicate", "suggestion_id": existing},
                               error={"code": "insufficient",
                                      "message": f"内容与已有建议相同（{existing}），未重复分析、不重复烧模型",
                                      "retryable": False})
        if not inbox._budget_available():
            return StageResult("blocked", error={"code": "config_error",
                                                 "message": "今日模型预算已用尽，保留材料等待恢复",
                                                 "retryable": False})
        try:
            suggestion_id, error, verdict = inbox._analyze_and_suggest(
                path=path, rel=Path(str(artifact.get("path") or path.name)).name,
                file_sha=str(artifact.get("sha256") or ""), body_text=body_text,
                read_status=str(artifact.get("read_status") or "readable"),
                contributed_by=contributed_by, declared_sources=declared_sources,
                source_path_rel=str(artifact.get("path") or ""))
        except Exception as exc:  # noqa: BLE001 —— 分析异常按 §7 分类，未知异常不自动重试
            return _map_exception(exc)
        if not suggestion_id:
            detail = str(error or "分析未返回建议")
            retryable = any(token in detail.lower() for token in ("http", "timeout", "network", "超时"))
            return StageResult("failed" if retryable else "blocked",
                               error={"code": "network_error" if retryable else "config_error",
                                      "message": f"知识增量分析失败：{detail}"[:200],
                                      "retryable": retryable})
        snapshot_path = _write_work_file(context, "suggestion.json", json.dumps(
            {"suggestion_id": suggestion_id, "verdict": verdict,
             "contributed_by": contributed_by, "declared_sources": declared_sources},
            ensure_ascii=False, indent=2))
        suggestion_artifact = _register(context, snapshot_path, KIND_SUGGESTION,
                                       complete=True, suggestion_id=suggestion_id)
        context.report_progress(1, 1)
        if verdict != "new_node":
            mapping = {"duplicate": "duplicate", "insufficient": "insufficient", "news_only": "news_only"}
            return StageResult("needs_review", artifacts=(suggestion_artifact,),
                               checkpoint={"verdict": verdict, "suggestion_id": suggestion_id},
                               result={"suggestion_id": suggestion_id,
                                       "disposition": mapping.get(verdict, "candidate")},
                               error={"code": "insufficient",
                                      "message": f"分析结论为 {verdict}：本轮只自动入库新建知识，保留候选待人确认",
                                      "retryable": False, "requires_confirmation": True})
        return StageResult("completed", artifacts=(suggestion_artifact,),
                           checkpoint={"verdict": verdict, "suggestion_id": suggestion_id},
                           result={"suggestion_id": suggestion_id})


class GenerateAdapter(_ProviderAdapter):
    """生成笔记：调用 prepare_note，不在这里重复 accept 的模型链路。"""

    stage_name = "generate"
    method_name = "prepare_note"

    @property
    def available(self):
        return self.provider() is not None and (self._provider is not None or
            getattr(getattr(self.svc, "discovery", None), "note_processor", None) is not None)

    def _call(self, provider, context):
        return provider(_suggestion_id(context), context=context)

    def _map_completed(self, value, context):
        prepared_ref = str(value.get("prepared_ref") or "").strip()
        artifacts = tuple(value.get("artifacts") or ())
        if not prepared_ref or not all(isinstance(item, dict) for item in artifacts):
            return StageResult("failed", error={"code": "invalid_output",
                                                "message": "加工结果缺少 prepared_ref 或产物结构非法",
                                                "retryable": False})
        result = {"suggestion_id": str(value["suggestion_id"])} if value.get("suggestion_id") else None
        return StageResult("completed", artifacts=artifacts,
                           checkpoint={"prepared_ref": prepared_ref}, result=result)


class ValidateAdapter(_ProviderAdapter):
    """校验：调用 validate_prepared；未通过不得进入 commit。"""

    stage_name = "validate"
    method_name = "validate_prepared"

    def _call(self, provider, context):
        prepared_ref = _latest_prepared_ref(context)
        if not prepared_ref:
            raise WorkflowError("source_missing", "缺少 prepared_ref，不能跳过生成阶段直接校验")
        return provider(prepared_ref, context=context)

    def _map_completed(self, value, context):
        if not bool(value.get("publishable")):
            return StageResult("needs_review",
                               artifacts=tuple(value.get("artifacts") or ()),
                               checkpoint=value.get("admission") if isinstance(value.get("admission"), dict) else None,
                               error={"code": "not_publishable",
                                      "message": str(value.get("reason") or "校验未通过，未入库")[:200],
                                      "retryable": False, "requires_confirmation": True})
        return StageResult("completed", artifacts=tuple(value.get("artifacts") or ()),
                           checkpoint=value.get("admission") if isinstance(value.get("admission"), dict) else None)


class ReviewAdapter(_ProviderAdapter):
    """独立 AI 审核：全文分段核对原文依据与写作规范。

    通过 → completed；需修正 → needs_review（问题写入 review_issues 产物供生成器修正）；
    审核不可判定或缺少原文/笔记 → blocked，绝不默认为通过。审核未配置模型同样 blocked。
    """

    stage_name = "review"
    method_name = "review_prepared"

    def __init__(self, service=None, *, provider=None, reviewer_factory=None, missing_reason=None):
        super().__init__(service, provider=provider, missing_reason=missing_reason)
        self._factory = reviewer_factory
        self._reviewer = None
        self._loaded = False

    def describe(self) -> str:
        reviewer = self._reviewer_instance()
        return reviewer.describe() if reviewer is not None else self.unavailable_reason()

    def _reviewer_instance(self):
        if self._loaded:
            return self._reviewer
        self._loaded = True
        if self._factory is not None:
            try:
                value = self._factory()
            except Exception:  # noqa: BLE001 —— 配置不可用按未配置处理，不默认为通过
                value = None
            if isinstance(value, tuple):
                value = value[0] if value and value[0] is not None else None
            self._reviewer = value
        return self._reviewer

    @property
    def available(self) -> bool:
        if self._provider is not None:
            return True
        discovery = getattr(self.svc, "discovery", None)
        if self.provider() is not None:
            return getattr(discovery, "note_reviewer", None) is not None
        return self._reviewer_instance() is not None

    def unavailable_reason(self) -> str:
        if self._factory is None and self.provider() is None:
            return "review_prepared 尚未提供，且未配置独立 AI 审核模型（不默认为通过）"
        return f"review_prepared 尚未提供（{self.missing_reason}）"

    def _call(self, provider, context):
        prepared_ref = _latest_prepared_ref(context)
        if not prepared_ref:
            raise WorkflowError("source_missing", "缺少 prepared_ref，不能审核未生成的草稿")
        return provider(prepared_ref, context=context)

    def _extra_provider(self, method_name):
        discovery = getattr(self.svc, "discovery", None)
        candidate = getattr(discovery, method_name, None)
        return candidate if callable(candidate) else None

    def execute(self, context):
        provider = self.provider()
        if provider is not None:
            return self._execute_review_rounds(provider, context)
        reviewer = self._reviewer_instance()
        if reviewer is None:
            return StageResult("blocked", error={"code": "not_supported",
                                                 "message": self.unavailable_reason(),
                                                 "retryable": False})
        if context.cancel_requested():
            return StageResult("cancelled", error={"code": "cancelled", "message": "任务已取消", "retryable": False})
        _, source_path = _artifact_file(context, KIND_SOURCE_TEXT)
        if source_path is None:
            return StageResult("blocked", error={"code": "source_missing",
                                                 "message": "缺少完整原文产物，审核阻塞（不默认为通过）",
                                                 "retryable": False})
        _, note_path = _artifact_file(context, KIND_NOTE_BODY)
        if note_path is None:
            return StageResult("blocked", error={"code": "source_missing",
                                                 "message": "缺少待审核笔记正文产物（generate 需登记 note_body）",
                                                 "retryable": False})
        skill_text = self._skill_text(context)
        try:
            review = reviewer.review(_read_text(source_path), _read_text(note_path),
                                     skill_text, task_context=context)
        except WorkflowError:
            raise
        except Exception as exc:  # noqa: BLE001 —— 审核异常不放过，也不冒充通过
            return _map_exception(exc)
        return self._map_review(review, context)

    def _execute_review_rounds(self, provider, context):
        """审核 → 需修正则把问题交回生成器，最多两轮；修订后重新校验，校验不过不进 commit。"""
        revise = self._extra_provider("revise_prepared")
        validate = self._extra_provider("validate_prepared")
        artifacts: tuple = ()
        review = None
        for round_index in range(3):
            if context.cancel_requested():
                return StageResult("cancelled", error={"code": "cancelled", "message": "任务已取消",
                                                       "retryable": False})
            try:
                review = provider(_latest_prepared_ref(context), context=context)
            except WorkflowError:
                raise
            except Exception as exc:  # noqa: BLE001 —— 审核异常不放过，也不冒充通过
                return _map_exception(exc)
            if not isinstance(review, dict):
                return StageResult("failed", error={"code": "invalid_output",
                                                    "message": "审核返回结构无效", "retryable": False})
            if str(review.get("verdict")) != "revise" or revise is None or round_index >= 2:
                break
            try:
                revised = revise(_latest_prepared_ref(context), review.get("issues") or [],
                                 context=context)
            except WorkflowError as exc:
                if exc.code == "review_limit":
                    break   # 已达上限：保留候选与原因，不循环消耗
                raise
            if not isinstance(revised, dict) or not str(revised.get("prepared_ref") or ""):
                break
            artifacts = artifacts + tuple(item for item in (revised.get("artifacts") or ())
                                          if isinstance(item, dict))
            context.artifacts = tuple(context.artifacts) + tuple(revised.get("artifacts") or ())
            if validate is not None:
                try:
                    admission = validate(str(revised["prepared_ref"]), context=context)
                except WorkflowError:
                    raise
                except Exception as exc:  # noqa: BLE001
                    return _map_exception(exc)
                if not isinstance(admission, dict) or not admission.get("publishable"):
                    reason = str((admission or {}).get("reason") or "修订后校验未通过")
                    return StageResult("needs_review", artifacts=artifacts,
                                       checkpoint={"verdict": "revise", "revision_round": revised.get("revision_round")},
                                       error={"code": "not_publishable", "message": reason[:200],
                                              "retryable": False, "requires_confirmation": True},
                                       result={"note_status": "review_revise"})
        return self._map_review(review, context, artifacts=artifacts)

    def _skill_text(self, context) -> str:
        _, path = _artifact_file(context, "note_skill")
        if path is not None:
            return _read_text(path)
        processor = getattr(self.svc, "note_processor", None)
        router = getattr(processor, "router", None)
        try:
            latest = router.latest(None)
        except Exception:  # noqa: BLE001 —— 取不到 Skill 时交给审核者按空 Skill 处理
            return ""
        return latest[2] if isinstance(latest, tuple) and len(latest) > 2 else ""

    def _map_review(self, review, context, *, artifacts: tuple = ()) -> StageResult:
        if not isinstance(review, dict):
            return StageResult("failed", error={"code": "invalid_output",
                                                "message": "审核返回结构无效", "retryable": False})
        verdict = str(review.get("verdict") or "")
        issues = review.get("issues") if isinstance(review.get("issues"), list) else []
        summary = self._issues_path(context, review, issues)
        coverage = review.get("coverage") if isinstance(review.get("coverage"), dict) else {}
        checkpoint = {"verdict": verdict, "coverage": coverage,
                      "reviewer_mode": review.get("reviewer_mode"), "model": review.get("model"),
                      "revision_round": review.get("revision_round"), "issues": issues[:20]}
        artifacts = tuple(artifacts) + ((summary,) if summary else ())
        if verdict == "pass" and coverage.get("complete"):
            return StageResult("completed", artifacts=artifacts, checkpoint=checkpoint,
                               result={"note_status": "review_passed"})
        if verdict == "revise":
            return StageResult("needs_review", artifacts=artifacts, checkpoint=checkpoint,
                               error={"code": "review_revise",
                                      "message": "AI 审核要求修正：" + self._first_reason(issues),
                                      "retryable": False, "requires_confirmation": True},
                               result={"note_status": "review_revise"})
        return StageResult("blocked", artifacts=artifacts, checkpoint=checkpoint,
                           error={"code": "review_blocked",
                                  "message": "AI 审核无法判定或未覆盖全文，不自动入库：" + self._first_reason(issues),
                                  "retryable": False},
                           result={"note_status": "review_blocked"})

    @staticmethod
    def _first_reason(issues) -> str:
        for issue in issues:
            reason = str((issue or {}).get("reason") or "").strip()
            if reason:
                return reason[:200]
        return "未给出可定位依据"

    def _issues_path(self, context, review, issues):
        if not issues:
            return None
        path = _write_work_file(context, "review.json",
                               json.dumps({"verdict": review.get("verdict"), "issues": issues,
                                           "coverage": review.get("coverage")}, ensure_ascii=False, indent=2))
        return _register(context, path, KIND_REVIEW_ISSUES, complete=True)


def _latest_prepared_ref(context) -> str:
    """prepared_ref 是服务端登记的 prepared_note 产物路径（协作约定 §6）。"""
    for artifact in reversed(context.artifacts):
        value = str(artifact.get("prepared_ref") or "").strip()
        if value:
            return value
        if artifact.get("kind") == "prepared_note" and artifact.get("path"):
            return str(artifact["path"])
    return ""


class CommitAdapter(_ProviderAdapter):
    """入库：调用 commit_prepared；入库副作用由它在 context.run_effect 内执行。

    这里不再包一层 run_effect：重复包会让「审核未过/来源变化」这类写前失败被记成
    unknown，而真正的结果未知语义由 commit_prepared 自己的 effect 记录表达。
    """

    stage_name = "commit"
    method_name = "commit_prepared"

    def _call(self, provider, context):
        prepared_ref = _latest_prepared_ref(context)
        if not prepared_ref:
            raise WorkflowError("source_missing", "缺少 prepared_ref，不能提交未生成的成果")
        extra = {}
        try:
            parameters = inspect.signature(provider).parameters
        except (TypeError, ValueError):  # pragma: no cover - 内建/装饰器不可反射
            parameters = {}
        if "request_id" in parameters:
            extra["request_id"] = f"{context.task_id}:commit"
        if "strict" in parameters:
            extra["strict"] = True
        return provider(prepared_ref, context=context, **extra)

    def _map_completed(self, value, context):
        artifacts = tuple(item for item in (value.get("artifacts") or ()) if isinstance(item, dict))
        result = {}
        for field in ("suggestion_id", "node_id", "note_status", "disposition"):
            if value.get(field) is not None:
                result[field] = value[field]
        if value.get("published"):
            # 完成授权的新建/更新才登记 collected（不等于已更新原笔记，更不等于已掌握）
            result.setdefault("disposition", "collected")
            return StageResult("completed", artifacts=artifacts,
                               result=result or None)
        if value.get("deferred"):
            return StageResult("needs_review", artifacts=artifacts,
                               error={"code": "not_publishable",
                                      "message": str(value.get("reason") or "入库被推迟，等待确认")[:200],
                                      "retryable": False, "requires_confirmation": True},
                               result=result or None)
        return StageResult("failed", error={"code": "invalid_output",
                                            "message": "入库结果缺少 published/deferred 语义", "retryable": False})


class SyncAdapter:
    """Notion 同步：调用统一同步核心；未授权目标不发布，远端未知不重复发布。"""

    def __init__(self, service=None, *, core=None, missing_reason=None):
        self.svc = service
        self._core = core
        self._missing_reason = missing_reason or "未配置 Notion 同步（notion_sync.local.json）"

    def describe(self) -> str:
        core = self.core()
        if core is None:
            return self._missing_reason
        return "统一 Notion 同步核心：" + "、".join(sorted((core.cfg.get("targets") or {}).keys()))

    def core(self):
        if self._core is not None:
            return self._core
        self._core = getattr(self.svc, "notion_core", None)
        return self._core

    @property
    def available(self) -> bool:
        return self.core() is not None

    def unavailable_reason(self) -> str:
        return self._missing_reason

    def execute(self, context):
        alias = str((context.options or {}).get("publish_to") or "").strip()
        if not alias:
            return StageResult("skipped", result=None)  # 核心通常会先判 skipped，这里兜底
        core = self.core()
        if core is None:
            return StageResult("blocked", error={"code": "config_error",
                                                 "message": self._missing_reason, "retryable": False})
        targets = set((core.cfg.get("targets") or {}).keys())
        if alias not in targets:
            return StageResult("blocked", error={"code": "config_error",
                                                 "message": f"目标别名不在白名单：{alias}", "retryable": False})
        # 只同步本次入库的确切文件，不能退回另一版草稿或该节点的旧个人笔记。
        artifact, path = _artifact_file(context, KIND_NOTE)
        if path is None:
            return StageResult("blocked", error={"code": "source_missing",
                                                 "message": "缺少已入库笔记产物，不能同步", "retryable": False})
        if context.cancel_requested():
            return StageResult("cancelled", error={"code": "cancelled", "message": "任务已取消", "retryable": False})

        def action():
            return core.sync(source_path=str(path), target_alias=alias, doc_type="candidate_note",
                             agent_name="pka-graph")

        try:
            result = context.run_effect(_effect_id("notion", alias + "|" + str(path)), kind="remote_write", action=action)
        except WorkflowError as exc:
            if exc.code == "remote_result_unknown":
                return StageResult("unknown_result", error={"code": "remote_result_unknown",
                                                            "message": "Notion 结果未知，已停止自动重发，需先核实归属",
                                                            "retryable": False})
            raise
        status = str((result or {}).get("status") or "")
        page_id = (result or {}).get("page_id") or None
        if status in ("full_success", "duplicate_skipped"):
            # 只补 notion_page_id：disposition 由 commit 决定，同步不覆盖
            return StageResult("completed", result={"notion_page_id": page_id})
        if status == "new_version_detected":
            return StageResult("needs_review", error={"code": "write_conflict",
                                                      "message": "远端已有该文档的不同版本，需确认后新建",
                                                      "retryable": False, "requires_confirmation": True})
        if status == "outcome_unknown":
            return StageResult("unknown_result", error={"code": "remote_result_unknown",
                                                        "message": "Notion 结果未知，需先核实", "retryable": False})
        if status in ("pending_authorization", "not_configured"):
            return StageResult("blocked", error={"code": "config_error",
                                                 "message": str((result or {}).get("note") or "Notion 未授权")[:200],
                                                 "retryable": False})
        if status == "processing":
            return StageResult("blocked", error={"code": "write_conflict",
                                                 "message": "同一文档正被另一个调用者同步，未重复发布",
                                                 "retryable": False})
        return StageResult("failed", error={"code": "invalid_output",
                                            "message": f"同步状态无法确认：{status or '未知'}", "retryable": False})


def build_adapters(service=None, *, fetcher=None, fetch_enabled=None,
                   reviewer_factory=None, notion_core=None, providers=None) -> dict:
    """构造一次并挂到 svc.workflow；核心模块不反向导入本文件。"""
    providers = dict(providers or {})
    return {
        "acquire": AcquireAdapter(service, fetcher=fetcher, fetch_enabled=fetch_enabled),
        "extract": ExtractAdapter(service),
        "analyze": AnalyzeAdapter(service),
        "generate": GenerateAdapter(service, provider=providers.get("generate")),
        "validate": ValidateAdapter(service, provider=providers.get("validate")),
        "review": ReviewAdapter(service, provider=providers.get("review"), reviewer_factory=reviewer_factory),
        "commit": CommitAdapter(service, provider=providers.get("commit")),
        "sync": SyncAdapter(service, core=notion_core),
    }


def capability_items(adapters: dict, *, notion_configured: bool = False,
                     transcribe_worker: bool = False) -> list[dict]:
    """capabilities 路由合并用的补充条目：逐项报告未接入能力与原因（不冒称可用）。"""
    unavailable = []
    for name in ("analyze", "generate", "validate", "review", "commit", "sync"):
        adapter = adapters.get(name)
        if adapter is None or adapter.available:
            continue
        reason = getattr(adapter, "unavailable_reason", None)
        unavailable.append(f"{name}：" + (reason() if callable(reason) else "未接入或未配置"))
    items = []
    if unavailable:
        items.append({
            "name": "note_detail",
            "implemented": False, "configured": False, "available": False,
            "reason": "；".join(unavailable),
        })
    items.append({
        "name": "transcribe_then_note",
        "implemented": bool(transcribe_worker), "configured": bool(transcribe_worker),
        "available": bool(transcribe_worker),
        "reason": "可用" if transcribe_worker else "缺外部转写 worker：extract 无法取得字幕/ASR，按不可用处理",
    })
    items.append({
        "name": "notion_sync",
        "implemented": notion_configured, "configured": notion_configured,
        "available": notion_configured and adapters["sync"].available,
        "reason": adapters["sync"].describe() if notion_configured else "未配置 Notion 同步目标（publish_to 默认 null）",
    })
    return items
