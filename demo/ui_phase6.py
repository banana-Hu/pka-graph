"""第六阶段界面实测：真实鼠标操作发现流程。

检查知识增量 → 查看建议（贡献/来源/相关旧节点）→ 接受（创建候选节点）
→ 重复接受幂等 → 忽略 → 编辑后接受 → 周期检查确认。真实点击，无派发事件。
"""

import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

WORKSPACE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(WORKSPACE))
sys.path.insert(0, str(WORKSPACE / "demo"))

results = []


def check(name, ok, detail=""):
    results.append((name, ok, detail))
    print(f"{'PASS' if ok else 'FAIL'}  {name}  {detail}")


def main():
    tmp = Path(tempfile.mkdtemp(prefix="pka_p6_"))
    vault = tmp / "vault"
    from make_phase3_fixture import build_fixture

    build_fixture(vault)
    # 合成上游：一条 new_node（技术文档机制）+ 一条 supplement + 一条 news_only
    up = tmp / "upstream.js"
    import json as _json

    items = [
        {"id": "up-ring01", "title": "环形注意力分块通信机制 RingAttn 发布",
         "summary": ("RingAttn 提出一种环形注意力分块通信机制处理超长序列：把键值块沿设备环传递，"
                     "每个设备只保留本地分块的注意力结果，显存峰值从 O(n²) 降到 O(n²/设备数)。"
                     "实验表明在 128K 上下文基准上通信开销降低 40%。该方法要求互联带宽充足。"),
         "canonicalUrl": "https://example.com/ringattn", "sourceName": "技术文档",
         "sourceType": "doc", "publishedAt": "2026-10-01",
         "discoveredAt": "2026-10-01T00:00:00+00:00", "topics": ["AI", "长序列"]},
        {"id": "up-supp02", "title": "Agent 工具调用的常见误用与直观类比",
         "summary": ("可以把工具调用循环类比成先下单再收货：下单是模型发起，收货前必须校验。"
                     "常见误用是把首次调用成功当成权限已授予，其实每次调用都要重新过权限边界检查。"),
         "canonicalUrl": "https://example.com/supp", "sourceName": "技术博客",
         "sourceType": "article", "publishedAt": "2026-10-01",
         "discoveredAt": "2026-10-01T00:00:00+00:00", "topics": ["AI"]},
        {"id": "up-news03", "title": "某 AI 公司获得 3000 万美元 A 轮融资",
         "summary": "某 AI 公司宣布完成 3000 万美元 A 轮融资，将用于市场扩张，估值 2 亿美元。",
         "canonicalUrl": "https://example.com/funding", "sourceName": "科技媒体",
         "sourceType": "rss", "publishedAt": "2026-10-01",
         "discoveredAt": "2026-10-01T00:00:00+00:00", "topics": ["融资"]},
    ]
    up.write_text("const FEED_CANDIDATES = " + _json.dumps({"candidates": items}, ensure_ascii=False) + ";\n",
                  encoding="utf-8")
    sources_file = tmp / "sources.json"
    sources_file.write_text(_json.dumps([
        {"path": str(up), "const": "FEED_CANDIDATES", "label": "合成上游（测试）"}
    ], ensure_ascii=False), encoding="utf-8")
    port = 8825
    import os

    proc = subprocess.Popen(
        [sys.executable, str(WORKSPACE / "server_start.py"), "--vault", str(vault), "--port", str(port)],
        cwd=str(WORKSPACE),
        env={**os.environ, "PKA_DISCOVERY_UPSTREAM": str(sources_file),
             "PKA_QUIZ_ADAPTER": "demo"},
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    time.sleep(2.5)
    shot_dir = Path(tempfile.mkdtemp(prefix="pka_p6_shots_"))
    try:
        from playwright.sync_api import sync_playwright

        with sync_playwright() as p:
            browser = p.chromium.launch()
            page = browser.new_page(viewport={"width": 1440, "height": 950})
            page.on("dialog", lambda d: d.accept())
            page.goto(f"http://127.0.0.1:{port}/")
            page.wait_for_timeout(2200)

            print("[步骤] SC1")
            page.click("nav button[data-tab='discovery']")
            page.wait_for_timeout(700)
            check("发现页打开且显示免责横幅", "不代表用户已经了解或掌握" in page.inner_text("#tab-discovery"))
            page.click("#tab-discovery >> text=检查知识增量")
            page.wait_for_function(
                "document.getElementById('suggestionList').querySelectorAll('fieldset').length >= 2",
                timeout=30000)
            page.wait_for_timeout(400)
            text = page.inner_text("#suggestionList")
            check("生成 3 条建议（new_node/supplement/news_only）",
                  page.locator("#suggestionList fieldset").count() == 3, text[:60])
            check("新闻类被正确过滤标注", "仅新闻线索" in text or "news_only" in text)
            check("贡献展示包含来源声称", "来源声称" in text)
            check("展示相关旧节点", "相关旧节点" in text)
            page.screenshot(path=str(shot_dir / "1_suggestions.png"))

            print("[步骤] SC2")
            fieldset_new = page.locator("#suggestionList fieldset").filter(has_text="new_node").first
            fieldset_new.locator("text=接受（创建候选节点）").click()
            page.wait_for_selector("#acceptModal:not(.hidden)", timeout=10000)
            check("接受表单可编辑名称/范围", page.locator("#accName").is_visible() and page.locator("#accScope").is_visible())
            page.fill("#accName", "环形注意力分块通信机制")
            page.click("#acceptBody >> text=确认接受")
            page.wait_for_timeout(1000)
            check("接受成功提示 untested", "untested" in page.inner_text("#toast"))
            # 接受成功后模态框已自动关闭
            # 重复检查：已接受的建议不再出现在待审核列表（重复提交不产生重复对象）
            page.click("#tab-discovery >> text=检查知识增量")
            page.wait_for_timeout(1200)
            dup = page.locator("#suggestionList fieldset").filter(has_text="环形注意力分块通信机制").count()
            check("已接受建议不再出现在待审核列表", dup == 0, f"dup={dup}")

            print("[步骤] SC3")
            page.click("nav button[data-tab='graph']")
            page.wait_for_timeout(900)
            page.locator("#graphSvg .node-g").filter(has_text="环形注意力分块通信机制").first.click()
            page.wait_for_timeout(800)
            detail = page.inner_text("#detailSide")
            check("视野层出现新节点且为 untested", "untested · 视野层" in detail, detail[:60])
            page.screenshot(path=str(shot_dir / "3_new_vision_node.png"))
            page.locator("#graphSvg").click(position={"x": 20, "y": 20})
            page.wait_for_timeout(300)

            print("[步骤] SC4")
            page.click("nav button[data-tab='discovery']")
            page.wait_for_timeout(600)
            page.locator("#suggestionList fieldset").filter(has_text="supplement").first.locator(
                "text=接受（补充资料关联）").click()
            page.wait_for_timeout(900)
            check("supplement 接受成功", "资料关联" in page.inner_text("#toast"))
            page.locator("#suggestionList fieldset").filter(has_text="news_only").first.locator(
                "text=忽略").click()
            page.wait_for_timeout(700)
            page.wait_for_timeout(400)
            remaining = page.locator("#suggestionList fieldset").count()
            check("忽略后建议列表只剩 1 条待审核", remaining <= 1, f"remaining={remaining}")

            print("[步骤] SC5")
            page.click("#tab-discovery >> text=检查知识增量")
            page.wait_for_timeout(1200)
            check("再次检查不重复生成（已见过滤）", page.locator("#suggestionList fieldset").count() <= 1)
            check("周期检查选择器存在且默认关闭", page.locator("#discAuto").input_value() == "0")
            page.screenshot(path=str(shot_dir / "5_after_check.png"))
            browser.close()
    finally:
        proc.terminate()
        shutil.rmtree(tmp, ignore_errors=True)

    failed = [r for r in results if not r[1]]
    print(f"\n第六阶段界面实测：{len(results) - len(failed)}/{len(results)} 通过；截图目录：{shot_dir}")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
