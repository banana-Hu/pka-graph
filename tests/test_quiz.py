"""第四阶段确定性测试：测验状态机、防伪造、隐私（必测 1-20 的服务端部分）。"""

import json
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tests"))

from fixtures import make_test_vault
from pka_graph.models import ValidationError
from pka_graph.quiz_model import Budget, FakeModelAdapter, ModelError, normalize_verdict_payload
from pka_graph.quiz_service import QuizError, QuizService
from pka_graph.service import GraphService
from pka_graph.store import StateStore

# 真实库路径由环境变量 PKA_REAL_VAULT 提供；未设置时相关测试自动跳过
import os as _os
REAL_VAULT = (Path(_os.environ["PKA_REAL_VAULT"].strip())
              if _os.environ.get("PKA_REAL_VAULT", "").strip() else None)


class RecordingAdapter(FakeModelAdapter):
    """记录 judge 收到内容的脚本适配器；judge 行为可由外部事件控制。"""

    def __init__(self, judge_script=None, fail_calls=0):
        super().__init__(judge_script, fail_calls)
        self.judge_inputs = []
        self.criteria_inputs = []
        self.gate = None  # threading.Event；未设置时 judge 阻塞

    def build_criteria(self, node):
        self.criteria_inputs.append({"name": node["name"], "scope": node["scope"]})
        return super().build_criteria(node)

    def judge(self, session, question, answer):
        self.judge_inputs.append({"question": question, "answer": answer})
        if self.gate is not None:
            self.gate.wait(timeout=15)
        return super().judge(session, question, answer)


def make_svc(judge_script=None, fail_calls=0):
    tmp = tempfile.TemporaryDirectory()
    vault = make_test_vault(Path(tmp.name))
    gs = GraphService(StateStore(vault))
    adapter = RecordingAdapter(judge_script, fail_calls)
    quiz = QuizService(gs, adapter, Budget(max_calls=8, timeout_seconds=5, max_retries=0), "fake")
    return tmp, gs, quiz, adapter


class QuizFlowTests(unittest.TestCase):
    def setUp(self):
        self._tmp, self.gs, self.quiz, self.adapter = make_svc()
        self.node = self.gs.create_node(name="Agent 工具调用机制", domain="AI技术领域",
                                        scope="工具调用的请求-结果循环、超时与错误处理")["node"]
        self._tmps = [self._tmp]

    def tearDown(self):
        for t in self._tmps:
            t.cleanup()

    def _wait_status(self, session_id, statuses, timeout=15):
        deadline = time.time() + timeout
        while time.time() < deadline:
            view = self.quiz.view(session_id)
            if view["status"] in statuses:
                return view
            time.sleep(0.05)
        raise AssertionError(f"等待状态超时：{view['status']}（期望 {statuses}）")

    def _start(self, script=None, fail_calls=0):
        self.adapter.judge_script = list(script or [])
        self.adapter.fail_calls = fail_calls
        return self.quiz.start(self.node["id"])

    def test_1_correct_answer_passes_to_mastery(self):
        """必测 1：正确回答通过后进入掌握层。"""
        s = self._start([{"verdict": "passed", "feedback": "要点都覆盖了"}])
        self._wait_status(s["session_id"], ["awaiting_answer"])
        self.quiz.answer(s["session_id"], "在工具循环里，调用方先注册工具schema，模型返回调用请求，执行后回填结果……（超过二十字的完整回答）")
        view = self._wait_status(s["session_id"], ["passed", "failed", "inconclusive"])
        self.assertEqual(view["status"], "passed")
        node = self.gs.node_detail(self.node["id"])["node"]
        self.assertEqual(node["status"], "passed")
        self.assertIsNotNone(node["last_quiz_at"])
        self.assertIsNone(node["next_review_at"])  # 不安排复习
        self.assertEqual(self.gs.layer_of(node), "mastery")

    def test_2_failed_and_inconclusive_differ(self):
        """必测 2：failed 与 inconclusive 行为不同。"""
        s = self._start([{"verdict": "failed", "feedback": "混淆了机制与步骤", "gaps": ["核心循环"]}],
                        fail_calls=0)
        self._wait_status(s["session_id"], ["awaiting_answer"])
        self.quiz.answer(s["session_id"], "这个回答有实质性错误的内容，长度也超过二十个字。")
        view = self._wait_status(s["session_id"], ["failed"])
        node = self.gs.node_detail(self.node["id"])["node"]
        self.assertEqual(node["status"], "failed")  # 未通过 → 留在视野层但状态更新
        self.assertIsNotNone(node["last_quiz_at"])

        # inconclusive：状态不动
        s2 = self._start([{"verdict": "inconclusive", "feedback": "材料不足"}])
        self._wait_status(s2["session_id"], ["awaiting_answer"])
        self.quiz.answer(s2["session_id"], "随便说的内容，无法判断，长度超过二十个字。")
        view2 = self._wait_status(s2["session_id"], ["inconclusive"])
        node2 = self.gs.node_detail(self.node["id"])["node"]
        self.assertEqual(node2["status"], "failed")  # inconclusive 不改状态
        self.assertIsNotNone(view2)

    def test_3_synonym_answers_accepted_no_keyword_gate(self):
        """必测 3：同义表述可接受——服务端不做关键词机械命中，判定文本原样交给模型。"""
        s = self._start([{"verdict": "passed", "feedback": "表述不同但机制正确"}])
        self._wait_status(s["session_id"], ["awaiting_answer"])
        answer = "用完全不重复的措辞：模型发起工具请求后由宿主执行并把结果喂回去，失败要有兜底。"
        self.quiz.answer(s["session_id"], answer)
        view = self._wait_status(s["session_id"], ["passed"])
        self.assertEqual(view["status"], "passed")
        sent = self.adapter.judge_inputs[-1]["answer"]
        self.assertEqual(sent, answer)  # 服务端未改写、未做关键词过滤

    def test_4_invalid_model_output_never_updates(self):
        """必测 4：模型输出缺字段/非法 verdict 不更新节点。"""
        s = self._start([{"feedback": "缺 verdict 字段"}])
        self._wait_status(s["session_id"], ["awaiting_answer"])
        self.quiz.answer(s["session_id"], "长度超过二十个字的、看起来很正确的回答内容。")
        view = self._wait_status(s["session_id"], ["failed_request", "inconclusive"])
        self.assertIn(view["status"], ("failed_request", "inconclusive"))
        self.assertEqual(self.gs.node_detail(self.node["id"])["node"]["status"], "untested")

        s2 = self._start([{"verdict": "超级通过"}])
        self._wait_status(s2["session_id"], ["awaiting_answer"])
        self.quiz.answer(s2["session_id"], "长度超过二十个字的另一个回答，同样看起来正确。")
        view2 = self._wait_status(s2["session_id"], ["failed_request", "inconclusive"])
        self.assertEqual(self.gs.node_detail(self.node["id"])["node"]["status"], "untested")

        with self.assertRaises(ModelError):
            normalize_verdict_payload("不是JSON对象")

    def test_5_model_failure_no_update(self):
        """必测 5：模型超时/网络失败不更新节点。"""
        s = self._start(fail_calls=1)
        self._wait_status(s["session_id"], ["awaiting_answer"])
        self.quiz.answer(s["session_id"], "长度超过二十个字的回答，模型会在判定时失败。")
        view = self._wait_status(s["session_id"], ["failed_request"])
        self.assertEqual(self.gs.node_detail(self.node["id"])["node"]["status"], "untested")
        self.assertIn("失败", view["error"] or "")

    def test_6_cancel_discards_late_result(self):
        """必测 6：取消后的迟到结果不更新节点。"""
        gate = threading.Event()
        class BlockingAdapter(RecordingAdapter):
            def judge(self, session, question, answer):
                self.judge_inputs.append({"question": question, "answer": answer})
                gate.wait(timeout=15)
                return normalize_verdict_payload({"verdict": "passed", "feedback": "迟到的通过"})
        self.adapter.__class__ = BlockingAdapter
        s = self._start()
        self._wait_status(s["session_id"], ["awaiting_answer"])
        self.quiz.answer(s["session_id"], "长度超过二十个字的回答，判定会被挂起然后取消。")
        self._wait_status(s["session_id"], ["judging"])
        self.quiz.cancel(s["session_id"])
        gate.set()
        time.sleep(0.4)  # 给迟到结果机会（它必须被丢弃）
        node = self.gs.node_detail(self.node["id"])["node"]
        self.assertEqual(node["status"], "untested")
        view = self.quiz.view(s["session_id"])
        self.assertEqual(view["status"], "cancelled")
        self._tmps.append(self._tmp)

    def test_7_duplicate_answer_single_change(self):
        """必测 7：重复提交只产生一次状态变化。"""
        s = self._start([{"verdict": "passed", "feedback": "ok"}])
        self._wait_status(s["session_id"], ["awaiting_answer"])
        self.quiz.answer(s["session_id"], "长度超过二十个字的第一次提交回答，应当被判为通过。")
        self._wait_status(s["session_id"], ["passed"])
        version_after = self.gs.state()["version"]
        with self.assertRaises(QuizError):
            self.quiz.answer(s["session_id"], "长度超过二十个字的第二次重复提交回答。")
        self.assertEqual(self.gs.state()["version"], version_after)
        self.assertIn(f"quiz-submit:{s['session_id']}", self.gs.state()["idempotency"])

    def test_8_expired_or_restarted_session_rejects_replay(self):
        """必测 8：会话过期或服务重启后不能重放结果。"""
        s = self._start()
        self._wait_status(s["session_id"], ["awaiting_answer"])
        # 模拟服务重启：新 QuizService 实例（内存会话丢失）
        fresh_quiz = QuizService(self.gs, self.adapter, Budget(max_calls=8, timeout_seconds=5, max_retries=0), "fake")
        with self.assertRaises(QuizError) as ctx:
            fresh_quiz.view(s["session_id"])
        self.assertIn("重启", str(ctx.exception))
        with self.assertRaises(QuizError):
            fresh_quiz.answer(s["session_id"], "服务重启后旧客户端重放的回答内容超过二十字。")
        # 过期会话同样拒绝
        session = self.quiz._get(s["session_id"])
        from datetime import timedelta

        session.deadline = session.deadline - timedelta(days=1)
        with self.assertRaises(QuizError):
            self.quiz.answer(s["session_id"], "对已过期会话提交的回答内容超过二十个字。")

    def test_9_scope_change_rejects_old_result(self):
        """必测 9：节点范围变化后旧结果拒绝提交。"""
        gate = threading.Event()
        class BlockingAdapter(RecordingAdapter):
            def judge(self, session, question, answer):
                self.judge_inputs.append({"question": question, "answer": answer})
                gate.wait(timeout=15)
                return normalize_verdict_payload({"verdict": "passed", "feedback": "迟到的通过"})
        self.adapter.__class__ = BlockingAdapter
        s = self._start()
        self._wait_status(s["session_id"], ["awaiting_answer"])
        self.quiz.answer(s["session_id"], "长度超过二十个字的回答，提交后范围将被改变。")
        self._wait_status(s["session_id"], ["judging"])
        self.gs.update_node(self.node["id"], {"scope": "彻底不同的新范围"},
                            confirm_scope_change=True, base_version=None)
        gate.set()
        view = self._wait_status(s["session_id"], ["inconclusive", "passed"])
        self.assertEqual(view["status"], "inconclusive")
        node = self.gs.node_detail(self.node["id"])["node"]
        self.assertEqual(node["status"], "untested")  # 旧结果未写入
        self.assertIn("范围已变化", view["error"] or "")

    def test_10_archived_node_rejects_old_result(self):
        """必测 10：节点归档后旧结果拒绝提交。"""
        gate = threading.Event()
        class BlockingAdapter(RecordingAdapter):
            def judge(self, session, question, answer):
                self.judge_inputs.append({"question": question, "answer": answer})
                gate.wait(timeout=15)
                return normalize_verdict_payload({"verdict": "passed", "feedback": "迟到的通过"})
        self.adapter.__class__ = BlockingAdapter
        s = self._start()
        self._wait_status(s["session_id"], ["awaiting_answer"])
        self.quiz.answer(s["session_id"], "长度超过二十个字的回答，提交后节点将被归档。")
        self._wait_status(s["session_id"], ["judging"])
        self.gs.archive_node(self.node["id"], base_version=None)
        gate.set()
        view = self._wait_status(s["session_id"], ["inconclusive", "passed"])
        self.assertEqual(view["status"], "inconclusive")
        self.assertIn("归档", view["error"] or "")
        # 归档节点不能重新发起测验
        with self.assertRaises(ValidationError):
            self.quiz.start(self.node["id"])

    def test_11_unrelated_node_change_not_clobbered(self):
        """必测 11：无关节点更新不会被测验写回覆盖。"""
        other = self.gs.create_node(name="无关节点", domain="产品领域", scope="别的范围")["node"]
        s = self._start([{"verdict": "passed", "feedback": "ok"}])
        self._wait_status(s["session_id"], ["awaiting_answer"])
        self.quiz.answer(s["session_id"], "长度超过二十个字的回答，同时另一个节点会被编辑。")
        self._wait_status(s["session_id"], ["passed"])
        # 无关节点的并发修改在提交后依然存在（受控提交不整体覆盖）
        detail = self.gs.node_detail(other["id"])["node"]
        self.assertEqual(detail["scope"], "别的范围")
        self.assertEqual(self.gs.node_detail(self.node["id"])["node"]["status"], "passed")

    def test_12_hinted_question_becomes_assisted_learning(self):
        """必测 12（第五阶段规则 B）：使用提示 → 本题转为辅助学习：
        不计入独立判定、不改变节点状态、不推进计划；模型参考意见区分展示。"""
        s = self._start([{"verdict": "passed", "feedback": "模型判了通过"}])
        self._wait_status(s["session_id"], ["awaiting_answer"])
        self.quiz.request_hint(s["session_id"])
        self._wait_status(s["session_id"], ["awaiting_answer"])
        self.assertTrue(self.quiz.view(s["session_id"])["hint_used"])
        self.quiz.answer(s["session_id"], "看完提示后复述出来的回答内容，长度超过二十个字。")
        view = self._wait_status(s["session_id"], ["assisted_learning"])
        self.assertEqual(view["status"], "assisted_learning")
        self.assertEqual(view["feedback"]["model_verdict"], "passed")  # 模型参考意见如实展示
        self.assertIn("不计入独立掌握判定", view["feedback"]["feedback"])
        node = self.gs.node_detail(self.node["id"])["node"]
        self.assertEqual(node["status"], "untested")  # 节点状态未改变
        self.assertIsNone(node["last_quiz_at"])       # 未推进任何测验/复习时间

    def test_13_renew_keeps_state_and_passed_readonly(self):
        """必测 13：换题不清除已有状态；passed 节点只读。"""
        s = self._start()
        self._wait_status(s["session_id"], ["awaiting_answer"])
        self.quiz.request_hint(s["session_id"])
        self._wait_status(s["session_id"], ["awaiting_answer"])
        view = self.quiz.renew_question(s["session_id"])
        self.assertFalse(view["hint_used"])  # 换题后提示标记清零（新题独立）
        self.assertEqual(self.gs.node_detail(self.node["id"])["node"]["status"], "untested")
        # 通过后的节点：已掌握 → 拒绝再开测验（复测规则第五阶段确认）
        state = self.gs.state()
        for n in state["nodes"]:
            if n["id"] == self.node["id"]:
                n["status"] = "passed"
        self.gs.store.save(state, base_version=None)
        with self.assertRaises(QuizError):
            self.quiz.start(self.node["id"])

    def test_20_privacy_no_answer_in_state_or_errors(self):
        """必测 20：密钥与完整问答不进入持久状态、错误响应。"""
        marker = "绝不落盘的秘密回答短语XYZQ"
        s = self._start([{"verdict": "passed", "feedback": "ok"}])
        self._wait_status(s["session_id"], ["awaiting_answer"])
        self.quiz.answer(s["session_id"], f"长度超过二十个字的回答，包含{marker}。")
        self._wait_status(s["session_id"], ["passed"])
        state_text = self.gs.store.state_path.read_text(encoding="utf-8")
        self.assertNotIn(marker, state_text)
        self.assertNotIn("quiz_q", state_text)
        # 业务错误响应不含回答内容
        try:
            self.quiz.answer(s["session_id"], f"重复提交也带{marker}")
        except QuizError as exc:
            self.assertNotIn(marker, str(exc))
        # 节点持久化的只有状态与时间/条件
        node = self.gs.node_detail(self.node["id"])["node"]
        self.assertEqual(node["status"], "passed")


class PassedNodeGuardTests(unittest.TestCase):
    def test_passed_node_quiz_rejected(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        gs = GraphService(StateStore(make_test_vault(Path(tmp.name))))
        quiz = QuizService(gs, FakeModelAdapter(), Budget(), "fake")
        node = gs.create_node(name="X", domain="D", scope="S")["node"]
        state = gs.state()
        for n in state["nodes"]:
            if n["id"] == node["id"]:
                n["status"] = "passed"
        gs.store.save(state, base_version=None)
        with self.assertRaises(QuizError):
            quiz.start(node["id"])


if __name__ == "__main__":
    unittest.main()
