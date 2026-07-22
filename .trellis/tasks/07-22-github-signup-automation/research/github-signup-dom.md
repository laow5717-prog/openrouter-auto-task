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

**诚实声明**：过码后的「输入 launch code」验证页，本项目**从未真实到达过**（需人工手动过
Arkose 才能进入），其选择器为**推断**。`github_signup.submit_email_code()` 用一组启发式候选
（`autocomplete="one-time-code"` / name·id 含 otp·launch·code·verification / `inputmode="numeric"`），
并兼容单框整串与分格逐位两种 OTP 形态。首次真实到达时应先看 `dump_verification_dom()` 的产出，
据此把权威选择器补进本文档，再收敛 `_CODE_INPUT_CANDIDATES` / `_CODE_SUBMIT_CANDIDATES`。

判定注册完成 `detect_signup_complete()` 走间接信号：URL 离开 /signup·verify·/session 进入
github.com 主区，或 `meta[name="user-login"]` 有值 / 出现 dashboard·头像·侧栏节点。
