# -*- coding: utf-8 -*-
"""服务端 PyInstaller 入口。

- 双击（无参数）：启动服务（内置测试库）→ 打开浏览器界面。
- `--server-worker --vault DIR --port N [--allow-real-vault]`：长期运行监督器拉起的服务进程。
- `--continuous ...`：长期运行监督器入口（same exe，进程监督 + 有限自动重启）。
"""
from __future__ import annotations

import sys
import threading
from pathlib import Path


def base_dir() -> Path:
    if hasattr(sys, "_MEIPASS"):
        return Path(sys._MEIPASS)
    return Path(__file__).resolve().parent


def _entry_url(port: int, vault: Path | None) -> str:
    """优先使用服务写出的含一次性票据入口链接（task 模式）；否则普通页面地址。"""
    if vault is not None:
        entry = Path(vault) / "个人学习状态" / "runtime" / "browser_entry.url"
        try:
            url = entry.read_text(encoding="utf-8").strip()
            if url.startswith("http://127.0.0.1:"):
                return url
        except OSError:
            pass
    return f"http://127.0.0.1:{port}"


def main() -> int:
    base = base_dir()
    sys.path.insert(0, str(base))
    argv = sys.argv[1:]

    if "--continuous" in argv:
        from continuous_start import main as continuous_main
        rest = [item for item in argv if item != "--continuous"]
        return continuous_main([item for item in rest if item != "--server-worker"]) or 0

    if "--server-worker" in argv:
        from pka_graph.server import main as server_main
        sys.argv = [sys.argv[0]] + [item for item in argv if item != "--server-worker"]
        return server_main() or 0

    vault = base / "test_vault"
    if not vault.exists():
        print("[知能图谱] 未找到内置测试库 test_vault；请在解压目录中运行。")
        return 1

    def open_browser():
        try:
            import webbrowser
            webbrowser.open(_entry_url(8765, vault))
        except Exception:  # noqa: BLE001 —— 打不开浏览器不影响服务
            pass

    threading.Timer(1.5, open_browser).start()

    from pka_graph.server import serve

    print("[知能图谱] 服务启动中：http://127.0.0.1:8765（内置测试知识库）")
    print("[知能图谱] 关闭本窗口即停止服务；数据保存在 test_vault/个人学习状态/。")
    print("[知能图谱] 高级：真实知识库用法见发布页说明（--vault 参数）。")
    return serve(vault, port=8765, allow_real_vault=False) or 0


if __name__ == "__main__":
    raise SystemExit(main())
