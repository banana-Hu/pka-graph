from pathlib import Path
# -*- coding: utf-8 -*-
"""第七阶段：设置 MiniMax 环境并运行真实模型试测。"""
import os
import sys

WORKSPACE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(WORKSPACE))
sys.path.insert(0, str(WORKSPACE / "tests"))

# 从信息获取项目读取 MiniMax 配置
INFO_SCRIPTS = Path(os.environ.get("PKA_INFO_SCRIPTS", ""))
sys.path.insert(0, str(INFO_SCRIPTS))
from search_discovery import load_local_env, ENV_KEY, ENV_BASE_URL, ENV_MODEL

load_local_env()
key = os.environ.get(ENV_KEY, "")
base = (os.environ.get(ENV_BASE_URL, "").strip() or "https://minnimax.chat").rstrip("/")
model = os.environ.get(ENV_MODEL, "").strip() or "MiniMax-M3"

# 设置发现/测验适配器环境变量（密钥只在环境变量中传递，不写入文件）
os.environ["PKA_QUIZ_ADAPTER"] = "http"
os.environ["PKA_QUIZ_API_BASE"] = base + "/v1"
os.environ["PKA_QUIZ_API_KEY"] = key
os.environ["PKA_QUIZ_MODEL"] = model
os.environ["PKA_DISCOVERY_ADAPTER"] = "http"
os.environ["PKA_DISCOVERY_API_BASE"] = base + "/v1"
os.environ["PKA_DISCOVERY_API_KEY"] = key
os.environ["PKA_DISCOVERY_MODEL"] = model

# 清除代理（中转站需直连）
for v in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy"):
    os.environ.pop(v, None)

cmd = sys.argv[1] if len(sys.argv) > 1 else "all"

if cmd in ("quiz", "all"):
    print("=== 测验真实模型试测 ===")
    print(f"适配器：{mode if (mode := os.environ.get('PKA_QUIZ_MODEL')) else '?'}")
    from pka_graph.quiz_model import build_adapter_from_env
    a, b, m, avail, r = build_adapter_from_env()
    print(f"可用：{avail} | {a.describe() if a else r}")
    if avail:
        from demo.eval_phase7 import run_quiz_eval
        run_quiz_eval()

if cmd in ("discovery", "all"):
    print("\n=== 发现真实模型试测 ===")
    from demo.eval_phase7 import run_discovery_eval
    run_discovery_eval()
