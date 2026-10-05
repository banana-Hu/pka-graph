# -*- coding: utf-8 -*-
"""真实知识库接入（经用户批准的变更清单）。

用法：
  python demo/real_vault_setup.py            # dry-run（默认）：只扫描与预览，不写任何文件
  python demo/real_vault_setup.py --apply    # 执行：建管理区 + 5 节点 + 关联 + frontmatter 注入
  python demo/real_vault_setup.py --rollback # 回退：从备份还原笔记 + 删除管理区

范围（已批准）：
- 新增目录 个人学习状态/（state.json、backups/、note_backups/、migration_manifest.json）
- 5 个 untested 节点 + 5 条关联
- 5 篇笔记 frontmatter 各加一行 mastery_status: untested（正文不动，逐篇备份）
回退：--rollback 还原全部笔记并删除管理区，等价于从未接入。
"""

import argparse
import json
import shutil
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

REAL_VAULT = Path(Path(os.environ.get("PKA_REAL_VAULT", "")))
STATE_DIR = "个人学习状态"

# 已批准的 5 篇目标笔记与节点定义
TARGETS = [
    {
        "path": "01-AI技术领域/正式笔记/Hermes实训班/04-Unit1-为什么需要Harness.md",
        "name": "Harness 的定位与价值",
        "scope": "能解释 Agent 工程中 Harness 存在的必要性：它解决什么可靠性问题、"
                 "与模型能力的分工边界、缺少 Harness 时的典型失败模式。",
    },
    {
        "path": "01-AI技术领域/候选笔记/Hermes实训班/01-从提问到可靠交付的五层演进.md",
        "name": "从提问到可靠交付的五层演进",
        "scope": "能说明从直接提问到可靠交付的五个层级各自解决什么问题、"
                 "相邻层之间的演进动机，以及如何判断一个任务需要停在哪一层。",
    },
    {
        "path": "02-产品领域/候选笔记/2026-10-01-书单下载/从零到一：用七个问题审视创业判断.md",
        "name": "创业判断的七个问题框架",
        "scope": "能用七个问题框架审视一个创业或产品判断：覆盖技术独特性、"
                 "市场切入、时机、垄断与分销等维度，并指出该框架的适用边界。",
    },
    {
        "path": "12-心理学领域/候选笔记/2026-10-01-书单下载/影响力：识别六种影响判断的线索.md",
        "name": "影响力的六种影响线索",
        "scope": "能识别互惠、承诺一致、社会认同、喜好、权威、稀缺六种影响线索的现实表现，"
                 "并说明它们在什么条件下更易生效（不要求复述实验史）。",
    },
    {
        "path": "04-内容创作领域/候选笔记/2026-10-01-书单下载/数据表达：先确定判断再组织图表.md",
        "name": "数据表达的判断先行原则",
        "scope": "能解释为什么图表要先确定判断再组织数据，并能针对给定数据"
                 "选择匹配判断意图的表达方式、指出常见误用。",
    },
    # ===== 第二批（2026-10-02 用户批准）：Agent 工程主线 Unit2-4 =====
    {
        "path": "01-AI技术领域/正式笔记/Hermes实训班/05-Unit2-让项目对Agent可见可接手.md",
        "name": "Agent-ready 项目的判断问题",
        "scope": "能用五个问题判断一个项目是否 Agent-ready（这是什么系统、怎么组织、"
                 "怎么启动运行、如何验证、当前状态），并说明让项目对 Agent 可见可接手的关键动作。",
    },
    {
        "path": "01-AI技术领域/正式笔记/Hermes实训班/06-Unit3-让Agent在正确边界内执行.md",
        "name": "Agent 执行的四个边界",
        "scope": "能说明任务、状态、工具、过程四个边界各自约束什么，解释 WIP=1"
                 "（当前任务通过前不开下一个）的必要性，以及四边界如何共同构成任务级控制层。",
    },
    {
        "path": "01-AI技术领域/正式笔记/Hermes实训班/07-Unit4-让完成可验证结果可交接.md",
        "name": "完成的证据化判定与交接",
        "scope": "能解释为什么「Agent 说完成」不等于工程完成，列举可验证完成所需要素"
                 "（终止标准、运行时信号、端到端路径、清洁状态、交接协议）并说明如何据此判定与交接。",
    },
]


def _load_note_text(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def dry_run() -> int:
    print("=== DRY-RUN（不写任何文件）===")
    print(f"真实库：{REAL_VAULT}")
    state_dir = REAL_VAULT / STATE_DIR
    print(f"管理区现状：{'已存在（需人工检查冲突）' if state_dir.exists() else '不存在（干净）'}")
    print()
    print(f"将创建 {len(TARGETS)} 个节点与其关联笔记：")
    ok = 0
    for t in TARGETS:
        note = REAL_VAULT / t["path"]
        exists = note.exists()
        text = _load_note_text(note) if exists else ""
        has_field = "mastery_status:" in text
        status = "OK" if exists and not has_field else ("已存在 mastery_status（跳过注入）" if has_field else "缺失")
        if exists and not has_field:
            ok += 1
        print(f"  [{status}] {t['path']}")
        print(f"        节点：{t['name']}")
        print(f"        范围：{t['scope'][:60]}…")
    print()
    print(f"可注入：{ok}/{len(TARGETS)} 篇")
    print()
    print("注入预览（以第一篇为例，只加一行）：")
    sample = REAL_VAULT / TARGETS[0]["path"]
    if sample.exists():
        text = _load_note_text(sample)
        lines = text.splitlines(keepends=True)
        end = next(i for i in range(1, len(lines)) if lines[i].strip() == "---")
        preview = "".join(lines[:end]) + "mastery_status: untested\n" + "".join(lines[end:end + 2])
        print("  " + preview.replace("\n", "\n  ")[:400])
    print()
    print("回退方式：python demo/real_vault_setup.py --rollback")
    return 0


def apply() -> int:
    from pka_graph.mirror import apply_status, note_backup_dir
    from pka_graph.service import GraphService
    from pka_graph.store import StateStore

    state_dir = REAL_VAULT / STATE_DIR
    store = StateStore(REAL_VAULT, allow_real_vault=True)
    gs = GraphService(store)

    # 幂等：已关联的笔记跳过（支持分批接入）
    state = store.load() if state_dir.exists() else None
    linked = {l["ref"].get("path") for l in (state or {"note_links": []})["note_links"]}
    todo = [t for t in TARGETS if t["path"] not in linked]
    if not todo:
        print("全部目标已接入，无需操作。")
        return 0
    print(f"待接入 {len(todo)} 篇（已接入 {len(linked)} 篇将跳过）：")
    for t in todo:
        print(f"  - {t['path']}")

    created = []
    injected = []
    try:
        # 步骤 1：逐篇注入 frontmatter（每篇先备份）
        for t in todo:
            note = REAL_VAULT / t["path"]
            if not note.exists():
                raise RuntimeError(f"目标笔记不存在：{t['path']}")
            result = apply_status(note, "untested", store=store)
            injected.append(t["path"])
            print(f"  注入 [{result['action']}] {t['path']}")
        # 步骤 2：建节点与关联 + 写 state（单次原子提交）
        state = store.load()
        for t in todo:
            entry = gs.import_preview([t["path"]], which="test")  # 校验可解析
            node = gs.create_node(name=t["name"], domain=_domain_of(t["path"]), scope=t["scope"])["node"]
            created.append({"node_id": node["id"], "name": node["name"], "note": t["path"]})
        # 关联：逐条建立（每个节点↔各自笔记）
        nodes_by_note = {c["note"]: c["node_id"] for c in created}
        for t in todo:
            gs.import_apply([t["path"]],
                            [{"path": t["path"], "node_id": nodes_by_note[t["path"]]}],
                            request_id=None, base_version=None)
        # 步骤 3：写迁移清单
        manifest_path = state_dir / "migration_manifest.json"
        existing = json.loads(manifest_path.read_text(encoding="utf-8")) if manifest_path.exists() else {}
        batches = existing.get("batches", [])
        batches.append({
            "at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "notes": [t["path"] for t in todo],
            "nodes": created,
        })
        manifest = {
            "approved_scope": "受控接入：节点 + 关联 + mastery_status 注入（笔记正文未改动）",
            "batches": batches,
            "injected_notes": sorted(set(existing.get("injected_notes", []) + injected)),
            "rollback": "python demo/real_vault_setup.py --rollback",
        }
        manifest_path.write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception as exc:
        print(f"执行中断（{type(exc).__name__}: {exc}）；回滚本次已注入的笔记…")
        _restore_notes()
        print("已回滚。管理区如已创建请手动删除。")
        raise

    print()
    print(f"完成：{len(created)} 个节点（全部 untested）、{len(injected)} 篇笔记已注入。")
    print(f"迁移清单：{state_dir / 'migration_manifest.json'}")
    print("下一步：启动真实库实例 → python server_start.py --vault \"<真实库>\" --port 8766 --allow-real-vault")
    return 0


def _domain_of(relpath: str) -> str:
    seg = relpath.split("/", 1)[0]
    return seg.split("-", 1)[1] if "-" in seg else seg


def _restore_notes() -> int:
    """从 note_backups 还原全部可匹配的笔记。"""
    bdir = REAL_VAULT / STATE_DIR / "note_backups"
    if not bdir.exists():
        print("无 note_backups 目录，跳过笔记还原。")
        return 0
    restored = 0
    for backup in sorted(bdir.glob("*.md.bak")):
        stem = backup.name.split(".", 1)[0]
        # 在目标清单中找回原名
        for t in TARGETS:
            if Path(t["path"]).stem == stem:
                shutil.copy2(backup, REAL_VAULT / t["path"])
                restored += 1
                print(f"  还原 {t['path']}")
                break
    return restored


def rollback() -> int:
    state_dir = REAL_VAULT / STATE_DIR
    if not state_dir.exists():
        print("管理区不存在，无需回退。")
        return 0
    print("=== 回退 ===")
    restored = _restore_notes()
    # 二次校验：确认笔记里已无注入字段
    leftovers = []
    for t in TARGETS:
        note = REAL_VAULT / t["path"]
        if note.exists() and "mastery_status:" in _load_note_text(note):
            leftovers.append(t["path"])
    if leftovers:
        print(f"警告：{leftovers} 还原后仍含 mastery_status，请人工检查。")
        return 1
    shutil.rmtree(state_dir)
    print(f"已删除管理区；还原笔记 {restored} 篇。回退完成。")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--apply", action="store_true", help="执行接入（写真实库）")
    parser.add_argument("--rollback", action="store_true", help="回退：还原笔记并删除管理区")
    args = parser.parse_args()
    if args.apply and args.rollback:
        parser.error("--apply 与 --rollback 互斥")
    if args.apply:
        return apply()
    if args.rollback:
        return rollback()
    return dry_run()


if __name__ == "__main__":
    raise SystemExit(main())
