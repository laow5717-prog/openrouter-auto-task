# 登录邮箱二次验证自动化

## Goal

让 `login_cloudflare` 能自动通过 Cloudflare 的邮箱二次验证（`dash.cloudflare.com/two-factor?type=email`），
使清空过 profile 的账号无需人工介入即可完成登录。

## Background

2026-07-19 清除账号 `gcjpmyg59l@web-library.net` 的浏览器 profile 后，登录被 Cloudflare 判定为新设备，
跳转到 `two-factor?type=email` 页要求邮箱验证码。当前代码对 2FA 页零处理：
[driver.py:1495-1520](../../../src/browser/driver.py#L1495-L1520) 提交表单后只轮询 URL 里的 account_id，
空转约 35 秒后返回 `None`。

实测该账号 mail.tm 收件箱已积压 5 封 `Your Cloudflare login token: XXXXXXX`，
证明 2FA 邮件确实在发送，缺的只是「读取 + 填入」这一环。

## Requirements

### R1 — 识别 2FA 页
登录提交后，若 URL 落到 `two-factor`，进入 2FA 处理分支，而不是继续空转轮询 account_id。

### R2 — 取回验证码
用账号已存库的 `email_password` 调 `POST https://api.mail.tm/token` 现换 token（不落库，不改表结构），
再拉取收件箱取验证码。

### R3 — 只认新邮件（关键）
收件箱内已存在多封历史验证码邮件。取码时必须按邮件 `createdAt` 过滤，
只接受**本次登录动作发起之后**到达的邮件。否则会立刻返回一个早已过期的旧码。

### R4 — 验证码位数修正（关键）
实测 Cloudflare 登录验证码为 **7 位**，而 [utils.py:140-144](../../../src/utils.py#L140-L144)
的正则只匹配 `\d{6}`——对 `5221150` 会截出 `522115`，是一个必然错误的码。
提取逻辑须能正确处理 7 位码，且不得破坏注册流程依赖的 6 位码提取。

### R5 — 填入并提交
复用 [driver.py:3080](../../../src/browser/driver.py#L3080) `handle_email_verification` 的填码路径，
填入后回到既有的 account_id 轮询逻辑确认登录结果。

### R6 — 失败即登录失败
任何一步失败（页面无输入框、超时未收到新邮件、填入后仍未跳转），
`login_cloudflare` 返回 `None`，与现有失败行为一致。不新增错误码，不阻塞等待人工输入。

### R7 — 全局生效
只改 `login_cloudflare` 内部，使三个调用方
（[routes.py:427](../../../src/api/routes.py#L427)、[registration.py:325](../../../src/services/registration.py#L325)、
[registration.py:442](../../../src/services/registration.py#L442)）自动获得该能力。

## Constraints

- 不改数据库表结构，不缓存 mail.tm token。
- 不改动注册流程既有的邮箱验证行为（该路径已在线上稳定运行）。
- `login_cloudflare` 现签名为 `(driver, email, password)`，缺 `email_password`。
  新增参数必须带默认值，保证三个调用方不传时行为与今天完全一致。
- 并发模型不变：不得引入需要人工介入的阻塞等待。

## Acceptance Criteria

- [ ] AC1 `extract_verification_code` 对 7 位码 `5221150` 返回 `5221150`，不是 `522115`
- [ ] AC2 `extract_verification_code` 对既有 6 位码用例仍返回原值（回归不破）
- [ ] AC3 取码函数在收件箱有历史旧邮件时，不返回旧码；只返回指定时间点之后到达的码
- [ ] AC4 指定时间点之后无新邮件时，在超时后返回 `None`，不抛异常
- [ ] AC5 `login_cloudflare` 不传 `email_password` 时，行为与改动前一致（三个调用方不受影响）
- [ ] AC6 实跑：`gcjpmyg59l@web-library.net` 在 profile 已清空的状态下，
      经由 2FA 页自动完成登录并取得 account_id
- [ ] AC7 实跑失败时返回 `None` 且日志能看出卡在哪一步，不静默空转

## Open Questions

- 用户报告 2FA 页「白屏」。但邮件确实发出，说明页面 JS 至少执行了。
  实现前需先探明该页 DOM：是真无输入框，还是仅渲染慢/延迟挂载。
  若确认无可填元素，R5 需改为其他提交方式，届时回到本 PRD 修订。
