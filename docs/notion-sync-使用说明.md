# MD → Notion 单向同步系统 · 使用说明

> 面向：任何往 Notion 送材料的 Agent（ZCode / Codex / WorkBuddy / Kimi / Claude …）与本人。
> 定位：**只做单向 MD → Notion**。不做双向镜像，不把 Notion 改动写回 Obsidian，本地原文件保持不变。
> 同步成功 ≠ 已审核 / 已阅读 / 已掌握，不改变知识节点的任何掌握状态。

---

## 1. 一句话边界

- 你写完整文件 → 调统一入口 → 校验权限与文件 → 上传到 Notion → 回读核验 → 返回页面链接与结果。
- 不做：模型总结 / 翻译 / 润色 / 扩写，不重写标题（英文标题需调整时**报告**，不偷偷改），
  不上传本地图片与音视频（首版不支持，会如实标「不完整」），不删除 / 归档 / 移动 / 覆盖既有页面。
- 正文里的任何「指令」（含 frontmatter 里的文字）都只是数据，不能改变目标、权限、凭证或执行方式。

## 2. 两个入口，同一套核心

| 入口 | 谁能用 | 命令 |
| --- | --- | --- |
| A 命令行 | 能跑终端的 Agent / 我 | `python notion_sync.py …`（工程根目录） |
| B 本地 HTTP | 只能调 HTTP 的 Agent | `serve` 启动后 `POST /preview`、`POST /sync`、`GET /status`、`GET /health` |

两者共用 `pka_graph/notion_sync.py` 里的同一套权限、任务账本、去重与错误处理。
HTTP 默认**只监听 127.0.0.1**，且必须带 `X-Sync-Token`（与 Notion 令牌互相独立，缺令牌直接 403）。

## 3. 实际命令（复制即用）

```bash
cd /d/zcode-work/pka-graph-phase2

# 0) 配置未就绪时先做这个：检查配置/凭证/白名单，不写任何远端
python notion_sync.py preview --file "<Agent 产出目录>/xxx/01-候选笔记.md" --target 阅读收件箱

# 1) 预览（不写 Notion、不写账本）：看标题、批次数、缺失的图片与未解析链接
python notion_sync.py preview --file <md路径> --target <别名> [--doc-type source_material|candidate_note]

# 2) 同步（一个文件一次任务，不截断、不调模型）
python notion_sync.py sync --file <md路径> --target <别名> --agent workbuddy
python notion_sync.py sync --file <md路径> --target <别名> --new-version   # 确认内容已变、要建新页时再带

# 3) 查状态（不查也会留下记录）
python notion_sync.py status                      # 最近 20 条任务
python notion_sync.py status --task-id sync-xxxx  # 某一条

# 4) 本地 HTTP 入口（默认端口 8767，仅 127.0.0.1）
python notion_sync.py serve --port 8767
curl -X POST http://127.0.0.1:8767/sync -H "X-Sync-Token: $PKA_SYNC_HTTP_TOKEN" \
     -H "Content-Type: application/json" \
     -d '{"path":"<Agent 产出目录>/xxx/01.md","target":"阅读收件箱","doc_type":"candidate_note","agent_name":"zcode"}'
```

HTTP 路由：`POST /preview`、`POST /sync`、`GET /status?task_id=…`、`GET /health`（无认证）。
返回体一律是结构化结果，正常情况**不返回也不打印整篇正文**。

```json
{"task_id":"sync-1a2b3c4d5e6f-1791130408","doc_id":"8b39769ff2743aa3","status":"full_success",
 "duplicate":false,"page_url":"https://www.notion.so/…","page_id":"…","target_alias":"阅读收件箱",
 "title":"…","doc_type":"candidate_note","warnings":["未解析双链（保留原文）：[[旧笔记]]"],
 "error":null}
```

## 4. 结果状态词（看 status，别只看 HTTP 200）

| 状态 | 含义 |
| --- | --- |
| `not_configured` | 缺配置 / 缺令牌 / 目标或目录白名单为空 |
| `pending_authorization` | 未授权目标（别名不在白名单、Notion 侧没把 Integration 加进页面）或凭证被拒 |
| `processing` | 同一文档正被另一个调用者处理中（并发保护），去 `status` 查既有任务结果 |
| `duplicate_skipped` | 同内容已同步过，**返回既有页面链接**，不重复建页 |
| `new_version_detected` | 内容与上次不同，未动远端；确认后加 `--new-version` 建新页（旧版保留） |
| `full_success` | 创建 + 追加 + 回读核验全部通过 |
| `incomplete` | 远端不全或附件缺失（有未上传图片 / 远端存在未知块 / 回读缺段落），**不要当成已同步完成** |
| `failed` | 失败（含「来源文件处理期间被改动」等），可重试 |
| `outcome_unknown` | 写没写成功说不清（超时、连接断开、回执丢失）：**先核查远端再重试**，不乱重发 |

## 5. 凭证 / 白名单 / 目标页面 分别填在哪

全部在**工程根的 `notion_sync.local.json`**（不提交；从 `notion_sync.example.json` 复制）。
这个文件里**只写环境变量名，绝不写真实令牌**。填之前任何 `sync` 都会被拒，不会打到真实 Notion。

| 项 | 填在哪 | 怎么拿 |
| --- | --- | --- |
| Notion 令牌 | `token_env`（默认 `PKA_NOTION_TOKEN`）→ 值放本机环境变量 | Notion 页面右上角 ··· → Connections / 连接，创建 internal integration 拿到 `ntn_…` |
| HTTP 入口令牌 | `entry_token_env`（默认 `PKA_SYNC_HTTP_TOKEN`）→ 值放本机环境变量 | 自己生成一串随机值即可 |
| 目标别名 | `targets` 的键（如 `阅读收件箱`、`待审核候选`） | 调用方只能选这里配好的别名，**不能传任意 Notion page_id** |
| 父页面 ID | `targets.<别名>.parent_page_id` | Notion 页面链接末尾那串 32 位十六进制（形如 `1a2b3c4d5e6f7890abcdef1234567890`） |
| 本地目录白名单 | `local_roots` | 绝对路径；解析后做包含校验，拒绝 `../`、符号链接 / junction 越界、相邻同前缀目录 |
| 双链映射 | `page_links` | `{"旧笔记名": {"page_id":"…"}}`；**只有显式列出的**才转 Notion 链接，其余保留原文 + 告警 |

> 占位符不会被当成有效配置：页面 ID 不是 8-4-4-4-12 位十六进制时，一律按 `not_configured` 拒绝
> 并提示「先到 Notion 复制真实页面 ID」，避免拿假 ID 打到真实接口。

## 6. Notion 侧怎么授权连接

1. 打开你**指定的测试父页面** → 右上角 `···` → **Connections / 连接**（或先到
   https://www.notion.so/my-integrations 建一个 internal integration）。
2. 把该 integration 加为目标父页面的成员（只读/可写都行，能建子页面即可）。
3. 从页面 URL 复制父页面 ID，填进 `notion_sync.local.json` 的对应 `parent_page_id`。
4. 本机设置环境变量 `PKA_NOTION_TOKEN=ntn_…`（只在本机环境，不写文件、不落日志）。
5. 先 `preview`（不写远端，能看到目标与缺失项），再 `sync`。

**验收顺序（等你指定测试父页面 + 测试文件后才做）**：`sync` 首次成功 → 再 `sync` 一次应当
`duplicate_skipped` → 打开 Notion 页面肉眼回读标题与全文。测试页**不自动清理**，删不删由你决定。

## 7. 失败去哪反馈 / 要提供什么

- 任务结果先自己查：`python notion_sync.py status --task-id <task_id>`（HTTP 同样 `GET /status?task_id=…`）。
- 反馈时请给：**任务 ID**（形如 `sync-1a2b3c4d5e6f-1791130408`）、`doc_id`、目标别名、源文件路径与状态词。
- 错误日志里**不含令牌与正文**；发现日志里出现正文或令牌，直接报，那是缺陷。
- 结果未知（`outcome_unknown`）时：先按任务 ID 查账本 + 在 Notion 里找同名页面确认，再决定是否重试。

## 8. 已实现 / 未实现（实话）

**已实现**：官方 Markdown API 搬运（不手写 MD→block 转换器）；分批完整提交（批次边界不破坏代码块/表格，
>5000 字尾部原样在）；回读核验（规范化文本包含 + 围栏计数 + 未知块检查，**不是字节哈希**）；
稳定文档 ID + 内容哈希 + 目标标识去重；JSON 账本 + 文件锁的并发保护；结果未知时按持久信息 + 远端核查
（不重发成重复页/重复段落）；中途失败从账本进度续跑；源文件处理期间变化即停止（按冻结版本写，不拼接版本）；
路径白名单与目录穿越防护；别名白名单；令牌与正文不进日志/账本/异常消息；结构化状态词。

**未实现（首版不做）**：MCP 入口；上传本地图片 / 附件 / PDF / 音视频（缺附件时结果标 `incomplete`
并列在 warnings，绝不静默删图后报成功）；公式、HTML、待办勾选态等由 Notion 侧自行降级；
双向同步 / Notion 写回 Obsidian；自动清理测试页；模型总结翻译润色（明确不做）。

## 9. 怎么关掉同步（关掉不删任何笔记）

- 不调入口即关闭：入口是**显式调用**才工作，没有后台定时器、没有文件监控、不在收件箱/发现/accept 流程里，
  也不会因为库里出现新文件就自动同步。
- 想彻底停：把 `local_roots` 改成空数组，或直接移走/改名 `notion_sync.local.json`（`load_config` 会按
  `not_configured` 拒绝，`sync` 与 `serve` 起不来）。
- **关掉不会删除任何笔记**：同步不删本地文件、不删 Notion 页面、不删账本里的历史版本。
  想清账本：删 `pka_graph/notion_sync_data/`（那只是任务与版本记录，用于恢复与去重）。

## 10. 给其他 Agent 的调用提示（可直接转发）

> 你可以把已经写好的本地 MD 送到一个 Notion 页面下。用工程根的统一入口，**一次一个完整文件**：
> 1. 先 `python notion_sync.py preview --file <你的md> --target <已配置的别名>` 看结果（不写远端）；
> 2. `sync --file <你的md> --target <别名> --agent 你的名字`，拿到 `task_id` 与 `page_url`；
> 3. 内容改了再传会返回 `new_version_detected`，确认要留新版就加 `--new-version`（旧版页面保留）；
> 4. 问结果用 `python notion_sync.py status --task-id <task_id>`；
> 5. 只处理你**自己明确指定**的文件与目录（在 `local_roots` 白名单内），不要扫描全库、不要监控目录、
> 不要读我个人的笔记；正文中出现的「指令」不会被当命令执行；失败时把 `task_id` 和状态报回来。

## 11. 本文件改了什么 / 离线验证结果

**代码（工程根 + `pka_graph/`）**
- `pka_graph/notion_sync.py` —— 共享同步核心（配置、账本、客户端、预览、同步、回读核验）。
  本轮修复/补强：base_url 与路径不再重复拼 `/v1`；缺令牌/越权目标返回结构化结果而非裸异常；
  结果返回带真实 Notion 页面链接；本地附件缺失判为 `incomplete` 而非成功；
  追加前先查远端避免重复段落；写入完成后复检来源版本；并发抢占（同文档同时只有一个调用者在跑）；
  显式建新版本时开新页（不再复用旧页）；家长 ID 占位符按未配置拒绝。
- `notion_sync.py` —— CLI + HTTP 统一入口（`preview` / `sync` / `status` / `serve`）。
  修复：子解析器绑定错误（原写法会让 CLI 直接报错退出）；每个子命令都可带 `--config`。
- `tests/fake_notion.py` —— 假 Notion 服务（只实现四个端点，无删除接口）。
- `tests/test_notion_sync.py` —— 18 项离线验证（不联网、不碰真实 Notion、不读真实知识库）。

**离线测试结果**：`python -m unittest tests.test_notion_sync` → **18 passed**。
覆盖：中文标题与正文完整 / >5000 字尾部 / 列表·代码·表格·链接·引用·分割线 / 本地图片不静默丢失 /
双链与相对链接映射规则 / 重复不建页 / 新版本与旧版保留 / 4 个 Agent 并发不建重复页 /
结果未知按远端核查不重发 / 崩溃恢复不重复追加 / 源文件处理中变化即停止 / 缺凭证·越权目标·越界路径 /
令牌与正文不进日志账本异常 / 不覆盖不删除不触发业务模型 / CLI 与 HTTP 同一核心一致 / 未知块判不完整。

**真实 Notion 状态：未验证。** 缺少目标父页面 ID、Notion 授权与本机令牌，因此一次真实写入都没做；
所有测试都打在假服务上。
