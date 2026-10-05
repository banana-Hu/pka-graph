"""真实库关联修复预览（只读）：8 条 note_links 逐条给出解析结果。

不读密钥、不写真实库任何文件、不改 state.json；只读 state.json 与笔记文件，
把每条关联的解析结果（原路径 / 状态 / 新位置 / 说明）写成预览报告。
按审查要求：本次只出预览，不提交任何 state.json 变更；未定位项如实保留。
"""
import os
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from pka_graph.service import GraphService
from pka_graph.store import StateStore

REAL_VAULT = Path(Path(os.environ.get("PKA_REAL_VAULT", "")))
OUT = ROOT / "运行反馈" / "20261003-关联修复预览.md"


def main() -> int:
    store = StateStore(REAL_VAULT, allow_real_vault=True)   # 仅读取；不调用任何写路径
    svc = GraphService(store)
    state = svc.state()
    lines = [
        "# 笔记关联修复预览（只读，未提交任何变更）",
        "",
        f"生成时间：{datetime.now().isoformat(timespec='seconds')}",
        f"知识库：{REAL_VAULT}",
        "",
        "| # | 关联 | 登记路径 | 解析状态 | 当前位置 | 说明 |",
        "|---|---|---|---|---|---|",
    ]
    summary = {"ok": 0, "missing": 0, "ambiguous": 0, "identity_conflict": 0, "other": 0}
    for i, link in enumerate(state["note_links"], 1):
        res = svc.resolve_note_link(link)
        summary[res["status"]] = summary.get(res["status"], 0) + 1
        node = next((n for n in state["nodes"] if n["id"] == link["node_id"]), None)
        lines.append(
            f"| {i} | {node['name'] if node else link['node_id'][:16]} "
            f"（{link['ref'].get('kind')}） "
            f"| `{link['ref'].get('path')}` "
            f"| {res['status']} "
            f"| `{res.get('path') or '—'}` "
            f"| {res.get('message') or '—'} |"
        )
    lines += ["", "## 汇总", ""]
    for k, v in summary.items():
        lines.append(f"- {k}: {v}")
    lines += [
        "",
        "说明：解析只在读取时发生（TTL 缓存索引），本预览未把新路径写回 state.json；",
        "持久化关联更新须经受控服务提交，由人工确认后执行。",
    ]
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text("\n".join(lines), encoding="utf-8")
    print(f"预览已生成：{OUT}")
    print("汇总:", summary)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
