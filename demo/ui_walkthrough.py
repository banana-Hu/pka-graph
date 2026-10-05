"""界面实测：无头浏览器真实点击走完核心操作（对 test_vault 的临时副本执行）。"""

import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

WORKSPACE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(WORKSPACE))

results = []


def check(name, ok, detail=""):
    results.append((name, ok, detail))
    print(f"{'PASS' if ok else 'FAIL'}  {name}  {detail}")


def main():
    tmp = Path(tempfile.mkdtemp(prefix="pka_ui_"))
    shutil.copytree(WORKSPACE / "test_vault", tmp / "vault", dirs_exist_ok=True)
    port = 8801
    proc = subprocess.Popen(
        [sys.executable, str(WORKSPACE / "server_start.py"), "--vault", str(tmp / "vault"), "--port", str(port)],
        cwd=str(WORKSPACE), stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
    )
    time.sleep(2.5)
    try:
        from playwright.sync_api import sync_playwright

        with sync_playwright() as p:
            browser = p.chromium.launch()
            page = browser.new_page(viewport={"width": 1280, "height": 1000})
            page.goto(f"http://127.0.0.1:{port}/")
            page.wait_for_timeout(1500)

            check("横幅警示可见", "测试知识库，未连接正式写入" in page.inner_text("#banner"))
            page.wait_for_timeout(1500)
            rows = page.locator("#scanTable tbody tr").count()
            check("扫描表渲染 5 行目标笔记", rows == 5, f"rows={rows}")
            dup_text = page.locator("#scanStats").inner_text()
            check("扫描统计含重复 ID 提示", "重复 ID 1" in dup_text, dup_text[:80])

            # 勾选正式笔记 + 无ID笔记 → 预览
            page.locator(".pick").nth(0).check()
            page.locator(".pick").nth(1).check()
            page.click('#tab-scan >> text=生成预览（不写入）')
            page.wait_for_timeout(800)
            preview_text = page.inner_text("#previewBox")
            check("预览包含登记动作（此时尚无节点可关联）", '"type": "register"' in preview_text)
            page.click("#applyBtn")
            page.wait_for_timeout(1000)
            check("接入写入成功提示", "已写入测试库管理区" in page.inner_text("#toast"))

            # 新建节点
            page.click("nav button[data-tab='nodes']")
            page.click("button:has-text('+ 新建节点')")
            page.fill("#cName", "界面实测节点")
            page.fill("#cDomain", "AI技术领域")
            page.fill("#cScope", "界面实测的初始范围")
            page.click('#createBox button.primary')
            page.wait_for_timeout(1000)
            check("创建节点提示 untested", "已创建" in page.inner_text("#toast"))
            page.wait_for_timeout(800)
            body = page.inner_text("#nodeTable")
            check("节点列表出现新节点", "界面实测节点" in body)

            # 打开详情 → 改范围 → 确认流程
            page.click("text=界面实测节点")
            page.wait_for_timeout(800)
            page.fill("#eScope", "界面实测的修改后范围")
            page.click('#detailPanel >> text=保存修改')
            page.wait_for_timeout(600)
            tip_visible = page.locator("#scopeTip").is_visible()
            check("改范围弹出确认提示", tip_visible)
            if tip_visible:
                page.click('#scopeTip >> text=确认修改并回到 untested')
                page.wait_for_timeout(1000)
                check("确认后提示回 untested", "回到 untested" in page.inner_text("#toast"))

            # 合并预览
            page.click("nav button[data-tab='ops']")
            page.wait_for_timeout(500)
            page.select_option("#mSrc", label="Harness 工程方法")
            page.select_option("#mDst", label="界面实测节点")
            page.click('#tab-ops >> text=预览受影响的关联')
            page.wait_for_timeout(800)
            mp = page.inner_text("#mergePreviewBox")
            check("合并预览展示迁移清单", "note_links_to_migrate" in mp)
            page.click("#mergeApplyBtn")
            page.wait_for_timeout(1000)
            check("合并完成提示", "合并完成" in page.inner_text("#toast"))

            # 归档与恢复
            page.click("nav button[data-tab='archive']")
            page.wait_for_timeout(500)
            page.click('#archiveTable button')
            page.wait_for_timeout(1000)
            check("恢复提示回 untested", "已恢复为 untested" in page.inner_text("#toast"))

            # 停止服务
            page.once("dialog", lambda d: d.accept())
            page.click('header >> text=停止服务')
            page.wait_for_timeout(800)
            check("停止服务页面提示", "服务已停止" in page.inner_text("body"))
            browser.close()
    finally:
        proc.terminate()
        shutil.rmtree(tmp, ignore_errors=True)

    failed = [r for r in results if not r[1]]
    print(f"\n界面实测：{len(results) - len(failed)}/{len(results)} 通过")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
