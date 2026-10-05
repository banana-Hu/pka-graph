"""第七阶段：外部 AI 只读接入（最小 MCP-like HTTP 接口）。

外部 AI 客户端通过带令牌的 HTTP GET 调用以下只读端点：
- /api/ext/search?q=       搜索节点（名称/别名/范围）
- /api/ext/node/{id}       读取节点详情（范围/状态/来源/关联笔记列表）
- /api/ext/status          查询整体掌握统计

权限：
- 只读，无任何写操作
- 需要独立的外部访问令牌（PKA_EXT_TOKEN 环境变量；不设置则关闭）
- 不暴露 state.json 全文、备份、密钥、临时问答
- candidate/accepted 不等于掌握；demo 来源标注为测试数据
"""

from __future__ import annotations


def build_ext_tools(service) -> dict:
    """返回外部 AI 可调用的只读工具清单与说明。"""
    return {
        "tools": [
            {"name": "search_nodes",
             "description": "搜索知识节点（按名称/别名/范围子串匹配）",
             "method": "GET", "path": "/api/ext/search",
             "params": {"q": "搜索关键词（必填）"}},
            {"name": "get_node",
             "description": "读取节点详情：范围、掌握状态、状态来源、关联笔记列表",
             "method": "GET", "path": "/api/ext/node/{node_id}",
             "params": {"node_id": "节点稳定 ID（必填）"}},
            {"name": "get_status",
             "description": "查询整体掌握统计：掌握层/视野层数量与全部节点列表",
             "method": "GET", "path": "/api/ext/status"},
        ],
        "semantics": [
            "status=passed → 已通过测验（掌握层）；但一个节点通过不代表整个领域都掌握",
            "status=untested|failed → 视野层（尚未测验或未通过）",
            "status_origin=demo → 演示数据，不是真实测验结果",
            "audit_status=candidate → 候选笔记，未经人工接受",
            "audit_status=accepted → 已接受的正式笔记；接受笔记不等于用户掌握",
        ],
        "readonly": True,
    }


def ext_search(service, q: str) -> dict:
    """外部搜索：复用 graph_view 的节点列表，只返回安全字段。"""
    nodes = service.list_nodes(name=q)
    return {
        "query": q,
        "search_kind": "node_search",
        "search_scope": "节点名称/别名/scope 子串匹配（不含笔记全文检索）",
        "results": [{
            "id": n["id"], "name": n["name"], "domain": n["domain"],
            "status": n["status"],
            "layer": "mastery" if n["status"] == "passed" else "vision",
            "scope": n.get("scope"),
            "status_origin": n.get("status_origin"),
        } for n in nodes],
        "semantics": "一个节点通过不代表整个领域都掌握；candidate ≠ 掌握；accepted ≠ 掌握；demo ≠ 真实测验结果",
    }


def ext_node(service, node_id: str) -> dict:
    """外部读取节点详情：只返回安全字段，不含密钥/备份/临时问答。"""
    detail = service.node_detail(node_id)
    node = detail["node"]
    return {
        "id": node["id"], "name": node["name"],
        "domain": node["domain"], "scope": node["scope"],
        "status": node["status"],
        "layer": "mastery" if node["status"] == "passed" else "vision",
        "status_origin": node.get("status_origin"),
        "last_quiz_at": node.get("last_quiz_at"),
        "next_review_at": node.get("next_review_at"),
        "aliases": node.get("aliases"),
        "note_links": [{
            "registered_path": l.get("path") or (l.get("ref") or {}).get("path"),
            "resolution": l.get("resolution") or {},
            "audit_status": (l.get("resolution") or {}).get("audit_status"),
        } for l in detail["note_links"]],
        "relations": [{
            "other": r.get("to_name") if r["from_id"] == node_id else r.get("from_name"),
            "relation": r["relation"],
        } for r in detail["relations"]],
        "semantics": ("一个节点通过不代表整个领域都掌握；candidate 笔记 ≠ 已接受；demo ≠ 真实测验；"
                      "audit_status 来自笔记 frontmatter 的实际 status 字段"
                      "（candidate/accepted/published，可能为空），"
                      "resolution.status ∈ ok|missing|identity_conflict|ambiguous|no_path"),
    }


def ext_status(service) -> dict:
    """外部查询掌握统计。"""
    nodes = service.list_nodes()
    mastery = [n for n in nodes if n["status"] == "passed"]
    vision = [n for n in nodes if n["status"] != "passed"]
    return {
        "total_active": len(nodes),
        "mastery_count": len(mastery),
        "vision_count": len(vision),
        "mastery_nodes": [{"id": n["id"], "name": n["name"], "domain": n["domain"],
                           "status_origin": n.get("status_origin")} for n in mastery],
        "semantics": "一个节点通过不代表整个领域都掌握；candidate ≠ 掌握；accepted ≠ 掌握；demo ≠ 真实测验结果",
    }
