# 修复 opencode OAuth「unknown state」错误页导致的卡死

## Goal

充值流水线里，浏览器会停在 opencode 认证服务（`auth.opencode.ai`，SST OpenAuth）的错误页：

> The browser was in an unknown state. This could be because certain cookies expired
> or the browser was switched in the middle of an authentication flow.

含义是 OAuth 回调回来时，OpenAuth 找不到它在 `/authorize` 阶段种下的 state cookie。

现在的代码**完全识别不出这一页**：[login.py:207](../../../src/platforms/opencode/login.py#L207)
的 `_wait_until` 只读 URL 不读正文，浏览器停在错误页时会一路空转到「未能取到自己的
workspace id」，随后 15 轮 provision 重试全部在同一个坏状态里打转，白耗上百秒后把账号
判失败。用户在 UI 实时画面里看到的就是浏览器长时间挂在这一页。

## 现场证据

2026-08-08 16:15 那轮（`server.log` 尾部）：4 个账号并发，三个打了
「[opencode] 未登录，点 Continue with GitHub」后再无后续状态输出，只有一个走到
「已登录，自己的 workspace = wrk_01KZ8E1WBFG342HH0ZXN6WHF3W」。

## 根因（两个入口，都要堵）

### C1 裸 goto 兜底跳过了 authorize 初始化

[login.py:66-71](../../../src/platforms/opencode/login.py#L66-L71)，`_click_continue_github`
找不到「Continue with GitHub」链接时直接：

```python
page.goto("https://auth.opencode.ai/github/authorize", ...)
```

这个 URL 绕过 opencode 侧的 `/authorize`，OpenAuth 没机会种 state cookie，GitHub 回调
回来必然报 unknown state。**这是确定性的构造错误，不是概率问题。**

### C2 state 陈旧

[billing.py:117](../../../src/platforms/opencode/billing.py#L117) `ensure_opencode_session`
先 `get("https://opencode.ai/auth")` 开了一个 OAuth 流程（种下 state cookie），然后中途跳去
`github.com/login` 做登录 + 新设备邮箱验证。这段实测耗时数分钟（收码等待上限 180s + 回填
+ 60s 等跳转）。等回过头再走 OAuth，第一次种的 state 可能已过期或与新流程串台——正是错误
文案里说的「in the middle of an authentication flow」。

## Requirements

### R1 识别错误页

- 在 `src/platforms/opencode/login.py` 新增 `_auth_broken(session)`，按**正文文本**判定
  （与既有 `_account_flagged` 同构，不限定域名——错误页可能落在 `auth.opencode.ai`
  或回跳中的其他 opencode 域）。
- 判据：正文小写后含 `unknown state`，或同时含 `cookies expired` 与 `authentication flow`。
- 读正文要 guard 异常并带短 timeout，导航中读失败一律返回 False，不能把正常流程判坏。

### R2 命中即恢复，不空转

- `login_and_open_own_go` 在两处查 `_auth_broken`：OAuth 跳转等待之后、以及 provision
  重试循环的每一轮（与 `_account_flagged` 并列）。
- 恢复动作分级，最多 2 次：
  1. 第 1 次：重新 `session.get("https://opencode.ai/auth")` 从头走一遍，拿新鲜 state；
  2. 第 2 次：先清掉 opencode 相关域的 cookie 再重来。
- 清 cookie **只能清 opencode 域**（`opencode.ai` / `auth.opencode.ai`）。全清会连带
  抹掉 GitHub 登录态，导致重新登录 + 再触发一次新设备邮箱验证，代价是几分钟加一封验证码。
- 2 次恢复都没过，如实返回失败，detail 写明是 OpenAuth state 问题，不要伪装成
  「未能取到 workspace id」。

### R3 去掉 C1 的裸 goto 兜底

- `_click_continue_github` 找不到链接时，改为重新 `session.get("https://opencode.ai/auth")`
  拿新鲜 authorize 页后再找一次链接；仍找不到才返回 False。
- 代码里不再出现 `auth.opencode.ai/github/authorize` 这个字面量。

### R4 留痕

- 恢复动作走 `_step`（同时进 print 与 monitor），日志里能看出「命中 OpenAuth 错误页 →
  第 N 次重来」。现在这条路径静默无输出，是排查不出来的直接原因。

## Non-goals

- 不动 GitHub 注册/登录/设备验证链路本身（`src/browser/github_signup.py`）。日志里
  「未等到可点提交按钮，尝试在输入框按 Enter 提交」是另一个可疑点，但它已有 60s 离开
  验证页的确认，本任务不扩到那里。
- 不动 infron 适配器。
- 不改并发数或 AdsPower 环境复用策略。

## Acceptance Criteria

- [ ] `_auth_broken` 对真实错误页文案返回 True；对正常 workspace 页、GitHub 授权页、
      flagged 页均返回 False；正文读取抛异常时返回 False。
- [ ] `_click_continue_github` 的**函数体**里不再有 `.goto(` 调用，也不再有
      `github/authorize`（docstring 里保留这个 URL 用于解释「为什么不能这么做」）。
- [ ] 停在错误页的假 session 跑 `login_and_open_own_go`：触发恢复重试，且在恢复后能
      正常取到 wid（用假 session 模拟第 2 次访问回落 workspace）。
- [ ] 恢复始终失败时，返回 `ok=False` 且 `detail` 含 OpenAuth state 字样，
      不再是「未能取到自己的 workspace id」。
- [ ] 清 cookie 只针对 opencode 域调用，不做全局 `clear_cookies()`（用假 context 断言
      调用参数）。
- [ ] 既有测试全绿，特别是 `tests/test_captcha_detection.py` 中对
      `login_and_open_own_go` / `ensure_opencode_session` 签名的断言不被破坏。
