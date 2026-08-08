# 执行计划

## 步骤

### 1. 新增检测与恢复原语（`src/platforms/opencode/login.py`）

- [ ] 顶部加常量 `_MAX_AUTH_RECOVER = 2`。
- [ ] 加 `_auth_broken(session)`：短 timeout 读 body，小写后匹配 `unknown state`
      或（`cookies expired` + `authentication flow`）。异常返回 False。
      docstring 写清为什么不限定域名。
- [ ] 加 `_clear_opencode_cookies(session)`：只对 `opencode.ai` / `auth.opencode.ai`
      调 `context.clear_cookies(domain=...)`。docstring 写清**为什么不能全清**
      （会抹掉 GitHub 登录态 → 重登 + 再触发一次新设备邮箱验证）。
- [ ] 加 `_recover_auth(session, monitor, attempt)`：attempt >= 2 先清 cookie，
      然后 `session.get("https://opencode.ai/auth")` + `time.sleep(3)`，全程 `_step` 留痕。

### 2. 改写 `_click_continue_github`

- [ ] 删掉 `page.goto("https://auth.opencode.ai/github/authorize")` 兜底。
- [ ] 换成：重载 `https://opencode.ai/auth` → sleep → 再找一次链接 → 点到即 True。
- [ ] docstring 记录为什么删裸 goto（C1：绕过 authorize，OpenAuth 无 state，回调必炸）。

### 3. 主流程接入（`login_and_open_own_go`）

- [ ] 函数内加局部 `recover_n = 0`。
- [ ] OAuth 跳转等待之后、取 wid 之前：查 `_auth_broken`，命中且未超限 → `_recover_auth`
      并重走 `_click_continue_github` + 等待。
- [ ] provision 重试循环内：在 `_account_flagged` 之后并列查 `_auth_broken`，命中且未超限
      → 该轮改走 `_recover_auth`（替代原本的 `session.get(".../auth")`）。
- [ ] 末次仍无 wid 时：先查 `_auth_broken`，命中则 detail 写
      `opencode 认证 state 失效（OpenAuth unknown state），已恢复 N 次未果，停在 <url>`。
      顺序上放在 `_account_flagged` 检查之后（flagged 是更确定的终态）。

### 4. 测试（`tests/test_opencode_auth_recovery.py`，新增）

- [ ] `FakeSession` / `FakePage` / `FakeContext`：正文可配、`current_url` 可脚本化、
      `get()` 记录导航、`clear_cookies` 记录参数。
- [ ] `test_auth_broken_detects_unknown_state` — 真实文案命中。
- [ ] `test_auth_broken_detects_alternate_wording` — `cookies expired` + `authentication flow`。
- [ ] `test_auth_broken_false_on_normal_page` — workspace 页 / GitHub 授权页不命中。
- [ ] `test_auth_broken_false_when_read_raises` — 读正文抛异常返回 False。
- [ ] `test_clear_cookies_scoped_to_opencode_domains` — 断言只按 domain 清，
      且**没有**无参调用。
- [ ] `test_no_bare_goto_in_click_continue_github` — 读 `_click_continue_github` 源码
      （去掉 docstring）断言不含 `.goto(` / `github/authorize`，守住 R3 不被回退。
      docstring 要留着那个 URL 解释「为什么不能这么做」，所以判据看函数体不看全模块。
- [ ] `test_recovers_from_error_page_then_gets_wid` — 脚本化 URL：先错误页 → 恢复后
      回落 `opencode.ai/workspace/wrk_xxx`，断言 `ok`/`wid` 正确且发生过恢复导航。
      传小 timeout 压测试时长。
- [ ] `test_detail_mentions_state_when_recovery_exhausted` — 恒错误页时
      `ok=False` 且 detail 含 state 字样。

### 5. 验证

```bash
.venv/bin/python -m pytest tests/test_opencode_auth_recovery.py -q
.venv/bin/python -m pytest tests/ -q          # 全量，确认 542 项无回归
```

单个新测试文件应在数秒内跑完；若超过 30 秒说明主流程用例的 timeout 没压下去，回到步骤 4 调。

## 审查关卡

- 步骤 1 完成后：确认 `clear_cookies(domain=)` 在本地 playwright 1.61 上确实可用
  （`.venv/bin/python -c "import inspect, playwright.sync_api as p; print(inspect.signature(p.BrowserContext.clear_cookies))"`）。
  若签名不支持 domain，改用逐条读 cookie 过滤后 `add_cookies` 重放的方式，并回到 design 更新。
- 步骤 3 完成后：人工通读 `login_and_open_own_go`，确认恢复分支不会与 provision 重试
  互相抵消（恢复消耗的是同一个 15 轮循环预算，但每次恢复都伴随一次真实导航，不是空转）。

## 实施中发现的额外缺陷（已一并修）

`_oauth_leg` 里等待离开 auth host 的 `_wait_until` 原本传
`max(10, int(deadline - time.time()))` —— **把剩余全部预算整块交给一次等待**。停在
OpenAuth 错误页时 URL 根本不变，这一次等待就能吃光 240 秒总预算，后面的恢复重试连跑的
机会都没有。是假时钟测试把它逼出来的（恢复次数恒为 0）。

修法：加 `_budget(deadline, cap, floor)` helper + `_HOP_WAIT_CAP = 45`，单跳等待封顶
45 秒。OAuth 重定向链正常是秒级，45 秒只是给走代理时留余量。

## 回滚点

- 步骤 2 单独可回滚（只删了一个兜底分支）。
- 步骤 3 若引入回归，可只回滚主流程接入，保留步骤 1 的检测原语（它们是纯新增，无副作用）。
