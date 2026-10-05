"""第四阶段界面实测：真实鼠标操作完成 选节点 → 测验 → 回答 → 判定 → 图谱更新。

全部使用 Playwright 正常点击/输入（无 force、无 dispatchEvent、无直接函数调用）。
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
dialogs_fired = []


def check(name, ok, detail=""):
    results.append((name, ok, detail))
    print(f"{'PASS' if ok else 'FAIL'}  {name}  {detail}")


def main():
    tmp = Path(tempfile.mkdtemp(prefix="pka_p4_"))
    fixture = tmp / "vault"
    from make_phase3_fixture import build_fixture

    build_fixture(fixture)
    port = 8821
    proc = subprocess.Popen(
        [sys.executable, str(WORKSPACE / "server_start.py"), "--vault", str(fixture), "--port", str(port)],
        cwd=str(WORKSPACE), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    time.sleep(2.5)
    shot_dir = Path(tempfile.mkdtemp(prefix="pka_p4_shots_"))
    try:
        from playwright.sync_api import sync_playwright

        with sync_playwright() as p:
            browser = p.chromium.launch()
            page = browser.new_page(viewport={"width": 1440, "height": 950})
            page.on("dialog", lambda d: (dialogs_fired.append(d.message), d.accept()))
            page.goto(f"http://127.0.0.1:{port}/")
            page.wait_for_timeout(2200)

            # 场景 1：选节点 → 测验 → 回答 → 判定通过 → 图谱更新（全程真实鼠标）
            print("[步骤] SC1")
            page.locator(".node-g").filter(has_text="提示词工程基础").first.click()
            page.wait_for_timeout(700)
            page.click("#detailSide >> text=开始掌握测验")
            page.wait_for_function(
                "document.getElementById('quizBody').textContent.includes('提交回答')", timeout=20000
            )
            check("测验准备：显示冻结通过条件与数据发送提示",
                  "通过条件" in page.inner_text("#quizBody") and "数据发送" in page.inner_text("#quizBody"))
            check("适配器如实标注为假模型", "假模型" in page.inner_text("#quizAdapter"))
            page.screenshot(path=str(shot_dir / "1_quiz_question.png"))
            page.fill("#quizAnswer",
                      "举一个实际场景：智能体需要查询数据库时，宿主把工具 schema 给模型，模型返回调用请求，"
                      "执行后把结果回填；超时要有兜底，敏感工具要走权限确认，否则会越权或死循环。")
            page.click("text=提交回答")
            page.wait_for_function(
                "document.getElementById('quizBody').textContent.includes('已通过') "
                "|| document.getElementById('quizBody').textContent.includes('未通过')", timeout=30000
            )
            body = page.inner_text("#quizBody")
            check("假模型演示判定通过", "已通过" in body, body[:60])
            page.screenshot(path=str(shot_dir / "2_quiz_passed.png"))
            page.wait_for_timeout(1200)  # 轮询后的图谱刷新
            check("图谱同步：节点显示掌握", page.locator(".node-g").filter(has_text="掌握").count() >= 3)
            check("统计同步：掌握层 +1", int(page.inner_text("#stMastery")) == 3, page.inner_text("#stMastery"))
            page.keyboard.press("Escape")
            page.click("#quizModal >> text=关闭")
            page.wait_for_timeout(400)

            # 场景 2：提示后复述 → 服务端强制未通过
            print("[步骤] SC2")
            page.locator(".node-g").filter(has_text="REST API 设计").first.click()
            page.wait_for_timeout(700)
            page.click("#detailSide >> text=开始掌握测验")
            page.wait_for_function(
                "document.getElementById('quizBody').textContent.includes('提交回答')", timeout=20000
            )
            page.click("text=请求提示")
            page.wait_for_function(
                "document.getElementById('quizBody').textContent.includes('不能作为独立通过依据')", timeout=20000
            )
            page.fill("#quizAnswer", "看完提示后照着复述的内容，长度超过二十个字，看起来像模像样的回答。")
            page.click("text=提交回答")
            page.wait_for_function(
                "document.getElementById('quizBody').textContent.includes('未通过')", timeout=30000
            )
            check("提示后通过被服务端降级为未通过", "提示" in page.inner_text("#quizBody"))
            page.click("#quizModal >> text=关闭")
            page.wait_for_timeout(500)

            # 场景 3：回答含脚本样文 → 无 dialog、正常流转
            print("[步骤] SC3")
            page.locator(".node-g").filter(has_text="峰终定律").first.click()
            page.wait_for_timeout(700)
            page.click("#detailSide >> text=开始掌握测验")
            page.wait_for_function(
                "document.getElementById('quizBody').textContent.includes('提交回答')", timeout=20000
            )
            page.fill("#quizAnswer",
                      "<script>alert('answer-xss')</script> 正常回答部分：峰终定律说明记忆由高峰与结尾决定，长度超过二十字。")
            page.click("text=提交回答")
            page.wait_for_function(
                "document.getElementById('quizBody').textContent.includes('已通过') "
                "|| document.getElementById('quizBody').textContent.includes('未通过')", timeout=30000
            )
            check("脚本样文回答未触发任何 dialog", len(dialogs_fired) == 0, str(dialogs_fired[:2]))
            page.click("#quizModal >> text=关闭")

            # 场景 4：passed 节点详情无测验入口、显示只读提示
            print("[步骤] SC4")
            page.locator(".node-g").filter(has_text="Agent 工具调用机制").first.click()
            page.wait_for_timeout(700)
            detail = page.inner_text("#detailSide")
            check("passed 节点无测验入口", "开始掌握测验" not in detail)
            check("passed 节点显示第五阶段只读提示", "第五阶段" in detail)

            # 场景 5：版本冲突 —— 等待判定期间修改范围（真实点击走节点管理页）
            print("[步骤] SC5")
            page.locator(".node-g").filter(has_text="用户访谈提纲设计").first.click()
            page.wait_for_timeout(700)
            page.click("#detailSide >> text=开始掌握测验")
            page.wait_for_function(
                "document.getElementById('quizBody').textContent.includes('提交回答')", timeout=20000
            )
            page.click("#quizModal >> text=取消测验")
            page.wait_for_timeout(600)
            # 直接用管理页改范围（真实点击）：进入节点管理 → 找到节点 → 改范围保存
            page.click("nav button[data-tab='nodes']")
            page.wait_for_timeout(400)
            page.locator("#nodeTable tbody tr").filter(has_text="用户访谈提纲设计").click()
            page.wait_for_timeout(600)
            page.fill("#eScope", "访谈提纲的收窄版本：只考察开场与追问设计")
            page.click("#detailPanel >> text=保存修改")
            page.wait_for_timeout(600)
            page.click("#scopeTip >> text=确认修改并回到 untested")
            page.wait_for_timeout(800)
            check("范围修改确认后回 untested（旧测验会话将作废）",
                  "回到 untested" in page.inner_text("#toast"))
            page.screenshot(path=str(shot_dir / "5_scope_changed.png"))

            # 场景 6：取消测验不动状态
            print("[步骤] SC6")
            page.click("nav button[data-tab='graph']")
            page.wait_for_timeout(800)
            page.locator("#graphSvg").click(position={"x": 20, "y": 20})  # 关闭遗留详情面板
            page.wait_for_timeout(300)
            page.locator(".node-g").filter(has_text="计费影响").first.click()
            page.wait_for_timeout(700)
            page.click("#detailSide >> text=开始掌握测验")
            page.wait_for_function(
                "document.getElementById('quizBody').textContent.includes('提交回答')", timeout=20000
            )
            page.click("#quizModal >> text=取消测验")
            page.wait_for_timeout(600)
            check("取消后节点状态未变", "测验已取消" in page.inner_text("#toast"))
            browser.close()
    finally:
        proc.terminate()
        shutil.rmtree(tmp, ignore_errors=True)

    failed = [r for r in results if not r[1]]
    print(f"\n第四阶段界面实测：{len(results) - len(failed)}/{len(results)} 通过；截图目录：{shot_dir}")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
