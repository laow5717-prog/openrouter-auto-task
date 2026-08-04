# GitHub signup 页 DOM 侦察结论（步骤0 实跑产出）

侦察时间：2026-07-22，实跑 `scripts/probe_github_signup.py`（有头 Patchright）。
页面：`https://github.com/signup?source=form-home-signup&user_email=`
title：`Sign up for GitHub · GitHub`

## 关键结论

1. **单页全字段表单**（非逐字段揭示）：email / password / username / country / 两个 checkbox
   一开始就全部 `visible=true`。无需"填一个等下一个"。
2. **mail.tm 邮箱被接受**：填入 `xxx@web-library.net` 后邮箱框右侧显示**绿色对勾**，
   `#email-err` 无错误文本 —— 该域名未被 GitHub 拒收（推翻了"临时邮箱必被拒"的预设）。
3. **Country 自动填充**：填邮箱后 `#country-dropdown-panel-button` 文本从 "Select Country/Region"
   自动变为 "United States of America"（GitHub 按 IP 地理填充）。通常无需手动选。
4. **Create account 按钮初始 disabled**，需三字段校验通过后才 enabled。
5. **Arkose FunCaptcha** 容器 `#captcha-container-nux` 初始就在 DOM（点提交后加载挑战）；
   另有 DataDome iframe `octocaptcha.com/datadome?origin_page=github_signup_redesign`（初始风控）。

## 字段选择器（权威）

| 字段 | 选择器 | name | 校验/提示节点 |
|---|---|---|---|
| Email | `#email` | `user[email]` | `#email-err`（错误文本容器） |
| Password | `#password` | `user[password]` | `#password-helper` |
| Username | `#login` | `user[login]` | `#username-helper` |
| Country 按钮 | `#country-dropdown-panel-button` | hidden `user_signup[country]` | 文本即当前选中国家 |
| Country 过滤框 | `#country-dropdown-panel-filter` (name=filter) | — | 展开下拉后可用 |
| Copilot 勾选 | `#user_signup\[copilot_opt_in\]` (checkbox) | `user_signup[copilot_opt_in]` | 默认**勾选** |
| Marketing 勾选 | `#user_signup\[marketing_consent\]` (checkbox) | `user_signup[marketing_consent]` | 默认不勾选 |
| 提交按钮 | `button[type=submit]` 文本 "Create account" | — | 初始 disabled |
| Arkose 容器 | `#captcha-container-nux` | — | 提交后加载挑战 |

> 注意：checkbox 的 id 含方括号 `user_signup[copilot_opt_in]`，CSS 选择器需转义为
> `#user_signup\[copilot_opt_in\]`，或用 `input[name="user_signup[copilot_opt_in]"]`。

## 字段规则（页面提示原文）

- Password：should be at least 15 characters OR at least 8 characters including a number and a lowercase letter.
- Username：may only contain alphanumeric characters or single hyphens, and cannot begin or end with a hyphen.（≤39 字符）

## 干扰项（勿填）

除上表可见字段外，页面还有大量 hidden input（`authenticity_token` / `timestamp` /
`timestamp_secret` / `octocaptcha-token` / `required_field_58dd`(蜜罐) 等），均由页面自身管理，
**自动化不要触碰**，尤其 `required_field_58dd` 是隐藏蜜罐，填了会被判机器人。

## 终态判定

- `reached_captcha`：点 Create account 后 `#captcha-container-nux` 内出现可见 challenge
  （Arkose iframe 可见）。本轮到此为止。
- `rejected_by_github`：`#email-err` 出现可见文本，或 username 提示"unavailable/taken"。
- `error`：字段定位失败 / 超时 / 页面结构与本文档不符。

## 半自动收尾（semi_auto=True）—— 验证页 DOM 待收敛

MVP 默认路径到 `reached_captcha` 为止即完成 PRD 验收。`semi_auto=True` 是超出 MVP 的扩展：
过码后收 mail.tm 里的 GitHub launch code（8 位）回填完成注册。

### 验证页真实 DOM（2026-07-22 实跑 --semi-auto 到达并侦察确认）

关键发现：**mail.tm 邮箱这条路很多时候压根不出 Arkose 验证码**，提交后 GitHub 直接跳
`https://github.com/account_verifications`（title `Please verify your email address`）要求输入
邮箱里收到的 8 位 launch code。因此新增终态 `TERM_VERIFY_EMAIL`，无需人工即可全自动收码回填。

验证页 DOM（权威）：

| 元素 | 选择器 | 说明 |
|---|---|---|
| launch code 输入 | 8 个 `input[name="launch_code[]"]`，`id="launch-code-0"`..`launch-code-7` | `type=number` `maxlength=1` 分格 OTP |
| 提交按钮 | `button` 文本 "Continue" | **填满 8 位前 disabled** |
| 页面 URL | `https://github.com/account_verifications` | — |
| 收件人提示 | "We have sent a code to <email>" | — |

**填码坑（已修）**：分格 OTP 不能逐格 `fill()`（只设 value 不派发完整事件，Continue 保持
disabled——与 Turnstile 受控组件同类问题）。正确做法：聚焦第一格后 `press_sequentially(整串)`，
GitHub 自动逐格进焦并派发原生 keydown/input 事件，Continue 才会启用。填完轮询等 Continue
enabled 再点击，兜底按 Enter。

**完成判定坑（已修）**：`detect_signup_complete` 早期把 URL 里不含字面 "verify" 当作已离开验证流，
但验证页是 `account_verifications`（"verifications" 不含 "verify"），导致码没提交成功也误报完成。
已改为排除 "verif" 片段（覆盖 verify / account_verifications），必须真正离开验证流才算完成。

判定完成信号：URL 离开 signup·verif·session·login·suspended 进入 github.com 主区（按真实 host
判断，不能子串匹配——OAuth 回跳 URL 的 query 里常编码有 github.com），或 `meta[name="user-login"]`
有值 / 出现 dashboard·头像·侧栏节点。

### 完整端到端实跑结论（2026-07-22，多次 --semi-auto 实跑）

流程已**完全跑通**：填表 → 提交 → 直接进 account_verifications（无 Arkose）→ 收 mail.tm
launch code → 逐格填 8 位 → 提交 → 账号创建成功（绿条 "Your account was created successfully"）
→ 跳登录页 → 用新用户名/密码自动登录。

**关键坑（均已修）**：
1. 分格 OTP 逐格 `fill()` 只设 value 不派发事件，Continue 保持 disabled。改为聚焦首格
   `press_sequentially(整串)`；提交按钮 disabled 时**在输入框按 Enter 可靠提交**（实测生效）。
2. 提交按钮不能用 `button[type=submit]` 泛选——会误点 "Continue with Google" 社交按钮跳到
   Google OAuth。必须限定在 launch_code 所在 `<form>` 内，或文本严格等于 Continue/Verify。
3. 完成判定必须按 URL 真实 host，且排除 /suspended。

**决定性外部限制（非脚本缺陷）**：账号建成后**登录即被 GitHub 反滥用立即挂起**，跳
`https://github.com/suspended`（连续多个账号 100% 复现）。根因是 GitHub 风控识别了
「mail.tm 的 web-library.net 临时邮箱域名 + Patchright 自动化指纹 + 数据中心 IP」组合。
脚本机制无缺陷，终态如实报 `outcome=account_suspended`（ok=False）。

**后续要突破挂起可尝试（下一任务）**：换非公开临时邮箱域名 / 自建邮箱域、住宅代理 IP、
更真人化的节奏与指纹、账号建成后先养号再登录。这些超出本任务范围。
