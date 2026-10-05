"""业务操作层：扫描接入、节点管理、关系、合并/拆分/归档。

所有写操作共用同一事务模式：
  读状态 → 幂等检查（request_id 命中则原样返回首次结果）→ 深拷贝上变更 →
  整体校验 → store.save（乐观锁 + 备份 + 原子替换）→ 幂等记录随同一次写入落盘。
因此合并/拆分等多对象操作要么完整成功、要么完全不变，不存在半完成状态。
"""

from __future__ import annotations

import copy
import threading
import time
import uuid
from pathlib import Path

from . import scanner
from .frontmatter import FrontmatterError, parse_file
from .models import (
    NODE_STATUSES,
    ValidationError,
    make_node_link,
    make_note_link,
    new_node,
    now_iso,
    validate_relation,
)
from .store import PathOutsideVault, StateStore, VersionConflict

IDEMPOTENCY_KEEP = 200
FINGERPRINT_KINDS = ("note_id", "registry")


class NotFound(KeyError):
    def __init__(self, kind: str, obj_id: str):
        super().__init__(f"{kind} 不存在：{obj_id}")
        self.kind = kind
        self.obj_id = obj_id


class ConfirmationRequired(RuntimeError):
    """操作需要用户明确确认（当前仅：修改知识范围）。"""

    def __init__(self, preview: dict):
        super().__init__("该操作需要明确确认")
        self.preview = preview


class GraphService:
    def __init__(self, store: StateStore, real_vault: Path | None = None):
        self.store = store
        self.real_vault = Path(real_vault).resolve() if real_vault else None
        self.test_vault = store.vault_root
        self.material_lock = threading.RLock()  # 收件箱/发现/笔记共用单文件串行边界

    # ---------- 基础 ----------

    def state(self) -> dict:
        return self.store.load()

    def _begin(self, state: dict, request_id: str | None, op: str):
        """幂等检查。命中返回 (True, 首次结果)；否则返回 (False, None)。"""
        if request_id and request_id in state.get("idempotency", {}):
            record = state["idempotency"][request_id]
            return True, dict(record.get("result") or {}, replayed=True, request_id=request_id, op=op)
        return False, None

    def _record_idempotency(self, state: dict, request_id: str | None, op: str, result: dict) -> None:
        if not request_id:
            return
        idem = state.setdefault("idempotency", {})
        idem[request_id] = {"op": op, "at": now_iso(), "result": result}
        while len(idem) > IDEMPOTENCY_KEEP:
            idem.pop(next(iter(idem)))

    def _node(self, state: dict, node_id: str) -> dict:
        for node in state["nodes"]:
            if node["id"] == node_id:
                return node
        raise NotFound("节点", node_id)

    # ---------- 任务 2：扫描 ----------

    def scan(self, which: str = "test") -> dict:
        if which == "real":
            if not self.real_vault:
                raise ValidationError("未配置真实知识库路径")
            return scanner.scan_vault(self.real_vault)
        if which == "test":
            return scanner.scan_vault(self.test_vault)
        raise ValidationError(f"scan 目标只支持 test / real，得到 {which!r}")

    # ---------- 任务 3：导入预览与接入 ----------

    def _read_note_entry(self, root: Path, relpath: str) -> dict:
        root = Path(root).resolve()
        path = Path(relpath)
        if path.is_absolute():
            raise PathOutsideVault(f"请使用知识库内的相对路径：{relpath}")
        resolved = (root / path).resolve()
        try:
            resolved.relative_to(root)  # 越界（../ 等）在此拒绝，先于存在性检查
        except ValueError:
            raise PathOutsideVault(f"路径越出知识库根：{relpath}")
        if not resolved.is_file():
            raise NotFound("笔记文件", relpath)
        try:
            meta, _body = parse_file(resolved)
        except FrontmatterError as exc:
            raise ValidationError(f"{relpath}：{exc}")
        fingerprint = scanner.content_fingerprint(resolved)
        note_id = str(meta.get("id") or "").strip() or None
        return {
            "path": resolved.relative_to(root).as_posix(),
            "note_id": note_id,
            "title": str(meta.get("title") or "").strip() or resolved.stem,
            "domain": str(meta.get("domain") or "").strip() or None,
            "audit_status": str(meta.get("status") or "").strip() or None,
            "fingerprint": fingerprint,
        }

    def import_preview(self, paths: list[str], assignments: list[dict] | None = None, which: str = "test") -> dict:
        """预览将生成的登记/引用记录，不写入任何文件。"""
        root = self.test_vault if which == "test" else self.real_vault
        if root is None:
            raise ValidationError("未配置扫描根")
        state = self.state()
        plan = []
        for relpath in paths:
            entry = self._read_note_entry(Path(root), relpath)
            registered = any(
                r["path"] == entry["path"] and r["fingerprint"] == entry["fingerprint"]
                for r in state["ingest_registry"]
            )
            fingerprint_mismatch = (
                entry["note_id"] is None
                and any(r["path"] == entry["path"] for r in state["ingest_registry"])
                and not registered
            )
            warnings = []
            if entry["note_id"] is None:
                warnings.append("无稳定 ID：将以 路径+指纹 做身份登记；文件改名或内容变化后需人工确认关联")
            if fingerprint_mismatch:
                warnings.append(
                    "待修复：该路径已登记过，但内容指纹已变化（文件被修改或改名冒充）。"
                    "登记保持指向旧指纹，关联不自动迁移；请人工确认后再更新登记"
                )
            if entry["audit_status"] not in ("candidate", "accepted"):
                warnings.append(f"审核状态异常：{entry['audit_status']!r}（不影响接入，仅提示）")
            actions = [{"type": "register", "registry_id": None, "needed": not registered and entry["note_id"] is None}]
            for assignment in assignments or []:
                if assignment.get("path") == relpath:
                    node_id = assignment.get("node_id")
                    self._node(state, node_id)  # 确认节点存在
                    ref = (
                        {"kind": "note_id", "note_id": entry["note_id"]}
                        if entry["note_id"]
                        else {"kind": "registry", "registry_id": None}
                    )
                    duplicate = self._has_note_link(state, node_id, entry, ref)
                    actions.append({"type": "link", "node_id": node_id, "needed": not duplicate})
            plan.append({**entry, "actions": actions, "warnings": warnings})
        return {"root": str(root), "plan": plan, "note": "预览未写入任何文件；确认后仅写入测试知识库管理区"}

    def _has_note_link(self, state: dict, node_id: str, entry: dict, ref: dict) -> bool:
        for link in state["note_links"]:
            if link["node_id"] != node_id:
                continue
            if ref["kind"] == "note_id" and link["ref"].get("note_id") == entry["note_id"]:
                return True
            if ref["kind"] == "registry":
                reg_id = link["ref"].get("registry_id")
                reg = next((r for r in state["ingest_registry"] if r["registry_id"] == reg_id), None)
                if reg and reg["path"] == entry["path"]:
                    return True
        return False

    def import_apply(
        self,
        paths: list[str],
        assignments: list[dict] | None,
        request_id: str | None,
        base_version: int,
    ) -> dict:
        replayed, replay = self._begin(self.state(), request_id, "import_apply")
        if replayed:
            return replay
        preview = self.import_preview(paths, assignments, which="test")
        state = copy.deepcopy(self.state())
        created_links = []
        registered = []
        # 先登记（无 ID 笔记），再建关联
        registry_by_path: dict[str, str] = {
            r["path"]: r["registry_id"] for r in state["ingest_registry"]
        }
        for item in preview["plan"]:
            if item["note_id"] is None and item["path"] not in registry_by_path:
                reg_id = "reg-" + uuid.uuid4().hex[:8]
                state["ingest_registry"].append(
                    {
                        "registry_id": reg_id,
                        "path": item["path"],
                        "fingerprint": item["fingerprint"],
                        "registered_at": now_iso(),
                    }
                )
                registry_by_path[item["path"]] = reg_id
                registered.append({"path": item["path"], "registry_id": reg_id})
        for assignment in assignments or []:
            item = next(p for p in preview["plan"] if p["path"] == assignment["path"])
            node_id = assignment["node_id"]
            if item["note_id"]:
                ref = {"kind": "note_id", "note_id": item["note_id"], "path": item["path"]}
            else:
                ref = {"kind": "registry", "registry_id": registry_by_path[item["path"]], "path": item["path"]}
            if self._has_note_link(state, node_id, item, ref):
                continue
            link = make_note_link(node_id, ref)
            state["note_links"].append(link)
            created_links.append({"link_id": link["link_id"], "path": item["path"], "node_id": node_id})
        result = {"registered": registered, "note_links": created_links}
        self._record_idempotency(state, request_id, "import_apply", result)
        self.store.save(state, base_version=base_version)
        return result

    # ---------- 任务 4：节点管理 ----------

    def list_nodes(self, name: str = "", domain: str = "", status: str = "", include_archived: bool = False) -> list[dict]:
        """节点搜索：匹配 名称 / 别名 / scope（明确是节点搜索，不是笔记全文检索）。"""
        state = self.state()
        nodes = []
        q = name.lower().strip()
        for node in state["nodes"]:
            if node["archived"] and not include_archived:
                continue
            if q:
                hit = (q in node["name"].lower()
                       or any(q in str(a).lower() for a in (node.get("aliases") or []))
                       or q in (node.get("scope") or "").lower())
                if not hit:
                    continue
            if domain and node["domain"] != domain:
                continue
            if status and node["status"] != status:
                continue
            nodes.append(node)
        return nodes

    def node_detail(self, node_id: str) -> dict:
        state = self.state()
        node = self._node(state, node_id)
        note_links = []
        for link in state["note_links"]:
            if link["node_id"] != node_id:
                continue
            info = dict(link)
            ref = link["ref"]
            if ref["kind"] == "registry":
                reg = next((r for r in state["ingest_registry"] if r["registry_id"] == ref.get("registry_id")), None)
                info["path"] = reg["path"] if reg else None
                info["missing_registry"] = reg is None
            else:
                info["path"] = ref.get("path")
            res = self.resolve_note_link(link)
            info["resolution"] = {"status": res["status"], "path": res.get("path"),
                                  "relocated": res.get("relocated", False),
                                  "audit_status": res.get("audit_status"),
                                  "candidates": res.get("candidates") or [],
                                  "message": res.get("message") or ""}
            note_links.append(info)
        relations = [dict(l) for l in state["node_links"] if node_id in (l["from_id"], l["to_id"])]
        name_by_id = {n["id"]: n["name"] for n in state["nodes"]}
        for rel in relations:
            rel["from_name"] = name_by_id.get(rel["from_id"])
            rel["to_name"] = name_by_id.get(rel["to_id"])
        return {"node": node, "note_links": note_links, "relations": relations}

    def create_node(
        self,
        *,
        name: str,
        domain: str,
        scope: str,
        aliases: list[str] | None = None,
        request_id: str | None = None,
        base_version: int | None = None,
    ) -> dict:
        replayed, replay = self._begin(self.state(), request_id, "create_node")
        if replayed:
            return replay
        state = copy.deepcopy(self.state())
        node = new_node(name=name, domain=domain, scope=scope, aliases=aliases)
        state["nodes"].append(node)
        result = {"node": node}
        self._record_idempotency(state, request_id, "create_node", {"node_id": node["id"]})
        self.store.save(state, base_version=base_version)
        return result

    def update_node(
        self,
        node_id: str,
        changes: dict,
        *,
        confirm_scope_change: bool = False,
        request_id: str | None = None,
        base_version: int | None = None,
    ) -> dict:
        """编辑名称/别名/领域/范围。改范围必须 confirm_scope_change=True，确认后状态回 untested。"""
        replayed, replay = self._begin(self.state(), request_id, "update_node")
        if replayed:
            return replay
        state = copy.deepcopy(self.state())
        node = self._node(state, node_id)
        scope_changing = "scope" in changes and changes["scope"].strip() != node["scope"]
        if scope_changing and not confirm_scope_change:
            raise ConfirmationRequired(
                {
                    "node_id": node_id,
                    "old_scope": node["scope"],
                    "new_scope": changes["scope"],
                    "effect": "知识范围实质变化：确认后节点将回到 untested（视野层）",
                    "hint": "再次调用并携带 confirm_scope_change=true",
                }
            )
        for field in ("name", "domain", "scope"):
            if field in changes:
                node[field] = str(changes[field]).strip()
        if "aliases" in changes:
            node["aliases"] = [a.strip() for a in changes["aliases"] if a.strip()]
        if scope_changing:
            node["status"] = "untested"
            node["content_version"] += 1
        node["updated_at"] = now_iso()
        result = {"node": node, "scope_changed": scope_changing}
        self._record_idempotency(state, request_id, "update_node", {"node_id": node_id, "scope_changed": scope_changing})
        self.store.save(state, base_version=base_version)
        return result

    # ---------- 关系 ----------

    def add_relation(self, from_id: str, to_id: str, relation: str, *, base_version: int | None = None, request_id: str | None = None) -> dict:
        replayed, replay = self._begin(self.state(), request_id, "add_relation")
        if replayed:
            return replay
        state = copy.deepcopy(self.state())
        self._node(state, from_id)
        self._node(state, to_id)
        link = make_node_link(from_id, to_id, relation)
        state["node_links"].append(link)
        result = {"link": link}
        self._record_idempotency(state, request_id, "add_relation", {"link_id": link["link_id"]})
        self.store.save(state, base_version=base_version)
        return result

    def remove_relation(self, link_id: str, *, base_version: int | None = None, request_id: str | None = None) -> dict:
        replayed, replay = self._begin(self.state(), request_id, "remove_relation")
        if replayed:
            return replay
        state = copy.deepcopy(self.state())
        before = len(state["node_links"])
        state["node_links"] = [l for l in state["node_links"] if l["link_id"] != link_id]
        if len(state["node_links"]) == before:
            raise NotFound("节点关系", link_id)
        self._record_idempotency(state, request_id, "remove_relation", {"removed": link_id})
        self.store.save(state, base_version=base_version)
        return {"removed": link_id}

    # ---------- 合并 / 拆分 ----------

    def _links_of(self, state: dict, node_id: str) -> tuple[list[dict], list[dict]]:
        note_links = [l for l in state["note_links"] if l["node_id"] == node_id]
        node_links = [l for l in state["node_links"] if node_id in (l["from_id"], l["to_id"])]
        return note_links, node_links

    def merge_preview(self, source_id: str, target_id: str) -> dict:
        if source_id == target_id:
            raise ValidationError("不能把节点合并到它自己")
        state = self.state()
        self._node(state, source_id)
        self._node(state, target_id)
        note_links, node_links = self._links_of(state, source_id)
        migrated, dropped = [], []
        for link in node_links:
            new_from = target_id if link["from_id"] == source_id else link["from_id"]
            new_to = target_id if link["to_id"] == source_id else link["to_id"]
            if new_from == new_to:
                dropped.append({"link": link, "reason": "迁移后将成为自连接"})
                continue
            dup = any(
                l["link_id"] != link["link_id"]
                and l["relation"] == link["relation"]
                and {l["from_id"], l["to_id"]} == {new_from, new_to}
                for l in state["node_links"]
            )
            if dup:
                dropped.append({"link": link, "reason": "目标节点已有等价关系"})
                continue
            migrated.append({**link, "new_from": new_from, "new_to": new_to})
        return {
            "source_id": source_id,
            "target_id": target_id,
            "note_links_to_migrate": note_links,
            "node_links_to_migrate": migrated,
            "node_links_dropped": dropped,
            "effect": "源节点将归档（可恢复），状态不迁移；目标节点状态不变",
        }

    def merge_nodes(
        self,
        source_id: str,
        target_id: str,
        *,
        confirm_migrate_note_links: bool = True,
        confirm_migrate_node_links: bool = True,
        request_id: str | None = None,
        base_version: int | None = None,
    ) -> dict:
        replayed, replay = self._begin(self.state(), request_id, "merge_nodes")
        if replayed:
            return replay
        preview = self.merge_preview(source_id, target_id)  # 校验存在性与迁移方案
        state = copy.deepcopy(self.state())
        target = self._node(state, target_id)
        moved_note_links = 0
        if confirm_migrate_note_links:
            for link in state["note_links"]:
                if link["node_id"] == source_id:
                    link["node_id"] = target_id
                    moved_note_links += 1
        migrated_rels, dropped = [], []
        if confirm_migrate_node_links:
            for link in state["node_links"]:
                if source_id in (link["from_id"], link["to_id"]):
                    new_from = target_id if link["from_id"] == source_id else link["from_id"]
                    new_to = target_id if link["to_id"] == source_id else link["to_id"]
                    if new_from == new_to:
                        state["node_links"].remove(link)
                        dropped.append(link["link_id"])
                        continue
                    dup = any(
                        l["link_id"] != link["link_id"]
                        and l["relation"] == link["relation"]
                        and {l["from_id"], l["to_id"]} == {new_from, new_to}
                        for l in state["node_links"]
                    )
                    if dup:
                        state["node_links"].remove(link)
                        dropped.append(link["link_id"])
                        continue
                    link["from_id"] = new_from
                    link["to_id"] = new_to
                    migrated_rels.append(link["link_id"])
        source = self._node(state, source_id)
        source["archived"] = True
        source["archived_at"] = now_iso()
        source["archive_note"] = {
            "reason": "merged",
            "into": target_id,
            "moved_note_links": moved_note_links,
            "migrated_relations": migrated_rels,
            "dropped_relations": dropped,
            "status_before": source["status"],
        }
        target["updated_at"] = now_iso()
        result = {
            "target_id": target_id,
            "moved_note_links": moved_note_links,
            "migrated_relations": migrated_rels,
            "dropped_relations": dropped,
            "archived": source_id,
        }
        self._record_idempotency(state, request_id, "merge_nodes", {"archived": source_id, "into": target_id})
        self.store.save(state, base_version=base_version)
        return result

    def split_preview(self, source_id: str, parts: list[dict]) -> dict:
        state = self.state()
        self._node(state, source_id)
        note_links, node_links = self._links_of(state, source_id)
        return {
            "source_id": source_id,
            "planned_parts": parts,
            "note_links": note_links,
            "node_links": node_links,
            "effect": "新节点一律 untested；源节点归档（可恢复）。请在确认时为每条关联指定去向（新节点序号或留空=留在归档节点上）",
        }

    def split_nodes(
        self,
        source_id: str,
        parts: list[dict],
        note_link_assignments: dict[str, int] | None = None,
        relation_assignments: dict[str, int] | None = None,
        *,
        request_id: str | None = None,
        base_version: int | None = None,
    ) -> dict:
        """拆分：parts 每项 {name, scope, domain?, aliases?}。
        note_link_assignments: {note_link_id: 新节点下标}；relation_assignments: {rel_link_id: 下标}。
        未指定的关联留在归档源节点上（引用仍有效、可恢复）。"""
        replayed, replay = self._begin(self.state(), request_id, "split_nodes")
        if replayed:
            return replay
        if len(parts) < 2:
            raise ValidationError("拆分至少要有两个新节点")
        preview = self.split_preview(source_id, parts)
        state = copy.deepcopy(self.state())
        source = self._node(state, source_id)
        domain_default = source["domain"]
        new_ids: list[str] = []
        for part in parts:
            node = new_node(
                name=part.get("name", ""),
                domain=part.get("domain") or domain_default,
                scope=part.get("scope", ""),
                aliases=part.get("aliases"),
            )
            state["nodes"].append(node)
            new_ids.append(node["id"])
        moved_notes: dict[str, str] = {}
        for link in state["note_links"]:
            idx = (note_link_assignments or {}).get(link["link_id"])
            if idx is not None and link["node_id"] == source_id:
                if not 0 <= idx < len(new_ids):
                    raise ValidationError(f"关联 {link['link_id']} 的去向下标越界：{idx}")
                link["node_id"] = new_ids[idx]
                moved_notes[link["link_id"]] = new_ids[idx]
        moved_rels: dict[str, str] = {}
        dropped_rels: list[str] = []
        for link in state["node_links"]:
            if source_id not in (link["from_id"], link["to_id"]):
                continue
            idx = (relation_assignments or {}).get(link["link_id"])
            if idx is None:
                continue
            if not 0 <= idx < len(new_ids):
                raise ValidationError(f"关系 {link['link_id']} 的去向下的标越界：{idx}")
            new_id = new_ids[idx]
            new_from = new_id if link["from_id"] == source_id else link["from_id"]
            new_to = new_id if link["to_id"] == source_id else link["to_id"]
            if new_from == new_to:
                state["node_links"].remove(link)
                dropped_rels.append(link["link_id"])
                continue
            link["from_id"] = new_from
            link["to_id"] = new_to
            moved_rels[link["link_id"]] = new_id
        source["archived"] = True
        source["archived_at"] = now_iso()
        source["archive_note"] = {
            "reason": "split",
            "into": new_ids,
            "moved_note_links": moved_notes,
            "moved_relations": moved_rels,
            "dropped_relations": dropped_rels,
            "status_before": source["status"],
        }
        result = {"new_nodes": [self._node(state, nid) for nid in new_ids], "archived": source_id}
        self._record_idempotency(
            state, request_id, "split_nodes", {"new_node_ids": new_ids, "archived": source_id}
        )
        self.store.save(state, base_version=base_version)
        return result

    # ---------- 归档 / 恢复 ----------

    def archive_node(self, node_id: str, *, reason: str = "manual", request_id: str | None = None, base_version: int | None = None) -> dict:
        replayed, replay = self._begin(self.state(), request_id, "archive_node")
        if replayed:
            return replay
        state = copy.deepcopy(self.state())
        node = self._node(state, node_id)
        if node["archived"]:
            raise ValidationError(f"节点已处于归档状态：{node_id}")
        node["archived"] = True
        node["archived_at"] = now_iso()
        node["archive_note"] = {"reason": reason, "status_before": node["status"]}
        self._record_idempotency(state, request_id, "archive_node", {"node_id": node_id})
        self.store.save(state, base_version=base_version)
        return {"node": node}

    def restore_node(self, node_id: str, *, request_id: str | None = None, base_version: int | None = None) -> dict:
        """恢复归档节点：一律回到 untested（结构变更后不自动回到掌握层）。"""
        replayed, replay = self._begin(self.state(), request_id, "restore_node")
        if replayed:
            return replay
        state = copy.deepcopy(self.state())
        node = self._node(state, node_id)
        if not node["archived"]:
            raise ValidationError(f"节点不在归档状态：{node_id}")
        node["archived"] = False
        node["archived_at"] = None
        node["status"] = "untested"
        node["updated_at"] = now_iso()
        note = dict(node["archive_note"] or {})
        node["archive_note"] = {**note, "restored_at": now_iso(), "restored_as": "untested"}
        self._record_idempotency(state, request_id, "restore_node", {"node_id": node_id})
        self.store.save(state, base_version=base_version)
        return {"node": node}

    # ---------- 第三阶段：图谱 / 搜索 / 原文 ----------

    @staticmethod
    def layer_of_status(status: str) -> str:
        if status not in NODE_STATUSES:
            raise ValidationError(
                f"节点状态非法：{status!r}（允许 {NODE_STATUSES}）；非法状态绝不默认当作已掌握"
            )
        return "mastery" if status == "passed" else "vision"

    @staticmethod
    def layer_of(node: dict) -> str:
        """层级由状态推导（不落盘）。"""
        return GraphService.layer_of_status(node["status"])

    def graph_view(
        self,
        q: str = "",
        domain: str = "",
        layer: str = "",
        status: str = "",
        include_archived: bool = False,
    ) -> dict:
        """只读图谱视图：一次读取 = 版本一致快照；默认排除归档；
        边只连接可见节点；非法状态明确报错。"""
        state = self.state()
        visible: list[dict] = []
        excluded_archived = 0
        stats_all = {"total": 0, "mastery": 0, "vision": 0}
        for node in state["nodes"]:
            layer_here = self.layer_of_status(node["status"])  # 非法状态在这里抛错
            if node["archived"]:
                if not include_archived:
                    excluded_archived += 1
                    continue
            stats_all["total"] += 1
            stats_all[layer_here] += 1
            q_l = q.strip().lower()
            if q_l:
                haystack = " ".join(
                    [node["name"], *(node.get("aliases") or []), node.get("scope") or ""]
                ).lower()
                if q_l not in haystack:
                    continue
            if domain and node["domain"] != domain:
                continue
            if status and node["status"] != status:
                continue
            if layer and layer_here != layer:
                continue
            visible.append(
                {
                    "id": node["id"],
                    "name": node["name"],
                    "aliases": node.get("aliases") or [],
                    "domain": node["domain"],
                    "scope": node["scope"],
                    "status": node["status"],
                    "layer": layer_here,
                    "content_version": node["content_version"],
                    "status_origin": node.get("status_origin"),
                }
            )
        visible_ids = {n["id"] for n in visible}
        all_edges = state["node_links"]
        edges = [l for l in all_edges if l["from_id"] in visible_ids and l["to_id"] in visible_ids]
        domains = sorted({n["domain"] for n in state["nodes"] if not n["archived"]})
        return {
            "version": state["version"],
            "nodes": visible,
            "edges": [
                {"link_id": l["link_id"], "from_id": l["from_id"], "to_id": l["to_id"], "relation": l["relation"]}
                for l in edges
            ],
            "stats": {
                "active_total": stats_all["total"],
                "mastery": stats_all["mastery"],
                "vision": stats_all["vision"],
                "filtered": len(visible),
            },
            "domains": domains,
            "excluded_archived": excluded_archived,
            "hidden_edges": len(all_edges) - len(edges),
        }

    # ---------- 笔记身份解析（移动/重命名恢复，2026-10-03 审查修复） ----------

    NOTE_ID_INDEX_TTL = 30.0   # 秒；索引过期后下一次读取时重建

    def _note_id_index(self, refresh: bool = False) -> dict:
        """合法笔记范围内 note_id → 相对路径列表 的简单可刷新索引。

        范围与 scanner 一致：任意层级名为「正式笔记」「候选笔记」的目录子树
        （排除管理区）。只读文件；按 TTL 缓存，避免每次读取都全库扫描。
        """
        now = time.monotonic()
        cache = getattr(self, "_id_index_cache", None)
        if cache and not refresh and (now - cache["built_at"]) < self.NOTE_ID_INDEX_TTL:
            return cache["ids"]
        ids: dict[str, list[str]] = {}
        root = self.store.vault_root
        for base in root.rglob("*"):
            if not base.is_dir() or base.name not in ("正式笔记", "候选笔记"):
                continue
            if "个人学习状态" in base.parts:
                continue
            for p in base.rglob("*.md"):
                try:
                    meta, _ = parse_file(p)
                except Exception:  # noqa: BLE001 —— 解析失败的文件不入索引（如实缺席）
                    continue
                nid = str(meta.get("id") or "").strip()
                if nid:
                    rel = p.relative_to(root).as_posix()
                    ids.setdefault(nid, []).append(
                        {"path": rel, "audit_status": str(meta.get("status") or "")})
        self._id_index_cache = {"built_at": now, "ids": ids}
        return ids

    def _resolve_by_id(self, note_id: str, original_path, fallback_message: str) -> dict:
        if not note_id:
            return {"status": "missing", "path": original_path, "relocated": False,
                    "candidates": [], "warnings": [],
                    "message": fallback_message + "；该关联没有稳定 ID，无法按 ID 回查"}
        hits = self._note_id_index().get(note_id, [])
        if len(hits) == 1:
            hit = hits[0]
            relocated = hit["path"] != original_path
            return {"status": "ok", "path": hit["path"], "relocated": relocated,
                    "audit_status": hit["audit_status"],
                    "candidates": [h["path"] for h in hits], "warnings": [],
                    "message": ("已按稳定 ID 在新位置找到（原路径失效，关联尚未持久化更新）"
                                if relocated else "")}
        if len(hits) > 1:
            return {"status": "ambiguous", "path": None, "relocated": False,
                    "candidates": [h["path"] for h in hits], "warnings": [],
                    "message": (f"稳定 ID {note_id} 命中 {len(hits)} 个文件，"
                                f"需人工裁决，不自动匹配（不按标题相似度猜测）")}
        return {"status": "missing", "path": original_path, "relocated": False,
                "candidates": [], "warnings": [],
                "message": (fallback_message +
                            f"；已按稳定 ID {note_id} 在合法笔记范围内回查，未找到，待定位")}

    def resolve_note_link(self, link: dict) -> dict:
        """把一条笔记关联解析到当前磁盘位置（阅读/节点详情/测验取材/外部接口共用）。

        - note_id 类：原路径存在且身份相符 → ok；原路径缺失或身份不符 → 按稳定 ID
          在合法笔记范围内回查，唯一匹配才解析；重复 ID → ambiguous；未找到 → missing。
        - registry 类：保持既有语义（指纹漂移 → ok + 警告；登记缺失/文件缺失 → missing）。
        - 不按标题相似度猜测身份；解析只读，不回写 state.json。
        """
        ref = link.get("ref") or {}
        path_rel = ref.get("path")
        if ref.get("kind") == "note_id":
            # 路径穿越：硬失败（安全信号，不降级为 missing）
            if path_rel:
                p = self.store.safe_read_path(path_rel)
                if p.exists():
                    try:
                        meta, _ = parse_file(p)
                    except Exception as exc:  # noqa: BLE001 —— 解析失败如实上报，不静默
                        return {"status": "missing", "path": path_rel, "relocated": False,
                                "candidates": [], "warnings": [],
                                "message": f"原路径文件存在但无法解析（{type(exc).__name__}），按 ID 回查"}
                    found = str(meta.get("id") or "").strip()
                    if found and found == ref.get("note_id"):
                        # 唯一性检查与旧路径是否存在无关：重复 ID 必须一致报告
                        dup_hits = [h["path"] for h in self._note_id_index().get(found, [])]
                        if len(dup_hits) > 1:
                            return {"status": "ambiguous", "path": None, "relocated": False,
                                    "audit_status": "", "candidates": dup_hits, "warnings": [],
                                    "message": (f"稳定 ID {found} 命中 {len(dup_hits)} 个文件，"
                                                f"需人工裁决，不自动匹配（不按标题相似度猜测）")}
                        return {"status": "ok", "path": path_rel, "relocated": False,
                                "audit_status": str(meta.get("status") or ""),
                                "candidates": [path_rel], "warnings": [], "message": ""}
                    if found:
                        # 登记路径存在但装着别的笔记：身份冲突，必须明确返回，
                        # 不静默换文件（可能是冒名/错放，需人工裁决）
                        return {"status": "identity_conflict", "path": path_rel,
                                "relocated": False, "candidates": [], "warnings": [],
                                "expected_id": ref.get("note_id"), "found_id": found,
                                "message": (f"身份冲突：登记路径当前文件 ID 是 {found}，"
                                            f"与关联登记的 {ref.get('note_id')} 不一致；不自动换文件")}
                    # 原路径文件无 ID：按稳定 ID 回查
            return self._resolve_by_id(
                ref.get("note_id"), path_rel,
                "来源文件不存在（可能被移动、重命名或删除）")
        # registry 类：统一校验（登记存在 → 路径存在 → 指纹一致），
        # 阅读/详情/测验/外部接口共用本入口，警告不在任何入口被丢弃。
        if not path_rel:
            return {"status": "no_path", "path": None, "relocated": False,
                    "candidates": [], "warnings": [],
                    "message": "该关联没有文件定位信息，无法读取原文"}
        self.store.safe_read_path(path_rel)   # 越界抛 PathOutsideVault
        registry = next((r for r in self.state()["ingest_registry"]
                         if r["registry_id"] == ref.get("registry_id")), None)
        if registry is None:
            return {"status": "missing", "path": path_rel, "relocated": False,
                    "candidates": [], "warnings": [],
                    "message": "关联指向的身份登记已不存在，无法确认文件身份。请修复关联。"}
        fp = self.store.vault_root / path_rel
        if not fp.exists():
            return {"status": "missing", "path": path_rel, "relocated": False,
                    "candidates": [], "warnings": [],
                    "message": "来源文件不存在（可能被移动或删除）；该类关联无稳定 ID，不自动回查"}
        audit_status = ""
        try:
            meta, _ = parse_file(fp)
            audit_status = str(meta.get("status") or "")
        except Exception:  # noqa: BLE001 —— 解析失败如实留空
            pass
        warnings = []
        if scanner.content_fingerprint(fp) != registry["fingerprint"]:
            warnings.append("待修复：文件内容与登记指纹不一致（内容已变化）；"
                            "以下为当前内容，请人工确认后更新登记。")
        return {"status": "ok", "path": path_rel, "relocated": False,
                "audit_status": audit_status, "warnings": warnings,
                "candidates": [path_rel], "message": ""}

    def read_note_content(self, link_id: str) -> dict:
        """通过已登记的笔记关联读取原文。安全规则：
        - 只接受 link_id，不接受任意磁盘路径；
        - 路径必须解析后位于测试知识库内（含真实库黑名单）；
        - 有稳定 ID 的关联：文件 front-matter 的 id 与登记 ID 不一致 → 拒绝展示（身份冲突）；
        - 登记指纹类关联：内容变化 → 展示但醒目警告（同一路径，内容已漂移，待人工确认）；
        - 返回纯文本，由界面转义渲染，绝不注入 HTML。"""
        state = self.state()
        link = next((l for l in state["note_links"] if l["link_id"] == link_id), None)
        if link is None:
            raise NotFound("笔记关联", link_id)
        ref = link["ref"]
        path_rel = ref.get("path")
        if not path_rel:
            raise ValidationError("该关联没有文件定位信息，无法读取原文")
        resolution = self.resolve_note_link(link)
        if resolution["status"] != "ok":
            return {"status": resolution["status"], "path": resolution.get("path") or path_rel,
                    "candidates": resolution.get("candidates") or [],
                    "relocated": resolution.get("relocated", False),
                    "message": resolution["message"]}
        path = self.store.safe_read_path(resolution["path"])
        warnings = list(resolution.get("warnings") or [])
        if ref["kind"] == "note_id":
            meta, body = parse_file(path)
            found_id = str(meta.get("id") or "").strip()
            if found_id != ref.get("note_id"):
                return {
                    "status": "identity_conflict",
                    "path": resolution["path"],
                    "expected_id": ref.get("note_id"),
                    "found_id": found_id or None,
                    "message": (
                        f"身份冲突：该路径当前文件的笔记 ID 是 {found_id or '（无）'}，"
                        f"与关联登记的 {ref.get('note_id')} 不一致。已拒绝展示，请人工确认后再更新关联。"
                    ),
                }
        else:
            # 登记存在性/指纹校验已在 resolve_note_link 统一完成（警告透传）
            meta, body = parse_file(path)
        return {
            "status": "ok",
            "path": resolution["path"],
            "relocated": resolution.get("relocated", False),
            "title": str(meta.get("title") or "").strip() or path.stem,
            "note_id": str(meta.get("id") or "").strip() or None,
            "warnings": warnings,
            "content": body,
        }
