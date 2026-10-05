"""掌握状态镜像：把 state.json 中的节点状态单向同步到笔记 frontmatter。

用途：Obsidian 图谱的 colorGroups 只能按笔记自身的查询着色，读不到外部
state.json；本模块把状态写进笔记 frontmatter 的 mastery_status 字段，供
Obsidian 展示三色（passed 绿 / failed 红 / untested 蓝）。

边界（重要）：
- state.json 是唯一权威；frontmatter 字段是**展示镜像**，由系统生成与覆盖。
  用户在 Obsidian 里手改该字段不会回写系统状态（下次同步会被覆盖）。
- 只处理「已关联节点」的笔记；只增改 mastery_status 一行，其余字节不动。
- 每次写入前把原文件备份到 个人学习状态/note_backups/（带时间戳）。
- 写路径必须位于 vault_root 内；真实库模式由调用方显式开启（enabled）。
"""

from __future__ import annotations

import re
import secrets
import shutil
from datetime import datetime, timezone
from pathlib import Path

from .models import now_iso

FIELD = "mastery_status"
VALID_STATUSES = ("untested", "failed", "passed")


class MirrorError(RuntimeError):
    """镜像写入被拒绝（缺 front-matter、越界、状态非法等）。"""


def _lines_with_endings(text: str) -> list[str]:
    return text.splitlines(keepends=True)


def set_status_in_text(text: str, status: str) -> tuple[str, str]:
    """在 front-matter 中设置 mastery_status。

    返回 (新文本, 动作)；动作 ∈ {inserted, updated, unchanged}。
    超出受限编辑范围（缺 front-matter / 未闭合）抛 MirrorError，不猜测。
    """
    if status not in VALID_STATUSES:
        raise MirrorError(f"非法状态：{status!r}（允许 {VALID_STATUSES}）")
    lines = _lines_with_endings(text)
    if not lines or lines[0].strip() != "---":
        raise MirrorError("笔记缺少 front-matter，拒绝注入")
    end = None
    for i in range(1, len(lines)):
        if lines[i].strip() == "---":
            end = i
            break
    if end is None:
        raise MirrorError("front-matter 未闭合，拒绝注入")
    for i in range(1, end):
        if lines[i].startswith(f"{FIELD}:"):
            current = lines[i].split(":", 1)[1].strip()
            if current == status:
                return text, "unchanged"
            newline = "\r\n" if lines[i].endswith("\r\n") else "\n"
            lines[i] = f"{FIELD}: {status}{newline}"
            return "".join(lines), "updated"
    # 插入到 front-matter 末尾（闭合 --- 之前），原有内容字节不动
    newline = "\r\n" if any(l.endswith("\r\n") for l in lines[: end + 1]) else "\n"
    lines.insert(end, f"{FIELD}: {status}{newline}")
    return "".join(lines), "inserted"


def read_status_from_text(text: str) -> str | None:
    m = re.search(rf"^{FIELD}:\s*(\S+)\s*$", text, re.M)
    return m.group(1) if m else None


def note_backup_dir(store) -> Path:
    return store.state_path.parent / "note_backups"


def apply_status(note_path: Path, status: str, *, store, backup: bool = True) -> dict:
    """写入一篇笔记的 mastery_status（原子替换 + 备份）。返回动作摘要。"""
    note_path = Path(note_path).resolve()
    if not _is_within(note_path, store.vault_root):
        raise MirrorError(f"笔记路径越出知识库：{note_path}")
    if not note_path.exists():
        return {"path": str(note_path), "action": "missing"}
    original = note_path.read_text(encoding="utf-8")
    new_text, action = set_status_in_text(original, status)
    if action == "unchanged":
        return {"path": str(note_path), "action": action, "status": status}
    if backup:
        bdir = note_backup_dir(store)
        bdir.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
        # .md.bak 扩展名：Obsidian 只索引 .md，备份不会变成幽灵笔记节点
        shutil.copy2(note_path, bdir / f"{note_path.stem}.{stamp}.md.bak")
    tmp = note_path.with_name(f".{note_path.name}.mirror-{secrets.token_hex(4)}")
    tmp.write_text(new_text, encoding="utf-8")
    tmp.replace(note_path)
    return {"path": str(note_path), "action": action, "status": status}


def sync_node(gs, node_id: str, *, enabled: bool) -> dict:
    """把单个节点的当前状态镜像到其关联笔记。enabled=False 时不做任何事。"""
    if not enabled:
        return {"skipped": True, "reason": "镜像未启用（非真实库模式）"}
    state = gs.state()
    node = next((n for n in state["nodes"] if n["id"] == node_id), None)
    if node is None:
        return {"skipped": True, "reason": "节点不存在"}
    status = node["status"] if node["status"] in VALID_STATUSES else "untested"
    results = []
    for link in state["note_links"]:
        if link["node_id"] != node_id:
            continue
        relpath = link["ref"].get("path")
        if not relpath:
            continue
        try:
            results.append(apply_status(gs.store.vault_root / relpath, status, store=gs.store))
        except MirrorError as exc:
            results.append({"path": relpath, "action": "refused", "reason": str(exc)})
    return {"node_id": node_id, "status": status, "results": results}


def sync_all(gs, *, enabled: bool) -> dict:
    """把全部节点的状态镜像到各自关联笔记（修复漂移用）。"""
    if not enabled:
        return {"skipped": True, "reason": "镜像未启用"}
    out = []
    for node in gs.state()["nodes"]:
        if node["archived"]:
            continue
        out.append(sync_node(gs, node["id"], enabled=True))
    return {"synced_nodes": len(out), "detail": out}


def _is_within(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False
