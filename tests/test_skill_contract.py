"""Skill 文档与代码行为一致性（交接检查 A）+ 演示状态来源标注（交接检查 B）。"""

import json
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tests"))

from fixtures import make_test_vault
from pka_graph.quiz_model import Budget, FakeModelAdapter
from pka_graph.quiz_service import QuizError, QuizService
from pka_graph.review_service import ReviewService
from pka_graph.service import GraphService
from pka_graph.store import StateStore

SKILL = Path(__file__).resolve().parents[1] / "skills" / "mastery_quiz" / "掌握判定.md"


class SkillContractTests(unittest.TestCase):
    def test_skill_matches_assisted_learning_semantics(self):
        """交接检查 A：Skill 必须描述辅助学习语义，不得再声称强制判 failed。"""
        text = SKILL.read_text(encoding="utf-8")
        self.assertIn("辅助学习", text)
        self.assertIn("assisted_learning", text)
        self.assertIn("不计入独立掌握判定", text)
        self.assertNotIn("强制降为 failed", text)

    def test_skill_distinguishes_assisted_from_inconclusive(self):
        text = SKILL.read_text(encoding="utf-8")
        self.assertIn("inconclusive", text)
        self.assertIn("两回事", text)


def _pass_via_demo_quiz(gs, quiz, node):
    quiz.adapter.judge_script = [{"verdict": "passed", "feedback": "演示通过"}]
    s = quiz.start(node["id"])
    deadline = time.time() + 10
    while time.time() < deadline:
        if quiz.view(s["session_id"])["status"] == "awaiting_answer":
            break
        time.sleep(0.05)
    quiz.answer(s["session_id"], "长度超过二十个字的演示回答内容，用于验证来源标注是否正确落盘。")
    deadline = time.time() + 10
    while time.time() < deadline:
        v = quiz.view(s["session_id"])
        if v["status"] in ("passed", "failed"):
            return v
        time.sleep(0.05)
    raise AssertionError("判定未完成")


class DemoOriginTests(unittest.TestCase):
    def test_demo_pass_sets_demo_origin(self):
        """交接检查 B：演示模式写入的通过带 status_origin=demo（测试来源可辨识，
        图谱视图与复习条目同样暴露该字段）。"""
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        vault = make_test_vault(Path(tmp.name))
        (vault / ".pka-test-vault.json").write_text(json.dumps({"purpose": "test"}), encoding="utf-8")
        gs = GraphService(StateStore(vault))
        review = ReviewService(gs)
        quiz = QuizService(gs, FakeModelAdapter(), Budget(), "demo", review=review)
        node = gs.create_node(name="演示来源节点", domain="AI技术领域", scope="演示范围")["node"]
        view = _pass_via_demo_quiz(gs, quiz, node)
        self.assertEqual(view["status"], "passed")
        node_now = gs.node_detail(node["id"])["node"]
        self.assertEqual(node_now["status"], "passed")
        self.assertEqual(node_now["status_origin"], "demo")
        g = gs.graph_view()
        gn = next(n for n in g["nodes"] if n["id"] == node["id"])
        self.assertEqual(gn["status_origin"], "demo")

    def test_deleting_marker_does_not_launder_demo_status(self):
        """删除标记后：演示会话被拒绝，既有演示状态保持 demo 来源（不自动洗白，
        且没有任何代码路径把它改写为 real 来源）。"""
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        vault = make_test_vault(Path(tmp.name))
        marker = vault / ".pka-test-vault.json"
        marker.write_text(json.dumps({"purpose": "test"}), encoding="utf-8")
        gs = GraphService(StateStore(vault))
        review = ReviewService(gs)
        quiz = QuizService(gs, FakeModelAdapter(), Budget(), "demo", review=review)
        node = gs.create_node(name="待洗白测试", domain="AI技术领域", scope="演示范围")["node"]
        _pass_via_demo_quiz(gs, quiz, node)
        self.assertEqual(gs.node_detail(node["id"])["node"]["status_origin"], "demo")
        marker.unlink()  # 删除标记（试图"转正"）
        with patch.object(quiz, "mode", "real"):
            self.assertEqual(gs.node_detail(node["id"])["node"]["status_origin"], "demo")
        # demo 模式下无标记的库：新会话仍被拒
        with self.assertRaises(QuizError):
            quiz.start(node["id"])


if __name__ == "__main__":
    unittest.main()
