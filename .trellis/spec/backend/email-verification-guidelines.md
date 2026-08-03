# Email Verification Guidelines

## hotmail inboxes are long-lived — always filter by time

The ruoanzhu-backed hotmail accounts are **real, permanent mailboxes**, not
throwaway mail.tm ones. A GitHub code from an earlier run stays in the inbox
forever, and both emails that matter look alike to the extractor:

- registration → `Your GitHub launch code`
- device verification → `[GitHub] Please verify your device`

Both carry an 8-digit code and both contain the word "github". Without a time
filter, `extract_github_code_from_emails` returns whichever it finds first — in
practice the stale one. The failure presents as *"we received a code but
verification failed"*, and it reproduces on every run, which makes it read like a
selector or form bug rather than a data-freshness one.

Observed 2026-08-03 in `cunninghamh22@hotmail.com`:

```
2026-08-03 05:34:23  [GitHub] Please verify your device-GitHub    ← previous run
2026-08-02 18:56:34  这是一封测试账号是否正常的邮件
```

Rules:

- Pass `since=` to `wait_for_github_launch_code_ruoanzhu`. Capture it **before
  the action that triggers the send** (before submitting the login form, before
  submitting the signup form) — taking it afterwards can filter out the very mail
  you are waiting for.
- `since` is a **naive local `datetime`**. The page renders
  `%Y-%m-%d %H:%M:%S` in local time; do not hand it the UTC-aware `since_ts` the
  mail.tm path uses.
- When several GitHub mails qualify, the **newest** wins. The page happens to be
  ordered newest-first, but the extractor sorts by parsed time rather than trust
  that.
- `_MAIL_TIME_TOLERANCE_SEC` (90s) absorbs clock skew between the mail service
  and this machine. It is deliberately far smaller than the gap between two runs,
  so it never lets a previous run's code back in.
- If **no** mail has a parseable timestamp, the filter degrades to off with a
  warning. Risking one stale code beats never finding any code if the page
  structure changes.

Regression coverage: `tests/test_mail_code_freshness.py`.

---

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
