"""知识库长期运行入口。注册自启只在明确使用 --install 时执行。"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path

from pka_graph.runtime import RuntimeSupervisor, read_runtime_status, request_runtime_stop

ROOT = Path(__file__).resolve().parent
RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"


def _arguments(vault: Path, port: int, allow_real_vault: bool) -> list[str]:
    arguments = ["--vault", str(vault.resolve()), "--port", str(port)]
    if allow_real_vault:
        arguments.append("--allow-real-vault")
    return arguments


def service_command(vault: Path, port: int, allow_real_vault: bool) -> list[str]:
    arguments = _arguments(vault, port, allow_real_vault)
    if getattr(sys, "frozen", False):
        return [sys.executable, "--server-worker", *arguments]
    return [sys.executable, str(ROOT / "server_start.py"), *arguments]


def autostart_command(vault: Path, port: int, allow_real_vault: bool) -> str:
    arguments = _arguments(vault, port, allow_real_vault)
    if getattr(sys, "frozen", False):
        command = [sys.executable, "--continuous", *arguments]
    else:
        pythonw = Path(sys.executable).with_name("pythonw.exe")
        executable = str(pythonw if pythonw.exists() else Path(sys.executable))
        command = [executable, str(Path(__file__).resolve()), *arguments]
    return subprocess.list2cmdline(command)


def _registration_name(vault: Path) -> str:
    fingerprint = hashlib.sha256(os.path.normcase(str(vault.resolve())).encode("utf-8")).hexdigest()[:12]
    return "PkaGraphContinuous-" + fingerprint


def install_autostart(vault: Path, port: int, allow_real_vault: bool) -> dict:
    if os.name != "nt":
        raise RuntimeError("开机注册目前仅支持 Windows")
    import winreg
    command = autostart_command(vault, port, allow_real_vault)
    runtime_dir = vault.resolve() / "个人学习状态" / "runtime"
    runtime_dir.mkdir(parents=True, exist_ok=True)
    config = {"vault": str(vault.resolve()), "port": port, "allow_real_vault": allow_real_vault,
              "command": command, "trigger": "current_user_logon"}
    with winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as key:
        winreg.SetValueEx(key, _registration_name(vault), 0, winreg.REG_SZ, command)
    # 准确配置保存到使用者选定知识库的管理区，不退回测试库。
    from pka_graph.runtime import _write_json
    _write_json(runtime_dir / "autostart.json", config)
    return {"installed": True, "config": config, "note": "下次当前用户登录后启动；本次没有启动服务"}


def uninstall_autostart(vault: Path) -> dict:
    if os.name != "nt":
        raise RuntimeError("开机注册目前仅支持 Windows")
    import winreg
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as key:
            winreg.DeleteValue(key, _registration_name(vault))
    except FileNotFoundError:
        return {"installed": False, "removed": False}
    return {"installed": False, "removed": True, "note": "只撤销本知识库自启；正在运行的服务未被停止"}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--vault", type=Path, required=True, help="明确指定知识库，不使用测试库默认值")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--allow-real-vault", action="store_true")
    actions = parser.add_mutually_exclusive_group()
    for action in ("status", "stop", "install", "uninstall"):
        actions.add_argument("--" + action, action="store_true")
    args = parser.parse_args(argv)
    if not args.vault.is_dir() or not 1 <= args.port <= 65535:
        parser.error("知识库目录不存在或端口无效")
    try:
        if args.status:
            result = read_runtime_status(args.vault)
        elif args.stop:
            result = request_runtime_stop(args.vault)
        elif args.install:
            result = install_autostart(args.vault, args.port, args.allow_real_vault)
        elif args.uninstall:
            result = uninstall_autostart(args.vault)
        else:
            supervisor = RuntimeSupervisor(args.vault, service_command(args.vault, args.port, args.allow_real_vault),
                                           port=args.port)
            result = supervisor.run()
        if sys.stdout is not None:
            print(json.dumps(result, ensure_ascii=False, indent=2))
        return 1 if result.get("status") in {"blocked", "unreadable", "stop_incomplete"} else 0
    except (OSError, RuntimeError, TimeoutError, ValueError) as exc:
        if sys.stderr is not None:
            print(f"长期运行入口未能完成：{type(exc).__name__}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
