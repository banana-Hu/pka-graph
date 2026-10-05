"""受限 Markdown front-matter 解析器。

支持范围（明确记录，超范围一律抛 FrontmatterError，不猜测）：
- 文件以 ``---`` 单独一行开始，其后再次出现 ``---`` 单独一行结束；
- 条目形如 ``key: value``：value 为不带引号标量、引号标量或空（空 = 待定列表头）；
- 列表一：``key:`` 行之后若干行 ``  - item``（短横线列表）；
- 列表二：``key: [a, b, c]`` 单行扁平内联字符串列表（空格可省略；项可为引号标量；
  不支持嵌套、不支持项内逗号与方括号）——真实知识库 2026-10-01 书单类笔记使用此格式；
- 其他一切（嵌套映射、多行字符串、项内逗号/嵌套括号、tab 缩进）均视为格式异常报错。
本解析器不是通用 YAML 解析器。
"""

from __future__ import annotations

from pathlib import Path


class FrontmatterError(ValueError):
    """front-matter 缺失或超出受限格式支持范围。"""


def split_frontmatter(text: str) -> tuple[dict, str]:
    """拆出 (元数据 dict, 正文)；没有 front-matter 时返回 ({}, 原文)。"""
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return {}, text
    end = None
    for i in range(1, len(lines)):
        if lines[i].strip() == "---":
            end = i
            break
    if end is None:
        raise FrontmatterError("front-matter 未闭合：缺少结束的 '---' 行")
    meta = _parse_lines(lines[1:end])
    body = "\n".join(lines[end + 1 :])
    return meta, body


def parse_file(path: Path) -> tuple[dict, str]:
    text = path.read_text(encoding="utf-8")
    return split_frontmatter(text)


def _parse_lines(lines: list[str]) -> dict:
    meta: dict = {}
    current_key: str | None = None

    for lineno, raw in enumerate(lines, start=2):
        if not raw.strip():
            continue
        if raw.startswith("\t"):
            raise FrontmatterError(f"第 {lineno} 行：使用 tab 缩进，超出支持范围")
        indented = raw.startswith(" ")
        stripped = raw.strip()

        if indented:
            if current_key is None:
                raise FrontmatterError(f"第 {lineno} 行：缩进行没有所属键")
            if not stripped.startswith("- "):
                raise FrontmatterError(f"第 {lineno} 行：列表项必须以 '- ' 开头")
            meta[current_key].append(_scalar(stripped[2:], lineno))
            continue

        if ":" not in stripped:
            raise FrontmatterError(f"第 {lineno} 行：缺少 'key:' 分隔")
        key, _, value = stripped.partition(":")
        key = key.strip()
        if not key or " " in key:
            raise FrontmatterError(f"第 {lineno} 行：非法键名 {key!r}")
        value = value.strip()
        if key in meta:
            raise FrontmatterError(f"第 {lineno} 行：键 {key!r} 重复")
        if value == "":
            meta[key] = []
            current_key = key
        elif value.startswith("[") and value.endswith("]"):
            meta[key] = _inline_list(value, lineno)
            current_key = None
        elif "[" in value or "{" in value:
            raise FrontmatterError(f"第 {lineno} 行：嵌套/半开集合超出支持范围（仅支持整行 [a, b] 扁平列表）")
        else:
            meta[key] = _scalar(value, lineno)
            current_key = None

    return meta


def _inline_list(value: str, lineno: int) -> list[str]:
    """解析整行扁平列表 ``[a, "b c"]``：项为标量，不支持嵌套或项内逗号。"""
    inner = value[1:-1].strip()
    if inner == "":
        return []
    items: list[str] = []
    current = ""
    quote: str | None = None
    for ch in inner:
        if quote:
            if ch == quote:
                quote = None
            else:
                current += ch
            continue
        if ch in ('"', "'"):
            quote = ch
            continue
        if ch == ",":
            items.append(current.strip())
            current = ""
            continue
        if ch in "[]{}":
            raise FrontmatterError(f"第 {lineno} 行：内联列表包含嵌套括号，超出支持范围")
        current += ch
    if quote is not None:
        raise FrontmatterError(f"第 {lineno} 行：内联列表引号未闭合")
    items.append(current.strip())
    return [item for item in items if item != ""]


def _scalar(value: str, lineno: int):
    if value.startswith("[") or value.startswith("{"):
        raise FrontmatterError(f"第 {lineno} 行：内联集合解析异常（应整行以 [ 开头 ] 结尾）")
    if len(value) >= 2 and value[0] == value[-1] and value[0] in ('"', "'"):
        return value[1:-1]
    return value
