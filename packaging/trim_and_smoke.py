# -*- coding: utf-8 -*-
"""构建后修剪 + 冒烟验证（正式打包流程的一部分）。"""
import shutil
import subprocess
import time
import urllib.request
from pathlib import Path

internal = Path(__file__).resolve().parents[1] / "dist" / \
    "知能图谱-v1.0.0-windows-x64" / "知能图谱服务" / "知能图谱服务" / "_internal"

HEAVY = ["torch", "torchvision", "torchaudio", "tensorflow", "scipy", "pandas",
         "matplotlib", "sklearn", "onnxruntime", "transformers", "IPython",
         "cv2", "twisted", "alembic", "fastapi", "pydantic", "sqlalchemy",
         "huggingface_hub", "tokenizers", "safetensors", "sympy", "networkx",
         "av.libs", "scipy.libs", "PIL", "grpc", "hf_xet", "lxml",
         "cryptography", "pydantic_core", "pydantic", "curl_cffi.libs",
         "imageio_ffmpeg", "yaml", "fsspec", "filelock", "tqdm"]


def trim() -> int:
    removed = 0
    for name in HEAVY:
        d = internal / name
        if d.exists():
            shutil.rmtree(d, ignore_errors=True)
            removed += 1
    return removed


def smoke() -> bool:
    exe = internal.parent / "知能图谱服务.exe"
    proc = subprocess.Popen([str(exe)], cwd=str(exe.parent))
    try:
        for _ in range(30):
            time.sleep(0.5)
            try:
                with urllib.request.urlopen("http://127.0.0.1:8765/api/meta", timeout=2) as r:
                    return r.status == 200 and "token" in r.read().decode()
            except Exception:
                continue
        return False
    finally:
        proc.terminate()


if __name__ == "__main__":
    n = trim()
    print(f"修剪 {n} 个重型目录")
    ok = smoke()
    total = sum(f.stat().st_size for f in internal.parent.rglob('*') if f.is_file())
    print(f"冒烟: {'通过' if ok else '失败'} | 服务端目录 {total // 1024 // 1024} MB")
    raise SystemExit(0 if ok else 1)
