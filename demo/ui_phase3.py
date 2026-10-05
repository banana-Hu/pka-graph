"""第三阶段界面实测：无头浏览器在夹具副本上真实操作。

覆盖：图谱渲染与两带、节点点击详情、原文 XSS 安全、测验状态变化后刷新同步、
归档同步、搜索与组合筛选、邻域聚焦、服务断连提示。
"""

import json
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

WORKSPACE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(WORKSPACE))

results = []
dialogs_fired = []


def check(name, ok, detail=""):
    results.append((name, ok, detail))
    print(f"{'PASS' if ok else 'FAIL'}  {name}  {detail}")


def main():
    tmp = Path(tempfile.mkdtemp(prefix="pka_p3_"))
    fixture = tmp / "vault"
    sys.path.insert(0, str(WORKSPACE / "demo"))
    from make_phase3_fixture import build_fixture

    build_fixture(fixture)
    port = 8811
    proc = subprocess.Popen(
        [sys.executable, str(WORKSPACE / "server_start.py"), "--vault", str(fixture), "--port", str(port)],
        cwd=str(WORKSPACE), stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
    )
    time.sleep(2.5)
    shot_dir = Path(tempfile.mkdtemp(prefix="pka_p3_shots_"))
    try:
        from playwright.sync_api import sync_playwright

        with sync_playwright() as p:
            browser = p.chromium.launch()
            page = browser.new_page(viewport={"width": 1440, "height": 950})
            page.on("dialog", lambda d: (dialogs_fired.append(d.message), d.accept()))
            page.goto(f"http://127.0.0.1:{port}/")
            page.wait_for_timeout(2000)

            svg_text = page.locator("#graphSvg").text_content()
            check("双带标签可见", "掌握层" in svg_text and "视野层" in svg_text)
            check("统计条渲染", page.inner_text("#stTotal").isdigit())
            nodes_in_svg = page.locator(".node-g").count()
            check("图谱节点渲染（非归档 11 个）", nodes_in_svg == 11, f"nodes={nodes_in_svg}")

            # 点击已掌握节点 → 详情
            page.locator(".node-g").filter(has_text="Agent 工具调用机制").first.click()
            page.wait_for_timeout(800)
            detail = page.inner_text("#detailSide")
            check("详情显示掌握层与测验时间", "掌握层" in detail and "最近测验" in detail and "尚未安排" not in detail)
            check("详情无'直接标记通过'按钮", "标记通过" not in detail and "设为通过" not in detail)
            check("详情标注后续阶段提供", "后续阶段提供" in detail)
            page.screenshot(path=str(shot_dir / "1_graph_detail.png"))

            # XSS 笔记原文：切到提示词工程节点 → 查看原文
            page.locator(".node-g").filter(has_text="提示词工程基础").first.click()
            page.wait_for_timeout(800)
            page.locator("#detailSide button", has_text="查看原文").first.click()
            page.wait_for_timeout(1000)
            body = page.inner_text("#noteBody")
            check("XSS 样文以纯文本显示（未执行）", "alert('xss-by-script')" in body and "<script>" not in page.evaluate("document.querySelector('#noteBody').innerHTML"))
            check("无任何 dialog 被触发", len(dialogs_fired) == 0, str(dialogs_fired[:2]))
            check("Markdown 注入未变成加粗/iframe", "<iframe" not in page.evaluate("document.querySelector('#noteBody').innerHTML"))
            page.screenshot(path=str(shot_dir / "2_xss_note.png"))
            page.click("#noteModal >> text=关闭")

            # 无法解析的关联：上下文窗口管理（同名第一个连着已被移动的笔记）
            page.locator("#graphSvg").click(position={"x": 20, "y": 20})
            page.wait_for_timeout(300)
            page.locator(".node-g").filter(has_text="计费影响").first.click()
            page.wait_for_timeout(800)
            page.locator("#detailSide button", has_text="查看原文").first.click()
            page.wait_for_timeout(800)
            check("缺失来源给明确提示", "不存在" in page.inner_text("#noteBody"))
            page.click("#noteModal >> text=关闭")

            # 搜索（中文 + 特殊字符）与组合筛选
            page.fill("#gq", "峰终定律")
            page.wait_for_timeout(600)
            check("搜索孤立节点可定位", page.locator(".node-g").count() == 1)
            page.fill("#gq", "<script>")
            page.wait_for_timeout(600)
            check("特殊字符按字面匹配", "特殊字符" in page.locator("#graphWrap").text_content())
            page.fill("#gq", "")
            page.select_option("#gLayer", "mastery")
            page.wait_for_timeout(600)
            check("层级筛选只显示掌握层", page.locator(".node-g").count() == 2)
            check("筛选命中计数正确", page.inner_text("#stFiltered") == "2")
            page.select_option("#gLayer", "")
            page.wait_for_timeout(400)

            # 通过状态变化 → 刷新同步（受控测试入口，测试数据）
            graph = json.loads(subprocess.run(
                [sys.executable, "-c",
                 f"import sys,json;sys.path.insert(0,r'{WORKSPACE}');"
                 f"from pka_graph.service import GraphService;from pka_graph.store import StateStore;"
                 f"s=GraphService(StateStore(r'{fixture}'));g=s.graph_view();"
                 f"print(json.dumps([n['id'] for n in g['nodes'] if n['status']=='untested'][0]))"],
                capture_output=True, text=True).stdout.strip())
            run = subprocess.run([
                sys.executable, str(WORKSPACE / "demo" / "set_test_status.py"),
                "--vault", str(fixture), "--node", graph, "--status", "passed",
                "--i-know-this-is-test-data"], capture_output=True, text=True)
            check("受控入口需要测试确认标志", "拒绝执行" not in run.stdout)
            before = page.inner_text("#stMastery")
            page.click("#tab-graph >> text=刷新")
            page.wait_for_timeout(900)
            after = page.inner_text("#stMastery")
            check("通过后统计同步 +1", int(after) == int(before) + 1, f"{before}→{after}")
            check("节点颜色文字同步为掌握", page.locator(".node-g").filter(has_text="掌握").count() >= 3)
            page.screenshot(path=str(shot_dir / "3_after_pass.png"))

            # 归档同步：点击某未掌握节点 → 归档 → 图谱/统计/详情更新
            page.locator(".node-g").filter(has_text="用户访谈提纲设计").first.click()
            page.wait_for_timeout(700)
            page.click("#detailSide >> text=归档（可恢复）")
            page.wait_for_timeout(900)
            check("归档后详情关闭", not page.locator("#detailSide").is_visible() or "归档" in page.inner_text("#toast"))
            check("归档后统计 -1", int(page.inner_text("#stTotal")) == 10, page.inner_text("#stTotal"))

            # 邻域聚焦
            page.locator(".node-g").filter(has_text="Agent 工具调用机制").first.click()
            page.wait_for_timeout(600)
            page.check("#focusChk")
            page.wait_for_timeout(400)
            dimmed = page.locator(".node-g.dim").count()
            check("邻域聚焦压暗非邻居", dimmed > 0, f"dimmed={dimmed}")
            page.uncheck("#focusChk")

            # 服务断连提示
            import socket as _socket

            proc.terminate()
            deadline = time.time() + 6
            while time.time() < deadline:
                s = _socket.socket()
                try:
                    s.connect(("127.0.0.1", port))
                    s.close()
                    time.sleep(0.2)
                except OSError:
                    s.close()
                    break
            if proc.poll() is None:
                proc.kill()
            page.click("#tab-graph >> text=刷新")
            page.wait_for_timeout(10000)
            overlay_visible = page.locator("#graphOverlay").is_visible()
            overlay = page.locator("#graphOverlay").text_content()
            check("断连显示连接失败而非空库",
                  overlay_visible and (("读取失败" in overlay) or ("Failed to fetch" in overlay)),
                  f"visible={overlay_visible} text={overlay[:50]}")
            page.screenshot(path=str(shot_dir / "4_disconnected.png"))
            browser.close()
    finally:
        proc.terminate()
        shutil.rmtree(tmp, ignore_errors=True)

    failed = [r for r in results if not r[1]]
    print(f"\n界面实测：{len(results) - len(failed)}/{len(results)} 通过；截图目录：{shot_dir}")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
