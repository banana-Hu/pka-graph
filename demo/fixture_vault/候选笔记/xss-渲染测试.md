---
id: note-20261001-777
status: candidate
title: "XSS 渲染测试笔记（含脚本样文）"
domain: "AI技术领域"
---

# 渲染安全测试

**测试数据**：本笔记正文包含脚本与注入样文，用于验证界面渲染必须转义、不能执行。

<script>alert('xss-by-script')</script>

<img src=x onerror="alert('xss-by-img')">

[点击领奖](javascript:alert('xss-by-link')) 与 <b>加粗注入</b> 与 <iframe src="about:blank"></iframe>

正常段落：图图谱渲染应当把以上内容显示为纯文本。
