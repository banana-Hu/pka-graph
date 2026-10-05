"""最小数据契约：节点、关联与状态存储的模型与校验。

产品规则（第二阶段边界，必须一直成立）：
- 节点状态只有 untested / failed / passed；untested、failed 属视野层，passed 属掌握层。
- 层级由状态推导，不维护第二套权威状态。
- 新建/导入节点一律 untested；作者是谁都不影响。
- 笔记的审核状态（candidate/accepted）与节点掌握状态互相独立。
- 同名不同范围可共存；重复提交不产生重复对象。
- 稳定 ID 与名称、文件名分离；改名不清状态。
- 实质改范围需用户确认，确认后回到 untested。
- 合并/拆分产生的新节点一律 untested，原节点可恢复地归档。
"""

from __future__ import annotations

import re
import uuid
from datetime import datetime, timezone

NODE_STATUSES = ("untested", "failed", "passed")
RELATIONS = ("related", "prerequisite", "extends")
SCHEMA = "pka-state/1"

# 稳定笔记 ID：以 note- 开头、字母数字/下划线/连字符组成。
# 统一笔记模型推荐 note-YYYYMMDD-NNN；2026-10 书单类笔记实际使用
# note-YYYYMMDD-<slug>，两种都接受，避免把真实数据当异常拒绝。
_NODE_ID_RE = re.compile(r"^node-[0-9a-f]{12}$")
_NOTE_ID_RE = re.compile(r"^note-[0-9A-Za-z_-]{3,}$")
_SHORT_ID_RE = re.compile(r"^[0-9a-f]{8}$")


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def new_node_id() -> str:
    return "node-" + uuid.uuid4().hex[:12]


def _short_id(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:8]}"


class ValidationError(ValueError):
    """输入不满足数据契约。"""


def validate_node(node: dict) -> None:
    """校验单个节点 dict；不合法抛 ValidationError。"""
    required = ("id", "name", "domain", "scope", "status")
    for field in required:
        if not str(node.get(field) or "").strip():
            raise ValidationError(f"节点缺少必填字段 {field}")
    if not _NODE_ID_RE.match(node["id"]):
        raise ValidationError(f"节点 ID 形如 node-<12位十六进制>，得到 {node['id']!r}")
    if node["status"] not in NODE_STATUSES:
        raise ValidationError(f"非法节点状态 {node['status']!r}，允许 {NODE_STATUSES}")
    for field in ("aliases",):
        if not isinstance(node.get(field), list):
            raise ValidationError(f"{field} 必须是列表")
    for field in ("content_version",):
        if not isinstance(node.get(field), int) or node[field] < 1:
            raise ValidationError(f"{field} 必须是正整数")
    if not isinstance(node.get("archived"), bool):
        raise ValidationError("archived 必须是布尔值")


def new_node(*, name: str, domain: str, scope: str, aliases: list[str] | None = None) -> dict:
    """创建节点 dict：一律 untested（产品规则 4）。"""
    ts = now_iso()
    node = {
        "id": new_node_id(),
        "name": name.strip(),
        "aliases": [a.strip() for a in (aliases or []) if a.strip()],
        "domain": domain.strip(),
        "scope": scope.strip(),
        "status": "untested",
        "content_version": 1,
        "created_at": ts,
        "updated_at": ts,
        "last_quiz_at": None,
        "next_review_at": None,
        "archived": False,
        "archived_at": None,
        "archive_note": None,
    }
    validate_node(node)
    return node


def validate_relation(relation: str) -> None:
    if relation not in RELATIONS:
        raise ValidationError(f"非法关系类型 {relation!r}，允许 {RELATIONS}")


def make_note_link(node_id: str, ref: dict) -> dict:
    """节点 ↔ 笔记关联。ref 两种形态：
    {"kind": "note_id", "note_id": "note-..."}   —— 有稳定 ID 的笔记，优先按 ID 关联
    {"kind": "registry", "registry_id": "reg-..."} —— 无 ID 笔记，指向身份登记
    """
    if not _NODE_ID_RE.match(node_id):
        raise ValidationError(f"节点 ID 非法：{node_id!r}")
    kind = ref.get("kind")
    if kind == "note_id":
        if not _NOTE_ID_RE.match(str(ref.get("note_id") or "")):
            raise ValidationError(
                f"笔记 ID 应以 note- 开头（如 note-YYYYMMDD-NNN），得到 {ref.get('note_id')!r}"
            )
    elif kind == "registry":
        if not _SHORT_ID_RE.match(str(ref.get("registry_id") or "").replace("reg-", "", 1)):
            raise ValidationError(f"登记 ID 形如 reg-<8位十六进制>，得到 {ref.get('registry_id')!r}")
    else:
        raise ValidationError(f"关联 ref.kind 只支持 note_id / registry，得到 {kind!r}")
    return {"link_id": _short_id("lnk"), "node_id": node_id, "ref": dict(ref), "created_at": now_iso()}


def make_node_link(from_id: str, to_id: str, relation: str) -> dict:
    """节点 ↔ 节点关系（有向）。

    方向语义：
    - related：对称，仅表示相关；
    - prerequisite：from 是 to 的前置（先掌握 from，才谈 to）；
    - extends：from 延伸自 to（to 是基础）。
    """
    validate_relation(relation)
    if from_id == to_id:
        raise ValidationError("不允许节点自连接")
    for nid in (from_id, to_id):
        if not _NODE_ID_RE.match(nid):
            raise ValidationError(f"节点 ID 非法：{nid!r}")
    return {
        "link_id": _short_id("rel"),
        "from_id": from_id,
        "to_id": to_id,
        "relation": relation,
        "created_at": now_iso(),
    }


def empty_state() -> dict:
    return {
        "schema": SCHEMA,
        "version": 1,
        "updated_at": now_iso(),
        "nodes": [],
        "note_links": [],
        "node_links": [],
        "ingest_registry": [],
        "idempotency": {},
    }


def validate_state(state: dict) -> None:
    """写入前整体校验：引用完整性、无自连接、无重复 ID。"""
    if state.get("schema") != SCHEMA:
        raise ValidationError(f"状态 schema 应为 {SCHEMA}，得到 {state.get('schema')!r}")
    if not isinstance(state.get("version"), int) or state["version"] < 1:
        raise ValidationError("version 必须是正整数")
    ids = set()
    for node in state.get("nodes", []):
        validate_node(node)
        if node["id"] in ids:
            raise ValidationError(f"节点 ID 重复：{node['id']}")
        ids.add(node["id"])
    node_links = state.get("node_links", [])
    note_links = state.get("note_links", [])
    registry_ids = {r.get("registry_id") for r in state.get("ingest_registry", [])}
    for link in note_links:
        if link["node_id"] not in ids:
            raise ValidationError(f"笔记关联指向不存在的节点：{link['node_id']}")
        ref = link["ref"]
        if ref["kind"] == "registry" and ref.get("registry_id") not in registry_ids:
            raise ValidationError(f"笔记关联指向不存在的身份登记：{ref.get('registry_id')}")
    seen_rel = set()
    for link in node_links:
        validate_relation(link["relation"])
        if link["from_id"] == link["to_id"]:
            raise ValidationError(f"存在自连接：{link['from_id']}")
        for nid in (link["from_id"], link["to_id"]):
            if nid not in ids:
                raise ValidationError(f"节点关系指向不存在的节点：{nid}")
        key = (link["relation"], frozenset((link["from_id"], link["to_id"])))
        if key in seen_rel:
            raise ValidationError(f"重复关系：{link['relation']} {link['from_id']}<->{link['to_id']}")
        seen_rel.add(key)
    for rid, record in state.get("idempotency", {}).items():
        if not isinstance(record, dict) or "result" not in record:
            raise ValidationError(f"幂等记录 {rid} 缺少 result")
