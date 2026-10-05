"""第三阶段演示夹具：生成包含各类边界情况的专用测试副本知识库。

用法：python demo/make_phase3_fixture.py [--out 目录] [--regenerate]
默认输出 demo/fixture_vault/。--regenerate 允许覆盖**本脚本自己生成的**目录
（校验其中含有本脚本写入的标记文件，绝不覆盖手工目录或真实库）。

包含情况：两个层级三种状态、多领域、同名不同范围、长名称、孤立节点、
三种关系、归档节点、无法解析的笔记关联、含 HTML/脚本样文的笔记。
passed 状态由本脚本通过受控存储写入并明确标注为测试数据；
正常用户界面没有"直接标记通过"的功能。
"""

import argparse
import json
import sys
from pathlib import Path

WORKSPACE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(WORKSPACE))

from pka_graph.models import empty_state, make_node_link, make_note_link, new_node, now_iso  # noqa: E402
from pka_graph.store import StateStore  # noqa: E402

MARKER_FILE = ".pka-phase3-fixture.json"


def build_fixture(out: Path) -> dict:
    out = Path(out).resolve()
    if out.exists() and not (out / MARKER_FILE).exists():
        raise SystemExit(f"拒绝：{out} 已存在且不是本脚本生成的夹具目录")
    # 笔记文件
    (out / "正式笔记").mkdir(parents=True, exist_ok=True)
    (out / "候选笔记").mkdir(parents=True, exist_ok=True)
    (out / "个人学习状态").mkdir(parents=True, exist_ok=True)

    formal = """---
id: note-20260926-001
status: accepted
title: "Unit 1：为什么需要 Harness"
domain: "AI技术领域"
---

# Unit 1：为什么需要 Harness

**测试数据**：复制自第二阶段测试库的正式笔记样本。

核心结论：完成必须由证据而不是 Agent 自评决定。
"""
    html_note = """---
id: note-20261001-777
status: candidate
title: "XSS 渲染测试笔记（含脚本样文）"
domain: "AI技术领域"
---

# 渲染安全测试

**测试数据**：本笔记正文包含脚本与注入样文，用于验证界面渲染必须转义、不能执行。

<script>alert('xss-by-script')</script>

<img src=x onerror="alert('xss-by-img')">

[点击领奖](javascript:alert('xss-by-link')) 与 <b>加粗注入</b> 与 <iframe src="about:blank"></iframe>

正常段落：图图谱渲染应当把以上内容显示为纯文本。
"""
    no_id_note = """---
title: "无 ID 的长名称测试笔记：关于大语言模型上下文窗口管理、截断策略与记忆分层机制的完整梳理"
domain: "AI技术领域"
---

无 ID 测试数据：正文与长名称节点关联，走登记指纹路径。
"""
    (out / "正式笔记" / "04-Unit1.md").write_text(formal, encoding="utf-8")
    (out / "候选笔记" / "xss-渲染测试.md").write_text(html_note, encoding="utf-8")
    (out / "候选笔记" / "无ID长名称笔记.md").write_text(no_id_note, encoding="utf-8")

    # 节点状态
    state = empty_state()

    def add(**kw) -> dict:
        node = new_node(name=kw["name"], domain=kw["domain"], scope=kw["scope"], aliases=kw.get("aliases"))
        state["nodes"].append(node)
        return node

    def force_status(node_id: str, status: str) -> None:
        """受控测试入口：模拟"测验通过/未通过"之后的真实状态。
        正常界面不提供此能力；这里写死并标注为测试数据。"""
        for n in state["nodes"]:
            if n["id"] == node_id:
                n["status"] = status
                n["last_quiz_at"] = "2026-10-01T10:00:00+00:00"
                n["next_review_at"] = "2026-10-04T10:00:00+00:00" if status == "passed" else None

    agent = add(name="Agent 工具调用机制", domain="AI技术领域", scope="工具调用的请求-结果循环、超时与错误处理、权限边界")
    prompt = add(name="提示词工程基础", domain="AI技术领域", scope="角色、边界、验收样例三类手法与适用场景")
    ctx_a = add(name="上下文窗口管理", domain="AI技术领域", scope="窗口的构成、计费影响与超限行为")
    ctx_b = add(name="上下文窗口管理", domain="AI技术领域", scope="截断、压缩与记忆分层策略（与前者同名不同范围）")
    long_name = add(
        name="关于大语言模型上下文窗口管理、截断策略与记忆分层机制的完整梳理（这是一个用于测试截断与悬浮提示的超长节点名称）",
        domain="AI技术领域",
        scope="长名称截断显示测试",
        aliases=["长名测试", "memory-layer"],
    )
    rest = add(name="REST API 设计", domain="程序开发领域", scope="资源建模、状态码语义与版本化")
    env = add(name="Python 虚拟环境管理", domain="程序开发领域", scope="venv/依赖锁定/多版本共存")
    interview = add(name="用户访谈提纲设计", domain="产品领域", scope="开放题与追问结构、避免诱导")
    ab_test = add(name="A/B 实验指标", domain="产品领域", scope="北极星与护栏指标的取舍")
    orphan = add(name="孤立的心理学概念节点：峰终定律", domain="心理学领域", scope="没有任何关系的孤立节点测试")
    special = add(name="带<script>alert(1)</script>的范围", domain="AI技术领域", scope="特殊字符 <>&\" 节点")
    legacy = add(name="被合并掉的历史节点", domain="AI技术领域", scope="合并测试的归档源")

    force_status(agent["id"], "passed")
    force_status(env["id"], "passed")
    force_status(ab_test["id"], "failed")
    force_status(rest["id"], "failed")

    # 关系：三种类型、跨层、涉及归档节点（应被图谱排除）
    state["node_links"].append(make_node_link(agent["id"], prompt["id"], "prerequisite"))   # Agent 前置=提示词
    state["node_links"].append(make_node_link(long_name["id"], ctx_b["id"], "extends"))     # 长名延伸自窗口策略
    state["node_links"].append(make_node_link(ctx_a["id"], ctx_b["id"], "related"))
    state["node_links"].append(make_node_link(rest["id"], env["id"], "prerequisite"))
    state["node_links"].append(make_node_link(legacy["id"], agent["id"], "related"))        # 连到归档节点 → 图谱中隐藏

    # 归档节点（合并归档样例）
    for n in state["nodes"]:
        if n["id"] == legacy["id"]:
            n["archived"] = True
            n["archived_at"] = now_iso()
            n["archive_note"] = {"reason": "merged", "into": agent["id"], "status_before": "untested"}

    # 笔记关联：有 ID / 登记指纹 / 无法解析（指向不存在的文件）
    state["note_links"].append(
        {"link_id": "lnk-formal01", "node_id": agent["id"],
         "ref": {"kind": "note_id", "note_id": "note-20260926-001", "path": "正式笔记/04-Unit1.md"},
         "created_at": now_iso()}
    )
    state["ingest_registry"].append(
        {"registry_id": "reg-xss0001", "path": "候选笔记/xss-渲染测试.md",
         "fingerprint": __import__("hashlib").sha256(html_note.encode("utf-8")).hexdigest().upper(),
         "registered_at": now_iso()}
    )
    state["note_links"].append(
        {"link_id": "lnk-xss0001", "node_id": prompt["id"],
         "ref": {"kind": "registry", "registry_id": "reg-xss0001", "path": "候选笔记/xss-渲染测试.md"},
         "created_at": now_iso()}
    )
    state["ingest_registry"].append(
        {"registry_id": "reg-long0001", "path": "候选笔记/无ID长名称笔记.md",
         "fingerprint": __import__("hashlib").sha256(no_id_note.encode("utf-8")).hexdigest().upper(),
         "registered_at": now_iso()}
    )
    state["note_links"].append(
        {"link_id": "lnk-long0001", "node_id": long_name["id"],
         "ref": {"kind": "registry", "registry_id": "reg-long0001", "path": "候选笔记/无ID长名称笔记.md"},
         "created_at": now_iso()}
    )
    # 无法解析的关联：登记存在但文件已不存在
    state["ingest_registry"].append(
        {"registry_id": "reg-gone0001", "path": "候选笔记/已被移动的笔记.md",
         "fingerprint": "DEADBEEF" * 8, "registered_at": now_iso()}
    )
    state["note_links"].append(
        {"link_id": "lnk-gone0001", "node_id": ctx_a["id"],
         "ref": {"kind": "registry", "registry_id": "reg-gone0001", "path": "候选笔记/已被移动的笔记.md"},
         "created_at": now_iso()}
    )

    store = StateStore(out)
    store.save(state, base_version=None)
    (out / ".pka-test-vault.json").write_text(
        json.dumps({"purpose": "test", "created_by": "demo/make_phase3_fixture.py"}, ensure_ascii=False),
        encoding="utf-8",
    )
    (out / MARKER_FILE).write_text(
        json.dumps({"generated_by": "demo/make_phase3_fixture.py", "generated_at": now_iso()}, ensure_ascii=False),
        encoding="utf-8",
    )
    return {
        "vault": str(out),
        "nodes": len(state["nodes"]),
        "passed": sum(1 for n in state["nodes"] if n["status"] == "passed"),
        "failed": sum(1 for n in state["nodes"] if n["status"] == "failed"),
        "archived": sum(1 for n in state["nodes"] if n["archived"]),
        "note_links": len(state["note_links"]),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="生成第三阶段演示夹具测试库（含测试标注的模拟状态）")
    parser.add_argument("--out", default=str(WORKSPACE / "demo" / "fixture_vault"))
    parser.add_argument("--regenerate", action="store_true", help="覆盖本脚本此前生成的夹具目录")
    args = parser.parse_args()
    out = Path(args.out)
    if out.exists() and not args.regenerate:
        print(f"目录已存在（不覆盖）：{out}\n如需重新生成请加 --regenerate（仅允许覆盖本脚本生成的目录）")
        return
    if out.exists():
        marker = out / MARKER_FILE
        if not marker.exists():
            raise SystemExit("拒绝 --regenerate：目标目录不是本脚本生成的夹具")
        import shutil

        shutil.rmtree(out)
    summary = build_fixture(out)
    print("夹具已生成（全部为测试数据，passed 状态是受控写入的模拟测验结果）：")
    for k, v in summary.items():
        print(f"  {k}: {v}")


if __name__ == "__main__":
    main()
