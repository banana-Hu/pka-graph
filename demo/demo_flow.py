"""第二阶段演示：扫描 → 预览接入 → 创建节点 → 编辑关联 → 模拟冲突 → 归档恢复。

对仓库测试知识库（test_vault/）实际运行，输出全部为真实执行结果。
"""

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from pka_graph.service import ConfirmationRequired, GraphService
from pka_graph.store import StateStore, VersionConflict

TEST_VAULT = Path(__file__).resolve().parents[1] / "test_vault"
REAL_VAULT = Path(Path(os.environ.get("PKA_REAL_VAULT", "")))


def step(n, title):
    print(f"\n{'=' * 62}\n步骤 {n}：{title}\n{'=' * 62}")


def main():
    store = StateStore(TEST_VAULT)
    # 演示可重复执行：每次从空状态开始（测试库 state.json 是再生数据，笔记文件不动）
    if store.exists():
        store.state_path.unlink()
    svc = GraphService(store, real_vault=REAL_VAULT)

    def find_node(name):
        for n in svc.list_nodes(name=name):
            return n
        return None

    step(1, "只读扫描真实知识库（dry-run，绝不写入）")
    real = svc.scan("real")
    print(f"真实库目标笔记 {real['stats']['target_total']} 篇"
          f"（正式 {real['stats']['formal']} / 候选 {real['stats']['candidate']}），"
          f"有 ID {real['stats']['with_id']}，重复 ID {len(real['duplicate_ids'])}，"
          f"跳过非目标 {real['skipped_non_target']}")

    step(2, "扫描测试知识库")
    test = svc.scan("test")
    for e in test["target_notes"]:
        print(f"  [{e['kind']:<9}] {e['path']}"
              f"  ID={e['note_id'] or '—（登记路径）'}"
              f"{'  ⚠ 重复ID' if e.get('duplicate_of') else ''}")

    step(3, "创建节点（一律 untested）")
    node = find_node("Agent 工具调用机制")
    if node is None:
        node = svc.create_node(
            name="Agent 工具调用机制",
            domain="AI技术领域",
            scope="能解释工具调用的请求-结果循环、超时与错误处理、权限边界",
            request_id="demo-create-1",
        )["node"]
    print(f"  {node['id']}  {node['name']}  status={node['status']}")

    step(4, "预览接入（两篇笔记：有 ID 的正式笔记 + 无 ID 的测试笔记）")
    paths = ["正式笔记/04-Unit1-为什么需要Harness.md", "候选笔记/无稳定ID笔记-提示词工程.md"]
    assignments = [{"path": p, "node_id": node["id"]} for p in paths]
    preview = svc.import_preview(paths, assignments, which="test")
    for item in preview["plan"]:
        print(f"  {item['path']}")
        print(f"    ID={item['note_id'] or '—'} 指纹={item['fingerprint'][:16]}… 动作={[a['type'] for a in item['actions']]}")
        for w in item["warnings"]:
            print(f"    ⚠ {w}")

    step(5, "确认接入（只写测试库管理区）")
    result = svc.import_apply(paths, assignments, request_id="demo-import-1", base_version=svc.state()["version"])
    print(f"  登记 {len(result['registered'])} 条：{[r['registry_id'] for r in result['registered']]}")
    print(f"  关联 {len(result['note_links'])} 条：{[l['link_id'] for l in result['note_links']]}")

    step(6, "编辑：改名（掌握状态不变）+ 加关系")
    svc.update_node(node["id"], {"name": "Agent 工具调用与权限边界"}, base_version=svc.state()["version"])
    other = find_node("Harness 工程方法")
    if other is None:
        other = svc.create_node(name="Harness 工程方法", domain="AI技术领域", scope="Harness 的分层与交接")["node"]
    if not any(r["relation"] == "extends" for r in svc.node_detail(node["id"])["relations"]):
        svc.add_relation(node["id"], other["id"], "extends", base_version=svc.state()["version"])
    detail = svc.node_detail(node["id"])
    print(f"  改名后 status={detail['node']['status']}（未变）")
    print(f"  关联笔记 {len(detail['note_links'])} 条，关系 {len(detail['relations'])} 条（extends → {other['name']}）")

    step(7, "模拟冲突：用过期版本提交编辑 → 乐观锁拒绝")
    stale = svc.state()["version"]
    svc.update_node(node["id"], {"name": "并发编辑者甲"}, base_version=stale)
    try:
        svc.update_node(node["id"], {"name": "并发编辑者乙"}, base_version=stale)
        print("  ❌ 不应到达这里")
    except VersionConflict as exc:
        print(f"  ✓ 被拒绝：{exc}")

    step(8, "修改知识范围：未确认 → 拒绝并提示；确认后 → 回 untested")
    try:
        svc.update_node(node["id"], {"scope": "彻底不同的范围：只讲提示词工程"}, base_version=svc.state()["version"])
    except ConfirmationRequired as exc:
        print(f"  ✓ 需要确认：{exc.preview['effect']}")
    out = svc.update_node(
        node["id"], {"scope": "彻底不同的范围：只讲提示词工程"},
        confirm_scope_change=True, base_version=svc.state()["version"],
    )
    print(f"  ✓ 已确认：status={out['node']['status']}，content_version=v{out['node']['content_version']}")

    step(9, "归档 → 恢复（恢复后一律 untested）")
    svc.archive_node(node["id"], reason="manual", base_version=svc.state()["version"])
    detail = svc.node_detail(node["id"])
    print(f"  已归档：archived={detail['node']['archived']}，原状态={detail['node']['archive_note']['status_before']}")
    restored = svc.restore_node(node["id"], base_version=svc.state()["version"])["node"]
    print(f"  已恢复：archived={restored['archived']}，status={restored['status']}")

    step(10, "收尾核验：真实知识库零写入 + 测试库状态文件落盘")
    import hashlib

    def snap(root):
        return {str(p): hashlib.sha256(p.read_bytes()).hexdigest() for p in sorted(Path(root).rglob("*.md"))}
    before = snap(REAL_VAULT)
    svc.scan("real")
    after = snap(REAL_VAULT)
    print(f"  真实库 md 文件 {len(before)} 篇，前后哈希一致：{before == after}")
    state_file = TEST_VAULT / "个人学习状态" / "state.json"
    print(f"  测试库状态文件：{state_file}")
    print(f"  节点 {len(svc.state()['nodes'])}，笔记关联 {len(svc.state()['note_links'])}，"
          f"节点关系 {len(svc.state()['node_links'])}，身份登记 {len(svc.state()['ingest_registry'])}")


if __name__ == "__main__":
    main()
