"""只读扫描与笔记身份解析（任务 2）。

身份规则（与任务书一致）：
- 有唯一稳定 ID（note-YYYYMMDD-NNN）的笔记，优先按 ID 关联；
- 没有 ID 的笔记不改原文件，用独立的关联登记（路径 + 内容指纹）记录；
- 重复 ID 必须报冲突，不任取一篇；
- 无 ID 文件若路径与指纹都无法唯一确认，标"待修复"，不猜关联对象。

扫描是纯只读：只 open 读取，不写、不移动、不改任何被扫文件。
目标范围（明确的支持范围）：路径中包含「候选笔记/」或「正式笔记/」目录的 .md；
其余（技能、证据、运行记录、资料、关系、.obsidian 等）一律跳过并计数。
"""

from __future__ import annotations

import hashlib
from pathlib import Path

from .frontmatter import FrontmatterError, parse_file

SKIP_DIR_NAMES = {".obsidian", "技能", "证据", "运行记录", "资料", "关系", "backups", "个人学习状态"}
TARGET_DIR_NAMES = {"候选笔记", "正式笔记"}


def content_fingerprint(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest().upper()


def _classify(relpath: str, meta: dict) -> str:
    status = str(meta.get("status") or "").strip().lower()
    if "正式笔记" in relpath:
        return "formal"
    if "候选笔记" in relpath:
        return "candidate"
    return "unknown"


def scan_vault(vault_root: Path) -> dict:
    """只读扫描一个知识库根，返回可 JSON 化的报告。"""
    vault_root = Path(vault_root).resolve()
    entries: list[dict] = []
    skipped = 0
    unreadable: list[dict] = []

    for path in sorted(vault_root.rglob("*.md")):
        relpath = path.relative_to(vault_root).as_posix()
        parts = set(path.parent.parts)
        if ".obsidian" in parts or path.name == "SOURCES.md":
            skipped += 1
            continue
        if not (parts & TARGET_DIR_NAMES):
            skipped += 1
            continue

        try:
            meta, _body = parse_file(path)
        except FrontmatterError as exc:
            unreadable.append({"path": relpath, "error": str(exc)})
            continue
        except (OSError, UnicodeDecodeError) as exc:
            unreadable.append({"path": relpath, "error": f"无法读取：{exc}"})
            continue

        note_id = str(meta.get("id") or "").strip()
        missing = [
            field
            for field in ("id", "title", "domain", "status")
            if not str(meta.get(field) or "").strip()
        ]
        entries.append(
            {
                "path": relpath,
                "note_id": note_id or None,
                "title": str(meta.get("title") or "").strip() or path.stem,
                "domain": str(meta.get("domain") or "").strip() or None,
                "audit_status": str(meta.get("status") or "").strip() or None,
                "kind": _classify(relpath, meta),
                "fingerprint": content_fingerprint(path),
                "size_bytes": path.stat().st_size,
                "missing_fields": missing,
            }
        )

    # 重复 ID 检测：同库内同 ID 多文件 → 冲突，不任取一篇
    by_id: dict[str, list[dict]] = {}
    for entry in entries:
        if entry["note_id"]:
            by_id.setdefault(entry["note_id"], []).append(entry)
    duplicate_ids = {
        note_id: [e["path"] for e in group] for note_id, group in sorted(by_id.items()) if len(group) > 1
    }
    for note_id in duplicate_ids:
        for entry in by_id[note_id]:
            entry["duplicate_of"] = duplicate_ids[note_id]

    # 易混淆排除项：文件名含"候选笔记/正式笔记"但父目录不是目标目录的 .md
    # （例如 流程说明/候选笔记说明.md 是流程文档，不是知识笔记）。
    # 显式列出，保证"数量差异"永远可解释，不静默消失。
    near_target_excluded = []
    for path in sorted(vault_root.rglob("*.md")):
        parts = set(path.parent.parts)
        if parts & TARGET_DIR_NAMES:
            continue
        if ".obsidian" in parts:
            continue
        name = path.stem
        if "候选笔记" in name or "正式笔记" in name:
            near_target_excluded.append(
                {
                    "path": path.relative_to(vault_root).as_posix(),
                    "reason": "文件名含'候选笔记/正式笔记'，但所在目录不是目标目录（非知识笔记，显式排除）",
                }
            )

    return {
        "vault_root": str(vault_root),
        "target_notes": entries,
        "skipped_non_target": skipped,
        "unreadable": unreadable,
        "duplicate_ids": duplicate_ids,
        "near_target_excluded": near_target_excluded,
        "stats": {
            "target_total": len(entries),
            "with_id": sum(1 for e in entries if e["note_id"]),
            "without_id": sum(1 for e in entries if not e["note_id"]),
            "formal": sum(1 for e in entries if e["kind"] == "formal"),
            "candidate": sum(1 for e in entries if e["kind"] == "candidate"),
        },
    }


def resolvable_targets(report: dict) -> list[dict]:
    """可安全作为关联目标的笔记：有唯一 ID，或无 ID（走登记路径）。
    重复 ID 的笔记不在此列——必须先解决冲突。"""
    return [
        e
        for e in report["target_notes"]
        if not e.get("duplicate_of")
    ]
