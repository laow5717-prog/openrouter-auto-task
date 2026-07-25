# 用 hotmail 数据注册 GitHub 并落库标记

## Goal

用根目录 `hotmail.xlsx` 的真实 hotmail 邮箱替换现有 mail.tm 临时邮箱，验证码改从 ruoanzhu 收信链接抓取；注册成功后把账号信息写入 `accounts` 表并标记状态。

## Background / 现状

- 现有闭环 `src/services/github_signup_service.py::signup_one` 全程用 mail.tm：`create_temp_email()` 建箱、`wait_for_github_launch_code(token, since_ts)` 收码，且**完全不落库**。
- `hotmail.xlsx`（Sheet1，10 行）每行是单串：`邮箱----密码----收码链接`，收码链接形如
  `https://www.ruoanzhu.com/s?e=<email>&p=<pwd>&h=1&r=1&i=2`。
- 实测该链接返回 HTML 收信页，邮件嵌在 `email-title`(主题+时间) / `email-content`(正文) 块中；已成功读到一封测试邮件（正文含"测试码：422025"），**收信链路本身是通的**。
- 外部约束：GitHub 注册中途出现 Arkose 人机验证码，`signup_one` 默认模式只能推进到验证码为止；真正跑到"注册成功"必须 `--semi-auto` 有头 + 人工手动过码。此约束与用哪种邮箱无关。

## Requirements

### 阶段一（本轮交付）— 收码链路验证 [lightweight]
- R1 能解析 `hotmail.xlsx`：把每行 `邮箱----密码----链接` 拆成结构化 (email, password, link)。
- R2 实现 ruoanzhu 收码函数：给定收信链接（或 email+pwd），拉取 HTML、解析邮件块、按发件人/主题过滤 GitHub、提取 8 位 launch code；与现有 `wait_for_github_launch_code` 同构（可作 drop-in 替换）。
- R3 用真实数据做一次实测，证明"读 xlsx → 拉 ruoanzhu → 解析出验证码"链路可跑通（用现有测试邮件或已有邮件验证解析正确性）。
- R4 本轮不改 `signup_one` 主流程、不落库、不解 Arkose。用独立可运行脚本承载，产出结构化结果。

### 阶段二（后续）— 半自动注册 + 落库标记
- R5 改造 `signup_one`：支持传入已有 hotmail 邮箱 + 注入收码函数（替换 mail.tm）。
- R6 半自动模式（有头 + 人工过码）跑通后，把账号 (email, login_password=github密码, email_password=hotmail密码) 通过 `AccountModel.upsert` 写入 `accounts` 表。
- R7 注册成功标记状态（如 `status='registered'` / 成功态），失败/挂起区分状态；批量入口遍历 xlsx 逐条处理。

### 阶段三 — 注册成功后自动登录 opencode 并进自己的 /go 页
- R8 注册成功后，在**同一浏览器 session**里自动登录 opencode（GitHub OAuth）。
- R9 登录后取该账号**自己自动创建的** workspace id，导航到 `/workspace/{own_wid}/go`。
- 探测结论（2026-07-25 实机）：
  - OAuth 链路：访问受保护页→跳 `auth.opencode.ai/authorize`→点「Continue with GitHub」(`<a role=link>`)
    →GitHub 授权页点「Authorize」(新号首次；client_id=Iv23liOTxMmED77mtyGd，scope read:user+user:email)
    →回落 `opencode.ai/workspace/{own_wid}`。GitHub 授权持久，重登无感；但 opencode 会话不跨浏览器重启持久。
  - **用户给的 `wrk_01KXQBHDNVBKX30TK740YF5D5F/go` 新号进不去**：非成员访问该 workspace 被弹回
    `auth.opencode.ai`。故目标改为「新号自己的 /go 页」（每个新号登录后自动建一个自己的 workspace）。

## Constraints

- 阶段一不动主流程与数据库，隔离在新脚本/新模块，回滚只需删文件。
- 收码解析对 HTML 结构变化要有容错（找不到邮件/找不到码时返回 None，不抛异常）。
- 不把 hotmail 明文密码打进日志正文（可打掩码）。

## Acceptance Criteria

### 阶段一 ✅（2026-07-25 完成）
- [x] 能从 `hotmail.xlsx` 正确解析出全部 10 行的 (email, password, link)。实测 10/10。
- [x] ruoanzhu 收码函数对真实邮箱返回可解析的邮件列表；对含验证码的邮件能提取出码。
      carold030 实测拉到 1 封测试邮件；合成 GitHub 邮件单测提取出 8 位码 83920145 且未误取测试码。
- [x] 提供可直接运行的验证脚本 `scripts/test_hotmail_ruoanzhu.py`，无未捕获异常。

交付物：`src/services/hotmail_inbox.py`（xlsx 解析 + ruoanzhu 收信解析 + GitHub 收码轮询）、
`scripts/test_hotmail_ruoanzhu.py`（验证脚本）。

### 阶段二（代码完成，真实注册待人在场验收）
- [x] `signup_one` 增 `account` 参数，可用 hotmail 邮箱 + ruoanzhu 收码跑半自动；mail.tm 老路径经 trellis-check 逐行确认零变化。
- [x] `--import` 已把 10 个 hotmail 导入 accounts 表（status='imported'，email_password 落对）。
- [x] `_persist_result` 状态映射单测通过（registered/suspended/failed，失败态不清凭据）。
- [x] 【已验收 2026-07-25】真实跑 carold030：**全自动跑通，连 Arkose 都没出现**（真实 hotmail+持久 profile），
      ruoanzhu 收到真实 GitHub launch code 19898948，账号创建并登录 dashboard，**未挂起**；
      accounts 中 status='registered'、login_password 有值、email_password=hotmail 密码。突破了旧 mail.tm 挂起卡点。

交付物（阶段二）：`src/services/github_signup_service.py`（收码解耦 + account 参数）、
`scripts/run_hotmail_github_signup.py`（批量入口 + 落库）。

## Notes

- 阶段一为 PRD-only 轻量交付；阶段二涉及主流程改造，届时再补 design/implement。
