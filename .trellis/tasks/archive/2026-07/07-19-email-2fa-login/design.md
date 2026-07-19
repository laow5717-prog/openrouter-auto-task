# 技术设计 — 登录邮箱二次验证自动化

## 改动边界

三个文件，各自职责不交叉：

| 文件 | 改动 | 性质 |
|---|---|---|
| `src/utils.py` | `extract_verification_code` 支持 7 位 | 修缺陷 |
| `src/services/email.py` | 新增 `get_mail_token`、`wait_for_login_code` | 新增，不动老函数 |
| `src/browser/driver.py` | `login_cloudflare` 加 2FA 分支 | 扩展 |

不改：数据库、API 路由、前端、注册流程。

## 1. utils.py — 验证码提取

现状缺陷：`patterns` 里三条正则都是 `\d{6}`，兜底的 `(\d{6})` 会从 7 位码前 6 位截断。

设计：把兜底改为**边界锚定的变长匹配**，优先长码。

```
r'login token[:\s]*(\d{6,8})'     # Cloudflare 登录码，最specific
r'code is\s*(\d{6,8})'
r'verification code[:\s]*(\d{6,8})'
r'(?<!\d)(\d{6,8})(?!\d)'          # 兜底：前后不得再有数字
```

`(?<!\d)...(?!\d)` 是关键——它保证 `5221150` 只能整体匹配，不会截出 `522115`。
对既有 6 位码，独立的 6 位数字仍然整体命中，行为不变（AC2）。

风险：邮件正文若含其他 6~8 位数字（如金额、ID），兜底可能误匹配。
缓解：把 `login token` 模式放最前，Cloudflare 登录邮件主题恒为
`Your Cloudflare login token: XXXXXXX`，会在兜底之前命中。

## 2. email.py — 取码

### `get_mail_token(address, password) -> str | None`
`POST {MAIL_TM_API}/token`，body `{address, password}`，取 `token` 字段。
已实测该账号返回 200。失败返回 `None`，不抛异常。

与 `create_temp_email` 里那段取 token 的代码逻辑相同，但**不抽公共函数**——
`create_temp_email` 已稳定运行，改它属于无谓风险。接受这点重复。

### `wait_for_login_code(token, since_ts, timeout=None) -> str | None`

与 `wait_for_verification_email` 分开写，不复用。理由：老函数「链接优先于验证码」
且不过滤时间，语义与 2FA 场景冲突；改它会波及注册流程（违反 Constraints）。

轮询逻辑：

```
while not timeout:
    for msg in fetch_emails(token):
        if parse(msg.createdAt) <= since_ts:  continue   # R3 时间闸门
        if 'cloudflare' not in sender:        continue
        code = extract_verification_code(subject) or extract_verification_code(detail)
        if code: return code
    sleep(poll_interval)
return None
```

**时间闸门是本设计的核心**。`since_ts` 由调用方在**点击登录按钮之前**取，
传 UTC aware datetime。mail.tm 的 `createdAt` 形如 `2026-07-19T00:03:48+00:00`，
用 `datetime.fromisoformat` 可直接解析（Python 3.11+ 支持该格式，本项目 3.14）。

边界：解析 `createdAt` 失败的邮件一律**跳过**（视为不可信），不能 fallback 成「当作新邮件」——
那会让旧码漏进来，正是 R3 要防的。

## 3. driver.py — login_cloudflare 2FA 分支

签名扩展（默认值保证 R7/AC5）：

```
def login_cloudflare(driver, email, password, email_password=None):
```

插入位置：`time.sleep(5)` + `check_and_handle_cf_challenge` 之后、
现有 account_id 轮询之前（约 [driver.py:1496](../../../src/browser/driver.py#L1496)）。

流程：

```
since_ts = utcnow()          # ← 必须在点登录按钮 *之前* 取
... 提交登录表单 ...
if 'two-factor' in driver.current_url:
    if not email_password:  → 打印提示，跳过（保持旧行为）
    token = get_mail_token(email, email_password)     → 失败则 return None
    code  = wait_for_login_code(token, since_ts)      → 失败则 return None
    handle_email_verification(driver, code)           → 失败则 return None
# 落回既有 account_id 轮询，不改动
```

`since_ts` 取值时机是正确性要害：若在提交后才取，可能错过已到达的邮件而超时。

复用 `handle_email_verification`（[driver.py:3080](../../../src/browser/driver.py#L3080)），
**但必须先扩充其输入框选择器**——见下节。提交按钮 `button[type="submit"]` 已可用，不改。

失败一律 `return None`（R6），不新增错误类型。

## 3b. 选择器修正【侦察后新增】

侦察（implement.md 步骤 1）证实 2FA 页真实输入框为：

```
name="twofactor_token"  type="text"  autocomplete="off"  inputmode="numeric"
data-testid="email-mfa-login-input-2fa-code"
```

`handle_email_verification` 现有三个选择器
（`input[name="code"]`、`input[type="text"][maxlength="6"]`、`input[autocomplete="one-time-code"]`）
**全部失配**。需追加：

```
input[name="twofactor_token"]
input[data-testid="email-mfa-login-input-2fa-code"]
```

采用**追加**而非替换：原三个选择器服务于注册流程的邮箱验证页，不能动（Constraints）。
locator 以逗号并列，`.first` 取先命中者，两个场景互不干扰。

## 白屏问题：已排除

用户报告的「白屏」经侦察不成立。页面 html 213KB、结构完整，
0/5/15/30s 四次采样一致，无延迟挂载现象。判定为当时的瞬时加载态或偶发。
设计不为此增加额外重试。

## 兼容性与回滚

- 三个调用方均不传 `email_password` → 走 `if not email_password` 分支 → 行为与今天逐字节一致。
- 回滚：`git revert` 单个 commit 即可，无数据迁移、无状态残留。
- 唯一有外溢风险的是 `extract_verification_code`（注册流程共用），故 AC2 专门守它。
