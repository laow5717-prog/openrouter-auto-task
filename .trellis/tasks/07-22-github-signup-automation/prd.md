# GitHub 自动注册（mail.tm 临时邮箱，为 opencode OAuth 前置）

## Goal

自动化 GitHub 账号注册流程的**前半段**：用本项目已有的 mail.tm 临时邮箱，打开 GitHub 注册页
（`https://github.com/signup?source=form-home-signup&user_email=`），自动填入邮箱 / 密码 /
用户名等信息并提交，一路推进到「人机验证码（Arkose FunCaptcha）出现」为止。

最终业务目的：opencode.ai 仅支持 GitHub / Google OAuth 登录，需要可批量产出的 GitHub 账号作为
OAuth 前置。本任务只交付注册流程的自动填表与提交，验证码/短信/邮件收尾留后续任务。

## Scope（本轮 MVP）

**做：**
- 复用 `src/services/email.py` 的 `create_temp_email()` 生成 mail.tm 邮箱。
- 用 Patchright 有头浏览器打开 GitHub signup 页。
- 自动填写注册表单必填字段（邮箱、密码、用户名，以及页面要求的其余字段如产品更新订阅选项）。
- 逐步推进 GitHub 的分步表单（GitHub signup 为逐字段揭示式表单），点击提交。
- 运行到 Arkose FunCaptcha 验证码出现（或页面明确拒绝邮箱）为止，截图 + 落日志后停下。

**不做（后续任务）：**
- 解 Arkose FunCaptcha 验证码。
- 手机短信验证。
- 从 mail.tm 收 GitHub 注册验证邮件并回填 8 位验证码完成注册。
- 批量并发调度与 opencode OAuth 对接（本轮只跑单账号打通流程）。
- 不改动 `registration.py` 中 opencode 存根，不复用其 Cloudflare 语义。

## Constraints

- 技术栈固定：Patchright（Playwright 反检测 fork）有头 Chrome，复用 `driver.py` 的
  `create_driver()` 及 `_safe_goto/_safe_fill/_safe_click/_wait_visible` 等通用 helper。
- GitHub signup 表单结构未知且可能 A/B，选择器必须以「实跑侦察 DOM」为准，不得凭空臆造。
- mail.tm 域名有较高概率被 GitHub 在邮箱校验阶段直接拒收（"Email is invalid or already taken"）；
  这属于外部限制，不是脚本缺陷——脚本须能识别该状态并如实报告，不得当作成功。
- 不得引入新的第三方打码/接码依赖（本轮不解验证码）。

## Acceptance Criteria

- [ ] 提供一个可独立运行的入口（脚本或函数），单次调用完成：建 mail.tm 邮箱 → 打开 signup 页 →
      填表 → 提交，推进到验证码出现为止。
- [ ] 表单字段选择器来自实跑侦察确认，能在当前 GitHub signup 页真实填入邮箱/密码/用户名。
- [ ] 运行到 Arkose 验证码出现时，脚本主动停下并输出：所用邮箱、当前 URL、状态截图路径、明确的
      「已到达验证码」日志，而非崩溃或空转。
- [ ] 若 GitHub 在邮箱/用户名校验阶段拒绝，脚本识别并输出该拒绝原因，返回可区分的失败状态
      （「被 GitHub 拒绝」vs「脚本异常」）。
- [ ] 全流程日志清晰（每一步做了什么、结果如何），便于后续接入验证码/邮件收尾。

## Notes

- 侦察阶段允许实跑真实 GitHub 页面观察 DOM；这是确定选择器的唯一可靠方式。
- 产出的 GitHub 页面操作代码应与 driver.py 的 Cloudflare LEGACY 方法隔离，放独立模块，
  避免污染既有 opencode 改造面。
