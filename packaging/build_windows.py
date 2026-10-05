# -*- coding: utf-8 -*-
"""一键构建 Windows 分发包（PyInstaller）。

用法：python packaging/build_windows.py
产出：dist/知能图谱-v{版本}-windows-x64.zip
  ├─ 知能图谱服务/知能图谱服务.exe   服务端（内置测试库 + Web 界面，双击启动并打开浏览器）
  ├─ 知识库入口.exe                  投递客户端（tkinter 窗口）
  └─ 启动说明.md

只打包运行所需：pka_graph + web + skills + test_vault。不包含 tests/demo/docs/真实库。
"""
from __future__ import annotations

import subprocess
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DIST = ROOT / "dist"
VERSION = "1.0.0"


def run(cmd: list[str]) -> None:
    print("+", " ".join(cmd), flush=True)
    subprocess.run(cmd, check=True, cwd=ROOT)


def main() -> int:
    excludes = ["torch", "torchvision", "torchaudio", "tensorflow", "scipy",
                "pandas", "matplotlib", "PIL", "sklearn", "onnxruntime",
                "transformers", "IPython", "playwright", "fitz", "cv2"]
    out_dir = DIST / f"知能图谱-v{VERSION}-windows-x64"
    if out_dir.exists():
        import shutil
        shutil.rmtree(out_dir)
    out_dir.mkdir(parents=True)

    # 1) 服务端（onedir：含 pka_graph/web/skills/test_vault 数据文件）
    run([sys.executable, "-m", "PyInstaller", "--noconfirm", "--clean",
         "--name", "知能图谱服务",
         "--distpath", str(out_dir / "知能图谱服务"),
         "--workpath", str(ROOT / "build" / "svc"),
         "--specpath", str(ROOT / "build"),
         "--add-data", f"{ROOT / 'web'}{chr(59)}web",
         "--add-data", f"{ROOT / 'skills'}{chr(59)}skills",
         "--add-data", f"{ROOT / 'test_vault'}{chr(59)}test_vault",
         "--hidden-import", "pka_graph.notion_sync",
         *[x for e in excludes for x in ("--exclude-module", e)],
         str(ROOT / "packaging" / "serve_entry.py")])

    # 2) 客户端（onefile + windowed）
    run([sys.executable, "-m", "PyInstaller", "--noconfirm", "--clean",
         "--onefile", "--windowed",
         "--name", "知识库入口",
         "--distpath", str(out_dir),
         "--workpath", str(ROOT / "build" / "client"),
         "--specpath", str(ROOT / "build"),
         *[x for e in excludes for x in ("--exclude-module", e)],
         str(ROOT / "知识库入口.py")])

    # 4) 使用说明
    (out_dir / "启动说明.md").write_text(
        Path(ROOT / "packaging" / "启动说明.md").read_text(encoding="utf-8"),
        encoding="utf-8")

    # 5) zip
    zip_path = DIST / f"知能图谱-v{VERSION}-windows-x64.zip"
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
        for f in sorted(out_dir.rglob("*")):
            if f.is_file():
                zf.write(f, f.relative_to(out_dir.parent))
    print(f"打包完成：{zip_path}（{zip_path.stat().st_size // 1024 // 1024} MB）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
