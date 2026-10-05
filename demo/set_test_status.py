"""受控测试状态入口（TEST ONLY）。

用途：向测试副本知识库的节点写入模拟测验状态（passed/failed/untested），
用于第三阶段演示"通过后变色/层间移动"与自动化测试。
它不是产品功能：正常管理界面没有"直接标记通过"的按钮；
正式掌握状态在第四阶段只能由真实测验入口产生。

用法：
  python demo/set_test_status.py --vault <测试库目录> --node <node-id> --status passed --i-know-this-is-test-data
不带 --i-know-this-is-test-data 标志时拒绝执行。
"""

import argparse
import sys
from pathlib import Path

WORKSPACE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(WORKSPACE))

from pka_graph.models import ValidationError  # noqa: E402
from pka_graph.store import StateStore  # noqa: E402


def main() -> None:
    parser = argparse.ArgumentParser(description="TEST ONLY：向测试副本写入模拟测验状态")
    parser.add_argument("--vault", required=True)
    parser.add_argument("--node", required=True)
    parser.add_argument("--status", required=True, choices=["passed", "failed", "untested"])
    parser.add_argument("--i-know-this-is-test-data", action="store_true")
    parser.add_argument("--due-in-days", type=int, default=None,
                        help="TEST ONLY：同时设置 next_review_at 为 N 天后（负数=已逾期）并置首档")
    args = parser.parse_args()

    if not args.i_know_this_is_test_data:
        raise SystemExit(
            "拒绝执行：这是受控测试入口，只能作用于测试副本知识库。\n"
            "确认理解后请加 --i-know-this-is-test-data 重新运行。"
        )
    store = StateStore(Path(args.vault))
    state = store.load()
    target = next((n for n in state["nodes"] if n["id"] == args.node), None)
    if target is None:
        raise SystemExit(f"节点不存在：{args.node}")
    target["status"] = args.status
    target["updated_at"] = __import__("pka_graph.models", fromlist=["now_iso"]).now_iso()
    if args.status == "passed":
        # 模拟一次测验留下的时间痕迹（测试数据）
        target["last_quiz_at"] = target["updated_at"]
    if args.due_in_days is not None:
        from datetime import timedelta

        base = __import__("pka_graph.models", fromlist=["now_iso"]).now_iso()
        target["next_review_at"] = (
            datetime.fromisoformat(base) + timedelta(days=args.due_in_days)
        ).isoformat(timespec="seconds")
        target["interval_index"] = 0
    store.save(state, base_version=None)
    print(f"[TEST DATA] 节点 {args.node} 状态已置为 {args.status}（{target['name']}）"
          + (f"，due={target['next_review_at']}" if args.due_in_days is not None else ""))


if __name__ == "__main__":
    main()
