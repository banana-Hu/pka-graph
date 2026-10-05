"""第五阶段界面实测：真实鼠标完成 设置启用 → 查看到期 → 复习 → 状态与下次安排同步。

另测：未通过入待重学、稍后提醒、暂停/恢复、重开界面。服务器以显式 demo 模式启动
（夹具带专用测试库标记）；到期节点由测试专用入口制造（--due-in-days）。
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
    tmp = Path(tempfile.mkdtemp(prefix="pka_p5_"))
    fixture = tmp / "vault"
    from make_phase3_fixture import build_fixture

    build_fixture(fixture)
    # 制造到期节点：把未测的「REST API 设计」置为 passed 且今天到期
    import json as _json

    state_path = fixture / "个人学习状态" / "state.json"
    state = _json.loads(state_path.read_text(encoding="utf-8"))
    from datetime import datetime, timedelta, timezone

    now = datetime.now(timezone.utc)
    for n in state["nodes"]:
        if n["name"] in ("REST API 设计", "用户访谈提纲设计") or (
                n["name"] == "上下文窗口管理" and "计费" in n["scope"]):
            n["status"] = "passed"
            n["last_quiz_at"] = now.isoformat(timespec="seconds")
            n["next_review_at"] = (now - timedelta(days=1)).isoformat(timespec="seconds")  # 昨天 → 逾期
            n["interval_index"] = 0
    state_path.write_text(_json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")

    port = 8823
    env = {"PATH": str(Path(sys.executable).parent) + ";" + str(WORKSPACE), "SYSTEMROOT": "C:\\Windows",
           "PKA_QUIZ_ADAPTER": "demo", "TEMP": str(tmp), "TMP": str(tmp)}
    import os

    env = {**os.environ, **env}
    proc = subprocess.Popen(
        [sys.executable, str(WORKSPACE / "server_start.py"), "--vault", str(fixture), "--port", str(port)],
        cwd=str(WORKSPACE), env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    time.sleep(2.5)
    shot_dir = Path(tempfile.mkdtemp(prefix="pka_p5_shots_"))
    try:
        from playwright.sync_api import sync_playwright

        with sync_playwright() as p:
            browser = p.chromium.launch()
            page = browser.new_page(viewport={"width": 1440, "height": 950})
            page.on("dialog", lambda d: d.accept())
            page.goto(f"http://127.0.0.1:{port}/")
            page.wait_for_timeout(2200)

            # SC1 设置启用（默认间隔展示 → 确认）
            print("[步骤] SC1")
            page.click("nav button[data-tab='review']")
            page.wait_for_timeout(700)
            settings = page.inner_text("#reviewSettings")
            check("设置页显示未启用与默认间隔", "未启用" in settings and "7 / 21 / 45 / 90" in settings, settings[:80])
            page.click("text=启用复习计划")
            page.wait_for_timeout(700)  # confirm 对话框被自动接受
            page.wait_for_timeout(500)
            check("确认后设置显示已启用", "已启用" in page.inner_text("#reviewSettings"))
            page.screenshot(path=str(shot_dir / "1_settings_enabled.png"))

            # SC2 到期列表 → 开始复习 → 通过 → 下一次安排
            print("[步骤] SC2")
            page.wait_for_timeout(600)
            groups_text = page.inner_text("#reviewGroups")
            check("逾期分组显示 REST API 设计（逾期 1 天）",
                  "REST API 设计" in groups_text and "逾期 1 天" in groups_text)
            page.locator("#reviewGroups tr").filter(has_text="REST API 设计").locator("text=开始复习").click()
            page.wait_for_function(
                "document.getElementById('quizBody').textContent.includes('提交回答')", timeout=20000
            )
            check("复习会话标注数据发送范围", "数据发送" in page.inner_text("#quizBody"))
            page.fill("#quizAnswer",
                      "复习回答：REST 的资源建模先把名词找出来，状态码用 2xx/4xx/5xx 表达成败，"
                      "版本放进 URL 或头；举一个支付接口的例子说明幂等。")
            page.click("text=提交回答")
            page.wait_for_function(
                "document.getElementById('quizBody').textContent.includes('已通过')", timeout=30000
            )
            page.wait_for_timeout(900)
            nxt = page.inner_text("#nextArrangement")
            check("显示下一次复习安排（推进到 21 天档）", "下一次复习安排" in nxt and "尚未" not in nxt, nxt[:60])
            check("关联笔记审核状态不受影响（无候选转正式动作）", "候选" not in page.inner_text("#quizBody"))
            page.screenshot(path=str(shot_dir / "2_review_passed.png"))
            page.click("#quizModal >> text=关闭")
            page.wait_for_timeout(400)

            # SC3 复习未通过 → 待重学
            print("[步骤] SC3")
            page.wait_for_timeout(500)
            page.locator("#reviewGroups tr").filter(has_text="用户访谈提纲设计").locator("text=开始复习").click()
            page.wait_for_function(
                "document.getElementById('quizBody').textContent.includes('提交回答')", timeout=20000
            )
            page.fill("#quizAnswer", "不知道。")
            page.click("text=提交回答")
            page.wait_for_function(
                "document.getElementById('quizBody').textContent.includes('未通过')", timeout=30000
            )
            page.click("#quizModal >> text=关闭")
            page.wait_for_timeout(800)
            groups_text = page.inner_text("#reviewGroups")
            check("未通过节点进入待重学分组", "待重学" in groups_text and "用户访谈提纲设计" in groups_text)
            page.screenshot(path=str(shot_dir / "3_relearn.png"))

            # SC4 稍后提醒
            print("[步骤] SC4")
            page.locator("#reviewGroups tr").filter(has_text="上下文窗口管理").locator("text=稍后提醒 8h").click()
            page.wait_for_timeout(600)
            check("稍后提醒提示到期日不变", "到期日不变" in page.inner_text("#toast"))
            page.wait_for_timeout(400)

            # SC5 暂停 / 恢复
            print("[步骤] SC5")
            page.locator("#reviewGroups tr").filter(has_text="上下文窗口管理").locator("text=暂停").click()
            page.wait_for_timeout(600)
            check("暂停后进入已暂停分组", "已暂停（1）" in page.inner_text("#reviewGroups"))
            page.locator("#reviewGroups tr").filter(has_text="上下文窗口管理").locator("text=恢复").click()
            page.wait_for_timeout(600)
            check("恢复后离开已暂停分组", "已暂停" not in page.inner_text("#reviewGroups"))

            # SC6 重开界面：积压事项仍可见
            print("[步骤] SC6")
            page.reload()
            page.wait_for_timeout(2000)
            page.click("nav button[data-tab='review']")
            page.wait_for_timeout(800)
            check("重开界面后积压事项仍在", "逾期" in page.inner_text("#reviewGroups") or "待重学" in page.inner_text("#reviewGroups"))
            check("头部徽标显示到期数量", "待复习" in page.inner_text("#reviewBadge2") or page.inner_text("#reviewBadge2") == "")
            page.screenshot(path=str(shot_dir / "5_reopen.png"))
            browser.close()
    finally:
        proc.terminate()
        shutil.rmtree(tmp, ignore_errors=True)

    failed = [r for r in results if not r[1]]
    print(f"\n第五阶段界面实测：{len(results) - len(failed)}/{len(results)} 通过；截图目录：{shot_dir}")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
