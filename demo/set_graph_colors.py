# -*- coding: utf-8 -*-
"""写入 Obsidian 图谱三色分组（mastery_status → 绿/红/蓝）。

重要：Obsidian 运行时会覆写 .obsidian/graph.json。必须在**完全退出 Obsidian 后**
运行本脚本，然后再打开 Obsidian，颜色才会生效。

用法：
  python demo/set_graph_colors.py           # 检查并写入（Obsidian 运行中则拒绝）
  python demo/set_graph_colors.py --check   # 只看当前状态
"""

import argparse
import json
import shutil
import subprocess
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

REAL_VAULT = Path(Path(os.environ.get("PKA_REAL_VAULT", "")))
GRAPH_JSON = REAL_VAULT / ".obsidian" / "graph.json"
BACKUP_DIR = REAL_VAULT / "个人学习状态" / "graph_backups"

GROUPS = [
    {"query": "[mastery_status:passed]",   "color": {"a": 1, "rgb": int("0F766E", 16)}, "label": "已通过（掌握层）绿"},
    {"query": "[mastery_status:failed]",   "color": {"a": 1, "rgb": int("B91C1C", 16)}, "label": "未通过（视野层）红"},
    {"query": "[mastery_status:untested]", "color": {"a": 1, "rgb": int("2563EB", 16)}, "label": "未测验（视野层）蓝"},
]


def obsidian_running() -> bool:
    """按字节匹配：tasklist 输出为系统本地编码（本机 GBK），不能按 UTF-8 解码。"""
    try:
        out = subprocess.run(["tasklist"], capture_output=True, timeout=15).stdout
        return b"Obsidian.exe" in out
    except Exception:  # noqa: BLE001
        return False


def current_groups() -> list:
    if not GRAPH_JSON.exists():
        return []
    try:
        return json.loads(GRAPH_JSON.read_text(encoding="utf-8")).get("colorGroups", [])
    except (json.JSONDecodeError, OSError):
        return []


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--check", action="store_true", help="只查看状态，不写入")
    args = parser.parse_args()

    running = obsidian_running()
    groups = current_groups()
    print(f"Obsidian 运行中：{'是' if running else '否'}")
    print(f"当前颜色分组：{len(groups)} 条")
    for g in groups:
        print(f"  {g.get('query')} → #{g.get('color', {}).get('rgb', 0):06X}")

    if args.check:
        return 0

    if running:
        print()
        print("拒绝写入：Obsidian 正在运行，它会在关闭时覆写 graph.json。")
        print("请完全退出 Obsidian（含托盘图标；可在任务管理器确认无 Obsidian.exe），再运行本脚本。")
        return 1

    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    if GRAPH_JSON.exists():
        stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
        shutil.copy2(GRAPH_JSON, BACKUP_DIR / f"graph.json.{stamp}.bak")

    cfg = json.loads(GRAPH_JSON.read_text(encoding="utf-8")) if GRAPH_JSON.exists() else {}
    cfg["colorGroups"] = [{"query": g["query"], "color": g["color"]} for g in GROUPS]
    cfg["collapse-color-groups"] = False
    GRAPH_JSON.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")

    print()
    print("已写入 3 条颜色分组：")
    for g in GROUPS:
        print(f"  {g['query']:<32} #{g['color']['rgb']:06X}  {g['label']}")
    print(f"原文件备份：{BACKUP_DIR}")
    print("现在重新打开 Obsidian，关系图谱即按状态着色。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
