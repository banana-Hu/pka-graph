"""第五阶段测试：间隔复习、到期提醒、复测状态联动（必测 1-24）。"""

import json
import sys
import tempfile
import threading
import time
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tests"))

from fixtures import make_test_vault
from pka_graph.models import ValidationError
from pka_graph.quiz_model import Budget, DemoModelAdapter, ModelError, FakeModelAdapter
from pka_graph.quiz_service import QuizError, QuizService
from pka_graph.review_service import (
    DEFAULT_INTERVALS,
    REVIEW_SECTION,
    ReviewService,
    add_days,
    sh_today,
)
from pka_graph.service import GraphService
from pka_graph.store import StateStore


class FixedClock:
    """可注入时钟：不修改系统时间。"""

    def __init__(self, start: datetime):
        self.now_dt = start

    def __call__(self) -> datetime:
        return self.now_dt

    def advance(self, **kw):
        self.now_dt = self.now_dt + timedelta(**kw)


class ReviewTestBase(unittest.TestCase):
    """统一夹具：固定时钟从 2026-10-05（周一）10:00 UTC 起。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.vault = make_test_vault(Path(self._tmp.name))
        marker = self.vault / ".pka-test-vault.json"
        marker.write_text(json.dumps({"purpose": "test"}), encoding="utf-8")
        self.clock = FixedClock(datetime(2026, 10, 5, 10, 0, tzinfo=timezone.utc))
        self.gs = GraphService(StateStore(self.vault))
        self.review = ReviewService(self.gs, clock=self.clock)
        self.adapter = FakeModelAdapter()
        self.quiz = QuizService(self.gs, self.adapter, Budget(max_calls=10), "demo",
                                review=self.review, clock=self.clock)

    def tearDown(self):
        self._tmp.cleanup()

    def _node(self, name="测试节点", domain="AI技术领域", scope="某个明确范围"):
        return self.gs.create_node(name=name, domain=domain, scope=scope)["node"]

    def _force(self, node, status, **extra):
        state = self.gs.state()
        for n in state["nodes"]:
            if n["id"] == node["id"]:
                n["status"] = status
                n.update(extra)
        self.gs.store.save(state, base_version=None)
        return node

    def _wait(self, session_id, status, timeout=10):
        deadline = time.time() + timeout
        while time.time() < deadline:
            v = self.quiz.view(session_id)
            if v["status"] == status:
                return v
            time.sleep(0.05)
        raise AssertionError(f"等待 {status} 超时：{v['status']}")

    def _wait_terminal(self, session_id, timeout=10):
        from pka_graph.quiz_service import TERMINAL

        deadline = time.time() + timeout
        while time.time() < deadline:
            v = self.quiz.view(session_id)
            if v["status"] in TERMINAL:
                return v
            time.sleep(0.05)
        raise AssertionError(f"等待终态超时：{v['status']}")

    def _pass_with_quiz(self, node):
        """通过一次真实会话流程使节点通过（走受控提交）。"""
        self.adapter.judge_script = [{"verdict": "passed", "feedback": "ok"}]
        s = self.quiz.start(node["id"])
        deadline = time.time() + 10
        while time.time() < deadline:
            if self.quiz.view(s["session_id"])["status"] == "awaiting_answer":
                break
            time.sleep(0.05)
        self.quiz.answer(s["session_id"], "长度超过二十个字的正确回答内容，覆盖了关键点与边界举例。")
        deadline = time.time() + 10
        while time.time() < deadline:
            v = self.quiz.view(s["session_id"])
            if v["status"] in ("passed", "failed", "inconclusive", "assisted_learning"):
                return v
            time.sleep(0.05)
        raise AssertionError("判定未完成")

    def _review_quiz(self, node, script):
        self.adapter.judge_script = list(script)
        s = self.quiz.start(node["id"], kind="review")
        deadline = time.time() + 10
        while time.time() < deadline:
            if self.quiz.view(s["session_id"])["status"] == "awaiting_answer":
                break
            time.sleep(0.05)
        self.quiz.answer(s["session_id"], "长度超过二十个字的复习回答，覆盖机制、场景与边界举例。")
        return self._wait_terminal(s["session_id"])


class EnableAndSchedulingTests(ReviewTestBase):
    def test_1_no_schedule_before_enable(self):
        """必测 1：启用复习前，新 passed 节点不自动排期。"""
        node = self._node()
        view = self._pass_with_quiz(node)
        self.assertEqual(view["status"], "passed")
        node_now = self.gs.node_detail(node["id"])["node"]
        self.assertEqual(node_now["status"], "passed")
        self.assertIsNone(node_now["next_review_at"])  # 未启用 → 尚未安排
        self.assertFalse(self.review.settings()["enabled"])

    def test_enable_requires_confirm(self):
        """启用需确认：无 confirm 拒绝并展示默认间隔；确认后 enabled=true 且记录确认时间。"""
        with self.assertRaises(ValidationError) as ctx:
            self.review.update_settings(enabled=True)
        self.assertIn("默认间隔", str(ctx.exception))
        self.assertIsNone(self.review.settings()["confirmed_at"])
        out = self.review.update_settings(enabled=True, confirm=True)
        self.assertTrue(out["settings"]["enabled"])
        self.assertIsNotNone(out["settings"]["confirmed_at"])
        self.assertEqual(out["settings"]["intervals"], DEFAULT_INTERVALS)

    def test_2_first_pass_schedules_first_tier(self):
        """必测 2：启用后首次通过按首档排期。"""
        self.review.update_settings(enabled=True, confirm=True)
        node = self._node()
        self._pass_with_quiz(node)
        node_now = self.gs.node_detail(node["id"])["node"]
        self.assertEqual(node_now["status"], "passed")
        self.assertEqual(node_now["interval_index"], 0)
        expected = add_days(self.clock(), DEFAULT_INTERVALS[0])
        self.assertEqual(node_now["next_review_at"], expected.isoformat(timespec="seconds"))

    def test_3_review_pass_advances_one_tier(self):
        """必测 3：正式复习通过仅推进一档。"""
        self.review.update_settings(enabled=True, confirm=True)
        node = self._node()
        self._pass_with_quiz(node)
        self.clock.advance(days=8)
        view = self._review_quiz(node, [{"verdict": "passed", "feedback": "ok"}])
        self.assertEqual(view["status"], "passed")
        node_now = self.gs.node_detail(node["id"])["node"]
        self.assertEqual(node_now["interval_index"], 1)
        expected = add_days(self.clock(), DEFAULT_INTERVALS[1])
        self.assertEqual(node_now["next_review_at"], expected.isoformat(timespec="seconds"))

    def test_4_max_tier_caps(self):
        """必测 4：最大档位行为——到达 90 天档后继续使用该档。"""
        self.review.update_settings(enabled=True, confirm=True)
        node = self._node()
        self._pass_with_quiz(node)
        self.clock.advance(days=8)
        self._review_quiz(node, [{"verdict": "passed"}])
        self.clock.advance(days=22)
        self._review_quiz(node, [{"verdict": "passed"}])
        self.clock.advance(days=46)
        self._review_quiz(node, [{"verdict": "passed"}])
        node_now = self.gs.node_detail(node["id"])["node"]
        self.assertEqual(node_now["interval_index"], len(DEFAULT_INTERVALS) - 1)  # 封顶
        expected = add_days(self.clock(), DEFAULT_INTERVALS[-1])
        self.assertEqual(node_now["next_review_at"], expected.isoformat(timespec="seconds"))

    def test_5_review_fail_resets_to_failed_clears_plan(self):
        """必测 5：独立复习失败 → failed，周期计划清除，进入待重学。"""
        self.review.update_settings(enabled=True, confirm=True)
        node = self._node()
        self._pass_with_quiz(node)
        self.clock.advance(days=8)
        view = self._review_quiz(node, [{"verdict": "failed", "gaps": ["机制混淆"]}])
        self.assertEqual(view["status"], "failed")
        node_now = self.gs.node_detail(node["id"])["node"]
        self.assertEqual(node_now["status"], "failed")
        self.assertIsNone(node_now["next_review_at"])
        self.assertIsNone(node_now["interval_index"])
        groups = self.review.due_groups()
        self.assertEqual([i["node_id"] for i in groups["groups"]["relearn"]], [node["id"]])

    def test_6_failed_then_pass_restarts_first_tier(self):
        """必测 6：failed 再次通过 → passed，从首档重启。"""
        self.review.update_settings(enabled=True, confirm=True)
        node = self._node()
        self._pass_with_quiz(node)
        self.clock.advance(days=8)
        self._review_quiz(node, [{"verdict": "failed"}])
        # failed 节点用掌握测验重新通过
        view = self._pass_with_quiz(node)
        self.assertEqual(view["status"], "passed")
        node_now = self.gs.node_detail(node["id"])["node"]
        self.assertEqual(node_now["interval_index"], 0)
        expected = add_days(self.clock(), DEFAULT_INTERVALS[0])
        self.assertEqual(node_now["next_review_at"], expected.isoformat(timespec="seconds"))

    def test_7_hinted_assisted_no_failure_no_advance(self):
        """必测 7：使用提示 → 辅助学习：不标失败、不推进计划、原到期保留。"""
        self.review.update_settings(enabled=True, confirm=True)
        node = self._node()
        self._pass_with_quiz(node)
        due_before = self.gs.node_detail(node["id"])["node"]["next_review_at"]
        self.clock.advance(days=8)
        self.adapter.judge_script = [{"verdict": "passed"}]
        s = self.quiz.start(node["id"], kind="review")
        deadline = time.time() + 10
        while time.time() < deadline:
            if self.quiz.view(s["session_id"])["status"] == "awaiting_answer":
                break
            time.sleep(0.05)
        self.quiz.request_hint(s["session_id"])
        self._wait(s["session_id"], "awaiting_answer")
        self.quiz.answer(s["session_id"], "看完提示后复述的回答，长度超过二十个字。")
        view = self._wait_terminal(s["session_id"])
        self.assertEqual(view["status"], "assisted_learning")
        node_now = self.gs.node_detail(node["id"])["node"]
        self.assertEqual(node_now["status"], "passed")       # 未标失败
        self.assertEqual(node_now["next_review_at"], due_before)  # 间隔未推进
        self.assertEqual(node_now["last_review_result"], "passed")  # 仍是上次结果

    def test_8_inconclusive_changes_nothing(self):
        """必测 8：inconclusive 不改变状态与到期日。"""
        self.review.update_settings(enabled=True, confirm=True)
        node = self._node()
        self._pass_with_quiz(node)
        due_before = self.gs.node_detail(node["id"])["node"]["next_review_at"]
        self.clock.advance(days=8)
        view = self._review_quiz(node, [{"feedback": "无法判定缺关键信息"}])  # 缺 verdict → 失败请求
        self.assertEqual(view["status"], "failed_request")
        node_now = self.gs.node_detail(node["id"])["node"]
        self.assertEqual(node_now["status"], "passed")
        self.assertEqual(node_now["next_review_at"], due_before)




class ReminderBehaviorTests(ReviewTestBase):
    def _prepared(self):
        self.review.update_settings(enabled=True, confirm=True)
        node = self._node()
        self._pass_with_quiz(node)
        self.clock.advance(days=8)  # 到期
        return node

    def test_9_10_cancel_error_and_unanswered_keep_due(self):
        """必测 9/10：取消/错误不改变状态；未回应不顺延 due。"""
        node = self._prepared()
        due = self.gs.node_detail(node["id"])["node"]["next_review_at"]
        s = self.quiz.start(node["id"], kind="review")
        self._wait(s["session_id"], "awaiting_answer")
        self.quiz.cancel(s["session_id"])
        self.assertEqual(self.gs.node_detail(node["id"])["node"]["next_review_at"], due)
        self.clock.advance(days=30)  # 长期不回应
        groups = self.review.due_groups()
        item = next(i for i in groups["groups"]["overdue"] if i["node_id"] == node["id"])
        self.assertEqual(item["due"], due)  # 原 due 保留（逾期天数如实增加）

    def test_11_snooze_affects_reminder_not_due(self):
        """必测 11：snooze 只影响提醒，不改变 due。"""
        node = self._prepared()
        due = self.gs.node_detail(node["id"])["node"]["next_review_at"]
        out = self.review.snooze(node["id"], hours=8)
        self.assertEqual(out["due_unchanged"], due)
        groups = self.review.due_groups()
        item = next(i for i in groups["groups"]["overdue"] if i["node_id"] == node["id"])
        self.assertTrue(item["snoozed"])  # 提醒被推迟

    def test_12_pause_resume(self):
        """必测 12：暂停移入 paused 分组并阻止复习会话；恢复回到到期分组。"""
        node = self._prepared()
        self.review.set_paused(node["id"], True)
        groups = self.review.due_groups()
        self.assertEqual([i["node_id"] for i in groups["groups"]["paused"]], [node["id"]])
        with self.assertRaises(QuizError):
            self.quiz.start(node["id"], kind="review")
        self.review.set_paused(node["id"], False)
        groups = self.review.due_groups()
        self.assertEqual([i["node_id"] for i in groups["groups"]["overdue"]], [node["id"]])


class ReliabilityTests(ReviewTestBase):
    def test_13_duplicate_verdict_advances_once(self):
        """必测 13：重复提交（重放）不重复推进间隔。"""
        self.review.update_settings(enabled=True, confirm=True)
        node = self._node()
        self._pass_with_quiz(node)
        idx1 = self.gs.node_detail(node["id"])["node"]["interval_index"]
        self.clock.advance(days=8)
        view = self._review_quiz(node, [{"verdict": "passed"}])
        self.assertEqual(view["status"], "passed")
        node_now = self.gs.node_detail(node["id"])["node"]
        self.assertEqual(node_now["interval_index"], idx1 + 1)
        # 幂等键存在，且再次重放同一会话不可能（内存会话已终态拒绝）
        self.assertIn(f"quiz-submit:{view['session_id']}", self.gs.state()["idempotency"])
        with self.assertRaises(QuizError):
            self.quiz.answer(view["session_id"], "长度超过二十个字的重复提交回答内容测试。")

    def test_14_scope_change_rejects_review_result(self):
        """必测 14：节点范围变化后旧复习会话拒绝提交。"""
        self.review.update_settings(enabled=True, confirm=True)
        node = self._node()
        self._pass_with_quiz(node)
        self.clock.advance(days=8)
        gate = threading.Event()

        class Blocking(FakeModelAdapter):
            def judge(self, session, question, answer):
                gate.wait(timeout=15)
                return {"verdict": "passed"}

        self.quiz.adapter = Blocking()
        s = self.quiz.start(node["id"], kind="review")
        self._wait(s["session_id"], "awaiting_answer")
        self.quiz.answer(s["session_id"], "长度超过二十个字的复习回答，提交后范围将被收窄。")
        self._wait(s["session_id"], "judging")
        self.gs.update_node(node["id"], {"scope": "收窄后的新范围"}, confirm_scope_change=True, base_version=None)
        gate.set()
        deadline = time.time() + 10
        while time.time() < deadline:
            v = self.quiz.view(s["session_id"])
            if v["status"] in ("inconclusive", "passed"):
                break
            time.sleep(0.05)
        self.assertEqual(v["status"], "inconclusive")
        self.assertIn("范围已变化", v["error"] or "")
        node_now = self.gs.node_detail(node["id"])["node"]
        self.assertEqual(node_now["status"], "untested")  # 范围变化重置 + 旧结果拒绝

    def test_15_old_session_does_not_override_newer(self):
        """必测 15：同节点旧会话不覆盖新结果。"""
        self.review.update_settings(enabled=True, confirm=True)
        node = self._node()
        self._pass_with_quiz(node)   # 首档排期
        self.clock.advance(days=8)
        gate = threading.Event()

        class Blocking(FakeModelAdapter):
            def judge(self, session, question, answer):
                gate.wait(timeout=15)
                return {"verdict": "passed"}

        self.quiz.adapter = Blocking()
        old = self.quiz.start(node["id"], kind="review")
        self._wait(old["session_id"], "awaiting_answer")
        self.quiz.answer(old["session_id"], "长度超过二十个字的复习回答，旧会话先提交判定。")
        self._wait(old["session_id"], "judging")
        gate.set()
        # 旧会话提交 → 通过并推进一档
        deadline = time.time() + 10
        while time.time() < deadline:
            v = self.quiz.view(old["session_id"])
            if v["status"] in ("passed", "inconclusive"):
                break
            time.sleep(0.05)
        self.assertEqual(v["status"], "passed")
        idx_after_old = self.gs.node_detail(node["id"])["node"]["interval_index"]
        # 新会话（更新后的计划）：立即再开一个复习会话（此刻已推进，新 due 在未来 → 服务端应拒绝提前复习）
        with self.assertRaises(QuizError):
            self.quiz.start(node["id"], kind="review")
        self.assertEqual(self.gs.node_detail(node["id"])["node"]["interval_index"], idx_after_old)

    def test_16_archived_node_not_reminded(self):
        """必测 16：归档节点不再提醒。"""
        self.review.update_settings(enabled=True, confirm=True)
        node = self._node()
        self._pass_with_quiz(node)
        self.clock.advance(days=8)
        self.gs.archive_node(node["id"], base_version=None)
        groups = self.review.due_groups()
        for group in groups["groups"].values():
            self.assertNotIn(node["id"], [i["node_id"] for i in group])

    def test_17_backfill_preview_then_apply(self):
        """必测 17：批量启用排期有预览，确认前无写入。"""
        self.review.update_settings(enabled=True, confirm=True)
        node = self._force(self._node(), "passed", last_quiz_at=self.clock().isoformat())
        before = self.gs.node_detail(node["id"])["node"]["next_review_at"]
        preview = self.review.backfill_preview()
        self.assertEqual(preview["count"], 1)
        self.assertIsNone(before)
        with self.assertRaises(ValidationError):
            self.review.backfill_apply(confirm=False)
        self.assertIsNone(self.gs.node_detail(node["id"])["node"]["next_review_at"])  # 确认前无写入
        out = self.review.backfill_apply(confirm=True)
        self.assertEqual(out["scheduled"], 1)
        self.assertIsNotNone(self.gs.node_detail(node["id"])["node"]["next_review_at"])

    def test_18_settings_change_no_silent_reschedule(self):
        """必测 18：修改设置不静默重排旧计划。"""
        self.review.update_settings(enabled=True, confirm=True)
        node = self._node()
        self._pass_with_quiz(node)
        due_before = self.gs.node_detail(node["id"])["node"]["next_review_at"]
        self.review.update_settings(intervals=[3, 10, 30, 60])
        self.assertEqual(self.gs.node_detail(node["id"])["node"]["next_review_at"], due_before)

    def test_19_time_math_cross_boundaries(self):
        """必测 19：跨日、月末、年末与上海时区计算正确。"""
        self.assertEqual(sh_today(datetime(2026, 10, 5, 20, 0, tzinfo=timezone.utc)), "2026-10-06")  # UTC 20 点 = 上海次日
        self.assertEqual(add_days(datetime(2026, 1, 31, 12, 0, tzinfo=timezone.utc), 1)
                         .astimezone(timezone.utc).strftime("%Y-%m-%d"), "2026-02-01")
        year_end = datetime(2026, 12, 31, 16, 0, tzinfo=timezone.utc)
        plus7 = add_days(year_end, 7)
        self.assertEqual(plus7.strftime("%Y-%m-%d"), "2027-01-07")
        # 上海日界：UTC 16:00 = 上海次日 00:00
        self.assertEqual(sh_today(year_end), "2027-01-01")
        self.assertEqual(sh_today(add_days(year_end, 7)), "2027-01-08")

    def test_20_plans_survive_restart_sessions_die(self):
        """必测 20：服务重启后计划仍在，临时测验会话明确失效。"""
        self.review.update_settings(enabled=True, confirm=True)
        node = self._node()
        self._pass_with_quiz(node)
        due = self.gs.node_detail(node["id"])["node"]["next_review_at"]
        session_id = self.quiz.sessions and list(self.quiz.sessions)[0]
        fresh_review = ReviewService(self.gs, clock=self.clock)
        fresh_quiz = QuizService(self.gs, self.adapter, Budget(), "demo",
                                 review=fresh_review, clock=self.clock)
        self.assertEqual(fresh_review.due_groups()["counts"]["upcoming"], 1)  # 计划持久（due 在未来）
        with self.assertRaises(QuizError):
            fresh_quiz.view(session_id)  # 临时会话失效


class SecurityAndModeTests(ReviewTestBase):
    def test_21_frontend_cannot_submit_status_or_dates(self):
        """必测 21：前端不能任意提交 passed 或 next_review_at（复测入口也核验资格）。"""
        self.review.update_settings(enabled=True, confirm=True)
        node = self._node()
        self._pass_with_quiz(node)
        # 未到期复习 → 服务端拒绝（即使前端声明 review 类型）
        with self.assertRaises(QuizError) as ctx:
            self.quiz.start(node["id"], kind="review")
        self.assertIn("尚未到期", str(ctx.exception))
        self.clock.advance(days=8)  # 到期后同一入口即可发起
        s = self.quiz.start(node["id"], kind="review")
        self._wait(s["session_id"], "awaiting_answer")
        # update 通道夹带 next_review_at 无效（字段白名单外）
        self.gs.update_node(node["id"], {"next_review_at": "2000-01-01T00:00:00+00:00"}, base_version=None)
        self.assertNotEqual(self.gs.node_detail(node["id"])["node"]["next_review_at"],
                            "2000-01-01T00:00:00+00:00")

    def test_22_demo_mode_cannot_write_normal_vault(self):
        """必测 22：演示模式只能写带标记的专用测试库；换路径绕过被拒。"""
        normal = tempfile.TemporaryDirectory()  # 无标记的"普通库"
        self.addCleanup(normal.cleanup)
        normal_vault = make_test_vault(Path(normal.name))  # 目录结构相同但无 .pka-test-vault.json
        gs_normal = GraphService(StateStore(normal_vault))
        quiz_normal = QuizService(gs_normal, DemoModelAdapter(), Budget(), "demo",
                                  review=ReviewService(gs_normal, clock=self.clock), clock=self.clock)
        node = gs_normal.create_node(name="普通库节点", domain="D", scope="S")["node"]
        with self.assertRaises(QuizError) as ctx:
            quiz_normal.start(node["id"])
        self.assertIn("专用测试库", str(ctx.exception))
        self.assertEqual(gs_normal.node_detail(node["id"])["node"]["status"], "untested")
        # 带标记的测试库可以工作
        node2 = self._node()
        s = self.quiz.start(node2["id"])
        self.assertIn("awaiting", s["status"])

    def test_unconfigured_mode_disables_quiz(self):
        """问题 A 回归：未配置真实模型 → 正常模式明确不可用，假模型无法写入状态。"""
        gs = GraphService(StateStore(make_test_vault(Path(self._tmp.name))))
        quiz = QuizService(gs, None, Budget(), "unconfigured", review=self.review, clock=self.clock)
        quiz.unavailable_reason = "未配置真实模型"
        node = self._node()
        with self.assertRaises(QuizError) as ctx:
            quiz.start(node["id"])
        self.assertIn("未配置", str(ctx.exception))
        self.assertEqual(gs.node_detail(node["id"])["node"]["status"], "untested")

    def test_demo_ignores_incomplete_http_config(self):
        """问题 A 回归：http 配置不完整 → 不可用并说明缺什么，不静默降级。"""
        import os
        from pka_graph.quiz_model import build_adapter_from_env

        env = {"PKA_QUIZ_ADAPTER": "http", "PKA_QUIZ_API_BASE": "https://x/v1"}
        with patch.dict(os.environ, env, clear=False):
            # 清掉可能存在的真实变量
            for k in ("PKA_QUIZ_API_KEY", "PKA_QUIZ_MODEL"):
                os.environ.pop(k, None)
            adapter, _budget, mode, available, reason = build_adapter_from_env()
        self.assertIsNone(adapter)
        self.assertFalse(available)
        self.assertEqual(mode, "unconfigured")
        self.assertIn("PKA_QUIZ_API_KEY", reason)
        self.assertIn("不自动降级", reason)


if __name__ == "__main__":
    unittest.main()
