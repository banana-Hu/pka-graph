# -*- coding: utf-8 -*-
"""发现模块补充改造（真实换行锚点版，一次性脚本，用后即删）。"""
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
p = ROOT / "pka_graph" / "discovery.py"
t = p.read_text(encoding="utf-8")

# 1) 导入 threading
a = "import secrets\nimport uuid"
b = "import secrets\nimport threading\nimport uuid"
assert a in t, "imports"
t = t.replace(a, b, 1)

# 2) init：分析器 + 意图日志 + 周期锁
a = """    def __init__(self, graph_service: GraphService, upstream: list[dict] | None = None,
                 clock=utcnow):
        self.svc = graph_service"""
b = """    def __init__(self, graph_service: GraphService, upstream: list[dict] | None = None,
                 clock=utcnow, analyzer=None, analyzer_budget=None, analyzer_mode="unconfigured"):
        self.svc = graph_service
        self.analyzer = analyzer                # 语义分析器；None = 未配置（只做规则预筛）
        self.analyzer_budget = analyzer_budget
        self.analyzer_mode = analyzer_mode
        self.intents_path = self.svc.store.state_path.parent / "discovery_intents.json"
        self.check_lock = threading.Lock()      # 防重叠：一轮未完成不叠加下一轮
        self._periodic_thread = None"""
assert a in t, "init anchor"
t = t.replace(a, b, 1)

# 3) 意图日志助手（插在存储段前）
a = """    # ---------- 存储 ----------

    def _load_suggestions(self) -> list[dict]:"""
b = """    # ---------- 接受意图日志（最小事务记录） ----------

    def _load_intents(self) -> list[dict]:
        if not self.intents_path.exists():
            return []
        try:
            data = json.loads(self.intents_path.read_text(encoding="utf-8"))
            return data if isinstance(data, list) else []
        except json.JSONDecodeError:
            return []

    def _save_intents(self, intents: list[dict]) -> None:
        self.intents_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.intents_path.with_name(f".discovery_intents.json.tmp-{secrets.token_hex(4)}")
        tmp.write_text(json.dumps(intents, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(self.intents_path)

    # ---------- 存储 ----------

    def _load_suggestions(self) -> list[dict]:"""
assert a in t, "intents anchor"
t = t.replace(a, b, 1)

# 4) 队列视图：能力三层区分
a = """        orphans = self._orphan_notes(suggestions)
        return {"suggestions": rows, "count": len(rows), "orphan_notes": orphans,
                "banner": "这里是待审核建议，不代表用户已经了解或掌握。"}"""
b = """        orphans = self._orphan_notes(suggestions)
        return {"suggestions": rows, "count": len(rows), "orphan_notes": orphans,
                "analysis": self.analysis_status(),
                "banner": "待审核建议。规则预筛结果不等于语义分析结论，更不代表用户已经了解或掌握。"}

    def analysis_status(self) -> dict:
        \"\"\"能力三层区分：规则预筛（本轮已做）/ 语义分析（按配置）/ 用户审核（始终必需）。\"\"\"
        if self.analyzer is not None:
            semantic = {"status": "configured", "adapter": self.analyzer.describe()}
        else:
            semantic = {"status": "unconfigured",
                        "reason": "未配置发现模块模型（PKA_DISCOVERY_ADAPTER=http + "
                                  "PKA_DISCOVERY_API_BASE/API_KEY/MODEL）；语义分析未执行，"
                                  "规则预筛结果仅供初筛参考"}
        return {"rule_prescreen": "已执行（确定性规则：可解释、可回归；不能认定知识增量已核实）",
                "semantic_analysis": semantic,
                "user_review": "必需：所有建议必须经人工审核，接受也不代表用户已了解或掌握"}"""
assert a in t, "analysis_status anchor"
t = t.replace(a, b, 1)

# 5) check 返回带 analysis 标注
a = """            "last_success": section["last_success"],
            "note": "确定性提取（deterministic_shallow）；真实模型的增量判断未启用、未验证",
        }"""
b = """            "last_success": section["last_success"],
            "analysis": self.analysis_status(),
            "note": ("规则预筛完成（deterministic_shallow）；语义分析"
                     + ("由配置的模型执行" if self.analyzer else "未配置、未执行")
                     + "；预筛结果不代表知识增量已核实"),
        }"""
assert a in t, "check return anchor"
t = t.replace(a, b, 1)

p.write_text(t, encoding="utf-8")
print("discovery.py supplemented (batch 1)")
