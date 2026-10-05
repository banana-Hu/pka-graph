# -*- coding: utf-8 -*-
"""发现模块补充改造批次二：新颖性措辞、预筛守卫、接受意图与恢复、周期线程。"""
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
p = ROOT / "pka_graph" / "discovery.py"
t = p.read_text(encoding="utf-8")

# 3) 阈值降为预筛工具：短标题守卫 + 措辞改为预筛
a = """    best_score = related[0]["score"] if related else 0.0

    if not readable:"""
b = """    best_score = related[0]["score"] if related else 0.0
    cand_token_count = len(tokens(title or "") | tokens(material_text or ""))
    # 阈值只是预筛工具：短文本的包含度不可靠（短标题易被长文本包含），
    # token 过少时不用阈值下结论，转待核查。
    thresholds_reliable = cand_token_count >= 12

    if not readable:"""
assert a in t, "threshold guard anchor"
t = t.replace(a, b, 1)

a = """    if best_score >= 0.75:
        return ("duplicate",
                f"与已有节点「{related[0]['name']}」高度重叠（确定性包含度 {best_score:.2f}），不新增节点",
                related)
    if CONFLICT_SIGNALS.search(material_text or "") and best_score >= 0.15:"""
b = """    if best_score >= 0.75 and thresholds_reliable:
        return ("duplicate",
                f"规则预筛：与已有节点「{related[0]['name']}」包含度 {best_score:.2f}，疑似重复"
                "（待语义分析与人工确认）",
                related)
    if CONFLICT_SIGNALS.search(material_text or "") and best_score >= 0.15 and thresholds_reliable:"""
assert a in t, "duplicate guard anchor"
t = t.replace(a, b, 1)

a = """    if VERSION_SIGNALS.search((title or "") + " " + (material_text or "")) and best_score >= 0.25:
        return ("revision",
                f"涉及版本变化且与「{related[0]['name']}」相关，可能影响旧方法，生成更新提案",
                related)
    if best_score >= 0.30:"""
b = """    if VERSION_SIGNALS.search((title or "") + " " + (material_text or "")) and best_score >= 0.25 \\
            and thresholds_reliable:
        return ("revision",
                f"规则预筛：涉及版本变化且与「{related[0]['name']}」相关，疑似影响旧方法"
                "（生成更新提案，待人工确认）",
                related)
    if best_score >= 0.30:"""
assert a in t, "version guard anchor"
t = t.replace(a, b, 1)

a = """    if best_score >= 0.30:
        return ("supplement",
                f"与「{related[0]['name']}」相关且包含补充解释/例证，建议作为资料关联补充",
                related)"""
b = """    if best_score >= 0.30:
        return ("supplement",
                f"规则预筛：与「{related[0]['name']}」相关，疑似补充解释/例证"
                "（待语义分析与人工确认）",
                related)"""
assert a in t, "supplement wording anchor"
t = t.replace(a, b, 1)

a = """    if not extraction["has_mechanism"]:
        if NEWS_TITLE_SIGNALS.search(title or ""):
            return ("news_only", "事件/商业信号为主且未提取到机制类贡献", related)
        return ("news_only", "材料未包含可核查的机制/方法类知识增量（可能仅为产品/事件介绍）", related)
    return ("new_node", "知识库中未检索到相关节点，且材料包含可核查的机制/方法类贡献", related)"""
b = """    if not extraction["has_mechanism"]:
        if NEWS_TITLE_SIGNALS.search(title or ""):
            return ("news_only",
                    "规则预筛：事件/商业信号为主且未提取到机制类贡献（可人工复核；材料提供了什么才是判定依据）",
                    related)
        return ("news_only",
                "规则预筛：材料未包含可核查的机制/方法类知识增量，待核查"
                "（如人工确认存在增量，可在审核后手动创建节点）",
                related)
    return ("new_node",
            "规则预筛：本次检索未找到相关节点，待确认；材料包含机制/方法类表述"
            "（待语义分析与人工确认后才算知识增量）",
            related)"""
assert a in t, "tail wording anchor"
t = t.replace(a, b, 1)

p.write_text(t, encoding="utf-8")
print("batch 2 applied")
