# -*- coding: utf-8 -*-
"""新建公开仓库并推送脱敏后的干净历史 + v1.0.0 Release。运行后删除。"""
import json
import subprocess
import urllib.parse
from pathlib import Path

ROOT = Path(r"D:\zcode-work\pka-graph-phase2")
ZIP = ROOT / "dist" / "知能图谱-v1.0.0-windows-x64.zip"
OLD = "banana-Hu/pka-graph"
NEW = "banana-Hu/pka-graph"


def gh_token():
    r = subprocess.run(["git", "credential", "fill"],
                       input="protocol=https\nhost=github.com\n\n",
                       capture_output=True, text=True, cwd=ROOT)
    for line in r.stdout.splitlines():
        if line.startswith("password="):
            return line.split("=", 1)[1]
    return ""


def api(method: str, path: str, payload=None, token="", base="https://api.github.com"):
    cmd = ["curl", "-s", "-X", method, f"https://api.github.com{path}",
           "-H", f"Authorization: token {token}",
           "-H", "Accept: application/vnd.github+json"]
    if payload is not None:
        Path(ROOT / ".api.tmp").write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        cmd += ["--data-binary", f"@{ROOT / '.api.tmp'}"]
    r = subprocess.run(cmd, capture_output=True, text=True)
    try:
        return json.loads(r.stdout)
    except Exception:
        return {"_raw": r.stdout[:200]}


def main() -> int:
    token = gh_token()
    if not token:
        raise SystemExit("未取到 GitHub 凭证")

    # 1) 旧仓库改名（腾出 pka-graph 名字；GitHub 自动重定向旧链接）
    r = api("PATCH", "/repos/banana-Hu/pka-graph",
            {"name": "pka-graph-private"}, token)
    print("旧仓库改名:", r.get("full_name") or r.get("message"))

    # 2) 新建公开仓库
    r = api("POST", "/user/repos", {
        "name": "pka-graph",
        "description": "知能图谱（PKA Graph）：把 Obsidian 知识库接入为可测验、可复习、"
                       "可持续生长的个人知识图谱 —— AI 笔记加工 + 掌握测验 + 间隔复习 + Notion 同步",
        "private": False,
        "has_issues": True, "has_wiki": False,
        "license_template": "mit"}, token)
    print("新仓库:", r.get("full_name"), "| private:", r.get("private"),
          "|", r.get("html_url") or r.get("errors") or r.get("message"))

    # 3) 推送干净历史 + tag
    subprocess.run(["git", "remote", "rename", "origin", "old-origin"], cwd=ROOT,
                   capture_output=True)
    subprocess.run(["git", "remote", "add", "origin",
                    f"https://github.com/banana-Hu/pka-graph.git"], cwd=ROOT,
                   capture_output=True)
    for cmd in (["git", "push", "-u", "origin", "main"],
                ["git", "push", "origin", "v1.0.0"]):
        r = subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True)
        tail = (r.stdout + r.stderr).strip().splitlines()
        print("推送:", tail[-1] if tail else "?")

    # 4) Release + 资产
    body = ("知能图谱第一版：把 Obsidian 知识库接入为可测验、可复习的个人知识图谱。\n\n"
            "## 快速开始\n"
            "1. 下载 `pka-graph-v1.0.0-windows-x64.zip`，解压到**可写目录**\n"
            "2. 双击 `知能图谱服务\\知能图谱服务.exe`（自动打开浏览器界面）\n"
            "3. 双击 `知识库入口.exe` 投递材料（PDF/Markdown/TXT）\n\n"
            "完整入门：[docs/从零开始-建立你自己的知识库.md]"
            "(https://github.com/banana-Hu/pka-graph/blob/main/docs/从零开始-建立你自己的知识库.md)\n\n"
            "## 边界\n"
            "- 全部仅监听本机 127.0.0.1；数据存磁盘，关闭不丢失\n"
            "- 掌握状态只能由测验产生；入库/发布/阅读都不等于掌握\n"
            "- 未签名 exe：首次运行如被 Defender 提示，选「仍要运行」")
    r = api("POST", "/repos/banana-Hu/pka-graph/releases", {
        "tag_name": "v1.0.0", "name": "v1.0.0 · 知能图谱第一版",
        "body": body, "prerelease": False}, token)
    rel_id = r.get("id")
    print("Release:", r.get("html_url") or r.get("message"))

    if rel_id:
        name = urllib.parse.quote("pka-graph-v1.0.0-windows-x64.zip")
        upload_url = f"https://uploads.github.com/repos/banana-Hu/pka-graph/releases/{rel_id}/assets?name={name}"
        subprocess.run(["curl", "-s", "-o", str(ROOT / ".up.tmp"), "-w", "%{http_code}",
                        "--max-time", "1800", "-X", "POST", upload_url,
                        "-H", f"Authorization: token {token}",
                        "-H", "Content-Type: application/zip",
                        "--data-binary", f"@{ZIP}"], capture_output=True, text=True)
        data = json.loads((ROOT / ".up.tmp").read_text(encoding="utf-8"))
        print("资产:", data.get("name"), "|", data.get("state"), "|",
              data.get("size", 0) // 1024 // 1024, "MB")
        print("下载:", data.get("browser_download_url"))
    (ROOT / ".api.tmp").unlink(missing_ok=True)
    (ROOT / ".up.tmp").unlink(missing_ok=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
