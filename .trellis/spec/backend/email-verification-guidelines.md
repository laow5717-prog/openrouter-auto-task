# Email Verification Guidelines

> mail.tm 收码与 Cloudflare 邮箱验证的约定。踩过的坑都在这里。

---

## 两种验证码，不要混用

| 场景 | 页面 | 码长 | 取码函数 |
|---|---|---|---|
| 注册邮箱验证 | 注册流程验证页 | 6 位 | `wait_for_verification_email` |
| 登录二次验证 | `dash.cloudflare.com/two-factor?type=email` | **7 位** | `wait_for_login_code` |

两个取码函数**刻意不合并**：注册那个「链接优先于验证码」且不做时间过滤，
语义与登录场景冲突；合并会波及线上稳定运行的注册流程。

## 坑 1：验证码长度不固定，正则不能写死 6 位

`extract_verification_code`（`src/utils.py`）曾用 `(\d{6})` 兜底，
遇到 7 位码 `5221150` 会截出 `522115` —— 一个**必然失败且难以察觉**的码。
日志里还会显示 "Found verification code: 522115"，看起来一切正常。

现用 `(?<!\d)(\d{6,8})(?!\d)` 锚定数字边界。
**新增码型时先确认位数**，别假设是 6 位。

## 坑 2：收件箱有历史验证码，必须按时间过滤

账号收件箱通常已积压多封 `Your Cloudflare login token: XXXXXXX`。
只按发件人匹配会**立刻返回一个早已过期的旧码**，且不会报错——
表现为"验证码填了但被拒"，极易误判成别的问题。

`wait_for_login_code` 用 `since_ts` 闸门解决：

- `since_ts` 必须在**点击登录按钮之前**取。提交后才取会漏掉已到达的邮件，白等到超时。
- `createdAt` 解析失败的邮件一律**跳过**，绝不能当作新邮件放行——那正是闸门要防的。

## 坑 3：清空 browser profile 会触发二次验证

`data/profiles/<email>/` 存着 Cloudflare 的信任设备 cookie。
删掉 profile 后再登录会被判定为新设备，跳转 `two-factor?type=email`。

由 `login_cloudflare(driver, email, password, email_password)` 自动处理。
`email_password` 不传则遇到 2FA 页直接登录失败——三个调用方均已从
`AccountModel.get_email_password()` 取值传入。

**复验注意**：一旦登录成功，profile 拿到信任 cookie，
**再次登录不会触发 2FA**。要重现必须先删 profile。

## mail.tm token

老账号注册时的 token 未落库。用 `get_mail_token(address, password)`
以 `email_password` 现换，**不缓存、不落库**。
