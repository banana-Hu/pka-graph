"""服务端测验会话（第四阶段建立，第五阶段扩展复习联动）。

权威边界：
- 前端只能提交「回答 / 取消 / 换题 / 请求提示」，永远不能提交 passed、
  判定理由、模型结果、next_review_at——判定与计划只由服务端维护。
- 会话绑定：节点 ID、内容版本、冻结通过条件（哈希）；提交前逐项重验。
- 会话类型：mastery（首次/重新掌握，面向 untested/failed）与
  review（正式复习，面向 passed 且已排期、已到期的节点）；
  资格由服务端核验，不信任前端声明的类型。
- 使用提示或查看答案 → 本题转为**辅助学习**（assisted_learning）：
  显示模型参考意见，但不改变节点状态、不推进复习间隔（第五阶段修正，
  替代第四阶段的"强制判 failed"）。
- 完整问答只存于服务内存；持久化的只有节点状态、必要时间、通过条件、
  最小防重放标识（不含问答正文）。
"""

from __future__ import annotations

import hashlib
import json
import secrets
import threading
import uuid
from datetime import datetime, timedelta

from .models import ValidationError
from .quiz_model import Budget, ModelError, normalize_verdict_payload
from .review_service import utcnow
from .service import GraphService, NotFound

SESSION_TTL_MINUTES = 45
MAX_SESSIONS = 50
MAX_ANSWER_CHARS = 5000

TERMINAL = ("passed", "failed", "inconclusive", "assisted_learning",
            "failed_request", "cancelled", "expired")


def _now() -> datetime:
    return utcnow()


def _iso(dt: datetime) -> str:
    return dt.isoformat(timespec="seconds")


class QuizError(RuntimeError):
    """会话层面的业务拒绝（非法状态流转、过期、重放、资格不符等）。"""


class QuizSession:
    def __init__(self, node_id: str, node_name: str, node_scope: str, content_version: int, kind: str,
                 clock=None):
        self.clock = clock or _now
        self.id = "quiz-" + uuid.uuid4().hex[:12]
        self.node_id = node_id
        self.node_name = node_name
        self.node_scope = node_scope
        self.content_version = content_version
        self.kind = kind  # mastery | review
        self.criteria: dict | None = None
        self.criteria_hash: str | None = None
        self.criteria_source: str = ""
        self.question: str | None = None
        self.hints: list[str] = []
        self.hint_used = False
        self.status = "preparing"
        self.feedback: dict | None = None
        self.error: str | None = None
        self.created_at = self.clock()
        self.deadline = self.created_at + timedelta(minutes=SESSION_TTL_MINUTES)
        self.generation = 1
        self.calls = 0
        self.submitted = False
        self.include_note_content = False
        self.note_excerpts: list[dict] = []
        self.note_material_report: dict | None = None   # 取材报告（取得/缺失/截断）
        self.mirror_result: dict | None = None          # 状态镜像结果（含 disabled）
        self.data_scope: dict | None = None
        self.lock = threading.Lock()

    def __getitem__(self, key):
        """适配器以 session["criteria"] 等字典式访问会话数据；统一映射到属性。"""
        try:
            return getattr(self, key)
        except AttributeError as exc:
            raise KeyError(key) from exc

    def get(self, key, default=None):
        """字典风格的 .get()，与 __getitem__ 配套。"""
        try:
            return self[key]
        except KeyError:
            return default

    def compute_criteria_digest(self) -> str:
        return hashlib.sha256(
            json.dumps(self.criteria, ensure_ascii=False, sort_keys=True).encode("utf-8")
        ).hexdigest()[:16]


class QuizService:
    def __init__(self, graph_service: GraphService, adapter, budget: Budget, mode: str,
                 review=None, clock=None):
        self.svc = graph_service
        self.adapter = adapter
        self.budget = budget
        self.mode = mode
        self.review = review  # ReviewService（第五阶段：计划联动）
        self.clock = clock or _now
        self.sessions: dict[str, QuizSession] = {}
        self.lock = threading.Lock()

    # ---------- 会话管理 ----------

    def _purge(self) -> None:
        now = self.clock()
        for sid in list(self.sessions):
            s = self.sessions[sid]
            if now > s.deadline and s.status not in TERMINAL:
                s.status = "expired"
        while len(self.sessions) > MAX_SESSIONS:
            oldest = min(self.sessions.values(), key=lambda s: s.created_at)
            self.sessions.pop(oldest.id, None)

    def _get(self, session_id: str) -> QuizSession:
        with self.lock:
            session = self.sessions.get(session_id)
        if session is None:
            raise QuizError("会话不存在，或已因服务重启失效——请重新发起测验")
        if self.clock() > session.deadline and session.status not in TERMINAL:
            with session.lock:
                if session.status not in TERMINAL:
                    session.status = "expired"
        return session

    def _spend(self, session: QuizSession):
        with session.lock:
            if session.calls >= self.budget.max_calls:
                raise QuizError(f"本会话模型调用次数已达上限（{self.budget.max_calls}）")
            session.calls += 1
            session.generation += 1
            return session.generation

    def _run_background(self, session: QuizSession, generation: int, job) -> None:
        def worker():
            try:
                job()
            except ModelError as exc:
                self._background_fail(session, generation, str(exc))
            except Exception as exc:  # noqa: BLE001
                self._background_fail(session, generation, f"{type(exc).__name__}")
        threading.Thread(target=worker, daemon=True).start()

    def _background_fail(self, session: QuizSession, generation: int, message: str):
        with session.lock:
            if session.generation != generation:
                return
            if session.status not in ("preparing", "awaiting_question", "awaiting_answer", "judging"):
                return
            session.status = "failed_request"
            session.error = message[:200]

    # ---------- 任务 2：准备（资格由服务端核验，不信任前端类型声明） ----------

    @staticmethod
    def _mirror_enabled() -> bool:
        """状态镜像独立开关：默认关闭；仅显式 PKA_MIRROR_STATUS=1/true/on 启用。"""
        import os

        return os.environ.get("PKA_MIRROR_STATUS", "").strip().lower() in ("1", "true", "on")

    def _collect_note_material(self, state: dict, node_id: str) -> dict:
        """测验取材：统一只读解析入口，逐关联读取正文并如实报告。

        - 与阅读/节点详情/外部接口共用 resolve_note_link（移动/重命名可按稳定 ID 回查）；
        - 读取或解析失败计入 missing，绝不静默吞掉后假装正文已提供；
        - 正文按预算截断时在 truncated 里如实登记；
        - audit_status 来自笔记 front-matter 的实际 status 字段。
        """
        from .frontmatter import parse_file

        excerpts: list[dict] = []
        missing: list[dict] = []
        truncated: list[str] = []
        requested = 0
        limit = self.budget.max_input_chars // 2
        for link in state["note_links"]:
            if link["node_id"] != node_id:
                continue
            requested += 1
            res = self.svc.resolve_note_link(link)
            if res["status"] != "ok":
                missing.append({"path": (link.get("ref") or {}).get("path"),
                                "resolution": res["status"],
                                "message": res.get("message") or ""})
                continue
            try:
                path = self.svc.store.safe_read_path(res["path"])
                meta, body = parse_file(path)
            except Exception as exc:  # noqa: BLE001 —— 显式计为缺失，不静默
                missing.append({"path": res["path"], "resolution": "read_error",
                                "message": f"{type(exc).__name__}: {exc}"[:160]})
                continue
            content = body
            if len(content) > limit:
                content = content[:limit]
                truncated.append(res["path"])
            excerpts.append({
                "title": str(meta.get("title") or path.stem),
                "audit_status": str(meta.get("status") or ""),
                "content": content,
                "path": res["path"],
                "relocated": res.get("relocated", False),
                "warnings": list(res.get("warnings") or []),
            })
        return {"excerpts": excerpts, "missing": missing, "truncated": truncated,
                "obtained": len(excerpts), "requested": requested}

    def start(self, node_id: str, kind: str = "mastery") -> dict:
        self._purge()
        if self.adapter is None:
            raise QuizError(self.unavailable_reason or "掌握测验不可用：未配置真实模型")
        if kind not in ("mastery", "review"):
            raise ValidationError(f"未知会话类型：{kind!r}")
        if self.mode == "demo":
            from .quiz_model import is_test_vault

            if not is_test_vault(self.svc.store.vault_root):
                raise QuizError(
                    "演示模式只能在带 .pka-test-vault.json 标记的专用测试库使用，"
                    "不能修改普通知识库的掌握状态。"
                )
        state = self.svc.state()
        node = next((n for n in state["nodes"] if n["id"] == node_id), None)
        if node is None:
            raise NotFound("节点", node_id)
        if node["archived"]:
            raise ValidationError("归档节点不能发起测验（先恢复再测）")

        if kind == "review":
            if node["status"] != "passed":
                raise QuizError("正式复习只面向已通过（passed）的节点；未掌握节点请用掌握测验")
            review_settings = self.review.settings() if self.review else {"enabled": False}
            if not review_settings.get("enabled"):
                raise QuizError("复习尚未启用：请在「待复习」页确认启用复习计划")
            due = node.get("next_review_at")
            if not due:
                raise QuizError("该节点还没有复习安排（尚未排期）")
            if node.get("review_paused"):
                raise QuizError("该节点的复习已暂停；请先在待复习列表恢复")
            due_dt = datetime.fromisoformat(str(due))
            if self.clock() < due_dt:
                raise QuizError(
                    f"尚未到期（到期时间 {due}）。提前练习功能未开放；到期后该事项会一直保留。"
                )
        else:
            if node["status"] == "passed":
                raise QuizError(
                    "该节点已通过测验（掌握层）。独立复测请使用「开始复习」（到期后），"
                    "未通过将按复习规则回到视野层。"
                )
        session = QuizSession(node_id, node["name"], node["scope"], node["content_version"], kind,
                              clock=self.clock)
        with self.lock:
            self.sessions[session.id] = session
        session.data_scope = {
            "adapter": self.adapter.describe(),
            "always_sent": ["节点名称", "测验范围", "冻结的通过条件", "当前问题", "你的本次回答"],
            "sent_only_if_confirmed": ["关联笔记正文摘录（默认不发送）"],
            "never_sent": ["state.json", "其他节点", "任何密钥"],
            "privacy": "问答仅存于服务内存：会话结束/过期/服务重启即清理；第三方模型服务自身的留存政策不受本地控制。",
        }
        if kind == "review":
            session.data_scope["kind"] = "正式复习：通过后推进间隔；未通过将回到视野层"
        criteria_hint = node.get("pass_criteria")

        def job():
            if isinstance(criteria_hint, dict) and criteria_hint.get("key_points"):
                criteria = criteria_hint
                source = "沿用节点已冻结的通过条件"
            else:
                criteria = self.adapter.build_criteria(node)
                source = "AI 提议并已冻结（回答后不再更改标准）"
            if not isinstance(criteria.get("key_points"), list) or not criteria["key_points"]:
                raise ModelError("通过条件生成失败（key_points 为空）")
            with session.lock:
                session.criteria = criteria
                session.criteria_hash = session.compute_criteria_digest()
                session.criteria_source = source
                session.status = "awaiting_question"
            generation = self._spend(session)
            self._run_background(session, generation, lambda: self._make_question(session, generation))

        self._run_background(session, session.generation, job)
        return self.view(session.id)

    def _make_question(self, session: QuizSession, generation: int) -> None:
        if session.include_note_content:
            state = self.svc.state()
            report = self._collect_note_material(state, session.node_id)
            session.note_material_report = report
            session.note_excerpts = report["excerpts"]
            # 门控（审查修复【5】）：零关联、空正文、全部缺失——任一情况都不得
            # 静默降级为仅凭标题/scope 出题。空正文摘录视同未取得。
            empty = [e for e in report["excerpts"] if not (e.get("content") or "").strip()]
            for e in empty:
                report["missing"].append({"path": e.get("path"), "resolution": "empty_body",
                                          "message": "笔记正文为空"})
            report["excerpts"] = [e for e in report["excerpts"]
                                  if (e.get("content") or "").strip()]
            session.note_excerpts = report["excerpts"]
            report["obtained"] = len(report["excerpts"])
            blocked_reason = None
            if report["requested"] == 0:
                blocked_reason = "该节点没有关联笔记，无法基于笔记内容出题。"
            elif report["obtained"] == 0:
                detail = "；".join((m.get("message") or m.get("resolution") or "")[:60]
                                   for m in report["missing"][:3])
                blocked_reason = f"无法取得关联笔记正文（{detail}）。"
            if blocked_reason:
                with session.lock:
                    if session.generation == generation and session.status == "awaiting_question":
                        session.status = "failed_request"
                        session.error = (blocked_reason +
                                         "已停止出题：可修复关联后重试，或改用不基于笔记内容的测验。")
                print(f"[quiz] 取材失败：{blocked_reason[:80]}", flush=True)
                return
        question = self.adapter.build_question(session)
        with session.lock:
            if session.generation != generation or session.status != "awaiting_question":
                return
            session.question = question
            session.status = "awaiting_answer"

    def confirm_note_content(self, session_id: str) -> dict:
        session = self._get(session_id)
        with session.lock:
            if session.status not in ("awaiting_answer", "awaiting_question"):
                raise QuizError("当前阶段不能更改数据发送范围")
            session.include_note_content = True
        return {"include_note_content": True, "note": "判定材料将附带关联笔记正文摘录（已截断）"}

    # ---------- 出题 / 提示 / 换题 ----------

    def renew_question(self, session_id: str) -> dict:
        session = self._get(session_id)
        with session.lock:
            if session.status != "awaiting_answer":
                raise QuizError(f"当前状态不能换题：{session.status}")
            session.hint_used = False
            session.hints = []
        generation = self._spend(session)
        self._run_background(session, generation, lambda: self._make_question(session, generation))
        return self.view(session_id)

    def request_hint(self, session_id: str) -> dict:
        session = self._get(session_id)
        with session.lock:
            if session.status != "awaiting_answer":
                raise QuizError(f"当前状态不能请求提示：{session.status}")
            session.hint_used = True  # 本题转为辅助学习，不能作为独立通过依据（服务端强制）
        generation = self._spend(session)
        self._run_background(session, generation, lambda: self._make_hint(session, generation))
        return self.view(session_id)

    def _make_hint(self, session: QuizSession, generation: int) -> None:
        hint = self.adapter.hint(session, session.question or "")
        with session.lock:
            if session.generation != generation or session.status != "awaiting_answer":
                return
            session.hints.append(hint)

    def answer(self, session_id: str, answer: str) -> dict:
        session = self._get(session_id)
        answer = (answer or "").strip()
        if not answer:
            raise ValidationError("回答不能为空")
        if len(answer) > MAX_ANSWER_CHARS:
            raise ValidationError(f"回答过长（>{MAX_ANSWER_CHARS} 字符）")
        with session.lock:
            if session.status == "judging":
                raise QuizError("正在判定中，请勿重复提交")
            if session.status != "awaiting_answer":
                raise QuizError(f"当前状态不能提交回答：{session.status}")
            if not session.question or not session.criteria:
                raise QuizError("会话尚未准备好问题")
            session.status = "judging"
        generation = self._spend(session)
        self._run_background(session, generation, lambda: self._judge(session, generation, answer))
        return self.view(session_id)

    # ---------- 判定与受控提交 ----------

    def _judge(self, session: QuizSession, generation: int, answer: str) -> None:
        try:
            payload = normalize_verdict_payload(self.adapter.judge(session, session.question or "", answer))
        except ModelError as exc:
            self._background_fail(session, generation, str(exc))
            return
        self._submit_verdict(session, generation, payload)

    def _submit_verdict(self, session: QuizSession, generation: int, payload: dict) -> None:
        # ---- 规则 B（第五阶段修正）：使用提示/查看答案 → 本题为辅助学习 ----
        # 不改变节点状态、不推进间隔；展示模型参考意见供学习，但明确不计入独立判定。
        if session.hint_used:
            with session.lock:
                if session.generation != generation or session.status != "judging":
                    return
                session.status = "assisted_learning"
                session.feedback = {
                    "verdict": "assisted_learning",
                    "model_verdict": payload["verdict"],
                    "feedback": (
                        f"本题使用了提示，属于辅助学习，不计入独立掌握判定"
                        f"（模型参考意见：{payload['verdict']}）。"
                        "节点状态与复习计划未改变；可稍后换一道独立新题再测。"
                    ),
                    "gaps": payload.get("gaps") or [],
                    "key_points_met": payload.get("key_points_met") or [],
                }
                session.submitted = True
            return

        verdict = payload["verdict"]
        for _attempt in range(3):
            state = self.svc.state()
            node = next((n for n in state["nodes"] if n["id"] == session.node_id), None)
            with session.lock:
                expired = session.generation != generation or session.status != "judging"
            if expired:
                return  # 取消/过期/换题后的迟到结果：丢弃
            if node is None:
                with session.lock:
                    session.status, session.error = "inconclusive", "节点已不存在，结果未写入"
                break
            if node["archived"]:
                with session.lock:
                    session.status, session.error = "inconclusive", "节点已归档，结果未写入"
                break
            if node["content_version"] != session.content_version:
                with session.lock:
                    session.status, session.error = "inconclusive", "节点知识范围已变化（内容版本不一致），旧会话结果拒绝提交；请发起新测验"
                break
            if session.criteria_source.startswith("沿用") and node.get("pass_criteria"):
                stored_hash = hashlib.sha256(
                    json.dumps(node["pass_criteria"], ensure_ascii=False, sort_keys=True).encode("utf-8")
                ).hexdigest()[:16]
                if stored_hash != session.compute_criteria_digest():
                    with session.lock:
                        session.status, session.error = "inconclusive", "通过条件已变化，旧结果拒绝提交"
                    break
            # 会话资格重验（review 类型）：计划仍存在且未被他处改动
            if session.kind == "review":
                if node["status"] != "passed" or not node.get("next_review_at"):
                    with session.lock:
                        session.status, session.error = "inconclusive", "复习计划已变化（节点不再是已排期的通过状态），结果拒绝提交"
                    break

            # 状态与复习计划统一由 ReviewService 维护（同一次原子 save 内完成）
            if self.review is not None:
                self.review.on_quiz_submitted(state, node, kind=session.kind, verdict=verdict)
            else:
                node["status"] = verdict if verdict in ("passed", "failed") else node["status"]
                if verdict in ("passed", "failed"):
                    node["last_quiz_at"] = _iso(self.clock())
            # 掌握状态来源标注（第五阶段交接检查 B）：演示模式的通过永远是测试数据，
            # 删除测试库标记也不会把它洗成真实测验结果；只有真实模式下的独立通过
            # 才写 status_origin="real"。
            if verdict in ("passed", "failed"):
                node["status_origin"] = "demo" if self.mode == "demo" else "real"

            if node.get("pass_criteria") is None or session.criteria_source.startswith("AI 提议"):
                node["pass_criteria"] = session.criteria

            key = f"quiz-submit:{session.id}"
            if key in state["idempotency"]:
                break  # 已提交过：重放保护（同一会话只推进一次间隔/状态）
            state["idempotency"][key] = {
                "op": "quiz_submit",
                "at": _iso(self.clock()),
                "result": {"verdict": verdict, "node_id": session.node_id, "kind": session.kind},
            }
            while len(state["idempotency"]) > 200:
                state["idempotency"].pop(next(iter(state["idempotency"])))
            try:
                self.svc.store.save(state, base_version=state["version"])
            except Exception:
                continue  # 并发冲突：用最新状态重试（无关节点变化不受影响）
            with session.lock:
                if session.generation != generation or session.status != "judging":
                    return
                session.status = verdict
                session.feedback = payload
                session.submitted = True
            # 状态镜像（best-effort，独立授权、默认关闭，2026-10-03 审查修复）：
            # 真实库模式下把新状态写回关联笔记 frontmatter 供 Obsidian 着色。
            # 写管理状态的授权不等于修改笔记的授权——必须显式 PKA_MIRROR_STATUS=1；
            # 关闭时测验状态与复习安排照常维护，只是不改笔记文件。失败可见不伪装。
            if verdict in ("passed", "failed") and self._mirror_enabled():
                try:
                    from . import mirror

                    out = mirror.sync_node(self.svc, session.node_id, enabled=True)
                    session.mirror_result = out
                except Exception as exc:  # noqa: BLE001 —— 镜像失败可见但不伪装提交结果
                    session.mirror_result = {"status": "failed",
                                             "error": f"{type(exc).__name__}: {exc}"[:160]}
                    print(f"[mirror] 状态镜像失败（{type(exc).__name__}）：{exc}", flush=True)
            elif verdict in ("passed", "failed"):
                session.mirror_result = {"status": "disabled"}
            return
        else:
            with session.lock:
                if session.status == "judging":
                    session.status, session.error = "inconclusive", "提交冲突：请基于最新图谱重新测验"

    # ---------- 取消 / 视图 ----------

    def cancel(self, session_id: str) -> dict:
        session = self._get(session_id)
        with session.lock:
            if session.status in TERMINAL:
                return self.view(session_id)
            session.status = "cancelled"
            session.generation += 1
        return self.view(session_id)

    def view(self, session_id: str) -> dict:
        session = self._get(session_id)
        with session.lock:
            return {
                "session_id": session.id,
                "node_id": session.node_id,
                "node_name": session.node_name,
                "node_scope": session.node_scope,
                "kind": session.kind,
                "status": session.status,
                "criteria": session.criteria,
                "criteria_source": session.criteria_source,
                "question": session.question,
                "hints": session.hints,
                "hint_used": session.hint_used,
                "feedback": session.feedback,
                "error": session.error,
                "calls_used": session.calls,
                "calls_max": self.budget.max_calls,
                "data_scope": session.data_scope,
                "include_note_content": session.include_note_content,
                "note_material_report": session.note_material_report,
                "mirror_result": session.mirror_result,
                "expires_at": _iso(session.deadline),
                "adapter": self.adapter.describe() if self.adapter else None,
            }
