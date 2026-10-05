"""第七阶段：受控原文获取（任务 4）。

只处理用户明确提供的本地文件和用户明确选择的公开 URL。
远程抓取默认关闭（PKA_DISCOVERY_FETCH=off），启用需确认来源范围。
不绕过登录/付费/访问控制；不下载/执行项目代码；保留超时/重定向边界。

三种结果严格区分：
- fetch_ok：内容获取成功
- parse_ok：正文解析成功
- sufficient：内容足以分析（由调用方判定）
"""

from __future__ import annotations

import hashlib
import re
import urllib.error
import urllib.request
from pathlib import Path

MAX_SIZE = 0           # 单文件不设字节上限；显式正数仅供受限调用/测试
MAX_REDIRECTS = 3
TIMEOUT = 15


class FetchError(Exception):
    def __init__(self, message: str, *, category: str):
        super().__init__(message)
        self.category = category  # network | timeout | auth | too_large | redirect | parse | local_missing


def read_local_file(path: str, vault_root: Path) -> dict:
    """读取本地文件（限测试知识库内或用户显式路径）。返回材料。"""
    p = Path(path).resolve()
    vault = Path(vault_root).resolve()
    if not p.exists():
        return {"status": "missing", "text": "", "note": f"文件不存在：{path}"}
    if p.suffix.lower() not in (".md", ".txt", ".html", ".htm"):
        if p.suffix.lower() == ".pdf":
            return {"status": "unsupported_format", "text": "",
                    "note": "PDF 暂不支持（需引入解析库，待确认后再启用）"}
        return {"status": "unsupported_format", "text": "", "note": f"不支持的格式：{p.suffix}"}
    raw = p.read_bytes()
    if MAX_SIZE > 0 and len(raw) > MAX_SIZE:
        return {"status": "too_large", "text": "", "note": f"文件超过 {MAX_SIZE // 1000}KB 上限"}
    text = raw.decode("utf-8", errors="replace")
    if p.suffix.lower() in (".html", ".htm"):
        text = _strip_html(text)
    return {
        "status": "ok", "text": text,
        "source_path": str(p),
        "sha256": hashlib.sha256(raw).hexdigest(),
        "read_scope": "full_text",
    }


def _strip_html(html: str) -> str:
    """最简 HTML → 纯文本：去 script/style/tag，保留正文。"""
    text = re.sub(r"<script[^>]*>.*?</script>", "", html, flags=re.S | re.I)
    text = re.sub(r"<style[^>]*>.*?</style>", "", text, flags=re.S | re.I)
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"\s{2,}", "\n", text)
    return text.strip()


def _is_private_url(url: str) -> tuple[bool, str]:
    """拒绝内网/本机/非 HTTP(S) 地址。"""
    from urllib.parse import urlparse

    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        return True, f"协议不允许：{parsed.scheme}"
    host = parsed.hostname or ""
    if host in ("localhost", "127.0.0.1", "0.0.0.0", "::1", "[::1]"):
        return True, "不允许访问本机地址"
    if re.match(r"^10\.|^172\.(1[6-9]|2\d|3[01])\.|^192\.168\.", host):
        return True, "不允许访问内网地址"
    if host.endswith(".local") or host.endswith(".internal"):
        return True, "不允许访问内部域名"
    return False, ""


def fetch_public_url(url: str, *, max_chars: int | None = None) -> dict:
    """受控抓取公开 URL。默认不启用（调用方需先确认来源范围）。"""
    blocked, reason = _is_private_url(url)
    if blocked:
        return {"status": "blocked", "text": "", "note": reason, "url": url}
    req = urllib.request.Request(url, headers={"User-Agent": "PKAGraph/1.0 (+knowledge-discovery)"})
    try:
        # 手动跟随重定向以检查目标
        opener = urllib.request.build_opener(_SafeRedirectHandler())
        with opener.open(req, timeout=TIMEOUT) as resp:
            content_type = resp.headers.get("Content-Type", "")
            raw = resp.read(MAX_SIZE + 1 if MAX_SIZE > 0 else -1)
            final_url = resp.url
    except urllib.error.HTTPError as exc:
        return {"status": "http_error", "text": "", "url": url,
                "note": f"HTTP {exc.code}（不转发正文）"}
    except urllib.error.URLError as exc:
        reason_str = str(getattr(exc, "reason", ""))
        cat = "timeout" if "timed out" in reason_str.lower() else "network"
        return {"status": "fetch_error", "text": "", "url": url, "note": f"网络错误（{cat}）"}
    if MAX_SIZE > 0 and len(raw) > MAX_SIZE:
        return {"status": "too_large", "text": "", "url": url,
                "note": f"响应超过 {MAX_SIZE // 1000}KB 上限"}
    if "text/html" in content_type:
        text = _strip_html(raw.decode("utf-8", errors="replace"))
    elif "text/plain" in content_type or "application/json" in content_type:
        text = raw.decode("utf-8", errors="replace")
    else:
        return {"status": "unsupported_type", "text": "", "url": url,
                "note": f"不支持的内容类型：{content_type}"}
    if max_chars is not None and len(text) > max_chars:
        return {"status": "too_large", "text": "", "url": url,
                "note": f"正文超过显式 {max_chars} 字符限制；未截断后冒充全文"}
    return {
        "status": "ok", "text": text, "url": url,
        "final_url": final_url, "sha256": hashlib.sha256(raw).hexdigest(),
        "read_scope": "full_text", "bytes": len(raw), "truncated": False,
    }


class _SafeRedirectHandler(urllib.request.HTTPRedirectHandler):
    """限制重定向次数并检查目标。"""
    max_redirections = MAX_REDIRECTS

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        blocked, reason = _is_private_url(newurl)
        if blocked:
            raise urllib.error.HTTPError(newurl, code, f"重定向目标被拒绝：{reason}", headers, None)
        if getattr(req, "_redirect_count", 0) >= self.max_redirections:
            raise urllib.error.HTTPError(newurl, code, "重定向次数超限", headers, None)
        req._redirect_count = getattr(req, "_redirect_count", 0) + 1
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def parse_pdf_placeholder(path: str) -> dict:
    """PDF 解析占位：需要引入 PyMuPDF 等依赖，待确认后再启用。"""
    return {"status": "not_implemented", "text": "",
            "note": "PDF 解析需要额外依赖，本阶段未引入；请提供 .md/.txt 版本或直接粘贴内容"}
