# 技术设计

## 改动边界

只动一个文件加一个新测试：

- `src/platforms/opencode/login.py` — 新增检测 + 恢复，改写兜底分支。
- `tests/test_opencode_auth_recovery.py` — 新增。

`src/platforms/opencode/billing.py` **不改**。它的 `ensure_opencode_session` 在 GitHub 登录
完成后调用 `login_and_open_own_go`，而后者内部第一件事就是重新 `get("https://opencode.ai/auth")`
（[login.py:170](../../../src/platforms/opencode/login.py#L170)），已经是新流程的起点；
C2（state 陈旧）的修复靠 R2 的恢复重试兜住即可，不需要在 billing 侧再加一层。

## 新增函数

### `_auth_broken(session) -> bool`

与 `_account_flagged` 同构：短 timeout 读 body 文本，异常吞掉返回 False。

```
判据（正文小写）：
  "unknown state" in body
  or ("cookies expired" in body and "authentication flow" in body)
```

不限定域名，理由与 `_account_flagged` 一致（第 143 行注释）：错误页可能出现在
`auth.opencode.ai`，也可能在回跳链路的中间态。`unknown state` 这个短语在正常的 opencode /
GitHub 页面里不会出现，误报风险可以接受。

第二条判据是防文案微调的冗余分支，两条都写。

### `_clear_opencode_cookies(session) -> bool`

```python
ctx = session.page.context
for domain in ("opencode.ai", "auth.opencode.ai"):
    ctx.clear_cookies(domain=domain)
```

Playwright 1.61（pyproject 锁 `>=1.61.0`）支持 `clear_cookies(domain=)` —— 已实测本地签名为
`(self, *, name=None, domain=None, path=None)`。

**运行环境只考虑 AdsPower**（2026-08-08 用户定调：以后全部走 AdsPower，本地 Chrome 路径不再
适配、不再验证）。AdsPower 走 `connect_over_cdp` 取 `browser.contexts[0]` 组装同构的
`BrowserSession`（[adspower_driver.py:357-374](../../../src/browser/adspower_driver.py#L357-L374)），
`page.context.clear_cookies` 与本地栈同构，无需分支。
**绝不能调无参 `clear_cookies()`** —— 那会连 GitHub 登录 cookie 一起抹掉，逼出一次完整重登
加一次新设备邮箱验证（实测数分钟 + 一封验证码）。整个函数 guard 异常，失败返回 False，
恢复流程照走（只是少了这一层加强）。

### `_recover_auth(session, monitor, attempt) -> None`

attempt 从 1 起。attempt >= 2 时先清 cookie，然后一律 `session.get("https://opencode.ai/auth")`
并短睡，让页面重新走 authorize 拿新鲜 state。走 `_step` 留痕。

## 主流程接入点

`login_and_open_own_go` 结构不变，插入三处：

1. **OAuth 跳转等待之后**（现第 203-204 行 `_wait_until` 离开 github.com 之后、
   第 207 行取 wid 之前）：查一次 `_auth_broken`，命中即恢复并回到步骤 2 重走
   `Continue with GitHub`。
2. **provision 重试循环内**（现第 214-234 行 `for` 循环）：每轮在 `_account_flagged`
   之后并列查 `_auth_broken`。命中则调 `_recover_auth`，**不计入** provision 重试的 15 次
   预算语义上的「等 provision」——但为简单起见仍复用同一个循环，只是把该轮的
   `session.get("https://opencode.ai/auth")` 换成恢复动作。
3. **末次兜底失败时**：若最终仍无 wid 且 `_auth_broken` 命中，detail 写
   `opencode 认证 state 失效（OpenAuth unknown state），N 次恢复未果`。

恢复次数用一个局部计数器 `recover_n` 控制，上限常量 `_MAX_AUTH_RECOVER = 2`。超限后不再
恢复，让循环自然走完并返回失败——避免在坏状态里无限重来。

### 为什么把上限定在 2

第 1 次重来解决 C2（陈旧 state，重开 authorize 即好）；第 2 次带清 cookie，解决 cookie 本身
损坏/串台。两次都不行说明是 opencode 服务端或代理层问题，继续重试只是把单账号耗时线性放大——
充值流水线是并发轮转的，账号失败一次会在下一轮被重试，不需要在单次调用里死磕。

## 改写 `_click_continue_github`

```
1) get_by_role("link", name="Continue with GitHub") → 点到即 True
2) 找不到：session.get("https://opencode.ai/auth") + sleep，再找一次 → 点到即 True
3) 仍找不到 → False（调用方报「未能点到 Continue with GitHub」）
```

删掉 `page.goto("https://auth.opencode.ai/github/authorize")`。这一跳是 C1 的确定性成因：
它让浏览器在 OpenAuth 没种 state 的情况下发起 GitHub 授权，回调必炸。

丢失的能力：原兜底能应付「链接文案变了但 `/github/authorize` 仍可用」的情形。实际上那种
情形下裸 goto 也只会撞进 unknown state，所以没有真正丢失可用路径。

## 可测性

沿用 `tests/test_captcha_detection.py` 的假对象风格（不起浏览器）：

- `FakeBody` 页面对象：`inner_text(sel, timeout)` 返回预设文本，可配置抛异常。
- `FakeContext`：记录 `clear_cookies` 的每次调用参数，供断言「只清了 opencode 域」。
- `FakeSession`：`current_url` 可按脚本逐次返回不同 URL（模拟「第一次停错误页、恢复后
  回落 workspace」），`get()` 记录导航历史。

`_wait_until` 的 `poll=1.5` 会让测试变慢。测试里对涉及等待的用例传小 timeout，或直接对
`_auth_broken` / `_click_continue_github` / `_clear_opencode_cookies` 做单元测试，
主流程只测一条「错误页 → 恢复 → 成功取 wid」的路径并把 timeout 压到几秒。

## 兼容性

- 函数签名全部不变（`login_and_open_own_go(session, monitor=None, timeout=240, open_go=True)`），
  `tests/test_captcha_detection.py` 的签名断言不受影响。
- 返回 dict 的键不变，只是 `detail` 文案在新分支下更具体。
- 无配置项新增，无 DB 变更。

## 回滚

单文件改动，`git revert` 即可。回滚后行为退回「停在错误页空转到失败」——不会更坏。
