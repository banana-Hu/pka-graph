"""便捷启动入口与开机自启管理。

启动：      python server_start.py [--vault DIR] [--port 8765]
注册自启：  python server_start.py --install     # HKCU Run 键（pythonw 无窗口，随登录启动）
查看状态：  python server_start.py --status      # 自启键 + 本机 8765 端口探测
取消自启：  python server_start.py --uninstall

说明：
- 自启使用当前解释器同目录的 pythonw.exe（本机为 Python 3.11 全路径），不依赖 PATH。
- pythonw 无控制台：stdout/stderr 重定向到工程根目录 server.log（滚动保留最近 2000 行）。
- 端口被占用时服务优雅退出（不崩溃），日志中给出原因。
"""

import argparse
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

LOG_FILE = ROOT / "server.log"
MAX_LOG_LINES = 2000
RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
RUN_VALUE = "PkaGraphServer"


def _ensure_streams() -> None:
    """pythonw 无控制台时 sys.stdout/stderr 为 None：重定向到 server.log，避免 print 崩溃。

    行缓冲（buffering=1）：日志逐行落盘，进程被强杀也不丢已有输出。
    """
    if sys.stdout is not None and sys.stderr is not None:
        return
    handle = None
    try:
        if LOG_FILE.exists():
            lines = LOG_FILE.read_text(encoding="utf-8", errors="replace").splitlines()
            if len(lines) > MAX_LOG_LINES:
                LOG_FILE.write_text("\n".join(lines[-MAX_LOG_LINES:]) + "\n", encoding="utf-8")
        handle = open(LOG_FILE, "a", encoding="utf-8", buffering=1)
    except OSError:
        handle = open(os.devnull, "w", encoding="utf-8", buffering=1)
    if sys.stdout is None:
        sys.stdout = handle
    if sys.stderr is None:
        sys.stderr = handle


def autostart_command() -> str:
    """自启命令行：pythonw 全路径 + 本脚本全路径（均加引号，兼容中文路径）。"""
    pythonw = Path(sys.executable).with_name("pythonw.exe")
    exe = str(pythonw if pythonw.exists() else sys.executable)
    return f'"{exe}" "{Path(__file__).resolve()}"'


def install_autostart() -> int:
    import winreg

    value = autostart_command()
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as key:
        winreg.SetValueEx(key, RUN_VALUE, 0, winreg.REG_SZ, value)
    print(f"已注册开机自启（{RUN_VALUE}）：{value}")
    print("下次登录后自动启动（pythonw 无窗口）；本次可用 --status 查看，或立即手动启动一次。")
    return 0


def uninstall_autostart() -> int:
    import winreg

    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as key:
            winreg.DeleteValue(key, RUN_VALUE)
        print(f"已取消开机自启（{RUN_VALUE}）。")
    except FileNotFoundError:
        print(f"自启键不存在（{RUN_VALUE}），无需取消。")
    return 0


def autostart_status() -> int:
    import urllib.request
    import winreg

    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_READ) as key:
            value, _ = winreg.QueryValueEx(key, RUN_VALUE)
        print(f"自启：已注册\n命令：{value}")
    except FileNotFoundError:
        print("自启：未注册（运行 --install 注册）")
    try:
        with urllib.request.urlopen("http://127.0.0.1:8765/api/meta", timeout=3) as resp:
            print(f"服务：运行中（HTTP {resp.status}，8765 端口响应正常）")
    except Exception as exc:  # noqa: BLE001 —— 探测失败只报告状态
        print(f"服务：未响应（{type(exc).__name__}）；启动方式：python server_start.py")
    if LOG_FILE.exists():
        print(f"日志：{LOG_FILE}（pythonw 启动时写入）")
    return 0


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--install", action="store_true", help="注册 HKCU Run 键开机自启")
    parser.add_argument("--uninstall", action="store_true", help="取消开机自启")
    parser.add_argument("--status", action="store_true", help="查看自启与服务状态")
    args, _rest = parser.parse_known_args()

    if args.install:
        raise SystemExit(install_autostart())
    if args.uninstall:
        raise SystemExit(uninstall_autostart())
    if args.status:
        raise SystemExit(autostart_status())

    _ensure_streams()

    # 实例本地配置（gitignored）：{"vault": "...", "allow_real_vault": true, "port": 8766}
    # 命令行参数优先于本文件。真实库路径同时注入 PKA_REAL_VAULT（安全门所需）。
    instance_cfg_path = ROOT / "instance.local.json"
    if instance_cfg_path.exists():
        try:
            instance = json.loads(instance_cfg_path.read_text(encoding="utf-8-sig"))
            if instance.get("vault") and "--vault" not in sys.argv:
                sys.argv += ["--vault", str(instance["vault"])]
            if instance.get("allow_real_vault"):
                sys.argv += ["--allow-real-vault"]
                os.environ.setdefault("PKA_REAL_VAULT", str(instance["vault"]))
            if instance.get("port") and "--port" not in sys.argv:
                sys.argv += ["--port", str(instance["port"])]
        except (json.JSONDecodeError, OSError) as exc:
            print(f"[知能图谱] instance.local.json 无法解析（忽略）：{type(exc).__name__}")

    from pka_graph.local_config import load_local_ai_config

    try:
        load_local_ai_config(ROOT / "ai.local.json")
    except RuntimeError as exc:
        raise SystemExit(str(exc)) from None

    from pka_graph.server import main as server_main

    raise SystemExit(server_main())


if __name__ == "__main__":
    main()
