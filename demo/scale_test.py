"""第三/四阶段规模测试：约 30 / 300 / 1000 个合成节点。

区分两类数字：
- 脚本执行时间：合成事件驱动的处理耗时（refreshGraph / 布局计算）。
- 实际交互：真实鼠标按下-拖动-滚轮-点击的浏览器端到端耗时。
"""

import shutil
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path

WORKSPACE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(WORKSPACE))

from pka_graph.models import empty_state, make_node_link, new_node  # noqa: E402
from pka_graph.store import StateStore  # noqa: E402

DOMAINS = ["AI技术领域", "程序开发领域", "产品领域", "心理学领域"]
STATUSES = ["untested", "failed", "passed"]


def build_scale_vault(n: int, out: Path) -> None:
    (out / "个人学习状态").mkdir(parents=True)
    state = empty_state()
    for i in range(n):
        node = new_node(
            name=f"规模测试节点{i:05d}",
            domain=DOMAINS[i % len(DOMAINS)],
            scope=f"合成节点 {i} 的边界描述，用于加载与交互规模测试。",
        )
        node["status"] = STATUSES[i % 3]
        state["nodes"].append(node)
    ids = [nd["id"] for nd in state["nodes"]]
    for i in range(1, n):
        state["node_links"].append(make_node_link(ids[i - 1], ids[i], "prerequisite"))
    for i in range(0, n - 2, 2):
        state["node_links"].append(make_node_link(ids[i], ids[i + 2], "related"))
    StateStore(out).save(state, base_version=None)


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def run_one(n: int, browser) -> dict:
    tmp = Path(tempfile.mkdtemp(prefix=f"pka_scale{n}_"))
    build_scale_vault(n, tmp / "vault")
    port = free_port()
    proc = subprocess.Popen(
        [sys.executable, str(WORKSPACE / "server_start.py"), "--vault", str(tmp / "vault"), "--port", str(port)],
        cwd=str(WORKSPACE), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    time.sleep(2.5)
    try:
        page = browser.new_page(viewport={"width": 1440, "height": 950})
        t0 = time.perf_counter()
        page.goto(f"http://127.0.0.1:{port}/")
        page.wait_for_function(f"document.querySelectorAll('.node-g').length >= {n}", timeout=60000)
        first_load_s = round(time.perf_counter() - t0, 2)

        # 脚本执行时间（合成事件）：处理耗时对照
        script_ms = page.evaluate(
            """(n) => (async () => {
        const out = {};
        let t = performance.now();
        await refreshGraph();
        out.reload_ms = performance.now() - t;
        t = performance.now();
        const svg = document.getElementById('graphSvg');
        for (let k = 0; k < 30; k++) {
          svg.dispatchEvent(new MouseEvent('mousedown', {clientX: 700, clientY: 400}));
          window.dispatchEvent(new MouseEvent('mousemove', {clientX: 700 + k * 6, clientY: 400 + k * 2}));
          window.dispatchEvent(new MouseEvent('mouseup', {}));
        }
        out.pan30_ms = performance.now() - t;
        resetView();
        return out;
      })()"""
        )

        # 实际交互：真实鼠标拖动（按下→移动→抬起）
        t0 = time.perf_counter()
        page.mouse.move(700, 400)
        page.mouse.down()
        for k in range(12):
            page.mouse.move(700 + (k + 1) * 8, 400 + k * 3)
        page.mouse.up()
        real_pan_ms = round((time.perf_counter() - t0) * 1000)
        # 真实滚轮缩放
        t0 = time.perf_counter()
        for _ in range(5):
            page.mouse.wheel(0, -120)
            page.wait_for_timeout(30)
        real_zoom_ms = round((time.perf_counter() - t0) * 1000)

        # 真实点击选中：搜索定位到唯一节点（集合变化自动适应视图后可点）
        t0 = time.perf_counter()
        page.fill("#gq", "规模测试节点00005")
        page.wait_for_function('document.getElementById("stFiltered").textContent === "1"', timeout=30000)
        search_ms = round((time.perf_counter() - t0) * 1000)
        t0 = time.perf_counter()
        page.locator(".node-g").first.click()  # 真实鼠标点击（无 force / 无事件派发）
        page.wait_for_selector("#detailSide:not(.hidden)", timeout=30000)
        select_ms = round((time.perf_counter() - t0) * 1000)

        page.close()
        return {
            "nodes": n,
            "edges": n - 1 + (n - 2) // 2 + 1,
            "first_load_s": first_load_s,
            "reload_ms": round(script_ms["reload_ms"]),
            "pan30_script_ms": round(script_ms["pan30_ms"]),
            "real_pan12_ms": real_pan_ms,
            "real_zoom5_ms": real_zoom_ms,
            "search_ms": search_ms,
            "real_click_select_ms": select_ms,
        }
    finally:
        proc.terminate()
        shutil.rmtree(tmp, ignore_errors=True)


def main():
    sizes = [int(a) for a in sys.argv[1:]] or [30, 300, 1000]
    from playwright.sync_api import sync_playwright

    rows = []
    with sync_playwright() as p:
        browser = p.chromium.launch()
        for n in sizes:
            rows.append(run_one(n, browser))
            print(f"已完成 {n} 节点")
        browser.close()
    print("\n| 节点 | 边 | 首次加载(s) | 脚本刷新(ms) | 拖动30次脚本(ms) | 真实拖动12步(ms) | 真实缩放5次(ms) | 搜索(ms) | 真实点击选中(ms) |")
    print("| --- | --- | --- | --- | --- | --- | --- | --- | --- |")
    for r in rows:
        print(f"| {r['nodes']} | {r['edges']} | {r['first_load_s']} | {r['reload_ms']} | {r['pan30_script_ms']} | "
              f"{r['real_pan12_ms']} | {r['real_zoom5_ms']} | {r['search_ms']} | {r['real_click_select_ms']} |")


if __name__ == "__main__":
    main()
