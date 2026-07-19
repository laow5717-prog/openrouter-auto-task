# 执行计划 — 登录邮箱二次验证自动化

## 步骤

### 1. DOM 侦察【阻塞门，必须先做】

有头浏览器登录 `gcjpmyg59l@web-library.net`，走到 `two-factor?type=email`，导出该页 DOM 与 console 错误。

判定：
- 有可填输入框 → 记下真实选择器，继续步骤 2
- 确实无输入框 → **停止**，回到 prd.md 修订 R5，不要硬写

产出：把选择器和结论追加到本文件末尾的「侦察结果」。

> 该步会真实触发一封 2FA 邮件，属预期。

### 2. utils.py — 验证码提取支持 7 位

改 `extract_verification_code` 的 patterns（见 design.md 第 1 节）。

验证（AC1/AC2）：
```bash
.venv/bin/python3 -c "
from src.utils import extract_verification_code as f
assert f('Your Cloudflare login token: 5221150') == '5221150'
assert f('Your code is 123456') == '123456'
assert f('verification code: 987654') == '987654'
print('OK')
"
```

### 3. email.py — 新增取码函数

新增 `get_mail_token`、`wait_for_login_code`。**不改动**现有函数。

验证（AC3/AC4）——用真实收件箱，那 5 封旧邮件正是天然测试数据：
```bash
.venv/bin/python3 -c "
from datetime import datetime, timezone
from src.services.email import get_mail_token, wait_for_login_code
t = get_mail_token('gcjpmyg59l@web-library.net','Z7zcAXBqb8RapRaX')
assert t, 'token 获取失败'
# 闸门设在未来 → 必须超时返回 None，证明旧码被挡住（AC3+AC4）
r = wait_for_login_code(t, datetime(2030,1,1,tzinfo=timezone.utc), timeout=8)
assert r is None, f'旧码泄漏: {r}'
print('OK')
"
```

### 4. driver.py — login_cloudflare 加 2FA 分支

按 design.md 第 3 节改。注意 `since_ts` 必须在**点击登录按钮之前**取。

验证（AC5）——签名向后兼容：
```bash
.venv/bin/python3 -c "
import inspect
from src.browser.driver import login_cloudflare as f
p = inspect.signature(f).parameters
assert p['email_password'].default is None
print('OK')
"
```

### 5. 实跑验证（AC6/AC7）

对 `gcjpmyg59l@web-library.net` 跑一次完整登录，确认自动过 2FA 并拿到 account_id。

失败时看日志能否定位到具体环节（取 token / 等码 / 填码 / 跳转）。

### 6. 质量检查

`/trellis-check`，覆盖全部 AC。

## 回滚点

- 步骤 2 是唯一影响既有流程（注册）的改动。若注册出问题，单独 revert 步骤 2 即可，步骤 3/4 不依赖它生效路径之外的行为。
- 整体回滚：单 commit `git revert`，无数据迁移。

## 侦察结果

2026-07-19 实跑 `gcjpmyg59l@web-library.net`，落到 `two-factor?type=email`。

**结论：页面渲染正常，「白屏」假说不成立。** html 长度 213149，input×10，button×16，
0s/5s/15s/30s 四次采样结构完全一致，无延迟挂载。body 文案：

> Verify with your email — Enter the 7-digit code we just sent to your email.

页面文案自己确认了**7 位**码，佐证 R4。

### 验证码输入框（唯一可见 input）
```
id/name = twofactor_token
type = text        autocomplete = off       inputmode = numeric
aria-labelledby = email-mfa-label
data-testid = email-mfa-login-input-2fa-code
```

**现有选择器全部失配**：`handle_email_verification` 找的
`input[name="code"]` / `input[type="text"][maxlength="6"]` / `input[autocomplete="one-time-code"]`
三者均不命中（无 maxlength、autocomplete 为 off、name 不是 code）。
→ 必须扩充选择器，否则 `_wait_visible` 干等 30s 后失败。

### 提交按钮
`type="submit"` + `data-testid="email-mfa-login-submit-button"`，文本 `Verify`。
现有 `button[type="submit"]` 路径可用，无需改动。

页面另有 `Resend` 按钮（本次不使用）。

存档：`scratchpad/2fa_page.html`。截图因字体加载超时未存，不影响结论。

## 实跑结果（步骤 5）

2026-07-19 实跑 PASS，耗时 284s，`account_id = 598c19a45f0a0940fe3ebba7b577eafc`。
自动完成：识别 2FA 页 → 换 token → 收到 8422156（7 位）→ 填入 → 跳转 home。

### 已知噪声：验证码提交按钮点击失败 3 次（不修，非缺陷）

日志会出现 `⚠️ 点击[验证码提交] 失败 [尝试1~3/3]`，但登录仍成功。

机制已查明，非偶然：`_safe_click` 重试耗尽后 `raise`，
[driver.py:3126](../../../src/browser/driver.py#L3126) 的 `except` 兜底执行
`code_input.press("Enter")`，表单由回车提交。

点击失败的原因不是按钮 disabled（存档 HTML 证实该按钮无 disabled 属性，
`disabled:opacity-50` 只是 Tailwind 变体类名），推断为 OneTrust cookie 同意横幅
（页面含 9 个隐藏 `ot-*` 元素及 Confirm My Choices 按钮）遮挡，
导致 Playwright 可交互性检查超时。

**决定不修**，理由：
1. 回车兜底已实跑验证可靠；
2. `handle_email_verification` 为注册流程共用，改它有回归风险；
3. profile 登录成功后已获得信任设备 cookie，**再次登录不再触发 2FA**，
   任何改动都无法在同等条件下复验，除非再清一次 profile 重跑。

若将来要优化，方向是先关闭 cookie 横幅，而非改选择器（选择器本身是对的）。
